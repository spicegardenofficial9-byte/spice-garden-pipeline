#!/usr/bin/env bash
# One command to save ALL of a slot's clips at once, instead of one
# command per clip. Generate + download every clip for the slot in Flow
# first, THEN run this once. Usage: ./save_clips.sh AM
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/.venv/bin/activate"
python "$DIR/scripts/save_clips_batch.py" "$@"
