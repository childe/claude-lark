"""状态目录与外部命令路径。测试通过环境变量隔离。"""

import os
import re
from pathlib import Path

_SESSION_ID_RE = re.compile(r"[0-9a-f-]+")


def home() -> Path:
    env = os.environ.get("CLAUDE_LARK_HOME")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "lark"


def session_dir(session_id: str) -> Path:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"invalid session id: {session_id!r}")
    return home() / "sessions" / session_id


def config_file() -> Path:
    return home() / "config.json"


def listener_pid_file() -> Path:
    return home() / "listener.pid"


def listener_log() -> Path:
    return home() / "listener.log"


def lark_cli() -> str:
    return os.environ.get("CLAUDE_LARK_CLI") or "lark-cli"
