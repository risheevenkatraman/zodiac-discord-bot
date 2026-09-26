import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from discord import app_commands

from bot import ZodiacBot, build_welcome_message, register_commands


class WelcomeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = ZodiacBot(Mock(), SimpleNamespace(close=AsyncMock()))
        register_commands(self.bot)
        self.command = self.bot.tree.get_command('welcome')

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_message_and_all_five_social_buttons(self):
        embed, view = build_welcome_message([(f'Social {i}', f'https://example.com/{i}') for i in range(5)])
        self.assertIn('Zodiac eSports Community Discord', embed.description)
        self.assertEqual(view.children[0].url, 'https://www.zodiacgg.com')
        self.assertEqual(len(view.children), 6)
        self.assertEqual(len(embed.fields), 2)
        self.assertTrue(all(button.style == discord.ButtonStyle.link for button in view.children))
        view.stop()
        embed, view = build_welcome_message([(None, None)])
        self.assertEqual(len(embed.fields), 1)
        self.assertEqual(len(view.children), 1)
        view.stop()

    async def test_invalid_socials(self):
        for pair in [('X', None), (None, 'https://example.com'), (' ', 'https://example.com'),
                     ('X', 'javascript:alert(1)'), ('X', 'https://'), ('X', 'https://[bad'),
                     ('X', 'https://example.com/has space')]:
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                build_welcome_message([pair])

    async def test_administrator_check(self):
        self.assertTrue(self.command.default_permissions.administrator)
        self.assertTrue(self.command.guild_only)
        for check in self.command.checks:
            with self.assertRaises(app_commands.MissingPermissions):
                check(SimpleNamespace(permissions=discord.Permissions.none()))
            self.assertTrue(check(SimpleNamespace(permissions=discord.Permissions(administrator=True))))

    async def test_post_and_missing_permissions(self):
        guild = SimpleNamespace(id=1, me=Mock())
        channel = SimpleNamespace(
            guild=guild, mention='#welcome',
            permissions_for=Mock(return_value=discord.Permissions(view_channel=True, send_messages=True, embed_links=True)),
            send=AsyncMock(return_value=SimpleNamespace(jump_url='https://discord.com/channels/1/2/3')),
        )
        request = SimpleNamespace(
            guild=guild, response=SimpleNamespace(defer=AsyncMock(), type=None,
                is_done=Mock(return_value=False), send_message=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        await self.command.callback(request, channel, 'X', 'https://example.com/zodiac')
        channel.send.assert_awaited_once()
        self.assertFalse(channel.send.await_args.kwargs['allowed_mentions'].everyone)
        request.edit_original_response.assert_awaited_once()
        channel.send.reset_mock()
        channel.permissions_for.return_value = discord.Permissions.none()
        await self.command.callback(request, channel)
        channel.send.assert_not_awaited()
        request.response.send_message.assert_awaited_once()
