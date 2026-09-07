"""
Stage 2 - Automated review gate (60-second, no-narration format).

Runs an automated pass/fail check on a generated script before any paid
downstream work happens. This gate is deliberately FAIL-CLOSED: if the
review call errors, times out, or returns anything that can't be parsed
into the expected shape, the script is treated as REJECTED. An ambiguous
result is never treated as a pass.

Reviewer is Gemini (config.GEMINI_TEXT_MODEL), used as an LLM-as-judge on
top of the dependency-free structural checks in _basic_shape_check(). For
the richer new schema the judge now checks four things: culinary
plausibility, ingredient completeness/sanity, regional accuracy, and -
new - that each moment_description is genuinely SPECIFIC (not generic
filler) and that dish_fact is factually sound.

Expected input (<output_dir>/script.json from Stage 1):
    see stage1_script_generation.py docstring (dish_name, region,
    ingredients, dish_fact, subscribe_cta_text, segments[...], ...).

Expected output (<output_dir>/review.json):
    {
        "run_id": str,
        "approved": bool,
        "reasons": [str],
        "checked_at": iso8601 str,
        "raw_response": <whatever the reviewer returned, or null>
    }

Exit code is 0 only when approved is true. run_pipeline.py (and CI)
treat a non-zero exit as "stop the pipeline here".
"""
import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import (
    DURATION_CHECK_SLACK_SEC, GEMINI_TEXT_MODEL, HERO_CLIP_DURATIONS_SEC,
    HERO_CLIPS_PER_SHORT, MAX_DURATION_SEC, MIN_DURATION_SEC,
    SEGMENTS_MAX, SEGMENTS_MIN, STILLS_PER_SHORT_MAX, STILLS_PER_SHORT_MIN,
    get_env, run_output_dir,
)
from common.cost_logger import log_cost
from common.io_utils import load_json, save_json
from common.retry import retry_with_backoff

logger = logging.getLogger(__name__)

REVIEW_API_KEY_ENV = "GEMINI_API_KEY"
REQUIRED_SCRIPT_FIELDS = (
    "dish_name", "region", "ingredients", "dish_fact",
    "subscribe_cta_text", "segments", "estimated_duration_sec",
)
MAX_SUBSCRIBE_CTA_CHARS = 40


class ReviewFailure(Exception):
    """Raised for any condition that must be treated as fail-closed."""


REVIEW_PROMPT_TEMPLATE = """You are a strict quality-control reviewer for an \
Indian home-cooking YouTube Shorts channel. The video is a clean ~50-second, \
NO-NARRATION, NO-ON-SCREEN-TEXT piece: a continuous Studio-Ghibli-style cooking \
story told entirely through short hero clips and a couple of stills with their \
own natural sound - no voiceover and no captions. The "ingredients" and \
"dish_fact" fields are metadata for the YouTube listing only; they are never \
shown on screen. Review the script JSON below against EXACTLY these four \
criteria, and nothing else:

1. Culinary plausibility - is the technique, timing, and sequence of steps \
across the "segments" realistic and physically correct?
2. Ingredient completeness & sanity - are the ingredients real and compatible \
with the dish, AND does the "ingredients" list include EVERY ingredient named \
anywhere in any moment_description or dish_fact? Reject if an ingredient is \
referenced but missing from the list, or vice versa.
3. Regional accuracy - does this match how the dish is actually made in Indian \
home cooking (not a generic/foreign approximation)?
4. Moment specificity & fact soundness - is EACH "moment_description" specific \
and concrete (a real, filmable action or composition like "mustard seeds \
crackling in hot oil"), NOT vague filler like "cooking begins" or "tempering \
the spices"? AND is "dish_fact" factually accurate for this dish/region (no \
invented history)?

If you are not fully confident on all four, reject it. When in doubt, reject.

Do NOT reject for anything outside these four criteria. In particular this \
channel intentionally has NO spoken narration and NO on-screen text - do not \
treat their absence as a defect, and do not expect an ingredient-list moment. \
Judge only the four criteria above.

Script JSON:
{script_json}

Return ONLY a JSON object (no markdown fences, no commentary):
{{"approved": true or false, "reasons": ["specific reason", "..."]}}
"""


@retry_with_backoff(max_attempts=3, exceptions=(ReviewFailure,))
def call_review_model(script: dict) -> dict:
    """Calls Gemini as an LLM-judge reviewer. Raises ReviewFailure for any
    transport error, timeout, or malformed response so retry_with_backoff
    and the caller's fail-closed handling both engage correctly.
    """
    api_key = get_env(REVIEW_API_KEY_ENV)
    if not api_key:
        raise ReviewFailure(
            f"{REVIEW_API_KEY_ENV} not set - no reviewer configured yet. "
            "Failing closed by design; see SETUP.md."
        )

    prompt = REVIEW_PROMPT_TEMPLATE.format(script_json=json.dumps(script, indent=2))

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=GEMINI_TEXT_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        result = json.loads(response.text)
    except Exception as exc:
        raise ReviewFailure(f"Gemini review call failed: {exc}") from exc

    return result


def _basic_shape_check(script: dict) -> list:
    """Cheap, dependency-free sanity checks that run before any model call.

    Enforces the 60-second segment structure: SEGMENTS_MIN..SEGMENTS_MAX
    segments, of which EXACTLY HERO_CLIPS_PER_SHORT (4) are "video" hero
    clips - each checked against its OWN duration cap in
    HERO_CLIP_DURATIONS_SEC (in order) - and STILLS_PER_SHORT_MIN..MAX are
    "image" stills. A script asking for a different clip count is rejected
    here rather than causing a mismatch between what's requested and the
    Flow credits actually spent. Also verifies every segment carries a
    non-empty moment_description and a valid type, and that the timeline is
    contiguous over the target duration. (No text cards - this format burns
    in no on-screen text.)
    """
    problems = []
    for field in REQUIRED_SCRIPT_FIELDS:
        if not script.get(field):
            problems.append(f"missing or empty field: {field}")

    duration = script.get("estimated_duration_sec")
    lo = MIN_DURATION_SEC - DURATION_CHECK_SLACK_SEC
    hi = MAX_DURATION_SEC + DURATION_CHECK_SLACK_SEC
    if isinstance(duration, (int, float)) and not (lo <= duration <= hi):
        problems.append(f"estimated_duration_sec out of range ({lo}-{hi}): {duration}")

    subscribe_cta = script.get("subscribe_cta_text") or ""
    if len(subscribe_cta) > MAX_SUBSCRIBE_CTA_CHARS:
        problems.append(
            f"subscribe_cta_text too long ({len(subscribe_cta)} chars, "
            f"max {MAX_SUBSCRIBE_CTA_CHARS}): {subscribe_cta!r}"
        )

    segments = script.get("segments") or []
    if not (SEGMENTS_MIN <= len(segments) <= SEGMENTS_MAX):
        problems.append(
            f"expected {SEGMENTS_MIN}-{SEGMENTS_MAX} segments, found {len(segments)}"
        )

    video_segments = [s for s in segments if s.get("type") == "video"]
    image_segments = [s for s in segments if s.get("type") == "image"]

    if len(video_segments) != HERO_CLIPS_PER_SHORT:
        problems.append(
            f"expected exactly {HERO_CLIPS_PER_SHORT} video segments (Veo hero "
            f"clips, manually generated in Flow), found {len(video_segments)}"
        )
    else:
        for idx, vs in enumerate(video_segments):
            cap = HERO_CLIP_DURATIONS_SEC[idx]
            vs_duration = vs.get("end_sec", 0) - vs.get("start_sec", 0)
            if vs_duration > cap + 0.5:
                problems.append(
                    f"video segment {vs.get('id')} (hero clip {idx + 1}) duration "
                    f"{vs_duration}s exceeds its {cap}s cap"
                )

    if not (STILLS_PER_SHORT_MIN <= len(image_segments) <= STILLS_PER_SHORT_MAX):
        problems.append(
            f"expected {STILLS_PER_SHORT_MIN}-{STILLS_PER_SHORT_MAX} still-image "
            f"segments, found {len(image_segments)}"
        )

    # Per-segment content quality (structural only - the LLM judges whether
    # the descriptions are actually specific, not just present).
    for seg in segments:
        sid = seg.get("id")
        if not seg.get("moment_description"):
            problems.append(f"segment {sid}: missing/empty moment_description")
        if seg.get("type") not in ("video", "image"):
            problems.append(f"segment {sid}: type must be 'video' or 'image', got {seg.get('type')!r}")

    # Timeline must be contiguous and cover the duration (no gaps/overlaps).
    ordered = sorted(
        [s for s in segments if isinstance(s.get("start_sec"), (int, float))
         and isinstance(s.get("end_sec"), (int, float))],
        key=lambda s: s["start_sec"],
    )
    if len(ordered) == len(segments) and segments:
        prev_end = 0.0
        for seg in ordered:
            if abs(seg["start_sec"] - prev_end) > 0.5:
                problems.append(
                    f"segment {seg.get('id')}: timeline gap/overlap - starts at "
                    f"{seg['start_sec']}s, expected ~{prev_end}s"
                )
                break
            prev_end = seg["end_sec"]
        if isinstance(duration, (int, float)) and abs(prev_end - duration) > 1.0:
            problems.append(
                f"segments end at {prev_end}s but estimated_duration_sec is {duration}s"
            )

    return problems


def review_script(script_path: str, output_dir: str, run_id: str) -> dict:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    result = {
        "run_id": run_id,
        "approved": False,
        "reasons": [],
        "checked_at": now,
        "raw_response": None,
    }

    try:
        script = load_json(script_path)
    except (OSError, json.JSONDecodeError) as exc:
        result["reasons"].append(f"fail_closed: could not read/parse script.json: {exc}")
        save_json(Path(output_dir) / "review.json", result)
        return result

    shape_problems = _basic_shape_check(script)
    if shape_problems:
        result["reasons"] = [f"fail_closed: {p}" for p in shape_problems]
        save_json(Path(output_dir) / "review.json", result)
        return result

    try:
        raw = call_review_model(script)
        result["raw_response"] = raw
        if not isinstance(raw, dict) or "approved" not in raw:
            raise ReviewFailure(f"unparseable reviewer response: {raw!r}")
        result["approved"] = bool(raw["approved"])
        result["reasons"] = list(raw.get("reasons", []))
    except Exception as exc:  # noqa: BLE001 - anything at all here must fail closed
        result["approved"] = False
        result["reasons"].append(f"fail_closed: {exc}")

    log_cost(
        run_id=run_id, stage="stage2_review_gate",
        provider="gemini-api", model=GEMINI_TEXT_MODEL,
        units=1, unit_type="review_call", cost_usd=0.0,
        notes="approved" if result["approved"] else "rejected/fail-closed",
    )

    save_json(Path(output_dir) / "review.json", result)
    logger.info("Review result: approved=%s reasons=%s", result["approved"], result["reasons"])
    return result


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Stage 2: automated review gate")
    parser.add_argument("--script", required=True, help="Path to script.json")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    output_dir = args.output_dir or str(run_output_dir(args.run_id))
    result = review_script(args.script, output_dir, args.run_id)
    sys.exit(0 if result["approved"] else 1)


if __name__ == "__main__":
    main()
