import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from bot import ChannelSetupView


class ChannelSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_roles_are_reachable_and_selections_survive_paging(self):
        roles = [SimpleNamespace(id=i, name=f'Role {i}') for i in range(60)]
        view = ChannelSetupView(1, Mock(), 'Channel', roles)
        request = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
        seen = []
        for page in range(3):
            seen.extend(int(option.value) for option in view.role_select.options)
            view.role_select._values = [str(page * 25)]
            await view.role_select.callback(request)
            if page < 2:
                await view.next_page.callback(request)
        self.assertEqual(seen, list(range(60)))
        self.assertEqual(view.selected_role_ids, [0, 25, 50])
        await view.previous_page.callback(request)
        self.assertTrue(view.role_select.options[0].default)
        view.role_select._values = []
        await view.role_select.callback(request)
        self.assertEqual(view.selected_role_ids, [0, 50])
        view.stop()

    async def test_text_and_voice_creation_permissions(self):
        for kind in ('text', 'voice'):
            for everyone_selected in (False, True):
                with self.subTest(kind=kind, everyone=everyone_selected):
                    everyone, role, member = Mock(), Mock(), Mock()
                    member.guild_permissions = discord.Permissions(manage_channels=True)
                    category = Mock(spec=discord.CategoryChannel)
                    category.name = 'Category'
                    guild = SimpleNamespace(
                        me=member, default_role=everyone,
                        get_channel=Mock(return_value=category),
                        get_role=Mock(return_value=everyone if everyone_selected else role),
                        create_text_channel=AsyncMock(), create_voice_channel=AsyncMock(),
                    )
                    request = SimpleNamespace(guild=guild, user='Admin')
                    view = ChannelSetupView(1, Mock(), 'Team Lounge', [])
                    view.category_id = 10
                    view.selected_role_ids = [20]
                    view.channel_kind._values = [kind]
                    request.response = SimpleNamespace(edit_message=AsyncMock())
                    await view.channel_kind.callback(request)
                    view.begin_operation = AsyncMock(return_value=True)
                    with patch('bot.publish_confirmation', new_callable=AsyncMock):
                        await view.create.callback(request)
                    method = guild.create_voice_channel if kind == 'voice' else guild.create_text_channel
                    other = guild.create_text_channel if kind == 'voice' else guild.create_voice_channel
                    method.assert_awaited_once()
                    other.assert_not_awaited()
                    args = method.await_args
                    self.assertEqual(args.args[0], 'Team Lounge' if kind == 'voice' else 'team-lounge')
                    overwrites = args.kwargs['overwrites']
                    self.assertEqual(overwrites[everyone].view_channel, everyone_selected)
                    allowed = everyone if everyone_selected else role
                    self.assertTrue(overwrites[allowed].view_channel)
                    if kind == 'voice':
                        self.assertTrue(overwrites[allowed].connect)
                        self.assertTrue(overwrites[allowed].speak)
                        self.assertTrue(overwrites[member].connect)
