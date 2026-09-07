"""
Stage 7 - Upload (YouTube Data API v3).

Requires OAuth credentials for a channel that has already completed the
one-time OAuth consent flow (see SETUP.md for how to mint a refresh
token). This stage refreshes the access token itself on every run, so
no browser interaction is needed once the refresh token exists.

TODO before the first real run: double-check the field name YouTube
currently uses for "altered/synthetic content" self-disclosure in
`videos.insert` against the live API docs - it's a newer part of the
API and the field name/location can change. Adjust the `status` dict in
upload_video() if it has moved.

Input (<output_dir>/):
    final.mp4    (Stage 6 output)
    script.json  (Stage 1 output, for title/description/hashtags)

Output (<output_dir>/upload_result.json):
    {
        "run_id": str,
        "youtube_video_id": str,
        "url": str,
        "uploaded_at": iso8601 str,
        "privacy_status": str,
        "self_declared_ai_content": true
    }
"""
import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import get_env, require_env, run_output_dir
from common.cost_logger import log_cost
from common.io_utils import load_json, save_json
from common.retry import retry_with_backoff

logger = logging.getLogger(__name__)

CLIENT_ID_ENV = "YOUTUBE_CLIENT_ID"
CLIENT_SECRET_ENV = "YOUTUBE_CLIENT_SECRET"
REFRESH_TOKEN_ENV = "YOUTUBE_REFRESH_TOKEN"


def _build_youtube_client():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=require_env(REFRESH_TOKEN_ENV),
        client_id=require_env(CLIENT_ID_ENV),
        client_secret=require_env(CLIENT_SECRET_ENV),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


@retry_with_backoff(max_attempts=4, base_delay=5.0, exceptions=(Exception,))
def _upload_video(youtube, video_path: str, body: dict) -> dict:
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.info("Upload progress: %d%%", int(status.progress() * 100))
    return response


def upload_video(output_dir: str, run_id: str, privacy_status: str = None) -> dict:
    out_dir = Path(output_dir)
    script = load_json(out_dir / "script.json")
    video_path = out_dir / "final.mp4"

    # Default unlisted, per the current review-first plan - switch to
    # "public" via YOUTUBE_PRIVACY_STATUS once satisfied with live runs.
    privacy_status = privacy_status or get_env("YOUTUBE_PRIVACY_STATUS", "unlisted")

    dish_name = script.get("dish_name", "")
    region = script.get("region", "")
    ingredients_line = ", ".join(script.get("ingredients", []))
    # No voiceover in this format - build the description from the dish's
    # region + interesting fact and the ingredient list instead.
    intro = " ".join(p for p in (
        f"{dish_name} ({region})." if dish_name or region else "",
        script.get("dish_fact", ""),
    ) if p)
    description_parts = [intro]
    if ingredients_line:
        description_parts.append(f"Ingredients: {ingredients_line}")
    description_parts.append(" ".join(script.get("hashtags", [])))

    required_tags = [t for t in (dish_name, region, "Indian cooking", "shorts") if t]
    hashtag_tags = [h.lstrip("#") for h in script.get("hashtags", [])]
    tags = list(dict.fromkeys(required_tags + hashtag_tags))  # dedupe, preserve order

    body = {
        "snippet": {
            "title": script.get("title", dish_name or "Spice Garden Shorts"),
            "description": "\n\n".join(p for p in description_parts if p),
            "tags": tags,
            "categoryId": "26",  # Howto & Style
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
            # TODO: verify this is still the correct field/value for AI
            # content disclosure - see the TODO at the top of this file.
            "containsSyntheticMedia": True,
        },
    }

    youtube = _build_youtube_client()
    response = _upload_video(youtube, str(video_path), body)

    video_id = response["id"]
    result = {
        "run_id": run_id,
        "youtube_video_id": video_id,
        "url": f"https://youtube.com/shorts/{video_id}",
        "uploaded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "privacy_status": privacy_status,
        "self_declared_ai_content": True,
    }
    save_json(out_dir / "upload_result.json", result)

    log_cost(
        run_id=run_id, stage="stage7_upload", provider="youtube-data-api",
        model="", units=1, unit_type="upload", cost_usd=0.0, notes=video_id,
    )
    logger.info("Uploaded video: %s", result["url"])
    return result


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Stage 7: upload to YouTube")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--privacy-status", default=None, choices=["public", "unlisted", "private"])
    args = parser.parse_args()

    output_dir = args.output_dir or str(run_output_dir(args.run_id))
    upload_video(output_dir, args.run_id, args.privacy_status)


if __name__ == "__main__":
    main()
