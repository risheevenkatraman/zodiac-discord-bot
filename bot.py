from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import Database
from media import MediaError, extract_info, extraction_options

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("zodiac-bot")
PERMISSION_NAMES = tuple(sorted(name for name, _ in discord.Permissions.none()))
MAX_QUEUE_SIZE = 200


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    discord_token: str
    webhook_secret: str
    webhook_host: str
    webhook_port: int
    twitch_client_id: str | None
    twitch_client_secret: str | None
    twitch_poll_seconds: int
    spotify_client_id: str | None
    spotify_client_secret: str | None
    ffmpeg_path: str
    ytdlp_cookies_file: str | None = None
    ytdlp_js_runtime: str | None = None
    spotify_refresh_token: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        twitch_id = os.getenv("TWITCH_CLIENT_ID")
        twitch_secret = os.getenv("TWITCH_CLIENT_SECRET")
        if bool(twitch_id) != bool(twitch_secret):
            raise RuntimeError(
                "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET must be configured together"
            )
        if bool(os.getenv("SPOTIFY_CLIENT_ID")) != bool(os.getenv("SPOTIFY_CLIENT_SECRET")):
            raise RuntimeError("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be configured together")
        return cls(
            discord_token=required_env("DISCORD_TOKEN"),
            webhook_secret=required_env("WEBHOOK_SECRET"),
            webhook_host=os.getenv("WEBHOOK_HOST", "127.0.0.1"),
            webhook_port=int(os.getenv("WEBHOOK_PORT", "8080")),
            twitch_client_id=twitch_id,
            twitch_client_secret=twitch_secret,
            twitch_poll_seconds=max(30, int(os.getenv("TWITCH_POLL_SECONDS", "60"))),
            spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID"),
            spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
            ffmpeg_path=os.getenv("FFMPEG_PATH", "ffmpeg"),
            ytdlp_cookies_file=os.getenv("YTDLP_COOKIES_FILE") or None,
            ytdlp_js_runtime=os.getenv("YTDLP_JS_RUNTIME") or None,
            spotify_refresh_token=os.getenv("SPOTIFY_REFRESH_TOKEN") or None,
        )


@dataclass(frozen=True)
class MusicTrack:
    source: str
    title: str
    notification_channel_id: int | None = None


class GuildMusicPlayer:
    def __init__(self, bot: ZodiacBot, guild_id: int) -> None:
        self.bot = bot
        self.guild_id = guild_id
        self.queue: asyncio.Queue[MusicTrack] = asyncio.Queue()
        self.voice_client: discord.VoiceClient | None = None
        self.current: MusicTrack | None = None
        self.connection_lock = asyncio.Lock()
        self.player_task = asyncio.create_task(self._run())

    async def enqueue(self, tracks: list[MusicTrack]) -> None:
        if self.queue.qsize() + len(tracks) > MAX_QUEUE_SIZE:
            raise ValueError(f"The queue can hold at most {MAX_QUEUE_SIZE} upcoming tracks.")
        for track in tracks:
            self.queue.put_nowait(track)

    async def _run(self) -> None:
        while True:
            track = await self.queue.get()
            self.current = track
            try:
                await self._play(track)
            except (discord.ClientException, discord.HTTPException, OSError, RuntimeError) as error:
                LOGGER.exception("Failed to play track in guild %s", self.guild_id)
                channel = self.bot.get_channel(track.notification_channel_id) if track.notification_channel_id else None
                if channel is not None:
                    detail = str(error) if isinstance(error, MediaError) else "Check FFmpeg, voice permissions, and the bot logs."
                    with suppress(discord.HTTPException):
                        await channel.send(
                            f"Could not play **{discord.utils.escape_markdown(track.title[:100])}**. {detail}",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
            finally:
                self.current = None
                self.queue.task_done()

    async def _play(self, track: MusicTrack) -> None:
        if not self.voice_client or not self.voice_client.is_connected():
            return
        info = await self.bot.extract_audio(track.source)
        stream_url = info.get("url")
        if not isinstance(stream_url, str):
            raise RuntimeError(f"No playable audio was found for {track.title}")
        finished = asyncio.get_running_loop().create_future()

        def finish(error: Exception | None) -> None:
            if finished.done():
                return
            if error:
                finished.set_exception(error)
            else:
                finished.set_result(None)

        def after(error: Exception | None) -> None:
            self.bot.loop.call_soon_threadsafe(finish, error)

        source = discord.FFmpegPCMAudio(
            stream_url,
            executable=self.bot.settings.ffmpeg_path,
            before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            options="-vn",
        )
        try:
            self.voice_client.play(source, after=after)
        except Exception:
            source.cleanup()
            raise
        await finished

    async def stop(self) -> None:
        self.player_task.cancel()
        with suppress(asyncio.CancelledError):
            await self.player_task
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        if self.voice_client:
            self.voice_client.stop()
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()


class ZodiacBot(commands.Bot):
    def __init__(self, settings: Settings, database: Database) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents,
                         allowed_mentions=discord.AllowedMentions.none())
        self.settings = settings
        self.database = database
        self.http_session: aiohttp.ClientSession | None = None
        self.twitch_token: str | None = None
        self.webhook_runner: web.AppRunner | None = None
        self.music_players: dict[int, GuildMusicPlayer] = {}
        self.spotify_token: str | None = None
        self.spotify_token_expires_at = 0.0
        self.spotify_refresh_token = settings.spotify_refresh_token
        self.media_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        await self.tree.sync()
        self.twitch_poll_loop.change_interval(seconds=self.settings.twitch_poll_seconds)
        self.twitch_poll_loop.start()

    async def close(self) -> None:
        self.twitch_poll_loop.cancel()
        if self.webhook_runner:
            await self.webhook_runner.cleanup()
        if self.http_session:
            await self.http_session.close()
        for player in list(self.music_players.values()):
            await player.stop()
        self.music_players.clear()
        await super().close()
        await self.database.close()

    def music_player(self, guild_id: int) -> GuildMusicPlayer:
        player = self.music_players.get(guild_id)
        if player is None:
            player = GuildMusicPlayer(self, guild_id)
            self.music_players[guild_id] = player
        return player

    async def extract_audio(self, source: str) -> dict[str, Any]:
        options = {
            **extraction_options(self.settings),
            "format": "bestaudio/best",
            "noplaylist": True,
        }
        async with self.media_lock:
            result = await asyncio.to_thread(extract_info, source, options)
        if result.get("entries"):
            first = next((entry for entry in result["entries"] if isinstance(entry, dict)), None)
            if first is None:
                raise RuntimeError("The media extractor returned no playable track")
            result = first
        return result

    async def resolve_media(self, url: str) -> list[MusicTrack]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Please provide a valid YouTube or Spotify URL.")
        hostname = (parsed.hostname or "").lower()
        if hostname == "open.spotify.com":
            return await self.resolve_spotify(url)
        if hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}:
            raise ValueError("Please provide a valid YouTube or Spotify URL.")
        options = {
            **extraction_options(self.settings),
            "extract_flat": True,
            "skip_download": True,
            "playlistend": MAX_QUEUE_SIZE + 1,
        }
        async with self.media_lock:
            result = await asyncio.to_thread(extract_info, url, options)
        if not isinstance(result, dict):
            raise RuntimeError("No media was found at that URL.")
        entries = result.get("entries")
        if entries:
            tracks = [
                MusicTrack(
                    source=str(entry.get("webpage_url") or entry.get("url")),
                    title=str(entry.get("title") or "Untitled track"),
                )
                for entry in entries
                if isinstance(entry, dict) and (entry.get("webpage_url") or entry.get("url"))
            ]
            if tracks:
                return tracks
        webpage_url = result.get("webpage_url") or url
        return [MusicTrack(source=str(webpage_url), title=str(result.get("title") or url))]

    async def spotify_access_token(self) -> str:
        if self.spotify_token and time.monotonic() < self.spotify_token_expires_at:
            return self.spotify_token
        if not self.http_session or not self.settings.spotify_client_id or not self.settings.spotify_client_secret:
            raise RuntimeError("Spotify links require SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.")
        credentials = base64.b64encode(
            f"{self.settings.spotify_client_id}:{self.settings.spotify_client_secret}".encode()
        ).decode()
        async with self.http_session.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {credentials}"},
            data=({"grant_type": "refresh_token", "refresh_token": self.spotify_refresh_token}
                  if self.spotify_refresh_token else {"grant_type": "client_credentials"}),
        ) as response:
            if response.status in {400, 401}:
                raise MediaError("Spotify rejected the credentials. Check the client ID, secret, and any configured refresh token on the host.")
            response.raise_for_status()
            payload = await response.json()
        token = payload.get("access_token")
        if not isinstance(token, str):
            raise RuntimeError("Spotify did not return an access token.")
        self.spotify_token = token
        if isinstance(payload.get("refresh_token"), str):
            self.spotify_refresh_token = payload["refresh_token"]
        self.spotify_token_expires_at = time.monotonic() + max(0, float(payload.get("expires_in", 3600)) - 60)
        return token

    async def spotify_get(self, endpoint: str) -> dict[str, Any]:
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or parsed.netloc != "api.spotify.com":
            raise MediaError("Spotify returned an invalid pagination URL.")
        for attempt in range(2):
            token = await self.spotify_access_token()
            assert self.http_session is not None
            async with self.http_session.get(endpoint, headers={"Authorization": f"Bearer {token}"}) as response:
                if response.status == 401:
                    self.spotify_token = None
                    if attempt == 0:
                        continue
                    raise MediaError("Spotify authorization failed after refreshing. Check the app credentials.")
                if response.status == 403:
                    raise MediaError("Spotify denied access. Check the app's Development Mode/Premium requirements and the authorized user's playlist access.")
                if response.status == 404:
                    raise MediaError("Spotify could not access that track or playlist. Check the link and the authorized user's access.")
                if response.status == 429:
                    raise MediaError("Spotify is rate limiting requests. Wait before trying again.")
                response.raise_for_status()
                return await response.json()
        raise MediaError("Spotify authorization failed.")

    async def resolve_spotify(self, url: str) -> list[MusicTrack]:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0].startswith("intl-"):
            parts = parts[1:]
        if len(parts) != 2 or parts[0] not in {"track", "playlist"} or not re.fullmatch(r"[A-Za-z0-9]{22}", parts[1]):
            raise ValueError("Only Spotify song and playlist links are supported.")
        endpoint = f"https://api.spotify.com/v1/{parts[0]}s/{parts[1]}"
        payload = await self.spotify_get(endpoint)
        if parts[0] == "track":
            items = [payload]
        else:
            page = payload.get("items", payload.get("tracks"))
            if not isinstance(page, dict):
                raise MediaError("Spotify returned playlist metadata without its songs. This playlist requires authorized user access; configure SPOTIFY_REFRESH_TOKEN for an eligible owner/collaborator, or use a track link.")
            items = list(page.get("items", []))
            next_url = page.get("next")
            visited = set()
            while isinstance(next_url, str):
                if len(items) > MAX_QUEUE_SIZE:
                    raise ValueError(f"Please choose a playlist with at most {MAX_QUEUE_SIZE} tracks.")
                if next_url in visited:
                    raise MediaError("Spotify repeated a playlist page. Try again later.")
                visited.add(next_url)
                page = await self.spotify_get(next_url)
                items.extend(page.get("items", []))
                next_url = page.get("next")
        tracks: list[MusicTrack] = []
        for item in items:
            if parts[0] == "playlist" and isinstance(item, dict):
                item = item.get("item", item.get("track", item))
            if not isinstance(item, dict) or not item.get("name"):
                continue
            if item.get("is_local") or item.get("type", "track") != "track":
                continue
            artists = ", ".join(
                artist["name"] for artist in item.get("artists", [])
                if isinstance(artist, dict) and isinstance(artist.get("name"), str)
            )
            query = f"ytsearch1:{artists} - {item['name']}"
            tracks.append(MusicTrack(source=query, title=f"{artists} - {item['name']}"))
        if not tracks:
            raise RuntimeError("No playable tracks were found in that Spotify link.")
        return tracks

    async def on_ready(self) -> None:
        LOGGER.info(
            "Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown"
        )

    async def post_to_configured_channel(
        self, guild_id: int, content: str, *, kind: str, role_id: int | None = None
    ) -> bool:
        channel_id = await self.database.get_channel(guild_id, kind)
        if not channel_id:
            LOGGER.warning("No configured channel for guild %s", guild_id)
            return False
        channel = self.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            LOGGER.warning("Configured channel %s is unavailable", channel_id)
            return False
        mention = f"<@&{role_id}> " if role_id else ""
        await channel.send(
            f"{mention}{content}",
            allowed_mentions=discord.AllowedMentions(
                everyone=False, users=False, replied_user=False,
                roles=[discord.Object(id=role_id)] if role_id else False,
            ),
        )
        return True

    async def get_twitch_token(self) -> str:
        if self.twitch_token:
            return self.twitch_token
        if (
            not self.http_session
            or not self.settings.twitch_client_id
            or not self.settings.twitch_client_secret
        ):
            raise RuntimeError("Twitch integration is not configured")
        async with self.http_session.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": self.settings.twitch_client_id,
                "client_secret": self.settings.twitch_client_secret,
                "grant_type": "client_credentials",
            },
        ) as response:
            response.raise_for_status()
            self.twitch_token = (await response.json())["access_token"]
            return self.twitch_token

    @tasks.loop(seconds=60)
    async def twitch_poll_loop(self) -> None:
        try:
            await self.poll_twitch()
        except (aiohttp.ClientError, TimeoutError, RuntimeError):
            LOGGER.exception("Twitch poll failed; will retry on the next interval")

    async def poll_twitch(self) -> None:
        if (
            not self.settings.twitch_client_id
            or not self.settings.twitch_client_secret
            or not self.http_session
        ):
            return
        accounts = await self.database.list_twitch_accounts()
        if not accounts:
            return
        token = await self.get_twitch_token()
        headers = {
            "Client-ID": self.settings.twitch_client_id,
            "Authorization": f"Bearer {token}",
        }
        usernames = sorted({account["username"].casefold() for account in accounts})
        streams: dict[str, dict[str, Any]] = {}
        for index in range(0, len(usernames), 100):
            async with self.http_session.get(
                "https://api.twitch.tv/helix/streams",
                params=[("first", "100")] + [
                    ("user_login", username) for username in usernames[index:index + 100]
                ],
                headers=headers,
            ) as response:
                if response.status == 401:
                    self.twitch_token = None
                    return
                response.raise_for_status()
                streams.update({
                    stream["user_login"].casefold(): stream
                    for stream in (await response.json()).get("data", [])
                })
        # Only update notification state after every request has succeeded.
        currently_live = {
            (account["guild_id"], account["username"].casefold())
            for account in accounts if account["username"].casefold() in streams
        }
        new_live_accounts = await self.database.sync_twitch_live_accounts(currently_live)
        for guild_id, username in sorted(new_live_accounts):
            stream = streams[username]
            role_id = await self.database.get_role(guild_id, "social")
            try:
                posted = await self.post_to_configured_channel(
                    guild_id,
                    f"**{stream['user_name']} is live:** https://twitch.tv/{stream['user_login']}",
                    kind="twitch",
                    role_id=role_id,
                )
                if not posted:
                    await self.database.remove_twitch_live_account(guild_id, username)
            except (discord.HTTPException, RuntimeError):
                await self.database.remove_twitch_live_account(guild_id, username)
                LOGGER.exception("Could not post Twitch notification to guild %s", guild_id)

    @twitch_poll_loop.before_loop
    async def before_twitch_poll_loop(self) -> None:
        await self.wait_until_ready()


def administrator_only() -> Any:
    def decorator(command: Any) -> Any:
        command = app_commands.guild_only()(command)
        command = app_commands.default_permissions(administrator=True)(command)
        return app_commands.checks.has_permissions(administrator=True)(command)

    return decorator


async def send_error(interaction: discord.Interaction, message: str) -> None:
    """Resolve pending responses without deleting the error the user needs to see."""
    if interaction.response.type == discord.InteractionResponseType.deferred_channel_message:
        await interaction.edit_original_response(content=message, view=None)
    elif interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def check_music_channel(interaction: discord.Interaction) -> bool:
    voice_client = interaction.guild.voice_client if interaction.guild else None
    voice = getattr(interaction.user, "voice", None)
    if voice_client and (not voice or voice.channel != voice_client.channel):
        await send_error(interaction, "Join my voice channel to control playback.")
        return False
    return True


def normalize_twitch_username(value: str) -> str:
    username = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,25}", username):
        raise ValueError("Enter a Twitch login name (letters, numbers, underscores), not a URL.")
    return username


async def publish_confirmation(
    interaction: discord.Interaction,
    message: str,
    *,
    dismiss_original: bool = False,
) -> None:
    """Post a successful command result publicly and dismiss private command UI."""
    already_responded = interaction.response.is_done()
    if interaction.response.type == discord.InteractionResponseType.deferred_channel_message:
        # Resolve the deferred response first: the first followup would otherwise
        # inherit its visibility, even when ephemeral=False is requested.
        await interaction.edit_original_response(content=message, view=None)
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=False, wait=True)
    else:
        await interaction.response.send_message(message)
    if dismiss_original and already_responded:
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            LOGGER.debug("Deferred interaction response was already dismissed.")


def parse_role_permissions(value: str) -> discord.Permissions:
    requested = {item.strip().lower() for item in value.split(",") if item.strip()}
    if not requested:
        raise ValueError("Enter at least one permission.")

    aliases = {
        "view": "view_channel",
        "read": "read_message_history",
        "write": "send_messages",
    }
    normalized = {aliases.get(item, item) for item in requested}
    invalid = sorted(normalized - set(discord.Permissions.VALID_FLAGS))
    if invalid:
        raise ValueError(f"Unknown permission(s): {', '.join(invalid)}")
    return discord.Permissions(**{permission: True for permission in normalized})


def parse_role_color(value: str) -> discord.Colour:
    normalized = value.strip().lstrip("#")
    if len(normalized) != 6:
        raise ValueError("Role color must be a 6-digit hexadecimal value, such as #5865F2.")
    try:
        return discord.Colour(int(normalized, 16))
    except ValueError as error:
        raise ValueError(
            "Role color must be a 6-digit hexadecimal value, such as #5865F2."
        ) from error


class SetupModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        LOGGER.error("Setup modal failed", exc_info=(type(error), error, error.__traceback__))
        await send_error(interaction, "The setup could not open. Please run the command again.")


class AccessSetupModal(SetupModal, title="Create role"):
    role_name = discord.ui.TextInput(
        label="Role name",
        placeholder="e.g. Event Staff",
        max_length=100,
    )
    role_color = discord.ui.TextInput(
        label="Role color (hex)",
        placeholder="#5865F2",
        default="#5865F2",
        max_length=7,
    )

    def __init__(self, owner_id: int, bot: ZodiacBot) -> None:
        super().__init__()
        self.owner_id = owner_id
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the person who opened this setup can use it.", ephemeral=True
            )
            return
        try:
            color = parse_role_color(str(self.role_color))
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        view = PermissionSetupView(
            owner_id=self.owner_id,
            bot=self.bot,
            role_name=str(self.role_name).strip(),
            color=color,
        )
        await interaction.response.send_message(
            "Select the permissions for the role, then click **Continue**.",
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()


class PermissionSelect(discord.ui.Select):
    def __init__(self, setup_view: PermissionSetupView) -> None:
        self.setup_view = setup_view
        super().__init__(
            placeholder="Choose permissions",
            min_values=0,
            max_values=len(setup_view.permission_pages[setup_view.permission_page]),
            options=self.build_options(),
            row=0,
        )

    def build_options(self) -> list[discord.SelectOption]:
        page_permissions = self.setup_view.permission_pages[
            self.setup_view.permission_page
        ]
        return [
            discord.SelectOption(
                label=permission.replace("_", " ").title(),
                value=permission,
                default=permission in self.setup_view.selected_permissions,
            )
            for permission in page_permissions
        ]

    def refresh_options(self) -> None:
        self.options = self.build_options()
        self.max_values = len(
            self.setup_view.permission_pages[self.setup_view.permission_page]
        )
        self.placeholder = (
            f"Choose permissions (page {self.setup_view.permission_page + 1}/"
            f"{len(self.setup_view.permission_pages)})"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        page_permissions = set(
            self.setup_view.permission_pages[self.setup_view.permission_page]
        )
        self.setup_view.selected_permissions.difference_update(page_permissions)
        self.setup_view.selected_permissions.update(self.values)
        self.setup_view.refresh_permission_controls()
        await interaction.response.edit_message(view=self.setup_view)


class SetupView(discord.ui.View):
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=4)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.disable_all_items()
        await interaction.response.edit_message(content="Setup cancelled.", view=None)

    async def begin_operation(self, interaction: discord.Interaction) -> bool:
        if not interaction.permissions.administrator:
            await send_error(interaction, "You need Administrator permission to submit this setup.")
            return False
        if self.is_finished():
            await send_error(interaction, "This setup has already been submitted. Start a new command to try again.")
            return False
        self.disable_all_items()
        await interaction.response.defer(thinking=True, ephemeral=True)
        if self.message:
            with suppress(discord.NotFound):
                await self.message.delete()
            self.message = None
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        LOGGER.error("Setup failed", exc_info=(type(error), error, error.__traceback__))
        await send_error(interaction, "The setup could not finish. Check the current roles/channels before trying again.")

    async def on_timeout(self) -> None:
        self.disable_all_items()
        if self.message:
            with suppress(discord.HTTPException):
                await self.message.edit(content="This setup expired. Run the command again to continue.", view=self)

    def disable_all_items(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select, discord.ui.ChannelSelect)):
                item.disabled = True
        self.stop()


class PermissionSetupView(SetupView):
    def __init__(
        self,
        owner_id: int,
        bot: ZodiacBot,
        role_name: str,
        color: discord.Colour,
    ) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.bot = bot
        self.role_name = role_name
        self.color = color
        self.selected_permissions: set[str] = set()
        self.message: discord.WebhookMessage | None = None
        permissions = list(PERMISSION_NAMES)
        self.permission_pages = [
            permissions[index : index + 25]
            for index in range(0, len(permissions), 25)
        ]
        self.permission_page = 0
        self.permission_select = PermissionSelect(self)
        self.add_item(self.permission_select)
        self.refresh_permission_controls()

    def refresh_permission_controls(self) -> None:
        self.permission_select.refresh_options()
        self.previous_page.disabled = self.permission_page == 0
        self.next_page.disabled = self.permission_page == len(self.permission_pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this setup can use it.", ephemeral=True
        )
        return False

    @discord.ui.button(
        label="Previous permissions", style=discord.ButtonStyle.secondary, row=1
    )
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.permission_page -= 1
        self.refresh_permission_controls()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(
        label="Next permissions", style=discord.ButtonStyle.secondary, row=1
    )
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.permission_page += 1
        self.refresh_permission_controls()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.success, row=2)
    async def continue_setup(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self.selected_permissions:
            await interaction.response.send_message(
                "Select at least one permission first.", ephemeral=True
            )
            return
        view = RoleSetupView(
            owner_id=self.owner_id,
            bot=self.bot,
            role_name=self.role_name,
            permissions=discord.Permissions(
                **{permission: True for permission in self.selected_permissions}
            ),
            color=self.color,
        )
        await interaction.response.edit_message(
            content=(
                "Review the role details, then click **Create**. This command creates "
                "only the role; use `/create_channel` separately for channels."
            ),
            view=view,
        )
        view.message = await interaction.original_response()
        self.stop()


class RoleSetupView(SetupView):
    def __init__(
        self,
        owner_id: int,
        bot: ZodiacBot,
        role_name: str,
        permissions: discord.Permissions,
        color: discord.Colour,
    ) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.bot = bot
        self.role_name = role_name
        self.permissions = permissions
        self.color = color
        self.message: discord.WebhookMessage | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this setup can use it.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Create", style=discord.ButtonStyle.success)
    async def create(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This setup can only be used in a server.", ephemeral=True
            )
            return
        member = guild.me
        if (
            member is None
            or not member.guild_permissions.manage_roles
        ):
            await interaction.response.send_message(
                "I need the **Manage Roles** permission.",
                ephemeral=True,
            )
            return
        if not self.role_name:
            await interaction.response.send_message(
                "The role name cannot be empty.", ephemeral=True
            )
            return

        if not await self.begin_operation(interaction):
            return
        try:
            role = await guild.create_role(
                name=self.role_name,
                permissions=self.permissions,
                colour=self.color,
                reason=f"Access setup by {interaction.user}",
            )
        except discord.HTTPException:
            LOGGER.exception("Failed to create role in guild %s", guild.id)
            await send_error(interaction,
                "Discord rejected the role creation. Check my role position and "
                "permissions, then try again.",
            )
            return

        self.disable_all_items()
        await publish_confirmation(
            interaction,
            f"Created {role.mention} with the requested permissions and color.",
            dismiss_original=True,
        )


class RoleEditPermissionSelect(discord.ui.Select):
    def __init__(self, edit_view: RoleEditView) -> None:
        self.edit_view = edit_view
        super().__init__(
            placeholder="Choose permissions",
            min_values=0,
            max_values=len(edit_view.permission_pages[edit_view.permission_page]),
            options=self.build_options(),
            row=0,
        )

    def build_options(self) -> list[discord.SelectOption]:
        page_permissions = self.edit_view.permission_pages[self.edit_view.permission_page]
        return [
            discord.SelectOption(
                label=permission.replace("_", " ").title(),
                value=permission,
                default=permission in self.edit_view.selected_permissions,
            )
            for permission in page_permissions
        ]

    def refresh_options(self) -> None:
        self.options = self.build_options()
        self.max_values = len(
            self.edit_view.permission_pages[self.edit_view.permission_page]
        )
        self.placeholder = (
            f"Permissions (page {self.edit_view.permission_page + 1}/"
            f"{len(self.edit_view.permission_pages)})"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        page_permissions = set(
            self.edit_view.permission_pages[self.edit_view.permission_page]
        )
        self.edit_view.selected_permissions.difference_update(page_permissions)
        self.edit_view.selected_permissions.update(self.values)
        self.edit_view.refresh_controls()
        await interaction.response.edit_message(view=self.edit_view)


class RoleEditChannelSelect(discord.ui.Select):
    def __init__(self, edit_view: RoleEditView) -> None:
        self.edit_view = edit_view
        super().__init__(
            placeholder="Choose channels the role can access",
            min_values=0,
            max_values=max(1, len(edit_view.channel_pages[edit_view.channel_page])),
            options=self.build_options(),
            row=1,
        )

    def build_options(self) -> list[discord.SelectOption]:
        page_channels = self.edit_view.channel_pages[self.edit_view.channel_page]
        return [
            discord.SelectOption(
                label=channel.name[:100],
                value=str(channel.id),
                default=channel.id in self.edit_view.selected_channel_ids,
            )
            for channel in page_channels
        ] or [discord.SelectOption(label="No channels available", value="none")]

    def refresh_options(self) -> None:
        self.options = self.build_options()
        self.max_values = max(1, len(
            self.edit_view.channel_pages[self.edit_view.channel_page]
        ))
        self.disabled = not self.edit_view.channel_pages[self.edit_view.channel_page]
        self.placeholder = (
            f"Channels (page {self.edit_view.channel_page + 1}/"
            f"{len(self.edit_view.channel_pages)})"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        page_channel_ids = {
            channel.id
            for channel in self.edit_view.channel_pages[self.edit_view.channel_page]
        }
        self.edit_view.selected_channel_ids.difference_update(page_channel_ids)
        self.edit_view.selected_channel_ids.update(int(value) for value in self.values)
        self.edit_view.refresh_controls()
        await interaction.response.edit_message(view=self.edit_view)


class RoleEditView(SetupView):
    def __init__(
        self,
        owner_id: int,
        role: discord.Role,
        channels: list[discord.abc.GuildChannel],
    ) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.role = role
        self.selected_permissions = {
            permission
            for permission in PERMISSION_NAMES
            if getattr(role.permissions, permission)
        }
        self.selected_channel_ids = {
            channel.id
            for channel in channels
            if channel.permissions_for(role).view_channel
        }
        permissions = list(PERMISSION_NAMES)
        self.permission_pages = [
            permissions[index : index + 25]
            for index in range(0, len(permissions), 25)
        ]
        self.channel_pages = [
            channels[index : index + 25] for index in range(0, len(channels), 25)
        ] or [[]]
        self.permission_page = 0
        self.channel_page = 0
        self.permission_select = RoleEditPermissionSelect(self)
        self.channel_select = RoleEditChannelSelect(self)
        self.add_item(self.permission_select)
        self.add_item(self.channel_select)
        self.message: discord.WebhookMessage | None = None
        self.refresh_controls()

    def refresh_controls(self) -> None:
        self.permission_select.refresh_options()
        self.channel_select.refresh_options()
        self.previous_permission_page.disabled = self.permission_page == 0
        self.next_permission_page.disabled = (
            self.permission_page == len(self.permission_pages) - 1
        )
        self.previous_channel_page.disabled = self.channel_page == 0
        self.next_channel_page.disabled = self.channel_page == len(self.channel_pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this editor can use it.", ephemeral=True
        )
        return False

    @discord.ui.button(
        label="Previous permissions", style=discord.ButtonStyle.secondary, row=2
    )
    async def previous_permission_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.permission_page -= 1
        self.refresh_controls()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(
        label="Next permissions", style=discord.ButtonStyle.secondary, row=2
    )
    async def next_permission_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.permission_page += 1
        self.refresh_controls()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(
        label="Previous channels", style=discord.ButtonStyle.secondary, row=3
    )
    async def previous_channel_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.channel_page -= 1
        self.refresh_controls()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(
        label="Next channels", style=discord.ButtonStyle.secondary, row=3
    )
    async def next_channel_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.channel_page += 1
        self.refresh_controls()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Save changes", style=discord.ButtonStyle.success, row=4)
    async def save(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        guild = interaction.guild
        member = guild.me if guild else None
        if guild is None or member is None:
            await interaction.response.send_message(
                "This editor can only be used in a server.", ephemeral=True
            )
            return
        if not member.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I need the **Manage Roles** permission.", ephemeral=True
            )
            return
        if not member.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "I need the **Manage Channels** permission.", ephemeral=True
            )
            return
        if self.role.managed or self.role == guild.default_role or self.role >= member.top_role:
            await interaction.response.send_message(
                "I can only edit roles below my highest role.", ephemeral=True
            )
            return

        channels = [channel for page in self.channel_pages for channel in page]
        selected_channels = set(self.selected_channel_ids)
        permissions = discord.Permissions(
            **{
                permission: permission in self.selected_permissions
                for permission in PERMISSION_NAMES
            }
        )
        if not await self.begin_operation(interaction):
            return
        try:
            if self.role.permissions != permissions:
                self.role = await self.role.edit(
                    permissions=permissions,
                    reason=f"Role edited by {interaction.user}",
                )
            for channel in channels:
                overwrite = channel.overwrites_for(self.role)
                allowed = channel.id in selected_channels
                if all(getattr(overwrite, permission) is allowed for permission in (
                    "view_channel", "read_message_history", "send_messages"
                )):
                    continue
                overwrite.view_channel = allowed
                overwrite.read_message_history = allowed
                overwrite.send_messages = allowed
                await channel.set_permissions(
                    self.role,
                    overwrite=overwrite,
                    reason=f"Role channel access edited by {interaction.user}",
                )
        except discord.HTTPException:
            LOGGER.exception("Failed to edit role %s in guild %s", self.role.id, guild.id)
            await send_error(interaction,
                "Discord rejected the role or channel permission update. "
                "Some changes may already have been applied. Check my role position and permissions.",
            )
            return

        self.disable_all_items()
        await publish_confirmation(
            interaction,
            f"Updated {self.role.mention} permissions and access for "
            f"{len(selected_channels)} channel(s).",
            dismiss_original=True,
        )


class ChannelSetupModal(SetupModal, title="Create private channel"):
    channel_name = discord.ui.TextInput(
        label="Channel name",
        placeholder="e.g. event-planning",
        max_length=100,
    )

    def __init__(self, owner_id: int, bot: ZodiacBot) -> None:
        super().__init__()
        self.owner_id = owner_id
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This setup can only be used in a server.", ephemeral=True
            )
            return
        view = ChannelSetupView(
            owner_id=self.owner_id,
            bot=self.bot,
            channel_name=str(self.channel_name).strip().lower().replace(" ", "-"),
            roles=[role for role in interaction.guild.roles if role != interaction.guild.default_role],
        )
        await interaction.response.send_message(
            "Select a category and the roles that should access the channel, "
            "then click **Create**.",
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()


class AllRolesSelect(discord.ui.Select):
    def __init__(self, view: ChannelSetupView) -> None:
        self.setup_view = view
        super().__init__(
            placeholder="Choose roles with access",
            min_values=0,
            max_values=max(1, min(25, len(view.role_pages[view.role_page]))),
            options=self.build_options(),
            row=1,
        )

    def build_options(self) -> list[discord.SelectOption]:
        page_roles = self.setup_view.role_pages[self.setup_view.role_page]
        return [
            discord.SelectOption(
                label=role.name[:100],
                value=str(role.id),
                default=role.id in self.setup_view.selected_role_ids,
            )
            for role in page_roles
        ] or [discord.SelectOption(label="No roles available", value="none")]

    def refresh_options(self) -> None:
        self.options = self.build_options()
        self.max_values = max(1, min(25, len(self.setup_view.role_pages[self.setup_view.role_page])))
        self.disabled = not self.setup_view.role_pages[self.setup_view.role_page]
        self.placeholder = (
            f"Choose roles (page {self.setup_view.role_page + 1}/"
            f"{len(self.setup_view.role_pages)})"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        page_role_ids = {
            role.id for role in self.setup_view.role_pages[self.setup_view.role_page]
        }
        self.setup_view.selected_role_ids = [
            role_id
            for role_id in self.setup_view.selected_role_ids
            if role_id not in page_role_ids
        ]
        self.setup_view.selected_role_ids.extend(int(value) for value in self.values)
        self.setup_view.refresh_role_controls()
        await interaction.response.edit_message(view=self.setup_view)


class ChannelSetupView(SetupView):
    def __init__(
        self,
        owner_id: int,
        bot: ZodiacBot,
        channel_name: str,
        roles: list[discord.Role],
    ) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.bot = bot
        self.channel_name = channel_name
        self.category_id: int | None = None
        self.selected_role_ids: list[int] = []
        self.message: discord.WebhookMessage | None = None
        self.role_pages = [
            roles[index : index + 25]
            for index in range(0, len(roles), 25)
            if roles[index : index + 25]
        ]
        if not self.role_pages:
            self.role_pages = [[]]
        self.role_page = 0
        self.role_select = AllRolesSelect(self)
        self.add_item(self.role_select)
        self.refresh_role_controls()

    def refresh_role_controls(self) -> None:
        self.role_select.refresh_options()
        self.previous_page.disabled = self.role_page == 0
        self.next_page.disabled = self.role_page == len(self.role_pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this setup can use it.", ephemeral=True
        )
        return False

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.category],
        placeholder="Choose a category",
        min_values=1,
        max_values=1,
    )
    async def category(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        self.category_id = select.values[0].id
        await interaction.response.edit_message(view=self)

    @discord.ui.button(
        label="Previous roles", style=discord.ButtonStyle.secondary, row=2
    )
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.role_page -= 1
        self.refresh_role_controls()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(
        label="Next roles", style=discord.ButtonStyle.secondary, row=2
    )
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.role_page += 1
        self.refresh_role_controls()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Create", style=discord.ButtonStyle.success, row=2)
    async def create(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This setup can only be used in a server.", ephemeral=True
            )
            return
        member = guild.me
        if member is None or not member.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "I need the **Manage Channels** permission.", ephemeral=True
            )
            return
        if not self.category_id:
            await interaction.response.send_message(
                "Select a category first.", ephemeral=True
            )
            return
        if not self.selected_role_ids:
            await interaction.response.send_message(
                "Select at least one role first.", ephemeral=True
            )
            return
        if not self.channel_name:
            await interaction.response.send_message(
                "The channel name cannot be empty.", ephemeral=True
            )
            return

        category = guild.get_channel(self.category_id)
        selected_roles = [
            role
            for role_id in self.selected_role_ids
            if (role := guild.get_role(role_id)) is not None
            and role != guild.default_role
        ]
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "That category is no longer available.", ephemeral=True
            )
            return
        if not selected_roles:
            await interaction.response.send_message(
                "The selected roles are no longer available.", ephemeral=True
            )
            return

        overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                read_message_history=True),
        }
        for role in selected_roles:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )

        if not await self.begin_operation(interaction):
            return
        try:
            channel = await guild.create_text_channel(
                self.channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Channel setup by {interaction.user}",
            )
        except discord.HTTPException:
            LOGGER.exception("Failed to create channel in guild %s", guild.id)
            await send_error(interaction,
                "Discord rejected the channel creation. Check my channel "
                "permissions and try again.",
            )
            return

        self.disable_all_items()
        await publish_confirmation(
            interaction,
            f"Created {channel.mention} in {category.name} with access for the "
            "selected roles.",
            dismiss_original=True,
        )


def register_commands(bot: ZodiacBot) -> None:
    @bot.tree.command(
        name="play", description="Join your voice channel and play a YouTube or Spotify link."
    )
    @app_commands.describe(url="A YouTube video/playlist or Spotify song/playlist URL")
    @app_commands.guild_only()
    async def play(interaction: discord.Interaction, url: str) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await interaction.response.send_message(
                "Join a voice channel first.", ephemeral=True
            )
            return
        channel = member.voice.channel
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await interaction.response.send_message(
                "I can only join a standard voice or stage channel.", ephemeral=True
            )
            return
        voice_client = interaction.guild.voice_client if interaction.guild else None
        if voice_client and voice_client.channel != channel:
            await interaction.response.send_message(
                "I am already playing in another voice channel.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            tracks = await bot.resolve_media(url.strip())
            tracks = [replace(track, notification_channel_id=interaction.channel_id) for track in tracks]
            player = bot.music_player(interaction.guild_id)
            async with player.connection_lock:
                if player.player_task.done():
                    raise ValueError("Playback was stopped while the link loaded. Run /play again.")
                voice_client = interaction.guild.voice_client
                if voice_client and voice_client.channel != channel:
                    raise ValueError("I am already playing in another voice channel.")
                if player.queue.qsize() + len(tracks) > MAX_QUEUE_SIZE:
                    raise ValueError(f"The queue can hold at most {MAX_QUEUE_SIZE} upcoming tracks.")
                player.voice_client = voice_client or await channel.connect()
                await player.enqueue(tracks)
        except (ValueError, RuntimeError, discord.ClientException, discord.HTTPException,
                aiohttp.ClientError, TimeoutError) as error:
            LOGGER.warning("Could not queue media in guild %s: %s", interaction.guild_id, error)
            await send_error(interaction, str(error))
            return
        if len(tracks) == 1:
            message = f"Queued **{discord.utils.escape_markdown(tracks[0].title[:300])}**."
        else:
            message = f"Queued **{len(tracks)} tracks**."
        await publish_confirmation(interaction, message, dismiss_original=True)

    @bot.tree.command(name="skip", description="Skip the currently playing track.")
    @app_commands.guild_only()
    async def skip(interaction: discord.Interaction) -> None:
        if not await check_music_channel(interaction):
            return
        player = bot.music_players.get(interaction.guild_id)
        if not player or not player.voice_client or not (
            player.voice_client.is_playing() or player.voice_client.is_paused()
        ):
            await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)
            return
        player.voice_client.stop()
        await publish_confirmation(interaction, "Skipped.")

    @bot.tree.command(name="pause", description="Pause the currently playing track.")
    @app_commands.guild_only()
    async def pause(interaction: discord.Interaction) -> None:
        if not await check_music_channel(interaction):
            return
        player = bot.music_players.get(interaction.guild_id)
        if not player or not player.voice_client or not player.voice_client.is_playing():
            await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)
            return
        player.voice_client.pause()
        await publish_confirmation(interaction, "Paused.")

    @bot.tree.command(name="resume", description="Resume paused music.")
    @app_commands.guild_only()
    async def resume(interaction: discord.Interaction) -> None:
        if not await check_music_channel(interaction):
            return
        player = bot.music_players.get(interaction.guild_id)
        if not player or not player.voice_client or not player.voice_client.is_paused():
            await interaction.response.send_message("Nothing is paused.", ephemeral=True)
            return
        player.voice_client.resume()
        await publish_confirmation(interaction, "Resumed.")

    @bot.tree.command(name="leave", description="Stop music and leave the voice channel.")
    @app_commands.guild_only()
    async def leave(interaction: discord.Interaction) -> None:
        if not await check_music_channel(interaction):
            return
        player = bot.music_players.get(interaction.guild_id)
        if not player:
            await interaction.response.send_message("I am not in a voice channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with player.connection_lock:
            await player.stop()
            bot.music_players.pop(interaction.guild_id, None)
        await publish_confirmation(
            interaction, "Stopped playback and left the voice channel.", dismiss_original=True
        )

    @bot.tree.command(name="queue", description="Show the current track and upcoming music.")
    @app_commands.guild_only()
    async def queue(interaction: discord.Interaction) -> None:
        player = bot.music_players.get(interaction.guild_id)
        if not player or (player.current is None and player.queue.empty()):
            await interaction.response.send_message("The music queue is empty.", ephemeral=True)
            return
        lines = []
        if player.current:
            lines.append(f"Current: **{discord.utils.escape_markdown(player.current.title[:70])}**")
        upcoming = list(player.queue._queue)
        lines.extend(f"{index}. {discord.utils.escape_markdown(track.title[:70])}"
                     for index, track in enumerate(upcoming[:10], 1))
        if len(upcoming) > 10:
            lines.append(f"…and {len(upcoming) - 10} more tracks.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @bot.tree.command(
        name="setup",
        description="Configure channels and the social media notification role.",
    )
    @app_commands.describe(
        social_channel="Channel for X posts",
        twitch_channel="Channel for Twitch notifications",
        social_role="Role to ping",
    )
    @administrator_only()
    async def setup(
        interaction: discord.Interaction,
        social_channel: discord.TextChannel,
        twitch_channel: discord.TextChannel,
        social_role: discord.Role,
    ) -> None:
        await bot.database.set_channel(
            interaction.guild_id, "social", social_channel.id
        )
        await bot.database.set_channel(
            interaction.guild_id, "twitch", twitch_channel.id
        )
        await bot.database.set_role(interaction.guild_id, "social", social_role.id)
        await publish_confirmation(
            interaction,
            "Saved the Zodiac notification channels and social media notification role.",
        )

    @bot.tree.command(
        name="create_access",
        description="Interactively create a role with permissions and a color.",
    )
    @administrator_only()
    @app_commands.guild_only()
    async def create_access(interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            AccessSetupModal(interaction.user.id, bot)
        )

    @bot.tree.command(
        name="edit_role",
        description="Edit an existing role's permissions and channel access.",
    )
    @app_commands.describe(role="Role to edit")
    @administrator_only()
    @app_commands.guild_only()
    async def edit_role(interaction: discord.Interaction, role: str) -> None:
        guild = interaction.guild
        member = guild.me if guild else None
        if guild is None or member is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        selected_role = guild.get_role(int(role)) if role.isdigit() else None
        if selected_role is None:
            await interaction.response.send_message(
                "Select a valid role from the server.", ephemeral=True
            )
            return
        role = selected_role
        if role.managed or role == guild.default_role:
            await interaction.response.send_message(
                "The @everyone role and integration-managed roles cannot be edited.", ephemeral=True
            )
            return
        if not member.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I need the **Manage Roles** permission.", ephemeral=True
            )
            return
        if not member.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "I need the **Manage Channels** permission.", ephemeral=True
            )
            return
        if role >= member.top_role:
            await interaction.response.send_message(
                "I can only edit roles below my highest role.", ephemeral=True
            )
            return

        channels = sorted(
            (
                channel
                for channel in guild.channels
                if isinstance(
                    channel,
                    (
                        discord.CategoryChannel,
                        discord.TextChannel,
                        discord.VoiceChannel,
                        discord.StageChannel,
                        discord.ForumChannel,
                    ),
                )
            ),
            key=lambda channel: (channel.category_id or 0, channel.position, channel.name),
        )
        view = RoleEditView(interaction.user.id, role, channels)
        await interaction.response.send_message(
            f"Edit **{role.name}**. Select its permissions and the channels it should "
            "be able to access, then click **Save changes**.",
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    @edit_role.autocomplete("role")
    async def edit_role_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        guild = interaction.guild
        if guild is None:
            return []
        search = current.casefold().strip()
        roles = [
            role
            for role in reversed(guild.roles)
            if not role.managed and role != guild.default_role
            and guild.me is not None and role < guild.me.top_role
            and (not search or search in role.name.casefold())
        ]
        return [
            app_commands.Choice(name=role.name[:100], value=str(role.id))
            for role in roles[:25]
        ]

    @bot.tree.command(
        name="create_channel",
        description="Interactively create a private channel in a category.",
    )
    @administrator_only()
    @app_commands.guild_only()
    async def create_channel(interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            ChannelSetupModal(interaction.user.id, bot)
        )

    @bot.tree.command(name="delete_role", description="Delete a server role.")
    @app_commands.describe(role="Role to delete")
    @administrator_only()
    @app_commands.guild_only()
    async def delete_role(
        interaction: discord.Interaction, role: discord.Role
    ) -> None:
        guild = interaction.guild
        member = guild.me if guild else None
        if guild is None or member is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if role.managed or role == guild.default_role:
            await interaction.response.send_message(
                "The @everyone role and integration-managed roles cannot be deleted.", ephemeral=True
            )
            return
        if not member.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I need the **Manage Roles** permission.", ephemeral=True
            )
            return
        if role >= member.top_role:
            await interaction.response.send_message(
                "I can only delete roles below my highest role.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await role.delete(reason=f"Deleted by {interaction.user}")
        except discord.HTTPException:
            LOGGER.exception("Failed to delete role %s in guild %s", role.id, guild.id)
            await send_error(interaction,
                "Discord rejected the role deletion. Check my permissions and "
                "role hierarchy.",
            )
            return
        await publish_confirmation(interaction, f"Deleted the `{role.name}` role.", dismiss_original=True)

    @bot.tree.command(name="delete_channel", description="Delete a server channel.")
    @app_commands.describe(channel="Channel to delete")
    @administrator_only()
    @app_commands.guild_only()
    async def delete_channel(
        interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        guild = interaction.guild
        member = guild.me if guild else None
        if guild is None or member is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        if not member.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "I need the **Manage Channels** permission.", ephemeral=True
            )
            return

        if channel.id == interaction.channel_id:
            await send_error(interaction, "Run /delete_channel from another channel so I can post the result there.")
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await channel.delete(reason=f"Deleted by {interaction.user}")
        except discord.HTTPException:
            LOGGER.exception(
                "Failed to delete channel %s in guild %s", channel.id, guild.id
            )
            await send_error(interaction,
                "Discord rejected the channel deletion. Check my permissions.",
            )
            return
        await publish_confirmation(interaction, f"Deleted the `#{channel.name}` channel.", dismiss_original=True)

    @bot.tree.command(
        name="add_twitch",
        description="Register a Twitch account for live notifications.",
    )
    @app_commands.describe(username="Twitch login name")
    @administrator_only()
    async def add_twitch(interaction: discord.Interaction, username: str) -> None:
        username = normalize_twitch_username(username)
        await bot.database.add_twitch_account(
            interaction.guild_id, username.strip().lower()
        )
        await publish_confirmation(interaction, f"Registered Twitch account `{username}`.")

    @bot.tree.command(
        name="remove_twitch", description="Stop notifying for a Twitch account."
    )
    @administrator_only()
    async def remove_twitch(interaction: discord.Interaction, username: str) -> None:
        normalized_username = normalize_twitch_username(username)
        removed = await bot.database.remove_twitch_account(
            interaction.guild_id, normalized_username
        )
        await bot.database.remove_twitch_live_account(
            interaction.guild_id, normalized_username
        )
        message = (
            f"Removed `{username}`." if removed else f"`{username}` was not registered."
        )
        await publish_confirmation(interaction, message)

    @bot.tree.command(
        name="list_twitch",
        description="Show the Twitch accounts registered for this server.",
    )
    @administrator_only()
    @app_commands.guild_only()
    async def list_twitch(interaction: discord.Interaction) -> None:
        accounts = await bot.database.list_twitch_accounts(interaction.guild_id)
        if not accounts:
            message = "No Twitch accounts are currently registered."
        else:
            usernames = "\n".join(f"- `{account['username']}`" for account in accounts)
            message = f"Registered Twitch accounts:\n{usernames}"
        for index in range(0, len(message), 1900):
            await publish_confirmation(interaction, message[index:index + 1900])

    @bot.tree.command(
        name="clear", description="Delete recent messages from this channel."
    )
    @app_commands.describe(amount="Number of messages to delete, from 1 to 100")
    @administrator_only()
    async def clear(
        interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "This command can only run in a text channel.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        deleted = await channel.purge(limit=amount)
        await publish_confirmation(
            interaction, f"Deleted {len(deleted)} messages.", dismiss_original=True
        )

    @bot.tree.command(name="timeout", description="Timeout a member.")
    @app_commands.describe(member="Member to timeout", minutes="Duration in minutes")
    @administrator_only()
    async def timeout(
        interaction: discord.Interaction,
        member: discord.Member,
        minutes: app_commands.Range[int, 1, 40320],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await member.timeout(
            discord.utils.utcnow() + timedelta(minutes=minutes),
            reason=f"Moderated by {interaction.user}",
        )
        await publish_confirmation(
            interaction, f"Timed out {member.mention} for {minutes} minutes.", dismiss_original=True
        )

    @bot.tree.command(name="kick", description="Kick a member.")
    @administrator_only()
    async def kick(
        interaction: discord.Interaction,
        member: discord.Member,
        reason: app_commands.Range[str, 1, 400] = "No reason provided",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await member.kick(reason=reason)
        await publish_confirmation(interaction, f"Kicked {member} ({reason}).", dismiss_original=True)

    @bot.tree.command(name="ban", description="Ban a member.")
    @administrator_only()
    async def ban(
        interaction: discord.Interaction,
        member: discord.Member,
        reason: app_commands.Range[str, 1, 400] = "No reason provided",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await member.ban(reason=reason)
        await publish_confirmation(interaction, f"Banned {member} ({reason}).", dismiss_original=True)

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(error, app_commands.MissingPermissions):
            message = "You do not have permission to use this command."
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "This command can only be used in a server."
        elif isinstance(original, discord.Forbidden):
            message = "Discord denied that action. Check my channel permissions and role position."
        elif isinstance(original, discord.NotFound):
            message = "That member, role, channel, or message is no longer available."
        elif isinstance(original, ValueError):
            message = str(original)
        elif isinstance(original, (aiohttp.ClientError, TimeoutError)):
            message = "An external service could not be reached. Please try again shortly."
        else:
            LOGGER.exception("Slash command failed", exc_info=error)
            message = "The command failed. Check the bot logs for details."
        await send_error(interaction, message)


async def webhook_handler(request: web.Request) -> web.Response:
    bot: ZodiacBot = request.app["bot"]
    provided = request.headers.get("X-Webhook-Secret", "")
    if not secrets.compare_digest(provided, bot.settings.webhook_secret):
        raise web.HTTPUnauthorized(text="Invalid webhook secret")
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="Request body must be valid JSON") from None
    nested_payload = payload.get("data", {}) if isinstance(payload, dict) else {}
    url = payload.get("url") if isinstance(payload, dict) else None
    if not url and isinstance(nested_payload, dict):
        url = nested_payload.get("url")
    if not isinstance(url, str) or not re.fullmatch(
        r"https://(?:x\.com|twitter\.com)/[A-Za-z0-9_]+/status/[0-9]+(?:\?[^\s<>]*)?", url
    ) or len(url) > 1500:
        raise web.HTTPBadRequest(text="Payload must contain an X post URL in `url`")
    guild_ids = await bot.database.list_guild_ids()
    posted = 0
    failed = 0
    for guild_id in guild_ids:
        try:
            posted += bool(await bot.post_to_configured_channel(
                guild_id,
                f"New post from Zodiac eSports: {url}",
                kind="social",
                role_id=await bot.database.get_role(guild_id, "social"),
            ))
        except discord.HTTPException:
            failed += 1
            LOGGER.exception("Could not forward X post to guild %s", guild_id)
    return web.json_response({"posted": posted, "failed": failed,
                              "skipped": len(guild_ids) - posted - failed})


async def run_webhook_server(bot: ZodiacBot) -> None:
    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/webhooks/x", webhook_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(
        runner, bot.settings.webhook_host, bot.settings.webhook_port
    ).start()
    bot.webhook_runner = runner
    LOGGER.info(
        "Webhook listening on %s:%s",
        bot.settings.webhook_host,
        bot.settings.webhook_port,
    )


async def main() -> None:
    settings = Settings.from_env()
    database = Database(os.getenv("DATABASE_PATH", "zodiac.db"))
    await database.initialize()
    bot = ZodiacBot(settings, database)
    register_commands(bot)
    async with bot:
        await run_webhook_server(bot)
        await bot.start(settings.discord_token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
