#!/usr/bin/env bash
# One command to pull the latest built video and open it for you to
# watch, instead of git pull + ls + xdg-open separately.
# Usage: ./review.sh
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1

echo "Pulling latest from GitHub..."
git pull --quiet

LATEST=$(ls -t review-pending/ 2>/dev/null | grep -v '.gitkeep' | head -1)
if [ -z "$LATEST" ]; then
    echo "Nothing waiting for review yet - the build may not have run yet, or nothing's fulfilled."
    exit 0
fi

echo "Opening review-pending/$LATEST/final.mp4"
echo "Run_id: $LATEST"
echo "If it looks good, publish it with:"
echo "  ./approve.sh"
xdg-open "review-pending/$LATEST/final.mp4"
