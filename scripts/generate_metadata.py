"""
Metadata generation - the ONLY job Gemini has anymore.

Per explicit user decision, the creative "script" (the dish, the village
story, and the six hero-clip moment descriptions) is authored by Claude and
committed to clip-pool/pending/<date>-<slot>/script.json. Gemini is used for
exactly one thing: writing a traction-optimised YouTube TITLE and DESCRIPTION
(plus tags) for a finished video, so the Shorts gain reach easily.

This runs at build time (see run_stage_bc.py), after the video is assembled,
so the copy is based on the final script. It is FAIL-SOFT: if GEMINI_API_KEY
is unset or Gemini errors (e.g. 503), it falls back to a solid baseline built
from the script itself, so publishing is never blocked by the metadata step.

Public function:
    generate_metadata(script: dict, run_id: str) -> dict
        returns {"title": str, "description": str, "tags": [str], "source": str}
"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import GEMINI_TEXT_MODEL, get_env
from common.cost_logger import log_cost
from common.retry import retry_with_backoff

logger = logging.getLogger(__name__)

GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
MAX_TITLE_CHARS = 100

METADATA_PROMPT_TEMPLATE = """You are a YouTube Shorts growth strategist for \
"Spice Garden", a cozy Studio-Ghibli-style channel telling short Indian \
village cooking STORIES (a young woman cooking a traditional dish, no \
voiceover, satisfying natural sound). Write metadata that maximises \
click-through and reach for ONE Short.

Dish: {dish_name} ({region})
Story beats (what the video shows, in order):
{beats}
Ingredients (context only, not all need naming): {ingredients}
Interesting fact: {dish_fact}

Return ONLY a JSON object (no markdown fences, no commentary):
{{
  "title": "string, <=90 chars, a scroll-stopping hook that sparks curiosity or a benefit, naturally includes the dish name and ends with #shorts, 1-2 tasteful emojis, NO clickbait lies",
  "description": "string, 3-5 short lines: line 1 a punchy hook; line 2-3 what you'll see / why it's special (weave in searchable keywords like the dish name, 'authentic', 'village style', 'homemade', 'recipe'); then a blank line; then 12-18 relevant hashtags on one line (mix broad + niche: #shorts #indianfood #<dish> #cooking #asmr #ghibli #streetfood #viral etc.); then a final line inviting people to subscribe",
  "tags": ["15-20 short SEO keyword tags, lowercase, no # - dish names, cuisine, cooking, shorts, asmr, etc."]
}}

Make it genuinely appealing and specific to THIS dish and story - not generic. \
Avoid ALL CAPS words and misleading claims."""


def _beats_text(script: dict) -> str:
    lines = []
    for seg in script.get("segments", []):
        lines.append(f"- {seg.get('moment_description', '')}")
    return "\n".join(lines) if lines else "- (a step-by-step cooking story)"


def _fallback(script: dict) -> dict:
    """A solid, non-lousy baseline used when Gemini is unavailable."""
    dish = script.get("dish_name", "Indian recipe")
    region = script.get("region", "")
    ingredients = ", ".join(script.get("ingredients", [])[:6])
    fact = script.get("dish_fact", "")
    title = script.get("title") or f"Authentic {dish} at home 🍲 #shorts"
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS].rstrip()
    dish_tag = "#" + dish.lower().replace(" ", "")
    region_tag = ("#" + region.lower().replace(" ", "")) if region else ""
    hashtags = " ".join(t for t in (
        "#shorts", "#indianfood", dish_tag, region_tag, "#recipe", "#homecooking",
        "#cooking", "#asmr", "#villagecooking", "#streetfood", "#foodie", "#viral",
    ) if t)
    desc_lines = [
        f"{dish} — made the traditional {region} village way. 🌿".strip(),
    ]
    if fact:
        desc_lines.append(fact)
    if ingredients:
        desc_lines.append(f"Made with {ingredients} and love.")
    desc_lines += ["", hashtags, "", "Subscribe for a new recipe story every day!"]
    return {
        "title": title,
        "description": "\n".join(desc_lines),
        "tags": [t.lstrip("#") for t in hashtags.split()],
        "source": "fallback",
    }


@retry_with_backoff(max_attempts=3, exceptions=(RuntimeError,))
def _call_gemini(script: dict) -> dict:
    api_key = get_env(GEMINI_API_KEY_ENV)
    if not api_key:
        raise RuntimeError(f"{GEMINI_API_KEY_ENV} not set")
    prompt = METADATA_PROMPT_TEMPLATE.format(
        dish_name=script.get("dish_name", ""),
        region=script.get("region", "Indian"),
        beats=_beats_text(script),
        ingredients=", ".join(script.get("ingredients", [])),
        dish_fact=script.get("dish_fact", ""),
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
        data = json.loads(response.text)
    except Exception as exc:
        raise RuntimeError(f"Gemini metadata call failed: {exc}") from exc

    if not isinstance(data, dict) or not data.get("title") or not data.get("description"):
        raise RuntimeError(f"Gemini returned unusable metadata: {data!r}")
    title = str(data["title"]).strip()[:MAX_TITLE_CHARS]
    tags = [str(t).lstrip("#").strip() for t in data.get("tags", []) if str(t).strip()]
    return {"title": title, "description": str(data["description"]).strip(),
            "tags": tags, "source": "gemini"}


def generate_metadata(script: dict, run_id: str = "") -> dict:
    """Traction-optimised title + description + tags for a video. Never raises
    - falls back to a solid baseline if Gemini is unavailable."""
    try:
        meta = _call_gemini(script)
        logger.info("Gemini metadata: title=%r", meta["title"])
    except Exception as exc:  # noqa: BLE001 - fail soft, publishing must not break
        logger.warning("Metadata: falling back to baseline (%s)", exc)
        meta = _fallback(script)

    if run_id:
        log_cost(
            run_id=run_id, stage="generate_metadata",
            provider="gemini-api" if meta["source"] == "gemini" else "none",
            model=GEMINI_TEXT_MODEL if meta["source"] == "gemini" else "fallback",
            units=1, unit_type="metadata_call", cost_usd=0.0,
            notes=f"title+description via {meta['source']}",
        )
    return meta
