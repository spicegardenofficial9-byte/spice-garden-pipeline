"""
Runs Stage 1 (script generation) + Stage 2 (review gate) for one or both
daily slots, and - for whichever slots pass review - writes the Flow
prompts + pending scripts that a human will later fulfill by dropping
hero clips into clip-pool/incoming/ (see generate_flow_prompt.py and
stageA_clip_pool_watcher.py).

This is the first half of each scheduled workflow run; run_stage_bc.py
is the second half, consuming whatever clip-pool request happens to be
fulfilled by the time a run executes (which may be a request from many
hours or a full day earlier, not necessarily this one).

run_batch() generates BOTH the AM and PM scripts together in one call -
per explicit user request, so the human can generate all
HERO_CLIPS_PER_SHORT * 2 (4, by default) hero clips in a single Flow
sitting instead of two separate sessions spread across the day. The
workflow's morning cron trigger calls this once; run_stage_bc.py still
runs on both the AM and PM triggers to consume whichever request has
been fulfilled by then.

A rejected script is not treated as a failure worth stopping the
workflow over - it just means no Flow prompt gets written this cycle
for that slot, so the human is never asked to spend Flow credits
generating a clip for a script that didn't pass review.
"""
import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cleanup_old_runs import cleanup_old_runs
from common.config import run_output_dir
from common.io_utils import load_json, new_run_id
from generate_flow_prompt import generate_flow_prompt, write_combined_brief, write_request

import stage1_script_generation as stage1
import stage2_review_gate as stage2

logger = logging.getLogger(__name__)


def _generate_and_review(topic_brief: str, slot: str) -> dict:
    run_id = new_run_id()
    output_dir = str(run_output_dir(run_id))
    logger.info("--- Generating %s script (run %s) ---", slot, run_id)

    stage1.generate_script(topic_brief, output_dir, run_id)
    script_path = f"{output_dir}/script.json"

    review = stage2.review_script(script_path, output_dir, run_id)
    if not review["approved"]:
        logger.error("%s script rejected - no Flow prompt written: %s", slot, review["reasons"])
        return {"slot": slot, "approved": False, "reasons": review["reasons"], "run_id": run_id}

    return {"slot": slot, "approved": True, "run_id": run_id, "script_path": script_path}


def run(topic_brief: str = None, date: str = None, slot: str = None) -> dict:
    """Single-slot path - kept for manual/one-off runs and testing."""
    date = date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    slot = (slot or "AM").upper()
    cleanup_old_runs()

    result = _generate_and_review(topic_brief, slot)
    if not result["approved"]:
        return {"approved": False, "reasons": result["reasons"], "run_id": result["run_id"]}

    pending_dir = generate_flow_prompt(result["script_path"], date, slot)
    logger.info("=== Flow prompt ready at %s ===", pending_dir)
    return {"approved": True, "pending_dir": str(pending_dir), "run_id": result["run_id"]}


def run_batch(topic_briefs: dict = None, date: str = None) -> dict:
    """Generates AM and PM scripts together and writes ONE combined brief
    covering whichever of them passed review. topic_briefs, if given, is
    {"AM": path_or_None, "PM": path_or_None}.
    """
    date = date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    topic_briefs = topic_briefs or {}
    cleanup_old_runs()

    logger.info("=== Batch request-generation starting for %s (AM + PM) ===", date)
    approved_scripts = {}
    rejected = {}
    for slot in ("AM", "PM"):
        result = _generate_and_review(topic_briefs.get(slot), slot)
        if result["approved"]:
            script = load_json(result["script_path"])
            pending_dir = write_request(script, date, slot, result["script_path"])
            approved_scripts[slot] = script
            logger.info("%s approved - pending request written to %s", slot, pending_dir)
        else:
            rejected[slot] = result["reasons"]

    brief_path = write_combined_brief(date, approved_scripts, rejected)
    logger.info(
        "=== Batch complete: %d/2 slots approved (%s) - brief at %s ===",
        len(approved_scripts), ", ".join(approved_scripts) or "none", brief_path,
    )
    return {"approved_slots": list(approved_scripts), "rejected_slots": rejected, "brief_path": str(brief_path)}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Stage 1+2: generate today's script(s) and Flow prompt request(s)")
    parser.add_argument("--topic-brief", default=None, help="Path to a topic brief JSON (single-slot mode only)")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to today (UTC)")
    parser.add_argument("--slot", default="AM", choices=["AM", "PM", "am", "pm"], help="Single-slot mode only")
    parser.add_argument("--batch", action="store_true", help="Generate AM and PM together into one combined brief")
    args = parser.parse_args()

    if args.batch:
        run_batch(date=args.date)
    else:
        run(args.topic_brief, args.date, args.slot)


if __name__ == "__main__":
    main()
