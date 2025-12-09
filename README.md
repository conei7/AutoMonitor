# AutoMonitor

Discordボットの自動監視・再起動システム

## 機能

- **プロセス監視**: 登録されたPythonスクリプトを監視し、停止したら自動的に再起動
- **GitHub連携**: `/pull`コマンドでGitHubから最新のコードを取得して更新
- **自己更新**: `/pull_self`コマンドでAutoMonitor自体を更新
- **安全機能**: config.jsonのバリデーション、自動バックアップ・復元

## セットアップ

1. `config.example.json`を`config.json`にコピー
2. 必要な情報を設定:
   - `TOKEN`: AutoMonitorのDiscordボットトークン
   - `GUILD_ID`: 対象のサーバーID
   - `AUTHORIZED_LIST`: 操作を許可するユーザーIDのリスト
   - `PROJECTS`: 監視するプロジェクトの設定

3. 依存関係をインストール:
   ```bash
   pip install discord.py
   ```

4. 起動:
   ```bash
   python AutoMonitor.py
   ```

## Discordコマンド

| コマンド | 説明 |
|---------|------|
| `/reboot_self` | AutoMonitorを再起動 |
| `/reboot <project>` | 指定したプロジェクトを再起動 |
| `/pull <project>` | GitHubから最新コードを取得して更新 |
| `/pull_self` | AutoMonitor自体をGitHubから更新 |
| `/get_config` | 現在のconfig.jsonを取得 |
| `/set_config` | 新しいconfig.jsonをアップロード（バリデーション付き） |
| `/restore_config` | バックアップからconfig.jsonを復元 |
| `/get_logs` | ログファイルを取得 |
| `/upgrade <library>` | ライブラリをアップグレード |

## 安全機能

- **config.jsonバリデーション**: 不正な設定ファイルをアップロードしても適用されない
- **自動バックアップ**: 正常動作時の設定を自動保存
- **自動復元**: 起動時にconfig.jsonが壊れていればバックアップから自動復元

## ライセンス

MIT License
