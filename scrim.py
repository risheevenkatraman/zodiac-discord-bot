"""Private scrim composer and timezone conversion."""

import logging
import re
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord


def scrim_timestamp(date: str, time: str, zone: str) -> int:
    try:
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", f"{date.strip()} {time.strip()}"
        ):
            raise ValueError
        local = datetime.strptime(f"{date.strip()} {time.strip()}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError(
            "Enter a valid date as YYYY-MM-DD and time as HH:MM (24-hour)."
        ) from None
    zone = zone.strip()
    offset = re.fullmatch(r"([+-])(\d{2}):(\d{2})", zone)
    if offset:
        hours, minutes = int(offset[2]), int(offset[3])
        if hours > 23 or minutes > 59:
            raise ValueError(
                "Invalid UTC offset. Use a value such as -04:00 or +05:30."
            )
        tz = timezone(
            timedelta(minutes=(hours * 60 + minutes) * (1 if offset[1] == "+" else -1))
        )
    else:
        try:
            tz = ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                "Use a timezone such as America/New_York, Europe/London, UTC, or an offset like -04:00."
            ) from None
    candidates = set()
    for fold in (0, 1):
        aware = local.replace(tzinfo=tz, fold=fold)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(tz).replace(tzinfo=None) == local:
            candidates.add(int(utc.timestamp()))
    if not candidates:
        raise ValueError(
            "That local time does not exist because the clocks move forward. Choose another time."
        )
    if len(candidates) > 1:
        raise ValueError(
            "That time occurs twice when clocks move back. Enter the intended UTC offset (for example -04:00 or -05:00) as your timezone."
        )
    return candidates.pop()


def announcement(timestamp: int, details: str) -> str:
    if not details.strip():
        raise ValueError("Enter a message describing the scrim.")
    content = f"**Scrim — <t:{timestamp}:F> (<t:{timestamp}:R>)**\n\n{details}"
    if len(content) > 2000:
        raise ValueError(
            "The message plus scrim timestamp must fit within Discord's 2,000-character limit."
        )
    return content


class ScrimModal(discord.ui.Modal, title="Scrim details"):
    def __init__(self, composer):
        super().__init__(timeout=300)
        self.composer = composer
        values = composer.values
        self.date = discord.ui.TextInput(
            label="Date (YYYY-MM-DD)", default=values[0], max_length=10
        )
        self.time = discord.ui.TextInput(
            label="Your local time (HH:MM, 24-hour)", default=values[1], max_length=5
        )
        self.zone = discord.ui.TextInput(
            label="Your timezone (Discord cannot detect it)",
            placeholder="America/New_York, Europe/London, or +05:30",
            default=values[2],
            max_length=100,
        )
        self.details = discord.ui.TextInput(
            label="Scrim message (Discord formatting supported)",
            style=discord.TextStyle.paragraph,
            default=values[3],
            max_length=1700,
        )
        for field in (self.date, self.time, self.zone, self.details):
            self.add_item(field)

    async def on_submit(self, interaction):
        view = self.composer
        if not await view.interaction_check(interaction):
            return
        view.values = tuple(
            str(field) for field in (self.date, self.time, self.zone, self.details)
        )
        try:
            timestamp = scrim_timestamp(*view.values[:3])
            content = announcement(timestamp, view.values[3])
        except ValueError as error:
            view.content = None
            view.post.disabled = True
            await interaction.response.edit_message(
                content=f"{error} Use Edit details to correct your draft.", view=view
            )
            return
        view.content = content
        view.post.disabled = view.channel_id is None
        await interaction.response.edit_message(
            content=view.preview(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ScrimView(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.values = (None, None, None, None)
        self.content = None
        self.channel_id = None
        self.message = None
        self.submitted = False

    def preview(self):
        destination = (
            f"<#{self.channel_id}>" if self.channel_id else "Select a channel below"
        )
        return f"**Scrim preview** · {destination}\nPost publishes immediately. Times display in each reader's timezone.\n\n{self.content or 'Choose Edit details to enter the date, local time, timezone, and message.'}"

    async def interaction_check(self, interaction):
        if (
            interaction.user.id != self.owner_id
            or interaction.guild_id != self.guild_id
            or not interaction.permissions.administrator
        ):
            await interaction.response.send_message(
                "Only the administrator who opened this menu can use it.",
                ephemeral=True,
            )
            return False
        if self.is_finished() or self.submitted:
            await interaction.response.send_message(
                "This scrim menu is closed. Run /scrim again.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Edit details", style=discord.ButtonStyle.primary, row=1)
    async def edit(self, interaction, button):
        await interaction.response.send_modal(ScrimModal(self))

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Select the announcement channel",
        row=0,
    )
    async def channel(self, interaction, select):
        self.channel_id = select.values[0].id
        self.post.disabled = self.content is None
        await interaction.response.edit_message(
            content=self.preview(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="Post scrim", style=discord.ButtonStyle.success, disabled=True, row=1
    )
    async def post(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        channel = interaction.guild.get_channel(self.channel_id)
        member = interaction.guild.me
        if not isinstance(channel, discord.TextChannel) or member is None:
            await interaction.response.send_message(
                "Select an available server text channel.", ephemeral=True
            )
            return
        permissions = channel.permissions_for(member)
        if not (permissions.view_channel and permissions.send_messages):
            await interaction.response.send_message(
                "I need View Channel and Send Messages in that channel.", ephemeral=True
            )
            return
        if self.content is None:
            await interaction.response.send_message(
                "Enter the scrim details first.", ephemeral=True
            )
            return
        self.submitted = (
            True  # Claim before the first await to prevent duplicate posts.
        )
        self.stop()
        await interaction.response.defer()
        try:
            message = await channel.send(
                self.content, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            await interaction.edit_original_response(
                content="Discord could not confirm the scrim post. Check the channel before starting /scrim again.",
                view=None,
            )
            return
        await interaction.edit_original_response(
            content=f"[Scrim posted in #{channel.name}]({message.jump_url}).", view=None
        )

    @discord.ui.button(label="Cancel", row=1)
    async def cancel(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content="Scrim cancelled.", view=None)

    async def on_timeout(self):
        if self.message:
            with suppress(discord.HTTPException):
                await self.message.edit(
                    content="Scrim menu expired. Run /scrim again.", view=None
                )

    async def on_error(self, interaction, error, item):
        logging.getLogger(__name__).error(
            "Scrim menu failed", exc_info=(type(error), error, error.__traceback__)
        )
        text = "Could not finish this action. Check the destination channel before trying again."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
