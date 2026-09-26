import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from PIL import Image

from bot import ZodiacBot
from welcome_card import render_join_card


class JoinCardTests(unittest.IsolatedAsyncioTestCase):
    def test_renderer_handles_missing_bad_avatar_and_long_names(self):
        for name, avatar in [('ZodiacMember', None), ('LongName' * 20, b'invalid'), ('星の選手 🎮', None)]:
            with self.subTest(name=name):
                result = render_join_card(name, 1221, avatar)
                with Image.open(result) as image:
                    self.assertEqual(image.size, (1500, 500))
                    self.assertEqual(image.format, 'PNG')

    async def test_system_join_posts_once_after_message(self):
        avatar = Mock()
        avatar.with_size.return_value.with_format.return_value.read = AsyncMock(return_value=b'avatar')
        channel = SimpleNamespace(id=5, permissions_for=Mock(return_value=discord.Permissions(
            view_channel=True, send_messages=True, attach_files=True)), send=AsyncMock())
        guild = SimpleNamespace(id=1, me=Mock(), system_channel=channel, member_count=1221)
        message = SimpleNamespace(id=42, type=discord.MessageType.new_member, guild=guild, channel=channel,
                                  author=SimpleNamespace(display_avatar=avatar, display_name='NewMember', id=2))
        bot = SimpleNamespace(join_card_lock=asyncio.Lock(), join_card_messages=set(), process_commands=AsyncMock())
        from io import BytesIO
        with patch('bot.render_join_card', return_value=BytesIO(b'png')) as render:
            await asyncio.gather(ZodiacBot.on_message(bot, message), ZodiacBot.on_message(bot, message))
            render.assert_called_once_with('NewMember', 1221, b'avatar')
        channel.send.assert_awaited_once()
        self.assertFalse(channel.send.await_args.kwargs['allowed_mentions'].everyone)
        bot.process_commands.assert_not_awaited()

    async def test_ordinary_messages_and_missing_permissions_do_not_post(self):
        bot = SimpleNamespace(join_card_lock=asyncio.Lock(), join_card_messages=set(), process_commands=AsyncMock())
        message = SimpleNamespace(type=discord.MessageType.default)
        await ZodiacBot.on_message(bot, message)
        bot.process_commands.assert_awaited_once_with(message)
        channel = SimpleNamespace(id=5, permissions_for=Mock(return_value=discord.Permissions.none()), send=AsyncMock())
        message = SimpleNamespace(id=42, type=discord.MessageType.new_member, channel=channel,
            guild=SimpleNamespace(id=1, me=Mock(), system_channel=channel, member_count=5))
        with self.assertLogs('zodiac-bot', level='WARNING'):
            await ZodiacBot.on_message(bot, message)
        channel.send.assert_not_awaited()
