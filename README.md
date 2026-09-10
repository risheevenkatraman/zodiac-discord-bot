# Zodiac eSports Community Discord Bot

Zodiac is a Discord community management bot for moderation, notifications,
Twitch monitoring, and interactive server administration. It uses Discord slash
commands and interactive components so authorized members can configure roles
and private channels without manually editing permission overwrites.

## Commands

### Moderation

- `/clear` - Delete 1 to 100 recent messages from the current text channel.
- `/timeout` - Temporarily restrict a member.
- `/kick` - Remove a member from the server.
- `/ban` - Ban a member from the server.

### Server configuration

- `/setup` - Configure the X/social notification channel, Twitch notification
  channel, and role to mention.
- `/create_access` - Open a GUI that creates a role with a selected color and
  Discord permissions.
- `/edit_role` - Edit an existing role's Discord permissions and the channels it
  can access.
- `/create_channel` - Open a GUI to choose a category, select roles, and create a
  private text channel with the appropriate permission overwrites.
- `/delete_role` - Delete a selected server role.
- `/delete_channel` - Delete a selected text channel.
  Run this from a different channel so the confirmation has somewhere to appear.

### Twitch notifications

- `/add_twitch` - Register a Twitch account for live notifications.
- `/remove_twitch` - Stop monitoring a registered Twitch account.
- `/list_twitch` - Show all Twitch accounts registered for the server.

### Music

- `/play` - Join your voice channel and queue a YouTube video/playlist or Spotify
  song/playlist URL.
- `/skip` - Skip the current track.
- `/pause` and `/resume` - Pause or resume playback.
- `/leave` - Stop playback, clear the queue, and leave voice.
- `/queue` - Privately show the current track and the next 10 queued tracks.

Join the bot's voice channel to skip, pause, resume, or stop playback. The queue
holds up to 200 upcoming tracks; playlists that exceed the available space are
rejected without adding a partial playlist. Skipping also works while paused.

All moderation and server-configuration commands require the Discord Administrator
permission. Music commands are available to server members.

## Features

- Discord application-command interface using slash commands.
- Permission checks for moderation and server administration commands.
- Interactive modals, role selectors, channel selectors, and confirmation
  buttons.
- Private channel creation with explicit `@everyone` denial and role access
  rules.
- Role creation with configurable Discord permissions.
- Twitch Helix polling with persisted live notification state across restarts,
  batched requests, and support for the same streamer in multiple servers.
- Authenticated X webhook endpoint at `POST /webhooks/x`.
- X post URL forwarding to the configured social channel with optional role
  mentions.
- SQLite persistence for configured channels, roles, and Twitch accounts.
- Environment-based configuration for tokens, secrets, polling, and database
  paths.
- Local HTTP webhook server for external X automation integrations.

Setup dialogs include a Cancel button and expire after five minutes. Submitted
dialogs cannot create duplicate roles or channels through repeated clicks.
Errors remain visible privately, and successful changes receive public
confirmations. Ordinary command output does not ping roles or users; social
notifications mention only the role configured with `/setup`.

## Running and testing

Install dependencies with `python -m pip install -r requirements.txt`. The
Discord voice extras are included; FFmpeg must also be installed on the host.
Copy `.env.example` to `.env`, fill in the required values, and run `python bot.py`.
Enable the Server Members Intent in the Discord developer portal for this bot.
Twitch and Spotify credentials are optional, but each client ID must be supplied
with its matching client secret. `TWITCH_POLL_SECONDS` controls the polling
interval, with a minimum of 30 seconds.

Run the regression suite with:

```sh
python -m unittest discover -s tests -v
```

The tests use mocked services and temporary SQLite databases; they do not send
Discord messages. After updating a hosted instance, install the dependencies
and restart the bot. Slash commands, including `/queue`, sync on startup.

## Implementation tools

- **Python 3.14** - Application runtime.
- **discord.py** - Discord gateway connection, slash commands, permissions,
  modals, select menus, buttons, roles, and channels.
- **SQLite** - Lightweight persistent configuration database.
- **aiohttp** - Twitch API requests and the X webhook HTTP server.
- **Twitch Helix API** - Live-stream monitoring.
- **python-dotenv** - Environment variable loading.
- **yt-dlp** - YouTube media metadata and audio-stream extraction.
- **FFmpeg** - Voice audio transcoding and streaming.
- **GitHub** - Source control and repository hosting.
- **GitHub Actions** - Automated deployment workflow.
- **AWS Lightsail or EC2** - Always-on hosting target.
- **systemd** - Process supervision and automatic restart on Linux.

## Project files

- `bot.py` - Discord commands, interactive workflows, Twitch polling, and the
  webhook server.
- `database.py` - SQLite initialization and data-access helpers.
- `requirements.txt` - Python dependencies.
- `.env.example` - Template for local or hosted environment variables.
- `.github/workflows/deploy.yml` - GitHub Actions AWS deployment workflow.
- `deploy/zodiac-bot.service` - Linux systemd service definition.

## External integrations

### Music troubleshooting and host setup

Spotify links supply song metadata; the bot searches YouTube for matching audio.
A YouTube verification failure can therefore affect both services. Playback
failures now appear in the channel where `/play` was used, even after queuing.

Install the updated requirements on the host. `yt-dlp[default]` includes its
JavaScript challenge scripts. Also install **Deno 2.3+** or **Node.js 22+** where
the `ubuntu` service user can execute it. The bot enables both by default; set
`YTDLP_JS_RUNTIME=node:/usr/bin/node` (using the actual executable path) in
`/etc/zodiac-bot.env` if discovery fails. See the
[yt-dlp runtime installation guide](https://github.com/yt-dlp/yt-dlp/wiki/EJS).
FFmpeg is still required separately.

For YouTube's “sign in to confirm you're not a bot” challenge, the bot supports
`YTDLP_COOKIES_FILE=/home/ubuntu/.config/zodiac/youtube-cookies.txt`. Export your
own YouTube session in Netscape cookie format using the
[yt-dlp cookie instructions](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies),
and transfer it privately to that path. The service user must be able to read
and write the file because yt-dlp saves cookie updates. On Linux, restrict the
directory to mode 700 and the file to mode 600, owned by `ubuntu`.
Cookies are login credentials: do not commit or share them. Cookie authentication
can expire and does not guarantee acceptance from a hosting IP. If access is
still denied, follow the upstream
[PO-token troubleshooting guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide);
the bot does not generate these tokens. Using an account with yt-dlp can result
in account restrictions, as noted in its cookie guide.

For Spotify, keep the client ID and secret in `/etc/zodiac-bot.env`. The parser
accepts both old and new playlist fields. Spotify's
[2026 Development Mode changes](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide)
require Premium for the app owner and restrict playlist contents to eligible
owners/collaborators. App credentials alone may return playlist metadata without
song details. To authorize your user:

1. Add `http://127.0.0.1:8888/callback` to your Spotify app's redirect URIs.
2. On your local computer, run `python spotify_auth.py` from the project directory.
   Supply the same app credentials when prompted, then open the printed URL in
   a browser on that computer and authorize the playlist owner/collaborator.
3. The helper saves `spotify-token.env` without printing the refresh token.
   Copy its `SPOTIFY_REFRESH_TOKEN` setting privately into `/etc/zodiac-bot.env`
   on the host and restart the bot. The helper file is ignored by Git and excluded
   from deployment. No callback port needs to be opened on the bot host.

This authorization only permits metadata access allowed by Spotify. It does not
enable direct Spotify audio streaming or grant access to other users' restricted
playlists. Without user authorization, supported track metadata still uses the
client credentials flow. Spotify HTTP errors now distinguish credential failures,
access restrictions, unavailable content, and rate limits.

The bot does not scrape or directly monitor X. An external X automation
provider must send approved post URLs to the authenticated webhook endpoint.
The webhook accepts X/Twitter URLs and forwards them to the configured Discord
social channel.

Send JSON such as `{"url":"https://x.com/example/status/123"}` with the
`X-Webhook-Secret` header. A nested `data.url` is also accepted. The response
reports `posted`, `failed`, and `skipped` destination counts. Invalid JSON and
URLs that are not post links return HTTP 400. Webhook retries are not deduplicated,
so the caller should avoid resending successfully delivered posts.

Twitch monitoring is built into the bot and uses the configured Twitch
application credentials to poll registered accounts.
