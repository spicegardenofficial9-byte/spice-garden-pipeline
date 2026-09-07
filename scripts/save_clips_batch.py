"""
Saves ALL of a slot's hero clips in one shot, instead of running
save_clip.py once per clip. Generate every clip for the slot in Flow
first, download them all, THEN run this once.

Takes the HERO_CLIPS_PER_SHORT most recently modified .mp4 files in
~/Downloads, sorts them oldest-to-newest (the order you generated them
in), and assigns them as clip 1, 2, 3... in that order.

Usage:
    python scripts/save_clips_batch.py AM
    python scripts/save_clips_batch.py PM
"""
import argparse
import glob
import logging
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import CLIP_POOL_INCOMING_DIR, HERO_CLIPS_PER_SHORT
from save_clip import _find_current_request

logger = logging.getLogger(__name__)

DOWNLOADS_DIR = Path.home() / "Downloads"


def save_clips_batch(slot: str):
    slot = slot.upper()
    request_name = _find_current_request(slot)

    candidates = glob.glob(str(DOWNLOADS_DIR / "*.mp4"))
    if len(candidates) < HERO_CLIPS_PER_SHORT:
        raise SystemExit(
            f"Only found {len(candidates)} .mp4 file(s) in {DOWNLOADS_DIR}, "
            f"need {HERO_CLIPS_PER_SHORT} - generate and download all clips first."
        )

    # Most recently modified N files, then oldest-first (generation order).
    newest_first = sorted(candidates, key=os.path.getmtime, reverse=True)
    batch = sorted(newest_first[:HERO_CLIPS_PER_SHORT], key=os.path.getmtime)

    CLIP_POOL_INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    for i, source in enumerate(batch, start=1):
        dest = CLIP_POOL_INCOMING_DIR / f"{request_name}-{i}.mp4"
        if dest.exists():
            logger.warning("%s already exists - overwriting.", dest)
        shutil.move(source, str(dest))
        logger.info("Saved clip %d: %s -> %s", i, source, dest)

    logger.info("All %d clips saved for %s.", HERO_CLIPS_PER_SHORT, request_name)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Save all of a slot's clips from Downloads in one command")
    parser.add_argument("slot", choices=["AM", "PM", "am", "pm"])
    args = parser.parse_args()
    save_clips_batch(args.slot.upper())


if __name__ == "__main__":
    main()
