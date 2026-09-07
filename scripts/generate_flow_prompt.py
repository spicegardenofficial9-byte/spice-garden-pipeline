"""
Generates the copy-pasteable Flow prompts for one or more approved
scripts' hero clips (HERO_CLIPS_PER_SHORT each, 2 by default), and
stashes each script + its Flow prompt in clip-pool/pending/<date>-<slot>/
so a later scheduled run (once the human has dropped the finished clips
into clip-pool/incoming/) can find the right script to build the video
from - GitHub Actions runners don't persist state between runs, so this
directory is committed back to the repo (see the workflow's commit step).

This is the "10%" human step from the automation report: a person reads
the brief, pastes each clip's prompt into Google Flow in turn (using
Flow's own, already-paid-for credits - not this pipeline calling any
API), attaches the character reference image in Flow's UI, generates
each clip (up to VEO_CLIP_DURATION_SEC seconds), downloads it, and
saves it with the one-command ./save_clip.sh wrapper.

Per explicit user request, AM and PM requests are generated TOGETHER in
one run (see run_pipeline.py's run_batch()) so the human can do all
HERO_CLIPS_PER_SHORT * 2 clips (4, by default) in a single Flow sitting
instead of two separate sessions per day. write_combined_brief() below
merges however many slots got approved into ONE document.

Output (clip-pool/pending/<date>-<slot>/), once per approved slot:
    script.json      - copy of the approved script (raw JSON, for the pipeline)
    flow_prompt.txt   - plain text, ready to paste into Flow, one section per clip
Output (clip-pool/LATEST_BRIEF.txt, fixed path, always the latest):
    One self-sufficient document covering every slot approved this run -
    open that one file every day, no date/slot lookup needed.
"""
import argparse
import logging
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import (
    CLIP_POOL_INCOMING_DIR, CLIP_POOL_PENDING_DIR, HERO_CLIP_DURATIONS_SEC,
    HERO_CLIPS_PER_SHORT, LATEST_BRIEF_PATH, ROOT_DIR, VISUAL_STYLE_PREFIX,
)
from common.io_utils import load_json

logger = logging.getLogger(__name__)

FLOW_PROMPT_HEADER = """Dish: {dish_name} ({region})

This video needs {n_clips} separate hero clips - generate each one
independently in Flow and save under its own filename below. Each clip
has its OWN duration cap, not a shared one - see per-clip below.

Common requirements for every clip:
- Attach the Spice Garden character reference image for consistency
  (image-to-video / reference-frame mode, not text description alone).
- Aspect ratio: 9:16 vertical (1080x1920 target).
- Style: {style} - vary only the action/setting described per clip below.
"""

FLOW_PROMPT_CLIP_SECTION = """
--- Clip {clip_num} of {n_clips} (up to {duration}s) ---
Prompt (paste into Flow):
{key_visual_moment}

Once generated, download and save as:
  clip-pool/incoming/{date}-{slot}-{clip_num}.mp4
"""

# STEP_SECTION is the ONLY part you actually need to act on each day: one
# prompt to paste into Flow, and the one command to save the result,
# right next to each other so there's nothing to hunt for.
STEP_SECTION = """
STEP {clip_num} of {n_clips} ({slot} clip {clip_num}, up to {duration}s)
------------------------------------------------------------
1) Copy everything between the lines below and paste it into Flow
   as the prompt (attach the Spice Garden character reference image
   in Flow's UI before generating). In Flow, set this clip's duration
   to {duration} seconds:
--------------------- COPY BELOW THIS LINE ------------------
{key_visual_moment}
--------------------- COPY ABOVE THIS LINE ------------------

2) Generate the clip in Flow and download it (it lands in your
   Downloads folder). Then run this ONE command in a terminal - no cd,
   no venv activation, nothing else needed. It grabs whatever you just
   downloaded and puts it in the right place automatically:

     {save_clip_cmd} {slot} {clip_num}

   (Prefer a file manager instead? Rename the file in Downloads to
   exactly "{filename}" and drag it into this folder:
       {incoming_dir}
   Do NOT type that folder path by itself into a terminal - it's a
   destination, not a command.)
"""

VIDEO_BLOCK = """
============================================================
{slot} VIDEO: {dish_name} ({region})
============================================================
{steps}"""

REFERENCE_BLOCK = """
--- {slot}: {dish_name} ({region}) ---
TITLE: {title}

INGREDIENTS:
{ingredients}

FULL NARRATION:
{voiceover_script}

VISUAL TIMELINE ({n_beats} beats, ~{duration}s total):
{beats}

HASHTAGS: {hashtags}
"""

# One self-sufficient file covering every slot approved this run. The
# actionable steps come FIRST (what to copy, where to save it) grouped by
# video, since that's the only part needed every day; reference details
# (ingredients, full narration, timeline) come after for context but are
# never required to complete the steps.
BRIEF_TEMPLATE = """SPICE GARDEN - {date}
{separator}
This is always the latest brief. To see it any time, run this ONE
command from anywhere in a terminal:
  {show_brief_cmd}
{n_videos_label}, {n_total_clips} clips total. Do them in order, video
by video.
{separator}
{videos_steps}
{separator}
That's it - once all the clips above exist, the scheduled runs pick up
each video automatically and build it. Nothing else to do.
{separator}
{rejected_note}
REFERENCE ONLY (not needed to complete the steps above)
{separator}
{videos_reference}
"""


def _format_beats(script: dict) -> str:
    lines = []
    for beat in script.get("visual_beats", []):
        kind = "HERO CLIP" if beat.get("type") == "video" else "STILL"
        start, end = beat.get("start_sec", 0), beat.get("end_sec", 0)
        lines.append(f"  [{start:5.1f}s - {end:5.1f}s] {kind:9s} - {beat.get('prompt', '')}")
    return "\n".join(lines) if lines else "  (none)"


def _get_key_visual_moments(script: dict) -> list:
    key_visual_moments = script.get("key_visual_moments")
    if not key_visual_moments:
        video_beats = [b for b in script.get("visual_beats", []) if b.get("type") == "video"]
        key_visual_moments = [b["prompt"] for b in video_beats]
    if not key_visual_moments:
        key_visual_moments = ["(no key visual moment found in script)"] * HERO_CLIPS_PER_SHORT
    return key_visual_moments[:HERO_CLIPS_PER_SHORT]


def write_request(script: dict, date: str, slot: str, script_path: str) -> Path:
    """Writes the per-slot pending files (script.json + flow_prompt.txt)
    that Stage A/B/C consume later. Returns the pending_dir."""
    slot = slot.upper()
    if slot not in ("AM", "PM"):
        raise ValueError(f"slot must be AM or PM, got {slot!r}")

    prompt_text = FLOW_PROMPT_HEADER.format(
        dish_name=script.get("dish_name", "unknown dish"),
        region=script.get("region", "Indian"),
        n_clips=HERO_CLIPS_PER_SHORT,
        style=VISUAL_STYLE_PREFIX.rstrip(": "),
    )
    for i, moment in enumerate(_get_key_visual_moments(script), start=1):
        prompt_text += FLOW_PROMPT_CLIP_SECTION.format(
            clip_num=i, n_clips=HERO_CLIPS_PER_SHORT, key_visual_moment=moment,
            date=date, slot=slot, duration=HERO_CLIP_DURATIONS_SEC[i - 1],
        )

    pending_dir = CLIP_POOL_PENDING_DIR / f"{date}-{slot}"
    pending_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(script_path, pending_dir / "script.json")
    (pending_dir / "flow_prompt.txt").write_text(prompt_text, encoding="utf-8")
    logger.info("Flow prompt written to %s", pending_dir / "flow_prompt.txt")
    return pending_dir


def _slot_steps(script: dict, date: str, slot: str) -> str:
    save_clip_cmd = f'"{ROOT_DIR / "save_clip.sh"}"'
    steps = "".join(
        STEP_SECTION.format(
            clip_num=i, n_clips=HERO_CLIPS_PER_SHORT, key_visual_moment=moment,
            slot=slot, filename=f"{date}-{slot}-{i}.mp4", duration=HERO_CLIP_DURATIONS_SEC[i - 1],
            incoming_dir=str(CLIP_POOL_INCOMING_DIR), save_clip_cmd=save_clip_cmd,
        )
        for i, moment in enumerate(_get_key_visual_moments(script), start=1)
    )
    return VIDEO_BLOCK.format(slot=slot, dish_name=script.get("dish_name", "unknown"),
                               region=script.get("region", "Indian"), steps=steps)


def _slot_reference(script: dict, slot: str) -> str:
    ingredients = "\n".join(f"  - {i}" for i in script.get("ingredients", [])) or "  (none listed)"
    return REFERENCE_BLOCK.format(
        slot=slot, dish_name=script.get("dish_name", "unknown"), region=script.get("region", "Indian"),
        title=script.get("title", ""), ingredients=ingredients,
        voiceover_script=script.get("voiceover_script") or "(none - minimal-narration video, visuals only)",
        n_beats=len(script.get("visual_beats", [])), duration=script.get("estimated_duration_sec", "?"),
        beats=_format_beats(script), hashtags=" ".join(script.get("hashtags", [])),
    )


def write_combined_brief(date: str, approved_slots: dict, rejected_slots: dict = None) -> Path:
    """approved_slots: {slot: script_dict}, in the order they should appear.
    rejected_slots: {slot: [reasons]} - slots that failed review this run,
    so the brief can say why fewer than expected videos are listed instead
    of silently going quiet on that slot.
    """
    rejected_slots = rejected_slots or {}
    slots = list(approved_slots.items())
    n_videos = len(slots)
    n_total_clips = n_videos * HERO_CLIPS_PER_SHORT

    videos_steps = "".join(_slot_steps(script, date, slot) for slot, script in slots)
    videos_reference = "".join(_slot_reference(script, slot) for slot, script in slots)

    rejected_note = ""
    if rejected_slots:
        lines = [
            f"  - {slot}: rejected this cycle ({'; '.join(reasons) or 'no reason given'}) - "
            "will retry on the next scheduled run."
            for slot, reasons in rejected_slots.items()
        ]
        rejected_note = "NOTE - some slots have no video today:\n" + "\n".join(lines) + f"\n{'=' * 60}\n\n"

    brief_text = BRIEF_TEMPLATE.format(
        date=date, separator="=" * 60,
        show_brief_cmd=f'"{ROOT_DIR / "show_brief.sh"}"',
        n_videos_label=f"There {'is' if n_videos == 1 else 'are'} {n_videos} video{'s' if n_videos != 1 else ''} today"
                       f" ({', '.join(slot for slot, _ in slots)})" if slots else "No videos passed review today",
        n_total_clips=n_total_clips,
        videos_steps=videos_steps or "  (nothing to do - no script passed review this run)",
        rejected_note=rejected_note,
        videos_reference=videos_reference or "  (none)",
    )
    LATEST_BRIEF_PATH.write_text(brief_text, encoding="utf-8")
    logger.info("Combined brief written to %s (%d video(s), %d clip(s))", LATEST_BRIEF_PATH, n_videos, n_total_clips)
    return LATEST_BRIEF_PATH


def generate_flow_prompt(script_path: str, date: str, slot: str) -> Path:
    """Single-slot convenience wrapper (used for manual/one-off runs and
    testing) - writes the pending request and a combined brief containing
    just this one slot."""
    script = load_json(script_path)
    slot = slot.upper()
    pending_dir = write_request(script, date, slot, script_path)
    write_combined_brief(date, {slot: script})
    return pending_dir


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Generate Flow prompts for a script's hero clips")
    parser.add_argument("--script", required=True, help="Path to an approved script.json")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--slot", required=True, choices=["AM", "PM", "am", "pm"])
    args = parser.parse_args()

    generate_flow_prompt(args.script, args.date, args.slot)


if __name__ == "__main__":
    main()
