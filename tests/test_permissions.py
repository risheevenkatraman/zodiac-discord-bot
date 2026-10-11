import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from discord import app_commands

from bot import ZodiacBot, register_commands
from database import Database
from permissions import can_administer
from scrim import ScrimView


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_grant_replace_revoke_and_guild_isolation(self):
        database = Database(":memory:")
        await database.initialize()
        bot = ZodiacBot(Mock(), database)
        register_commands(bot)
        request = SimpleNamespace(
            guild_id=2,
            client=bot,
            permissions=discord.Permissions.none(),
            user=SimpleNamespace(id=1, roles=[SimpleNamespace(id=3)]),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        try:
            check = bot.tree.get_command("scrim").checks[0]
            with self.assertRaises(app_commands.MissingPermissions):
                await check(request)
            await database.set_role(2, "bot_admin", 3)
            self.assertTrue(await check(request))
            view = ScrimView(1, 2)
            self.assertTrue(await view.interaction_check(request))
            request.guild_id = 4
            self.assertFalse(await can_administer(request))
            request.guild_id = 2
            grant = bot.tree.get_command("admin_role")
            for admin_check in grant.checks:
                with self.assertRaises(app_commands.MissingPermissions):
                    admin_check(request)
            await database.set_role(2, "bot_admin", 5)
            self.assertFalse(await view.interaction_check(request))
            await database.remove_role(2, "bot_admin", 3)
            self.assertEqual(await database.get_role(2, "bot_admin"), 5)
            await database.remove_role(2, "bot_admin", 5)
            self.assertIsNone(await database.get_role(2, "bot_admin"))
            view.stop()
        finally:
            await bot.close()
