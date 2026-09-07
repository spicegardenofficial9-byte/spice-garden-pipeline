"""
Fetches the recurring host character's reference image from a private
content source at runtime.

Per the automation report (Section 4.2): the character reference image
must live in a private content repository/store, not in this public
orchestration repo, so it's never committed here - this script pulls it
in fresh on every run into a gitignored local path
(see common.config.CHARACTER_REF_IMAGE_PATH, under _private_assets/).

Env vars:
    CHARACTER_REF_URL          HTTPS URL to the reference image. If
                                unset, this script logs a warning and
                                does nothing - mock-mode visual
                                generation (Stage 5 without
                                GEMINI_API_KEY) doesn't need a
                                reference image. Real Veo 3.1 Lite /
                                Nano Banana calls do; Stage 5 raises
                                a clear error if GEMINI_API_KEY is set
                                but no reference image was fetched.
    CHARACTER_REF_AUTH_HEADER  optional - full Authorization header
                                value for the request, e.g.
                                "token ghp_xxx" for private GitHub raw
                                content. Not needed for a pre-signed
                                private-storage URL.
"""
import logging
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import CHARACTER_REF_IMAGE_PATH, get_env
from common.retry import retry_with_backoff

logger = logging.getLogger(__name__)


@retry_with_backoff(max_attempts=4, base_delay=2.0, exceptions=(Exception,))
def _download(url: str, auth_header: str, out_path: Path):
    req = urllib.request.Request(url)
    if auth_header:
        req.add_header("Authorization", auth_header)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)


def fetch_character_reference():
    url = get_env("CHARACTER_REF_URL")
    if not url:
        logger.warning(
            "CHARACTER_REF_URL not set - skipping character reference fetch. "
            "Required before real (non-mock) Nano Banana generation; see SETUP.md."
        )
        return None
    auth_header = get_env("CHARACTER_REF_AUTH_HEADER")
    _download(url, auth_header, CHARACTER_REF_IMAGE_PATH)
    logger.info("Character reference fetched to %s", CHARACTER_REF_IMAGE_PATH)
    return CHARACTER_REF_IMAGE_PATH


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    fetch_character_reference()


if __name__ == "__main__":
    main()
