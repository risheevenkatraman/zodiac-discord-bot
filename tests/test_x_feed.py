import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import webhook_handler
from database import Database
from x_feed import XMonitor, account_username, post_identity


class XFeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.folder.name) / 'test.db'))
        await self.database.initialize()
        self.deliver = AsyncMock(return_value=True)
        self.monitor = XMonitor('zodiacsesport', 'not-a-real-token', self.database, self.deliver)
        self.monitor.user_id = '42'

    async def asyncTearDown(self):
        await self.database.close()
        self.folder.cleanup()

    def post(self, post_id, author='42', reference=None):
        post = {'id': str(post_id), 'author_id': author}
        if reference:
            post['referenced_tweets'] = [{'type': reference}]
        return post

    def test_account_link_and_post_normalization(self):
        self.assertEqual(account_username('https://x.com/zodiacsesport?s=20'), 'zodiacsesport')
        self.assertEqual(post_identity('https://twitter.com/ZodiacSEsport/status/123?s=20'), ('zodiacsesport', '123'))
        for url in ('https://x.com.evil.test/name', 'https://x.com/name/status/123'):
            with self.assertRaises(ValueError):
                account_username(url)

    async def test_first_poll_baselines_without_history(self):
        self.monitor.get = AsyncMock(return_value={'data': [self.post(11), self.post(10)]})
        await self.monitor.poll(None)
        self.deliver.assert_not_awaited()
        self.assertEqual(await self.database.get_x_checkpoint('42'), '11')

    async def test_new_posts_delivered_oldest_first_across_pages(self):
        await self.database.set_x_checkpoint('42', '10')
        self.monitor.get = AsyncMock(side_effect=[
            {'data': [self.post(13)], 'meta': {'next_token': 'next'}},
            {'data': [self.post(12), self.post(11)]},
        ])
        await self.monitor.poll(None)
        self.assertEqual([call.args[1] for call in self.deliver.await_args_list], ['11', '12', '13'])
        self.assertEqual(await self.database.get_x_checkpoint('42'), '13')

    async def test_other_authors_replies_and_reposts_ignored(self):
        await self.database.set_x_checkpoint('42', '10')
        self.monitor.get = AsyncMock(return_value={'data': [
            self.post(11, author='99'), self.post(12, reference='replied_to'),
            self.post(13, reference='retweeted'), self.post(14, reference='quoted'),
        ]})
        await self.monitor.poll(None)
        self.deliver.assert_awaited_once_with('zodiacsesport', '14')

    async def test_failed_delivery_preserves_checkpoint(self):
        await self.database.set_x_checkpoint('42', '10')
        self.monitor.get = AsyncMock(return_value={'data': [self.post(11)]})
        self.deliver.return_value = False
        await self.monitor.poll(None)
        self.assertEqual(await self.database.get_x_checkpoint('42'), '10')

    async def test_failed_second_page_does_not_advance_or_deliver_partial_batch(self):
        await self.database.set_x_checkpoint('42', '10')
        self.monitor.get = AsyncMock(side_effect=[
            {'data': [self.post(12)], 'meta': {'next_token': 'next'}}, RuntimeError('failed'),
        ])
        with self.assertRaises(RuntimeError):
            await self.monitor.poll(None)
        self.deliver.assert_not_awaited()
        self.assertEqual(await self.database.get_x_checkpoint('42'), '10')

    def bot(self):
        return SimpleNamespace(
            settings=SimpleNamespace(webhook_secret='test', x_account='zodiacsesport'),
            database=self.database, x_delivery_lock=asyncio.Lock(),
            post_to_configured_channel=AsyncMock(return_value=True),
        )

    def request(self, bot, username='zodiacsesport'):
        return SimpleNamespace(app={'bot': bot}, headers={'X-Webhook-Secret': 'test'},
                               json=AsyncMock(return_value={'url': f'https://x.com/{username}/status/11'}))

    async def test_webhook_rejects_other_accounts_without_sending(self):
        bot = self.bot()
        result = await webhook_handler(self.request(bot, 'other'))
        self.assertTrue(json.loads(result.text)['ignored'])
        bot.post_to_configured_channel.assert_not_awaited()

    async def test_concurrent_webhooks_and_restart_do_not_duplicate(self):
        await self.database.set_channel(1, 'social', 123)
        bot = self.bot()
        await asyncio.gather(webhook_handler(self.request(bot)), webhook_handler(self.request(bot)))
        await self.database.close()
        await self.database.initialize()
        result = await webhook_handler(self.request(bot))
        self.assertEqual(json.loads(result.text)['duplicates'], 1)
        bot.post_to_configured_channel.assert_awaited_once()

    async def test_failed_destination_retries_without_resending_successful_one(self):
        await self.database.set_channel(1, 'social', 123)
        await self.database.set_channel(2, 'social', 456)
        bot = self.bot()
        bot.post_to_configured_channel.side_effect = [True, False, True]
        first = await webhook_handler(self.request(bot))
        second = await webhook_handler(self.request(bot))
        self.assertEqual(first.status, 503)
        self.assertEqual(second.status, 200)
        self.assertEqual([call.args[0] for call in bot.post_to_configured_channel.await_args_list], [1, 2, 2])


if __name__ == '__main__':
    unittest.main()
