"""Bot-only supervisor. Importing this module never starts a bot or edits files."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
import zipfile


def read_json(path: Path, default=None):
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        tmp.chmod(0o600)
    tmp.replace(path)


def safe_relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/") or ":" in value:
        raise ValueError("相対パスを指定してください")
    p = Path(value)
    if p.is_absolute() or any(part in ("..", ".git", ".venv") for part in p.parts):
        raise ValueError("管理対象外のパスです")
    return p.as_posix()


def validate_recipe(recipe: dict) -> dict:
    r = copy.deepcopy(recipe)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", r.get("name", "")):
        raise ValueError("bot名は英小文字・数字・ハイフン・アンダースコアです")
    repo = r.get("repository", "")
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", repo):
        # Local sources are only configured by the owner during provisioning.
        if not (r.get("local_source") and Path(repo).is_absolute()):
            raise ValueError("GitHubリポジトリのHTTPS URLを指定してください")
    r["entrypoint"] = safe_relative(r.get("entrypoint", "bot.py"))
    r["requirements"] = safe_relative(r.get("requirements") or "requirements.txt")
    r["ref"] = r.get("ref") or ""
    if r["ref"] and (r["ref"].startswith("-") or not re.fullmatch(r"[A-Za-z0-9_./-]+", r["ref"])):
        raise ValueError("Git refが無効です")
    if not isinstance(r.get("args", []), list) or not all(isinstance(x, str) for x in r.get("args", [])):
        raise ValueError("argsは文字列のリストです")
    r["args"] = r.get("args", [])
    r["mounts"] = [safe_relative(x) for x in r.get("mounts", [])]
    return r


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {k: "***" if any(s in k.lower() for s in ("token", "password", "secret", "private_key")) else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            if len(secret) >= 8:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?:mfa\.[\w-]{20,}|[\w-]{20,}\.[\w-]{5,}\.[\w-]{20,}|github_pat_[\w]+|gh[pousr]_[\w]+)", "[REDACTED]", value)
    return value


def merge_redacted(old, new):
    if isinstance(new, str) and new in ("***", "[REDACTED]"):
        return copy.deepcopy(old)
    if isinstance(new, dict):
        return {k: merge_redacted((old or {}).get(k), v) for k, v in new.items()}
    return new


class Supervisor:
    def __init__(self, home: Path):
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.home.chmod(0o700)
        self.config_path = self.home / "config.json"
        self.state_path = self.home / "registry.json"
        self.config = read_json(self.config_path, {})
        self.state = read_json(self.state_path, {"projects": {}})
        self.processes = {}
        self.locks = {}
        self.last_started = {}
        self.failures = {}
        self.log_handles = {}
        self.closing = False
        self.discord_state = "not_connected"

    def save(self):
        write_json(self.state_path, self.state)

    def lock(self, name):
        return self.locks.setdefault(name, asyncio.Lock())

    def project(self, name):
        if name not in self.state["projects"]:
            raise ValueError("登録されていないbotです")
        return self.state["projects"][name]

    def base(self, name):
        self.project(name)
        return self.home / "bots" / name

    def secrets(self):
        values = []
        def collect(v):
            if isinstance(v, dict):
                for x in v.values(): collect(x)
            elif isinstance(v, list):
                for x in v: collect(x)
            elif isinstance(v, str) and len(v) >= 8:
                values.append(v)
        collect({k: v for k, v in self.config.items() if "TOKEN" in k.upper()})
        for name in self.state["projects"]:
            collect(read_json(self.home / "bots" / name / "private" / "secrets.json", {}))
        return values

    def clean(self, value):
        return redact(value, self.secrets())

    async def command(self, args, cwd=None, env=None, timeout=300):
        def run():
            result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, errors="replace", timeout=timeout)
            if result.returncode:
                raise RuntimeError(self.clean(result.stderr[-3500:] or result.stdout[-3500:] or f"終了コード {result.returncode}"))
            return result.stdout
        return await asyncio.to_thread(run)

    def git_env(self):
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        token = self.config.get("GITHUB_TOKEN")
        if token:
            import base64
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                       GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + base64.b64encode(("x-access-token:" + token).encode()).decode())
        return env

    async def prepare(self, recipe, extra_packages=None):
        name = recipe["name"]
        base = self.home / "bots" / name
        release = base / "releases" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        release.mkdir(parents=True)
        repo = release / "repo"
        try:
            await self.command(["git", "clone", "--quiet", "--", recipe["repository"], str(repo)], env=self.git_env())
            if recipe.get("ref"):
                await self.command(["git", "checkout", "--quiet", recipe["ref"]], cwd=repo, env=self.git_env())
            commit = (await self.command(["git", "rev-parse", "HEAD"], cwd=repo)).strip()
            if not (repo / recipe["entrypoint"]).is_file():
                raise ValueError("起動ファイルが見つかりません")
            await self.command([sys.executable, "-m", "venv", str(release / ".venv")])
            python = release / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            await self.command([str(python), "-m", "pip", "install", "--upgrade", "pip"], timeout=300)
            requirements = repo / recipe["requirements"]
            if requirements.exists():
                await self.command([str(python), "-m", "pip", "install", "-r", str(requirements)], timeout=900)
            packages = extra_packages if extra_packages is not None else recipe.get("packages", [])
            if packages:
                await self.command([str(python), "-m", "pip", "install", *packages], timeout=900)
            await self.command([str(python), "-m", "pip", "check"])
            await self.command([str(python), "-m", "py_compile", str(repo / recipe["entrypoint"])])
            (release / "dependencies.txt").write_text(await self.command([str(python), "-m", "pip", "freeze"]), encoding="utf-8")
            write_json(release / "release.json", {"commit": commit, "recipe": recipe})
            self.attach_data(name, release, recipe)
            return str(release)
        except BaseException:
            shutil.rmtree(release)
            raise

    def attach_data(self, name, release, recipe):
        base = self.home / "bots" / name
        repo = Path(release) / "repo"
        for rel in recipe.get("mounts", []):
            rel = safe_relative(rel)
            target = base / "data" / rel
            link = repo / rel
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                if link.is_dir(): shutil.copytree(link, target)
                elif link.exists(): shutil.copy2(link, target)
                else: target.mkdir(parents=True)
            if link.is_dir() and not link.is_symlink(): shutil.rmtree(link)
            elif link.exists() or link.is_symlink(): link.unlink()
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target, target_is_directory=target.is_dir())
        secrets = read_json(base / "private" / "secrets.json", {})
        for rel, value in secrets.get("files", {}).items():
            rel = safe_relative(rel)
            target = base / "private" / "files" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, str): target.write_text(value, encoding="utf-8")
            else: write_json(target, value)
            target.chmod(0o600)
            link = repo / rel
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.exists() or link.is_symlink(): link.unlink()
            link.symlink_to(target)

    async def register(self, recipe):
        recipe = validate_recipe(recipe)
        name = recipe["name"]
        if name in self.state["projects"] and not self.state["projects"][name].get("unregistered"):
            raise ValueError("同名botが登録済みです")
        # Registration is persistent even if provisioning fails, so secrets can be supplied and retried.
        old = self.state["projects"].get(name, {})
        self.state["projects"][name] = {"recipe": recipe, "enabled": False, "active": old.get("active"), "previous": old.get("previous"), "error": ""}
        self.save()
        return await self.deploy(name, start=False)

    async def _stop(self, name):
        proc = self.processes.pop(name, None)
        if proc and proc.poll() is None:
            if os.name == "posix":
                with contextlib.suppress(ProcessLookupError): os.killpg(proc.pid, signal.SIGTERM)
            else: proc.terminate()
            try: await asyncio.to_thread(proc.wait, timeout=10)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    with contextlib.suppress(ProcessLookupError): os.killpg(proc.pid, signal.SIGKILL)
                else: proc.kill()
                await asyncio.to_thread(proc.wait)
        handle = self.log_handles.pop(name, None)
        if handle: handle.close()

    async def _start(self, name):
        p = self.project(name)
        if not p.get("active"): raise ValueError("botを先に導入してください")
        running = self.processes.get(name)
        if running and running.poll() is None: return
        await self._stop(name)
        release = Path(p["active"])
        r = p["recipe"]
        self.attach_data(name, release, r)
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in read_json(self.base(name) / "private" / "secrets.json", {}).get("env", {}).items()})
        ready = self.base(name) / "ready.json"
        with contextlib.suppress(FileNotFoundError): ready.unlink()
        env.update(PYTHONUNBUFFERED="1", SBC_MANAGED="1", SBC_READY_FILE=str(ready))
        logfile = self.base(name) / "bot.log"
        if logfile.exists() and logfile.stat().st_size > 5 * 1024 * 1024:
            logfile.replace(logfile.with_suffix(".log.1"))
        handle = open(logfile, "ab", buffering=0)
        self.log_handles[name] = handle
        python = release / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        try:
            self.processes[name] = subprocess.Popen([str(python), str(release / "repo" / r["entrypoint"]), *r.get("args", [])],
                cwd=release / "repo", env=env, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                start_new_session=(os.name == "posix"))
        except BaseException:
            handle.close(); self.log_handles.pop(name, None); raise
        self.last_started[name] = time.monotonic()

    async def start(self, name):
        async with self.lock(name):
            p = self.project(name)
            if p.get("unregistered"): raise ValueError("登録解除済みです。再登録してください")
            p["enabled"] = True; self.save()
            await self._start(name)
        return self.status(name)

    async def stop(self, name):
        async with self.lock(name):
            self.project(name)["enabled"] = False; self.save()
            await self._stop(name)
        return self.status(name)

    async def restart(self, name):
        async with self.lock(name):
            self.project(name)["enabled"] = True; self.save()
            await self._stop(name); await self._start(name)
        return self.status(name)

    async def deploy(self, name, start=None, packages=None, pinned_release=None):
        async with self.lock(name):
            p = self.project(name)
            r = copy.deepcopy(p["recipe"])
            if packages is not None: r["packages"] = packages
            active = p.get("active")
            if pinned_release:
                # A dependency upgrade keeps the currently deployed commit.
                r["ref"] = read_json(Path(pinned_release) / "release.json")["commit"]
            try:
                candidate = await self.prepare(r)
            except Exception as e:
                p["error"] = self.clean(str(e)); self.save(); raise
            enabled = p["enabled"] if start is None else start
            await self._stop(name)
            old_recipe = p["recipe"]
            p.update(active=candidate, enabled=enabled, recipe=r, error="")
            try:
                if enabled:
                    await self._start(name)
                    await asyncio.sleep(3)
                    if self.processes[name].poll() is not None:
                        raise RuntimeError("新しい版のプロセスが終了しました。ログを確認してください")
            except Exception as e:
                await self._stop(name)
                p.update(active=active, recipe=old_recipe, error=str(e))
                self.save()
                if active and enabled: await self._start(name)
                raise
            p["previous"] = active
            self.save()
        return self.status(name)

    async def rollback(self, name):
        async with self.lock(name):
            p = self.project(name)
            previous = p.get("previous")
            if not previous: raise ValueError("戻せる版がありません")
            await self._stop(name)
            p["active"], p["previous"] = previous, p["active"]
            p["recipe"] = read_json(Path(previous) / "release.json")["recipe"]
            self.save()
            if p["enabled"]: await self._start(name)
        return self.status(name)

    async def unregister(self, name):
        await self.stop(name)
        p = self.project(name)
        p["unregistered"] = True
        self.save()
        return {"name": name, "registered": False, "retained": True}

    def status(self, name=None):
        result = []
        for n, p in self.state["projects"].items():
            if name and n != name: continue
            proc = self.processes.get(n)
            active = p.get("active")
            release = read_json(Path(active) / "release.json", {}) if active else {}
            running = bool(proc and proc.poll() is None)
            ready = read_json(self.home / "bots" / n / "ready.json", {}) if running else {}
            result.append({"name": n, "registered": not p.get("unregistered", False), "enabled": p["enabled"],
                "process": "running" if running else "stopped", "discord": "connected" if ready else "not_verified",
                "pid": proc.pid if proc and proc.poll() is None else None, "commit": release.get("commit"),
                "error": self.clean(p.get("error", ""))})
        if name and not result: raise ValueError("登録されていないbotです")
        return result if name else {"manager": {"process": "running", "discord": self.discord_state}, "bots": result}

    def logs(self, name=None):
        path = self.base(name) / "bot.log" if name else self.home / "manager.log"
        if not path.exists(): return "ログはまだありません"
        with path.open("rb") as f:
            f.seek(max(0, path.stat().st_size - 128 * 1024))
            return self.clean(f.read().decode("utf-8", errors="replace"))

    def get_config(self, name=None):
        if not name: return self.clean(self.config)
        return self.clean({"recipe": self.project(name)["recipe"], "secrets": read_json(self.base(name) / "private" / "secrets.json", {})})

    async def set_config(self, value, name=None):
        if not isinstance(value, dict): raise ValueError("設定はJSON objectです")
        if not name:
            merged = merge_redacted(self.config, value)
            if not isinstance(merged.get("GUILD_ID"), int) or not merged.get("TOKEN") or not isinstance(merged.get("AUTHORIZED_LIST"), list):
                raise ValueError("TOKEN、整数のGUILD_ID、AUTHORIZED_LISTが必要です")
            self.backup()
            self.config = merged; write_json(self.config_path, merged)
            return {"saved": True, "restart_required": True}
        async with self.lock(name):
            p = self.project(name)
            self.backup(name)
            if "recipe" in value:
                r = validate_recipe(value["recipe"])
                if r["name"] != name: raise ValueError("bot名は変更できません")
                p["recipe"] = r
            if "secrets" in value:
                path = self.base(name) / "private" / "secrets.json"
                secret = merge_redacted(read_json(path, {}), value["secrets"])
                if not isinstance(secret.get("env", {}), dict) or not isinstance(secret.get("files", {}), dict):
                    raise ValueError("envとfilesはJSON objectです")
                for rel in secret.get("files", {}): safe_relative(rel)
                write_json(path, secret)
            self.save()
            if p["enabled"]:
                await self._stop(name); await self._start(name)
        return self.get_config(name)

    def backup(self, name=None):
        directory = self.home / "backups"
        directory.mkdir(exist_ok=True)
        backup_id = (name or "manager") + "-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        path = directory / (backup_id + ".zip")
        base = self.base(name) if name else self.home
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            roots = [base / "data", base / "private"] if name else [self.config_path, self.state_path]
            for root in roots:
                files = root.rglob("*") if root.is_dir() else [root]
                for f in files:
                    if f.is_file() and not f.is_symlink(): z.write(f, f.relative_to(base).as_posix())
        path.chmod(0o600)
        return {"backup": backup_id, "stored_on_sbc": True}

    async def restore(self, backup_id, name=None):
        if not re.fullmatch(r"[a-z0-9_-]+", backup_id): raise ValueError("バックアップIDが無効です")
        path = self.home / "backups" / (backup_id + ".zip")
        if not path.exists(): raise ValueError("バックアップがありません")
        expected = (name or "manager") + "-"
        if not backup_id.startswith(expected): raise ValueError("別の対象のバックアップです")
        lock = self.lock(name or "__manager__")
        async with lock:
            was_enabled = self.project(name)["enabled"] if name else False
            if name: await self._stop(name)
            else: await asyncio.gather(*(self._stop(n) for n in list(self.processes)))
            base = self.base(name) if name else self.home
            with zipfile.ZipFile(path) as z:
                for item in z.infolist():
                    rel = safe_relative(item.filename)
                    if name and Path(rel).parts[0] not in ("data", "private"): raise ValueError("バックアップ内容が無効です")
                    if not name and rel not in ("config.json", "registry.json"): raise ValueError("バックアップ内容が無効です")
                    dest = base / rel
                    if not dest.resolve().is_relative_to(base.resolve()): raise ValueError("管理領域外のパスです")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(z.read(item)); dest.chmod(0o600)
            if name and was_enabled: await self._start(name)
            if not name: self.config = read_json(self.config_path); self.state = read_json(self.state_path)
        return {"restored": backup_id, "restart_required": not bool(name)}

    async def diff(self, name):
        p = self.project(name); r = p["recipe"]
        if not p.get("active"): return "初回導入です"
        repo = Path(p["active"]) / "repo"
        await self.command(["git", "fetch", "--quiet", "origin"], cwd=repo, env=self.git_env())
        ref = r.get("ref") or "origin/HEAD"
        if ref and not ref.startswith("origin/") and not re.fullmatch(r"[a-f0-9]{40}", ref): ref = "origin/" + ref
        return await self.command(["git", "diff", "--stat", "HEAD", ref], cwd=repo)

    async def upgrade(self, name, library, version=None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?", library): raise ValueError("ライブラリ名が無効です")
        if version and not re.fullmatch(r"[A-Za-z0-9_.+-]+", version): raise ValueError("versionが無効です")
        p = self.project(name)
        if not p.get("active"): raise ValueError("稼働版がありません")
        freeze = (Path(p["active"]) / "dependencies.txt").read_text().splitlines()
        key = re.split(r"[\[=]", library)[0].lower().replace("_", "-")
        packages = [x for x in freeze if x and x.split("==")[0].lower().replace("_", "-") != key]
        packages.append(library + ("==" + version if version else ""))
        return await self.deploy(name, packages=packages, pinned_release=p["active"])

    async def monitor_one(self, name):
        async with self.lock(name):
            p = self.project(name)
            if self.closing or not p["enabled"] or p.get("unregistered") or not p.get("active"): return
            proc = self.processes.get(name)
            if proc and proc.poll() is None:
                if time.monotonic() - self.last_started.get(name, 0) > 60: self.failures[name] = 0
                return
            if proc:
                p["error"] = f"プロセス終了: {proc.returncode}"
            delay = min(300, 10 * 2 ** min(self.failures.get(name, 0), 5))
            if time.monotonic() - self.last_started.get(name, 0) < delay: return
            try:
                await self._start(name)
                self.failures[name] = self.failures.get(name, 0) + 1
            except Exception as e:
                p["error"] = self.clean(str(e)); self.last_started[name] = time.monotonic()
            self.save()

    async def monitor(self):
        while not self.closing:
            await asyncio.gather(*(self.monitor_one(n) for n in list(self.state["projects"])))
            await asyncio.sleep(2)

    async def close(self):
        self.closing = True
        await asyncio.gather(*(self._stop(n) for n in set(self.processes) | set(self.log_handles)))

    async def dispatch(self, action, name=None, **kwargs):
        if action in ("status", "logs", "get_config", "backup"):
            return getattr(self, action)(name)
        if action in ("start", "stop", "restart", "deploy", "rollback", "unregister", "diff"):
            return await getattr(self, action)(name)
        if action == "register": return await self.register(kwargs["recipe"])
        if action == "set_config": return await self.set_config(kwargs["value"], name)
        if action == "restore": return await self.restore(kwargs["backup_id"], name)
        if action == "upgrade": return await self.upgrade(name, kwargs["library"], kwargs.get("version"))
        raise ValueError("未対応の操作です")
