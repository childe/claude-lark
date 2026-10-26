"""Claude Code hook 处理。run() 返回要写到 stdout 的文本，出任何错都返回 ""。"""

import json
import os
import signal
import threading
import time

from . import feishu, paths, state
from .text import (
    DELIVER_PREFIX,
    JOIN_SEP,
    KEEPALIVE_REASON,
    format_permission,
    format_questions,
    parse_answers,
    parse_permission,
)

POLL_SECONDS = 0.5
BLOCK_CAP = 1000
KEEPALIVE_WARN_AT = 950

MSG_CAP_REACHED = "续命次数已用完，会话已空闲，回到电脑前继续"
MSG_CAP_WARN = "剩余续命次数不多"
TERMINAL_PREFIX = "💻 终端："
MSG_ALLOWED = "✅ 已允许"
MSG_DENIED = "❌ 已拒绝"
MSG_PERM_HINT = "请回复「同意」或「拒绝」"
MSG_PERM_TIMEOUT = "等待超时，已交回终端"
MSG_PERM_TERMINAL = "已在终端处理"
MSG_ASK_HANDBACK = "已交回终端"
DENY_MESSAGE = "用户在飞书上拒绝了"
# UserPromptSubmit 会卡住终端，发送不能等太久；SIGTERM 之后的告知同理
SHORT_SEND_TIMEOUT = 5


class HookTerminated(BaseException):
    """收到 SIGTERM（hook 超时或用户在终端按 Esc）。

    继承 BaseException，这样 feishu.send 等处的 `except Exception` 不会吞掉它。
    """


# defer=True 之后 SIGTERM 只记录、不打断：游标已经 commit，结果必须交出去
_term = {"defer": False, "pending": False}


def _on_sigterm(signum, frame):
    if _term["defer"]:
        _term["pending"] = True
        return
    raise HookTerminated()


def _commit_final(sid, offset) -> None:
    """进入"必须交出结果"阶段：先让 SIGTERM 只记录，再 commit（offset 为 None 则不 commit）。"""
    _term["defer"] = True
    if offset is not None:
        state.commit_cursor(sid, offset)


def wait_seconds() -> int:
    return int(os.environ.get("CLAUDE_LARK_WAIT_SECONDS") or 3540)


class Peeked(list):
    """peek 到的消息列表（文件顺序），附带 commit 用的偏移。

    offset 是全部消息之后的偏移；ends[i] 是第 i 条消息所在行结束处的偏移。"""

    offset: int = 0
    ends: tuple = ()


def wait_for_messages(sid, deadline: float, now, sleep):
    """每 POLL_SECONDS 秒 peek 一次。有消息 → 返回 Peeked（不 commit）；
    到 deadline 或桥接被关闭 → 返回 None。SIGTERM 时 HookTerminated 向上抛出。"""
    while True:
        entries, offset = state.peek_inbox_entries(sid)
        if entries:
            out = Peeked(m for m, _ in entries)
            out.offset = offset
            out.ends = [e for _, e in entries]
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


def _create_order(msgs):
    """按 create_time 排序的下标（稳定）。"""
    return sorted(range(len(msgs)), key=lambda i: _sort_key(msgs[i]))


def _prefix_end(msgs, done):
    """done 里的下标在文件顺序上能连成的最长前缀的结束偏移；没有则 None。

    游标只能 commit 前缀，所以文件里排在前面、还没处理的消息不能被跳过。"""
    end = None
    for i in range(len(msgs)):
        if i not in done:
            break
        end = msgs.ends[i]
    return end


def _text(m) -> str:
    t = m.get("text")
    return t if isinstance(t, str) else ""


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
    if not msgs and not state.is_enabled(sid):
        return ""
    blocks = state.get_counter(sid, "blocks")
    if blocks + 1 >= BLOCK_CAP:
        # 消息留在收件箱里，用户回到终端后再处理
        feishu.send(chat, MSG_CAP_REACHED)
        return ""
    if msgs:
        reason = DELIVER_PREFIX + merge_texts(msgs)
        _commit_final(sid, msgs.offset)
        state.set_counter(sid, "blocks", blocks + 1)
        return json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False)

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
            feishu.send(
                binding.chat_id, TERMINAL_PREFIX + prompt, timeout=SHORT_SEND_TIMEOUT
            )
    return ""


def _permission(sid, data, binding, start, now, sleep) -> str:
    chat = binding.chat_id
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        raise ValueError(f"permission: tool_input is {type(tool_input).__name__}")
    feishu.send(chat, format_permission(str(data.get("tool_name")), tool_input))
    deadline = start + wait_seconds()
    try:
        while True:
            msgs = wait_for_messages(sid, deadline, now, sleep)
            if msgs is None:
                if state.is_enabled(sid):
                    feishu.send(chat, MSG_PERM_TIMEOUT)
                return ""
            done = set()
            for i in _create_order(msgs):
                done.add(i)
                verdict = parse_permission(_text(msgs[i]))
                if verdict is None:
                    continue
                _commit_final(sid, _prefix_end(msgs, done))
                feishu.send(chat, MSG_ALLOWED if verdict is True else MSG_DENIED)
                decision = {"behavior": "allow"}
                if verdict is not True:
                    decision = {"behavior": "deny", "message": DENY_MESSAGE}
                return json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PermissionRequest",
                            "decision": decision,
                        }
                    },
                    ensure_ascii=False,
                )
            # 整批都不是明确的同意/拒绝：消费掉，提示后继续等
            state.commit_cursor(sid, msgs.offset)
            feishu.send(chat, MSG_PERM_HINT)
    except HookTerminated:
        # 用户在终端处理了确认框
        feishu.send(chat, MSG_PERM_TERMINAL, timeout=SHORT_SEND_TIMEOUT)
        return ""


def _ask(sid, data, binding, start, now, sleep) -> str:
    chat = binding.chat_id
    tool_input = data.get("tool_input")
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if (
        not isinstance(questions, list)
        or not questions
        or not all(
            isinstance(q, dict) and isinstance(q.get("question"), str)
            for q in questions
        )
    ):
        raise ValueError("ask: malformed tool_input.questions")
    feishu.send(chat, format_questions(questions))
    try:
        msgs = wait_for_messages(sid, start + wait_seconds(), now, sleep)
        if msgs is None:
            if state.is_enabled(sid):
                feishu.send(chat, MSG_ASK_HANDBACK)
            return ""
        first = _create_order(msgs)[0]
        answers = parse_answers(_text(msgs[first]), questions)
        _commit_final(sid, _prefix_end(msgs, {first}))
    except HookTerminated:
        feishu.send(chat, MSG_ASK_HANDBACK, timeout=SHORT_SEND_TIMEOUT)
        return ""
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {**tool_input, "answers": answers},
            }
        },
        ensure_ascii=False,
    )


_HANDLERS = {
    "stop": _stop,
    "prompt": _prompt,
    "permission": _permission,
    "ask": _ask,
}


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
    _term["defer"] = _term["pending"] = False
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
        _term["defer"] = _term["pending"] = False
