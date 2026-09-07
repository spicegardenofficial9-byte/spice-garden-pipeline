"""
Deletes old run folders under output/ so local/CI disk usage doesn't grow
forever. Each pipeline run writes output/<run_id>/ (script.json, stills,
audio, captions, the final MP4) - once you've reviewed/uploaded a video
there's no reason to keep that folder around indefinitely.

A run folder is deleted once it's older than OUTPUT_RETENTION_DAYS (see
common/config.py, default 14 days, override via the env var of the same
name). Age is based on the folder's last-modified time, so a folder is
never removed sooner than that many days after the run actually finished.

This only touches output/ - it never touches clip-pool/ (incoming clips,
pending requests) or cost_logs/, since those hold state the pipeline
still needs or a record you'd want to keep.

Run manually any time:
    python scripts/cleanup_old_runs.py
    python scripts/cleanup_old_runs.py --dry-run   # just list what would go

Also run automatically at the start of every run_pipeline.py invocation,
so cleanup happens twice a day without any separate step to remember.
"""
import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import OUTPUT_DIR, OUTPUT_RETENTION_DAYS

logger = logging.getLogger(__name__)


def cleanup_old_runs(retention_days: int = None, dry_run: bool = False) -> list:
    retention_days = OUTPUT_RETENTION_DAYS if retention_days is None else retention_days
    cutoff = time.time() - retention_days * 86400
    removed = []

    if not OUTPUT_DIR.exists():
        return removed

    for run_dir in OUTPUT_DIR.iterdir():
        if not run_dir.is_dir():
            continue
        if run_dir.stat().st_mtime >= cutoff:
            continue
        removed.append(str(run_dir))
        if dry_run:
            logger.info("[dry-run] would remove %s", run_dir)
        else:
            shutil.rmtree(run_dir, ignore_errors=True)
            logger.info("Removed old run folder %s (older than %d days)", run_dir, retention_days)

    if not removed:
        logger.info("No run folders older than %d days - nothing to clean up.", retention_days)
    return removed


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Delete output/ run folders older than the retention window")
    parser.add_argument("--retention-days", type=int, default=None, help="Override OUTPUT_RETENTION_DAYS")
    parser.add_argument("--dry-run", action="store_true", help="List what would be removed without deleting")
    args = parser.parse_args()
    cleanup_old_runs(args.retention_days, args.dry_run)


if __name__ == "__main__":
    main()
