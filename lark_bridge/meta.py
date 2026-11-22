"""读取 Claude Code 的会话元数据 ~/.claude/sessions/<pid>.json。

文件按 claude 进程的 pid 命名，pid 可能被复用，所以只认 sessionId 等于本会话的。"""

import json
from typing import Optional

from . import paths


def read(pid, sid: str) -> Optional[dict]:
    """不存在、解析失败、不是对象、sessionId 不是 sid 时返回 None。"""
    if not isinstance(pid, int):
        return None
    try:
        data = json.loads((paths.claude_sessions_dir() / f"{pid}.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("sessionId") != sid:
        return None
    return data


def is_interactive(pid, sid: str) -> bool:
    m = read(pid, sid)
    return m is not None and m.get("kind") == "interactive"


def session_name(pid, sid: str) -> str:
    m = read(pid, sid)
    name = m.get("name") if m else None
    return name.strip() if isinstance(name, str) else ""
