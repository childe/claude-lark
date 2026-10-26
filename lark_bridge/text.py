"""Reply parsing and Feishu message templates. Pure functions, no I/O."""

import json
import re

KEEPALIVE_REASON = "（claude-lark 保活）没有新的飞书消息。只回复 waiting 这一个词，不要调用任何工具，不要做其他事。"
DELIVER_PREFIX = "以下是用户在飞书群里发来的消息：\n\n"
JOIN_SEP = "\n\n---\n\n"

_YES = {"同意", "允许", "1", "y", "yes", "ok"}
_NO = {"拒绝", "2", "n", "no"}
_TRAILING_PUNCT = "。.!！"

PERMISSION_FOOTER = "回复「同意」或「拒绝」（1 / 2）"
_JSON_LIMIT = 1500


def parse_permission(reply: str) -> bool | None:
    """True / False only for an exact allow / deny word; anything else is None."""
    s = reply.strip().lower().rstrip(_TRAILING_PUNCT).strip()
    if s in _YES:
        return True
    if s in _NO:
        return False
    return None


def _options(question: dict) -> list[str]:
    return [str(o.get("label", "")) for o in question.get("options") or []]


def _parse_one(reply: str, question: dict) -> str:
    reply = reply.strip()
    labels = _options(question)
    tokens = [t for t in re.split(r"[,，、\s]+", reply) if t]
    if (
        tokens
        and labels
        and all(
            t.isascii() and t.isdigit() and 1 <= int(t) <= len(labels) for t in tokens
        )
    ):
        chosen = [labels[int(t) - 1] for t in tokens]
        if not question.get("multiSelect"):
            chosen = chosen[:1]
        return ", ".join(chosen)
    return reply


def parse_answers(reply: str, questions: list[dict]) -> dict[str, str]:
    if len(questions) == 1:
        q = questions[0]
        return {q["question"]: _parse_one(reply, q)}
    lines = reply.strip().splitlines()
    answers = {}
    for i, q in enumerate(questions):
        answers[q["question"]] = _parse_one(lines[i], q) if i < len(lines) else ""
    return answers


def format_permission(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        parts = ["Claude 想执行 Bash 命令："]
        desc = tool_input.get("description")
        if desc:
            parts.append(str(desc))
        parts.append("```\n" + str(tool_input.get("command", "")) + "\n```")
    else:
        body = json.dumps(tool_input, ensure_ascii=False, indent=2)
        if len(body) > _JSON_LIMIT:
            body = body[:_JSON_LIMIT] + "\n…（已截断）"
        parts = [f"Claude 想使用工具 {tool_name}：", "```\n" + body + "\n```"]
    parts.append(PERMISSION_FOOTER)
    return "\n".join(parts)


def format_questions(questions: list[dict]) -> str:
    blocks = []
    for q in questions:
        lines = [str(q.get("question", ""))]
        for i, o in enumerate(q.get("options") or [], 1):
            line = f"{i}. {o.get('label', '')}"
            if o.get("description"):
                line += f" — {o['description']}"
            lines.append(line)
        blocks.append("\n".join(lines))
    blocks.append("回复序号，多选用逗号分隔；也可以直接回复文字")
    return "\n\n".join(blocks)


def _is_fence(line: str) -> bool:
    return line.lstrip().startswith("```")


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Split on newlines, keeping every part <= limit and code fences balanced."""
    if len(text) <= limit:
        return [text]
    # Room for a closing "\n```" plus an opening "```\n" around any piece.
    max_piece = limit - 8
    pieces = []
    for line in text.split("\n"):
        while len(line) > max_piece:
            pieces.append(line[:max_piece])
            line = line[max_piece:]
        pieces.append(line)

    parts: list[str] = []
    cur = ""
    fence = False  # inside a code block at the end of cur
    for piece in pieces:
        new_fence = fence != _is_fence(piece)
        cand = cur + "\n" + piece if cur else piece
        if len(cand) + (4 if new_fence else 0) <= limit:
            cur, fence = cand, new_fence
            continue
        parts.append(cur + "\n```" if fence else cur)
        cur = "```" if fence else ""
        cur = cur + "\n" + piece if cur else piece
        fence = fence != _is_fence(piece)
    if cur:
        parts.append(cur + "\n```" if fence else cur)
    return parts


_PLACEHOLDER = re.compile(r"@_user_\d+\s*|@_all\s*")
_LEADING = re.compile(r"^\s*@\S+\s*")


def strip_mentions(text: str) -> str:
    """Remove @_user_N / @_all placeholders and leading @name mentions."""
    text = _PLACEHOLDER.sub("", text)
    while True:
        new = _LEADING.sub("", text, count=1)
        if new == text:
            break
        text = new
    return text.strip()
