"""Discord + GitHub bot administration and same-owner Unix control socket."""
import argparse
import asyncio
import contextlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import sys

from runtime import Supervisor, write_json

HOME = Path(os.environ.get("AUTOMONITOR_HOME", str(Path(__file__).parent / "runtime")))


async def control():
    request = json.load(sys.stdin)
    reader, writer = await asyncio.open_unix_connection(str(HOME / "control.sock"), limit=1024*1024+1)
    writer.write(json.dumps(request).encode() + b"\n"); await writer.drain()
    response = json.loads(await reader.readline())
    writer.close(); await writer.wait_closed()
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0 if response.get("ok") else 1


async def serve():
    import discord
    from discord import app_commands
    from discord.ext import commands
    import fcntl
    engine = Supervisor(HOME)
    if not engine.config.get("TOKEN") or not engine.config.get("GUILD_ID") or not engine.config.get("AUTHORIZED_LIST"):
        raise RuntimeError("TOKEN、GUILD_ID、AUTHORIZED_LISTが必要です")
    lockfile = open(HOME / "manager.lock", "w")
    try: fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: raise RuntimeError("管理botは既に動いています")
    handler = RotatingFileHandler(HOME / "manager.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
    class SecretFilter(logging.Filter):
        def filter(self, record):
            record.msg = engine.clean(record.getMessage()); record.args = (); return True
    handler.addFilter(SecretFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s", force=True)
    stop_event = asyncio.Event()
    restart_requested = False

    async def rpc(reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readline(), 15)
            if len(raw) > 1024*1024: raise ValueError("入力が大きすぎます")
            request = json.loads(raw)
            result = await self_update() if request.get("action") == "self_update" else await engine.dispatch(**request)
            response = {"ok": True, "result": result}
        except Exception as e: response = {"ok": False, "error": engine.clean(str(e))}
        writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
        await writer.drain(); writer.close(); await writer.wait_closed()

    socket = HOME / "control.sock"
    with contextlib.suppress(FileNotFoundError): socket.unlink()
    server = await asyncio.start_unix_server(rpc, path=str(socket), limit=1024*1024+1)
    socket.chmod(0o600)

    async def respond(interaction, result, filename="result.json"):
        text = engine.clean(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
        if len(text) <= 1800:
            await interaction.followup.send("```\n" + text.replace("```", "''' ") + "\n```", ephemeral=True)
        else: await interaction.followup.send(file=discord.File(io.BytesIO(text.encode()), filename=filename), ephemeral=True)

    async def run(interaction, operation):
        if not interaction.response.is_done(): await interaction.response.defer(ephemeral=True)
        try: await respond(interaction, await operation())
        except Exception as e:
            logging.error("operation failed: %s", e)
            await respond(interaction, {"error": str(e)})

    class Confirm(discord.ui.View):
        def __init__(self, user_id, operation):
            super().__init__(timeout=300); self.user_id = user_id; self.operation = operation
        async def interaction_check(self, interaction):
            if interaction.user.id != self.user_id or interaction.user.id not in engine.config["AUTHORIZED_LIST"]:
                await interaction.response.send_message("実行権限がありません", ephemeral=True); return False
            return True
        @discord.ui.button(label="実行", style=discord.ButtonStyle.green)
        async def confirm(self, interaction, button):
            button.disabled = True; await interaction.response.edit_message(view=self)
            await run(interaction, self.operation); self.stop()

    class SecretModal(discord.ui.Modal, title="秘密設定（本人のみ）"):
        key = discord.ui.TextInput(label="環境変数名／管理bot設定キー", default="DISCORD_BOT_TOKEN", max_length=100)
        value = discord.ui.TextInput(label="値", style=discord.TextStyle.paragraph, max_length=4000)
        def __init__(self, name): super().__init__(); self.name = name
        async def on_submit(self, interaction):
            if interaction.user.id not in engine.config["AUTHORIZED_LIST"]:
                await interaction.response.send_message("実行権限がありません", ephemeral=True); return
            config = engine.get_config(self.name)
            if self.name: config.setdefault("secrets", {}).setdefault("env", {})[str(self.key)] = str(self.value)
            else: config[str(self.key)] = str(self.value)
            await run(interaction, lambda: engine.set_config(config, self.name))

    class RegisterModal(discord.ui.Modal, title="GitHubからbotを登録"):
        name = discord.ui.TextInput(label="bot名（英小文字）", max_length=40)
        repository = discord.ui.TextInput(label="GitHubリポジトリURL", placeholder="https://github.com/owner/repo", max_length=200)
        entrypoint = discord.ui.TextInput(label="起動ファイル", default="bot.py", max_length=150)
        requirements = discord.ui.TextInput(label="依存ファイル（空欄ならrequirements.txt）", required=False, max_length=150)
        ref = discord.ui.TextInput(label="branch/tag/commit（空欄なら既定）", required=False, max_length=150)
        async def on_submit(self, interaction):
            if interaction.user.id not in engine.config["AUTHORIZED_LIST"]:
                await interaction.response.send_message("実行権限がありません", ephemeral=True); return
            recipe = {"name": str(self.name), "repository": str(self.repository), "entrypoint": str(self.entrypoint), "requirements": str(self.requirements), "ref": str(self.ref)}
            await run(interaction, lambda: engine.register(recipe))
            await interaction.followup.send("/set_secret または /set_config で設定し、/start で起動してください。", ephemeral=True)

    async def self_update():
        nonlocal restart_requested
        recipe = {"name": "manager", "repository": "https://github.com/conei7/AutoMonitor.git", "entrypoint": "AutoMonitor.py", "requirements": "requirements.txt", "ref": "", "args": [], "mounts": []}
        release = Path(await engine.prepare(recipe))
        await engine.command([str(release / ".venv/bin/python"), "-m", "unittest", "discover", "-s", "tests"], cwd=release / "repo")
        current = HOME.parent / "manager/current"
        write_json(HOME / "manager-release.json", {"previous": str(current.resolve()), "active": str(release)})
        tmp = current.with_name("current.new")
        with contextlib.suppress(FileNotFoundError): tmp.unlink()
        tmp.symlink_to(release, target_is_directory=True); tmp.replace(current)
        # Allow the interaction reply to be sent before closing the gateway.
        restart_requested = True; asyncio.get_running_loop().call_later(2, stop_event.set)
        return {"updated": True, "restarting": True}

    class Admin(commands.Cog):
        async def interaction_check(self, interaction):
            if interaction.guild_id != engine.config["GUILD_ID"] or interaction.user.id not in engine.config["AUTHORIZED_LIST"]:
                await interaction.response.send_message("実行権限がありません", ephemeral=True); return False
            return True
        async def names(self, interaction, current):
            return [app_commands.Choice(name=n, value=n) for n in engine.state["projects"] if current.lower() in n.lower()][:25]

        @app_commands.command(name="register", description="GitHubから新しいbotを導入します")
        async def register_cmd(self, interaction: discord.Interaction): await interaction.response.send_modal(RegisterModal())
        @app_commands.command(name="status", description="botの状態・稼働版・直近の異常")
        async def status_cmd(self, interaction: discord.Interaction, project: str | None = None):
            await interaction.response.defer(ephemeral=True); await respond(interaction, engine.status(project))
        @app_commands.command(name="start", description="botを起動します")
        async def start_cmd(self, interaction: discord.Interaction, project: str): await run(interaction, lambda: engine.start(project))
        @app_commands.command(name="stop", description="botを停止し停止状態を保存")
        async def stop_cmd(self, interaction: discord.Interaction, project: str): await run(interaction, lambda: engine.stop(project))
        @app_commands.command(name="reboot", description="botを終了して再起動します")
        async def reboot_cmd(self, interaction: discord.Interaction, project: str): await run(interaction, lambda: engine.restart(project))
        @app_commands.command(name="unregister", description="登録解除（コードとデータは保持）")
        async def unregister_cmd(self, interaction: discord.Interaction, project: str):
            await interaction.response.send_message("停止して登録を解除します", view=Confirm(interaction.user.id, lambda: engine.unregister(project)), ephemeral=True)
        @app_commands.command(name="pull", description="GitHubとの差分を確認してbot全体を更新")
        async def pull_cmd(self, interaction: discord.Interaction, project: str):
            await interaction.response.defer(ephemeral=True)
            try:
                await respond(interaction, await engine.diff(project), "diff.txt")
                await interaction.followup.send("このbotを更新しますか？", view=Confirm(interaction.user.id, lambda: engine.deploy(project)), ephemeral=True)
            except Exception as e: await respond(interaction, {"error": str(e)})
        @app_commands.command(name="upgrade", description="対象botのライブラリを追加・更新")
        async def upgrade_cmd(self, interaction: discord.Interaction, project: str, library: str, version: str | None = None):
            await interaction.response.send_message("依存環境を作り直します", view=Confirm(interaction.user.id, lambda: engine.upgrade(project, library, version)), ephemeral=True)
        @app_commands.command(name="rollback", description="直前のコード・依存環境へ戻す")
        async def rollback_cmd(self, interaction: discord.Interaction, project: str): await run(interaction, lambda: engine.rollback(project))
        @app_commands.command(name="get_logs", description="秘密値を伏せてログを取得")
        async def logs_cmd(self, interaction: discord.Interaction, project: str | None = None):
            await interaction.response.defer(ephemeral=True); await respond(interaction, engine.logs(project), "logs.txt")
        @app_commands.command(name="get_config", description="秘密値を伏せて設定を取得")
        async def get_config_cmd(self, interaction: discord.Interaction, project: str | None = None):
            await interaction.response.defer(ephemeral=True); await respond(interaction, engine.get_config(project), "config.json")
        @app_commands.command(name="set_config", description="JSON設定を反映（***は元の値を保持）")
        async def set_config_cmd(self, interaction: discord.Interaction, file: discord.Attachment, project: str | None = None):
            async def operation():
                if file.size > 1024*1024: raise ValueError("設定は1MiB以下です")
                return await engine.set_config(json.loads((await file.read()).decode("utf-8-sig")), project)
            await run(interaction, operation)
        @app_commands.command(name="set_secret", description="秘密値を本人のみの入力フォームで設定")
        async def secret_cmd(self, interaction: discord.Interaction, project: str | None = None): await interaction.response.send_modal(SecretModal(project))
        @app_commands.command(name="backup", description="設定・実データをSBCにバックアップ")
        async def backup_cmd(self, interaction: discord.Interaction, project: str | None = None):
            await interaction.response.defer(ephemeral=True); await respond(interaction, engine.backup(project))
        @app_commands.command(name="restore_config", description="保存したバックアップを復元")
        async def restore_cmd(self, interaction: discord.Interaction, backup_id: str, project: str | None = None):
            await interaction.response.send_message("バックアップを復元します", view=Confirm(interaction.user.id, lambda: engine.restore(backup_id, project)), ephemeral=True)
        @app_commands.command(name="reboot_self", description="管理botを子botを整理して再起動")
        async def reboot_self_cmd(self, interaction: discord.Interaction):
            nonlocal restart_requested
            await interaction.response.send_message("再起動します", ephemeral=True)
            restart_requested = True; stop_event.set()
        @app_commands.command(name="pull_self", description="管理botをGitHubから更新して再起動")
        async def pull_self_cmd(self, interaction: discord.Interaction):
            await interaction.response.send_message("管理bot全体を検証して更新します", view=Confirm(interaction.user.id, self_update), ephemeral=True)

    class Bot(commands.Bot):
        async def setup_hook(self):
            cog = Admin()
            for command in cog.get_app_commands():
                if "project" in {p.name for p in command.parameters}: command.autocomplete("project")(cog.names)
            await self.add_cog(cog)
            guild = discord.Object(id=engine.config["GUILD_ID"])
            self.tree.copy_global_to(guild=guild); await self.tree.sync(guild=guild)
        async def on_ready(self):
            engine.discord_state = "connected"
            logging.info("Manager Discord ready: %s", self.user.id)
        async def on_disconnect(self): engine.discord_state = "disconnected"
    bot = None
    async def connect_discord():
        nonlocal bot
        while not stop_event.is_set():
            bot = Bot(command_prefix="/", intents=discord.Intents.default())
            try:
                await bot.start(engine.config["TOKEN"])
            except (discord.LoginFailure, discord.HTTPException, OSError) as e:
                engine.discord_state = "credentials_invalid" if isinstance(e, discord.LoginFailure) else "disconnected"
                logging.error("Discord unavailable; local bot control remains available: %s", e)
            finally:
                await bot.close()
            if not stop_event.is_set(): await asyncio.sleep(30)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT): loop.add_signal_handler(sig, stop_event.set)
    monitor = asyncio.create_task(engine.monitor())
    gateway = asyncio.create_task(connect_discord())
    stopped = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait([gateway, stopped], return_when=asyncio.FIRST_COMPLETED)
        if gateway in done: await gateway
    finally:
        if bot: await bot.close()
        gateway.cancel(); monitor.cancel(); stopped.cancel()
        with contextlib.suppress(asyncio.CancelledError): await gateway
        with contextlib.suppress(asyncio.CancelledError): await monitor
        await engine.close(); server.close(); await server.wait_closed()
        with contextlib.suppress(FileNotFoundError): socket.unlink()
        lockfile.close()
    return 75 if restart_requested else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", choices=["serve", "ctl"], default="serve")
    args = parser.parse_args()
    return asyncio.run(control() if args.mode == "ctl" else serve())

if __name__ == "__main__": raise SystemExit(main())
