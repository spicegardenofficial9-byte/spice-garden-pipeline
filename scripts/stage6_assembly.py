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
  6. The ONLY on-screen text is an animated Subscribe call-to-action (from the
     script's subscribe_cta_text) shown over the final
     config.SUBSCRIBE_CTA_DURATION_SEC (~4s): the card slides up and fades in
     with a gold notification bell above it that fades in and "rings" (a
     rotate oscillation) - an overlay animation, no external tool.
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
    CLIP_LAG_TRIM_SEC, COLOR_NORMALIZE_FILTER, DEFAULT_SUBSCRIBE_CTA_TEXT,
    MUSIC_DIR, PACING_HOLD_MULTIPLIER, PACING_QUICK_MULTIPLIER,
    SUBSCRIBE_CTA_DURATION_SEC, SUBSCRIBE_CTA_FONT_SIZE, TARGET_DURATION_SEC,
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
# Subscribe end-card (the only on-screen text)
# --------------------------------------------------------------------------

def _load_font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _render_subscribe_png(text: str, out_png: Path):
    """Full-frame transparent PNG for the animated subscribe end-card: the
    (per-video) CTA line above a fixed 'SUBSCRIBE' pill, centred in the
    upper-middle - clear of the bottom-right watermark band."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    big = _load_font(SUBSCRIBE_CTA_FONT_SIZE)
    small = _load_font(52)

    cta = (text or DEFAULT_SUBSCRIBE_CTA_TEXT).strip()
    sub = "▶  SUBSCRIBE"

    cx = VIDEO_WIDTH // 2
    cy = int(VIDEO_HEIGHT * 0.42)

    cta_w = draw.textlength(cta, font=big)
    sub_w = draw.textlength(sub, font=small)
    block_w = int(max(cta_w, sub_w)) + 120
    block_h = SUBSCRIBE_CTA_FONT_SIZE + 52 + 70
    draw.rounded_rectangle(
        [cx - block_w // 2, cy - block_h // 2, cx + block_w // 2, cy + block_h // 2],
        radius=32, fill=(0, 0, 0, 150),
    )

    draw.text((cx - cta_w / 2, cy - block_h // 2 + 30), cta, font=big, fill=(255, 255, 255, 255))
    pill_w = int(sub_w) + 70
    pill_h = 52 + 30
    pill_top = cy - block_h // 2 + 30 + SUBSCRIBE_CTA_FONT_SIZE + 16
    draw.rounded_rectangle(
        [cx - pill_w // 2, pill_top, cx + pill_w // 2, pill_top + pill_h],
        radius=22, fill=(200, 30, 30, 235),
    )
    draw.text((cx - sub_w / 2, pill_top + 12), sub, font=small, fill=(255, 255, 255, 255))

    img.save(out_png)


BELL_CANVAS = 280  # small square so rotate() wiggles the bell around its own centre


def _render_bell_png(out_png: Path):
    """A small gold notification-bell icon centred on a transparent square
    canvas, drawn with primitives (no emoji font needed). It's centred so the
    rotate filter can wiggle it in place for a 'ringing' animation."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (BELL_CANVAS, BELL_CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = BELL_CANVAS // 2
    gold = (255, 201, 64, 255)
    edge = (120, 78, 0, 255)

    # top handle knob
    draw.ellipse([cx - 14, 44, cx + 14, 72], fill=gold, outline=edge, width=3)
    # bell body: rounded dome shoulders tapering to a wide base
    body = [
        (cx - 78, 190), (cx - 66, 150), (cx - 52, 108),
        (cx - 40, 82), (cx + 40, 82), (cx + 52, 108),
        (cx + 66, 150), (cx + 78, 190),
    ]
    draw.polygon(body, fill=gold, outline=edge)
    draw.ellipse([cx - 40, 66, cx + 40, 100], fill=gold, outline=edge, width=3)
    # base rim
    draw.rounded_rectangle([cx - 92, 186, cx + 92, 212], radius=13, fill=gold, outline=edge, width=3)
    # clapper
    draw.ellipse([cx - 15, 216, cx + 15, 246], fill=gold, outline=edge, width=3)

    img.save(out_png)


# --------------------------------------------------------------------------
# Segment builders (each outputs a normalised mp4 with video + audio streams
# of exactly `duration` - no on-screen text)
# --------------------------------------------------------------------------

def _build_still_segment(image_path: Path, duration: float, out_path: Path):
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


def _build_clip_segment(video_path: Path, duration: float, out_path: Path):
    """Hero clip: loop-safe head-lag trim, scale/colour/sharpen, native audio
    (or silence). The clip is looped so a pacing-adjusted duration slightly
    longer than the trimmed source repeats the motion rather than freezing."""
    has_audio = _ffprobe_has_audio(video_path)
    lag = CLIP_LAG_TRIM_SEC
    vf = f"[0:v]trim=start={lag},setpts=PTS-STARTPTS,{_CLIP_VF}[v]"
    cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(video_path)]
    if has_audio:
        filter_complex = (
            f"{vf};"
            f"[0:a]atrim=start={lag},asetpts=PTS-STARTPTS,apad,"
            f"aformat=sample_rates=44100:channel_layouts=stereo[a]"
        )
        audio_map = "[a]"
    else:
        cmd += ["-f", "lavfi", "-t", str(duration), "-i", "anullsrc=r=44100:cl=stereo"]
        filter_complex = vf
        audio_map = "1:a"
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", audio_map,
        "-r", str(VIDEO_FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-t", str(duration), str(out_path),
    ]
    _run(cmd)


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

def _finalize(combined: Path, total_duration: float, subscribe_png: Path,
              bell_png: Path, out_path: Path):
    cta_start = max(0.0, total_duration - SUBSCRIBE_CTA_DURATION_SEC)
    fade = 0.4

    # Input 0 = combined video; 1 = subscribe card PNG; 2 = bell PNG; then
    # optional logo, then optional music - indices tracked as we append.
    inputs = ["-i", str(combined),
              "-loop", "1", "-i", str(subscribe_png),
              "-loop", "1", "-i", str(bell_png)]
    parts = []

    # Subscribe card: fade + slight upward slide into place.
    parts.append(f"[1:v]format=rgba,fade=t=in:st={cta_start:.3f}:d={fade}:alpha=1[cta]")
    slide = f"40*(1-min(1,(t-{cta_start:.3f})/{fade}))"
    parts.append(
        f"[0:v][cta]overlay=x=(W-w)/2:y='{slide}':"
        f"enable='between(t,{cta_start:.3f},{total_duration:.3f})'[vcta]"
    )

    # Bell: fade in, then a continuous ringing wiggle (rotate oscillation
    # around its own centre), overlaid just above the subscribe card.
    bell_y = int(VIDEO_HEIGHT * 0.205)
    parts.append(
        f"[2:v]format=rgba,rotate=a='0.28*sin(2*PI*3*t)':c=none:ow=rotw(0):oh=roth(0),"
        f"fade=t=in:st={cta_start:.3f}:d={fade}:alpha=1[bell]"
    )
    parts.append(
        f"[vcta][bell]overlay=x=(W-w)/2:y={bell_y}:"
        f"enable='between(t,{cta_start:.3f},{total_duration:.3f})'[vbell]"
    )
    last_v = "[vbell]"
    next_idx = 3

    logo = BRANDING_LOGO_PATH
    if logo.exists():
        inputs += ["-i", str(logo)]
        parts.append(f"[{next_idx}:v]scale={BRANDING_LOGO_WIDTH_PX}:-1[logo]")
        parts.append(f"{last_v}[logo]overlay={BRANDING_MARGIN_PX}:{BRANDING_MARGIN_PX}[vbr]")
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

    filter_complex = ";".join(parts)
    _run([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", last_v, "-map", audio_map,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-t", str(total_duration), "-movflags", "+faststart", str(out_path),
    ])


def assemble(output_dir: str, run_id: str) -> dict:
    out_dir = Path(output_dir)
    work_dir = out_dir / "_assembly_tmp"
    work_dir.mkdir(parents=True, exist_ok=True)

    script = load_json(out_dir / "script.json")
    visuals_meta = load_json(out_dir / "visuals_meta.json")
    beats = visuals_meta["beats"]

    target = script.get("estimated_duration_sec") or beats[-1]["end_sec"] or TARGET_DURATION_SEC
    durations = _pacing_durations(beats, float(target))

    segment_paths = []
    for beat, dur in zip(beats, durations):
        seg_path = work_dir / f"seg_{beat['beat_id']}.mp4"
        asset_path = out_dir / beat["path"]
        if beat["type"] == "image":
            _build_still_segment(asset_path, dur, seg_path)
        else:
            _build_clip_segment(asset_path, dur, seg_path)
        segment_paths.append(seg_path)

    combined = work_dir / "combined.mp4"
    total_duration = _crossfade(segment_paths, durations, combined)

    subscribe_png = work_dir / "subscribe.png"
    _render_subscribe_png(script.get("subscribe_cta_text", DEFAULT_SUBSCRIBE_CTA_TEXT), subscribe_png)
    bell_png = work_dir / "bell.png"
    _render_bell_png(bell_png)

    final_path = out_dir / "final.mp4"
    _finalize(combined, total_duration, subscribe_png, bell_png, final_path)

    meta = {
        "run_id": run_id,
        "final_path": "final.mp4",
        "duration_sec": total_duration,
        "resolution": f"{VIDEO_WIDTH}x{VIDEO_HEIGHT}",
        "transition_sec": TRANSITION_DURATION_SEC,
        "clip_lag_trim_sec": CLIP_LAG_TRIM_SEC,
        "on_screen_text": "subscribe end-card only",
        "subscribe_cta_text": script.get("subscribe_cta_text", DEFAULT_SUBSCRIBE_CTA_TEXT),
        "subscribe_cta_window_sec": [round(total_duration - SUBSCRIBE_CTA_DURATION_SEC, 3), round(total_duration, 3)],
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
