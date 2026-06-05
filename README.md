# Discord → Anthropic Bot

Discord でメンションすると、その内容を Anthropic API（Claude）に転送して返答する Bot。
Discord の **Gateway（WebSocket）に接続して `on_message` で受ける**方式なので、
公開HTTPSエンドポイントやドメインは不要。

## ファイル構成

```
discord-bot/
├── main.py                     # Gateway ボット本体（discord.py）
├── requirements.txt
└── deploy/
    ├── setup.sh                # GCE VM 初期セットアップスクリプト
    ├── discord-bot.service     # systemd ユニット
    └── env.conf.example        # systemd ドロップイン（環境変数）テンプレート
```

## 仕組み

- `discord.py` で Gateway に接続（アウトバウンドのWebSocketのみ）
- `on_message` で **Bot へのメンション**を検知 → メンション表記を除去 → Claude に問い合わせ → 返信
- 2000文字を超える返答は自動で分割送信

公開ポートを開けないため、リバースプロキシ・TLS証明書・Interactions Endpoint URL は一切不要。

## セットアップ手順

### 1. Discord Application を作成

1. [Discord Developer Portal](https://discord.com/developers/applications) を開く
2. **New Application** → アプリ名を入力
3. **Bot** タブ → **Add Bot** → `TOKEN` をコピー（`.env` の `DISCORD_BOT_TOKEN` に設定）
4. **Bot** タブ → **MESSAGE CONTENT INTENT** を **ON** にする（メンション本文の取得に必須）
5. **OAuth2 → URL Generator** → スコープ `bot` + 権限 `Send Messages`, `Read Message History`
   → 生成されたURLでサーバーに招待

### 2. Google Compute Engine (GCE) にデプロイ

Gateway 方式なので待ち受けポートは不要。**systemd で常駐させるだけ**でよい。

#### 2-1. VM インスタンスを作成

```bash
# gcloud CLI で作成（us-central1 は Always Free 対象リージョン）
gcloud compute instances create discord-bot \
  --machine-type=e2-micro \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --zone=us-central1-a
```

> インバウンドのファイアウォール開放は不要（Bot は外向きに接続するだけ）。
> SSH 用の `default-allow-ssh` があれば十分。

#### 2-2. VM 上でセットアップ

```bash
# VM に SSH 接続
gcloud compute ssh discord-bot --zone=us-central1-a

# セットアップスクリプトを実行（Python・systemd を一括構成）
#   REPO_URL は自分のリポジトリに置き換える
sudo REPO_URL="https://github.com/<your-account>/discord_bot.git" bash -c \
  "$(curl -fsSL https://raw.githubusercontent.com/<your-account>/discord_bot/main/deploy/setup.sh)"
```

> リポジトリが private、または手元から転送したい場合は `deploy/setup.sh` を
> `scp` で送って `sudo bash setup.sh` でもよい。

### 3. 環境変数を設定

環境変数は **systemd のドロップイン**で渡す（`setup.sh` がテンプレートを
`/etc/systemd/system/discord-bot.service.d/env.conf` に配置済み）。
シークレットはリポジトリ外（`/etc`）に置かれるため、`git pull` の影響を受けない。

```bash
sudo nano /etc/systemd/system/discord-bot.service.d/env.conf
```

| 変数 | 必須 | 説明 |
|------|------|------|
| `DISCORD_BOT_TOKEN` | ✅ | Bot トークン |
| `ANTHROPIC_API_KEY` | ✅ | Anthropic API キー |
| `ANTHROPIC_MODEL` | 任意 | モデル名（既定 `claude-sonnet-4-6`） |
| `ANTHROPIC_BASE_URL` | 任意 | Anthropic API のベース URL（未設定時は SDK デフォルト） |
| `SYSTEM_PROMPT` | 任意 | システムプロンプト（スペースを含む値は `Environment="KEY=..."` と全体をクオート） |

### 4. 起動

```bash
sudo systemctl daemon-reload      # env.conf を編集したら必要
sudo systemctl restart discord-bot

# ログ確認（"✅ ログイン完了" が出れば成功）
sudo journalctl -u discord-bot -f
```

Discord 上で Bot にメンション（`@BotName こんにちは`）すると返信が来る。

#### コード更新時の再デプロイ

```bash
cd /opt/discord_bot
sudo git pull
sudo /opt/discord_bot/.venv/bin/pip install -r requirements.txt   # 依存が変わった時のみ
sudo systemctl restart discord-bot
```

## ローカルでの動作確認

環境変数はシェルで直接セットして起動する（`.env` ファイルは使わない）。

PowerShell:

```powershell
pip install -r requirements.txt
$env:DISCORD_BOT_TOKEN = "..."
$env:ANTHROPIC_API_KEY = "..."
$env:ANTHROPIC_MODEL  = "claude-sonnet-4-6"   # 省略可
python main.py
```

bash:

```bash
pip install -r requirements.txt
export DISCORD_BOT_TOKEN="..."
export ANTHROPIC_API_KEY="..."
export ANTHROPIC_MODEL="claude-sonnet-4-6"    # 省略可
python main.py
```

トンネル（ngrok 等）は不要。`python main.py` で起動すればそのまま Gateway に接続する。
