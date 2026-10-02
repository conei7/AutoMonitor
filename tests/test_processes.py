"""Actual Linux process tests: these bots do not use Discord or the network."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest

from runtime import Supervisor, validate_recipe, write_json


@unittest.skipUnless(os.name == 'posix', 'Linux process groups')
class ProcessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.engine=Supervisor(Path(self.tmp.name))
        base=self.engine.home/'bots/fixture';release=base/'releases/one'
        (release/'repo').mkdir(parents=True);(release/'.venv/bin').mkdir(parents=True)
        (release/'.venv/bin/python').symlink_to(sys.executable)
        (release/'repo/bot.py').write_text('import time\nwhile True: time.sleep(1)\n')
        recipe=validate_recipe({'name':'fixture','repository':'https://github.com/example/bot','entrypoint':'bot.py'})
        write_json(release/'release.json',{'commit':'fixture','recipe':recipe})
        self.engine.state['projects']['fixture']={'recipe':recipe,'active':str(release),'previous':None,'enabled':False,'error':''}
    async def asyncTearDown(self):
        await self.engine.close();self.tmp.cleanup()

    async def test_restart_reaps_old_process(self):
        await self.engine.start('fixture');old=self.engine.processes['fixture']
        await self.engine.restart('fixture');new=self.engine.processes['fixture']
        self.assertNotEqual(old.pid,new.pid);self.assertIsNotNone(old.poll());self.assertIsNone(new.poll())

    async def test_close_reaps_process_and_preserves_desired_state(self):
        await self.engine.start('fixture');proc=self.engine.processes['fixture']
        await self.engine.close()
        self.assertIsNotNone(proc.poll());self.assertTrue(self.engine.project('fixture')['enabled'])

    async def test_stop_is_persistent_and_stays_stopped(self):
        await self.engine.start('fixture');proc=self.engine.processes['fixture']
        await self.engine.stop('fixture');await self.engine.monitor_one('fixture')
        self.assertIsNotNone(proc.poll());self.assertFalse(self.engine.project('fixture')['enabled'])

    async def test_exit_is_recovered_without_duplicate(self):
        await self.engine.start('fixture');old=self.engine.processes['fixture']
        old.terminate();await asyncio.to_thread(old.wait)
        self.engine.last_started['fixture']=0
        await self.engine.monitor_one('fixture');new=self.engine.processes['fixture']
        await self.engine.monitor_one('fixture')
        self.assertNotEqual(old.pid,new.pid);self.assertIs(new,self.engine.processes['fixture'])

    async def test_stop_terminates_helper_after_parent_exits(self):
        release=Path(self.engine.project('fixture')['active'])
        (release/'repo/bot.py').write_text('import subprocess,sys\np=subprocess.Popen([sys.executable,"-c","import time;time.sleep(120)"])\nprint(p.pid,flush=True)\n')
        await self.engine.start('fixture')
        proc=self.engine.processes['fixture'];await asyncio.to_thread(proc.wait)
        helper=int((self.engine.base('fixture')/'bot.log').read_text().strip())
        await self.engine.stop('fixture');await asyncio.sleep(0.1)
        stat=Path(f'/proc/{helper}/stat')
        self.assertTrue(not stat.exists() or stat.read_text().split()[2]=='Z')

if __name__=='__main__':unittest.main()
