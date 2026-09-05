from __future__ import annotations

import asyncio
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import Database

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("zodiac-bot")


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

    @classmethod
    def from_env(cls) -> Settings:
        twitch_id = os.getenv("TWITCH_CLIENT_ID")
        twitch_secret = os.getenv("TWITCH_CLIENT_SECRET")
        if bool(twitch_id) != bool(twitch_secret):
            raise RuntimeError(
                "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET must be configured together"
            )
        return cls(
            discord_token=required_env("DISCORD_TOKEN"),
            webhook_secret=required_env("WEBHOOK_SECRET"),
            webhook_host=os.getenv("WEBHOOK_HOST", "127.0.0.1"),
            webhook_port=int(os.getenv("WEBHOOK_PORT", "8080")),
            twitch_client_id=twitch_id,
            twitch_client_secret=twitch_secret,
            twitch_poll_seconds=max(30, int(os.getenv("TWITCH_POLL_SECONDS", "60"))),
        )


class ZodiacBot(commands.Bot):
    def __init__(self, settings: Settings, database: Database) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.database = database
        self.http_session: aiohttp.ClientSession | None = None
        self.twitch_token: str | None = None
        self.seen_streams: set[tuple[int, str]] = set()
        self.webhook_runner: web.AppRunner | None = None

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession()
        await self.tree.sync()
        self.twitch_poll_loop.start()

    async def close(self) -> None:
        self.twitch_poll_loop.cancel()
        if self.webhook_runner:
            await self.webhook_runner.cleanup()
        if self.http_session:
            await self.http_session.close()
        await super().close()

    async def on_ready(self) -> None:
        LOGGER.info(
            "Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown"
        )

    async def post_to_configured_channel(
        self, guild_id: int, content: str, *, kind: str, role_id: int | None = None
    ) -> None:
        channel_id = await self.database.get_channel(guild_id, kind)
        if not channel_id:
            LOGGER.warning("No configured channel for guild %s", guild_id)
            return
        channel = self.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            LOGGER.warning("Configured channel %s is unavailable", channel_id)
            return
        mention = f"<@&{role_id}> " if role_id else ""
        await channel.send(
            f"{mention}{content}",
            allowed_mentions=discord.AllowedMentions(roles=bool(role_id)),
        )

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
        async with self.http_session.get(
            "https://api.twitch.tv/helix/streams",
            params=[("user_login", account["username"]) for account in accounts],
            headers=headers,
        ) as response:
            if response.status == 401:
                self.twitch_token = None
                return
            response.raise_for_status()
            streams: list[dict[str, Any]] = (await response.json()).get("data", [])
        account_map = {account["username"].lower(): account for account in accounts}
        for stream in streams:
            account = account_map.get(stream["user_login"].lower())
            if not account:
                continue
            key = (account["guild_id"], stream["id"])
            if key in self.seen_streams:
                continue
            self.seen_streams.add(key)
            role_id = await self.database.get_role(account["guild_id"], "social")
            await self.post_to_configured_channel(
                account["guild_id"],
                f"**{stream['user_name']} is live:** https://twitch.tv/{stream['user_login']}",
                kind="twitch",
                role_id=role_id,
            )

    @twitch_poll_loop.before_loop
    async def before_twitch_poll_loop(self) -> None:
        await self.wait_until_ready()


def administrator_only() -> Any:
    def decorator(command: Any) -> Any:
        command = app_commands.default_permissions(administrator=True)(command)
        return app_commands.checks.has_permissions(administrator=True)(command)

    return decorator


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


class AccessSetupModal(discord.ui.Modal, title="Create role"):
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
    permissions = discord.ui.TextInput(
        label="Role permissions (comma-separated)",
        placeholder="view_channel, send_messages, read_message_history",
        max_length=500,
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
            permissions = parse_role_permissions(str(self.permissions))
            color = parse_role_color(str(self.role_color))
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        view = RoleSetupView(
            owner_id=self.owner_id,
            bot=self.bot,
            role_name=str(self.role_name).strip(),
            permissions=permissions,
            color=color,
        )
        await interaction.response.send_message(
            "Review the role details, then click **Create**. This command creates "
            "only the role; use `/create_channel` separately for channels.",
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()


class RoleSetupView(discord.ui.View):
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

        await interaction.response.defer(ephemeral=True)
        try:
            role = await guild.create_role(
                name=self.role_name,
                permissions=self.permissions,
                colour=self.color,
                reason=f"Access setup by {interaction.user}",
            )
        except discord.HTTPException:
            LOGGER.exception("Failed to create role in guild %s", guild.id)
            await interaction.followup.send(
                "Discord rejected the role creation. Check my role position and "
                "permissions, then try again.",
                ephemeral=True,
            )
            return

        self.disable_all_items()
        await interaction.followup.send(
            f"Created {role.mention} with the requested permissions and color.",
            ephemeral=True,
        )
        if self.message:
            await self.message.edit(view=self)

    async def on_timeout(self) -> None:
        self.disable_all_items()
        if self.message:
            await self.message.edit(view=self)


class ChannelSetupModal(discord.ui.Modal, title="Create private channel"):
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
            roles=interaction.guild.roles,
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
            min_values=1,
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
        ]

    def refresh_options(self) -> None:
        self.options = self.build_options()
        self.max_values = max(1, min(25, len(self.setup_view.role_pages[self.setup_view.role_page])))
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


class ChannelSetupView(discord.ui.View):
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
        await interaction.response.send_message(
            "Category selected.", ephemeral=True
        )

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

        overwrites: dict[discord.Role, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False)
        }
        for role in selected_roles:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )

        await interaction.response.defer(ephemeral=True)
        try:
            channel = await guild.create_text_channel(
                self.channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Channel setup by {interaction.user}",
            )
        except discord.HTTPException:
            LOGGER.exception("Failed to create channel in guild %s", guild.id)
            await interaction.followup.send(
                "Discord rejected the channel creation. Check my channel "
                "permissions and try again.",
                ephemeral=True,
            )
            return

        self.disable_all_items()
        await interaction.followup.send(
            f"Created {channel.mention} in {category.name} with access for the "
            "selected roles.",
            ephemeral=True,
        )
        if self.message:
            await self.message.edit(view=self)

    async def on_timeout(self) -> None:
        self.disable_all_items()
        if self.message:
            await self.message.edit(view=self)


def register_commands(bot: ZodiacBot) -> None:
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
        await interaction.response.send_message(
            "Zodiac notification channels and role saved.", ephemeral=True
        )

    @bot.tree.command(
        name="create_access",
        description="Interactively create a permissioned role and private channel.",
    )
    @administrator_only()
    @app_commands.guild_only()
    async def create_access(interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            AccessSetupModal(interaction.user.id, bot)
        )

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
        if role == guild.default_role:
            await interaction.response.send_message(
                "The @everyone role cannot be deleted.", ephemeral=True
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

        try:
            await role.delete(reason=f"Deleted by {interaction.user}")
        except discord.HTTPException:
            LOGGER.exception("Failed to delete role %s in guild %s", role.id, guild.id)
            await interaction.response.send_message(
                "Discord rejected the role deletion. Check my permissions and "
                "role hierarchy.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(f"Deleted the `{role.name}` role.")

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

        try:
            await channel.delete(reason=f"Deleted by {interaction.user}")
        except discord.HTTPException:
            LOGGER.exception(
                "Failed to delete channel %s in guild %s", channel.id, guild.id
            )
            await interaction.response.send_message(
                "Discord rejected the channel deletion. Check my permissions.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Deleted the `#{channel.name}` channel.", ephemeral=True
        )

    @bot.tree.command(
        name="add_twitch",
        description="Register a Twitch account for live notifications.",
    )
    @app_commands.describe(username="Twitch login name")
    @administrator_only()
    async def add_twitch(interaction: discord.Interaction, username: str) -> None:
        await bot.database.add_twitch_account(
            interaction.guild_id, username.strip().lower()
        )
        await interaction.response.send_message(
            f"Registered Twitch account `{username}`.", ephemeral=True
        )

    @bot.tree.command(
        name="remove_twitch", description="Stop notifying for a Twitch account."
    )
    @administrator_only()
    async def remove_twitch(interaction: discord.Interaction, username: str) -> None:
        removed = await bot.database.remove_twitch_account(
            interaction.guild_id, username.strip().lower()
        )
        message = (
            f"Removed `{username}`." if removed else f"`{username}` was not registered."
        )
        await interaction.response.send_message(message, ephemeral=True)

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
        await interaction.response.send_message(message, ephemeral=True)

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
        await interaction.followup.send(
            f"Deleted {len(deleted)} messages.", ephemeral=True
        )

    @bot.tree.command(name="timeout", description="Timeout a member.")
    @app_commands.describe(member="Member to timeout", minutes="Duration in minutes")
    @administrator_only()
    async def timeout(
        interaction: discord.Interaction,
        member: discord.Member,
        minutes: app_commands.Range[int, 1, 40320],
    ) -> None:
        await member.timeout(
            discord.utils.utcnow() + timedelta(minutes=minutes),
            reason=f"Moderated by {interaction.user}",
        )
        await interaction.response.send_message(
            f"Timed out {member.mention} for {minutes} minutes."
        )

    @bot.tree.command(name="kick", description="Kick a member.")
    @administrator_only()
    async def kick(
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
    ) -> None:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"Kicked {member} ({reason}).")

    @bot.tree.command(name="ban", description="Ban a member.")
    @administrator_only()
    async def ban(
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided",
    ) -> None:
        await member.ban(reason=reason)
        await interaction.response.send_message(f"Banned {member} ({reason}).")

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "You do not have permission to use this command."
        else:
            LOGGER.exception("Slash command failed", exc_info=error)
            message = "The command failed. Check the bot logs for details."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def webhook_handler(request: web.Request) -> web.Response:
    bot: ZodiacBot = request.app["bot"]
    provided = request.headers.get("X-Webhook-Secret", "")
    if not secrets.compare_digest(provided, bot.settings.webhook_secret):
        raise web.HTTPUnauthorized(text="Invalid webhook secret")
    payload = await request.json()
    nested_payload = payload.get("data", {}) if isinstance(payload, dict) else {}
    url = payload.get("url") if isinstance(payload, dict) else None
    if not url and isinstance(nested_payload, dict):
        url = nested_payload.get("url")
    if not isinstance(url, str) or not url.startswith(
        ("https://x.com/", "https://twitter.com/")
    ):
        raise web.HTTPBadRequest(text="Payload must contain an X post URL in `url`")
    guild_ids = await bot.database.list_guild_ids()
    for guild_id in guild_ids:
        await bot.post_to_configured_channel(
            guild_id,
            f"New post from Zodiac eSports: {url}",
            kind="social",
            role_id=await bot.database.get_role(guild_id, "social"),
        )
    return web.json_response({"posted": len(guild_ids)})


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
    await asyncio.gather(bot.start(settings.discord_token), run_webhook_server(bot))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
