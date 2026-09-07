#!/usr/bin/env bash
# One command to publish the video you just reviewed - auto-detects
# which one if there's only one waiting, so you don't need to type/copy
# a run_id. Usage: ./approve.sh          (auto-detect)
#           or:     ./approve.sh <run_id> (if more than one is pending)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1

RUN_ID="$1"
if [ -z "$RUN_ID" ]; then
    PENDING=($(ls review-pending/ 2>/dev/null | grep -v '.gitkeep'))
    if [ ${#PENDING[@]} -eq 0 ]; then
        echo "Nothing waiting for review/approval right now."
        exit 0
    elif [ ${#PENDING[@]} -gt 1 ]; then
        echo "More than one video is waiting - specify which one:"
        printf '  %s\n' "${PENDING[@]}"
        echo "Usage: ./approve.sh <run_id>"
        exit 1
    fi
    RUN_ID="${PENDING[0]}"
fi

read -p "Publish review-pending/$RUN_ID/final.mp4 to YouTube now? [y/N] " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
    echo "Cancelled - nothing published."
    exit 0
fi

source "$DIR/.venv/bin/activate"
python "$DIR/scripts/approve_upload.py" --run-id "$RUN_ID"
