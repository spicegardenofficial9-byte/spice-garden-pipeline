"""
Generates the copy-pasteable Flow prompts for one or more approved
scripts' hero clips (HERO_CLIPS_PER_SHORT each, 4 for the 60s format),
and stashes each script + its Flow prompt in clip-pool/pending/<date>-<slot>/
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
HERO_CLIPS_PER_SHORT * 2 clips (8, for the 60s format) in a single Flow
sitting instead of two separate sessions per day. write_combined_brief() below
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
    CLIP_POOL_PENDING_DIR, HERO_CLIP_DURATIONS_SEC, HERO_CLIPS_PER_SHORT,
    LATEST_BRIEF_PATH, VISUAL_STYLE_PREFIX,
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

# STEP_SECTION is just the prompt to paste into Flow for one clip - no
# save command here anymore. Generate + download ALL of a video's clips
# first, THEN save them all in one shot (see SAVE_ALL_SECTION below).
STEP_SECTION = """
CLIP {clip_num} of {n_clips} ({slot}, up to {duration}s)
------------------------------------------------------------
Copy everything between the lines below and paste it into Flow as the
prompt (attach the Spice Garden character reference image in Flow's UI,
and set this clip's duration to {duration} seconds before generating):
--------------------- COPY BELOW THIS LINE ------------------
{key_visual_moment}
--------------------- COPY ABOVE THIS LINE ------------------
Download it when done - don't save it anywhere yet, just leave it in
Downloads.
"""

SAVE_ALL_SECTION = """
Once ALL {n_clips} clips above are generated and downloaded, run this
ONE command - no cd, no venv activation. It grabs all {n_clips} of your
most recent downloads and files them correctly, in order:

    {save_clips_cmd} {slot}

(Prefer a file manager instead? Rename each downloaded file to exactly
"{date}-{slot}-1.mp4", "-2.mp4", etc. (in the order you generated them)
and drag them into: {incoming_dir}
Do NOT type that folder path by itself into a terminal - it's a
destination, not a command.)
"""

VIDEO_BLOCK = """
============================================================
{slot} VIDEO: {dish_name} ({region})
============================================================
{steps}
{save_all}"""

REFERENCE_BLOCK = """
--- {slot}: {dish_name} ({region}) ---
TITLE: {title}

INGREDIENTS:
{ingredients}

DISH FACT (shown as on-screen context, not narration):
{dish_fact}

SEGMENT TIMELINE ({n_segments} segments, ~{duration}s total; no voiceover -
each segment shows a short on-screen text card):
{segments}

SUBSCRIBE END-CARD COPY: {subscribe_cta_text}

HASHTAGS: {hashtags}
"""

# One self-sufficient file covering every slot approved this run. The
# actionable steps come FIRST (what to copy, where to save it) grouped by
# video, since that's the only part needed every day; reference details
# (ingredients, full narration, timeline) come after for context but are
# never required to complete the steps.
BRIEF_TEMPLATE = """SPICE GARDEN - {date}
{separator}
All commands below assume you're in a terminal INSIDE your project
folder (cd there first if needed). Start every day with ONE command
(pulls the latest + shows this brief):
  {daily_cmd}
(Or just re-view this file without pulling: {show_brief_cmd})
{n_videos_label}, {n_total_clips} clips total. Do them in order, video
by video.
{separator}
{videos_steps}
{separator}
That's it. Once all the clips for a video are saved and pushed
(./push_clips.sh), the scheduled runs pick it up automatically, build
it, and wait for you at ./review.sh - then ./approve.sh to publish.
{separator}
{rejected_note}
REFERENCE ONLY (not needed to complete the steps above)
{separator}
{videos_reference}
"""


def _format_segments(script: dict) -> str:
    lines = []
    for seg in script.get("segments", []):
        kind = "HERO CLIP" if seg.get("type") == "video" else "STILL"
        start, end = seg.get("start_sec", 0), seg.get("end_sec", 0)
        card = seg.get("text_card_copy", "")
        lines.append(
            f"  [{start:5.1f}s - {end:5.1f}s] {kind:9s} - {seg.get('moment_description', '')}"
        )
        lines.append(f"                        card: “{card}”")
    return "\n".join(lines) if lines else "  (none)"


def _get_hero_moments(script: dict) -> list:
    """The moment_description of each "video" segment, in order - one per
    hero clip the human generates in Flow."""
    video_segments = [s for s in script.get("segments", []) if s.get("type") == "video"]
    moments = [s.get("moment_description", "") for s in video_segments]
    if not moments:
        moments = ["(no hero-clip moment found in script)"] * HERO_CLIPS_PER_SHORT
    return moments[:HERO_CLIPS_PER_SHORT]


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
    for i, moment in enumerate(_get_hero_moments(script), start=1):
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
    steps = "".join(
        STEP_SECTION.format(
            clip_num=i, n_clips=HERO_CLIPS_PER_SHORT, key_visual_moment=moment,
            slot=slot, duration=HERO_CLIP_DURATIONS_SEC[i - 1],
        )
        for i, moment in enumerate(_get_hero_moments(script), start=1)
    )
    # Relative paths/commands only - this brief may be GENERATED on a
    # GitHub Actions runner (an ephemeral machine with its own unrelated
    # absolute paths like /home/runner/work/...), but is always READ on
    # your own machine after a git pull. An absolute path baked in here
    # would point at a path that doesn't exist on your computer at all.
    save_all = SAVE_ALL_SECTION.format(
        n_clips=HERO_CLIPS_PER_SHORT, slot=slot, date=date,
        save_clips_cmd="./save_clips.sh",
        incoming_dir="clip-pool/incoming/ (inside your project folder)",
    )
    return VIDEO_BLOCK.format(slot=slot, dish_name=script.get("dish_name", "unknown"),
                               region=script.get("region", "Indian"), steps=steps, save_all=save_all)


def _slot_reference(script: dict, slot: str) -> str:
    ingredients = "\n".join(f"  - {i}" for i in script.get("ingredients", [])) or "  (none listed)"
    return REFERENCE_BLOCK.format(
        slot=slot, dish_name=script.get("dish_name", "unknown"), region=script.get("region", "Indian"),
        title=script.get("title", ""), ingredients=ingredients,
        dish_fact=script.get("dish_fact") or "(none)",
        subscribe_cta_text=script.get("subscribe_cta_text") or "(default)",
        n_segments=len(script.get("segments", [])), duration=script.get("estimated_duration_sec", "?"),
        segments=_format_segments(script), hashtags=" ".join(script.get("hashtags", [])),
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
        daily_cmd="./daily.sh",
        show_brief_cmd="./show_brief.sh",
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
