"""
Stage A - Clip-pool watcher (trigger point for Stages B/C).

Checks clip-pool/incoming/ for a complete set of hero clips matching
<date>-<slot>-<n>.mp4 (n = 1..HERO_CLIPS_PER_SHORT) that also has a
corresponding pending script in clip-pool/pending/<date>-<slot>/script.json
(written earlier by generate_flow_prompt.py, possibly in a previous
scheduled run many hours ago). A request is only "fulfilled" once ALL of
its clips are present - a partial drop (e.g. clip 1 of 2) is left
waiting for the rest.

Finding nothing is NOT a failure - it just means the human hasn't
finished generating today's clips in Flow yet. The caller should exit
cleanly and let the next scheduled run check again.

If multiple fulfilled requests are backlogged (the human got ahead, or a
previous run failed partway through), the one that became complete
earliest is returned; subsequent runs pick up the rest one at a time.
"""
import argparse
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import (
    CLIP_FILENAME_PATTERN, CLIP_POOL_INCOMING_DIR, CLIP_POOL_PENDING_DIR,
    HERO_CLIPS_PER_SHORT,
)

logger = logging.getLogger(__name__)


def find_all_pending_clips():
    """Returns a list of {"clip_paths", "script_path", "pending_dir", "date",
    "slot"} for EVERY fully-fulfilled clip-pool request, oldest-complete first.
    Empty list if none are complete. clip_paths is ordered 1..HERO_CLIPS_PER_SHORT.
    """
    if not CLIP_POOL_INCOMING_DIR.exists():
        return []

    groups = defaultdict(dict)  # (date, slot) -> {clip_num: path}
    for clip_path in CLIP_POOL_INCOMING_DIR.glob("*.mp4"):
        match = re.match(CLIP_FILENAME_PATTERN, clip_path.name)
        if not match:
            logger.warning(
                "Ignoring file that doesn't match <date>-<slot>-<n>.mp4: %s", clip_path.name
            )
            continue
        date, slot, clip_num = match.group(1), match.group(2), int(match.group(3))
        groups[(date, slot)][clip_num] = clip_path

    candidates = []
    for (date, slot), clips_by_num in groups.items():
        if len(clips_by_num) < HERO_CLIPS_PER_SHORT:
            logger.info(
                "Request %s-%s has %d/%d clips so far - waiting for the rest.",
                date, slot, len(clips_by_num), HERO_CLIPS_PER_SHORT,
            )
            continue

        pending_dir = CLIP_POOL_PENDING_DIR / f"{date}-{slot}"
        script_path = pending_dir / "script.json"
        if not script_path.exists():
            logger.warning(
                "Clips for %s-%s have no matching pending script at %s - skipping "
                "(was it dropped for a request that was never made, or "
                "already fulfilled and cleaned up?)",
                date, slot, script_path,
            )
            continue

        clip_paths = [clips_by_num[n] for n in range(1, HERO_CLIPS_PER_SHORT + 1)]
        latest_mtime = max(p.stat().st_mtime for p in clip_paths)
        candidates.append({
            "clip_paths": clip_paths, "script_path": script_path,
            "pending_dir": pending_dir, "date": date, "slot": slot,
            "_sort_key": latest_mtime,
        })

    candidates.sort(key=lambda c: c["_sort_key"])
    for c in candidates:
        del c["_sort_key"]
    return candidates


def find_pending_clip():
    """The oldest fully-fulfilled clip-pool request, or None. Kept for the
    single-build (`--one`) path and standalone use."""
    all_pending = find_all_pending_clips()
    return all_pending[0] if all_pending else None


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Stage A: clip-pool watcher")
    parser.parse_args()

    result = find_pending_clip()
    if result is None:
        logger.info("No fully-fulfilled clip-pool request found - nothing to do this run.")
        sys.exit(0)

    logger.info(
        "Found fulfilled request: %s-%s (%d clips)",
        result["date"], result["slot"], len(result["clip_paths"]),
    )
    for p in result["clip_paths"]:
        print(str(p))
    sys.exit(0)


if __name__ == "__main__":
    main()
