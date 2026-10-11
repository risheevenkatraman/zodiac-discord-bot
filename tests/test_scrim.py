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
            self.assertTrue(command.default_permissions.administrator)
            for check in command.checks:
                with self.assertRaises(app_commands.MissingPermissions):
                    check(SimpleNamespace(permissions=discord.Permissions.none()))
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

    async def test_modal_and_timeout(self):
        view = ScrimView(1, 2)
        view.channel_id = 3
        modal = ScrimModal(view)
        for field, value in zip(
            (modal.date, modal.time, modal.zone, modal.details),
            ("2026-12-10", "18:00", "America/New_York", "**Practice**\nDetails"),
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
