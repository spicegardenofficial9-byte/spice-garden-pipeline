"""
One-command way to save a Flow-downloaded hero clip - for the terminal,
no file manager, no typing/remembering any path.

Finds whatever .mp4 you most recently downloaded to ~/Downloads and
moves+renames it straight into clip-pool/incoming/ under the correct
name for the given slot's pending request (clip-pool/pending/<date>-<slot>/
- if more than one request happens to be pending for that slot, e.g. a
retry, the most recently created one is used). AM and PM requests are
now generated together (see run_pipeline.py's run_batch()), so the slot
argument disambiguates which of the two this clip belongs to.

Usage (from the project root, with the venv active):
    python scripts/save_clip.py AM 1   # right after generating AM's hero clip 1 in Flow
    python scripts/save_clip.py AM 2   # right after generating AM's hero clip 2 in Flow
    python scripts/save_clip.py PM 1   # right after generating PM's hero clip 1 in Flow
    python scripts/save_clip.py PM 2   # right after generating PM's hero clip 2 in Flow
"""
import argparse
import glob
import logging
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import CLIP_POOL_INCOMING_DIR, CLIP_POOL_PENDING_DIR, HERO_CLIPS_PER_SHORT

logger = logging.getLogger(__name__)

DOWNLOADS_DIR = Path.home() / "Downloads"


def _find_current_request(slot: str) -> str:
    slot = slot.upper()
    pending_dirs = (
        [d for d in CLIP_POOL_PENDING_DIR.iterdir() if d.is_dir() and d.name.endswith(f"-{slot}")]
        if CLIP_POOL_PENDING_DIR.exists() else []
    )
    if not pending_dirs:
        raise SystemExit(f"No pending {slot} clip-pool request found under clip-pool/pending/ - nothing to save a clip for.")
    pending_dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    if len(pending_dirs) > 1:
        logger.warning("Multiple pending %s requests found - using the most recent: %s", slot, pending_dirs[0].name)
    return pending_dirs[0].name  # "<date>-<slot>"


def _find_latest_download() -> Path:
    candidates = glob.glob(str(DOWNLOADS_DIR / "*.mp4"))
    if not candidates:
        raise SystemExit(f"No .mp4 files found in {DOWNLOADS_DIR} - download the clip from Flow first.")
    return Path(max(candidates, key=os.path.getmtime))


def save_clip(slot: str, clip_num: int) -> Path:
    if clip_num < 1 or clip_num > HERO_CLIPS_PER_SHORT:
        raise SystemExit(f"clip number must be between 1 and {HERO_CLIPS_PER_SHORT}, got {clip_num}")

    request_name = _find_current_request(slot)
    source = _find_latest_download()
    CLIP_POOL_INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    dest = CLIP_POOL_INCOMING_DIR / f"{request_name}-{clip_num}.mp4"

    if dest.exists():
        logger.warning("%s already exists - overwriting with the newly downloaded clip.", dest)

    shutil.move(str(source), str(dest))
    logger.info("Saved: %s -> %s", source, dest)
    return dest


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description="Move the most recently downloaded .mp4 into clip-pool/incoming/ for the given slot's pending request"
    )
    parser.add_argument("slot", choices=["AM", "PM", "am", "pm"], help="Which video this clip belongs to")
    parser.add_argument("clip_num", type=int, help="Which hero clip this is (1, 2, ...)")
    args = parser.parse_args()
    save_clip(args.slot.upper(), args.clip_num)


if __name__ == "__main__":
    main()
