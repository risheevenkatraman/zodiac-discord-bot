"""Shared yt-dlp configuration for metadata lookup and audio playback."""
from pathlib import Path
from typing import Any

import yt_dlp


class MediaError(RuntimeError):
    """A playback failure with a message safe to show in Discord."""


def extraction_options(settings: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 2,
        "cachedir": False,
        "js_runtimes": {"deno": {}, "node": {}},
    }
    if settings.ytdlp_js_runtime:
        runtime, separator, path = settings.ytdlp_js_runtime.partition(":")
        if runtime not in {"deno", "node", "quickjs"}:
            raise MediaError("YTDLP_JS_RUNTIME must specify deno, node, or quickjs, optionally followed by :path.")
        options["js_runtimes"] = {runtime: {"path": path} if separator else {}}
    if settings.ytdlp_cookies_file:
        cookie_path = Path(settings.ytdlp_cookies_file).expanduser()
        if not cookie_path.is_file():
            raise MediaError("The configured YouTube cookie file is missing on the bot host.")
        options["cookiefile"] = str(cookie_path)
    return options


def extract_info(source: str, options: dict[str, Any]) -> dict[str, Any]:
    try:
        with yt_dlp.YoutubeDL(options) as extractor:
            result = extractor.extract_info(source, download=False)
    except yt_dlp.utils.DownloadError as error:
        detail = str(error).casefold()
        if "not a bot" in detail or "sign in to confirm" in detail:
            message = (
                "YouTube requires verification from the bot host. Configure a valid "
                "YTDLP_COOKIES_FILE and a supported JavaScript runtime; see the README. "
                "Spotify matches also use YouTube audio."
            )
        elif "private" in detail or "unavailable" in detail or "removed" in detail:
            message = "This video is private, unavailable, or removed. Try another link."
        elif "403" in detail or "po token" in detail:
            message = "YouTube denied audio access. Update yt-dlp and check its runtime, cookies, and PO-token requirements on the host."
        else:
            message = "YouTube extraction failed. Check the host's yt-dlp/runtime setup and the bot logs."
        raise MediaError(message) from error
    except OSError as error:
        raise MediaError("The media extractor could not access a required file. Check cookie-file permissions on the host.") from error
    if not isinstance(result, dict):
        raise MediaError("No media information was returned for that link.")
    return result
