"""
Stage 6 - Assembly (FFmpeg), for the clean ~50-second, no-text format.

Fully local stage: no external API. Requires `ffmpeg` and `ffprobe` on PATH.

Combines the Stage 5 visuals into the final 1080x1920 video as a clean,
continuous Studio-Ghibli-style cooking story - modelled on the reference the
user provided: no voiceover, NO on-screen text of any kind, only the clips'
own natural sound. Specifically:

  1. Each segment is normalised to the same resolution / fps / pixel format
     and colour-corrected with one shared filter (config.COLOR_NORMALIZE_FILTER)
     so the illustrated stills and the (upscaled) hero clips don't jar in
     exposure or colour temperature sitting next to each other.
  2. A still gets a Ken-Burns push. A hero clip is scaled/cropped to frame,
     has its laggy head trimmed (config.CLIP_LAG_TRIM_SEC) so the cut lands on
     real motion, gets a mild unsharp (config.UPSCALE_SHARPEN_FILTER) to
     counter the 360p->1080p upscale, and keeps its OWN native audio (sizzle,
     pour, chop). Stills contribute silence.
  3. NO text cards, NO titles, NO ingredient labels - nothing is burned in.
  4. Segments are joined with short, fast crossfades
     (config.TRANSITION_DURATION_SEC), video via xfade and audio via
     acrossfade - clean dissolves, no flashing/strobing.
  5. Pacing is redistributed to match each moment's energy: quicker holds on
     early/prep beats, a longer hold on the final plated dish, preserving the
     overall ~50s length.
  6. A pre-made ~4s subscribe animation asset (config.SUBSCRIBE_ANIMATION_PATH),
     with its own bell-chime sound, is appended (crossfaded) as the FINAL
     segment of every video. There is no drawn/overlaid subscribe card - if the
     asset is missing the video simply ends on the final dish shot.
  7. Background music (if present) is mixed low under the native audio.

Watermark policy: if the source clips carry a generator watermark it sits in
the BOTTOM-RIGHT; the channel logo overlays TOP-LEFT and the subscribe card is
centred upper-middle, both kept clear of the reserved bottom-right band
(config.WATERMARK_RESERVED_*).

Input (<output_dir>/):
    visuals_meta.json + visuals/   (Stage 5)
    script.json                    (Stage 1; subscribe_cta_text, duration)

Output (<output_dir>/):
    final.mp4
    assembly_meta.json
"""
import argparse
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import (
    BRANDING_LOGO_PATH, BRANDING_LOGO_WIDTH_PX, BRANDING_MARGIN_PX,
    CLIP_LAG_TRIM_SEC, COLOR_NORMALIZE_FILTER, MUSIC_DIR,
    PACING_HOLD_MULTIPLIER, PACING_QUICK_MULTIPLIER,
    SUBSCRIBE_ANIMATION_PATH, SUBSCRIBE_CTA_DURATION_SEC, TARGET_DURATION_SEC,
    TRANSITION_DURATION_SEC, UPSCALE_SHARPEN_FILTER, VIDEO_FPS, VIDEO_HEIGHT,
    VIDEO_WIDTH, WATERMARK_RESERVED_H_PX, WATERMARK_RESERVED_W_PX,
    get_env, run_output_dir,
)
from common.io_utils import load_json, save_json

logger = logging.getLogger(__name__)

MUSIC_VOLUME = 0.12

# Common per-segment video normalisation for HERO CLIPS: fill the 1080x1920
# frame, lock fps and SAR, apply the shared colour correction, then a mild
# sharpen to recover crispness lost upscaling a low-res (360p) source.
_CLIP_VF = (
    f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
    f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},fps={VIDEO_FPS},"
    f"{COLOR_NORMALIZE_FILTER},{UPSCALE_SHARPEN_FILTER},setsar=1,format=yuv420p"
)


def _run(cmd):
    logger.debug("ffmpeg cmd: %s", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, capture_output=True)


def _ffprobe_has_audio(path: Path) -> bool:
    """True if the file has at least one audio stream (real hero clips do;
    mock placeholder clips and stills don't)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True,
        )
        return bool(out.stdout.strip())
    except subprocess.CalledProcessError:
        return False


# --------------------------------------------------------------------------
# Pacing
# --------------------------------------------------------------------------

def _pacing_durations(beats: list, total: float) -> list:
    """Redistribute segment lengths to match each moment's energy while
    preserving `total`. The first segment (a quiet establishing / prep beat)
    is cut quicker; the final segment (the plated dish, which also backs the
    subscribe card) is held longer. Everything else keeps weight 1.0.
    """
    base = [max(0.5, b["end_sec"] - b["start_sec"]) for b in beats]
    if not base:
        return base
    weights = [1.0] * len(base)
    weights[0] = PACING_QUICK_MULTIPLIER
    mid = len(base) // 2
    for i, b in enumerate(beats):
        if 0 < i < mid and b.get("type") == "image":
            weights[i] = PACING_QUICK_MULTIPLIER
    weights[-1] = PACING_HOLD_MULTIPLIER

    weighted = [b * w for b, w in zip(base, weights)]
    scale = total / sum(weighted)
    return [round(w * scale, 3) for w in weighted]


# --------------------------------------------------------------------------
# Subscribe ending (a user-supplied animation asset with its own bell sound)
# --------------------------------------------------------------------------

def _ffprobe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True,
        )
        return float(out.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return 0.0


def _build_subscribe_segment(anim_path: Path, out_path: Path) -> float:
    """Normalise the pre-made subscribe animation into a final segment: scale
    to frame, lock fps/SAR, keep its OWN audio (the bell chime) or add silence
    if it has none. NOT colour-graded/sharpened/lag-trimmed - it's a finished
    branded asset. Capped at SUBSCRIBE_CTA_DURATION_SEC. Returns its duration."""
    src_dur = _ffprobe_duration(anim_path)
    dur = min(SUBSCRIBE_CTA_DURATION_SEC, src_dur) if src_dur > 0 else SUBSCRIBE_CTA_DURATION_SEC
    vf = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},fps={VIDEO_FPS},setsar=1,format=yuv420p"
    )
    cmd = ["ffmpeg", "-y", "-i", str(anim_path)]
    if _ffprobe_has_audio(anim_path):
        filter_complex = (
            f"[0:v]{vf}[v];"
            f"[0:a]apad,aformat=sample_rates=44100:channel_layouts=stereo[a]"
        )
        audio_map = "[a]"
    else:
        cmd += ["-f", "lavfi", "-t", str(dur), "-i", "anullsrc=r=44100:cl=stereo"]
        filter_complex = f"[0:v]{vf}[v]"
        audio_map = "1:a"
    cmd += [
        "-filter_complex", filter_complex, "-map", "[v]", "-map", audio_map,
        "-r", str(VIDEO_FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-t", str(dur), str(out_path),
    ]
    _run(cmd)
    return round(dur, 3)


# --------------------------------------------------------------------------
# Segment builders (each outputs a normalised mp4 with video + audio streams
# of exactly `duration` - no on-screen text)
# --------------------------------------------------------------------------

def _build_still_segment(image_path: Path, duration: float, out_path: Path) -> float:
    num_frames = max(1, round(duration * VIDEO_FPS))
    kb = (
        f"scale={VIDEO_WIDTH*2}:{VIDEO_HEIGHT*2}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH*2}:{VIDEO_HEIGHT*2},"
        f"zoompan=z='min(zoom+0.0022,1.35)':d={num_frames}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={VIDEO_FPS},"
        f"{COLOR_NORMALIZE_FILTER},setsar=1,format=yuv420p"
    )
    _run([
        "ffmpeg", "-y",
        "-loop", "1", "-t", str(duration), "-i", str(image_path),
        "-f", "lavfi", "-t", str(duration), "-i", "anullsrc=r=44100:cl=stereo",
        "-vf", kb, "-map", "0:v", "-map", "1:a",
        "-r", str(VIDEO_FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-t", str(duration), str(out_path),
    ])
    return round(duration, 3)


def _build_clip_segment(video_path: Path, duration: float, out_path: Path) -> float:
    """Hero clip: trim the head lag, scale/colour/sharpen, keep native audio
    (or add silence). The segment is CAPPED at the clip's real usable length
    (source duration minus the lag trim) - we never loop or freeze to stretch
    it, which also avoids an ffmpeg -stream_loop hang seen on some clips.
    Returns the actual segment duration used (so the crossfade stays in sync).
    """
    lag = CLIP_LAG_TRIM_SEC
    src_dur = _ffprobe_duration(video_path)
    usable = max(0.5, src_dur - lag) if src_dur > 0 else duration
    dur = round(min(duration, usable), 3)

    has_audio = _ffprobe_has_audio(video_path)
    vf = f"[0:v]trim=start={lag},setpts=PTS-STARTPTS,{_CLIP_VF}[v]"
    cmd = ["ffmpeg", "-y", "-i", str(video_path)]
    if has_audio:
        filter_complex = (
            f"{vf};"
            f"[0:a]atrim=start={lag},asetpts=PTS-STARTPTS,"
            f"aformat=sample_rates=44100:channel_layouts=stereo[a]"
        )
        audio_map = "[a]"
    else:
        cmd += ["-f", "lavfi", "-t", str(dur), "-i", "anullsrc=r=44100:cl=stereo"]
        filter_complex = vf
        audio_map = "1:a"
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", audio_map,
        "-r", str(VIDEO_FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-t", str(dur), str(out_path),
    ]
    _run(cmd)
    return dur


# --------------------------------------------------------------------------
# Crossfade join
# --------------------------------------------------------------------------

def _crossfade(segment_paths: list, durations: list, out_path: Path) -> float:
    """xfade the video and acrossfade the audio of all segments with
    TRANSITION_DURATION_SEC dissolves. Returns the resulting total duration."""
    t = TRANSITION_DURATION_SEC
    n = len(segment_paths)
    if n == 1:
        segment_paths[0].replace(out_path)
        return durations[0]

    inputs = []
    for p in segment_paths:
        inputs += ["-i", str(p)]

    vfilters, afilters = [], []
    prev_v = "[0:v]"
    prev_a = "[0:a]"
    cum = durations[0]
    for i in range(1, n):
        offset = cum - t
        out_v = f"[v{i}]"
        vfilters.append(
            f"{prev_v}[{i}:v]xfade=transition=fade:duration={t}:offset={offset:.3f}{out_v}"
        )
        out_a = f"[a{i}]"
        afilters.append(f"{prev_a}[{i}:a]acrossfade=d={t}{out_a}")
        prev_v, prev_a = out_v, out_a
        cum = cum + durations[i] - t

    filter_complex = ";".join(vfilters + afilters)
    _run([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", prev_v, "-map", prev_a,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(out_path),
    ])
    return round(cum, 3)


# --------------------------------------------------------------------------
# Music
# --------------------------------------------------------------------------

def _pick_music_track():
    override = get_env("MUSIC_TRACK")
    if override:
        return MUSIC_DIR / override
    if MUSIC_DIR.exists():
        tracks = sorted(MUSIC_DIR.glob("*.mp3"))
        if tracks:
            return tracks[0]
    return None


# --------------------------------------------------------------------------
# Finalise: animated subscribe overlay + top-left branding + music mix
# --------------------------------------------------------------------------

def _finalize(combined: Path, total_duration: float, out_path: Path):
    """Overlay the channel logo (top-left, watermark-safe) and mix low
    background music under the native audio. The subscribe ending is already
    baked into `combined` as its own final segment, so nothing is drawn here."""
    inputs = ["-i", str(combined)]
    parts = []
    last_v = "0:v"
    next_idx = 1

    logo = BRANDING_LOGO_PATH
    if logo.exists():
        inputs += ["-i", str(logo)]
        parts.append(f"[{next_idx}:v]scale={BRANDING_LOGO_WIDTH_PX}:-1[logo]")
        parts.append(f"[0:v][logo]overlay={BRANDING_MARGIN_PX}:{BRANDING_MARGIN_PX}[vbr]")
        last_v = "[vbr]"
        next_idx += 1
    else:
        logger.warning("No branding logo at %s - skipping logo overlay (still watermark-safe).", logo)

    music = _pick_music_track()
    if music and music.exists():
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        parts.append(f"[{next_idx}:a]volume={MUSIC_VOLUME}[music]")
        parts.append("[0:a][music]amix=inputs=2:duration=first:dropout_transition=2[aout]")
        audio_map = "[aout]"
    else:
        logger.warning("No background music in %s - using native clip audio only.", MUSIC_DIR)
        audio_map = "0:a"

    cmd = ["ffmpeg", "-y", *inputs]
    if parts:
        cmd += ["-filter_complex", ";".join(parts)]
    cmd += [
        "-map", last_v, "-map", audio_map,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-t", str(total_duration), "-movflags", "+faststart", str(out_path),
    ]
    _run(cmd)


def assemble(output_dir: str, run_id: str) -> dict:
    out_dir = Path(output_dir)
    work_dir = out_dir / "_assembly_tmp"
    work_dir.mkdir(parents=True, exist_ok=True)

    script = load_json(out_dir / "script.json")
    visuals_meta = load_json(out_dir / "visuals_meta.json")
    beats = visuals_meta["beats"]

    target = script.get("estimated_duration_sec") or beats[-1]["end_sec"] or TARGET_DURATION_SEC
    requested = _pacing_durations(beats, float(target))

    # Collect the ACTUAL duration of each built segment (a clip may be capped
    # at its real usable length), so the crossfade offsets stay in sync.
    segment_paths, durations = [], []
    for beat, dur in zip(beats, requested):
        seg_path = work_dir / f"seg_{beat['beat_id']}.mp4"
        asset_path = out_dir / beat["path"]
        if beat["type"] == "image":
            actual = _build_still_segment(asset_path, dur, seg_path)
        else:
            actual = _build_clip_segment(asset_path, dur, seg_path)
        segment_paths.append(seg_path)
        durations.append(actual)

    # Append the pre-made subscribe animation (with its own bell sound) as the
    # final segment, crossfaded in - used at the END of every video. If the
    # asset isn't there yet, the video just ends on the final dish shot.
    subscribe_used = False
    subscribe_dur = 0.0
    if SUBSCRIBE_ANIMATION_PATH.exists():
        sub_seg = work_dir / "seg_subscribe.mp4"
        subscribe_dur = _build_subscribe_segment(SUBSCRIBE_ANIMATION_PATH, sub_seg)
        segment_paths.append(sub_seg)
        durations.append(subscribe_dur)
        subscribe_used = True
        logger.info("Subscribe animation appended (%.2fs) from %s", subscribe_dur, SUBSCRIBE_ANIMATION_PATH)
    else:
        logger.warning(
            "No subscribe animation at %s - the video will END on the final dish "
            "shot. Drop your ~%.0fs subscribe animation (with its bell sound) there "
            "to have it appended to every ending.",
            SUBSCRIBE_ANIMATION_PATH, SUBSCRIBE_CTA_DURATION_SEC,
        )

    combined = work_dir / "combined.mp4"
    total_duration = _crossfade(segment_paths, durations, combined)

    final_path = out_dir / "final.mp4"
    _finalize(combined, total_duration, final_path)

    meta = {
        "run_id": run_id,
        "final_path": "final.mp4",
        "duration_sec": total_duration,
        "resolution": f"{VIDEO_WIDTH}x{VIDEO_HEIGHT}",
        "transition_sec": TRANSITION_DURATION_SEC,
        "clip_lag_trim_sec": CLIP_LAG_TRIM_SEC,
        "on_screen_text": "none (subscribe ending is a supplied animation asset)",
        "subscribe_animation_used": subscribe_used,
        "subscribe_animation_sec": subscribe_dur,
        "watermark_safe_zone_px": {"right": WATERMARK_RESERVED_W_PX, "bottom": WATERMARK_RESERVED_H_PX},
        "segment_durations_sec": durations,
        "beats_used": beats,
    }
    save_json(out_dir / "assembly_meta.json", meta)
    logger.info("Final video written to %s (%.2fs)", final_path, total_duration)
    return meta


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Stage 6: assembly (FFmpeg)")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    output_dir = args.output_dir or str(run_output_dir(args.run_id))
    assemble(output_dir, args.run_id)


if __name__ == "__main__":
    main()
