"""
Stage 1 - Script generation (60-second, no-narration format).

Calls Gemini (config.GEMINI_TEXT_MODEL) to write the "script". Until
GEMINI_API_KEY is set, this stage produces a deterministic mock script
instead, so downstream stages can be built and tested without an API key.

This is a MAJOR rewrite for the new video format, modelled on a clean
Studio-Ghibli cooking short: one consistent character, a continuous
farm-to-plate story, natural sound effects, and NO on-screen text and NO
voiceover of any kind. The finished video carries the hero clips' own
native audio (sizzle, pour, chop) under a bed of background music. So the
"script" writes no narration AND no on-screen copy; it produces the
STRUCTURED CONTENT that drives the visuals across ~50 seconds, plus a
little metadata used only for the YouTube listing (never shown on screen):

  - dish name + region                (metadata: title/tags)
  - the full ingredient list           (metadata only - NOT shown on screen)
  - a short interesting fact / tip      (metadata: video description)
  - a handful of distinct "moment" segments, each a detailed, specific
    visual (a hero clip or a still) forming one continuous cooking story
  - the copy for the ONE piece of on-screen text: a short animated
    "subscribe" call-to-action shown in the last few seconds

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
        "ingredients": [str],             # FULL list - metadata only, NOT shown on screen
        "title": str,                     # YouTube title, <= 100 chars
        "dish_fact": str,                 # one interesting fact / origin note / tip (description)
        "subscribe_cta_text": str,        # short CTA copy for the animated end-card
        "segments": [                     # SEGMENTS_MIN..SEGMENTS_MAX moments, in order
            {
                "id": int,
                "type": "video" | "image",   # hero clip vs supporting still
                "moment_description": str,   # detailed, specific visual for this beat
                "start_sec": float,
                "end_sec": float
            }
        ],
        # segments MUST contain exactly HERO_CLIPS_PER_SHORT (4) "video"
        # segments (the hero clips a human generates in the creative tool and
        # drops into clip-pool/incoming/ - this pipeline never calls a video
        # API, see stage5's docstring) plus STILLS_PER_SHORT_MIN..MAX "image"
        # segments (free Pollinations stills generated here). No segment
        # carries on-screen text. Stage 2's review gate fails closed on this
        # shape.
        "estimated_duration_sec": float,  # target ~TARGET_DURATION_SEC (50)
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
an Indian home-cooking YouTube Shorts channel. Design ONE ~{target_dur}-second \
video about: {dish_hint}.

STYLE - study this carefully. The video is a clean, cinematic Studio-Ghibli-style \
animated cooking story: ONE consistent character (a young woman cooking in a \
rustic Indian village kitchen/garden) shown across a continuous farm-to-plate \
journey. It is SILENT of narration and has ABSOLUTELY NO on-screen text - no \
title, no ingredient labels, no captions, no per-step words. The whole thing is \
carried by beautiful vertical clips with their own natural sound effects \
(sizzling, pouring, chopping, bubbling). Do NOT write any narration or any \
on-screen copy. The ONLY text anywhere is a short animated subscribe line at the \
very end.

The ingredient list and dish fact you return are METADATA ONLY (for the YouTube \
title/tags/description) - they are never shown on screen, so do not design a \
"list of ingredients" moment.

Return ONLY a single JSON object (no markdown fences, no commentary) with \
exactly this shape:
{{
  "dish_name": "string",
  "region": "string, the Indian region/cuisine this dish belongs to (e.g. South Indian, Punjabi, Gujarati)",
  "ingredients": ["string", "..."],
  "title": "string, <=100 chars, include an emoji and #shorts",
  "dish_fact": "string, one genuinely interesting, factually-sound fact, origin note, or pro tip about this dish (for the description)",
  "subscribe_cta_text": "string, a SHORT subscribe call-to-action (<=4 words), ideally tied to the dish (e.g. 'More dosa secrets?', 'Subscribe for more', 'Daily Indian recipes')",
  "segments": [
    {{"id": 1, "type": "image", "moment_description": "string, a vivid, SPECIFIC visual", "start_sec": 0.0, "end_sec": 0.0}}
  ],
  "estimated_duration_sec": {target_dur}.0,
  "hashtags": ["#shorts", "..."]
}}

Hard requirements for "segments", non-negotiable and checked by an automated \
reviewer that rejects any mismatch:
- Between {seg_min} and {seg_max} segments total, in chronological order, forming \
ONE coherent story (e.g. gathering/harvesting -> prepping -> cooking -> the \
finished dish). Keep the same character and setting consistent across them.
- EXACTLY {n_hero_clips} segments with "type": "video" - the hero clips a human \
generates in the creative tool. Choose the {n_hero_clips} moments with the most \
satisfying MOTION and SOUND (e.g. mustard seeds crackling in hot oil, batter \
spread on a hot tawa, dough folded, water poured into a pot, a dish garnished). \
Each video segment is capped at its own length - do not exceed it: {per_clip_caps}.
- Between {stills_min} and {stills_max} segments with "type": "image" - stills for \
slower story beats that don't need motion (a quiet establishing shot, an \
over-time step like fermenting/resting/soaking shown with dim or covered-vessel \
lighting, or the final plated dish). Do NOT make any still an "ingredient \
lineup"/labels shot.
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
length - one tightly-scoped action for a {shortest_clip}s clip.
- Every segment must depict a clear PHYSICAL ACTION or a rich, deliberate \
composition - never a bland, empty frame, and never any text/lettering in frame.
- You MAY describe the consistent character performing the action (e.g. "the \
young woman in a white-and-gold sari pours..."), keeping her look consistent, but \
keep the focus on the food and the action.
- Cooking technique must be realistic at every step: correct traditional tools, \
correct order of operations (aromatics brown before tomatoes; batter spread \
immediately after pouring), correct timing.

Hard requirement for "ingredients" (metadata), checked by the reviewer:
- The "ingredients" list MUST include every ingredient referenced anywhere in the \
output (moment_description or dish_fact) - garnishes, tempering/tadka items, \
spices, aromatics, everything - even though ingredients are never shown on screen.

Hard requirement for "dish_fact" (metadata):
- One or two sentences, genuinely informative and factually accurate for this \
dish and region. No invented history.
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
        if not seg.get("moment_description"):
            raise RuntimeError(f"segment missing moment_description: {seg!r}")


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
    # A 6-segment, ~50s continuous story: 2 stills (a quiet over-time beat and
    # the plated finish) + 4 hero clips, in cooking order, one consistent
    # character. No on-screen text on any segment. The final still is held
    # longest - it backs the subscribe end-card.
    return {
        "dish_name": dish_name,
        "region": "South Indian",
        "ingredients": [
            "rice", "urad dal", "fenugreek seeds", "water", "salt", "potatoes",
            "onion", "green chilies", "ginger", "mustard seeds", "curry leaves",
            "turmeric", "ghee", "coconut", "coriander",
        ],
        "title": f"The Secret to Perfect {dish_name} \U0001F525 #shorts",
        "dish_fact": (
            "The dosa's signature tang and lightness come from wild fermentation - "
            "the rice-and-urad-dal batter is left overnight so natural lactic "
            "bacteria leaven it, no yeast and no baking soda."
        ),
        "subscribe_cta_text": "More dosa secrets?",
        "segments": [
            {
                "id": 1, "type": "image",
                "moment_description": (
                    "A covered earthen clay pot of dosa batter resting overnight on "
                    "a rustic wooden shelf in a dim, warm village kitchen; the batter "
                    "has risen and its surface is dotted with tiny fermentation "
                    "bubbles, a soft shaft of dawn light falling across it, hushed "
                    "and still"
                ),
                "start_sec": 0.0, "end_sec": 6.0,
            },
            {
                "id": 2, "type": "video",
                "moment_description": (
                    "Extreme macro on a heavy black iron kadai: dark mustard seeds "
                    "tumble into a shallow pool of shimmering hot ghee and burst into "
                    "a dancing crackle, then a fistful of fresh curry leaves is "
                    "scattered in and spits violently; a beat later finely chopped "
                    "onion, slit green chillies and grated ginger cascade in and hiss "
                    "loudly, turning glassy and golden as a wooden spatula folds them "
                    "through, fragrant steam curling upward in warm morning light"
                ),
                "start_sec": 6.0, "end_sec": 16.0,
            },
            {
                "id": 3, "type": "video",
                "moment_description": (
                    "Soft boiled potato chunks are tipped from a brass bowl into the "
                    "tempered aromatics; a hand presses and folds them with a wooden "
                    "masher, a pinch of bright turmeric rains over the top and stains "
                    "everything gold, a splash of water is added and it all sizzles "
                    "as the spatula stirs it into a glossy, steaming spiced potato "
                    "mash, tight close-up of the potatoes breaking apart"
                ),
                "start_sec": 16.0, "end_sec": 26.0,
            },
            {
                "id": 4, "type": "video",
                "moment_description": (
                    "Straight-down on a screaming-hot flat tawa: a steel ladle pours "
                    "a pool of pale fermented batter at the centre, then in one "
                    "confident continuous spiral from the inside outward spreads it "
                    "paper-thin into a wide even circle; the surface instantly "
                    "blisters into hundreds of tiny bubbles, the lacy edges set and "
                    "lift, and a spoon drizzles ghee around the rim that sizzles and "
                    "browns it to crisp golden lace"
                ),
                "start_sec": 26.0, "end_sec": 36.0,
            },
            {
                "id": 5, "type": "video",
                "moment_description": (
                    "A spoon lays a neat line of golden potato masala down the centre "
                    "of the crackly dosa; a flat spatula folds the dosa over the "
                    "filling, pressing so it audibly crackles, then both hands lift "
                    "the long folded dosa off the tawa and set it onto a fresh green "
                    "banana leaf, a thin ribbon of steam rising off its glassy golden "
                    "surface"
                ),
                "start_sec": 36.0, "end_sec": 45.0,
            },
            {
                "id": 6, "type": "image",
                "moment_description": (
                    "Overhead hero shot of the finished masala dosa on a banana leaf "
                    "with a small bowl of white coconut chutney and a steaming bowl "
                    "of sambar, scattered with fresh coriander, rich and appetising "
                    "in soft morning light"
                ),
                "start_sec": 45.0, "end_sec": 50.0,
            },
        ],
        "estimated_duration_sec": 50.0,
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
