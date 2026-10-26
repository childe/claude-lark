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
            data = f.read()
            for line in data.split(b"\n"):
                try:
                    if json.loads(line).get("message_id") == msg["message_id"]:
                        return
                except (ValueError, AttributeError):
                    continue
            f.seek(0, os.SEEK_END)
            if data and not data.endswith(b"\n"):
                # 修复被截断的上一行，否则它会吞掉这条消息
                f.write(b"\n")
            f.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
            f.flush()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _cursor(sid) -> int:
    try:
        return int((paths.session_dir(sid) / "inbox.cursor").read_text().strip())
    except (OSError, ValueError):
        return 0


def _consumed_path(sid):
    return paths.session_dir(sid) / "consumed.txt"


def _mid(m) -> str:
    return str(m.get("message_id"))


def _consumed(sid) -> set:
    try:
        return {x for x in _consumed_path(sid).read_text().split("\n") if x}
    except OSError:
        return set()


def _write_consumed(sid, ids: set):
    if ids:
        _atomic_write(_consumed_path(sid), "".join(f"{x}\n" for x in sorted(ids)))
    else:
        try:
            _consumed_path(sid).unlink()
        except OSError:
            pass


def _scan(sid):
    """游标之后的完整行：([(消息或 None, 行首偏移, 行尾偏移)], 完整行结束偏移)。"""
    start = _cursor(sid)
    try:
        with open(_inbox(sid), "rb") as f:
            f.seek(start)
            data = f.read()
    except OSError:
        return [], start
    end = data.rfind(b"\n") + 1
    rows = []
    pos = 0
    while pos < end:
        nl = data.index(b"\n", pos)
        line = data[pos:nl]
        m = None
        if line.strip():
            try:
                m = json.loads(line)
            except ValueError:
                pass
        rows.append((m if isinstance(m, dict) else None, start + pos, start + nl + 1))
        pos = nl + 1
    return rows, start + end


def inbox_size(sid) -> int:
    """收件箱当前字节数（等正在进行的 append 写完）。用作"此后到达"的边界。"""
    try:
        with open(_inbox(sid), "rb") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return os.fstat(f.fileno()).st_size
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        return 0


def peek_inbox(sid, since: int = 0):
    """返回游标之后、行首偏移 >= since、且未被 consume 的消息与新偏移，不移动游标。

    新偏移是全部完整行之后的位置；commit 它等于"游标之后的都处理过了"。"""
    rows, offset = _scan(sid)
    consumed = _consumed(sid)
    msgs = [
        m
        for m, start, _ in rows
        if m is not None and start >= since and _mid(m) not in consumed
    ]
    return msgs, offset


def commit_cursor(sid, offset: int):
    """游标前移到 offset，并从 consumed 集合里删掉被游标越过的 id。"""
    passed = {_mid(m) for m, _, end in _scan(sid)[0] if m is not None and end <= offset}
    _atomic_write(paths.session_dir(sid) / "inbox.cursor", str(offset))
    consumed = _consumed(sid)
    if consumed & passed:
        _write_consumed(sid, consumed - passed)


def consume(sid, message_ids):
    """把这些消息标记为已消费（不再被 peek 到），再把游标推过开头连续的已消费行。

    用于权限 / 提问的回复：它们不一定在游标处，不能直接 commit 偏移。"""
    consumed = _consumed(sid) | {str(x) for x in message_ids}
    _write_consumed(sid, consumed)
    rows, _ = _scan(sid)
    offset = None
    for m, _, end in rows:
        if m is not None and _mid(m) not in consumed:
            break
        offset = end
    if offset is not None:
        commit_cursor(sid, offset)


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
