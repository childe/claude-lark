"""Claude Code hook 处理。run() 返回要写到 stdout 的文本，出任何错都返回 ""。"""

import json
import os
import signal
import threading
import time

from . import feishu, paths, state
from .text import DELIVER_PREFIX, JOIN_SEP, KEEPALIVE_REASON

POLL_SECONDS = 0.5
BLOCK_CAP = 1000
KEEPALIVE_WARN_AT = 950

MSG_CAP_REACHED = "续命次数已用完，会话已空闲，回到电脑前继续"
MSG_CAP_WARN = "剩余续命次数不多"
TERMINAL_PREFIX = "💻 终端："


class HookTerminated(BaseException):
    """收到 SIGTERM（hook 超时或用户在终端按 Esc）。

    继承 BaseException，这样 feishu.send 等处的 `except Exception` 不会吞掉它。
    """


def _on_sigterm(signum, frame):
    raise HookTerminated()


def wait_seconds() -> int:
    return int(os.environ.get("CLAUDE_LARK_WAIT_SECONDS") or 3540)


class Peeked(list):
    """peek 到的消息列表，附带 commit 用的偏移。"""

    offset: int = 0


def wait_for_messages(sid, deadline: float, now, sleep):
    """每 POLL_SECONDS 秒 peek 一次。有消息 → 返回 Peeked（不 commit）；
    到 deadline 或桥接被关闭 → 返回 None。SIGTERM 时 HookTerminated 向上抛出。"""
    while True:
        msgs, offset = state.peek_inbox(sid)
        if msgs:
            out = Peeked(msgs)
            out.offset = offset
            return out
        t = now()
        if t >= deadline or not state.is_enabled(sid):
            return None
        sleep(min(POLL_SECONDS, deadline - t))


def _sort_key(m):
    try:
        return int(m.get("create_time"))
    except (TypeError, ValueError):
        return 0


def merge_texts(msgs) -> str:
    ordered = sorted(msgs, key=_sort_key)  # sorted() 是稳定的
    return JOIN_SEP.join(str(m.get("text", "")) for m in ordered)


def _stop(sid, data, binding, start, now, sleep) -> str:
    chat = binding.chat_id
    if state.get_counter(sid, "keepalive_pending") == 1:
        state.set_counter(sid, "keepalive_pending", 0)
    else:
        reply = data.get("last_assistant_message")
        if isinstance(reply, str) and reply.strip():
            feishu.send(chat, reply)

    msgs = wait_for_messages(sid, start + wait_seconds(), now, sleep)
    if msgs:
        reason = DELIVER_PREFIX + merge_texts(msgs)
        state.commit_cursor(sid, msgs.offset)
        state.set_counter(sid, "blocks", state.get_counter(sid, "blocks") + 1)
        return json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False)

    if not state.is_enabled(sid):
        return ""
    blocks = state.get_counter(sid, "blocks")
    if blocks + 1 >= BLOCK_CAP:
        feishu.send(chat, MSG_CAP_REACHED)
        return ""
    blocks += 1
    state.set_counter(sid, "blocks", blocks)
    state.set_counter(sid, "keepalive_pending", 1)
    if blocks == KEEPALIVE_WARN_AT:
        feishu.send(chat, MSG_CAP_WARN)
    return json.dumps(
        {"decision": "block", "reason": KEEPALIVE_REASON}, ensure_ascii=False
    )


def _prompt(sid, data, binding, start, now, sleep) -> str:
    state.set_counter(sid, "blocks", 0)
    # 用户在终端发了新 prompt，下一次 Stop 的回复是真回复，不能当保活回复跳过
    state.set_counter(sid, "keepalive_pending", 0)
    prompt = data.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        if not prompt.lstrip().startswith("/lark"):
            feishu.send(binding.chat_id, TERMINAL_PREFIX + prompt)
    return ""


_HANDLERS = {"stop": _stop, "prompt": _prompt}


def _install_sigterm():
    if threading.current_thread() is not threading.main_thread():
        return None
    try:
        return (signal.signal(signal.SIGTERM, _on_sigterm),)
    except (ValueError, OSError):
        return None


def _restore_sigterm(saved):
    if saved is None:
        return
    try:
        signal.signal(signal.SIGTERM, saved[0])
    except (ValueError, OSError, TypeError):
        pass


def run(event: str, stdin_text: str, now=time.monotonic, sleep=time.sleep) -> str:
    start = now()
    saved = None
    try:
        try:
            data = json.loads(stdin_text)
        except (TypeError, ValueError):
            return ""
        if not isinstance(data, dict):
            return ""
        sid = data.get("session_id")
        try:
            paths.session_dir(sid)
        except ValueError:
            return ""
        handler = _HANDLERS.get(event)
        if handler is None or not state.is_enabled(sid):
            return ""
        binding = state.load_binding(sid)
        if binding is None:
            feishu.log_error(f"hook {event}: session {sid} enabled but has no binding")
            return ""
        saved = _install_sigterm()
        return handler(sid, data, binding, start, now, sleep)
    except HookTerminated:
        return ""
    except Exception:
        feishu.log_exception(f"hook {event}")
        return ""
    finally:
        _restore_sigterm(saved)
