"""
Stage 4 - Captions (local Whisper via faster-whisper).

Fully local/no-API-key stage. Model weights download once (from Hugging
Face) and are cached; no API key required. First run needs network
access to fetch the model unless it's pre-cached in the CI image.

Input: voiceover.mp3 (Stage 3 output), passed via --audio.

Output (<output_dir>/):
    captions.srt
    captions.json:
        {
            "run_id": str,
            "model": str,
            "segments": [{"start_sec": float, "end_sec": float, "text": str}],
            "srt_path": "captions.srt"
        }
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import get_env, run_output_dir
from common.cost_logger import log_cost
from common.io_utils import save_json
from common.retry import retry_with_backoff

logger = logging.getLogger(__name__)

DEFAULT_MODEL_SIZE = "base"


def _format_srt_timestamp(seconds: float) -> str:
    ms_total = round(seconds * 1000)
    hours, rem = divmod(ms_total, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _write_srt(segments: list, srt_path: Path):
    lines = []
    for i, seg in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(
            f"{_format_srt_timestamp(seg['start_sec'])} --> {_format_srt_timestamp(seg['end_sec'])}"
        )
        lines.append(seg["text"].strip())
        lines.append("")
    srt_path.write_text("\n".join(lines), encoding="utf-8")


@retry_with_backoff(max_attempts=2, base_delay=5.0, exceptions=(Exception,))
def _transcribe(audio_path: str, model_size: str):
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments_iter, info = model.transcribe(audio_path, word_timestamps=False)
    segments = [
        {"start_sec": seg.start, "end_sec": seg.end, "text": seg.text}
        for seg in segments_iter
    ]
    return segments, info


def generate_captions(audio_path: str, output_dir: str, run_id: str, model_size: str = None) -> dict:
    model_size = model_size or get_env("WHISPER_MODEL_SIZE", DEFAULT_MODEL_SIZE)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    segments, info = _transcribe(audio_path, model_size)
    srt_path = out_dir / "captions.srt"
    _write_srt(segments, srt_path)

    meta = {
        "run_id": run_id,
        "model": f"faster-whisper-{model_size}",
        "segments": segments,
        "srt_path": "captions.srt",
        "detected_language": getattr(info, "language", None),
    }
    save_json(out_dir / "captions.json", meta)

    log_cost(
        run_id=run_id, stage="stage4_captions", provider="faster-whisper",
        model=model_size, units=1, unit_type="transcription",
        cost_usd=0.0, notes="free/local Whisper",
    )
    logger.info("Captions written to %s (%d segments)", srt_path, len(segments))
    return meta


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Stage 4: captions (Whisper)")
    parser.add_argument("--audio", required=True, help="Path to voiceover.mp3")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--model-size", default=None, help=f"Whisper model size (default: {DEFAULT_MODEL_SIZE})")
    args = parser.parse_args()

    output_dir = args.output_dir or str(run_output_dir(args.run_id))
    generate_captions(args.audio, output_dir, args.run_id, args.model_size)


if __name__ == "__main__":
    main()
