"""
Discord Gateway ボット（on_message 方式）

Discord の Gateway に WebSocket で接続し、チャンネル監視セッションを管理する。
- メンション + "/start" でそのチャンネルの監視を開始
- 監視中チャンネルの全メッセージを Anthropic API へ転送して返答
- メンション + "/end" で監視を終了
- 並行シーン対応: /register /scenes
- シーン遷移は AI (GM) が fork_scene / end_scene ツールで実行
"""
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

import anthropic
import discord

from tools import TOOLS, execute_tool

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
SESSION_BASE = Path(os.getenv("SESSION_BASE", "game_data"))
CONTEXT_DOCS_DIR = Path(os.getenv("CONTEXT_DOCS_DIR", "docs"))
SYSTEM_PROMPT_DOC = os.getenv("SYSTEM_PROMPT_DOC")
SYSTEM_DOC = os.getenv("SYSTEM_DOC")
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


class ContextAssembler:
    """シーン単位の会話履歴を保持し、ログへの書き込みを一元管理する。"""

    def __init__(
        self,
        session_id: str,
        scene_id: str = "1",
        participants: list[str] | None = None,
        chapter_summaries: list[str] | None = None,
        chapter_overview: str | None = None,
        scene_summaries: list[str] | None = None,
        parallel_scene_summaries: list[tuple[str, str]] | None = None,
        scene_start_line: int = 0,
    ) -> None:
        self.session_id = session_id
        self.scene_id = scene_id
        self.participants: list[str] = list(participants or [])
        self.chapter_summaries: list[str] = list(chapter_summaries or [])
        self.chapter_overview: str | None = chapter_overview
        self.scene_summaries: list[str] = list(scene_summaries or [])
        # list of (sibling_scene_id, summary_text)
        self.parallel_scene_summaries: list[tuple[str, str]] = list(parallel_scene_summaries or [])
        self.scene_start_line = scene_start_line
        self._messages: list[dict] = []

    def log_path(self, session_base: Path) -> Path:
        safe_id = re.sub(r"[^\w]", "_", self.scene_id)
        return session_base / self.session_id / f"scene_{safe_id}" / "log.jsonl"

    def add(self, role: str, content: str, session_base: Path) -> None:
        self._messages.append({"role": role, "content": content})
        path = self.log_path(session_base)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"role": role, "content": content}, ensure_ascii=False) + "\n")

    def log_line_count(self, session_base: Path) -> int:
        path = self.log_path(session_base)
        try:
            return sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip())
        except Exception:
            return 0

    def messages(self) -> list[dict]:
        preamble = []
        for i, summary in enumerate(self.chapter_summaries, 1):
            preamble.append({"role": "user", "content": f"[第{i}章 要約]\n\n{summary}"})
            preamble.append({"role": "assistant", "content": f"第{i}章の要約を確認しました。"})
        if self.chapter_overview:
            preamble.append({"role": "user", "content": f"[現在の章 概要]\n\n{self.chapter_overview}"})
            preamble.append({"role": "assistant", "content": "現在の章の概要を確認しました。"})
        for summary in self.scene_summaries:
            preamble.append({"role": "user", "content": summary})
            preamble.append({"role": "assistant", "content": "前シーンの内容を確認しました。"})
        for sid, summary in self.parallel_scene_summaries:
            preamble.append({"role": "user", "content": f"[並行シーン {sid} の要約]\n\n{summary}"})
            preamble.append({"role": "assistant", "content": f"並行シーン {sid} の内容を確認しました。"})
        return preamble + self._messages


class SceneManager:
    """複数の並行シーンとユーザー→キャラクター→シーンのルーティングを管理する。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.scenes: dict[str, ContextAssembler] = {}
        self.scene_parents: dict[str, str | None] = {}
        self.user_to_char: dict[int, str] = {}   # discord user_id -> char_name
        self.char_to_scene: dict[str, str] = {}  # char_name -> scene_id

    def _put(self, ctx: ContextAssembler, parent_id: str | None) -> ContextAssembler:
        self.scenes[ctx.scene_id] = ctx
        self.scene_parents[ctx.scene_id] = parent_id
        return ctx

    def initial_scene(self) -> ContextAssembler:
        return self._put(ContextAssembler(self.session_id, scene_id="1"), None)

    def get_siblings(self, scene_id: str) -> list[str]:
        parent = self.scene_parents.get(scene_id)
        if parent is None:
            return []
        return [sid for sid, pid in self.scene_parents.items() if pid == parent and sid != scene_id]

    def update_sibling_summaries(self, scene_id: str, summary: str) -> None:
        for sib_id in self.get_siblings(scene_id):
            ctx = self.scenes.get(sib_id)
            if ctx:
                ctx.parallel_scene_summaries = [
                    (sid, s) for sid, s in ctx.parallel_scene_summaries if sid != scene_id
                ] + [(scene_id, summary)]

    def get_context_for_user(self, user_id: int) -> ContextAssembler | None:
        char = self.user_to_char.get(user_id)
        if char:
            scene_id = self.char_to_scene.get(char)
            if scene_id:
                return self.scenes.get(scene_id)
        # unregistered users fall back to the first scene
        return next(iter(self.scenes.values()), None)

    def register_char(self, user_id: int, char_name: str, scene_id: str | None = None) -> str:
        old_char = self.user_to_char.get(user_id)
        if old_char:
            old_sid = self.char_to_scene.pop(old_char, None)
            if old_sid:
                old_ctx = self.scenes.get(old_sid)
                if old_ctx and old_char in old_ctx.participants:
                    old_ctx.participants.remove(old_char)

        target_id = scene_id if (scene_id and scene_id in self.scenes) else next(iter(self.scenes), None)
        if target_id is None:
            return "エラー: アクティブなシーンがありません"

        self.user_to_char[user_id] = char_name
        self.char_to_scene[char_name] = target_id
        ctx = self.scenes[target_id]
        if char_name not in ctx.participants:
            ctx.participants.append(char_name)
        return f"✅ {char_name} をシーン {target_id} に登録しました"

    def fork_scene(self, from_scene_id: str, chars_to_move: list[str]) -> tuple[ContextAssembler | None, str]:
        source = self.scenes.get(from_scene_id)
        if source is None:
            return None, f"エラー: シーン {from_scene_id} が見つかりません"
        missing = [c for c in chars_to_move if c not in source.participants]
        if missing:
            return None, f"エラー: {', '.join(missing)} はシーン {from_scene_id} に参加していません"

        children_count = sum(1 for pid in self.scene_parents.values() if pid == from_scene_id)
        new_id = f"{from_scene_id}-{children_count + 1}"

        for char in chars_to_move:
            source.participants.remove(char)
            self.char_to_scene[char] = new_id

        new_ctx = ContextAssembler(
            session_id=self.session_id,
            scene_id=new_id,
            participants=list(chars_to_move),
            chapter_summaries=source.chapter_summaries,
            chapter_overview=source.chapter_overview,
            scene_summaries=source.scene_summaries,
            parallel_scene_summaries=[],
            scene_start_line=0,
        )
        self._put(new_ctx, from_scene_id)
        chars_str = ", ".join(chars_to_move)
        return new_ctx, f"シーン {new_id} を作成しました。参加者: {chars_str}"

    def end_scene(self, scene_id: str, target_scene_id: str) -> str:
        scene = self.scenes.get(scene_id)
        if scene is None:
            return f"エラー: シーン {scene_id} が見つかりません"
        target = self.scenes.get(target_scene_id)
        if target is None:
            return f"エラー: 移動先シーン {target_scene_id} が見つかりません"

        moved = list(scene.participants)
        for char in moved:
            self.char_to_scene[char] = target_scene_id
            if char not in target.participants:
                target.participants.append(char)

        del self.scenes[scene_id]
        del self.scene_parents[scene_id]
        chars_str = ", ".join(moved) if moved else "なし"
        return f"シーン {scene_id} を終了。{chars_str} をシーン {target_scene_id} に移動しました。"

    def list_scenes(self) -> str:
        if not self.scenes:
            return "アクティブなシーンがありません"
        lines = []
        for sid, ctx in self.scenes.items():
            parent = self.scene_parents.get(sid)
            parent_str = f"（親: {parent}）" if parent else ""
            chars = ", ".join(ctx.participants) if ctx.participants else "なし"
            lines.append(f"- シーン {sid}{parent_str}: 参加者: {chars}")
        return "\n".join(lines)

    def apply_scene_compression(self, scene_id: str, summary: str, session_base: Path) -> None:
        old = self.scenes.get(scene_id)
        if old is None:
            return
        new_ctx = ContextAssembler(
            session_id=self.session_id,
            scene_id=scene_id,
            participants=old.participants,
            chapter_summaries=old.chapter_summaries,
            chapter_overview=old.chapter_overview,
            scene_summaries=old.scene_summaries + [summary],
            parallel_scene_summaries=old.parallel_scene_summaries,
            scene_start_line=old.log_line_count(session_base),
        )
        self._put(new_ctx, self.scene_parents.get(scene_id))
        self.update_sibling_summaries(scene_id, summary)

    def apply_chapter_advance(self, scene_id: str, chapter_summary: str, new_overview: str, session_base: Path) -> None:
        old = self.scenes.get(scene_id)
        if old is None:
            return
        new_summaries = old.chapter_summaries + ([chapter_summary] if chapter_summary else [])
        new_ctx = ContextAssembler(
            session_id=self.session_id,
            scene_id=scene_id,
            participants=old.participants,
            chapter_summaries=new_summaries,
            chapter_overview=new_overview,
            scene_summaries=[],
            parallel_scene_summaries=old.parallel_scene_summaries,
            scene_start_line=old.log_line_count(session_base),
        )
        self._put(new_ctx, self.scene_parents.get(scene_id))


# ---------------------------------------------------------------------------
# Anthropic client & helpers
# ---------------------------------------------------------------------------

_anthropic = anthropic.AsyncAnthropic(
    api_key=ANTHROPIC_API_KEY,
    **({"base_url": ANTHROPIC_BASE_URL} if ANTHROPIC_BASE_URL else {}),
)

active_channel_id: int | None = None
_manager: SceneManager | None = None
_active_reply_task: asyncio.Task | None = None


def _build_system() -> str:
    base = _system_prompt_doc if _system_prompt_doc else SYSTEM_PROMPT
    if _system_doc:
        return f"{base}\n\n{_system_doc}"
    return base


def _parse_jsonl_history(text: str) -> list[dict] | None:
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


async def call_anthropic(ctx: ContextAssembler) -> tuple[str, str | None, str | None]:
    """Anthropic API を呼び出すエージェントループ（tool_use 対応）。

    Returns (text, scene_summary, chapter_advance_json).
    """
    scene_summary: str | None = None
    chapter_advance_json: str | None = None
    working = list(ctx.messages())
    system = [{"type": "text", "text": _build_system(), "cache_control": {"type": "ephemeral"}}]
    characters_dir = SESSION_BASE / ctx.session_id / "characters"
    scene_log_path = ctx.log_path(SESSION_BASE)

    while True:
        response = await _anthropic.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=8192,
            system=system,
            tools=TOOLS,
            messages=_build_messages(working),
        )

        text = next((b.text for b in response.content if b.type == "text"), "")

        if response.stop_reason != "tool_use":
            return text, scene_summary, chapter_advance_json

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

        tool_results = []
        for b in response.content:
            if b.type == "tool_use":
                logger.info("🔧 tool_use: name=%s input=%s", b.name, b.input)
                result = await execute_tool(
                    b.name, b.input,
                    docs_dir=CONTEXT_DOCS_DIR,
                    scene_log_path=scene_log_path,
                    scene_start_line=ctx.scene_start_line,
                    scene_summaries=ctx.scene_summaries,
                    characters_dir=characters_dir,
                    anthropic_client=_anthropic,
                    model=ANTHROPIC_MODEL,
                    scene_manager=_manager,
                    current_scene_id=ctx.scene_id,
                )
                logger.info("🔧 tool_result: %s", result)
                if b.name == "compress_context":
                    scene_summary = result
                elif b.name == "advance_chapter":
                    chapter_advance_json = result
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


async def _reply_anthropic(message: discord.Message, text: str, ctx: ContextAssembler) -> None:
    nick = message.author.display_name
    labeled = f"[{nick}]: {text}"
    ctx.add("user", labeled, SESSION_BASE)

    async with message.channel.typing():
        try:
            reply, scene_summary, chapter_advance_json = await call_anthropic(ctx)
        except Exception as e:
            reply = f"❌ エラーが発生しました: {e}"
            scene_summary = None
            chapter_advance_json = None

    ctx.add("assistant", reply, SESSION_BASE)

    if _manager:
        if chapter_advance_json:
            try:
                data = json.loads(chapter_advance_json)
                ch_summary = data.get("chapter_summary", "")
                new_overview = data.get("chapter_overview", "")
            except Exception:
                ch_summary = ""
                new_overview = ""
            _manager.apply_chapter_advance(ctx.scene_id, ch_summary, new_overview, SESSION_BASE)
            logger.info("📖 章進行完了: scene=%s", ctx.scene_id)
        elif scene_summary:
            _manager.apply_scene_compression(ctx.scene_id, scene_summary, SESSION_BASE)
            logger.info("🗜 シーン圧縮完了: scene=%s", ctx.scene_id)

    for chunk in _split(reply, 2000):
        await message.reply(chunk, mention_author=False)


@client.event
async def on_message(message: discord.Message):
    global active_channel_id, _manager, _active_reply_task

    if message.author.bot:
        return

    is_mention = client.user in message.mentions
    is_active = message.channel.id == active_channel_id

    if is_mention:
        clean = _extract_clean(message)
        cmd_lower = clean.lower()

        # /start
        if cmd_lower == "/start":
            if active_channel_id is None or is_active:
                if SESSION_BASE.exists():
                    shutil.rmtree(SESSION_BASE)
                active_channel_id = message.channel.id
                _manager = SceneManager(str(uuid.uuid4()))
                _manager.initial_scene()
                await message.channel.send(f"start (session: {_manager.session_id})")
            else:
                await message.reply(f"I'm busy, stay in <#{active_channel_id}>", mention_author=False)
            return

        # /end
        if cmd_lower == "/end":
            if is_active and _manager:
                if _active_reply_task and not _active_reply_task.done():
                    _active_reply_task.cancel()
                _active_reply_task = None
                active_channel_id = None
                _manager = None
                await message.channel.send("end")
            return

        if not is_active:
            if active_channel_id is not None:
                await message.reply(f"I'm busy, stay in <#{active_channel_id}>", mention_author=False)
            return

        if not _manager:
            return

        # /register <char_name> [scene_id]
        if cmd_lower.startswith("/register "):
            parts = clean.split()[1:]
            if not parts:
                await message.reply("使い方: `/register <キャラクター名> [シーンID]`", mention_author=False)
                return
            char_name = parts[0]
            scene_id = parts[1] if len(parts) > 1 else None
            await message.reply(_manager.register_char(message.author.id, char_name, scene_id), mention_author=False)
            return

        # /scenes
        if cmd_lower == "/scenes":
            await message.reply(_manager.list_scenes(), mention_author=False)
            return

        # regular message → route to scene
        ctx = _manager.get_context_for_user(message.author.id)
        if ctx is None:
            await message.reply("エラー: シーンが見つかりません。`/start` を実行してください。", mention_author=False)
            return
        _active_reply_task = asyncio.create_task(_reply_anthropic(message, clean, ctx))
        try:
            await _active_reply_task
        except asyncio.CancelledError:
            pass

    else:
        # no mention: route based on character
        if not is_active or not _manager:
            return
        ctx = _manager.get_context_for_user(message.author.id)
        if ctx is None:
            return
        _active_reply_task = asyncio.create_task(_reply_anthropic(message, message.content, ctx))
        try:
            await _active_reply_task
        except asyncio.CancelledError:
            pass


def _split(text: str, size: int):
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
