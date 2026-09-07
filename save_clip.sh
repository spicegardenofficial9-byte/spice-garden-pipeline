#!/usr/bin/env bash
# One command to push a downloaded Flow clip into the pipeline - no cd,
# no manual venv activation. Usage: ./save_clip.sh AM 1
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/.venv/bin/activate"
python "$DIR/scripts/save_clip.py" "$@"
