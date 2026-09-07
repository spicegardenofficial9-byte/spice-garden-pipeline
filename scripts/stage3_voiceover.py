"""
Stage 3 - Voiceover (Edge TTS).

Fully local/no-API-key stage. edge-tts streams audio from Microsoft's
public "Read Aloud" service over a websocket - no account or API key
required, but it does need outbound network access (GitHub Actions
runners have this by default).

Input: script.json (Stage 1 output), or any JSON with a
       "voiceover_script" string field.

Output (<output_dir>/):
    voiceover.mp3
    voiceover_meta.json:
        {
            "run_id": str,
            "audio_path": "voiceover.mp3",
            "voice": str,
            "duration_sec": float,
            "word_boundaries": [{"text": str, "start_sec": float, "end_sec": float}]
        }
"""
import argparse
import asyncio
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import MIN_DURATION_SEC, get_env, run_output_dir
from common.cost_logger import log_cost
from common.io_utils import load_json, save_json
from common.retry import retry_with_backoff

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "en-IN-NeerjaNeural"


def _generate_silence(duration_sec: float, out_mp3: Path):
    """For minimal-narration scripts with an empty voiceover_script (an
    intentional, encouraged choice - see stage1's docstring): produce a
    silent track of the script's target duration instead of calling TTS,
    so downstream stages (captions skip naturally, assembly) still have
    an audio track and a duration to build around.
    """
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
            "-t", str(max(duration_sec, 1)), "-q:a", "9", str(out_mp3),
        ],
        check=True, capture_output=True,
    )


@retry_with_backoff(max_attempts=4, base_delay=2.0, exceptions=(Exception,))
def _synthesize(text: str, voice: str, out_mp3: Path):
    import edge_tts

    async def run():
        communicate = edge_tts.Communicate(text, voice)
        submaker = edge_tts.SubMaker()
        with open(out_mp3, "wb") as audio_file:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_file.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    submaker.feed(chunk)
        return submaker

    return asyncio.run(run())


def _extract_word_boundaries(submaker) -> list:
    words = []
    for cue in getattr(submaker, "cues", []):
        words.append({
            "text": cue.content,
            "start_sec": cue.start.total_seconds(),
            "end_sec": cue.end.total_seconds(),
        })
    return words


def _probe_duration_sec(mp3_path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(mp3_path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def generate_voiceover(script_path: str, output_dir: str, run_id: str, voice: str = None) -> dict:
    script = load_json(script_path)
    text = (script.get("voiceover_script") or "").strip()

    voice = voice or get_env("TTS_VOICE", DEFAULT_VOICE)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    audio_path = out_dir / "voiceover.mp3"

    if not text:
        duration_sec = float(script.get("estimated_duration_sec") or MIN_DURATION_SEC)
        _generate_silence(duration_sec, audio_path)
        word_boundaries = []
        logger.info("No voiceover_script - minimal-narration video, generated %.1fs of silence instead.", duration_sec)
    else:
        submaker = _synthesize(text, voice, audio_path)
        duration_sec = _probe_duration_sec(audio_path)
        word_boundaries = _extract_word_boundaries(submaker)

    meta = {
        "run_id": run_id,
        "audio_path": "voiceover.mp3",
        "voice": voice,
        "duration_sec": duration_sec,
        "word_boundaries": word_boundaries,
    }
    save_json(out_dir / "voiceover_meta.json", meta)

    log_cost(
        run_id=run_id, stage="stage3_voiceover", provider="edge-tts" if text else "none",
        model=voice if text else "silence", units=len(text), unit_type="characters",
        cost_usd=0.0, notes="free/local TTS" if text else "minimal-narration video - no TTS call made",
    )
    logger.info("Voiceover written to %s (%.1fs)", audio_path, duration_sec)
    return meta


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Stage 3: voiceover (Edge TTS)")
    parser.add_argument("--script", required=True, help="Path to script.json")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--voice", default=None, help=f"Edge TTS voice name (default: {DEFAULT_VOICE})")
    args = parser.parse_args()

    output_dir = args.output_dir or str(run_output_dir(args.run_id))
    generate_voiceover(args.script, output_dir, args.run_id, args.voice)


if __name__ == "__main__":
    main()
