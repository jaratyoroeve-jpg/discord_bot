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

# キャラクターシートのネストdictフィールド（キー単位でマージする対象）
_CHAR_NESTED_DICTS = {"hp", "ability_scores", "death_saves", "spell_slots", "currency"}


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
            "戦闘終了・ダンジョン踏破・会話終了・場面の大きな区切りなど、章内のシーン変わり目で使用してください。"
            "セッションログを読み取り、Anthropic APIで要約を生成してログ行番号付きで返します。"
            "このツールを呼び出すと、過去の会話履歴は要約に置き換えられ、コンテキストが節約されます。"
            "章の区切りには compress_context ではなく advance_chapter を使用してください。"
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
    {
        "name": "list_characters",
        "description": (
            "登録されているすべてのキャラクターの一覧を取得します。"
            "各キャラクターの名前・プレイヤー名・クラス・レベル・HP を表示します。"
            "セッション開始時やキャラクター確認が必要な場面で使用してください。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_character_sheet",
        "description": (
            "キャラクターシートを取得します。"
            "HP・能力値・AC・アイテム・状態異常・スペルスロットなどの全情報を JSON で返します。"
            "戦闘中の状態確認・判定の補助・アイテム効果の確認に使用してください。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "character_name": {
                    "type": "string",
                    "description": "取得するキャラクター名（例: 'Thorin'）",
                }
            },
            "required": ["character_name"],
        },
    },
    {
        "name": "update_character_sheet",
        "description": (
            "キャラクターシートを更新します。"
            "HP変化・状態異常の付与/解除・アイテムの追加/削除・能力値変更・死亡セーヴなどに使用します。"
            "キャラクターが存在しない場合は新規作成します。"
            "updates に変更するフィールドを指定してください。"
            "hp / ability_scores / death_saves / spell_slots / currency はキー単位でマージします。"
            "conditions / inventory / features などの配列は全体を置き換えます。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "character_name": {
                    "type": "string",
                    "description": "更新するキャラクター名",
                },
                "updates": {
                    "type": "object",
                    "description": (
                        "更新するフィールドと値。例: "
                        '{"hp": {"current": 8}, "conditions": ["Poisoned"]} '
                        "または "
                        '{"ability_scores": {"STR": 18}, "level": 2, "inventory": ['
                        '{"name": "Longsword +1", "description": "魔法の剣", "weight": 3, '
                        '"value": "1000 gp", "bonus_stats": {"attack_bonus": 1, "damage_bonus": 1}, '
                        '"effect": "この武器での攻撃は魔法攻撃として扱われる"}]}'
                    ),
                },
            },
            "required": ["character_name", "updates"],
        },
    },
    {
        "name": "advance_chapter",
        "description": (
            "現在の章を終了し、新しい章を開始します。"
            "ゲーム開始時・キャラクター作成完了後・GMが章の区切りと判断した際に使用してください。"
            "内部で自動的に現在のシーンを圧縮し、この章のすべてのシーン要約を前章要約にまとめます。"
            "戦闘や小さな場面転換には compress_context を使用してください。"
            "新しい章の概要・目的・終了条件を chapter_overview に指定してください。"
            "結果は JSON 形式で返ります。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "chapter_overview": {
                    "type": "string",
                    "description": "新しい章の概要、目的、終了条件（例: '第2章: 魔王の城へ。目的: 同盟者を3人集める。終了条件: 同盟者獲得または全滅'）",
                },
                "scene_description": {
                    "type": "string",
                    "description": "現在のシーンの説明（残りログ圧縮の品質向上のためのヒント）",
                },
            },
            "required": ["chapter_overview"],
        },
    },
    {
        "name": "fork_scene",
        "description": (
            "現在のシーンから並行シーンを作成し、指定したキャラクターを新しいシーンへ移動します。"
            "パーティが分かれて別々の行動をとる場面転換で使用してください。"
            "新しいシーンは現在シーンの子シーンとなり、並行して進行します。"
            "シーン圧縮（compress_context）は fork_scene の前後どちらでも可能です。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "characters": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "新しいシーンへ移動するキャラクター名のリスト（例: ['アレックス']）",
                },
            },
            "required": ["characters"],
        },
    },
    {
        "name": "end_scene",
        "description": (
            "現在のシーンを終了し、参加者を指定のシーンへ合流させます。"
            "並行シーンが収束してキャラクターが合流する際に使用してください。"
            "このツールを呼ぶ前に compress_context でシーンを要約しておくと、"
            "合流先のシーンにこのシーンの要約が伝わります。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_scene_id": {
                    "type": "string",
                    "description": "参加者の移動先シーン ID（例: '1', '1-1'）",
                },
            },
            "required": ["target_scene_id"],
        },
    },
    {
        "name": "list_scenes",
        "description": (
            "現在アクティブなすべてのシーンとその参加者を一覧表示します。"
            "シーン構造を確認したい場合や fork_scene / end_scene の前後に使用してください。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
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


def _default_character_sheet(character_name: str) -> dict:
    return {
        "character_name": character_name,
        "player_name": "",
        "race": "",
        "class": "",
        "level": 1,
        "background": "",
        "alignment": "",
        "experience_points": 0,
        "ability_scores": {
            "STR": 10, "DEX": 10, "CON": 10,
            "INT": 10, "WIS": 10, "CHA": 10,
        },
        "hp": {"max": 0, "current": 0, "temp": 0},
        "ac": 10,
        "initiative": 0,
        "speed": 30,
        "proficiency_bonus": 2,
        "saving_throw_proficiencies": [],
        "skill_proficiencies": [],
        "skill_expertise": [],
        "death_saves": {"successes": 0, "failures": 0},
        "spell_slots": {},
        "conditions": [],
        "features": [],
        "inventory": [],
        "currency": {"cp": 0, "sp": 0, "ep": 0, "gp": 0, "pp": 0},
        "notes": "",
    }


def _character_path(characters_dir: Path, character_name: str) -> Path:
    safe_name = re.sub(r"[^\w\-]", "_", character_name)
    return characters_dir / f"{safe_name}.json"


def _tool_list_characters(characters_dir: Path) -> str:
    characters_dir.mkdir(parents=True, exist_ok=True)
    sheets = sorted(characters_dir.glob("*.json"))
    if not sheets:
        return "キャラクターが登録されていません。"
    lines = []
    for sheet_path in sheets:
        try:
            data = json.loads(sheet_path.read_text(encoding="utf-8"))
            name = data.get("character_name", sheet_path.stem)
            player = data.get("player_name") or "未設定"
            cls = data.get("class") or "不明"
            level = data.get("level", 1)
            hp = data.get("hp", {})
            hp_str = f"{hp.get('current', '?')}/{hp.get('max', '?')}"
            conds = data.get("conditions", [])
            cond_str = f" [{', '.join(conds)}]" if conds else ""
            lines.append(f"- {name} (Player: {player} | {cls} Lv.{level} | HP: {hp_str}{cond_str})")
        except Exception:
            lines.append(f"- {sheet_path.stem} (読み込みエラー)")
    return "\n".join(lines)


def _tool_get_character_sheet(tool_input: dict, characters_dir: Path) -> str:
    character_name = tool_input.get("character_name", "")
    if not character_name:
        return "エラー: character_name を指定してください"
    characters_dir.mkdir(parents=True, exist_ok=True)
    path = _character_path(characters_dir, character_name)
    if not path.exists():
        return f"エラー: キャラクター '{character_name}' が見つかりません"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"エラー: {e}"


def _tool_update_character_sheet(tool_input: dict, characters_dir: Path) -> str:
    character_name = tool_input.get("character_name", "")
    updates = tool_input.get("updates", {})
    if not character_name:
        return "エラー: character_name を指定してください"
    if not updates:
        return "エラー: updates を指定してください"

    characters_dir.mkdir(parents=True, exist_ok=True)
    path = _character_path(characters_dir, character_name)

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            return f"エラー: シートの読み込みに失敗しました: {e}"
    else:
        data = _default_character_sheet(character_name)

    for key, value in updates.items():
        if key in _CHAR_NESTED_DICTS and isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value

    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return f"'{character_name}' のシートを更新しました。\n" + json.dumps(data, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"エラー: {e}"


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
    scene_log_path: Path,
    anthropic_client,
    model: str,
    scene_start_line: int = 0,
) -> str:
    if not scene_log_path.exists():
        return "エラー: セッションログが見つかりません"

    all_lines = [l for l in scene_log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
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


async def _tool_advance_chapter(
    tool_input: dict,
    scene_log_path: Path,
    anthropic_client,
    model: str,
    scene_start_line: int = 0,
    scene_summaries: list[str] | None = None,
) -> str:
    chapter_overview = tool_input.get("chapter_overview", "")

    # Step 1: 残っている生ログをシーン圧縮
    compress_input = {}
    if sd := tool_input.get("scene_description"):
        compress_input["scene_description"] = sd

    current_scene = await _tool_compress_context(
        compress_input, scene_log_path, anthropic_client, model, scene_start_line
    )

    # 有効なシーン要約のみ収集（エラーは除外）
    all_summaries = list(scene_summaries or [])
    if current_scene and not current_scene.startswith("エラー:"):
        all_summaries.append(current_scene)

    # Step 2: 全シーン要約を前章要約に統合
    chapter_summary = ""
    if all_summaries:
        combined = "\n\n---\n\n".join(all_summaries)
        system = (
            "あなたはTRPGセッションの記録係です。"
            "以下の複数のシーン要約を読み、1つの章の要約としてまとめてください。"
            "重要なイベント・決定事項・キャラクターの成長・場面の転換を中心に整理し、"
            "次の章でGMとプレイヤーが振り返れる形で箇条書きにまとめてください。"
            "600文字以内に収めてください。"
        )
        try:
            resp = await anthropic_client.messages.create(
                model=model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": combined}],
            )
            chapter_summary = next((b.text for b in resp.content if b.type == "text"), "")
        except Exception as e:
            return json.dumps({"error": f"章要約の生成に失敗しました: {e}"}, ensure_ascii=False)

    return json.dumps(
        {"chapter_summary": chapter_summary, "chapter_overview": chapter_overview},
        ensure_ascii=False,
    )


async def execute_tool(
    name: str,
    tool_input: dict,
    *,
    docs_dir: Path,
    scene_log_path: Path | None = None,
    scene_start_line: int = 0,
    scene_summaries: list[str] | None = None,
    characters_dir: Path | None = None,
    anthropic_client=None,
    model: str | None = None,
    scene_manager=None,
    current_scene_id: str | None = None,
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

    _chars_dir = characters_dir if characters_dir is not None else Path("characters")

    if name == "list_characters":
        return _tool_list_characters(_chars_dir)

    if name == "get_character_sheet":
        return _tool_get_character_sheet(tool_input, _chars_dir)

    if name == "update_character_sheet":
        return _tool_update_character_sheet(tool_input, _chars_dir)

    if name == "compress_context":
        if not scene_log_path or not anthropic_client or not model:
            return "エラー: compress_context はアクティブなセッション内でのみ使用できます"
        return await _tool_compress_context(tool_input, scene_log_path, anthropic_client, model, scene_start_line)

    if name == "advance_chapter":
        if not scene_log_path or not anthropic_client or not model:
            return json.dumps({"error": "advance_chapter はアクティブなセッション内でのみ使用できます"}, ensure_ascii=False)
        return await _tool_advance_chapter(
            tool_input, scene_log_path, anthropic_client, model, scene_start_line, scene_summaries or []
        )

    if name == "fork_scene":
        if not scene_manager or not current_scene_id:
            return "エラー: fork_scene はアクティブなセッション内でのみ使用できます"
        chars = tool_input.get("characters", [])
        if not chars:
            return "エラー: characters を指定してください"
        _, msg = scene_manager.fork_scene(current_scene_id, chars)
        return msg

    if name == "end_scene":
        if not scene_manager or not current_scene_id:
            return "エラー: end_scene はアクティブなセッション内でのみ使用できます"
        target = tool_input.get("target_scene_id", "")
        if not target:
            return "エラー: target_scene_id を指定してください"
        return scene_manager.end_scene(current_scene_id, target)

    if name == "list_scenes":
        if not scene_manager:
            return "エラー: list_scenes はアクティブなセッション内でのみ使用できます"
        return scene_manager.list_scenes()

    return f"不明なツール: {name}"
