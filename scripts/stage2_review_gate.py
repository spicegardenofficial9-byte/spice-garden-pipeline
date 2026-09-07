"""
Stage 2 - Automated review gate.

Runs an automated pass/fail check on a generated script before any paid
downstream work happens. This gate is deliberately FAIL-CLOSED: if the
review call errors, times out, or returns anything that can't be parsed
into the expected shape, the script is treated as REJECTED. An ambiguous
result is never treated as a pass.

Reviewer is Gemini (config.GEMINI_TEXT_MODEL), used as an LLM-as-judge
for culinary plausibility, ingredient sanity, and regional accuracy, on
top of the dependency-free structural checks in _basic_shape_check().

Expected input (<output_dir>/script.json from Stage 1):
    see stage1_script_generation.py docstring.

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
    STILLS_PER_SHORT_MAX, STILLS_PER_SHORT_MIN, get_env, run_output_dir,
)
from common.cost_logger import log_cost
from common.io_utils import load_json, save_json
from common.retry import retry_with_backoff

logger = logging.getLogger(__name__)

REVIEW_API_KEY_ENV = "GEMINI_API_KEY"
# voiceover_script is deliberately NOT required here - minimal/no-narration
# videos are a valid, intentional choice (see stage1's docstring), so an
# empty voiceover_script must not fail this check.
REQUIRED_SCRIPT_FIELDS = ("visual_beats", "estimated_duration_sec")


class ReviewFailure(Exception):
    """Raised for any condition that must be treated as fail-closed."""


REVIEW_PROMPT_TEMPLATE = """You are a strict quality-control reviewer for an \
Indian home-cooking YouTube Shorts channel. Review the script JSON below \
against EXACTLY these three criteria, and nothing else:

1. Culinary plausibility - is the technique, timing, and sequence of steps \
realistic and physically correct?
2. Ingredient sanity - are the ingredients real, compatible with the dish, \
and free of contradictions?
3. Regional accuracy - does this match how the dish is actually made in \
Indian home cooking (not a generic/foreign approximation)?

If you are not fully confident on all three, reject it. When in doubt, reject.

Do NOT reject for anything outside these three criteria. In particular, \
this channel intentionally uses minimal or no spoken narration - it is \
CORRECT and EXPECTED for "voiceover_script" to be short or empty, and for \
long stretches of "visual_beats" to have no corresponding "segments" entry \
(silence during on-screen action). Never treat narration length, narration \
gaps, or silence as a defect - judge only the three criteria above.

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

    Also enforces the hybrid clip structure (HERO_CLIPS_PER_SHORT hero
    clips/video, per explicit user confirmation): exactly
    HERO_CLIPS_PER_SHORT Veo 3.1 Lite hero clips, each checked against
    its OWN duration cap in HERO_CLIP_DURATIONS_SEC (in clip order - not
    a uniform cap), plus 3-6 Nano Banana stills. This bounds how much
    you're asked to manually generate in Flow per video and keeps the
    monthly Flow-credit math accurate - a script asking for a different
    clip count is rejected here rather than causing a mismatch between
    what's requested and what Flow credits were actually spent on.
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

    beats = script.get("visual_beats") or []
    video_beats = [b for b in beats if b.get("type") == "video"]
    image_beats = [b for b in beats if b.get("type") == "image"]

    if len(video_beats) != HERO_CLIPS_PER_SHORT:
        problems.append(
            f"expected exactly {HERO_CLIPS_PER_SHORT} video beats (Veo hero "
            f"clips, manually generated in Flow), found {len(video_beats)}"
        )
    else:
        for idx, vb in enumerate(video_beats):
            cap = HERO_CLIP_DURATIONS_SEC[idx]
            vb_duration = vb.get("end_sec", 0) - vb.get("start_sec", 0)
            if vb_duration > cap + 0.5:
                problems.append(
                    f"video beat {vb.get('id')} (hero clip {idx + 1}) duration {vb_duration}s "
                    f"exceeds its {cap}s cap"
                )

    if not (STILLS_PER_SHORT_MIN <= len(image_beats) <= STILLS_PER_SHORT_MAX + 1):
        problems.append(
            f"expected {STILLS_PER_SHORT_MIN}-{STILLS_PER_SHORT_MAX} still-image "
            f"beats, found {len(image_beats)}"
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
