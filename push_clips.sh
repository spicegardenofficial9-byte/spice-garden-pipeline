#!/usr/bin/env bash
# One command to hand your saved clips off to GitHub, instead of
# add/commit/push separately. Usage: ./push_clips.sh
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1

git add clip-pool/
if git diff --staged --quiet; then
    echo "Nothing new to push - did you run save_clips.sh first?"
    exit 0
fi
git commit -m "add hero clips"
git push
