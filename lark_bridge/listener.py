"""全机唯一的飞书事件监听进程：按 chat_id 把群消息写进对应会话的收件箱。

飞书长连接对同一应用的多个连接是随机投递，所以整台机器只能有一个消费者。
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
from .text import strip_mentions

NOT_BRIDGED = "当前没有会话在桥接，回到电脑前在终端敲 /lark"
OFF_REPLY = "已断开，终端里敲 /lark 可重新连接"
UNSUPPORTED = "暂不支持该类型消息，请发文字"
HANDLE_FAILED = "处理失败，请重发"
NOTICE_INTERVAL = 600  # 同一个群的"没有会话在桥接"提示最多 10 分钟一次
SEND_TIMEOUT = 10
EVENT_KEY = "im.message.receive_v1"
_TEXT_TYPES = {"text", "post"}
KILL_GRACE = 5  # terminate 之后这么多秒读循环还没结束就 kill

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


def _stop_child(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass


def _process_line(line: str) -> None:
    line = line.strip()
    if not line:
        return
    try:
        ev = json.loads(line)
    except ValueError:
        return
    if not isinstance(ev, dict):
        return
    try:
        handle_event(ev, _load_owner())
    except Exception:
        _log(
            f"handle_event failed for {ev.get('message_id')}: {traceback.format_exc()}"
        )


def main(check_interval: float = 30, restart_delay: float = 5) -> int:
    if not _acquire_pid_file():
        return 0
    stop = threading.Event()
    cur = {"proc": None}
    lock = threading.RLock()  # SIGTERM 处理函数可能在主线程持锁时运行

    def request_stop():
        stop.set()
        with lock:
            proc = cur["proc"]
            _stop_child(proc)
        if proc is not None:
            # 读循环 KILL_GRACE 秒后还没结束（子进程不理 SIGTERM）就升级为 kill
            def escalate():
                if proc.poll() is None:
                    _log(f"lark-cli pid={proc.pid} ignored SIGTERM, killing")
                    try:
                        proc.kill()
                    except OSError:
                        pass

            t = threading.Timer(KILL_GRACE, escalate)
            t.daemon = True
            t.start()

    def on_sigterm(signum, frame):
        _log("SIGTERM received")
        request_stop()

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
                    request_stop()
            except Exception:
                _log(f"watchdog failed: {traceback.format_exc()}")

    try:
        if not _any_enabled() and _try_exit():
            _log("no enabled sessions at start, exiting")
            return 0
        _log("listener started")
        threading.Thread(target=watchdog, daemon=True).start()
        while not stop.is_set():
            proc = None
            try:
                with lock:
                    if stop.is_set():
                        break
                    with open(paths.listener_log(), "a") as errlog:
                        proc = subprocess.Popen(
                            [
                                paths.lark_cli(),
                                "event",
                                "consume",
                                EVENT_KEY,
                                "--as",
                                "bot",
                            ],
                            # lark-cli 把 stdin EOF 当作退出信号：给一根不写也不关的管道
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=errlog,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                        )
                    cur["proc"] = proc
                    if stop.is_set():  # SIGTERM 落在 Popen 与上一行之间
                        _stop_child(proc)
                _log(f"started lark-cli pid={proc.pid}")
                for line in proc.stdout:
                    _process_line(line)
                rc = proc.wait()
            except Exception:
                _log(f"lark-cli loop failed: {traceback.format_exc()}")
                rc = None
            finally:
                if proc is not None:
                    _stop_child(proc)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    if proc.stdout:
                        proc.stdout.close()
                    if proc.stdin:
                        proc.stdin.close()
            if stop.is_set():
                break
            _log(f"lark-cli exited rc={rc}, restarting in {restart_delay}s")
            stop.wait(restart_delay)
        return 0
    finally:
        stop.set()
        with lock:
            p = cur["proc"]
        if p is not None and p.poll() is None:
            p.kill()
            p.wait()
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
