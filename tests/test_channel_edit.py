import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from bot import ChannelEditView


class ChannelEditTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_updates_only_changed_roles_and_preserves_other_permissions(self):
        for voice in (False, True):
            with self.subTest(voice=voice):
                roles = [Mock(id=i) for i in range(60)]
                for i, role in enumerate(roles):
                    role.name = f'Role {i}'
                member = Mock()
                channel = Mock(spec=discord.VoiceChannel if voice else discord.TextChannel)
                channel.name = 'channel'
                channel.id = 100
                channel.mention = '#channel'
                channel.permissions_for.side_effect = lambda target: (
                    discord.Permissions(manage_roles=True) if target is member else
                    discord.Permissions(view_channel=target.id == 0, connect=True)
                )
                channel.overwrites_for.side_effect = lambda role: discord.PermissionOverwrite(
                    send_messages=False, speak=False, attach_files=True,
                )
                channel.set_permissions = AsyncMock()
                guild = SimpleNamespace(me=member, fetch_channel=AsyncMock(return_value=channel),
                                        fetch_roles=AsyncMock(return_value=roles))
                view = ChannelEditView(1, Mock(), channel, roles)
                self.assertEqual(view.selected_role_ids, [0])
                self.assertNotIn(view.create, view.children)
                self.assertNotIn(view.category, view.children)
                self.assertNotIn(view.channel_kind, view.children)
                view.to_components()
                request = SimpleNamespace(guild=guild, user='Admin',
                    response=SimpleNamespace(edit_message=AsyncMock()))
                await view.next_page.callback(request)
                await view.next_page.callback(request)
                view.role_select._values = ['59']
                await view.role_select.callback(request)
                self.assertEqual(set(view.selected_role_ids), {0, 59})
                view.selected_role_ids.remove(0)
                view.begin_operation = AsyncMock(return_value=True)
                with patch('bot.publish_confirmation', new_callable=AsyncMock):
                    await view.save.callback(request)
                self.assertEqual(channel.set_permissions.await_count, 2)
                for call in channel.set_permissions.await_args_list:
                    role = call.args[0]
                    overwrite = call.kwargs['overwrite']
                    self.assertEqual(overwrite.view_channel, role.id == 59)
                    self.assertEqual(overwrite.connect, (role.id == 59) if voice else None)
                    self.assertFalse(overwrite.send_messages)
                    self.assertFalse(overwrite.speak)
                    self.assertTrue(overwrite.attach_files)
                view.stop()

    async def test_no_changes_does_not_write_overwrites(self):
        role = Mock(id=1)
        role.name = 'Role'
        channel = Mock(spec=discord.TextChannel)
        channel.name = 'channel'
        channel.permissions_for.return_value = discord.Permissions(view_channel=True, manage_roles=True)
        channel.set_permissions = AsyncMock()
        view = ChannelEditView(1, Mock(), channel, [role])
        view.begin_operation = AsyncMock(return_value=True)
        request = SimpleNamespace(guild=SimpleNamespace(me=Mock(),
            fetch_channel=AsyncMock(return_value=channel), fetch_roles=AsyncMock(return_value=[role])))
        with patch('bot.publish_confirmation', new_callable=AsyncMock):
            await view.save.callback(request)
        channel.set_permissions.assert_not_awaited()
        view.stop()
