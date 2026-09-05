# Zodiac eSports Community Discord Bot

This bot provides:

- Moderation slash commands: `/clear`, `/timeout`, `/kick`, and `/ban`.
- `/setup` to configure the social-post channel, Twitch channel, and social role.
- `/create_access` to interactively create a role with selected permissions and a
  private text channel accessible to selected server roles.
- `/create_channel` to interactively create a private channel in a selected
  category and grant access to selected server roles.
- `/add_twitch` and `/remove_twitch` to manage Twitch accounts.
- An authenticated `POST /webhooks/x` endpoint that reposts an X post URL and pings the configured social role.
- Twitch Helix polling that posts each newly observed live stream once per bot process.

## Setup

1. Create a Discord application and bot at the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable the **Server Members Intent**, invite the bot with `bot` and `applications.commands` scopes, and grant only the permissions it needs.
3. Copy `.env.example` to `.env` and set `DISCORD_TOKEN` and a strong `WEBHOOK_SECRET`.
4. For Twitch notifications, create an application at [dev.twitch.tv](https://dev.twitch.tv/), then set both Twitch credentials.
5. Install dependencies and start the bot:

   ```powershell
   C:/Python314/python.exe -m pip install -r requirements.txt
   C:/Python314/python.exe bot.py
   ```

6. In Discord, run `/setup`, then register accounts with `/add_twitch`.

## Creating permissioned spaces

Members with **Manage Server** can run `/create_access`. The interactive flow:

1. Enter the new role name, channel name, and comma-separated Discord permission
   names (for example, `view_channel, send_messages, read_message_history`).
2. Select one or more existing roles that should access the private channel.
3. Confirm creation.

The bot creates the role and channel, denies `@everyone` access, and grants the
selected roles plus the new role access. The bot needs **Manage Roles** and
**Manage Channels**, and its highest role must be above roles it creates or edits.

To create only a channel, run `/create_channel`. Enter the channel name, choose
its category, select the roles that should have access, and confirm creation.
The bot denies `@everyone` access and grants the selected roles permission to
view, read, and send messages.

## X webhook payload

Send the webhook URL from your X integration to `POST /webhooks/x` with:

```http
X-Webhook-Secret: your-secret
Content-Type: application/json
```

```json
{"url":"https://x.com/ZodiacEsports/status/1234567890"}
```

The endpoint intentionally accepts only X/Twitter URLs. Put it behind HTTPS and a reverse proxy when exposing it to the internet. The bot does not scrape X; use an X-compatible automation/webhook provider to deliver the post URL.

## AWS deployment with GitHub

For an always-on deployment with persistent SQLite storage, use an Ubuntu AWS
Lightsail instance (or EC2 instance) with a static IP. GitHub stores the source
and GitHub Actions deploys each push to `main`; the bot runs under `systemd`.
The instance's EBS/root disk (or an attached Lightsail disk) persists `zodiac.db`.

### AWS setup

1. Create an Ubuntu instance in Lightsail or EC2. Use a static IP/Elastic IP.
2. Allow inbound SSH (TCP 22) only from your own IP. No public inbound port is
   needed for Discord gateway operation.
3. Connect as `ubuntu`, install Python, and create the deployment directory:

   ```bash
   sudo apt update
   sudo apt install -y python3-venv python3-pip
   sudo mkdir -p /opt/zodiac-discord-bot
   sudo chown -R ubuntu:ubuntu /opt/zodiac-discord-bot
   ```

4. Create `/etc/zodiac-bot.env` on the instance. Set `DISCORD_TOKEN`,
   `WEBHOOK_SECRET`, `DATABASE_PATH=/opt/zodiac-discord-bot/zodiac.db`, and any
   Twitch variables. Keep this file out of GitHub and run:

   ```bash
   sudo chmod 600 /etc/zodiac-bot.env
   ```

5. Copy `deploy/zodiac-bot.service` into the repository and commit it. The
   included `.github/workflows/deploy.yml` deploys to the instance on pushes to
   `main`.
6. Add these GitHub repository secrets:
   `AWS_HOST` (static IP or DNS), `AWS_USER` (`ubuntu`), `AWS_SSH_KEY` (private
   deployment key), and `AWS_KNOWN_HOSTS` (the output of
   `ssh-keyscan -H YOUR_STATIC_IP`).
7. Push to `main`. The workflow uploads source, installs dependencies, installs
   the systemd service, and restarts the bot.
8. Check status with:

   ```bash
   sudo systemctl status zodiac-bot
   journalctl -u zodiac-bot -f
   ```

Do not add the Discord token, `/etc/zodiac-bot.env`, private SSH keys, or
`zodiac.db` to the repository. Back up the persistent disk/database regularly.
