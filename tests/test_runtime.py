import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from runtime import Supervisor, merge_redacted, redact, validate_recipe, write_json


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = Supervisor(Path(self.temp.name))
        self.recipe = validate_recipe({"name": "example", "repository": "https://github.com/owner/bot", "entrypoint": "bot.py"})
        self.engine.state["projects"]["example"] = {"recipe": self.recipe, "active": "old", "previous": None, "enabled": False, "error": ""}
        self.engine.base("example").mkdir(parents=True)
    async def asyncTearDown(self):
        await self.engine.close(); self.temp.cleanup()

    def test_paths_cannot_leave_bot(self):
        for path in ("../bot.py", "/etc/passwd", ".git/config", "..\\bot.py"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_recipe({**self.recipe, "entrypoint": path})

    def test_secret_config_roundtrip(self):
        old = {"env": {"DISCORD_BOT_TOKEN": "private-value-123456"}, "files": {"config.json": {"bot_token": "private-value-123456"}}}
        redacted = redact(old)
        self.assertNotIn("private-value", json.dumps(redacted))
        self.assertEqual(merge_redacted(old, redacted), old)

    async def test_start_is_idempotent(self):
        self.engine.attach_data = mock.Mock()
        proc = mock.Mock(); proc.poll.return_value = None
        with mock.patch("runtime.subprocess.Popen", return_value=proc) as popen:
            await self.engine.start("example"); await self.engine.start("example")
            self.assertEqual(popen.call_count, 1)
        self.engine.processes.clear()

    async def test_stop_persists_and_monitor_does_not_restart(self):
        self.engine._stop = mock.AsyncMock()
        self.engine._start = mock.AsyncMock()
        await self.engine.stop("example")
        await self.engine.monitor_one("example")
        self.engine._start.assert_not_awaited()
        reread = Supervisor(self.engine.home)
        self.assertFalse(reread.project("example")["enabled"])

    async def test_restart_stops_before_start(self):
        calls = []
        async def stop(name): calls.append("stop")
        async def start(name): calls.append("start")
        self.engine._stop = stop; self.engine._start = start
        await self.engine.restart("example")
        self.assertEqual(calls, ["stop", "start"])

    async def test_failed_install_does_not_stop_current_bot(self):
        self.engine.prepare = mock.AsyncMock(side_effect=RuntimeError("install failed"))
        self.engine._stop = mock.AsyncMock()
        with self.assertRaises(RuntimeError): await self.engine.deploy("example")
        self.engine._stop.assert_not_awaited()
        self.assertEqual(self.engine.project("example")["active"], "old")

    async def test_failed_candidate_restores_previous_release(self):
        self.engine.project("example")["enabled"] = True
        self.engine.prepare = mock.AsyncMock(return_value="candidate")
        self.engine._stop = mock.AsyncMock()
        proc = mock.Mock(); proc.poll.return_value = 1
        self.engine.processes["example"] = proc
        self.engine._start = mock.AsyncMock()
        with mock.patch("runtime.asyncio.sleep", new=mock.AsyncMock()):
            with self.assertRaises(RuntimeError): await self.engine.deploy("example")
        self.assertEqual(self.engine.project("example")["active"], "old")
        self.assertEqual(self.engine._start.await_count, 2)
        self.engine.processes.clear()

    async def test_unregister_keeps_data_and_blocks_start(self):
        self.engine._stop = mock.AsyncMock()
        await self.engine.unregister("example")
        self.assertIn("example", self.engine.state["projects"])
        with self.assertRaises(ValueError): await self.engine.start("example")

    async def test_backup_restore_preserves_secrets(self):
        self.engine._stop = mock.AsyncMock()
        p = self.engine.base("example") / "private/secrets.json"
        write_json(p, {"env": {"DISCORD_BOT_TOKEN": "token-before"}})
        backup_id = self.engine.backup("example")["backup"]
        write_json(p, {"env": {"DISCORD_BOT_TOKEN": "token-after"}})
        await self.engine.restore(backup_id, "example")
        self.assertIn("token-before", p.read_text())

    async def test_per_bot_lock_serializes_restart(self):
        order = []
        async def stop(name): order.append("stop"); await asyncio.sleep(0.01)
        async def start(name): order.append("start")
        self.engine._stop = stop; self.engine._start = start
        await asyncio.gather(self.engine.restart("example"), self.engine.restart("example"))
        self.assertEqual(order, ["stop", "start", "stop", "start"])

    def test_logs_mask_known_tokens(self):
        self.engine.config["TOKEN"] = "test-secret-token-long"
        (self.engine.home / "manager.log").write_text("login test-secret-token-long")
        self.assertNotIn("test-secret-token-long", self.engine.logs())


if __name__ == "__main__": unittest.main()
