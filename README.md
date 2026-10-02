# AutoMonitor

DiscordとGitHubを入口に、botの導入・設定・起動停止・更新・依存環境・ログ・復旧を管理します。

## 現行ランタイム

Python 3.10以上、Linux、git、python3-venvを使用します。`pip install -r requirements.txt`で管理bot自身の依存を導入します。
`AUTOMONITOR_HOME`に運用データの絶対パスを指定し、その中の`config.json`へ`TOKEN`、整数の`GUILD_ID`、許可ユーザーID配列の`AUTHORIZED_LIST`を設定します。非公開リポジトリには任意の`GITHUB_TOKEN`（対象リポジトリの読み取り権限）を設定できます。

`python AutoMonitor.py serve`で起動します。秘密設定・registry・botの版別仮想環境・保存データ・バックアップはこのホーム配下へ保存します。Discordに接続できない場合も、同じ所有者のUnix socketによる保守と子botの監視は継続します。

PC/SSHからはJSONを標準入力で渡します。例：

```bash
printf '%s\n' '{"action":"status"}' | python AutoMonitor.py ctl
```

対応操作は`register/status/start/stop/restart/deploy/rollback/unregister/logs/get_config/set_config/backup/restore/upgrade/diff`です。個別操作は`name`、登録は`recipe`、設定反映は`value`を指定します。起動停止はDiscordと同じ管理処理を使用します。

`/register`のフォームでGitHub URLと起動ファイルを指定し、`/set_secret`または`/set_config`で設定してから`/start`します。初回は自動起動しません。`/unregister`はデータを保持して停止します。明示的な停止状態は再起動後も維持されます。

`/pull`は差分確認後にリポジトリ全体と仮想環境を新しい版として準備し、起動失敗時は旧版へ戻します。`/upgrade`も対象botの環境だけを準備して切り替えます。設定・データは`mounts`と秘密ファイル設定で版から分離できます。

`/get_config`の秘密値は`***`になり、そのまま再入力すると既存値を保持します。`/backup`は秘密を含むバックアップをSBC内だけへ保存し、IDを返します。`/restore_config backup_id:...`で復元します。管理bot設定の反映・復元後は`/reboot_self`を使用します。

各botの設定JSONは`{"recipe": {...}, "secrets": {"env": {...}, "files": {...}}}`です。`files`のキーはリポジトリ内の相対パス、値はJSON objectまたはテキストです。`mounts`は永続化するリポジトリ内の相対パスの配列です。トークンを起動引数に入れず、環境変数か秘密ファイルを使ってください。

`/status`はプロセス稼働とDiscord接続を区別し、汎用botのDiscordログインは未確認として表示します。詳細はログを確認します。全コマンドと確認ボタンは許可ユーザーだけが操作できます。

自己更新はGitHubのAutoMonitor全体を取得し、テスト後に稼働版を切り替えます。SBC用のサービス／launcherは運用リポジトリ側で管理します。旧`config.json`の管理bot用TOKEN/GUILD_ID/AUTHORIZED_LISTはそのまま使用できます。旧PROJECTSは現行recipeへの移行が必要です。

## 旧版の参考

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
