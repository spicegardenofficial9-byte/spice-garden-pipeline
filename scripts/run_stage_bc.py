"""
Stage B orchestrator - the automated part that runs once Stage A
(stageA_clip_pool_watcher.py) finds a fulfilled clip-pool request.

Stage B (automated editing): stills generation, then FFmpeg assembly.
There is no voiceover step anymore - the 60-second format is silent of
narration and carries the hero clips' own native audio under background
music (see stage1/stage6 docstrings). Captions (Stage 4) are likewise
not part of this chain - burned-in captions looked clumsy and YouTube
already auto-generates its own for Shorts, per explicit user decision.
If ANY of these steps fails, the run stops immediately: nothing
is ever uploaded from this script, all intermediate files are preserved
(nothing is ever deleted on failure), and the run's report.json records
exactly which step failed. The source clips and their pending script
are left untouched so the next scheduled run automatically retries the
same request from scratch.

Per explicit user decision, this script NEVER calls Stage 7 (upload) -
that was previously fully automatic, but skipped a human review step
that only worked by accident during manual testing (a person happened
to stop the chain by hand each time). Now, once Stage B succeeds, the
built video is copied to REVIEW_PENDING_DIR/<run_id>/ (final.mp4 +
script.json + review_meta.json) and left there for a human to watch.
Nothing is deleted from clip-pool yet - only scripts/approve_upload.py
(a separate, explicit step) calls Stage 7 and cleans up clip-pool once
upload actually succeeds.
"""
import argparse
import datetime as dt
import logging
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import (
    CREDITS_PER_VEO_CLIP, HERO_CLIPS_PER_SHORT, REPORT_FILENAME,
    REVIEW_PENDING_DIR, run_output_dir,
)
from common.cost_logger import log_cost
from common.io_utils import new_run_id, save_json
from stageA_clip_pool_watcher import find_pending_clip

import stage5_visual_generation as stage5
import stage6_assembly as stage6

logger = logging.getLogger(__name__)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class _Report:
    """Persists to disk after every step so partial progress survives a hard crash."""

    def __init__(self, path: Path, run_id: str, date: str, slot: str, hero_clip_sources: list):
        self.path = path
        self.data = {
            "run_id": run_id, "date": date, "slot": slot,
            "hero_clip_sources": hero_clip_sources,
            "steps": [], "final_status": "in_progress",
        }
        self._save()

    def step_ok(self, name: str, detail: dict = None):
        self.data["steps"].append({"name": name, "status": "ok", "timestamp": _now(), "detail": detail or {}})
        self._save()

    def step_failed(self, name: str, error: Exception):
        self.data["steps"].append({
            "name": name, "status": "failed", "timestamp": _now(), "error": f"{type(error).__name__}: {error}",
        })
        self._save()

    def finish(self, final_status: str):
        self.data["final_status"] = final_status
        self._save()

    def _save(self):
        save_json(self.path, self.data)


def run_stage_bc(clip_info: dict = None) -> dict:
    clip_info = clip_info or find_pending_clip()
    if clip_info is None:
        logger.info("No fulfilled clip-pool request - nothing for Stage B to do this run.")
        return {"final_status": "no_pending_clip"}

    clip_paths = clip_info["clip_paths"]
    script_path = clip_info["script_path"]
    date, slot = clip_info["date"], clip_info["slot"]

    run_id = f"{date}-{slot}-{new_run_id()}"
    output_dir = run_output_dir(run_id)
    shutil.copy(script_path, output_dir / "script.json")
    script_path = output_dir / "script.json"

    report = _Report(output_dir / REPORT_FILENAME, run_id, date, slot, [str(p) for p in clip_paths])
    logger.info("=== Stage B run %s starting (%d clips) ===", run_id, len(clip_paths))

    try:
        stage5.generate_visuals(str(script_path), str(output_dir), run_id, hero_clip_paths=[str(p) for p in clip_paths])
        total_credits = CREDITS_PER_VEO_CLIP * HERO_CLIPS_PER_SHORT
        log_cost(
            run_id=run_id, stage="run_stage_bc", provider="manual-flow", model="veo-3.1-lite",
            units=total_credits, unit_type="flow_credits_assumed", cost_usd=0.0,
            notes=f"{len(clip_paths)} hero clips manually generated in Flow, consumed from clip-pool "
                  f"({', '.join(p.name for p in clip_paths)})",
        )
        report.step_ok("stills_and_hero_clips")
    except Exception as exc:
        logger.error("Stage B failed at stills_and_hero_clips: %s", exc)
        report.step_failed("stills_and_hero_clips", exc)
        report.finish("stage_b_failed")
        return report.data

    try:
        assembly_meta = stage6.assemble(str(output_dir), run_id)
        final_path = output_dir / "final.mp4"
        report.step_ok("assembly", {
            "duration_sec": assembly_meta["duration_sec"],
            "resolution": assembly_meta["resolution"],
            "file_size_bytes": final_path.stat().st_size,
        })
    except Exception as exc:
        logger.error("Stage B failed at assembly: %s", exc)
        report.step_failed("assembly", exc)
        report.finish("stage_b_failed")
        return report.data

    # Hand off to review - copy just what's needed (final.mp4, script.json)
    # plus bookkeeping for approve_upload.py to clean up clip-pool later.
    # clip-pool itself is left untouched until upload is actually approved.
    review_dir = REVIEW_PENDING_DIR / run_id
    review_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(output_dir / "final.mp4", review_dir / "final.mp4")
    shutil.copy(script_path, review_dir / "script.json")
    save_json(review_dir / "review_meta.json", {
        "run_id": run_id, "date": date, "slot": slot,
        "clip_paths": [str(p) for p in clip_paths],
        "pending_dir": str(clip_info["pending_dir"]),
    })

    report.finish("ready_for_review")
    logger.info(
        "=== Stage B complete for %s - built video ready for review at %s ===\n"
        "Watch it, then run: python scripts/approve_upload.py --run-id %s",
        run_id, review_dir / "final.mp4", run_id,
    )
    return report.data


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Stage B: automated editing, stops before upload for review")
    parser.parse_args()

    result = run_stage_bc()
    if result.get("final_status") not in ("ready_for_review", "no_pending_clip"):
        sys.exit(1)


if __name__ == "__main__":
    main()
