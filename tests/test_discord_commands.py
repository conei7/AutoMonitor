"""Build the actual command tree and verify authorization without contacting Discord."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from runtime import write_json


@unittest.skipUnless(os.name == 'posix', 'Linux command service')
class DiscordCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_tree_and_owner_checks(self):
        import AutoMonitor
        import discord
        class ProbeComplete(Exception):pass
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory)
            write_json(home/'config.json',{'TOKEN':'test-placeholder-only','GUILD_ID':123,'AUTHORIZED_LIST':[7]})
            async def probe(bot,token,**kwargs):
                await bot.setup_hook()
                admin=bot.get_cog('Admin')
                names={c.name for c in admin.get_app_commands()}
                self.assertTrue({'register','pull','upgrade','get_config','set_config','restore_config','pull_self','reboot_self','start','stop','status','rollback','set_secret'} <= names)
                interaction=mock.Mock();interaction.guild_id=123;interaction.user.id=7
                interaction.response.send_message=mock.AsyncMock()
                self.assertTrue(await admin.interaction_check(interaction))
                interaction.user.id=8
                command=next(c for c in admin.get_app_commands() if c.name=='start')
                self.assertFalse(await command._check_can_run(interaction))
                interaction.response.send_message.assert_awaited_once()
                raise ProbeComplete()
            with mock.patch.object(AutoMonitor,'HOME',home), mock.patch('discord.ext.commands.Bot.start',new=probe), mock.patch('discord.app_commands.CommandTree.sync',new=mock.AsyncMock(return_value=[])):
                with self.assertRaises(ProbeComplete):await AutoMonitor.serve()
            import logging
            for handler in logging.getLogger().handlers[:]:handler.close();logging.getLogger().removeHandler(handler)
