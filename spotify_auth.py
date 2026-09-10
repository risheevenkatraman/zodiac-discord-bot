"""Run locally to authorize playlist access; never runs as part of the bot."""
import base64
import getpass
import json
import os
import secrets
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv

REDIRECT_URI = "http://127.0.0.1:8888/callback"


def main() -> None:
    load_dotenv()
    output = Path("spotify-token.env")
    if output.exists():
        raise SystemExit("spotify-token.env already exists. Move it to a safe location before authorizing again.")
    client_id = os.getenv("SPOTIFY_CLIENT_ID") or input("Spotify client ID: ").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET") or getpass.getpass("Spotify client secret: ")
    state = secrets.token_urlsafe(32)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Callback URLs contain authorization codes.

        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if parsed.path != "/callback" or not secrets.compare_digest(params.get("state", [""])[0], state):
                self.send_error(400, "Invalid callback")
                return
            result.update(code=params.get("code", [""])[0], error=params.get("error", [""])[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Return to the terminal to finish setup. You may close this tab.")

    with HTTPServer(("127.0.0.1", 8888), Callback) as server:
        server.timeout = 1
        print(f"First add this exact redirect URI to your Spotify app settings: {REDIRECT_URI}")
        print("Then open this URL in a browser on this computer:")
        print("https://accounts.spotify.com/authorize?" + urlencode({
            "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
            "scope": "playlist-read-private playlist-read-collaborative", "state": state,
        }))
        deadline = time.monotonic() + 300
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result.get("code") or result.get("error"):
        raise SystemExit("Authorization was declined or timed out. No token was saved.")
    authorization = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    request = Request("https://accounts.spotify.com/api/token", data=urlencode({
        "grant_type": "authorization_code", "code": result["code"], "redirect_uri": REDIRECT_URI,
    }).encode(), headers={"Authorization": f"Basic {authorization}",
                         "Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(request, timeout=30) as response:
        token = json.load(response).get("refresh_token")
    if not isinstance(token, str) or "\n" in token or "\r" in token:
        raise SystemExit("Spotify did not return a valid refresh token.")
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"SPOTIFY_REFRESH_TOKEN={token}\n")
    print("Saved spotify-token.env. Copy its setting into /etc/zodiac-bot.env on the host, then restart the bot.")
    print("This file contains a credential. Keep it private; do not paste it into chat or commit it.")


if __name__ == "__main__":
    main()
