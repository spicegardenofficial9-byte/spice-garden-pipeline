#!/usr/bin/env bash
# One command to see today's brief - no cd, no venv, nothing else needed.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cat "$DIR/clip-pool/LATEST_BRIEF.txt"
