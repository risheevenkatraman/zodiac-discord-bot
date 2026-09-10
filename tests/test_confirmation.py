import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import discord

from bot import (
    ChannelSetupView,
    PermissionSetupView,
    RoleEditView,
    RoleSetupView,
    SetupView,
    publish_confirmation,
)


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self, deferred=True):
        response = SimpleNamespace(
            type=discord.InteractionResponseType.deferred_channel_message if deferred else None,
            is_done=Mock(return_value=deferred),
            send_message=AsyncMock(),
        )
        return SimpleNamespace(
            response=response,
            edit_original_response=AsyncMock(),
            followup=SimpleNamespace(send=AsyncMock()),
            delete_original_response=AsyncMock(),
        )

    async def test_deferred_confirmation_resolves_before_public_followup(self):
        interaction = self.interaction()
        sequence = Mock()
        sequence.attach_mock(interaction.edit_original_response, 'resolve')
        sequence.attach_mock(interaction.followup.send, 'publish')
        sequence.attach_mock(interaction.delete_original_response, 'dismiss')

        await publish_confirmation(interaction, 'Created role.', dismiss_original=True)

        self.assertEqual(sequence.mock_calls, [
            call.resolve(content='Created role.', view=None),
            call.publish('Created role.', ephemeral=False, wait=True),
            call.dismiss(),
        ])

    async def test_fresh_confirmation_is_not_deleted(self):
        interaction = self.interaction(deferred=False)
        await publish_confirmation(interaction, 'Done.', dismiss_original=True)
        interaction.response.send_message.assert_awaited_once_with('Done.')
        interaction.delete_original_response.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()

    async def test_failed_public_send_keeps_resolved_result(self):
        interaction = self.interaction()
        interaction.followup.send.side_effect = RuntimeError('send failed')
        with self.assertRaises(RuntimeError):
            await publish_confirmation(interaction, 'Done.', dismiss_original=True)
        interaction.edit_original_response.assert_awaited_once_with(content='Done.', view=None)
        interaction.delete_original_response.assert_not_awaited()

    async def test_setup_views_can_disable_controls_and_stop(self):
        for view_type in (PermissionSetupView, RoleSetupView, RoleEditView, ChannelSetupView):
            self.assertTrue(issubclass(view_type, SetupView))
        view = SetupView()
        view.add_item(discord.ui.Button(label='Create'))
        view.add_item(discord.ui.Select(options=[discord.SelectOption(label='Role')]))
        view.disable_all_items()
        self.assertTrue(all(item.disabled for item in view.children))
        self.assertTrue(view.is_finished())


if __name__ == '__main__':
    unittest.main()
