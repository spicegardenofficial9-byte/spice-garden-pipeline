"""
Reads cost_logs/usage.csv and reports ASSUMED Flow credit spend for the
current calendar month against the confirmed Google AI Pro budget
(1,000 credits/month - automation report Sections 3.2 and 6.2).

"Assumed" because Veo's API does not appear to report exact credits
deducted per call (see stage5_visual_generation.py's module docstring) -
this totals stage5's per-clip assumption (config.CREDITS_PER_VEO_CLIP),
not a verified API figure. Cross-check against Google AI Studio / Cloud
Console's billing dashboard for ground truth.

Informational only - never exits non-zero, so it's safe to run as a
non-blocking step in CI or by hand anytime to sanity-check spend before
scaling volume, per the report's risk register.
"""
import csv
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import COST_LOG_PATH, MONTHLY_FLOW_CREDIT_BUDGET

logger = logging.getLogger(__name__)


def credits_used_this_month() -> int:
    if not COST_LOG_PATH.exists():
        return 0
    now = dt.datetime.now(dt.timezone.utc)
    total = 0
    with open(COST_LOG_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("unit_type") != "flow_credits_assumed":
                continue
            ts = dt.datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
            if ts.year == now.year and ts.month == now.month:
                total += int(float(row["units"]))
    return total


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    used = credits_used_this_month()
    pct = (used / MONTHLY_FLOW_CREDIT_BUDGET) * 100
    logger.info(
        "ASSUMED Flow credits used this month: %d / %d (%.0f%%) - not API-verified, see docstring",
        used, MONTHLY_FLOW_CREDIT_BUDGET, pct,
    )
    if pct >= 90:
        logger.warning("Approaching the monthly Flow credit budget - consider throttling volume.")


if __name__ == "__main__":
    main()
