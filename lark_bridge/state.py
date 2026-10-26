"""会话状态：绑定、开关、收件箱与游标、计数器、群占用。"""

import contextlib
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
    """enabled 文件存在，且（记了 pid 时）那个 claude 进程还是当初那个。

    文件内容是 "<pid>\\n<启动时间>"：pid 活着且启动时间一致才算开启，
    pid 被别的进程复用（例如重启后）就不算。只有 pid 的旧格式也不算。
    空文件（没有 pid）算开启。判定为失效时顺手删掉 enabled。"""
    f = paths.session_dir(sid) / "enabled"
    try:
        content = f.read_text().strip()
    except OSError:
        return False
    if not content:
        return True
    pid_s, _, start = content.partition("\n")
    start = " ".join(start.split())
    try:
        pid = int(pid_s.strip())
    except ValueError:
        pid = None
    alive = pid is not None and bool(start) and procs.pid_alive(pid)
    if alive:
        now = procs.start_time(pid)
        # ps 偶尔取不到（进程刚好退出等）时无法否定，按 pid 存活判断
        alive = not now or now == start
    if not alive:
        try:
            f.unlink()
        except OSError:
            pass
    return alive


def enabled_at_ms(sid):
    """最近一次开启的时间（enabled 文件的 mtime，毫秒）；没开启返回 None。"""
    try:
        return int((paths.session_dir(sid) / "enabled").stat().st_mtime * 1000)
    except OSError:
        return None


def set_enabled(sid, on: bool, claude_pid=None):
    """claude_pid 不为 None 时连同它的启动时间一起记下（取不到启动时间则
    is_enabled 会判定为未开启，调用方应检查）。"""
    f = paths.session_dir(sid) / "enabled"
    if on:
        if claude_pid is None:
            _atomic_write(f, "")
        else:
            _atomic_write(f, f"{claude_pid}\n{procs.start_time(claude_pid)}")
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


@contextlib.contextmanager
def _consumed_lock(sid):
    """游标与 consumed.txt 的读-改-写都在这把锁里做（持有时间很短）。"""
    p = paths.session_dir(sid) / "consumed.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def try_lock_interactive(sid):
    """非阻塞地拿会话的交互锁（权限 / 提问 hook 串行）。拿到返回文件对象（close 即释放），否则 None。"""
    p = paths.session_dir(sid) / "interactive.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    except BaseException:
        f.close()
        raise
    return f


def commit_cursor(sid, offset: int):
    """游标前移到 offset，并从 consumed 集合里删掉被游标越过的 id。"""
    with _consumed_lock(sid):
        _commit_cursor_locked(sid, offset)


def _commit_cursor_locked(sid, offset: int):
    passed = {_mid(m) for m, _, end in _scan(sid)[0] if m is not None and end <= offset}
    _atomic_write(paths.session_dir(sid) / "inbox.cursor", str(offset))
    consumed = _consumed(sid)
    if consumed & passed:
        _write_consumed(sid, consumed - passed)


def consume(sid, message_ids):
    """把这些消息标记为已消费（不再被 peek 到），再把游标推过开头连续的已消费行。

    用于权限 / 提问的回复：它们不一定在游标处，不能直接 commit 偏移。"""
    with _consumed_lock(sid):
        consumed = _consumed(sid) | {str(x) for x in message_ids}
        _write_consumed(sid, consumed)
        rows, _ = _scan(sid)
        offset = None
        for m, _, end in rows:
            if m is not None and _mid(m) not in consumed:
                break
            offset = end
        if offset is not None:
            _commit_cursor_locked(sid, offset)


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
