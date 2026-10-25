import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from discord import app_commands

from bot import ZodiacBot, register_commands
from scrim import ScrimModal, ScrimView, announcement, scrim_timestamp


class TimeTests(unittest.TestCase):
    def test_conversion_and_dst(self):
        for date, hour in [("2026-07-10", 22), ("2026-12-10", 23)]:
            stamp = scrim_timestamp(date, "18:00", "America/New_York")
            self.assertEqual(datetime.fromtimestamp(stamp, timezone.utc).hour, hour)
        self.assertEqual(
            scrim_timestamp("2026-07-10", "18:00", "+05:30"),
            scrim_timestamp("2026-07-10", "12:30", "UTC"),
        )

    def test_invalid_and_clock_changes(self):
        for args in [
            ("2026-02-30", "12:00", "UTC"),
            ("2026-01-01", "25:00", "UTC"),
            ("2026-01-01", "12:00", "fake/zone"),
            ("2026-01-01", "12:00", "+25:00"),
            ("2026-03-08", "02:30", "America/New_York"),
            ("2026-11-01", "01:30", "America/New_York"),
        ]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                scrim_timestamp(*args)
        self.assertEqual(
            scrim_timestamp("2026-11-01", "01:30", "-05:00")
            - scrim_timestamp("2026-11-01", "01:30", "-04:00"),
            3600,
        )

    def test_message_preserves_formatting(self):
        details = "**Team A**\n> Bring your roster\nhttps://example.com"
        self.assertEqual(
            announcement(123, details),
            f"**Scrim — <t:123:F> (<t:123:R>)**\n\n{details}",
        )
        for details in [" ", "x" * 2000]:
            with self.assertRaises(ValueError):
                announcement(123, details)


class ScrimTests(unittest.IsolatedAsyncioTestCase):
    def request(self):
        channel = Mock(spec=discord.TextChannel)
        channel.name = "scrims"
        channel.permissions_for.return_value = discord.Permissions(
            view_channel=True, send_messages=True
        )
        channel.send = AsyncMock(
            return_value=SimpleNamespace(jump_url="https://discord.com/message")
        )
        return SimpleNamespace(
            user=SimpleNamespace(id=1),
            guild_id=2,
            permissions=discord.Permissions(administrator=True),
            guild=SimpleNamespace(me=Mock(), get_channel=Mock(return_value=channel)),
            response=SimpleNamespace(
                send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock()
            ),
            edit_original_response=AsyncMock(),
        )

    async def test_command_permissions(self):
        bot = ZodiacBot(Mock(), SimpleNamespace(close=AsyncMock()))
        try:
            register_commands(bot)
            command = bot.tree.get_command("scrim")
            self.assertTrue(command.guild_only)
            self.assertIsNone(command.default_permissions)
            for check in command.checks:
                with self.assertRaises(app_commands.MissingPermissions):
                    await check(SimpleNamespace(permissions=discord.Permissions.none()))
        finally:
            await bot.close()

    async def test_post_and_duplicate_protection(self):
        request = self.request()
        view = ScrimView(1, 2)
        view.channel_id = 3
        view.content = announcement(123, "Hello")
        await view.post.callback(request)
        await view.post.callback(request)
        channel = request.guild.get_channel.return_value
        channel.send.assert_awaited_once()
        self.assertEqual(channel.send.await_args.args, (view.content,))
        self.assertFalse(channel.send.await_args.kwargs["allowed_mentions"].everyone)

    async def test_permissions_owner_and_closed_menu(self):
        view = ScrimView(1, 2)
        request = self.request()
        request.user.id = 9
        self.assertFalse(await view.interaction_check(request))
        request.user.id = 1
        request.permissions = discord.Permissions.none()
        self.assertFalse(await view.interaction_check(request))
        request.permissions = discord.Permissions(administrator=True)
        request.guild.get_channel.return_value.permissions_for.return_value = (
            discord.Permissions.none()
        )
        await view.post.callback(request)
        request.guild.get_channel.return_value.send.assert_not_awaited()
        self.assertFalse(view.is_finished())
        await view.cancel.callback(request)
        self.assertFalse(await view.interaction_check(request))

    async def test_role_selection_preview_clear_and_post(self):
        view = ScrimView(1, 2)
        view.channel_id = 3
        view.values = ("2026-12-10", "18:00", "UTC", "Practice @everyone <@&99> <@42>")
        request = self.request()
        request.guild.get_role = Mock(return_value=SimpleNamespace(mentionable=True))
        view.ping_role._values = [discord.Object(id=7)]
        await view.ping_role.callback(request)
        self.assertTrue(view.content.startswith("<@&7>\n\n"))
        self.assertEqual(view.ping_role.default_values[0].id, 7)
        self.assertFalse(request.response.edit_message.await_args.kwargs["allowed_mentions"].roles)
        view.ping_role._values = []
        await view.ping_role.callback(request)
        self.assertIsNone(view.role_id)
        self.assertNotIn("<@&7>", view.content)
        view.ping_role._values = [discord.Object(id=7)]
        await view.ping_role.callback(request)
        await view.post.callback(request)
        sent = request.guild.get_channel.return_value.send.await_args
        self.assertTrue(sent.args[0].startswith("<@&7>\n\n"))
        mentions = sent.kwargs["allowed_mentions"].to_dict()
        self.assertEqual(mentions["roles"], [7])
        self.assertEqual(mentions["parse"], [])

    async def test_unpingable_role_keeps_composer_open(self):
        view = ScrimView(1, 2)
        view.channel_id = 3
        view.role_id = 7
        view.content = announcement(123, "Practice", 7)
        request = self.request()
        request.guild.get_role = Mock(return_value=SimpleNamespace(mentionable=False))
        await view.post.callback(request)
        channel = request.guild.get_channel.return_value
        channel.send.assert_not_awaited()
        self.assertFalse(view.is_finished())
        channel.permissions_for.return_value.mention_everyone = True
        await view.post.callback(request)
        channel.send.assert_awaited_once()

    async def test_modal_and_timeout(self):
        view = ScrimView(1, 2)
        view.channel_id = 3
        view.values = ("2026-12-10", None, "America/New_York", None)
        modal = ScrimModal(view)
        for field, value in zip(
            (modal.time, modal.details),
            ("18:00", "**Practice**\nDetails"),
        ):
            field._value = value
        request = self.request()
        await modal.on_submit(request)
        self.assertIn("**Practice**\nDetails", view.content)
        self.assertFalse(view.post.disabled)
        request.response.edit_message.assert_awaited_once()
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        view.message.edit.assert_awaited_once()
        view.stop()
        modal.stop()


class SelectorTests(unittest.IsolatedAsyncioTestCase):
    request = ScrimTests.request

    async def test_calendar_leap_year_and_navigation(self):
        view = ScrimView(1, 2)
        view.month = datetime(2028, 2, 1).date()
        request = self.request()
        await view.show_calendar(request)
        selects = [
            item for item in view.children if isinstance(item, discord.ui.Select)
        ]
        self.assertEqual(sum(len(item.options) for item in selects), 29)
        selects[1]._values = ["2028-02-29"]
        await selects[1].callback(request)
        self.assertEqual(view.values[0], "2028-02-29")
        self.assertIn(view.post, view.children)
        view.month = datetime(2026, 12, 1).date()
        await view.show_calendar(request)
        await next(
            item
            for item in view.children
            if getattr(item, "label", None) == "Next month"
        ).callback(request)
        self.assertEqual(view.month.isoformat(), "2027-01-01")
        view.stop()

    async def test_timezone_change_invalidates_preview(self):
        view = ScrimView(1, 2)
        view.channel_id = 3
        view.values = ("2026-03-08", "02:30", "UTC", "Practice")
        request = self.request()
        await view.refresh(request)
        self.assertFalse(view.post.disabled)
        view.zone._values = ["America/New_York"]
        await view.zone.callback(request)
        self.assertIsNone(view.content)
        self.assertTrue(view.post.disabled)
        view.stop()
