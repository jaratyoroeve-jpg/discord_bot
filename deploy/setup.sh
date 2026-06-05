#!/usr/bin/env bash
# ──────────────────────────────────────────────
# GCE VM (Debian / Ubuntu) 初期セットアップスクリプト
# VM に SSH 接続したあと、root 権限で実行する想定。
#   curl -fsSL <このファイルのURL> | sudo REPO_URL=... bash
# もしくは scp で転送して  sudo bash setup.sh
#
# Gateway(on_message) 方式なので公開ポート・HTTPS・ドメインは不要。
# ──────────────────────────────────────────────
set -euo pipefail

APP_DIR=/opt/discord_bot
APP_USER=botuser
REPO_URL="${REPO_URL:-https://github.com/<your-account>/discord_bot.git}"

echo "==> パッケージ更新 & Python / git インストール"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git

echo "==> 実行用ユーザー作成"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
fi

echo "==> リポジトリ取得 / 更新"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

echo "==> Python 仮想環境 & 依存インストール"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> systemd ユニット配置"
cp "$APP_DIR/deploy/discord-bot.service" /etc/systemd/system/discord-bot.service

echo "==> 環境変数ドロップイン配置（無ければテンプレートからコピー）"
DROPIN_DIR=/etc/systemd/system/discord-bot.service.d
mkdir -p "$DROPIN_DIR"
if [ ! -f "$DROPIN_DIR/env.conf" ]; then
  cp "$APP_DIR/deploy/env.conf.example" "$DROPIN_DIR/env.conf"
  chmod 600 "$DROPIN_DIR/env.conf"
  echo "!! $DROPIN_DIR/env.conf を編集して各シークレットを設定してください"
fi

systemctl daemon-reload
systemctl enable discord-bot

cat <<'EOF'

────────────────────────────────────────────
セットアップ完了。残りの手動作業:

1. 環境変数を設定（最低限 DISCORD_BOT_TOKEN と ANTHROPIC_API_KEY）:
     sudo nano /etc/systemd/system/discord-bot.service.d/env.conf
2. 反映して起動:
     sudo systemctl daemon-reload
     sudo systemctl restart discord-bot
3. ログ確認（"✅ ログイン完了" が出れば成功）:
     sudo journalctl -u discord-bot -f

Discord 側は Developer Portal で
  - Bot の MESSAGE CONTENT INTENT を ON
  - bot スコープでサーバーに招待
しておくこと。公開エンドポイントURLの設定は不要。
────────────────────────────────────────────
EOF
