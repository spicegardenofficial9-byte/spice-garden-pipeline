"""
Changes the thrice-daily GitHub Actions schedule (IST) from the command
line instead of hand-editing YAML.

The pipeline runs one trigger per slot in config.SLOTS (AM, MID, PM). The
first slot's morning trigger generates every slot's script as a batch and
then fulfills; the later triggers only fulfill whatever hero clips have
been dropped into the clip pool by then.

GitHub Actions cron triggers are read from the workflow file as it
exists on GitHub - there is no live API to change a schedule without a
commit, so after running this you still need to commit and push
.github/workflows/pipeline.yml yourself (see SETUP.md) for the new
times to actually take effect on the real schedule. Safe to run and
re-run anytime locally before that - local practice (save_clip.sh,
show_brief.sh, run_pipeline.py --batch) never depends on this schedule.

Usage:
    python scripts/set_schedule.py --am 09:00 --mid 13:00 --pm 17:00
    python scripts/set_schedule.py --mid 12:30            # change just one
"""
import argparse
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import ROOT_DIR, SLOTS

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


def _role_note(idx: int) -> str:
    if idx == 0:
        return f"generates all {'+'.join(SLOTS)} scripts, then fulfills"
    return "fulfills only, no new scripts"


def set_schedule(**times: str) -> dict:
    """times maps lowercased slot names (am/mid/pm) to new HH:MM IST strings.
    Any slot not passed keeps its current cron entry."""
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    cron_matches = re.findall(r'cron: "(\d+ \d+ \* \* \*)"', text)
    if len(cron_matches) != len(SLOTS):
        raise RuntimeError(
            f"expected exactly {len(SLOTS)} schedule entries in {WORKFLOW_PATH}, "
            f"found {len(cron_matches)}"
        )

    new_crons = []
    for slot, old_cron in zip(SLOTS, cron_matches):
        hhmm = times.get(slot.lower())
        new_crons.append(_ist_to_utc_cron(hhmm) if hhmm else old_cron)

    # Rebuild the whole schedule block deterministically so we never depend
    # on the previous inline comments and can't clobber a cron during a
    # time swap between two slots.
    lines = [f'    - cron: "{cron}"   # {_utc_cron_to_ist_label(cron)} IST - {_role_note(i)}'
             for i, cron in enumerate(new_crons)]
    block = "  schedule:\n" + "\n".join(lines) + "\n"
    text, n = re.subn(r'  schedule:\n(?:    - cron: .*\n)+', block, text)
    if n != 1:
        raise RuntimeError(f"could not locate a single schedule: block to rewrite in {WORKFLOW_PATH}")

    WORKFLOW_PATH.write_text(text, encoding="utf-8")
    result = {}
    for slot, cron in zip(SLOTS, new_crons):
        label = _utc_cron_to_ist_label(cron)
        result[slot] = {"ist": label, "cron": cron}
        logger.info("%s trigger: %s IST (cron %r)", slot, label, cron)
    logger.info(
        "Written to %s - this only takes effect on GitHub once you commit and "
        "push it: git add .github/workflows/pipeline.yml && git commit -m "
        "\"chore: update schedule\" && git push",
        WORKFLOW_PATH,
    )
    return result


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Change the GitHub Actions per-slot schedule (IST, 24h HH:MM)")
    for slot in SLOTS:
        parser.add_argument(f"--{slot.lower()}", default=None,
                            help=f"New {slot} trigger time, IST, 24h HH:MM (e.g. 09:00)")
    args = parser.parse_args()
    times = {slot.lower(): getattr(args, slot.lower()) for slot in SLOTS}
    if not any(times.values()):
        parser.error("pass at least one of " + ", ".join(f"--{s.lower()}" for s in SLOTS))
    set_schedule(**{k: v for k, v in times.items() if v})


if __name__ == "__main__":
    main()
