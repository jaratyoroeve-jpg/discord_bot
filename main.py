"""
Discord Gateway ボット（on_message 方式）

Discord の Gateway に WebSocket で接続し、チャンネル監視セッションを管理する。
- メンション + "start" でそのチャンネルの監視を開始
- 監視中チャンネルの全メッセージを Anthropic API へ転送して返答
- メンション + "end" で監視を終了
- 監視中に別チャンネルからメンションされたら "busy" と返す
"""
import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path

import anthropic
import discord

from tools import TOOLS, execute_tool

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
CONTEXT_DOCS_DIR = Path(os.getenv("CONTEXT_DOCS_DIR", "docs"))
# sysprompt系: ペルソナ・ルール → system フィールドに追記
SYSTEM_PROMPT_DOC = os.getenv("SYSTEM_PROMPT_DOC")  # 基本プロンプト (.md)
SYSTEM_DOC = os.getenv("SYSTEM_DOC")                # ゲームルール (.md)
# context compression系: 過去会話の圧縮サマリー → messages 先頭に挿入
CONTEXT_DOC = os.getenv("CONTEXT_DOC")


def _load_doc(filename: str | None) -> str | None:
    if not filename:
        return None
    path = CONTEXT_DOCS_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"ファイルが見つかりません: {path}")
    return path.read_text(encoding="utf-8")


_system_prompt_doc: str | None = _load_doc(SYSTEM_PROMPT_DOC)
_system_doc: str | None = _load_doc(SYSTEM_DOC)
_context_doc: str | None = _load_doc(CONTEXT_DOC)

logger = logging.getLogger("discord.bot")


def _log(session_id: str, role: str, content) -> None:
    session_dir = SESSIONS_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    entry = {"role": role, "content": content}
    with (session_dir / "log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class ContextAssembler:
    """セッション中の会話履歴を保持し、ログへの書き込みを一元管理する。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._messages: list[dict] = []

    def add(self, role: str, content: str) -> None:
        """メッセージを履歴に追加し、同時にログへ書き込む（重複なし）。"""
        self._messages.append({"role": role, "content": content})
        _log(self.session_id, role, content)

    def messages(self) -> list[dict]:
        return list(self._messages)


# ---------------------------------------------------------------------------
# Anthropic client & helpers
# ---------------------------------------------------------------------------

_anthropic = anthropic.AsyncAnthropic(
    api_key=ANTHROPIC_API_KEY,
    **({"base_url": ANTHROPIC_BASE_URL} if ANTHROPIC_BASE_URL else {}),
)

# 現在監視中のチャンネルIDとセッションID（None = 監視していない）
active_channel_id: int | None = None
active_session_id: str | None = None
_context: ContextAssembler | None = None
_active_reply_task: asyncio.Task | None = None


def _build_system() -> str:
    base = _system_prompt_doc if _system_prompt_doc else SYSTEM_PROMPT
    if _system_doc:
        return f"{base}\n\n{_system_doc}"
    return base


def _parse_jsonl_history(text: str) -> list[dict] | None:
    """JSONL形式の会話ログを [{role, content}, ...] に変換する。
    1行でもパース失敗 or role が不正な場合は None を返す。"""
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return None
        role = entry.get("role")
        content = entry.get("content")
        if role not in ("user", "assistant") or content is None:
            return None
        result.append({"role": role, "content": content})
    return result or None


def _build_messages(messages: list[dict]) -> list[dict]:
    if not _context_doc:
        return messages
    history = _parse_jsonl_history(_context_doc)
    if history is not None:
        return history + messages
    preamble = [
        {"role": "user", "content": _context_doc},
        {"role": "assistant", "content": "Context loaded."},
    ]
    return preamble + messages


async def call_anthropic(messages: list[dict]) -> str:
    """Anthropic API を呼び出すエージェントループ（tool_use 対応）。

    messages はコンテキスト会話履歴。ツール呼び出しが発生した場合は
    内部コピーでループを回し、最終的なテキスト応答だけを返す。
    """
    # ツール呼び出しの往復は内部コピーで管理し、呼び出し元の履歴に影響させない。
    working = list(messages)
    system = [{"type": "text", "text": _build_system(), "cache_control": {"type": "ephemeral"}}]

    while True:
        response = await _anthropic.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4096,
            system=system,
            tools=TOOLS,
            messages=_build_messages(working),
        )

        # テキスト部分を取り出す（tool_use と共存する場合もある）
        text = next((b.text for b in response.content if b.type == "text"), "")

        if response.stop_reason != "tool_use":
            return text

        # ---- tool_use: アシスタントの応答を working に追加 ----
        assistant_content = []
        for b in response.content:
            if b.type == "text":
                assistant_content.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use",
                    "id": b.id,
                    "name": b.name,
                    "input": b.input,
                })
        working.append({"role": "assistant", "content": assistant_content})

        # ---- ツールを実行して結果を収集 ----
        tool_results = []
        for b in response.content:
            if b.type == "tool_use":
                logger.info("🔧 tool_use: name=%s input=%s", b.name, b.input)
                result = await execute_tool(b.name, b.input, docs_dir=CONTEXT_DOCS_DIR)
                logger.info("🔧 tool_result: %s", result)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": result,
                })

        working.append({"role": "user", "content": tool_results})


# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

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
    nick = message.author.display_name
    labeled = f"[{nick}]: {text}"
    if _context:
        _context.add("user", labeled)
        ctx_messages = _context.messages()
    else:
        ctx_messages = [{"role": "user", "content": labeled}]

    async with message.channel.typing():
        try:
            reply = await call_anthropic(ctx_messages)
        except Exception as e:
            reply = f"❌ エラーが発生しました: {e}"

    if _context:
        _context.add("assistant", reply)

    for chunk in _split(reply, 2000):
        await message.reply(chunk, mention_author=False)


@client.event
async def on_message(message: discord.Message):
    global active_channel_id, active_session_id, _context, _active_reply_task

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
                _context = ContextAssembler(active_session_id)
                await message.channel.send(f"start (session: {active_session_id})")
            else:
                await message.reply(f"I'm busy, stay in <#{active_channel_id}>", mention_author=False)
            return

        if cmd == "/end":
            if is_active and active_session_id:
                if _active_reply_task and not _active_reply_task.done():
                    _active_reply_task.cancel()
                _active_reply_task = None
                active_channel_id = None
                active_session_id = None
                _context = None
                await message.channel.send("end")
            return

        # start/end 以外のメンション
        if is_active:
            _active_reply_task = asyncio.create_task(_reply_anthropic(message, clean))
            try:
                await _active_reply_task
            except asyncio.CancelledError:
                pass
        elif active_channel_id is not None:
            await message.reply(f"I'm busy, stay in <#{active_channel_id}>", mention_author=False)

    else:
        # メンションなし：監視中チャンネルの発言は Anthropic へ転送
        if is_active:
            _active_reply_task = asyncio.create_task(_reply_anthropic(message, message.content))
            try:
                await _active_reply_task
            except asyncio.CancelledError:
                pass


def _split(text: str, size: int):
    """text を size 文字ごとに分割して返す（空文字なら1つだけ返す）。"""
    if not text:
        return [""]
    return [text[i : i + size] for i in range(0, len(text), size)]


async def main():
    async with client:
        await client.start(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
