# Shorts Pipeline

A small, personal automation project that generates short-form vertical
videos end-to-end and publishes them on a schedule using GitHub Actions.

The format is a clean **~50-second, no-narration, no-on-screen-text**
vertical video, modelled on a Studio-Ghibli-style cooking short: four
native-audio hero clips (~10s each) plus 1-2 supporting stills, told as
one continuous farm-to-plate story with a consistent character and only
the clips' natural sound. A pre-made ~4-second subscribe animation (with its
own bell sound, dropped at assets/branding/subscribe.mp4) is appended to every
ending. No voiceover, no titles, no ingredient labels, no captions - the only
branding text lives inside that supplied subscribe animation.

It's built as a chain of independent stages rather than one monolithic
script, so each part can be run and debugged on its own:

1. **Script generation** - produces the structured content for the ~50s
   video: 5-6 "moment" segments (each a hero clip or a still, with a
   detailed visual description) forming one continuous story, plus
   metadata used only for the YouTube listing (dish name + region, the
   full ingredient list, an interesting dish fact) and a per-video
   subscribe CTA line. No narration and no on-screen copy are written -
   the visuals and natural sound carry the video.
2. **Review gate** - an automated pass/fail check on the generated
   script before any further (potentially paid) work happens. Designed
   to fail closed: anything ambiguous is treated as a rejection. Checks
   ingredient completeness, moment specificity, and dish-fact soundness.
3. **Voiceover** - retired from the automated chain (this format has no
   narration). The Edge-TTS stage is kept only for standalone use.
4. **Captions** - speech-to-text timing (local, no API key). Not part of
   the automated video pipeline anymore (see below) - still runnable
   standalone if ever needed again.
5. **Visual generation** - produces the supporting stills (Pollinations)
   from each "image" segment's description, and maps the manually-made
   hero clips onto the "video" segments. The short video clips for the
   moments that most need motion aren't API-generated - see
   "Human-in-the-loop" below.
6. **Assembly** - normalises and colour-grades every segment, adds a
   Ken-Burns push on stills, trims each hero clip's laggy head and mildly
   sharpens the upscale, keeps each hero clip's native audio, joins
   everything with short crossfades, appends the supplied ~4s subscribe
   animation (with its bell sound) as the final segment, overlays branding
   TOP-LEFT (keeping any bottom-right generator watermark unobstructed),
   mixes music low under the native audio, and outputs a final vertical MP4
   with FFmpeg. No text
   or captions are burned in.
7. **Upload** - publishes the finished video, with an AI-content
   disclosure flag set on upload. Never runs automatically - see
   "Review before publishing" below.

## Review before publishing

`run_stage_bc.py` (what the schedule runs automatically) builds through
Stage 6 and STOPS - it never calls Stage 7 itself. The built video is
copied to `review-pending/<run_id>/final.mp4` for a human to watch.
Publishing is a separate, deliberate action:
[scripts/approve_upload.py](scripts/approve_upload.py) --run-id
<run_id>, the only script that ever calls Stage 7 - run it locally, or
trigger the manual-only `approve-upload.yml` GitHub Actions workflow.
Only on a successful upload are the consumed clip-pool inputs and the
review-pending entry cleaned up.

## No narration, no on-screen text

This format has no spoken narration and no burned-in text at all - no
titles, no ingredient labels, no per-step captions. The hero clips' own
natural sound plays under background music and the visuals carry the
whole story, exactly like the reference it's modelled on. The only branding
text anywhere lives inside the supplied ~4s subscribe animation appended to
the end (assets/branding/subscribe.mp4; see SUBSCRIBE_ANIMATION_PROMPT.txt
there for how to make it). The
ingredient list and dish fact the script produces are metadata for the
YouTube listing only - see `SCRIPT_PROMPT_TEMPLATE` in
[stage1_script_generation.py](scripts/stage1_script_generation.py).

## Human-in-the-loop: the clip pool

A fixed number of video clips per short (`HERO_CLIPS_PER_SHORT`, 4 for
this format, each ~10s with its own duration cap in
`HERO_CLIP_DURATIONS_SEC`) are generated manually in a separate creative
tool (e.g. Omni 1.1 Flash), then dropped into a watched "clip pool" folder via
[scripts/save_clip.py](scripts/save_clip.py) (or the one-command
`save_clip.sh` wrapper) - the automation picks them up from there and
builds the video (stills, assembly) with no further input,
then stops for review (see above). A request isn't considered fulfilled
until all of its clips have arrived. If nothing is waiting yet, a
scheduled run just checks, finds nothing, and exits cleanly rather than
failing - it tries again next scheduled run.

AM and PM scripts/requests are generated together in one batch (see
`run_batch()` in [run_pipeline.py](scripts/run_pipeline.py)) so all
clips for both videos can be produced in a single Flow sitting - see
the combined brief at `clip-pool/LATEST_BRIEF.txt` (or run
`show_brief.sh`).

## Running a stage locally

Each stage is a standalone script under `scripts/` that reads a JSON
input and writes JSON/media output, so you can test any one of them in
isolation without running the rest of the pipeline. See
[SETUP.md](SETUP.md) for exact commands and required environment
variables per stage.

Each scheduled run does two independent things: generate + review
today's scripts (and request clips for them), and separately check
whether any earlier request has been fulfilled yet:

```bash
python scripts/run_pipeline.py --batch --date 2026-09-07   # request half (both AM+PM)
python scripts/run_stage_bc.py                              # build half - stops before upload
python scripts/approve_upload.py --run-id <run_id>           # separate, manual: actually publish
```

## Automation

`.github/workflows/pipeline.yml` runs the request+build halves on a
twice-daily schedule via GitHub Actions and can also be triggered
manually - it never uploads. `.github/workflows/approve-upload.yml` is
a separate, manual-only workflow (or run `approve_upload.py` locally)
for the explicit "publish this one" action. Secrets (API keys, OAuth
tokens) are configured as GitHub Actions Secrets and are never
committed to the repository.

Per-run cost/usage is logged to [`cost_logs/usage.csv`](cost_logs/usage.csv)
so spend can be tracked over time directly in git history, and checked
against a monthly budget with `scripts/check_credit_budget.py`.

## Requirements

- Python 3.11+
- `ffmpeg` / `ffprobe` on `PATH`
- See [requirements.txt](requirements.txt) for Python dependencies
