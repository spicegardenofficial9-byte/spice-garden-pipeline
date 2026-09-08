"""
The ONLY script that ever calls Stage 7 (YouTube upload). Everything
upstream (run_stage_bc.py) stops at a built, not-yet-published
final.mp4 under review-pending/<run_id>/ specifically so a human can
watch it first - this script is the explicit "yes, publish this one"
action, run only after that review.

Usage (locally, with .env's YOUTUBE_* credentials, or via the
approve-upload.yml manual GitHub Actions workflow using repo Secrets):
    python scripts/approve_upload.py --run-id 2026-09-07-AM-20260907T...

On success: uploads via Stage 7, then cleans up the clip-pool inputs
that were consumed for this video (the hero clips in
clip-pool/incoming/ and the fulfilled request in clip-pool/pending/)
and removes the review-pending/<run_id>/ entry - there's no reason to
keep any of it once the video is actually live.

On failure: nothing is deleted. review-pending/<run_id>/ (including
final.mp4) is left exactly as-is so you can just re-run this same
command to retry once the underlying problem (expired token, network,
quota) is fixed.
"""
import argparse
import datetime as dt
import logging
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import IST_OFFSET_MINUTES, REVIEW_PENDING_DIR, SLOT_PUBLISH_SCHEDULE
from common.io_utils import load_json

import stage7_upload as stage7

logger = logging.getLogger(__name__)


def publish_at_for_slot(slot: str, now_utc: dt.datetime = None) -> str:
    """RFC3339 UTC timestamp for this slot's daily publish time (IST), or None
    for 'immediate'. If today's time has already passed, schedules the next day
    so YouTube's publishAt is always in the future."""
    spec = SLOT_PUBLISH_SCHEDULE.get(slot, "immediate")
    if spec == "immediate":
        return None
    hh, mm = (int(x) for x in spec.split(":"))
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    ist_off = dt.timedelta(minutes=IST_OFFSET_MINUTES)
    ist_now = now_utc + ist_off  # current IST wall-clock (as an aware UTC dt)
    target_ist = ist_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target_ist <= ist_now:
        target_ist += dt.timedelta(days=1)
    target_utc = target_ist - ist_off
    return target_utc.isoformat(timespec="seconds").replace("+00:00", "Z")


def approve_upload(run_id: str, privacy_status: str = None, publish_at: str = None) -> dict:
    review_dir = REVIEW_PENDING_DIR / run_id
    meta_path = review_dir / "review_meta.json"
    if not meta_path.exists():
        raise SystemExit(
            f"No review-pending entry for run_id {run_id!r} at {review_dir} - "
            "check the run_id, or has this one already been approved/uploaded?"
        )
    meta = load_json(meta_path)

    slot = meta.get("slot", "")
    # Per-slot daily schedule unless explicitly overridden on the command line.
    if publish_at is None and privacy_status is None:
        publish_at = publish_at_for_slot(slot)

    when = f"scheduled for {publish_at}" if publish_at else "immediately (public)"
    logger.info("Uploading %s (built from %s-%s) - publishing %s...", run_id, meta["date"], slot, when)
    result = stage7.upload_video(str(review_dir), run_id, privacy_status, publish_at)
    logger.info("Published: %s (%s)", result["url"], when)

    # Only clean up once the upload actually succeeded.
    for clip_path_str in meta["clip_paths"]:
        Path(clip_path_str).unlink(missing_ok=True)
    shutil.rmtree(meta["pending_dir"], ignore_errors=True)
    shutil.rmtree(review_dir, ignore_errors=True)
    logger.info(
        "Cleaned up clip-pool inputs for %s-%s and removed the review-pending entry.",
        meta["date"], meta["slot"],
    )
    return result


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Approve and publish a reviewed video (the only script that uploads)")
    parser.add_argument("--run-id", required=True, help="run_id shown by run_stage_bc.py")
    parser.add_argument("--privacy-status", default=None, choices=["public", "unlisted", "private"],
                        help="override; disables the per-slot schedule")
    parser.add_argument("--publish-at", default=None,
                        help="override the scheduled publish time (RFC3339 UTC, e.g. 2026-09-08T07:00:00Z)")
    args = parser.parse_args()
    approve_upload(args.run_id, args.privacy_status, args.publish_at)


if __name__ == "__main__":
    main()
