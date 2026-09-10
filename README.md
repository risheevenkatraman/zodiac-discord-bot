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
- Automatic monitoring of @zodiacsesport through the X API, with new post links
  forwarded to the configured social channel and optional role mentions.
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

Twitch monitoring is built into the bot and uses the configured Twitch
application credentials to poll registered accounts.
