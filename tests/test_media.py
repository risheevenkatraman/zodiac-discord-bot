import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import yt_dlp

from bot import GuildMusicPlayer, MusicTrack, ZodiacBot
from media import MediaError, extract_info, extraction_options
from test_reliability import Response


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, **kwargs):
        return SimpleNamespace(ytdlp_js_runtime=kwargs.get('runtime'),
                               ytdlp_cookies_file=kwargs.get('cookies'))

    def test_cookie_and_runtime_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cookies.txt'
            path.write_text('# Netscape HTTP Cookie File\n')
            options = extraction_options(self.settings(runtime='node:C:/node/node.exe', cookies=str(path)))
            self.assertEqual(options['cookiefile'], str(path))
            self.assertEqual(options['js_runtimes'], {'node': {'path': 'C:/node/node.exe'}})

    def test_missing_cookie_file_reports_configuration_error(self):
        with self.assertRaisesRegex(MediaError, 'cookie file is missing'):
            extraction_options(self.settings(cookies='/missing/cookies.txt'))

    def test_youtube_challenge_has_actionable_error(self):
        with patch('media.yt_dlp.YoutubeDL') as factory:
            factory.return_value.__enter__.return_value.extract_info.side_effect = yt_dlp.utils.DownloadError(
                "Sign in to confirm you're not a bot"
            )
            with self.assertRaisesRegex(MediaError, 'YTDLP_COOKIES_FILE'):
                extract_info('url', {})

    async def test_both_playlist_schemas_and_pagination(self):
        for field, item_field in [('tracks', 'track'), ('items', 'item')]:
            bot = SimpleNamespace(spotify_get=AsyncMock(side_effect=[
                {field: {'items': [{item_field: {'name': 'One', 'artists': [{'name': 'Artist'}]}}],
                         'next': 'https://api.spotify.com/v1/page2'}},
                {'items': [{item_field: {'name': 'Two', 'artists': []}}, {item_field: None}], 'next': None},
            ]))
            tracks = await ZodiacBot.resolve_spotify(bot, 'https://open.spotify.com/playlist/' + 'a' * 22)
            self.assertEqual(len(tracks), 2)
            self.assertEqual(tracks[0].source, 'ytsearch1:Artist - One')

    async def test_playlist_without_items_explains_user_authorization(self):
        bot = SimpleNamespace(spotify_get=AsyncMock(return_value={'name': 'Playlist'}))
        with self.assertRaisesRegex(MediaError, 'SPOTIFY_REFRESH_TOKEN'):
            await ZodiacBot.resolve_spotify(bot, 'https://open.spotify.com/playlist/' + 'a' * 22)

    async def test_spotify_401_refreshes_and_retries(self):
        bot = SimpleNamespace(spotify_access_token=AsyncMock(side_effect=['old', 'fresh']),
                              http_session=SimpleNamespace(get=Mock(side_effect=[
                                  Response({}, 401), Response({'name': 'Song'})])), spotify_token='old')
        self.assertEqual(await ZodiacBot.spotify_get(bot, 'https://api.spotify.com/v1/tracks/id'), {'name': 'Song'})
        self.assertIsNone(bot.spotify_token)
        self.assertEqual(bot.http_session.get.call_args.kwargs['headers']['Authorization'], 'Bearer fresh')

    async def test_spotify_pagination_cannot_leak_token_to_other_host(self):
        bot = SimpleNamespace(spotify_access_token=AsyncMock())
        with self.assertRaises(MediaError):
            await ZodiacBot.spotify_get(bot, 'https://example.org/page')
        bot.spotify_access_token.assert_not_awaited()

    async def test_background_failure_is_reported_to_command_channel(self):
        channel = SimpleNamespace(send=AsyncMock())
        player = GuildMusicPlayer(SimpleNamespace(get_channel=Mock(return_value=channel)), 1)
        player._play = AsyncMock(side_effect=MediaError('YouTube requires verification.'))
        try:
            with self.assertLogs('zodiac-bot', level='ERROR'):
                await player.enqueue([MusicTrack('url', 'Song', 123)])
                await asyncio.wait_for(player.queue.join(), 1)
            self.assertIn('YouTube requires verification', channel.send.call_args.args[0])
        finally:
            await player.stop()


if __name__ == '__main__':
    unittest.main()
