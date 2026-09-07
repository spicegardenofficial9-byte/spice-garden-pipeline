#!/usr/bin/env bash
# One command for the start of your day: pulls whatever the automation
# built/wrote overnight, then shows the brief. Usage: ./daily.sh
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1
echo "Pulling latest from GitHub..."
git pull --quiet
echo
cat "$DIR/clip-pool/LATEST_BRIEF.txt"
