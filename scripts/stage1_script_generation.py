"""
Stage 1 - Script generation (60-second, no-narration format).

Calls Gemini (config.GEMINI_TEXT_MODEL) to write the "script". Until
GEMINI_API_KEY is set, this stage produces a deterministic mock script
instead, so downstream stages can be built and tested without an API key.

This is a MAJOR rewrite for the new video format. There is no spoken
voiceover anywhere in the pipeline anymore - the finished video is silent
of narration and carries the hero clips' own native audio (sizzle, pour,
chop) under a bed of background music. So the "script" no longer writes
narration; it produces richer STRUCTURED CONTENT that drives on-screen
text cards and the visuals across a full ~60 seconds:

  - dish name + region
  - the full ingredient list (also used to render an ingredient still)
  - a handful of distinct "moment" segments, each detailed enough to drive
    ONE specific visual (a hero clip or a still), plus the short, punchy
    on-screen text-card copy for that moment
  - a short interesting fact / origin note / tip about the dish (the extra
    depth that fills 60 seconds meaningfully instead of stretching a
    30-second idea thin)
  - the copy for an animated "subscribe" call-to-action shown in the last
    few seconds (varies per video rather than being hardcoded)

Expected input:
    A "topic brief" JSON (optional), e.g.:
    {
        "dish_hint": "Masala Dosa"   # omit to let the LLM pick a dish
    }

Expected output (written to <output_dir>/script.json):
    {
        "run_id": str,
        "dish_name": str,
        "region": str,                    # e.g. "South Indian" - used for YouTube tags
        "ingredients": [str],             # FULL list, drives the ingredient still
        "title": str,                     # YouTube title, <= 100 chars
        "dish_fact": str,                 # one interesting fact / origin note / tip
        "subscribe_cta_text": str,        # short CTA copy for the animated end-card
        "segments": [                     # SEGMENTS_MIN..SEGMENTS_MAX moments, in order
            {
                "id": int,
                "type": "video" | "image",   # hero clip vs supporting still
                "moment_description": str,   # detailed, specific visual for this beat
                "text_card_copy": str,       # short on-screen card copy (2-3s readable)
                "start_sec": float,
                "end_sec": float
            }
        ],
        # segments MUST contain exactly HERO_CLIPS_PER_SHORT (4) "video"
        # segments (the hero clips a human generates in Google Flow and drops
        # into clip-pool/incoming/ - this pipeline never calls Veo via API,
        # see stage5's docstring) plus STILLS_PER_SHORT_MIN..MAX "image"
        # segments (free Pollinations stills generated here). Stage 2's
        # review gate fails closed on this shape.
        "estimated_duration_sec": float,  # target ~TARGET_DURATION_SEC (60)
        "hashtags": [str],
        "ai_disclosure_required": true,
        "generated_at": iso8601 str,
        "model": str
    }

Downstream consumers of this exact schema:
    - generate_flow_prompt.py : the "video" segments -> 4 hero-clip Flow prompts
    - stage5_visual_generation.py : the "image" segments -> Pollinations stills,
      the "video" segments -> mapped onto the manually-generated hero clips
    - stage6_assembly.py : segment timings -> cut points + text-card timing,
      subscribe_cta_text -> the animated subscribe end-card
"""
import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import (
    GEMINI_TEXT_MODEL, HERO_CLIP_DURATIONS_SEC, HERO_CLIPS_PER_SHORT,
    STILLS_PER_SHORT_MAX, STILLS_PER_SHORT_MIN, TARGET_DURATION_SEC,
    get_env, run_output_dir,
)
from common.cost_logger import log_cost
from common.io_utils import load_json, new_run_id, save_json

from common.retry import retry_with_backoff

logger = logging.getLogger(__name__)

GEMINI_API_KEY_ENV = "GEMINI_API_KEY"

SCRIPT_PROMPT_TEMPLATE = """You are the content designer for "Spice Garden", \
an Indian home-cooking YouTube Shorts channel. Design ONE {target_dur}-second \
video about: {dish_hint}.

These videos are SILENT of narration - there is no voiceover at all. The video \
is carried by short vertical hero clips (with their own natural cooking sound) \
and still images, with brief on-screen TEXT CARDS. Your job is NOT to write \
narration; it is to design rich, specific structured content: the moments, the \
text cards, the ingredient list, an interesting fact, and a subscribe line.

Return ONLY a single JSON object (no markdown fences, no commentary) with \
exactly this shape:
{{
  "dish_name": "string",
  "region": "string, the Indian region/cuisine this dish belongs to (e.g. South Indian, Punjabi, Gujarati)",
  "ingredients": ["string", "..."],
  "title": "string, <=100 chars, include an emoji and #shorts",
  "dish_fact": "string, one genuinely interesting, factually-sound fact, origin note, or pro tip about this dish",
  "subscribe_cta_text": "string, a SHORT subscribe call-to-action (<=4 words), ideally tied to the dish (e.g. 'More dosa secrets?', 'Subscribe for more', 'Daily Indian recipes')",
  "segments": [
    {{"id": 1, "type": "image", "moment_description": "string, a vivid, SPECIFIC visual", "text_card_copy": "string, short punchy card", "start_sec": 0.0, "end_sec": 0.0}}
  ],
  "estimated_duration_sec": {target_dur}.0,
  "hashtags": ["#shorts", "..."]
}}

Hard requirements for "segments", non-negotiable and checked by an automated \
reviewer that rejects any mismatch:
- Between {seg_min} and {seg_max} segments total, in chronological cooking order.
- EXACTLY {n_hero_clips} segments with "type": "video" - these are the hero clips \
a human generates in Google Flow. Choose the {n_hero_clips} moments that most \
need real MOTION (e.g. mustard seeds crackling in hot oil, batter being spread \
on a hot tawa, dough being folded, a finished dish being garnished). Each video \
segment is capped at its own length - do not exceed it: {per_clip_caps}.
- Between {stills_min} and {stills_max} segments with "type": "image" - supporting \
stills for moments that don't need motion. AT LEAST ONE image segment MUST be a \
clear shot of the full ingredient spread (its moment_description should reference \
the key ingredients), and one should be the finished, plated dish. Also use a \
still for any step that happens over real time a short clip cannot show \
(fermenting overnight, marinating, dough resting, simmering) - dim/covered-vessel \
lighting to imply elapsed time.
- All segments' start_sec/end_sec MUST be contiguous (no gaps, no overlaps) and \
cover the full estimated_duration_sec (~{target_dur}s). The final segment should \
be held slightly longer (it doubles as the backdrop for the subscribe end-card).

Hard requirements for "moment_description" - this is the single most common \
mistake, the reviewer rejects generic filler:
- Each one must describe a SPECIFIC, concrete visual, not a vague stage label. \
Write "mustard seeds crackling and popping in shimmering hot oil, curry leaves \
dropped in" - NOT "tempering the spices" or "cooking begins". Name what the food \
is doing (sizzling, bubbling, browning, steam rising), the specific hand action, \
and the framing/angle. A video segment's description should be scaled to its \
length - one tightly-scoped action for an {shortest_clip}s clip.
- Every segment must depict a clear PHYSICAL ACTION or a rich, deliberate \
composition (an ingredient spread, the plated dish) - never a bland, empty frame.
- Describe ONLY the scene/action/framing (what a camera sees). Do NOT mention the \
host character's appearance - a separate reference image handles that.
- Cooking technique must be realistic at every step: correct traditional tools, \
correct order of operations (aromatics brown before tomatoes; batter spread \
immediately after pouring), correct timing.

Hard requirements for "text_card_copy", checked by the reviewer:
- SHORT and punchy - readable in 2-3 seconds. A few words or a short phrase, NOT \
a full sentence and NOT narration. Think caption, not script. (e.g. "Crispy. \
Golden. Perfect.", "Ferment 12 hrs", "Ghee, not oil".)
- One per segment, relevant to that segment's moment.

Hard requirement for "ingredients", non-negotiable and checked by the reviewer:
- The "ingredients" list MUST include every single ingredient referenced anywhere \
else in the output - in any moment_description, text_card_copy, or dish_fact \
(garnishes, tempering/tadka items, spices, aromatics like ginger/garlic, \
everything). Re-read the whole output and add any ingredient you referenced but \
left out of the list.

Hard requirement for "dish_fact":
- One or two sentences, genuinely informative and factually accurate for this \
dish and region. No invented history. This is the depth that justifies a full \
{target_dur}s video.
"""


def _validate_segments(script: dict) -> None:
    """Raise RuntimeError if the LLM's segments are structurally unusable, so
    retry_with_backoff regenerates rather than passing junk downstream. This
    is a light guard; Stage 2 is the authoritative fail-closed gate.
    """
    segments = script.get("segments")
    if not isinstance(segments, list) or not segments:
        raise RuntimeError(f"Gemini returned no usable segments: {script!r}")
    video_segments = [s for s in segments if s.get("type") == "video"]
    if len(video_segments) != HERO_CLIPS_PER_SHORT:
        raise RuntimeError(
            f"expected {HERO_CLIPS_PER_SHORT} video segments, got {len(video_segments)}"
        )
    for seg in segments:
        if not seg.get("moment_description") or not seg.get("text_card_copy"):
            raise RuntimeError(f"segment missing moment_description/text_card_copy: {seg!r}")


@retry_with_backoff(max_attempts=3, exceptions=(RuntimeError,))
def call_llm_for_script(topic_brief: dict) -> dict:
    """Calls Gemini to write the script. Raises RuntimeError on any
    failure (transport, auth, or unparseable/unusable response) so
    retry_with_backoff can retry it.
    """
    api_key = get_env(GEMINI_API_KEY_ENV)
    if not api_key:
        logger.warning(
            "%s not set - generating a mock script instead of calling Gemini.",
            GEMINI_API_KEY_ENV,
        )
        return _mock_script(topic_brief)

    dish_hint = topic_brief.get("dish_hint") or "your choice of a popular Indian home-cooked dish"
    per_clip_caps = ", ".join(
        f"hero clip {i} <= {cap}s" for i, cap in enumerate(HERO_CLIP_DURATIONS_SEC, start=1)
    )
    prompt = SCRIPT_PROMPT_TEMPLATE.format(
        target_dur=int(TARGET_DURATION_SEC), dish_hint=dish_hint,
        n_hero_clips=HERO_CLIPS_PER_SHORT, per_clip_caps=per_clip_caps,
        stills_min=STILLS_PER_SHORT_MIN, stills_max=STILLS_PER_SHORT_MAX,
        seg_min=HERO_CLIPS_PER_SHORT + STILLS_PER_SHORT_MIN,
        seg_max=HERO_CLIPS_PER_SHORT + STILLS_PER_SHORT_MAX,
        shortest_clip=min(HERO_CLIP_DURATIONS_SEC),
    )

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=GEMINI_TEXT_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        script = json.loads(response.text)
    except Exception as exc:
        raise RuntimeError(f"Gemini script generation failed: {exc}") from exc

    if not isinstance(script, dict):
        raise RuntimeError(f"Gemini returned an unusable script: {script!r}")
    _validate_segments(script)

    script["model"] = GEMINI_TEXT_MODEL
    script["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    script.setdefault("ai_disclosure_required", True)
    script.setdefault("region", "Indian")
    script.setdefault("ingredients", [])
    script.setdefault("dish_fact", "")
    script.setdefault("subscribe_cta_text", "Subscribe for more")
    script.setdefault("estimated_duration_sec", float(TARGET_DURATION_SEC))
    return script


def _mock_script(topic_brief: dict) -> dict:
    dish_name = topic_brief.get("dish_hint", "Masala Dosa")
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    # A 7-segment, ~60s timeline: 3 stills (ingredient spread, a fermenting
    # over-time still, the plated finish) + 4 hero clips, in cooking order.
    # The final still is held longest - it backs the subscribe end-card.
    return {
        "dish_name": dish_name,
        "region": "South Indian",
        "ingredients": [
            "rice", "urad dal", "fenugreek seeds", "water", "salt", "potatoes",
            "onion", "mustard seeds", "curry leaves", "turmeric", "green chilies",
            "ginger", "ghee", "coconut", "coriander",
        ],
        "title": f"The Secret to Perfect {dish_name} \U0001F525 #shorts",
        "dish_fact": (
            "The dosa's signature tang and lightness come from wild fermentation: "
            "the rice-and-urad-dal batter is left overnight so natural lactic "
            "bacteria leaven it - no yeast, no baking soda."
        ),
        "subscribe_cta_text": "More dosa secrets?",
        "segments": [
            {
                "id": 1, "type": "image",
                "moment_description": (
                    "Flat-lay of the full ingredient spread on a stone counter: "
                    "soaked rice and urad dal in bowls, whole potatoes, onion, "
                    "green chilies, ginger, mustard seeds, curry leaves, turmeric "
                    "and a small pot of ghee, warm morning light"
                ),
                "text_card_copy": "5 pantry staples", "start_sec": 0.0, "end_sec": 9.0,
            },
            {
                "id": 2, "type": "image",
                "moment_description": (
                    "A covered steel vessel of dosa batter resting overnight in dim "
                    "kitchen light, the batter risen and bubbled at the surface to "
                    "imply long fermentation"
                ),
                "text_card_copy": "Ferment 12 hrs", "start_sec": 9.0, "end_sec": 17.0,
            },
            {
                "id": 3, "type": "video",
                "moment_description": (
                    "Mustard seeds crackling and popping in shimmering hot ghee in a "
                    "kadai, curry leaves dropped in and spluttering, close-up, steam "
                    "rising"
                ),
                "text_card_copy": "Bloom the tadka", "start_sec": 17.0, "end_sec": 25.0,
            },
            {
                "id": 4, "type": "video",
                "moment_description": (
                    "A ladle of batter poured onto a screaming-hot tawa and spread "
                    "outward in a smooth spiral with the base of the ladle, edges "
                    "already crisping, steam rising"
                ),
                "text_card_copy": "Spread it thin", "start_sec": 25.0, "end_sec": 33.0,
            },
            {
                "id": 5, "type": "video",
                "moment_description": (
                    "Golden spiced potato masala spooned along the center of the "
                    "crisp dosa, then the dosa folded over the filling with a flat "
                    "spatula, close-up"
                ),
                "text_card_copy": "Ghee, not oil", "start_sec": 33.0, "end_sec": 41.0,
            },
            {
                "id": 6, "type": "video",
                "moment_description": (
                    "The finished folded dosa lifted off the tawa and set onto a "
                    "banana leaf, shattering-crisp edges catching the light"
                ),
                "text_card_copy": "Crispy. Golden.", "start_sec": 41.0, "end_sec": 49.0,
            },
            {
                "id": 7, "type": "image",
                "moment_description": (
                    "Overhead hero shot of the plated masala dosa with coconut "
                    "chutney and a bowl of steaming sambar, garnished with coriander, "
                    "rich and appetizing"
                ),
                "text_card_copy": "Serve hot", "start_sec": 49.0, "end_sec": 60.0,
            },
        ],
        "estimated_duration_sec": 60.0,
        "hashtags": ["#shorts", "#indiancooking", "#spicegarden", "#dosa"],
        "ai_disclosure_required": True,
        "generated_at": now,
        "model": "mock",
    }


def generate_script(topic_brief_path: str, output_dir: str, run_id: str) -> dict:
    topic_brief = load_json(topic_brief_path) if topic_brief_path else {}
    script = call_llm_for_script(topic_brief)
    script["run_id"] = run_id

    is_mock = script.get("model") == "mock"
    log_cost(
        run_id=run_id, stage="stage1_script_generation",
        provider="mock" if is_mock else "gemini-api",
        model=script.get("model", "mock"),
        units=1, unit_type="generation", cost_usd=0.0,
        notes="mock mode - no LLM call made" if is_mock else "free-tier Gemini API quota",
    )

    out_path = Path(output_dir) / "script.json"
    save_json(out_path, script)
    logger.info("Wrote script to %s", out_path)
    return script


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Stage 1: script generation")
    parser.add_argument("--topic-brief", default=None, help="Path to a topic brief JSON (optional)")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    run_id = args.run_id or new_run_id()
    output_dir = args.output_dir or str(run_output_dir(run_id))
    generate_script(args.topic_brief, output_dir, run_id)


if __name__ == "__main__":
    main()
