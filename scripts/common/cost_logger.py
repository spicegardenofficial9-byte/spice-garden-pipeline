"""Append-only cost/usage ledger.

Writes to cost_logs/usage.csv, which is a tracked repo file (not a
secret) so real spend can be reviewed via normal git history/diffs.
Never log API keys, tokens, or other secrets here - only spend metadata.
"""
import csv
import datetime as dt

from .config import COST_LOG_PATH

FIELDNAMES = [
    "timestamp_utc", "run_id", "stage", "provider", "model",
    "units", "unit_type", "cost_usd", "notes",
]


def log_cost(run_id, stage, provider, model="", units=0, unit_type="", cost_usd=0.0, notes=""):
    COST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_new = not COST_LOG_PATH.exists()
    with open(COST_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if is_new:
            writer.writeheader()
        writer.writerow({
            "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "run_id": run_id,
            "stage": stage,
            "provider": provider,
            "model": model,
            "units": units,
            "unit_type": unit_type,
            "cost_usd": f"{float(cost_usd):.6f}",
            "notes": notes,
        })
