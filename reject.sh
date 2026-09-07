#!/usr/bin/env bash
# One command to REJECT the video you just reviewed and re-edit it from
# the SAME clips (never regenerates the paid clips). Auto-detects which
# one if only one is waiting.
# Usage: ./reject.sh            (auto-detect)
#   or:  ./reject.sh <run_id>   (if more than one is pending)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1

RUN_ID="$1"
if [ -z "$RUN_ID" ]; then
    PENDING=($(ls review-pending/ 2>/dev/null | grep -v '.gitkeep'))
    if [ ${#PENDING[@]} -eq 0 ]; then
        echo "Nothing waiting for review right now."
        exit 0
    elif [ ${#PENDING[@]} -gt 1 ]; then
        echo "More than one video is waiting - specify which one:"
        printf '  %s\n' "${PENDING[@]}"
        echo "Usage: ./reject.sh <run_id>"
        exit 1
    fi
    RUN_ID="${PENDING[0]}"
fi

read -p "Reject review-pending/$RUN_ID and re-edit from the same clips? [y/N] " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
    echo "Cancelled - nothing changed."
    exit 0
fi

source "$DIR/.venv/bin/activate"
python "$DIR/scripts/reject_review.py" --run-id "$RUN_ID"
echo
echo "A fresh edit was built (same clips). Review it with:  ./review.sh"
echo "Then commit the change so the new video is saved:      ./push_clips.sh  (or git add review-pending && git commit && git push)"
