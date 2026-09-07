"""
Stage 1 - Script generation.

Calls Gemini (config.GEMINI_TEXT_MODEL) to write the script. Until
GEMINI_API_KEY is set, this stage produces a deterministic mock script
instead, so downstream stages can be built and tested without an API key.

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
        "ingredients": [str],
        "title": str,                     # YouTube title, <= 100 chars
        "hook": str,                      # first line, must grab attention in <3s
        "voiceover_script": str,          # full narration, plain text
        "segments": [                     # rough narration beats, timing is provisional
            {"id": int, "text": str, "start_sec": float, "end_sec": float}
        ],
        "key_visual_moments": [str, str], # descriptions of the two hero clips' actions -
                                           # these get turned into Flow prompts for the
                                           # human to manually generate
        "visual_beats": [                 # what should be on screen, timing is provisional
            {"id": int, "type": "image"|"video", "prompt": str,
             "start_sec": float, "end_sec": float}
        ],
        # visual_beats MUST contain exactly HERO_CLIPS_PER_SHORT (2) "video"
        # beats, each <= 8s - these are the hero clips a human manually
        # generates in Google Flow (using the Google AI Pro subscription's
        # Flow credits) and drops into clip-pool/incoming/; this pipeline
        # never calls Veo via API (see stage5's docstring for why) - plus
        # 3-6 "image" beats (free-tier Nano Banana stills, generated here).
        # Stage 2's review gate fails closed on this shape.
        "estimated_duration_sec": float,  # target range 30-40s
        "hashtags": [str],
        "ai_disclosure_required": true,
        "generated_at": iso8601 str,
        "model": str
    }

Note: timing in `segments`/`visual_beats` is provisional. Stage 3 produces
the true voiceover duration and Stage 6 rescales visual_beats to match it.
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
    MAX_DURATION_SEC, MIN_DURATION_SEC, get_env, run_output_dir,
)
from common.cost_logger import log_cost
from common.io_utils import load_json, new_run_id, save_json
from common.retry import retry_with_backoff

logger = logging.getLogger(__name__)

GEMINI_API_KEY_ENV = "GEMINI_API_KEY"

SCRIPT_PROMPT_TEMPLATE = """You are writing a {min_dur}-{max_dur} second YouTube Shorts script \
for "Spice Garden", an Indian home-cooking channel. Dish: {dish_hint}.

The narration must hook viewers in the first 3 seconds, be culinarily accurate \
and regionally authentic (how this dish is actually made in an Indian home \
kitchen), and end with a clear call to action.

Return ONLY a single JSON object (no markdown fences, no commentary) with \
exactly this shape:
{{
  "dish_name": "string",
  "region": "string, the Indian region/cuisine this dish belongs to (e.g. South Indian, Punjabi, Gujarati)",
  "ingredients": ["string", "..."],
  "title": "string, <=100 chars, include an emoji and #shorts",
  "hook": "string, the first line of narration",
  "voiceover_script": "string, the full narration as plain text",
  "segments": [
    {{"id": 1, "text": "string", "start_sec": 0.0, "end_sec": 0.0}}
  ],
  "key_visual_moments": [{key_visual_moments_example}],
  "visual_beats": [
    {{"id": 1, "type": "image", "prompt": "string, a vivid visual description", "start_sec": 0.0, "end_sec": 0.0}}
  ],
  "estimated_duration_sec": 0.0,
  "hashtags": ["#shorts", "..."]
}}

Hard requirements for visual_beats, non-negotiable:
- Exactly {n_hero_clips} beats with "type": "video" (not more, not fewer). Their prompts MUST match \
key_visual_moments[0] through key_visual_moments[{n_hero_clips_minus_1}] in order - pick the \
{n_hero_clips} separate moments that most need motion (e.g. an early prep/cooking action, \
a mid-cook action, and a later plating/finishing action; cutting, pouring, sizzling, \
flipping, plating - whichever moments benefit most from motion). The \
video beats do not need to be adjacent in time.
- EACH video beat has its OWN duration cap, not a shared one - do not exceed it: \
{per_clip_caps}. It's fine for a clip to run shorter than its cap if the \
moment is genuinely quick, but do not exceed the cap for that clip's position.
- Write each hero clip's prompt IN DEPTH, scaled to how much time it actually \
has - a vague one-liner wastes the clip's length. Describe the specific hand \
movements, what the ingredient/food is doing (sizzling, bubbling, changing \
color/texture), camera framing/angle, and pacing. The 8s clip should be ONE \
tightly-scoped action (e.g. just the pour, just the flip). The two 10s clips \
have room for a slightly fuller action or two connected sub-actions in the \
same shot (e.g. spooning filling on AND folding it over) - use that extra \
time, don't pad it with the same single gesture repeated.
- Use "image" beats wisely, not just as filler: at minimum, one should be a \
mouthwatering shot of the finished dish (a strong hook or closing shot). \
Also use still images to represent any step that happens over real time a \
clip physically cannot show - fermenting overnight, marinating, dough/batter \
resting, chilling, proofing, soaking. For dishes where such a step is \
authentically mandatory (e.g. curd rice needs fermented curd, dosa/idli \
batter ferments overnight, pickles marinate), include a still for it (dim/ \
night lighting or a covered bowl/vessel to imply elapsed time) rather than \
skipping it - it's accurate to the real process AND naturally helps fill out \
the video's length for dishes with a genuine waiting step.
- Between 3 and 6 beats with "type": "image" for everything else.
- All beats' start_sec/end_sec must be contiguous and cover the full \
estimated_duration_sec with no gaps or overlaps.
- Each visual_beats prompt should describe ONLY the scene/action/framing \
(what a camera would see) - do not mention the host character's appearance, \
since a separate reference image handles that.
- Every visual_beats prompt, including "image" beats, MUST depict a clear \
PHYSICAL ACTION actually happening (hands stirring, oil sizzling, batter \
being poured, a knife cutting, steam rising off a pan) rather than a static, \
already-finished composition - viewers respond to motion and process, not \
posed shots. Reserve a purely plated/finished shot for at most one beat \
(the very first hook shot or the very last shot), never the majority.

Hard requirement for chronological sync, non-negotiable and checked by an \
automated reviewer that rejects any mismatch - this is the single most \
common mistake, check it carefully:
- Every visual_beats entry's (start_sec, end_sec) window MUST show what is \
ACTUALLY being narrated in the "segments" entries covering that same time \
window. Build the narration (segments) first in cooking order, then place \
each visual beat to match what that specific slice of narration is \
describing - never write generic visuals in an order that doesn't track \
the voiceover. Before finalizing, walk through every visual beat's time \
window and confirm the segment(s) overlapping it describe the same action.
- Cooking technique must be realistic at every step: correct traditional \
tools (e.g. dosa batter is spread with a flat-bottomed ladle/katori, never \
a wooden spoon or ladle), correct order of operations (e.g. onions/aromatics \
brown before tomatoes go in; whole spices are removed or strained before a \
gravy is called "smooth"; batter is spread immediately after pouring, not \
after a delay), and correct timing for each action shown.

Hard requirement for ingredients, non-negotiable and checked by an automated \
reviewer that rejects any mismatch:
- The "ingredients" list MUST include every single ingredient mentioned \
anywhere else in the output - in voiceover_script, segments, \
key_visual_moments, AND visual_beats prompts (garnishes, tempering/tadka \
items, spices, aromatics like ginger/garlic, everything). Before finalizing, \
re-read the entire narration and every visual beat prompt and add any \
ingredient you referenced but left out of the list.

Hard requirement for voiceover_script - minimal narration, non-negotiable:
- Short-form cooking videos with little or no spoken narration consistently \
outperform heavily-narrated ones - the visuals and on-screen action should \
carry the story, not a running commentary. Default to VERY LITTLE narration: \
"voiceover_script" should normally be 0 to 20 words total - a single punchy \
hook line, a single call-to-action, or both. It is correct and expected for \
"voiceover_script" to be an empty string "" when the visual_beats and \
key_visual_moments already make the process self-explanatory (e.g. a \
satisfying close-up cooking process needs no narration at all) - do not \
force narration onto every video just to fill time.
- Never write a full step-by-step spoken walkthrough of the recipe - that \
is the single biggest mistake. If you do include narration, it is read \
aloud at roughly 125-135 words per minute, so even 20 words only takes \
about 9 seconds; the remaining screen time is carried by visual_beats alone.
- "segments" must still cover the full narration you did write (or be an \
empty list if voiceover_script is empty) with correct start_sec/end_sec.
"""


@retry_with_backoff(max_attempts=3, exceptions=(RuntimeError,))
def call_llm_for_script(topic_brief: dict) -> dict:
    """Calls Gemini to write the script. Raises RuntimeError on any
    failure (transport, auth, or unparseable response) so
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
    key_visual_moments_example = ", ".join(
        f'"string, vivid description of hero clip {i}\'s moment"' for i in range(1, HERO_CLIPS_PER_SHORT + 1)
    )
    per_clip_caps = ", ".join(
        f"hero clip {i} <= {cap}s" for i, cap in enumerate(HERO_CLIP_DURATIONS_SEC, start=1)
    )
    prompt = SCRIPT_PROMPT_TEMPLATE.format(
        min_dur=int(MIN_DURATION_SEC), max_dur=int(MAX_DURATION_SEC), dish_hint=dish_hint,
        n_hero_clips=HERO_CLIPS_PER_SHORT, n_hero_clips_minus_1=HERO_CLIPS_PER_SHORT - 1,
        per_clip_caps=per_clip_caps, key_visual_moments_example=key_visual_moments_example,
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

    if not isinstance(script, dict) or not script.get("voiceover_script"):
        raise RuntimeError(f"Gemini returned an unusable script: {script!r}")

    script["model"] = GEMINI_TEXT_MODEL
    script["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    script.setdefault("ai_disclosure_required", True)
    script.setdefault("region", "Indian")
    script.setdefault("ingredients", [])
    if not script.get("key_visual_moments"):
        video_beats = [b for b in script.get("visual_beats", []) if b.get("type") == "video"]
        script["key_visual_moments"] = [b["prompt"] for b in video_beats]
    return script


def _mock_script(topic_brief: dict) -> dict:
    dish_name = topic_brief.get("dish_hint", "Masala Dosa")
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "dish_name": dish_name,
        "region": "South Indian",
        "ingredients": [
            "rice", "urad dal", "water", "salt", "potatoes", "onion",
            "mustard seeds", "curry leaves", "turmeric", "green chilies", "ghee",
        ],
        "title": f"The Secret to Perfect {dish_name} 🔥 #shorts",
        "hook": f"Why does your {dish_name.lower()} never turn out this crispy?",
        # Minimal-narration default (see stage1's hard requirement above) -
        # a single punchy hook line, not a full step-by-step walkthrough.
        "voiceover_script": f"Why does your {dish_name.lower()} never turn out this crispy? Try this tonight.",
        "segments": [
            {"id": 1, "text": f"Why does your {dish_name.lower()} never turn out this crispy?", "start_sec": 0.0, "end_sec": 3.0},
            {"id": 2, "text": "Try this tonight.", "start_sec": 30.0, "end_sec": 33.0},
        ],
        # Hybrid structure: exactly HERO_CLIPS_PER_SHORT hero-clip beats
        # (per config.HERO_CLIP_DURATIONS_SEC caps, in order - 8s, 10s,
        # 10s - manually generated in Flow, dropped into
        # clip-pool/incoming/) for the moments that most need motion,
        # plus 3-6 Pollinations stills animated with pan/zoom in Stage 6.
        "key_visual_moments": [
            "Batter being poured and spread across a sizzling hot tawa in smooth circles, steam rising, slow motion",
            f"Spiced potato masala filling being spooned onto the crispy {dish_name}, steam rising",
            f"Hands folding the crispy {dish_name} over the filling, then plating it, slow motion",
        ],
        "visual_beats": [
            {"id": 1, "type": "image", "prompt": f"Close-up of raw {dish_name} batter fermenting in a bowl, morning light", "start_sec": 0.0, "end_sec": 3.0},
            {"id": 2, "type": "video", "prompt": "Batter being poured and spread across a sizzling hot tawa in smooth circles, steam rising, slow motion", "start_sec": 3.0, "end_sec": 11.0},
            {"id": 3, "type": "image", "prompt": f"Crispy golden {dish_name} on the tawa, edges lifting on their own", "start_sec": 11.0, "end_sec": 14.0},
            {"id": 4, "type": "video", "prompt": f"Spiced potato masala filling being spooned onto the crispy {dish_name}, steam rising", "start_sec": 14.0, "end_sec": 24.0},
            {"id": 5, "type": "image", "prompt": "Overhead shot of the spiced potato masala in a pan, steam rising", "start_sec": 24.0, "end_sec": 27.0},
            {"id": 6, "type": "video", "prompt": f"Hands folding the crispy {dish_name} over the filling, then plating it, slow motion", "start_sec": 27.0, "end_sec": 37.0},
            {"id": 7, "type": "image", "prompt": f"Finished {dish_name} plated with coconut chutney and sambar, steam rising", "start_sec": 37.0, "end_sec": 40.0},
        ],
        "estimated_duration_sec": 40.0,
        "hashtags": ["#shorts", "#indiancooking", "#spicegarden"],
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
