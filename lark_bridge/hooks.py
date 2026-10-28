"""Claude Code hook 处理。run() 返回要写到 stdout 的文本（Rewake 写 stderr 并 exit 2），
出任何错都返回 ""。"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import feishu, paths, state, transcript
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

TERMINAL_PREFIX = "💻 终端："
MSG_ALLOWED = "✅ 已允许"
MSG_DENIED = "❌ 已拒绝"
MSG_PERM_HINT = "上一条消息不是「同意/拒绝」，已被忽略；请回复「同意」或「拒绝」"
MSG_PERM_TIMEOUT = "等待超时，已交回终端"
MSG_PERM_TERMINAL = "已在终端处理"
MSG_ASK_HANDBACK = "已交回终端"
MSG_ANSWERED = "✅ 已作答"
DENY_MESSAGE = "用户在飞书上拒绝了"
MSG_SESSION_ENDED = "会话已结束（/clear 或退出），桥接已断开。终端里敲 /lark 可重新连接"
# 这些 SessionStart 来源意味着"换了一段对话"：resume 不自动接回飞书（spec 第 9 节）。
# compact 是同一个活着的会话在压缩上下文，不能断开。
_START_DISCONNECT = ("startup", "resume", "clear")
# UserPromptSubmit 会卡住终端，发送不能等太久；SIGTERM 之后的告知同理
SHORT_SEND_TIMEOUT = 5


class Rewake(str):
    """asyncRewake hook 的输出：cli 把它写到 stderr 并以 exit 2 退出，
    Claude Code 据此唤醒模型，把文本当作下一条指令。"""


class ResolvedElsewhere(Exception):
    """确认框 / 选项框已在终端处理（transcript 里出现了 tool_result）。"""


class HookTerminated(BaseException):
    """收到 SIGTERM（hook 超时，或会话结束时 Claude Code 结束 hook）。

    继承 BaseException，这样 feishu.send 等处的 `except Exception` 不会吞掉它。
    """


# defer=True 之后 SIGTERM 只记录、不打断：游标已经 commit，结果必须交出去
_term = {"defer": False, "pending": False}


def _on_sigterm(signum, frame):
    if _term["defer"]:
        _term["pending"] = True
        return
    raise HookTerminated()


def _commit_final(sid, offset: int) -> None:
    """进入"必须交出结果"阶段：先让 SIGTERM 只记录，再 commit 游标。"""
    _term["defer"] = True
    state.commit_cursor(sid, offset)


def _consume_final(sid, message_ids) -> None:
    """同 _commit_final，但按 message_id 消费（回复不一定在游标处）。"""
    _term["defer"] = True
    state.consume(sid, message_ids)


def wait_seconds() -> int:
    """权限 / 提问 hook 等飞书回复的时长（hook timeout 是 3600）。"""
    return int(os.environ.get("CLAUDE_LARK_WAIT_SECONDS") or 3540)


def idle_seconds() -> int:
    """后台等待进程单次等多久（hook timeout 是 86400）。到点后用保活唤醒一次，
    这一轮结束时 Stop hook 再拉起新的等待进程。"""
    return int(os.environ.get("CLAUDE_LARK_IDLE_SECONDS") or 86100)


class Peeked(list):
    """peek 到的消息列表（文件顺序），附带 commit 用的偏移。"""

    offset: int = 0


def wait_for_messages(
    sid, deadline: float, now, sleep, since: int = 0, accept=None, watch=None
):
    """每 POLL_SECONDS 秒 peek 一次（只看收件箱里 since 偏移之后到达的消息，
    且 accept(m) 为真的；accept 为 None 时不过滤）。
    有消息 → 返回 Peeked（不 commit）；到 deadline 或桥接被关闭 → 返回 None。
    watch（ToolWatch）发现已在终端处理 → 抛 ResolvedElsewhere。
    SIGTERM 时 HookTerminated 向上抛出。"""
    while True:
        if watch is not None and watch.resolved():
            raise ResolvedElsewhere()
        msgs, offset = state.peek_inbox(sid, since)
        if accept is not None:
            msgs = [m for m in msgs if accept(m)]
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


def _create_order(msgs):
    """按 create_time 排序的下标（稳定）。"""
    return sorted(range(len(msgs)), key=lambda i: _sort_key(msgs[i]))


def _created_after(sent_at):
    """只认 create_time 不早于提示消息的回复（都是飞书服务端时间，没有时钟偏差）。

    防的是上一次提示的重复回复晚到、排在收件箱边界之后的情况。
    提示的 create_time 拿不到时返回 None，只按收件箱位置判断；
    回复自己的 create_time 不可用时也不拦。"""
    if sent_at is None:
        return None

    def accept(m) -> bool:
        t = feishu.to_ms(m.get("create_time"))
        return t is None or t >= sent_at

    return accept


def _text(m) -> str:
    t = m.get("text")
    return t if isinstance(t, str) else ""


def merge_texts(msgs) -> str:
    ordered = sorted(msgs, key=_sort_key)  # sorted() 是稳定的
    return JOIN_SEP.join(str(m.get("text", "")) for m in ordered)


def _stop(sid, data, binding, start, now, sleep) -> str:
    """asyncRewake 的 Stop hook：在后台运行，不占终端。

    先把最终回复发到群里，然后（全会话只留一个等待进程）等飞书消息：
    有消息 → Rewake(消息)，Claude 被唤醒处理；空闲太久 → Rewake(保活)。"""
    chat = binding.chat_id
    state.set_counter(sid, "busy", 0)
    if state.get_counter(sid, "keepalive_pending") == 1:
        state.set_counter(sid, "keepalive_pending", 0)
    else:
        reply = data.get("last_assistant_message")
        if isinstance(reply, str) and reply.strip():
            feishu.send(chat, reply)

    return _become_waiter(sid, start, now, sleep)


def _wait(sid, data, binding, start, now, sleep) -> str:
    """UserPromptSubmit 上的 asyncRewake hook：每个回合开始时补一个等待进程。

    飞书消息唤醒的回合里，投递消息的那个等待进程已经退出；用户在终端按 Esc
    打断回合时 Stop hook 不会触发。没有它，之后的飞书消息要等到下一次终端输入。"""
    return _become_waiter(sid, start, now, sleep)


def _become_waiter(sid, start, now, sleep) -> str:
    waiter = state.try_lock_waiter(sid)
    if waiter is None:
        return ""  # 已有等待进程在等
    try:
        return _wait_and_deliver(sid, start + idle_seconds(), now, sleep)
    finally:
        waiter.close()


def _wait_and_deliver(sid, deadline, now, sleep) -> str:
    while True:
        msgs = wait_for_messages(sid, deadline, now, sleep)
        if msgs is None:
            if not state.is_enabled(sid):
                return ""
            if state.get_counter(sid, "busy") == 1:
                # 正在干活：这一轮结束时 Stop hook 会拉起新的等待进程
                return ""
            state.set_counter(sid, "keepalive_pending", 1)
            return Rewake(KEEPALIVE_REASON)
        # 权限 / 提问 hook 正在等飞书回复时不投递：消息可能是它的答案
        lock = state.try_lock_interactive(sid)
        if lock is None:
            t = now()
            if t >= deadline:
                # 权限 / 提问 hook 只在干活时出现：这一轮结束时会有新的等待进程
                return ""
            sleep(min(POLL_SECONDS, deadline - t))
            continue
        try:
            msgs, offset = state.peek_inbox(sid)
            if not msgs:
                continue
            _commit_final(sid, offset)
        finally:
            lock.close()
        state.set_counter(sid, "busy", 1)
        return Rewake(DELIVER_PREFIX + merge_texts(msgs))


# 等待进程唤醒模型时，Claude Code 把唤醒内容包成这样的 prompt，也会触发 UserPromptSubmit
_NOTIFICATION_PREFIX = "<task-notification>"


def _prompt(sid, data, binding, start, now, sleep) -> str:
    prompt = data.get("prompt")
    if isinstance(prompt, str) and prompt.lstrip().startswith(_NOTIFICATION_PREFIX):
        # 后台通知（飞书消息投递、保活等），不是终端输入：不镜像，保活标记也不动
        return ""
    state.set_counter(sid, "busy", 1)
    # 用户在终端发了新 prompt，下一次 Stop 的回复是真回复，不能当保活回复跳过
    state.set_counter(sid, "keepalive_pending", 0)
    if isinstance(prompt, str) and prompt.strip():
        if not prompt.lstrip().startswith("/lark"):
            _spawn_mirror(sid, prompt)
    return ""


_MIRROR_PREFIX = "mirror-"


def _spawn_mirror(sid, text: str) -> None:
    """把终端输入交给一个脱离的后台进程发到群里，立刻返回：UserPromptSubmit
    会卡住终端，不能等飞书。文本经会话目录里的临时文件传过去。"""
    d = paths.session_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{_MIRROR_PREFIX}{time.time_ns()}-{os.getpid()}.txt"
    f.write_text(text, encoding="utf-8")
    script = Path(__file__).resolve().parent.parent / "bin" / "claude-lark"
    try:
        subprocess.Popen(
            [sys.executable, str(script), "mirror", "--session", sid, "--file", str(f)],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ),
        )
    except Exception:
        try:
            f.unlink()
        except OSError:
            pass
        raise


def mirror(sid, file_path: str) -> None:
    """后台进程入口：读出临时文件、删掉它、发到绑定的群。自身不抛出。"""
    try:
        d = paths.session_dir(sid).resolve()
        f = Path(file_path).resolve()
        if f.parent != d or not f.name.startswith(_MIRROR_PREFIX):
            feishu.log_error(f"mirror: refusing file outside session dir: {f}")
            return
        try:
            text = f.read_text(encoding="utf-8")
        finally:
            try:
                f.unlink()
            except OSError:
                pass
        b = state.load_binding(sid)
        if b is None:
            feishu.log_error(f"mirror: session {sid} has no binding")
            return
        feishu.send(b.chat_id, TERMINAL_PREFIX + text)
    except Exception:
        feishu.log_exception("mirror")


def _acquire_interactive(sid, deadline: float, now, sleep, watch=None):
    """轮询拿交互锁。拿到 → 文件对象；到 deadline 或桥接被关闭 → None。
    已在终端处理 → ResolvedElsewhere；SIGTERM 时 HookTerminated 从 sleep 里向上抛出。"""
    while True:
        if watch is not None and watch.resolved():
            raise ResolvedElsewhere()
        lock = state.try_lock_interactive(sid)
        if lock is not None:
            return lock
        t = now()
        if t >= deadline or not state.is_enabled(sid):
            return None
        sleep(min(POLL_SECONDS, deadline - t))


def _serialized(inner):
    """同一会话的权限 / 提问 hook 串行执行（Claude 可能并行调用工具）。

    锁从取收件箱边界之前一直持有到最终 consume 之后，
    这样一条"同意"只会被一个 hook 用掉，后一个 hook 的边界也在它之后。"""

    def handler(sid, data, binding, start, now, sleep):
        deadline = start + wait_seconds()
        watch = transcript.ToolWatch(
            data.get("transcript_path"), data.get("tool_name"), data.get("tool_input")
        )
        watch.resolved()  # 先记下已有的同名同参调用
        try:
            lock = _acquire_interactive(sid, deadline, now, sleep, watch)
        except ResolvedElsewhere:
            return ""
        if lock is None:
            return ""
        try:
            return inner(sid, data, binding, deadline, now, sleep, watch)
        finally:
            lock.close()

    return handler


def _permission(sid, data, binding, deadline, now, sleep, watch=None) -> str:
    if data.get("tool_name") == "AskUserQuestion":
        return _ask(sid, data, binding, deadline, now, sleep, watch)
    chat = binding.chat_id
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        raise ValueError(f"permission: tool_input is {type(tool_input).__name__}")
    # 只有提示发出之后才到达的消息算回复；之前积压的留给 Stop hook
    since = state.inbox_size(sid)
    _, sent_at = feishu.send_with_time(
        chat, format_permission(str(data.get("tool_name")), tool_input)
    )
    accept = _created_after(sent_at)
    try:
        while True:
            msgs = wait_for_messages(sid, deadline, now, sleep, since, accept, watch)
            if msgs is None:
                if state.is_enabled(sid):
                    feishu.send(chat, MSG_PERM_TIMEOUT)
                return ""
            done = []
            for i in _create_order(msgs):
                done.append(msgs[i].get("message_id"))
                verdict = parse_permission(_text(msgs[i]))
                if verdict is None:
                    continue
                _consume_final(sid, done)
                feishu.send(
                    chat,
                    MSG_ALLOWED if verdict is True else MSG_DENIED,
                    timeout=SHORT_SEND_TIMEOUT,
                )
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
            state.consume(sid, done)
            feishu.send(chat, MSG_PERM_HINT, timeout=SHORT_SEND_TIMEOUT)
    except (HookTerminated, ResolvedElsewhere):
        # 用户在终端处理了确认框
        feishu.send(chat, MSG_PERM_TERMINAL, timeout=SHORT_SEND_TIMEOUT)
        return ""


def _ask(sid, data, binding, deadline, now, sleep, watch=None) -> str:
    """AskUserQuestion 的 PermissionRequest：终端的选项框同时显示，谁先答算谁的。"""
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
    since = state.inbox_size(sid)
    _, sent_at = feishu.send_with_time(chat, format_questions(questions))
    try:
        msgs = wait_for_messages(
            sid, deadline, now, sleep, since, _created_after(sent_at), watch
        )
        if msgs is None:
            if state.is_enabled(sid):
                feishu.send(chat, MSG_ASK_HANDBACK)
            return ""
        first = _create_order(msgs)[0]
        answers = parse_answers(_text(msgs[first]), questions)
        _consume_final(sid, [msgs[first].get("message_id")])
    except HookTerminated:
        feishu.send(chat, MSG_ASK_HANDBACK, timeout=SHORT_SEND_TIMEOUT)
        return ""
    except ResolvedElsewhere:
        feishu.send(chat, MSG_PERM_TERMINAL, timeout=SHORT_SEND_TIMEOUT)
        return ""
    feishu.send(chat, MSG_ANSWERED, timeout=SHORT_SEND_TIMEOUT)
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {
                    "behavior": "allow",
                    "updatedInput": {**tool_input, "answers": answers},
                },
            }
        },
        ensure_ascii=False,
    )


def _session_end(sid, data) -> str:
    """SessionEnd（/clear、/resume 切走、退出）：断开桥接并告知群里。

    claude 进程在 /clear 后还活着，不断开的话监听进程会继续往这个
    已经不会再被读的收件箱里投消息，消息就悄悄丢了。"""
    state.set_enabled(sid, False)
    feishu.log_error(f"session {sid} ended ({data.get('reason')}), bridge disabled")
    b = state.load_binding(sid)
    if b is not None:
        feishu.send(b.chat_id, MSG_SESSION_ENDED, timeout=SHORT_SEND_TIMEOUT)
    return ""


def _session_start(sid, data) -> str:
    """SessionStart：一个已开启的 session_id 以 startup/resume/clear 开始，静默断开。"""
    if data.get("source") in _START_DISCONNECT:
        state.set_enabled(sid, False)
        feishu.log_error(
            f"session {sid} started ({data.get('source')}), stale bridge disabled"
        )
    return ""


# 不需要绑定、也不阻塞的 hook
_SESSION_HANDLERS = {
    "session-start": _session_start,
    "session-end": _session_end,
}

_HANDLERS = {
    "stop": _stop,
    "wait": _wait,
    "prompt": _prompt,
    # AskUserQuestion 也走 PermissionRequest（_permission 里分流）
    "permission": _serialized(_permission),
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


def run(
    event: str,
    stdin_text: str,
    now=time.monotonic,
    sleep=time.sleep,
    *,
    hold_sigterm: bool = False,
) -> str:
    """hold_sigterm=True（cli 用）：返回后 SIGTERM 仍保持"只记录"，
    直到调用方把结果写完 stdout，避免已 commit 的结果在写出前丢失。"""
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
        session_handler = _SESSION_HANDLERS.get(event)
        handler = _HANDLERS.get(event)
        if (handler is None and session_handler is None) or not state.is_enabled(sid):
            return ""
        if session_handler is not None:
            return session_handler(sid, data)
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
        # 先切到"只记录"，再恢复旧处理函数：cli 事先装的就是 _on_sigterm
        _term["defer"] = True
        _restore_sigterm(saved)
        _term["pending"] = False
        _term["defer"] = hold_sigterm
