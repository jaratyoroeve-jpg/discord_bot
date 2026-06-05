"""
Discord Gateway ボット（on_message 方式）

Discord の Gateway に WebSocket で接続し、Bot へのメンションに反応して
Anthropic API（Claude）の返答を送る。公開HTTPSエンドポイントは不要。
"""
import logging
import os
import sys

import anthropic
import discord

# Windows コンソール（cp932）でも絵文字・日本語ログを出せるよう UTF-8 に固定。
# Linux は元から UTF-8 なので影響なし。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL")  # 未設定時は SDK デフォルト
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")

# discord.py が client.run() で設定するハンドラ（"discord" ロガー）を継承するため、
# あえて "discord." 配下の名前にする。独立した名前だとハンドラが無く INFO が破棄される。
logger = logging.getLogger("discord.bot")


# ──────────────────────────────────────────────
# Anthropic API 呼び出し
# ──────────────────────────────────────────────
_anthropic = anthropic.AsyncAnthropic(
    api_key=ANTHROPIC_API_KEY,
    **({"base_url": ANTHROPIC_BASE_URL} if ANTHROPIC_BASE_URL else {}),
)


async def call_anthropic(user_message: str) -> str:
    message = await _anthropic.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return message.content[0].text


# ──────────────────────────────────────────────
# Discord クライアント
# ──────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True  # メンション本文の取得に必要（Developer Portal で要ON）
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    logger.info("✅ ログイン完了: %s (id=%s)", client.user, client.user.id)


@client.event
async def on_message(message: discord.Message):
    # 自分・他Botのメッセージは無視
    if message.author.bot:
        return

    # Bot へのメンションがある場合のみ反応
    if client.user not in message.mentions:
        return

    # メンション表記を除去して本文だけ取り出す
    clean_message = message.content
    for mention in (f"<@{client.user.id}>", f"<@!{client.user.id}>"):
        clean_message = clean_message.replace(mention, "")
    clean_message = clean_message.strip()

    if not clean_message:
        return

    # 入力中インジケータを出しつつ Claude に問い合わせ
    async with message.channel.typing():
        try:
            reply = await call_anthropic(clean_message)
        except Exception as e:
            reply = f"❌ エラーが発生しました: {e}"

    # Discord は1メッセージ2000文字制限。超える場合は分割して返信。
    for chunk in _split(reply, 2000):
        await message.reply(chunk, mention_author=False)


def _split(text: str, size: int):
    """text を size 文字ごとに分割して返す（空文字なら1つだけ返す）。"""
    if not text:
        return [""]
    return [text[i : i + size] for i in range(0, len(text), size)]


async def main():
    async with client:
        await client.start(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    import asyncio
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
