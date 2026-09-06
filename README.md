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
- `/create_channel` - Open a GUI to choose a category, select roles, and create a
  private text channel with the appropriate permission overwrites.
- `/delete_role` - Delete a selected server role.
- `/delete_channel` - Delete a selected text channel.

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

Spotify links are resolved through the Spotify Web API and searched on YouTube
for playback. Configure `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` for
Spotify support. YouTube-only playback does not need Spotify credentials.
FFmpeg must be installed on the deployment host and `FFMPEG_PATH` must point to
it when it is not on `PATH`.

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
- Twitch Helix polling with duplicate prevention during each bot process.
- Authenticated X webhook endpoint at `POST /webhooks/x`.
- X post URL forwarding to the configured social channel with optional role
  mentions.
- SQLite persistence for configured channels, roles, and Twitch accounts.
- Environment-based configuration for tokens, secrets, polling, and database
  paths.
- Optional HTTP webhook server for external X automation integrations.

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

The bot does not scrape or directly monitor X. An external X automation
provider must send approved post URLs to the authenticated webhook endpoint.
The webhook accepts X/Twitter URLs and forwards them to the configured Discord
social channel.

Twitch monitoring is built into the bot and uses the configured Twitch
application credentials to poll registered accounts.
