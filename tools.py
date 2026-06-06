"""
ツール定義と実行ロジック。
CONTEXT_DOCS_DIR を外部から注入して使う（read_file / grep_files のルートに使用）。
"""
import ast
import datetime
import json
import operator
import random
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Tool definitions (Anthropic API 形式)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "get_current_datetime",
        "description": (
            "Returns the current date and time in JST (Japan Standard Time). "
            "Use this when the user asks about the current time or date."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "calculate",
        "description": (
            "Evaluates a mathematical expression and returns the result. "
            "Supports +, -, *, /, **, % and parentheses. "
            "Use this for arithmetic the user asks you to compute."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "The arithmetic expression to evaluate, e.g. '(3 + 4) * 2'.",
                }
            },
            "required": ["expression"],
        },
    },
    {
        "name": "roll_dice",
        "description": (
            "Rolls one or more dice and returns the results. "
            "Use this when the user wants to roll dice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "Number of dice to roll (1–100).",
                },
                "sides": {
                    "type": "integer",
                    "description": "Number of sides on each die (2–1000).",
                },
            },
            "required": ["count", "sides"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Reads the contents of a file in the context documents directory. "
            "Path is relative to that directory. "
            "Use this to look up documentation or reference material."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative file path within the docs directory, e.g. 'rules.md'.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "grep_files",
        "description": (
            "Searches for lines matching a regex pattern across files in the context documents directory. "
            "Returns matching lines with file name and line number. "
            "Use this to find specific content or keywords."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The text or regex pattern to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "Optional relative path within docs directory to restrict search (file or subdirectory). Defaults to all files.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "compress_context",
        "description": (
            "現在のシーンの会話履歴を要約し、コンテキストを圧縮します。"
            "戦闘終了・ダンジョン踏破・場面の大きな区切りなど、シーン変わり目で使用してください。"
            "セッションログを読み取り、Anthropic APIで要約を生成してログ行番号付きで返します。"
            "このツールを呼び出すと、過去の会話履歴は要約に置き換えられ、コンテキストが節約されます。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scene_description": {
                    "type": "string",
                    "description": "要約するシーンの説明（例: '第1シーン: ゴブリン洞窟での戦闘'）。要約品質の向上に使われます。",
                }
            },
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _safe_eval(expr: str) -> float:
    """Evaluate a math expression without eval(). Raises ValueError for unsupported ops."""
    def _eval(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
            return _SAFE_OPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPS:
            return _SAFE_OPS[type(node.op)](_eval(node.operand))
        raise ValueError(f"サポートされていない式です: {ast.dump(node)}")
    tree = ast.parse(expr, mode="eval")
    return _eval(tree.body)


def _tool_read_file(tool_input: dict, docs_dir: Path) -> str:
    raw_path = tool_input.get("path", "")
    base = docs_dir.resolve()
    target = (docs_dir / raw_path).resolve()
    if not str(target).startswith(str(base)):
        return "エラー: アクセス拒否（ドキュメントディレクトリ外のパスです）"
    if not target.is_file():
        return f"エラー: ファイルが見つかりません: {raw_path}"
    try:
        content = target.read_text(encoding="utf-8")
        if len(content) > 10000:
            content = content[:10000] + "\n... (以降省略)"
        return content
    except Exception as e:
        return f"エラー: {e}"


def _tool_grep_files(tool_input: dict, docs_dir: Path) -> str:
    pattern = tool_input.get("pattern", "")
    raw_path = tool_input.get("path", "")
    base = docs_dir.resolve()
    if raw_path:
        search_root = (docs_dir / raw_path).resolve()
        if not str(search_root).startswith(str(base)):
            return "エラー: アクセス拒否（ドキュメントディレクトリ外のパスです）"
    else:
        search_root = base
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"エラー: 無効な正規表現: {e}"
    results = []
    candidates = [search_root] if search_root.is_file() else sorted(search_root.rglob("*"))
    for f in candidates:
        if not f.is_file():
            continue
        try:
            for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if regex.search(line):
                    results.append(f"{f.relative_to(base)}:{lineno}: {line}")
        except Exception:
            continue
    if not results:
        return "一致する行が見つかりませんでした。"
    output = "\n".join(results[:100])
    if len(results) > 100:
        output += f"\n... ({len(results) - 100} 件省略)"
    return output


async def _tool_compress_context(
    tool_input: dict,
    session_id: str,
    sessions_dir: Path,
    anthropic_client,
    model: str,
    scene_start_line: int = 0,
) -> str:
    log_path = sessions_dir / session_id / "log.jsonl"
    if not log_path.exists():
        return "エラー: セッションログが見つかりません"

    all_lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    total_line_count = len(all_lines)
    # 現シーンの未圧縮部分だけを対象にする
    scene_lines = all_lines[scene_start_line:]

    messages = []
    for line in scene_lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            role = entry.get("role")
            content = entry.get("content")
            if role in ("user", "assistant") and isinstance(content, str):
                messages.append(f"[{role}]: {content}")
        except Exception:
            continue

    if not messages:
        return "エラー: 要約する会話履歴がありません"

    scene_description = tool_input.get("scene_description", "")
    scene_hint = f"\nシーン: {scene_description}" if scene_description else ""

    system = (
        "あなたはTRPGセッションの記録係です。"
        "以下の会話履歴を簡潔に要約してください。"
        "重要なイベント・決定事項・場面の展開を中心に、"
        "次のシーンのGMとプレイヤーが状況を把握できる形で箇条書きにまとめてください。"
        "HP・MP・アイテムの数値は別途管理されるため記載不要です。"
        "ただし「致命傷を負った」「毒にかかった」のように物語上の意味がある状態変化は簡潔に含めてください。"
        "400文字以内に収めてください。"
        + scene_hint
    )

    try:
        response = await anthropic_client.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": "\n".join(messages)}],
        )
        summary = next((b.text for b in response.content if b.type == "text"), "")
    except Exception as e:
        return f"エラー: 要約生成に失敗しました: {e}"

    return f"[前シーンの要約 (ログ行: {scene_start_line + 1}-{total_line_count})]\n\n{summary}"


async def execute_tool(
    name: str,
    tool_input: dict,
    *,
    docs_dir: Path,
    session_id: str | None = None,
    sessions_dir: Path | None = None,
    anthropic_client=None,
    model: str | None = None,
    scene_start_line: int = 0,
) -> str:
    """ツールを実行して結果を文字列で返す。"""
    if name == "get_current_datetime":
        jst = datetime.timezone(datetime.timedelta(hours=9))
        now = datetime.datetime.now(tz=jst)
        return now.strftime("%Y-%m-%d %H:%M:%S JST (%A)")

    if name == "calculate":
        expression = tool_input.get("expression", "")
        try:
            result = _safe_eval(expression)
            if isinstance(result, float) and result.is_integer():
                result = int(result)
            return f"{expression} = {result}"
        except ZeroDivisionError:
            return "エラー: ゼロ除算"
        except Exception as e:
            return f"エラー: {e}"

    if name == "roll_dice":
        count = max(1, min(int(tool_input.get("count", 1)), 100))
        sides = max(2, min(int(tool_input.get("sides", 6)), 1000))
        rolls = [random.randint(1, sides) for _ in range(count)]
        total = sum(rolls)
        rolls_str = ", ".join(map(str, rolls))
        return f"{count}d{sides}: [{rolls_str}] 合計={total}"

    if name == "read_file":
        return _tool_read_file(tool_input, docs_dir)

    if name == "grep_files":
        return _tool_grep_files(tool_input, docs_dir)

    if name == "compress_context":
        if not session_id or not sessions_dir or not anthropic_client or not model:
            return "エラー: compress_context はアクティブなセッション内でのみ使用できます"
        return await _tool_compress_context(tool_input, session_id, sessions_dir, anthropic_client, model, scene_start_line)

    return f"不明なツール: {name}"
