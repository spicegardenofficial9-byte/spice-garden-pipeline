"""
Reject a built video that's waiting in review, and RE-EDIT it from the
SAME already-generated hero clips - never regenerate the clips.

Per explicit user decision: if a review video looks bad, throw away ONLY
the edited video and build a fresh edit from the same clips (the paid
Omni/Flow generations are expensive, so they are reused, not remade).

What it does, for a given run_id under review-pending/<run_id>/:
  1. Deletes ONLY that edited output: review-pending/<run_id>/ and, if
     present, the working dir output/<run_id>/.
  2. Leaves the clip pool completely untouched - the hero clips in
     clip-pool/incoming/ and the fulfilled request in clip-pool/pending/
     stay exactly where they are.
  3. Re-runs Stage B (run_stage_bc), which finds those same still-present
     clips and assembles a NEW edit into a fresh review-pending entry for
     you to review again. (Supporting stills are regenerated, so the new
     edit genuinely differs; the hero clips are identical and unre-billed.)

Usage:
    python scripts/reject_review.py --run-id <run_id>        # reject + re-edit
    python scripts/reject_review.py --run-id <run_id> --no-rebuild  # just delete

Nothing here ever uploads or deletes a hero clip.
"""
import argparse
import logging
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import OUTPUT_DIR, REVIEW_PENDING_DIR

import run_stage_bc

logger = logging.getLogger(__name__)


def reject_review(run_id: str, rebuild: bool = True) -> dict:
    review_dir = REVIEW_PENDING_DIR / run_id
    if not review_dir.exists():
        raise SystemExit(
            f"No review-pending entry for run_id {run_id!r} at {review_dir} - "
            "check the run_id (see review-pending/)."
        )

    # Delete ONLY the edited video + its working dir. The clip pool is never
    # touched here, so the same hero clips remain available to rebuild from.
    shutil.rmtree(review_dir, ignore_errors=True)
    stale_out = OUTPUT_DIR / run_id
    if stale_out.exists():
        shutil.rmtree(stale_out, ignore_errors=True)
    logger.info("Rejected and removed edited video for %s (clips kept in the pool).", run_id)

    if not rebuild:
        return {"rejected": run_id, "rebuilt": None}

    logger.info("Re-editing from the SAME clips (no regeneration)...")
    result = run_stage_bc.run_stage_bc()
    new_run = result.get("run_id")
    status = result.get("final_status")
    if status == "no_pending_clip":
        logger.warning(
            "No fulfilled clip-pool request found to rebuild from - were the clips "
            "removed? Nothing was rebuilt."
        )
    else:
        logger.info("Re-edit complete: new review video is %s (status=%s).", new_run, status)
    return {"rejected": run_id, "rebuilt": new_run, "final_status": status}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Reject a review video and re-edit from the same clips")
    parser.add_argument("--run-id", required=True, help="run_id under review-pending/ to reject")
    parser.add_argument("--no-rebuild", action="store_true", help="only delete the edit, don't re-edit")
    args = parser.parse_args()
    reject_review(args.run_id, rebuild=not args.no_rebuild)


if __name__ == "__main__":
    main()
