"""全机唯一的飞书消息监听进程：按 chat_id 把群消息写进对应会话的收件箱。

以 user 身份轮询已绑定的群（GET /open-apis/im/v1/messages），不用事件长连接：
同一应用若已有别的后端连着长连接，飞书会把事件随机投给其中一个，本机收不全。
"""

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import feishu, paths, procs, state
from .text import render_content, strip_mentions

NOT_BRIDGED = "当前没有会话在桥接，回到电脑前在终端敲 /lark"
OFF_REPLY = "已断开，终端里敲 /lark 可重新连接"
UNSUPPORTED = "暂不支持该类型消息，请发文字"
HANDLE_FAILED = "处理失败，请重发"
NOTICE_INTERVAL = 600  # 同一个群的"没有会话在桥接"提示最多 10 分钟一次
SEND_TIMEOUT = 10
_TEXT_TYPES = {"text", "post"}
POLL_INTERVAL = 2  # 开启中的群多久拉一次
IDLE_POLL_INTERVAL = 60  # 已绑定但没开启的群多久拉一次（只为回"没有会话在桥接"）
POLL_TIMEOUT = 15
CLOCK_SLACK_MS = 5000  # 开启时间用本机时钟，消息时间是飞书服务端时钟
_SEEN_MAX = 2000

_notice_at = {}  # chat_id -> 上次发"没有会话"提示的时间
_now = time.monotonic
_owner_missing = {"logged": False}


def _log(msg: str) -> None:
    """追加一行到 listener.log，自身绝不抛出。"""
    try:
        p = paths.listener_log()
        p.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(p, "a") as f:
            f.write(f"{stamp} [{os.getpid()}] {msg.rstrip()}\n")
    except Exception:
        pass


def _reply(chat_id: str, text: str) -> bool:
    return feishu.send(chat_id, text, timeout=SEND_TIMEOUT)


def _clean_text(ev: dict) -> str:
    text = ev.get("content")
    if not isinstance(text, str):
        return ""
    text = strip_mentions(text)
    mentions = ev.get("mentions")
    if isinstance(mentions, list):
        for m in mentions:
            if not isinstance(m, dict):
                continue
            for field in ("key", "name"):
                v = m.get(field)
                if isinstance(v, str) and v:
                    text = text.replace("@" + v.lstrip("@"), "")
    return text.strip()


def _load_owner():
    try:
        oid = json.loads(paths.config_file().read_text()).get("owner_open_id")
    except (OSError, ValueError, AttributeError):
        oid = None
    if isinstance(oid, str) and oid:
        _owner_missing["logged"] = False
        return oid
    if not _owner_missing["logged"]:
        _owner_missing["logged"] = True
        _log("owner_open_id missing in config.json; dropping all messages")
    return None


def handle_event(ev: dict, owner: str) -> None:
    if not isinstance(ev, dict) or not owner or ev.get("sender_id") != owner:
        return
    chat_id = ev.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id:
        return
    try:
        _route(ev, chat_id)
    except Exception:
        # 绑定者的消息不能悄悄丢：记日志并让他重发
        _log(
            f"handle_event failed for {ev.get('message_id')}: {traceback.format_exc()}"
        )
        try:
            _reply(chat_id, HANDLE_FAILED)
        except Exception:
            _log(f"reply {HANDLE_FAILED} failed: {traceback.format_exc()}")


def _route(ev: dict, chat_id: str) -> None:
    b = state.find_enabled_by_chat(chat_id)
    if b is None:
        if any(x.chat_id == chat_id for x in state.all_bindings()):
            last = _notice_at.get(chat_id)
            now = _now()
            if last is None or now - last >= NOTICE_INTERVAL:
                if _reply(chat_id, NOT_BRIDGED):
                    _notice_at[chat_id] = now
        return
    mtype = ev.get("message_type")
    if mtype is not None and mtype not in _TEXT_TYPES:
        _reply(chat_id, UNSUPPORTED)
        return
    message_id = ev.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        return
    text = _clean_text(ev)
    if not text:
        return
    if text == "/off":
        state.set_enabled(b.session_id, False)
        _reply(chat_id, OFF_REPLY)
        _log(f"session {b.session_id} disabled by /off")
        return
    state.append_inbox(
        b.session_id,
        {"message_id": message_id, "text": text, "create_time": ev.get("create_time")},
    )


# ---------- 进程管理 ----------


def _read_pid():
    try:
        return int(paths.listener_pid_file().read_text().strip())
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def _pid_lock():
    """短锁：监听进程的"拿存活锁 / 决定退出并放锁"与 ensure_running 的
    "看有没有监听进程 → 拉起"互斥，否则开启中的会话可能没人监听。"""
    p = paths.home() / "listener.pid.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# 监听进程整个生命周期持有 listener.lock 的排他 flock。"有没有监听进程在跑"
# 只看这把锁：进程死了锁自动释放，不受 pid 复用影响。pid 文件只用于展示和 kill。
_life = {"f": None}


def _lock_held_by_other() -> bool:
    p = paths.listener_lock_file()
    if not p.exists():
        return False
    try:
        f = open(p, "a+")
    except OSError:
        return False
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        f.close()  # 拿到了也立刻放掉
    return False


def _acquire_pid_file() -> bool:
    """拿存活锁并写 pid 文件；锁被别人持有（已有监听进程）返回 False。"""
    with _pid_lock():
        p = paths.listener_lock_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        f = open(p, "a+")  # Python 打开的 fd 不会被子进程继承
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            return False
        except BaseException:
            f.close()
            raise
        _life["f"] = f
        state._atomic_write(paths.listener_pid_file(), str(os.getpid()))
        return True


def _drop_life_lock_locked() -> None:
    """调用方持有 _pid_lock：删掉自己的 pid 文件，放掉存活锁。"""
    try:
        if _read_pid() == os.getpid():
            paths.listener_pid_file().unlink()
    except OSError:
        pass
    f, _life["f"] = _life["f"], None
    if f is not None:
        f.close()


def _release_pid_file() -> None:
    try:
        with _pid_lock():
            _drop_life_lock_locked()
    except OSError:
        pass


def _try_exit() -> bool:
    """在 pid 锁里做退出决定，与 ensure_running 互斥。

    复查一遍：若期间有会话开启（它的 ensure_running 看到锁被我们持有而没拉起
    新进程），就继续跑，返回 False；否则删 pid 文件、放掉存活锁，返回 True，
    调用方退出。放锁之后 ensure_running 就会拉起新的监听进程。"""
    with _pid_lock():
        if _any_enabled():
            _log("session enabled during shutdown, keep running")
            return False
        _drop_life_lock_locked()
        return True


def _any_enabled() -> bool:
    """顺手把 claude 进程已退出的会话关掉；返回是否还有开启中的会话。"""
    alive = False
    for b in state.all_bindings():
        if not state.is_enabled(b.session_id):
            continue
        if b.claude_pid and not procs.pid_alive(b.claude_pid):
            state.set_enabled(b.session_id, False)
            _log(f"session {b.session_id} disabled: claude pid {b.claude_pid} gone")
            continue
        alive = True
    return alive


def item_to_event(item: dict, chat_id: str) -> dict:
    """消息接口的 item → handle_event 认的事件格式。"""
    sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
    body = item.get("body") if isinstance(item.get("body"), dict) else {}
    mtype = item.get("msg_type")
    return {
        "chat_id": item.get("chat_id") or chat_id,
        "message_id": item.get("message_id"),
        "message_type": mtype,
        "sender_id": sender.get("id") if sender.get("id_type") == "open_id" else None,
        "sender_type": sender.get("sender_type"),
        "create_time": item.get("create_time"),
        "mentions": item.get("mentions") or [],
        "content": render_content(mtype, body.get("content")),
    }


def _process_event(ev: dict) -> None:
    _log(
        f"event {ev.get('message_id')} type={ev.get('message_type')}"
        f" chat={ev.get('chat_id')} sender_type={ev.get('sender_type')}"
    )
    try:
        handle_event(ev, _load_owner())
    except Exception:
        _log(
            f"handle_event failed for {ev.get('message_id')}: {traceback.format_exc()}"
        )


def _cursor_file() -> Path:
    return paths.home() / "poll_cursors.json"


def _load_cursors() -> dict:
    try:
        data = json.loads(_cursor_file().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, int)}


class _Poller:
    """每个群记一个 message_position 游标（持久化，监听进程重启不重放）。

    只处理游标之后、且 create_time 不早于"起点"的消息：开启中的群起点是开启时间，
    所以关着时发的消息不会在重新开启后补发；没开启的群起点是本进程启动时间。"""

    def __init__(self, idle_interval: float):
        self.idle_interval = idle_interval
        self.cursors = _load_cursors()
        self.started_ms = int(time.time() * 1000)
        self.idle_at = {}  # chat_id -> 上次拉取（monotonic）
        self.failing = set()
        self.seen = []  # 最近处理过的 message_id（兜底：拿不到 position 的消息）

    def targets(self) -> dict:
        out = {}
        idle = set()
        for b in state.all_bindings():
            if not b.chat_id:
                continue
            if state.is_enabled(b.session_id):
                t = state.enabled_at_ms(b.session_id)
                since = (t - CLOCK_SLACK_MS) if t else self.started_ms
                out[b.chat_id] = min(since, out.get(b.chat_id, since))
            else:
                idle.add(b.chat_id)
        now = _now()
        for chat in idle - set(out):
            last = self.idle_at.get(chat)
            if last is None or now - last >= self.idle_interval:
                self.idle_at[chat] = now
                out[chat] = self.started_ms - CLOCK_SLACK_MS
        return out

    def poll_once(self, stop) -> None:
        for chat, since in self.targets().items():
            if stop.is_set():
                return
            try:
                self.poll_chat(chat, since, stop)
            except Exception as e:
                if chat not in self.failing:
                    self.failing.add(chat)
                    _log(f"poll {chat} failed: {e}")
                continue
            if chat in self.failing:
                self.failing.discard(chat)
                _log(f"poll {chat} recovered")

    def poll_chat(self, chat: str, since: int, stop) -> None:
        cur = self.cursors.get(chat)
        items = feishu.list_messages(chat, after_position=cur, timeout=POLL_TIMEOUT)
        changed = False
        for it in items:
            if stop.is_set():
                break
            pos = feishu.position(it)
            if cur is not None and 0 <= pos <= cur:
                continue
            mid = it.get("message_id")
            if mid in self.seen:
                continue
            ct = feishu.to_ms(it.get("create_time"))
            if not it.get("deleted") and (ct is None or ct >= since):
                _process_event(item_to_event(it, chat))
            if isinstance(mid, str):
                self.seen.append(mid)
                del self.seen[:-_SEEN_MAX]
            if pos > self.cursors.get(chat, -1):
                self.cursors[chat] = pos
                changed = True
        if changed:
            state._atomic_write(_cursor_file(), json.dumps(self.cursors))


def main(
    check_interval: float = 30,
    poll_interval: float = POLL_INTERVAL,
    idle_interval: float = IDLE_POLL_INTERVAL,
) -> int:
    if not _acquire_pid_file():
        return 0
    stop = threading.Event()

    def on_sigterm(signum, frame):
        _log("SIGTERM received")
        stop.set()

    old_handler = None
    try:
        old_handler = signal.signal(signal.SIGTERM, on_sigterm)
    except ValueError:  # 不在主线程（测试）
        pass

    def watchdog():
        while not stop.wait(check_interval):
            try:
                if not _any_enabled() and _try_exit():
                    _log("no enabled sessions, exiting")
                    stop.set()
            except Exception:
                _log(f"watchdog failed: {traceback.format_exc()}")

    try:
        if not _any_enabled() and _try_exit():
            _log("no enabled sessions at start, exiting")
            return 0
        _log("listener started (polling as user)")
        threading.Thread(target=watchdog, daemon=True).start()
        poller = _Poller(idle_interval)
        while not stop.is_set():
            try:
                poller.poll_once(stop)
            except Exception:
                _log(f"poll loop failed: {traceback.format_exc()}")
            stop.wait(poll_interval)
        return 0
    finally:
        stop.set()
        _release_pid_file()
        _log("listener exited")
        if old_handler is not None:
            try:
                signal.signal(signal.SIGTERM, old_handler)
            except ValueError:
                pass


def is_running() -> bool:
    """监听进程是否存活：listener.lock 被别人持有即存活（与 pid 文件无关）。"""
    return _lock_held_by_other()


def ensure_running() -> None:
    """监听进程没在跑就在后台拉起一个。自身不抛出。"""
    try:
        # 与监听进程的退出决定（_try_exit）互斥：要么它看到我们刚开启的会话而留下，
        # 要么我们看到存活锁已放掉而拉起新进程
        with _pid_lock():
            if _lock_held_by_other():
                return
            log = paths.listener_log()
            log.parent.mkdir(parents=True, exist_ok=True)
            script = Path(__file__).resolve().parent.parent / "bin" / "claude-lark"
            with open(log, "a") as out:
                subprocess.Popen(
                    [sys.executable, str(script), "listen"],
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=out,
                    env=dict(os.environ),
                )
    except Exception:
        feishu.log_exception("listener.ensure_running")
