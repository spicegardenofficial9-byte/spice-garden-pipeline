"""
Stage 6 - Assembly (FFmpeg), rebuilt for the 60-second, no-narration format.

Fully local stage: no external API. Requires `ffmpeg` and `ffprobe` on PATH.

Combines the Stage 5 visuals into the final 1080x1920 video with a
professional edit rather than hard jump-cuts:

  1. Each segment is normalised to the same resolution / fps / pixel format
     and colour-corrected with one shared filter (config.COLOR_NORMALIZE_FILTER)
     so illustrated stills and photoreal hero clips don't jar in exposure or
     colour temperature sitting next to each other.
  2. A still gets a Ken-Burns push; a hero clip is scaled/cropped to frame and
     keeps its OWN native audio (sizzle, pour, chop) - there is no voiceover
     anywhere in this format. Stills contribute silence.
  3. Each segment carries a text card (config typography, one fixed lower-third
     placement for every video) that fades in and out - never a hard pop.
  4. Segments are joined with short, fast crossfades (config.TRANSITION_DURATION_SEC),
     video via xfade and audio via acrossfade - clean dissolves, no flashing/
     strobing/"disco" transitions.
  5. Pacing is redistributed to match each moment's energy: quicker holds on
     ingredient/prep shots, a longer hold on the final plated dish, while
     preserving the overall ~60s length.
  6. An animated Subscribe call-to-action (config-driven text from the script's
     subscribe_cta_text) slides up and fades in over the final
     config.SUBSCRIBE_CTA_DURATION_SEC - a drawtext/overlay animation, no
     external tool.
  7. Background music (if present) is mixed low under the native audio.

Watermark policy: Google Veo stamps a "Made with Veo"/SynthID mark in the
BOTTOM-RIGHT corner of every hero clip and it must stay fully visible. So the
channel logo overlays TOP-LEFT (opposite corner), and neither the text cards
(fixed lower-third, horizontally centred and kept narrow of the right edge)
nor the subscribe end-card (centred, upper-middle) ever enter the reserved
bottom-right band (config.WATERMARK_RESERVED_*).

No captions are burned in (Stage 4 is retired - YouTube auto-captions Shorts).

Input (<output_dir>/):
    visuals_meta.json + visuals/   (Stage 5; each beat carries text_card_copy)
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
    COLOR_NORMALIZE_FILTER, DEFAULT_SUBSCRIBE_CTA_TEXT, MUSIC_DIR,
    PACING_HOLD_MULTIPLIER, PACING_QUICK_MULTIPLIER, SUBSCRIBE_CTA_DURATION_SEC,
    SUBSCRIBE_CTA_FONT_SIZE, TARGET_DURATION_SEC, TEXT_CARD_BOX_COLOR,
    TEXT_CARD_FADE_SEC, TEXT_CARD_FONT_COLOR, TEXT_CARD_FONT_PATH,
    TEXT_CARD_FONT_SIZE, TEXT_CARD_Y_RATIO, TRANSITION_DURATION_SEC,
    VIDEO_FPS, VIDEO_HEIGHT, VIDEO_WIDTH, WATERMARK_RESERVED_H_PX,
    WATERMARK_RESERVED_W_PX, get_env, run_output_dir,
)
from common.io_utils import load_json, save_json

logger = logging.getLogger(__name__)

MUSIC_VOLUME = 0.12

# Common per-segment video normalisation: fill the 1080x1920 frame, lock fps
# and SAR, then apply the shared colour correction. Identical for stills and
# clips so the two look like one graded piece.
_COMMON_VF = (
    f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
    f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},fps={VIDEO_FPS},"
    f"{COLOR_NORMALIZE_FILTER},setsar=1,format=yuv420p"
)


def _run(cmd):
    logger.debug("ffmpeg cmd: %s", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, capture_output=True)


def _ffprobe_has_audio(path: Path) -> bool:
    """True if the file has at least one audio stream (real hero clips from
    Veo do; mock placeholder clips and stills don't)."""
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
    preserving `total`. The first still (ingredient/prep) is cut quicker; the
    final segment (the plated dish, which also backs the subscribe card) is
    held longer. Everything else keeps weight 1.0.
    """
    base = [max(0.5, b["end_sec"] - b["start_sec"]) for b in beats]
    if not base:
        return base
    weights = [1.0] * len(base)
    # First segment: quick (prep / ingredient reveal).
    weights[0] = PACING_QUICK_MULTIPLIER
    # An early still (before the midpoint) also reads as prep - quicken it.
    mid = len(base) // 2
    for i, b in enumerate(beats):
        if 0 < i < mid and b.get("type") == "image":
            weights[i] = PACING_QUICK_MULTIPLIER
    # Final segment: longer hold on the finish.
    weights[-1] = PACING_HOLD_MULTIPLIER

    weighted = [b * w for b, w in zip(base, weights)]
    scale = total / sum(weighted)
    return [round(w * scale, 3) for w in weighted]


# --------------------------------------------------------------------------
# Text cards (consistent typography, one placement for every video)
# --------------------------------------------------------------------------

def _wrap_to_width(draw, text: str, font, max_width: int) -> list:
    words = text.split()
    lines, cur = [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _load_font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(TEXT_CARD_FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


def _render_text_card_png(text: str, out_png: Path):
    """Full-frame transparent PNG with the text card drawn at the fixed
    lower-third anchor, horizontally centred, on a semi-opaque plate. Kept
    clear of the reserved bottom-right watermark band."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _load_font(TEXT_CARD_FONT_SIZE)

    # Keep the card narrow of the right watermark column so a lower-third card
    # can never creep under the mark even at max width.
    max_text_width = VIDEO_WIDTH - 2 * 70
    lines = _wrap_to_width(draw, text or "", font, max_text_width)
    line_h = TEXT_CARD_FONT_SIZE + 14
    block_h = line_h * len(lines)

    pad_x, pad_y = 40, 26
    text_w = max((draw.textlength(ln, font=font) for ln in lines), default=0)
    box_w = int(text_w) + 2 * pad_x
    box_h = block_h + 2 * pad_y
    cx = VIDEO_WIDTH // 2
    cy = int(VIDEO_HEIGHT * TEXT_CARD_Y_RATIO)

    box_left = cx - box_w // 2
    box_top = cy - box_h // 2
    draw.rounded_rectangle(
        [box_left, box_top, box_left + box_w, box_top + box_h],
        radius=24, fill=(0, 0, 0, 140),
    )

    y = box_top + pad_y
    for ln in lines:
        w = draw.textlength(ln, font=font)
        draw.text((cx - w / 2, y), ln, font=font, fill=(255, 255, 255, 255))
        y += line_h

    img.save(out_png)


def _render_subscribe_png(text: str, out_png: Path):
    """Full-frame transparent PNG for the animated subscribe end-card:
    the (per-video) CTA line above a fixed 'SUBSCRIBE' pill, centred in the
    upper-middle - clear of the bottom-right Veo watermark."""
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
    # A branded red pill behind the SUBSCRIBE line.
    pill_w = int(sub_w) + 70
    pill_h = 52 + 30
    pill_top = cy - block_h // 2 + 30 + SUBSCRIBE_CTA_FONT_SIZE + 16
    draw.rounded_rectangle(
        [cx - pill_w // 2, pill_top, cx + pill_w // 2, pill_top + pill_h],
        radius=22, fill=(200, 30, 30, 235),
    )
    draw.text((cx - sub_w / 2, pill_top + 12), sub, font=small, fill=(255, 255, 255, 255))

    img.save(out_png)


# --------------------------------------------------------------------------
# Segment builders (each outputs a normalised mp4 with video + audio streams
# of exactly `duration`, text card faded in/out and baked in)
# --------------------------------------------------------------------------

def _card_overlay_filter(duration: float) -> str:
    """Fade the card PNG (input [1:v]) in and out, then overlay on the base
    video ([base]) -> [v]. Fades both text and plate together (no pop)."""
    f = TEXT_CARD_FADE_SEC
    fade_out_st = max(0.0, duration - f)
    return (
        f"[1:v]format=rgba,"
        f"fade=t=in:st=0:d={f}:alpha=1,"
        f"fade=t=out:st={fade_out_st:.3f}:d={f}:alpha=1[card];"
        f"[base][card]overlay=0:0:format=auto[v]"
    )


def _build_still_segment(image_path: Path, duration: float, card_png: Path, out_path: Path):
    num_frames = max(1, round(duration * VIDEO_FPS))
    # Ken-Burns push, then the shared colour grade, then the card overlay.
    kb = (
        f"scale={VIDEO_WIDTH*2}:{VIDEO_HEIGHT*2}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH*2}:{VIDEO_HEIGHT*2},"
        f"zoompan=z='min(zoom+0.0022,1.35)':d={num_frames}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={VIDEO_FPS},"
        f"{COLOR_NORMALIZE_FILTER},setsar=1,format=yuv420p"
    )
    filter_complex = f"[0:v]{kb}[base];{_card_overlay_filter(duration)}"
    _run([
        "ffmpeg", "-y",
        "-loop", "1", "-t", str(duration), "-i", str(image_path),
        "-loop", "1", "-t", str(duration), "-i", str(card_png),
        "-f", "lavfi", "-t", str(duration), "-i", "anullsrc=r=44100:cl=stereo",
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "2:a",
        "-r", str(VIDEO_FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-t", str(duration), str(out_path),
    ])


def _build_clip_segment(video_path: Path, duration: float, card_png: Path, out_path: Path):
    has_audio = _ffprobe_has_audio(video_path)
    filter_complex = f"[0:v]{_COMMON_VF}[base];{_card_overlay_filter(duration)}"
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", str(video_path),
        "-loop", "1", "-t", str(duration), "-i", str(card_png),
    ]
    if has_audio:
        # Keep the clip's native audio, padded with silence if it's shorter
        # than the (pacing-adjusted) segment length, and normalised to a
        # common format so every segment acrossfades cleanly.
        filter_complex += ";[0:a]apad,aformat=sample_rates=44100:channel_layouts=stereo[a]"
        audio_map = "[a]"
    else:
        cmd += ["-f", "lavfi", "-t", str(duration), "-i", "anullsrc=r=44100:cl=stereo"]
        audio_map = "2:a"
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
              out_path: Path, work_dir: Path):
    cta_start = max(0.0, total_duration - SUBSCRIBE_CTA_DURATION_SEC)
    fade = 0.4

    inputs = ["-i", str(combined), "-loop", "1", "-i", str(subscribe_png)]
    parts = []

    # Animated subscribe CTA: fade its alpha in at cta_start and slide it up
    # ~40px into place. enable-gated to the final window only.
    parts.append(
        f"[1:v]format=rgba,fade=t=in:st={cta_start:.3f}:d={fade}:alpha=1[cta]"
    )
    slide = f"40*(1-min(1,(t-{cta_start:.3f})/{fade}))"
    parts.append(
        f"[0:v][cta]overlay=x=(W-w)/2:y='{slide}':"
        f"enable='between(t,{cta_start:.3f},{total_duration:.3f})'[vcta]"
    )
    last_v = "[vcta]"

    logo = BRANDING_LOGO_PATH
    logo_idx = 2
    if logo.exists():
        inputs += ["-i", str(logo)]
        parts.append(f"[{logo_idx}:v]scale={BRANDING_LOGO_WIDTH_PX}:-1[logo]")
        # TOP-LEFT, opposite Veo's bottom-right watermark corner.
        parts.append(
            f"{last_v}[logo]overlay={BRANDING_MARGIN_PX}:{BRANDING_MARGIN_PX}[vbr]"
        )
        last_v = "[vbr]"
        music_idx = 3
    else:
        logger.warning("No branding logo at %s - skipping logo overlay (still watermark-safe).", logo)
        music_idx = 2

    music = _pick_music_track()
    if music and music.exists():
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        parts.append(f"[{music_idx}:a]volume={MUSIC_VOLUME}[music]")
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

    # Authoritative length is the script's ~60s target; pacing redistributes
    # within it (see _pacing_durations).
    target = script.get("estimated_duration_sec") or beats[-1]["end_sec"] or TARGET_DURATION_SEC
    durations = _pacing_durations(beats, float(target))

    segment_paths = []
    for beat, dur in zip(beats, durations):
        card_png = work_dir / f"card_{beat['beat_id']}.png"
        _render_text_card_png(beat.get("text_card_copy", ""), card_png)

        seg_path = work_dir / f"seg_{beat['beat_id']}.mp4"
        asset_path = out_dir / beat["path"]
        if beat["type"] == "image":
            _build_still_segment(asset_path, dur, card_png, seg_path)
        else:
            _build_clip_segment(asset_path, dur, card_png, seg_path)
        segment_paths.append(seg_path)

    combined = work_dir / "combined.mp4"
    total_duration = _crossfade(segment_paths, durations, combined)

    subscribe_png = work_dir / "subscribe.png"
    _render_subscribe_png(script.get("subscribe_cta_text", DEFAULT_SUBSCRIBE_CTA_TEXT), subscribe_png)

    final_path = out_dir / "final.mp4"
    _finalize(combined, total_duration, subscribe_png, final_path, work_dir)

    meta = {
        "run_id": run_id,
        "final_path": "final.mp4",
        "duration_sec": total_duration,
        "resolution": f"{VIDEO_WIDTH}x{VIDEO_HEIGHT}",
        "transition_sec": TRANSITION_DURATION_SEC,
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
