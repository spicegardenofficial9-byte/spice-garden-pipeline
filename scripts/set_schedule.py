"""
Changes the twice-daily GitHub Actions schedule (IST) from the command
line instead of hand-editing YAML.

GitHub Actions cron triggers are read from the workflow file as it
exists on GitHub - there is no live API to change a schedule without a
commit, so after running this you still need to commit and push
.github/workflows/pipeline.yml yourself (see SETUP.md) for the new
times to actually take effect on the real schedule. Safe to run and
re-run anytime locally before that - local practice (save_clip.sh,
show_brief.sh, run_pipeline.py --batch) never depends on this schedule.

Usage:
    python scripts/set_schedule.py --am 09:00 --pm 18:00
    python scripts/set_schedule.py --am 07:30            # change just one
"""
import argparse
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import ROOT_DIR

logger = logging.getLogger(__name__)

WORKFLOW_PATH = ROOT_DIR / ".github" / "workflows" / "pipeline.yml"
IST_OFFSET_MIN = 5 * 60 + 30


def _parse_hhmm(hhmm: str) -> int:
    hh, mm = (int(x) for x in hhmm.split(":"))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"invalid time {hhmm!r}, expected 24h HH:MM")
    return hh * 60 + mm


def _ist_to_utc_cron(hhmm: str) -> str:
    total = (_parse_hhmm(hhmm) - IST_OFFSET_MIN) % (24 * 60)
    return f"{total % 60} {total // 60} * * *"


def _utc_cron_to_ist_label(cron: str) -> str:
    minute, hour = (int(x) for x in cron.split()[:2])
    total = (hour * 60 + minute + IST_OFFSET_MIN) % (24 * 60)
    h, m = total // 60, total % 60
    period = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {period}"


def set_schedule(am: str = None, pm: str = None) -> dict:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    cron_matches = re.findall(r'cron: "(\d+ \d+ \* \* \*)"', text)
    if len(cron_matches) != 2:
        raise RuntimeError(f"expected exactly 2 schedule entries in {WORKFLOW_PATH}, found {len(cron_matches)}")
    old_am_cron, old_pm_cron = cron_matches

    new_am_cron = _ist_to_utc_cron(am) if am else old_am_cron
    new_pm_cron = _ist_to_utc_cron(pm) if pm else old_pm_cron

    # Swap via placeholders first so an AM<->PM time swap can't clobber itself.
    text = text.replace(old_am_cron, "__AM_CRON__").replace(old_pm_cron, "__PM_CRON__")
    text = text.replace("__AM_CRON__", new_am_cron).replace("__PM_CRON__", new_pm_cron)

    am_label = _utc_cron_to_ist_label(new_am_cron)
    pm_label = _utc_cron_to_ist_label(new_pm_cron)
    text = re.sub(
        r"# [\d:APM ]+ IST - generates both AM\+PM scripts, then fulfills",
        f"# {am_label} IST - generates both AM+PM scripts, then fulfills", text,
    )
    text = re.sub(
        r"# [\d:APM ]+ IST - fulfills only, no new scripts",
        f"# {pm_label} IST - fulfills only, no new scripts", text,
    )

    WORKFLOW_PATH.write_text(text, encoding="utf-8")
    logger.info("AM trigger: %s IST (cron %r)", am_label, new_am_cron)
    logger.info("PM trigger: %s IST (cron %r)", pm_label, new_pm_cron)
    logger.info(
        "Written to %s - this only takes effect on GitHub once you commit and "
        "push it: git add .github/workflows/pipeline.yml && git commit -m "
        "\"chore: update schedule\" && git push",
        WORKFLOW_PATH,
    )
    return {"am_ist": am_label, "pm_ist": pm_label, "am_cron": new_am_cron, "pm_cron": new_pm_cron}


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Change the GitHub Actions AM/PM schedule (IST, 24h HH:MM)")
    parser.add_argument("--am", default=None, help="New AM trigger time, IST, 24h HH:MM (e.g. 09:00)")
    parser.add_argument("--pm", default=None, help="New PM trigger time, IST, 24h HH:MM (e.g. 18:00)")
    args = parser.parse_args()
    if not args.am and not args.pm:
        parser.error("pass at least one of --am or --pm")
    set_schedule(args.am, args.pm)


if __name__ == "__main__":
    main()
