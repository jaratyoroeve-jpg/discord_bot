"""
ツール定義と実行ロジック。
CONTEXT_DOCS_DIR を外部から注入して使う（read_file / grep_files のルートに使用）。
"""
import ast
import datetime
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


async def execute_tool(name: str, tool_input: dict, *, docs_dir: Path) -> str:
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

    return f"不明なツール: {name}"
