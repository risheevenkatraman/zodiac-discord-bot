"""Private scrim composer and timezone conversion."""

import calendar
import logging
import re
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from permissions import can_administer


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
            "That time occurs twice when clocks move back. Choose UTC in the timezone dropdown and enter the corresponding UTC date and time."
        )
    return candidates.pop()


def announcement(timestamp: int, details: str, role_id: int | None = None) -> str:
    if not details.strip():
        raise ValueError("Enter a message describing the scrim.")
    content = f"**Scrim — <t:{timestamp}:F> (<t:{timestamp}:R>)**\n\n{details}"
    if role_id is not None:
        content = f"<@&{role_id}>\n\n{content}"
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
        self.time = discord.ui.TextInput(
            label="Your local time (HH:MM, 24-hour)", default=values[1], max_length=5
        )
        self.details = discord.ui.TextInput(
            label="Scrim message (Discord formatting supported)",
            style=discord.TextStyle.paragraph,
            default=values[3],
            max_length=1700,
        )
        for field in (self.time, self.details):
            self.add_item(field)

    async def on_submit(self, interaction):
        view = self.composer
        if not await view.interaction_check(interaction):
            return
        view.values = (
            view.values[0],
            str(self.time),
            view.values[2],
            str(self.details),
        )
        await view.refresh(interaction)


class ScrimView(discord.ui.View):
    def __init__(self, owner_id: int, guild_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.values = (None, None, None, None)
        self.content = None
        self.channel_id = None
        self.role_id = None
        self.message = None
        self.submitted = False
        self.month = datetime.now(timezone.utc).date().replace(day=1)
        self.main_items = list(self.children)

    def preview(self):
        destination = (
            f"<#{self.channel_id}>" if self.channel_id else "Select a channel below"
        )
        return f"Date: {self.values[0] or 'Choose a date'} | Timezone: {self.values[2] or 'Choose a timezone'}\n**Scrim preview** · {destination}\nPost publishes immediately. Times display in each reader's timezone.\n\n{self.content or 'Choose a date and timezone, then Edit details to enter the local time and message.'}"

    async def interaction_check(self, interaction):
        if (
            interaction.user.id != self.owner_id
            or interaction.guild_id != self.guild_id
            or not await can_administer(interaction)
        ):
            await interaction.response.send_message(
                "Only the authorized member who opened this menu can use it.",
                ephemeral=True,
            )
            return False
        if self.is_finished() or self.submitted:
            await interaction.response.send_message(
                "This scrim menu is closed. Run /scrim again.", ephemeral=True
            )
            return False
        return True

    async def refresh(self, interaction):
        self.content = None
        error = None
        if all(self.values):
            try:
                self.content = announcement(
                    scrim_timestamp(*self.values[:3]), self.values[3], self.role_id
                )
            except ValueError as exc:
                error = str(exc)
        self.post.disabled = self.content is None or self.channel_id is None
        await interaction.response.edit_message(
            content=self.preview() + (f"\n\n{error}" if error else ""),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.select(
        placeholder="Choose your timezone",
        row=2,
        options=[
            discord.SelectOption(label=label, value=zone)
            for label, zone in [
                ("US Eastern", "America/New_York"),
                ("US Central", "America/Chicago"),
                ("US Mountain", "America/Denver"),
                ("US Pacific", "America/Los_Angeles"),
                ("Alaska", "America/Anchorage"),
                ("Hawaii", "Pacific/Honolulu"),
                ("Arizona (no DST)", "America/Phoenix"),
                ("Atlantic / Halifax", "America/Halifax"),
                ("Brazil / S?o Paulo", "America/Sao_Paulo"),
                ("UTC", "UTC"),
                ("UK / London", "Europe/London"),
                ("Central Europe / Berlin", "Europe/Berlin"),
                ("Eastern Europe / Athens", "Europe/Athens"),
                ("South Africa", "Africa/Johannesburg"),
                ("Dubai", "Asia/Dubai"),
                ("India", "Asia/Kolkata"),
                ("Singapore / China", "Asia/Singapore"),
                ("Japan / Korea", "Asia/Tokyo"),
                ("Australia / Perth", "Australia/Perth"),
                ("Australia / Sydney", "Australia/Sydney"),
                ("New Zealand", "Pacific/Auckland"),
            ]
        ],
    )
    async def zone(self, interaction, select):
        self.values = (self.values[0], self.values[1], select.values[0], self.values[3])
        for option in select.options:
            option.default = option.value == select.values[0]
        await self.refresh(interaction)

    @discord.ui.button(label="Choose date", row=1)
    async def date(self, interaction, button):
        if self.values[0]:
            self.month = (
                datetime.strptime(self.values[0], "%Y-%m-%d").date().replace(day=1)
            )
        elif self.values[2]:
            self.month = datetime.now(ZoneInfo(self.values[2])).date().replace(day=1)
        await self.show_calendar(interaction)

    async def show_calendar(self, interaction):
        self.clear_items()
        days = calendar.monthrange(self.month.year, self.month.month)[1]
        for row, start in enumerate((1, 17)):
            select = discord.ui.Select(
                placeholder=f"Choose day ({start}?{min(start + 15, days)})",
                row=row,
                options=[
                    discord.SelectOption(
                        label=self.month.replace(day=day).strftime("%a, %B %d"),
                        value=self.month.replace(day=day).isoformat(),
                        default=self.values[0]
                        == self.month.replace(day=day).isoformat(),
                    )
                    for day in range(start, min(start + 16, days + 1))
                ],
            )

            async def choose(request, control=select):
                self.values = (
                    control.values[0],
                    self.values[1],
                    self.values[2],
                    self.values[3],
                )
                self.restore_menu()
                await self.refresh(request)

            select.callback = choose
            self.add_item(select)
        for label, delta in (("Previous month", -1), ("Next month", 1), ("Back", 0)):
            button = discord.ui.Button(
                label=label,
                row=2,
                disabled=(
                    delta == -1 and self.month.year == 1 and self.month.month == 1
                )
                or (delta == 1 and self.month.year == 9999 and self.month.month == 12),
            )

            async def navigate(request, change=delta):
                if change == 0:
                    self.restore_menu()
                    await self.refresh(request)
                    return
                index = self.month.year * 12 + self.month.month - 1 + change
                self.month = self.month.replace(year=index // 12, month=index % 12 + 1)
                await self.show_calendar(request)

            button.callback = navigate
            self.add_item(button)
        await interaction.response.edit_message(
            content=f"**Choose a scrim date ? {self.month:%B %Y}**\n```\n{calendar.month(self.month.year, self.month.month)}```",
            view=self,
        )

    def restore_menu(self):
        self.clear_items()
        for item in self.main_items:
            self.add_item(item)

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

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Select a role to ping (optional)",
        min_values=0,
        max_values=1,
        row=3,
    )
    async def ping_role(self, interaction, select):
        self.role_id = select.values[0].id if select.values else None
        select.default_values = [discord.Object(id=self.role_id)] if self.role_id else []
        await self.refresh(interaction)

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
        if self.role_id is not None:
            role = interaction.guild.get_role(self.role_id)
            if role is None or (not role.mentionable and not permissions.mention_everyone):
                await interaction.response.send_message(
                    "Choose an existing mentionable role, or grant me Mention Everyone in that channel to ping the selected role.",
                    ephemeral=True,
                )
                return
        self.submitted = (
            True  # Claim before the first await to prevent duplicate posts.
        )
        self.stop()
        await interaction.response.defer()
        try:
            message = await channel.send(
                self.content,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=[discord.Object(id=self.role_id)] if self.role_id else False,
                    replied_user=False,
                ),
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
