import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import psutil
from difflib import unified_diff
from urllib.request import urlopen
from logging.handlers import RotatingFileHandler

import discord
from discord.ext import commands
from discord import app_commands
from discord import File as DiscordFile

from typing import Any, Dict, List, Optional

# =====================================================
# 安全機能付きAutoMonitor
# - config.jsonのバリデーションとバックアップ自動復元
# - AutoMonitor自体の更新機能 (/pull_self)
# - 堅牢なGitHub URL生成
# - クラッシュ防止のための例外ハンドリング
# =====================================================

# ログ専用フォルダの作成
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# ログ設定
log_file_path = os.path.join(LOG_DIR, "monitor.log")
log_handler = RotatingFileHandler(log_file_path, maxBytes=5*1024*1024, backupCount=3)
logging.basicConfig(handlers=[log_handler], level=logging.INFO, format="%(asctime)s - %(message)s")

# AutoMonitor自体の設定（GitHubから更新可能にする）
AUTOMONITOR_GITHUB_PATH = "https://github.com/conei7/AutoMonitor"  # ★必要に応じて変更
AUTOMONITOR_LOCAL_PATH = os.path.abspath(__file__)

# 設定ファイルのパス
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
CONFIG_BACKUP_PATH = CONFIG_PATH + ".bak"
CONFIG_SAFE_BACKUP_PATH = CONFIG_PATH + ".safe"  # 最後に正常動作した設定

def validate_config(config_data: dict) -> tuple[bool, str]:
    """config.jsonのバリデーション"""
    required_keys = ["GUILD_ID", "TOKEN", "AUTHORIZED_LIST", "PROJECTS"]
    
    for key in required_keys:
        if key not in config_data:
            return False, f"必須キー '{key}' がありません"
    
    if not isinstance(config_data["GUILD_ID"], int):
        return False, "GUILD_ID は整数である必要があります"
    
    if not isinstance(config_data["TOKEN"], str) or len(config_data["TOKEN"]) < 50:
        return False, "TOKEN が無効です"
    
    if not isinstance(config_data["AUTHORIZED_LIST"], list):
        return False, "AUTHORIZED_LIST はリストである必要があります"
    
    if not isinstance(config_data["PROJECTS"], list):
        return False, "PROJECTS はリストである必要があります"
    
    for i, project in enumerate(config_data["PROJECTS"]):
        if "local_path" not in project:
            return False, f"PROJECTS[{i}] に 'local_path' がありません"
    
    return True, "OK"

def load_config_safely() -> dict:
    """安全にconfig.jsonを読み込む（失敗時はバックアップから復元）"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        
        is_valid, error_msg = validate_config(config)
        if not is_valid:
            raise ValueError(f"設定ファイルが無効: {error_msg}")
        
        # 正常に読み込めたら安全なバックアップを作成
        with open(CONFIG_SAFE_BACKUP_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        
        return config
    
    except Exception as e:
        logging.error(f"config.json読み込みエラー: {e}")
        
        # バックアップから復元を試みる
        for backup_path in [CONFIG_SAFE_BACKUP_PATH, CONFIG_BACKUP_PATH]:
            if os.path.exists(backup_path):
                try:
                    logging.info(f"バックアップから復元を試みます: {backup_path}")
                    with open(backup_path, "r", encoding="utf-8") as f:
                        config = json.load(f)
                    
                    is_valid, _ = validate_config(config)
                    if is_valid:
                        # バックアップからconfig.jsonを復元
                        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                            json.dump(config, f, ensure_ascii=False, indent=2)
                        logging.info("バックアップから復元しました")
                        return config
                except Exception as backup_error:
                    logging.error(f"バックアップ復元エラー: {backup_error}")
        
        raise RuntimeError("config.jsonの読み込みに失敗しました。バックアップも利用できません。")

# 設定ファイルの読み込み
config = load_config_safely()

CHECK_INTERVAL = config.get("CHECK_INTERVAL", 60)
GUILD_ID = config["GUILD_ID"]
TOKEN = config["TOKEN"]
AUTHORIZED_LIST = config["AUTHORIZED_LIST"]

# PROJECTSのlocal_pathは相対パスなので絶対パスに変換
PROJECTS = []
for p in config["PROJECTS"]:
    proj = p.copy()
    # local_pathを絶対パスに
    if not os.path.isabs(proj["local_path"]):
        proj["local_path"] = os.path.join(SCRIPT_DIR, proj["local_path"])
    PROJECTS.append(proj)

# argsのkeyを値に置換
for project in PROJECTS:
    if "args" in project:
        new_args = []
        for arg in project["args"]:
            value = project[arg] if arg in project else globals().get(arg, arg)

            if isinstance(value, dict):
                value = json.dumps(value, ensure_ascii=False)
            new_args.append(value)
        project["args"] = new_args

# PROJECTSの各辞書にnameをlocal_pathのファイル名（拡張子なし）で自動設定
for project in PROJECTS:
    if "local_path" in project:
        filename = os.path.basename(project["local_path"])
        name, _ = os.path.splitext(filename)
        project["name"] = name


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

def update_libraries() -> None:
    logging.info("Updating libraries...")
    for project in PROJECTS:
        for library in project.get("libraries", []):
            subprocess.run(["pip", "install", "--upgrade", library], check=True)

def kill_existing_process(script_path: str) -> None:
    """同じスクリプトを実行している既存プロセスを全てkill"""
    script_name = os.path.basename(script_path)
    current_pid = os.getpid()
    
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.pid == current_pid:
                continue
            cmdline = proc.info.get('cmdline') or []
            # コマンドラインにスクリプト名が含まれているか確認
            if any(script_name in str(arg) for arg in cmdline):
                logging.info(f"既存プロセスを終了: PID={proc.pid}, cmdline={cmdline}")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

def monitor_scripts() -> None:
    processes: Dict[str, Optional[subprocess.Popen]] = {project["name"]: None for project in PROJECTS}
    # 各プロセスの最後の再起動時間を記録（レート制限防止用）
    last_restart_time: Dict[str, float] = {project["name"]: 0 for project in PROJECTS}
    RESTART_COOLDOWN = 30  # 再起動間隔の最小秒数（Discordボットの安全な再起動のため）
    
    try:
        while True:
            for project in PROJECTS:
                name: str = project["name"]
                path: str = project["local_path"]
                args: List[str] = ["python", path]
                if project.get("args"):
                    args.extend([str(a) if not isinstance(a, list) else ",".join(map(str, a)) for a in project["args"]])
                
                if processes[name] is None or processes[name].poll() is not None:
                    current_time = time.time()
                    time_since_last_restart = current_time - last_restart_time[name]
                    
                    # 前回の再起動から十分な時間が経過しているか確認
                    if time_since_last_restart < RESTART_COOLDOWN:
                        wait_time = RESTART_COOLDOWN - time_since_last_restart
                        logging.info(f"{name}: 再起動待機中... あと {wait_time:.1f}秒")
                        time.sleep(wait_time)
                    
                    # 既存のプロセスが残っていれば確実に終了させる（システム全体で）
                    kill_existing_process(path)
                    
                    if processes[name] is not None:
                        try:
                            processes[name].terminate()
                            processes[name].wait(timeout=5)
                        except Exception as e:
                            logging.warning(f"{name}: プロセス終了待機中にエラー: {e}")
                            try:
                                processes[name].kill()
                            except:
                                pass
                    
                    logging.warning(f"{name} stopped. Restarting...")
                    processes[name] = subprocess.Popen(args)
                    last_restart_time[name] = time.time()
                    
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        logging.info("Monitoring stopped by user.")
    finally:
        for name, process in processes.items():
            if process:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except:
                    try:
                        process.kill()
                    except:
                        pass

# GitHubのrawファイルURLを生成（堅牢版）
def get_github_raw_url(github_path: str, local_path: str, github_file_path: str = None) -> Optional[str]:
    """
    GitHubリポジトリURLからrawファイルURLを生成
    対応形式:
    - https://github.com/user/repo
    - https://github.com/user/repo.git
    - https://github.com/user/repo/
    """
    if not github_path:
        return None
    
    # .git と末尾の / を除去
    cleaned_path = github_path.rstrip('/').removesuffix('.git')
    
    # パターンマッチング
    patterns = [
        r"https://github\.com/([^/]+)/([^/]+)$",
        r"https://github\.com/([^/]+)/([^/]+?)/?$",
    ]
    
    for pattern in patterns:
        m = re.match(pattern, cleaned_path)
        if m:
            user, repo = m.group(1), m.group(2)
            # github_file_pathがあればそれを使用、なければlocal_pathのbasenameを使用
            if github_file_path:
                file_path = github_file_path
            else:
                file_path = os.path.basename(local_path)
            return f"https://raw.githubusercontent.com/{user}/{repo}/main/{file_path}"
    
    return None

def fetch_github_file(github_url: str, timeout: int = 30) -> Optional[str]:
    """GitHubからファイルを取得（タイムアウト付き）"""
    if not github_url:
        return None
    
    try:
        req = urllib.request.Request(github_url)
        req.add_header('User-Agent', 'AutoMonitor/1.0')
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except Exception as e:
        logging.error(f"GitHubファイル取得エラー: {e}")
        return None

class Main(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def is_authorized(self, user_id: int) -> bool:
        return user_id in AUTHORIZED_LIST

    @app_commands.command(name="reboot_self", description="この監視Bot自身を再起動します")
    @app_commands.guilds(int(GUILD_ID))
    async def reboot_self_command(self, interaction: discord.Interaction) -> None:
        if not self.is_authorized(interaction.user.id):
            await interaction.response.send_message("あなたには実行権限がありません。", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send("Botを再起動します...", ephemeral=True)
            python = sys.executable
            os.execl(python, python, *sys.argv)
        except Exception as e:
            logging.error(f"reboot_self エラー: {e}")
            await interaction.followup.send(f"再起動エラー: {e}", ephemeral=True)

    @app_commands.command(name="get_config", description="現在のconfig.jsonを取得します")
    @app_commands.guilds(int(GUILD_ID))
    async def get_config_command(self, interaction: discord.Interaction) -> None:
        if not self.is_authorized(interaction.user.id):
            await interaction.response.send_message("あなたには実行権限がありません。", ephemeral=True)
            return
        try:
            await interaction.response.send_message("config.jsonを送信します。", ephemeral=True)
            await interaction.followup.send(file=DiscordFile(CONFIG_PATH, filename="config.json"), ephemeral=True)
        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"送信中にエラー: {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"送信中にエラー: {e}", ephemeral=True)

    @app_commands.command(name="set_config", description="新しいconfig.jsonをアップロードして反映します（安全機能付き）")
    @app_commands.guilds(int(GUILD_ID))
    @app_commands.describe(file="新しいconfig.jsonファイル")
    async def set_config_command(self, interaction: discord.Interaction, file: discord.Attachment) -> None:
        if not self.is_authorized(interaction.user.id):
            await interaction.response.send_message("あなたには実行権限がありません。", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        temp_path = CONFIG_PATH + ".tmp"
        
        try:
            # ファイルを一時保存
            await file.save(temp_path)
            
            # JSONとしてパースできるか確認
            try:
                with open(temp_path, "r", encoding="utf-8") as f:
                    new_config = json.load(f)
            except json.JSONDecodeError as e:
                await interaction.followup.send(f"❌ JSONパースエラー: {e}\n設定は変更されませんでした。", ephemeral=True)
                os.remove(temp_path)
                return
            
            # バリデーション
            is_valid, error_msg = validate_config(new_config)
            if not is_valid:
                await interaction.followup.send(f"❌ 設定ファイルが無効です: {error_msg}\n設定は変更されませんでした。", ephemeral=True)
                os.remove(temp_path)
                return
            
            # 現在の設定をバックアップ
            if os.path.exists(CONFIG_PATH):
                # 安全なバックアップを作成（正常動作中の設定）
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    current_config = f.read()
                with open(CONFIG_SAFE_BACKUP_PATH, "w", encoding="utf-8") as f:
                    f.write(current_config)
                # 通常のバックアップも作成
                os.replace(CONFIG_PATH, CONFIG_BACKUP_PATH)
            
            # 新しい設定を適用
            os.replace(temp_path, CONFIG_PATH)
            
            await interaction.followup.send("✅ config.jsonを更新しました。Botを再起動します...\n（問題があれば自動的にバックアップから復元されます）", ephemeral=True)
            
            # 再起動処理
            python = sys.executable
            os.execl(python, python, *sys.argv)
            
        except Exception as e:
            tb = traceback.format_exc()
            await interaction.followup.send(f"❌ エラーが発生しました: {e}\n```{tb}```\n設定は変更されませんでした。", ephemeral=True)
            # 一時ファイルを削除
            if os.path.exists(temp_path):
                os.remove(temp_path)

    @app_commands.command(name="pull_self", description="AutoMonitor自体をGitHubから更新します")
    @app_commands.guilds(int(GUILD_ID))
    async def pull_self_command(self, interaction: discord.Interaction) -> None:
        """AutoMonitor自体を更新するコマンド"""
        if not self.is_authorized(interaction.user.id):
            await interaction.response.send_message("あなたには実行権限がありません。", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            github_url = get_github_raw_url(AUTOMONITOR_GITHUB_PATH, AUTOMONITOR_LOCAL_PATH)
            
            if not github_url:
                await interaction.followup.send(
                    f"❌ GitHub URLの生成に失敗しました。\n"
                    f"AUTOMONITOR_GITHUB_PATH: {AUTOMONITOR_GITHUB_PATH}\n"
                    f"コード内の AUTOMONITOR_GITHUB_PATH を正しいリポジトリURLに設定してください。",
                    ephemeral=True
                )
                return
            
            # 現在のコードを読み込み
            with open(AUTOMONITOR_LOCAL_PATH, "r", encoding="utf-8") as f:
                local_code = f.read()
            
            # GitHubからコードを取得
            github_code = fetch_github_file(github_url)
            
            if github_code is None:
                await interaction.followup.send(
                    f"❌ GitHubからのファイル取得に失敗しました。\n"
                    f"URL: {github_url}\n"
                    f"リポジトリが存在し、mainブランチにAutoMonitor.pyがあることを確認してください。",
                    ephemeral=True
                )
                return
            
            # 差分を計算
            if local_code == github_code:
                await interaction.followup.send("✅ AutoMonitorに変更点はありません。最新版です。", ephemeral=True)
                return
            
            diff_lines = list(unified_diff(
                local_code.splitlines(),
                github_code.splitlines(),
                fromfile="local",
                tofile="github",
                lineterm=''))
            diff = '\n'.join(diff_lines)
            
            # 一時保存
            tmp_path = os.path.join(LOG_DIR, "AutoMonitor_github_tmp.txt")
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(github_code)
            
            # 差分を表示
            if len(diff) > 1800:
                diff_file = discord.File(io.BytesIO(diff.encode('utf-8')), filename="AutoMonitor_diff.txt")
                await interaction.followup.send("AutoMonitorの差分が長いため、ファイルとして送信します。", file=diff_file, ephemeral=True)
            else:
                await interaction.followup.send(f"**AutoMonitorの差分:**\n```diff\n{diff[:1800]}```", ephemeral=True)
            
            # 確認ボタンを表示
            await interaction.followup.send(
                "⚠️ AutoMonitor自体を更新しますか？\n（更新後は自動的に再起動されます）",
                view=AutoMonitorUpdateConfirmView(),
                ephemeral=True
            )
            
        except Exception as e:
            tb = traceback.format_exc()
            await interaction.followup.send(f"❌ エラーが発生しました: {e}\n```{tb}```", ephemeral=True)

    @app_commands.command(name="pull", description="指定したプロジェクトの最新変更をGitHubからpullします。")
    @app_commands.guilds(int(GUILD_ID))
    @app_commands.describe(project="pullしたいプロジェクト名（必須）")
    async def pull_command(self, interaction: discord.Interaction, project: str) -> None:
        if not self.is_authorized(interaction.user.id):
            await interaction.response.send_message("あなたには実行権限がありません。", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            project_names = [p["name"] for p in PROJECTS]
            if project not in project_names:
                await interaction.followup.send("該当するプロジェクトが見つかりません。", ephemeral=True)
                return
            
            for p in PROJECTS:
                if p["name"] == project:
                    if not p.get("github_path"):
                        await interaction.followup.send("このプロジェクトはGitHub連携されていません。", ephemeral=True)
                        return

                    # パス設定
                    local_path = p["local_path"]
                    github_url = get_github_raw_url(p["github_path"], p["local_path"], p.get("github_file_path"))

                    # GitHub URLが生成できなかった場合
                    if not github_url:
                        await interaction.followup.send(
                            f"❌ GitHub URLの生成に失敗しました。\n"
                            f"github_path: {p['github_path']}\n"
                            f"対応形式: https://github.com/user/repo または https://github.com/user/repo.git",
                            ephemeral=True
                        )
                        return

                    # 各ソースコードの取得
                    local_code = ""
                    if os.path.exists(local_path):
                        try:
                            with open(local_path, "r", encoding="utf-8") as f:
                                local_code = f.read()
                        except Exception as e:
                            await interaction.followup.send(f"❌ ローカルファイルの読み込みエラー: {e}", ephemeral=True)
                            return
                    
                    # GitHubからコードを取得
                    github_code = fetch_github_file(github_url)
                    
                    if github_code is None:
                        await interaction.followup.send(
                            f"❌ GitHubからのファイル取得に失敗しました。\n"
                            f"URL: {github_url}\n"
                            f"リポジトリとブランチ（main）を確認してください。",
                            ephemeral=True
                        )
                        return

                    # 一時保存
                    tmp_path = os.path.join(LOG_DIR, f"{p['name']}_github_tmp.txt")
                    with open(tmp_path, "w", encoding="utf-8") as tmpf:
                        tmpf.write(github_code)

                    # ローカルとGitHubの差分を計算
                    if local_code == github_code:
                        await interaction.followup.send(f"✅ {p['name']} に変更点はありません。最新版です。", ephemeral=True)
                        return

                    diff_lines = list(unified_diff(
                        local_code.splitlines(),
                        github_code.splitlines(),
                        fromfile="local_file",
                        tofile="github_latest",
                        lineterm=''))
                    diff = '\n'.join(diff_lines)

                    # メッセージ作成
                    final_message = f"**[ローカル vs GitHub]**\n```diff\n{diff}```"

                    # メッセージが長すぎる場合の対策
                    if len(final_message) > 1900:
                        diff_file = discord.File(io.BytesIO(diff.encode('utf-8')), filename=f"{p['name']}_diff.txt")
                        await interaction.followup.send(f"{p['name']} の差分が長すぎるため、ファイルとして送信します。", file=diff_file, ephemeral=True)
                    else:
                        await interaction.followup.send(f"{p['name']} の差分情報:\n{final_message}", ephemeral=True)

                    await interaction.followup.send(f"{p['name']} をGitHubの最新版で上書き更新しますか？", view=UpdateConfirmView(p), ephemeral=True)
                    return
                    
        except Exception as e:
            tb = traceback.format_exc()
            await interaction.followup.send(f"❌ エラーが発生しました: {e}\n```{tb}```", ephemeral=True)


    @pull_command.autocomplete("project")
    async def pull_project_autocomplete(self, interaction: discord.Interaction, current: str) -> List[discord.app_commands.Choice[str]]:
        return [
            discord.app_commands.Choice(name=p["name"], value=p["name"])
            for p in PROJECTS if current.lower() in p["name"].lower()
        ]

    @app_commands.command(name="reboot", description="指定したプロジェクトのみ再起動します。")
    @app_commands.guilds(int(GUILD_ID))
    @app_commands.describe(project="再起動したいプロジェクト名（必須）")
    async def reboot_command(
        self,
        interaction: discord.Interaction,
        project: str,
    ) -> None:
        if not self.is_authorized(interaction.user.id):
            await interaction.response.send_message("あなたには実行権限がありません。", ephemeral=True)
            return
        try:
            # 入力候補リストを作成
            project_names = [p["name"] for p in PROJECTS]
            if project not in project_names:
                await interaction.response.send_message("該当するプロジェクトが見つかりません。", ephemeral=True)
                return
            for p in PROJECTS:
                if p["name"] == project:
                    logging.info(f"Restarting script: {p['name']}")
                    args = ["python", p["local_path"]]
                    if p.get("args"):
                        args.extend([str(a) if not isinstance(a, list) else ",".join(map(str, a)) for a in p["args"]])
                    subprocess.Popen(args)
                    await interaction.response.send_message(f"{p['name']} を再起動しました。", ephemeral=True)
                    return
        except Exception as e:
            logging.error(f"Failed to restart script: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Failed to restart script: {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"Failed to restart script: {e}", ephemeral=True)

    @reboot_command.autocomplete("project")
    async def reboot_project_autocomplete(self, interaction: discord.Interaction, current: str) -> List[discord.app_commands.Choice[str]]:
        return [
            discord.app_commands.Choice(name=p["name"], value=p["name"])
            for p in PROJECTS if current.lower() in p["name"].lower()
        ]

    @app_commands.command(name="get_logs", description="ログファイルを取得します")
    @app_commands.guilds(int(GUILD_ID))
    async def get_logs_command(self, interaction: discord.Interaction) -> None:
        if not self.is_authorized(interaction.user.id):
            await interaction.response.send_message("あなたには実行権限がありません。", ephemeral=True)
            return
        try:
            log_path = os.path.join(LOG_DIR, "monitor.log")
            if os.path.exists(log_path):
                await interaction.response.send_message("ログをファイルとして送信します。", ephemeral=True)
                await interaction.followup.send(file=discord.File(log_path, "monitor.log"), ephemeral=True)
            else:
                await interaction.response.send_message("ログファイルが見つかりません。", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"エラーが発生しました: {e}", ephemeral=True)

    @app_commands.command(name="upgrade", description="指定したライブラリをアップグレードします（バージョン指定可）")
    @app_commands.guilds(int(GUILD_ID))
    @app_commands.describe(library="アップグレードしたいライブラリ名", version="バージョン（省略可）")
    async def upgrade_command(self, interaction: discord.Interaction, library: str, version: Optional[str] = None) -> None:
        if not self.is_authorized(interaction.user.id):
            await interaction.response.send_message("あなたには実行権限がありません。", ephemeral=True)
            return
        try:
            await interaction.response.defer(ephemeral=True)
            if version:
                cmd = ["pip", "install", f"{library}=={version}"]
            else:
                cmd = ["pip", "install", "--upgrade", library]
            result = subprocess.run(cmd, capture_output=True, text=True)
            await interaction.followup.send(f"コマンド: {' '.join(cmd)}\n```\n{result.stdout or result.stderr}\n```", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"エラーが発生しました: {e}", ephemeral=True)

    @upgrade_command.autocomplete("library")
    async def upgrade_library_autocomplete(self, interaction: discord.Interaction, current: str) -> List[discord.app_commands.Choice[str]]:
        # すべてのPROJECTSの"libraries"をまとめて重複なしリスト化
        all_libs = set()
        for p in PROJECTS:
            all_libs.update(p.get("libraries", []))
        return [
            discord.app_commands.Choice(name=lib, value=lib)
            for lib in all_libs if current.lower() in lib.lower()
        ]

    @app_commands.command(name="restore_config", description="config.jsonをバックアップから復元します")
    @app_commands.guilds(int(GUILD_ID))
    async def restore_config_command(self, interaction: discord.Interaction) -> None:
        """config.jsonをバックアップから復元するコマンド"""
        if not self.is_authorized(interaction.user.id):
            await interaction.response.send_message("あなたには実行権限がありません。", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        # 利用可能なバックアップを確認
        backups = []
        if os.path.exists(CONFIG_SAFE_BACKUP_PATH):
            backups.append(("安全なバックアップ (.safe)", CONFIG_SAFE_BACKUP_PATH))
        if os.path.exists(CONFIG_BACKUP_PATH):
            backups.append(("直前のバックアップ (.bak)", CONFIG_BACKUP_PATH))
        
        if not backups:
            await interaction.followup.send("❌ 利用可能なバックアップがありません。", ephemeral=True)
            return
        
        backup_info = "\n".join([f"- {name}" for name, _ in backups])
        await interaction.followup.send(
            f"📁 利用可能なバックアップ:\n{backup_info}",
            view=RestoreConfigView(backups),
            ephemeral=True
        )


# AutoMonitor更新確認ビュー
class AutoMonitorUpdateConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="✅ 更新して再起動", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        tmp_path = os.path.join(LOG_DIR, "AutoMonitor_github_tmp.txt")
        
        if not os.path.exists(tmp_path):
            await interaction.response.send_message("❌ 一時保存ファイルが見つかりません。pull_selfコマンドを再度実行してください。", ephemeral=True)
            return
        
        try:
            # GitHubのコードを読み込み
            with open(tmp_path, "r", encoding="utf-8") as f:
                github_code = f.read()
            
            # 現在のファイルをバックアップ
            backup_path = AUTOMONITOR_LOCAL_PATH + ".bak"
            with open(AUTOMONITOR_LOCAL_PATH, "r", encoding="utf-8") as f:
                current_code = f.read()
            with open(backup_path, "w", encoding="utf-8") as f:
                f.write(current_code)
            
            # 新しいコードを書き込み
            with open(AUTOMONITOR_LOCAL_PATH, "w", encoding="utf-8") as f:
                f.write(github_code)
            
            # 一時ファイル削除
            os.remove(tmp_path)
            
            await interaction.response.send_message("✅ AutoMonitorを更新しました。再起動します...", ephemeral=True)
            
            # 再起動
            python = sys.executable
            os.execl(python, python, *sys.argv)
            
        except Exception as e:
            tb = traceback.format_exc()
            await interaction.response.send_message(f"❌ 更新中にエラーが発生しました: {e}\n```{tb}```", ephemeral=True)

    @discord.ui.button(label="❌ キャンセル", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("AutoMonitorの更新をキャンセルしました。", ephemeral=True)


# プロジェクト更新確認ビュー
class UpdateConfirmView(discord.ui.View):
    def __init__(self, project):
        super().__init__(timeout=300)
        self.project = project

    @discord.ui.button(label="変更を反映 & 再起動", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        project_name = self.project["name"]
        local_path = self.project["local_path"]
        tmp_path = os.path.join(LOG_DIR, f"{project_name}_github_tmp.txt")
        
        if not os.path.exists(tmp_path):
            msg = "❌ 一時保存ファイルが見つかりません。pullコマンドを再度実行してください。"
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return
        
        try:
            with open(tmp_path, "r", encoding="utf-8") as tmpf:
                github_code = tmpf.read()
            
            # バックアップを作成
            if os.path.exists(local_path):
                backup_path = local_path + ".bak"
                with open(local_path, "r", encoding="utf-8") as f:
                    with open(backup_path, "w", encoding="utf-8") as bf:
                        bf.write(f.read())
            
            # ディレクトリがなければ作成
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="utf-8") as f:
                f.write(github_code)
            
            # 一時ファイル削除
            os.remove(tmp_path)
            
            # プロセス再起動
            logging.info(f"Restarting script after update: {project_name}")
            args = ["python", local_path]
            if self.project.get("args"):
                args.extend([str(a) if not isinstance(a, list) else ",".join(map(str, a)) for a in self.project["args"]])
            subprocess.Popen(args)
            
            if not interaction.response.is_done():
                await interaction.response.send_message(f"✅ {project_name} を更新し、再起動しました。", ephemeral=True)
            else:
                await interaction.followup.send(f"✅ {project_name} を更新し、再起動しました。", ephemeral=True)
                
        except Exception as e:
            tb = traceback.format_exc()
            msg = f"❌ 更新・再起動中にエラーが発生しました: {e}\n```{tb}```"
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                try:
                    await interaction.followup.send(msg, ephemeral=True)
                except:
                    pass

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"{self.project['name']} の更新をキャンセルしました。", ephemeral=True)


# config復元ビュー
class RestoreConfigView(discord.ui.View):
    def __init__(self, backups: list):
        super().__init__(timeout=300)
        self.backups = backups
        
        # バックアップごとにボタンを追加
        for i, (name, path) in enumerate(backups):
            button = discord.ui.Button(
                label=name,
                style=discord.ButtonStyle.primary,
                custom_id=f"restore_{i}"
            )
            button.callback = self.make_callback(path, name)
            self.add_item(button)
    
    def make_callback(self, path: str, name: str):
        async def callback(interaction: discord.Interaction):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    backup_config = json.load(f)
                
                is_valid, error_msg = validate_config(backup_config)
                if not is_valid:
                    await interaction.response.send_message(f"❌ バックアップが無効です: {error_msg}", ephemeral=True)
                    return
                
                # 復元
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(backup_config, f, ensure_ascii=False, indent=2)
                
                await interaction.response.send_message(f"✅ {name}から復元しました。再起動します...", ephemeral=True)
                
                python = sys.executable
                os.execl(python, python, *sys.argv)
                
            except Exception as e:
                await interaction.response.send_message(f"❌ 復元エラー: {e}", ephemeral=True)
        
        return callback


@bot.event
async def on_ready():
    try:
        main = Main(bot)
        await bot.add_cog(main)
        await main.bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        logging.info(f"Bot ready: {bot.user}")
    except Exception as e:
        logging.error(f"on_ready エラー: {e}")


if __name__ == "__main__":
    try:
        t = threading.Thread(target=monitor_scripts, daemon=True)
        t.start()
    except Exception as e:
        logging.error(f"An error occurred: {e}")

    try:
        bot.run(TOKEN)
    except Exception as e:
        logging.error(f"Bot起動エラー: {e}")
        # 設定に問題がある場合、バックアップから復元を試みる
        try:
            config = load_config_safely()
            TOKEN = config["TOKEN"]
            bot.run(TOKEN)
        except:
            logging.error("復旧できませんでした")