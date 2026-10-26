"""会话状态：绑定、开关、收件箱与游标、计数器、群占用。"""

import fcntl
import json
import os
from dataclasses import asdict, dataclass
from typing import Optional

from . import paths, procs


@dataclass
class Binding:
    session_id: str
    chat_id: str
    chat_name: str
    cwd: str
    claude_pid: Optional[int]
    created_at: float


def _atomic_write(path, data: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def load_binding(sid):
    p = paths.session_dir(sid) / "binding.json"
    try:
        return Binding(**json.loads(p.read_text()))
    except (OSError, ValueError, TypeError):
        return None


def save_binding(b: Binding):
    _atomic_write(
        paths.session_dir(b.session_id) / "binding.json",
        json.dumps(asdict(b), ensure_ascii=False),
    )


def is_enabled(sid) -> bool:
    f = paths.session_dir(sid) / "enabled"
    try:
        content = f.read_text().strip()
    except OSError:
        return False
    if content:
        try:
            pid = int(content)
        except ValueError:
            pid = None
        if pid is not None and not procs.pid_alive(pid):
            try:
                f.unlink()
            except OSError:
                pass
            return False
    return True


def set_enabled(sid, on: bool, claude_pid=None):
    f = paths.session_dir(sid) / "enabled"
    if on:
        _atomic_write(f, "" if claude_pid is None else str(claude_pid))
    else:
        try:
            f.unlink()
        except OSError:
            pass


def all_bindings():
    root = paths.home() / "sessions"
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        try:
            b = load_binding(d.name)
        except ValueError:
            continue
        if b:
            out.append(b)
    return out


def find_enabled_by_chat(chat_id):
    for b in all_bindings():
        if b.chat_id == chat_id and is_enabled(b.session_id):
            return b
    return None


def _inbox(sid):
    return paths.session_dir(sid) / "inbox.jsonl"


def append_inbox(sid, msg: dict):
    p = _inbox(sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+b") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            for line in f.read().split(b"\n"):
                try:
                    if json.loads(line).get("message_id") == msg["message_id"]:
                        return
                except (ValueError, AttributeError):
                    continue
            f.seek(0, os.SEEK_END)
            f.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _cursor(sid) -> int:
    try:
        return int((paths.session_dir(sid) / "inbox.cursor").read_text().strip())
    except (OSError, ValueError):
        return 0


def peek_inbox(sid):
    """返回游标之后的完整行与新偏移，不移动游标。"""
    start = _cursor(sid)
    try:
        with open(_inbox(sid), "rb") as f:
            f.seek(start)
            data = f.read()
    except OSError:
        return [], start
    end = data.rfind(b"\n") + 1
    msgs = []
    for line in data[:end].split(b"\n"):
        if not line.strip():
            continue
        try:
            msgs.append(json.loads(line))
        except ValueError:
            continue
    return msgs, start + end


def commit_cursor(sid, offset: int):
    _atomic_write(paths.session_dir(sid) / "inbox.cursor", str(offset))


def get_counter(sid, name) -> int:
    try:
        return int((paths.session_dir(sid) / f"counter_{name}").read_text().strip())
    except (OSError, ValueError):
        return 0


def set_counter(sid, name, v: int):
    _atomic_write(paths.session_dir(sid) / f"counter_{name}", str(int(v)))


def chat_occupant(chat_id, except_sid):
    for b in all_bindings():
        if b.chat_id != chat_id or b.session_id == except_sid:
            continue
        if is_enabled(b.session_id) and procs.pid_alive(b.claude_pid):
            return b
    return None
