"""Reply parsing and Feishu message templates. Pure functions, no I/O."""

from __future__ import annotations

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
        parts.append(fenced(str(tool_input.get("command", ""))))
    else:
        body = json.dumps(tool_input, ensure_ascii=False, indent=2)
        if len(body) > _JSON_LIMIT:
            body = body[:_JSON_LIMIT] + "\n…（已截断）"
        parts = [f"Claude 想使用工具 {tool_name}：", fenced(body)]
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


_FENCE_OPEN = re.compile(r"[ \t]{0,3}(`{3,})")


def fence_for(content: str) -> str:
    """比内容里最长的一串反引号多一个（至少 3 个），内容里的 ``` 就关不掉它。"""
    longest = max((len(r) for r in re.findall(r"`+", content)), default=0)
    return "`" * max(3, longest + 1)


def fenced(content: str) -> str:
    f = fence_for(content)
    return f"{f}\n{content}\n{f}"


def _fence_step(open_fence, line: str):
    """读入一行后的围栏状态：None 表示不在代码块里，否则是开代码块的那串反引号。"""
    m = _FENCE_OPEN.match(line)
    if open_fence is None:
        return m.group(1) if m else None
    # 关闭围栏：只有反引号，且不短于开围栏（CommonMark）
    if m and len(m.group(1)) >= len(open_fence) and not line[m.end() :].strip():
        return None
    return open_fence


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Split on newlines, keeping every part <= limit and code fences balanced.

    围栏可以比 3 个反引号长（见 fence_for）；分段处用同样长度的围栏关闭、重新打开。"""
    if len(text) <= limit:
        return [text]
    widest = max(
        [3] + [len(m.group(1)) for m in map(_FENCE_OPEN.match, text.split("\n")) if m]
    )
    # 每段前后都可能要补一个 "<fence>\n" / "\n<fence>"
    max_piece = max(1, limit - 2 * (widest + 1))
    pieces = []
    for line in text.split("\n"):
        while len(line) > max_piece:
            pieces.append(line[:max_piece])
            line = line[max_piece:]
        pieces.append(line)

    parts: list[str] = []
    cur = ""
    fence = None  # 当前 cur 末尾所在代码块的围栏
    for piece in pieces:
        new_fence = _fence_step(fence, piece)
        cand = cur + "\n" + piece if cur else piece
        if len(cand) + (len(new_fence) + 1 if new_fence else 0) <= limit:
            cur, fence = cand, new_fence
            continue
        parts.append(cur + "\n" + fence if fence else cur)
        cur = fence or ""
        cur = cur + "\n" + piece if cur else piece
        fence = new_fence
    if cur:
        parts.append(cur + "\n" + fence if fence else cur)
    return parts


_PLACEHOLDER = re.compile(r"(?:@_user_\d+|@_all)(?!\w)[ \t]*")


def strip_mentions(text: str) -> str:
    """Remove @_user_N / @_all placeholders only; user-written @path is kept."""
    return _PLACEHOLDER.sub("", text).strip()


_POST_TEXT_TAGS = {"text", "a", "md", "code_block"}


def _post_body(c: dict):
    """post 内容可能是 {"title","content"}，也可能按语言包一层 {"zh_cn": {...}}。"""
    if isinstance(c.get("content"), list):
        return c
    for v in c.values():
        if isinstance(v, dict) and isinstance(v.get("content"), list):
            return v
    return None


def render_content(msg_type, raw) -> str:
    """消息接口返回的 body.content（JSON 字符串）→ 纯文本。

    text 取 text 字段（@ 占位符 @_user_N 原样保留，交给 strip_mentions）；
    post 拼接各段落里的文字，@ 元素丢弃；其他类型返回空串。"""
    if not isinstance(raw, str):
        return ""
    try:
        c = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(c, dict):
        return ""
    if msg_type == "text":
        t = c.get("text")
        return t if isinstance(t, str) else ""
    if msg_type != "post":
        return ""
    body = _post_body(c)
    if body is None:
        return ""
    lines = []
    title = body.get("title")
    if isinstance(title, str) and title.strip():
        lines.append(title)
    for para in body["content"]:
        if not isinstance(para, list):
            continue
        parts = [
            el["text"]
            for el in para
            if isinstance(el, dict)
            and el.get("tag") in _POST_TEXT_TAGS
            and isinstance(el.get("text"), str)
        ]
        lines.append("".join(parts))
    return "\n".join(lines).strip()
