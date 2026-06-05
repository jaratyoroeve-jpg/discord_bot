"""
Discord Gateway ボット（on_message 方式）

Discord の Gateway に WebSocket で接続し、チャンネル監視セッションを管理する。
- メンション + "start" でそのチャンネルの監視を開始
- 監視中チャンネルの全メッセージを Anthropic API へ転送して返答
- メンション + "end" で監視を終了
- 監視中に別チャンネルからメンションされたら "busy" と返す
"""
import datetime
import json
import logging
import os
import sys
import uuid
from pathlib import Path

import anthropic
import discord

# Windows コンソール（cp932）でも絵文字・日本語ログを出せるよう UTF-8 に固定。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL")
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")
SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", "sessions"))

logger = logging.getLogger("discord.bot")


def _log(session_id: str, role: str, content: str) -> None:
    session_dir = SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "role": role,
        "content": content,
    }
    with (session_dir / "log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


_anthropic = anthropic.AsyncAnthropic(
    api_key=ANTHROPIC_API_KEY,
    **({"base_url": ANTHROPIC_BASE_URL} if ANTHROPIC_BASE_URL else {}),
)

# 現在監視中のチャンネルIDとセッションID（None = 監視していない）
active_channel_id: int | None = None
active_session_id: str | None = None


async def call_anthropic(user_message: str) -> str:
    message = await _anthropic.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return message.content[0].text


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    logger.info("✅ ログイン完了: %s (id=%s)", client.user, client.user.id)


def _extract_clean(message: discord.Message) -> str:
    text = message.content
    for mention in (f"<@{client.user.id}>", f"<@!{client.user.id}>"):
        text = text.replace(mention, "")
    return text.strip()


async def _reply_anthropic(message: discord.Message, text: str) -> None:
    if active_session_id:
        _log(active_session_id, "user", text)
    async with message.channel.typing():
        try:
            reply = await call_anthropic(text)
        except Exception as e:
            reply = f"❌ エラーが発生しました: {e}"
    if active_session_id:
        _log(active_session_id, "assistant", reply)
    for chunk in _split(reply, 2000):
        await message.reply(chunk, mention_author=False)


@client.event
async def on_message(message: discord.Message):
    global active_channel_id, active_session_id

    if message.author.bot:
        return

    is_mention = client.user in message.mentions
    is_active = message.channel.id == active_channel_id

    if is_mention:
        clean = _extract_clean(message)
        cmd = clean.lower()

        if cmd == "/start":
            if active_channel_id is None or is_active:
                active_channel_id = message.channel.id
                active_session_id = str(uuid.uuid4())
                _log(active_session_id, "system", "session_start")
                await message.channel.send(f"start (session: {active_session_id})")
            else:
                await message.reply(f"I'm busy, stay in <#{active_channel_id}>", mention_author=False)
            return

        if cmd == "/end":
            if is_active and active_session_id:
                _log(active_session_id, "system", "session_end")
                active_channel_id = None
                active_session_id = None
                await message.channel.send("end")
            return

        # start/end 以外のメンション
        if is_active:
            await _reply_anthropic(message, clean)
        elif active_channel_id is not None:
            await message.reply(f"I'm busy, stay in <#{active_channel_id}>", mention_author=False)

    else:
        # メンションなし：監視中チャンネルの発言は Anthropic へ転送
        if is_active:
            await _reply_anthropic(message, message.content)


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
