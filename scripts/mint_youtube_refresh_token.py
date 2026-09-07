"""
One-time, LOCAL-ONLY script to mint a YouTube OAuth refresh token.

Run this once on your own machine, never in CI. It opens your browser
for you to sign in as the channel's Google account and grant upload
permission, then prints a refresh token to save as YOUTUBE_REFRESH_TOKEN
(in .env locally, and as a GitHub Actions Secret once you push).

Needs YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET already set (in .env
or the environment) - get these from Google Cloud Console first, see
SETUP.md's "Minting a YouTube refresh token" section.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.config import require_env

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main():
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_id = require_env("YOUTUBE_CLIENT_ID")
    client_secret = require_env("YOUTUBE_CLIENT_SECRET")

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    print("Opening your browser - sign in as the channel's Google account and grant access...")
    creds = flow.run_local_server(port=0)

    print("\n=== Success ===")
    print("Save this value as YOUTUBE_REFRESH_TOKEN (in .env now, and as a")
    print("GitHub Actions Secret once you push to GitHub):\n")
    print(creds.refresh_token)
    print()


if __name__ == "__main__":
    main()
