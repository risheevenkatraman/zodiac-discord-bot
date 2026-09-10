import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import discord
from aiohttp import web

from bot import (
    MAX_QUEUE_SIZE, PERMISSION_NAMES, GuildMusicPlayer, MusicTrack,
    ChannelSetupView, RoleEditView, SetupView, ZodiacBot, check_music_channel,
    normalize_twitch_username, register_commands, send_error, webhook_handler,
)
from database import Database


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientError('service unavailable')


def interaction():
    return SimpleNamespace(
        response=SimpleNamespace(
            type=discord.InteractionResponseType.deferred_channel_message,
            is_done=Mock(return_value=True), defer=AsyncMock(),
            send_message=AsyncMock(),
        ),
        permissions=discord.Permissions(administrator=True),
        edit_original_response=AsyncMock(), delete_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_resolves_defer_and_remains_visible(self):
        request = interaction()
        await send_error(request, 'Failed.')
        request.edit_original_response.assert_awaited_once_with(content='Failed.', view=None)
        request.delete_original_response.assert_not_awaited()

    async def test_setup_double_submit_only_starts_once(self):
        view = SetupView()
        view.message = SimpleNamespace(delete=AsyncMock())
        first, second = interaction(), interaction()
        self.assertEqual(await asyncio.gather(
            view.begin_operation(first), view.begin_operation(second)
        ), [True, False])
        first.response.defer.assert_awaited_once()
        second.response.defer.assert_not_awaited()

    async def test_setup_rechecks_administrator_permission(self):
        view = SetupView()
        request = interaction()
        request.permissions = discord.Permissions.none()
        self.assertFalse(await view.begin_operation(request))
        self.assertFalse(view.is_finished())
        request.response.defer.assert_not_awaited()
        view.stop()

    async def test_permission_selection_has_no_alias_collisions(self):
        values = [discord.Permissions.VALID_FLAGS[name] for name in PERMISSION_NAMES]
        self.assertEqual(len(values), len(set(values)))
        role = SimpleNamespace(permissions=discord.Permissions(view_channel=True))
        view = RoleEditView(1, role, [])
        permissions = discord.Permissions(**{
            name: name in view.selected_permissions for name in PERMISSION_NAMES
        })
        self.assertTrue(permissions.view_channel)
        self.assertFalse(permissions.administrator)
        view.stop()

    async def test_empty_selects_still_have_valid_disabled_options(self):
        views = [RoleEditView(1, SimpleNamespace(permissions=discord.Permissions.none()), []),
                 ChannelSetupView(1, Mock(), 'private', [])]
        for view in views:
            try:
                for component in view.children:
                    if isinstance(component, discord.ui.Select):
                        self.assertGreaterEqual(len(component.options), 1)
                        self.assertGreaterEqual(component.max_values, 1)
                empty_select = view.channel_select if isinstance(view, RoleEditView) else view.role_select
                self.assertTrue(empty_select.disabled)
            finally:
                view.stop()

    async def test_expired_spotify_token_is_refreshed_and_cached(self):
        bot = SimpleNamespace(
            spotify_token='expired', spotify_token_expires_at=1,
            spotify_refresh_token=None,
            settings=SimpleNamespace(spotify_client_id='id', spotify_client_secret='secret'),
            http_session=SimpleNamespace(post=Mock(return_value=Response({
                'access_token': 'fresh', 'expires_in': 3600,
            }))),
        )
        with patch('bot.time.monotonic', return_value=100):
            self.assertEqual(await ZodiacBot.spotify_access_token(bot), 'fresh')
            self.assertEqual(await ZodiacBot.spotify_access_token(bot), 'fresh')
        bot.http_session.post.assert_called_once()
        self.assertEqual(bot.spotify_token_expires_at, 3640)

    async def test_worker_continues_after_failed_track(self):
        player = GuildMusicPlayer(Mock(), 1)
        player._play = AsyncMock(side_effect=[RuntimeError('bad media'), None])
        try:
            await player.enqueue([MusicTrack('bad', 'Bad'), MusicTrack('good', 'Good')])
            with self.assertLogs('zodiac-bot', level='ERROR'):
                await asyncio.wait_for(player.queue.join(), 1)
            self.assertEqual(player._play.await_count, 2)
            self.assertFalse(player.player_task.done())
        finally:
            await player.stop()

    async def test_queue_limit_does_not_partially_enqueue_playlist(self):
        player = GuildMusicPlayer(Mock(), 1)
        try:
            with self.assertRaises(ValueError):
                await player.enqueue([MusicTrack('url', 'Title')] * (MAX_QUEUE_SIZE + 1))
            self.assertTrue(player.queue.empty())
        finally:
            await player.stop()

    async def test_other_voice_channel_cannot_control_music(self):
        request = interaction()
        request.guild = SimpleNamespace(voice_client=SimpleNamespace(channel='music'))
        request.user = SimpleNamespace(voice=SimpleNamespace(channel='other'))
        self.assertFalse(await check_music_channel(request))
        request.user.voice.channel = 'music'
        self.assertTrue(await check_music_channel(request))

    async def test_media_rejects_unsupported_and_lookalike_hosts(self):
        for url in ('file:///etc/passwd', 'https://notspotify.com/track/123', 'https://example.org'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await ZodiacBot.resolve_media(Mock(), url)

    async def test_webhook_rejects_malformed_json_and_non_post_urls(self):
        bot = SimpleNamespace(settings=SimpleNamespace(webhook_secret='test'))
        request = SimpleNamespace(app={'bot': bot}, headers={'X-Webhook-Secret': 'test'},
                                  json=AsyncMock(side_effect=ValueError()))
        with self.assertRaises(web.HTTPBadRequest):
            await webhook_handler(request)
        request.json = AsyncMock(return_value={'url': 'https://x.com/example'})
        with self.assertRaises(web.HTTPBadRequest):
            await webhook_handler(request)

    async def test_webhook_counts_actual_deliveries(self):
        bot = SimpleNamespace(
            settings=SimpleNamespace(webhook_secret='test'),
            database=SimpleNamespace(list_guild_ids=AsyncMock(return_value=[1, 2]),
                                     get_role=AsyncMock(return_value=None)),
            post_to_configured_channel=AsyncMock(side_effect=[True, False]),
        )
        request = SimpleNamespace(app={'bot': bot}, headers={'X-Webhook-Secret': 'test'},
                                  json=AsyncMock(return_value={'url': 'https://x.com/example/status/123'}))
        response = await webhook_handler(request)
        self.assertIn('"posted": 1', response.text)
        self.assertIn('"skipped": 1', response.text)

    async def test_all_commands_register_as_guild_only(self):
        bot = ZodiacBot(Mock(), SimpleNamespace(close=AsyncMock()))
        try:
            register_commands(bot)
            self.assertIn('queue', [command.name for command in bot.tree.get_commands()])
            self.assertTrue(all(command.guild_only for command in bot.tree.get_commands()))
            for command in bot.tree.get_commands():
                command.to_dict(bot.tree)
        finally:
            await bot.close()

    def test_twitch_username_validation(self):
        self.assertEqual(normalize_twitch_username(' Example_123 '), 'example_123')
        for value in ('', 'https://twitch.tv/example', '@everyone', 'a b'):
            with self.assertRaises(ValueError):
                normalize_twitch_username(value)


class TwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.directory.name) / 'test.db'))
        await self.database.initialize()
        self.bot = SimpleNamespace(
            settings=SimpleNamespace(twitch_client_id='id', twitch_client_secret='secret'),
            database=self.database, get_twitch_token=AsyncMock(return_value='token'),
            http_session=SimpleNamespace(get=Mock()),
            post_to_configured_channel=AsyncMock(return_value=True),
        )

    async def asyncTearDown(self):
        await self.database.close()
        self.directory.cleanup()

    async def test_shared_streamer_notifies_both_servers_once(self):
        for guild_id in (1, 2):
            await self.database.add_twitch_account(guild_id, 'streamer')
        self.bot.http_session.get.return_value = Response({'data': [
            {'user_login': 'streamer', 'user_name': 'Streamer'}
        ]})
        await ZodiacBot.poll_twitch(self.bot)
        self.assertEqual([args.args[0] for args in self.bot.post_to_configured_channel.await_args_list], [1, 2])
        await ZodiacBot.poll_twitch(self.bot)
        self.assertEqual(self.bot.post_to_configured_channel.await_count, 2)
        # Stored notification state survives a process/database restart.
        await self.database.close()
        await self.database.initialize()
        await ZodiacBot.poll_twitch(self.bot)
        self.assertEqual(self.bot.post_to_configured_channel.await_count, 2)

    async def test_more_than_100_streamers_are_batched(self):
        for index in range(101):
            await self.database.add_twitch_account(1, f'user{index}')
        self.bot.http_session.get.return_value = Response({'data': []})
        await ZodiacBot.poll_twitch(self.bot)
        calls = self.bot.http_session.get.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual([len(call.kwargs['params']) for call in calls], [101, 2])
        self.assertTrue(all(('first', '100') in call.kwargs['params'] for call in calls))

    async def test_unavailable_destination_is_retried(self):
        await self.database.add_twitch_account(1, 'streamer')
        self.bot.http_session.get.return_value = Response({'data': [
            {'user_login': 'streamer', 'user_name': 'Streamer'}
        ]})
        self.bot.post_to_configured_channel.side_effect = [False, True]
        await ZodiacBot.poll_twitch(self.bot)
        await ZodiacBot.poll_twitch(self.bot)
        await ZodiacBot.poll_twitch(self.bot)
        self.assertEqual(self.bot.post_to_configured_channel.await_count, 2)

    async def test_failed_poll_preserves_live_state(self):
        await self.database.add_twitch_account(1, 'streamer')
        await self.database.sync_twitch_live_accounts({(1, 'streamer')})
        self.bot.http_session.get.return_value = Response({}, status=503)
        with self.assertRaises(aiohttp.ClientError):
            await ZodiacBot.poll_twitch(self.bot)
        self.assertEqual(await self.database.sync_twitch_live_accounts({(1, 'streamer')}), set())


if __name__ == '__main__':
    unittest.main()
