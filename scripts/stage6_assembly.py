"""
Stage 6 - Assembly (FFmpeg).

Fully local stage: no external API, so no retry/backoff here (ffmpeg
failures are deterministic, not transient - retrying won't fix a bad
filter graph). Requires the `ffmpeg` and `ffprobe` binaries on PATH.

Combines the outputs of Stages 3-5 into the final vertical video:
    - pans/zooms ("Ken Burns" effect) on still images
    - inserts the generated video clip(s) at their beat
    - overlays a branding logo (skipped with a warning if none is present)
    - mixes voiceover (or silence, for minimal-narration videos) with
      background music (skipped if no music present)
    - outputs a 1080x1920 MP4

No captions are burned in - per explicit user decision, the burned-in
whisper-generated captions looked clumsy, and YouTube already generates
its own auto-captions for Shorts, so Stage 4 (captions) is no longer
part of the video pipeline (still runnable standalone if ever needed
again).

Input (<output_dir>/):
    voiceover.mp3, voiceover_meta.json  (Stage 3)
    visuals_meta.json + visuals/        (Stage 5)

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
    BRANDING_LOGO_PATH, MUSIC_DIR, VIDEO_FPS, VIDEO_HEIGHT, VIDEO_WIDTH,
    get_env, run_output_dir,
)
from common.io_utils import load_json, save_json

logger = logging.getLogger(__name__)

MUSIC_VOLUME = 0.12


def _run(cmd):
    logger.debug("ffmpeg cmd: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, capture_output=True)


def _rescale_beats(beats: list, actual_duration: float) -> list:
    """Stage 1's visual_beats timing is provisional; stretch/shrink it to
    match the real voiceover duration measured in Stage 3."""
    if not beats:
        return beats
    original_end = beats[-1]["end_sec"]
    if original_end <= 0:
        return beats
    factor = actual_duration / original_end
    rescaled = []
    for beat in beats:
        rescaled.append({
            **beat,
            "start_sec": round(beat["start_sec"] * factor, 3),
            "end_sec": round(beat["end_sec"] * factor, 3),
        })
    rescaled[-1]["end_sec"] = round(actual_duration, 3)
    return rescaled


def _build_image_segment(image_path: Path, duration: float, out_path: Path):
    num_frames = max(1, round(duration * VIDEO_FPS))
    # Faster zoom than a typical Ken Burns pan - per user feedback that the
    # video needed to feel more active, stills should read as more dynamic,
    # not just gently drifting.
    zoompan = (
        f"scale={VIDEO_WIDTH*2}:{VIDEO_HEIGHT*2}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH*2}:{VIDEO_HEIGHT*2},"
        f"zoompan=z='min(zoom+0.0028,1.4)':d={num_frames}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={VIDEO_FPS},"
        f"format=yuv420p"
    )
    _run([
        "ffmpeg", "-y", "-loop", "1", "-i", str(image_path),
        "-vf", zoompan, "-t", str(duration), "-r", str(VIDEO_FPS),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path),
    ])


def _build_video_segment(video_path: Path, duration: float, out_path: Path):
    vf = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},fps={VIDEO_FPS},format=yuv420p"
    )
    _run([
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(video_path),
        "-vf", vf, "-t", str(duration), "-an",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path),
    ])


def _concat_segments(segment_paths: list, out_path: Path, work_dir: Path):
    filelist = work_dir / "concat_list.txt"
    filelist.write_text(
        "\n".join(f"file '{p.resolve()}'" for p in segment_paths), encoding="utf-8"
    )
    _run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(filelist),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path),
    ])


def _overlay_branding(video_path: Path, logo_path: Path, out_path: Path):
    # Placed top-right, clear of Veo's "Made with Veo" watermark, which is
    # small white text fixed in the bottom-right corner (confirmed against
    # a real 720p/8s Veo sample clip - see automation report Section 4.3;
    # Veo's mark is intentionally left visible, not covered).
    if not logo_path.exists():
        logger.warning("No branding logo at %s - skipping overlay.", logo_path)
        video_path.replace(out_path)
        return
    _run([
        "ffmpeg", "-y", "-i", str(video_path), "-i", str(logo_path),
        "-filter_complex", "[1:v]scale=180:-1[logo];[0:v][logo]overlay=W-w-30:30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path),
    ])


def _pick_music_track():
    override = get_env("MUSIC_TRACK")
    if override:
        return MUSIC_DIR / override
    if MUSIC_DIR.exists():
        tracks = sorted(MUSIC_DIR.glob("*.mp3"))
        if tracks:
            return tracks[0]
    return None


def _mix_audio(voiceover_path: Path, duration: float, out_path: Path):
    music_path = _pick_music_track()
    if not music_path or not music_path.exists():
        logger.warning("No background music found in %s - using voiceover only.", MUSIC_DIR)
        _run(["ffmpeg", "-y", "-i", str(voiceover_path), "-t", str(duration), str(out_path)])
        return
    _run([
        "ffmpeg", "-y", "-i", str(voiceover_path),
        "-stream_loop", "-1", "-i", str(music_path),
        "-filter_complex",
        f"[1:a]volume={MUSIC_VOLUME}[music];"
        "[0:a][music]amix=inputs=2:duration=first:dropout_transition=2[aout]",
        "-map", "[aout]", "-t", str(duration), str(out_path),
    ])


def _mux(video_path: Path, audio_path: Path, out_path: Path):
    _run([
        "ffmpeg", "-y", "-i", str(video_path), "-i", str(audio_path),
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
        "-shortest", "-movflags", "+faststart", str(out_path),
    ])


def assemble(output_dir: str, run_id: str) -> dict:
    out_dir = Path(output_dir)
    work_dir = out_dir / "_assembly_tmp"
    work_dir.mkdir(parents=True, exist_ok=True)

    voiceover_meta = load_json(out_dir / "voiceover_meta.json")
    visuals_meta = load_json(out_dir / "visuals_meta.json")
    duration = voiceover_meta["duration_sec"]
    beats = _rescale_beats(visuals_meta["beats"], duration)

    segment_paths = []
    for beat in beats:
        beat_duration = beat["end_sec"] - beat["start_sec"]
        asset_path = out_dir / beat["path"]
        seg_path = work_dir / f"seg_{beat['beat_id']}.mp4"
        if beat["type"] == "image":
            _build_image_segment(asset_path, beat_duration, seg_path)
        else:
            _build_video_segment(asset_path, beat_duration, seg_path)
        segment_paths.append(seg_path)

    silent_video = work_dir / "silent.mp4"
    _concat_segments(segment_paths, silent_video, work_dir)

    branded_video = work_dir / "branded.mp4"
    _overlay_branding(silent_video, BRANDING_LOGO_PATH, branded_video)

    mixed_audio = work_dir / "mixed_audio.mp3"
    _mix_audio(out_dir / "voiceover.mp3", duration, mixed_audio)

    final_path = out_dir / "final.mp4"
    _mux(branded_video, mixed_audio, final_path)

    meta = {
        "run_id": run_id,
        "final_path": "final.mp4",
        "duration_sec": duration,
        "resolution": f"{VIDEO_WIDTH}x{VIDEO_HEIGHT}",
        "beats_used": beats,
    }
    save_json(out_dir / "assembly_meta.json", meta)
    logger.info("Final video written to %s", final_path)
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
