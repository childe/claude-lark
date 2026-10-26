import json
import signal

import pytest

from lark_bridge import feishu, hooks, state
from lark_bridge.text import (
    DELIVER_PREFIX,
    JOIN_SEP,
    format_permission,
    format_questions,
)
from test_hooks_stop import (  # noqa: F401 - fixtures
    SID,
    Clock,
    bridged,
    fake,
    msg,
    prompt_input,
    sent_texts,
    stop_input,
)

BASH_INPUT = {"command": "rm -rf build", "description": "Remove build dir"}
QUESTIONS = [
    {
        "question": "Which color?",
        "header": "Color",
        "options": [
            {"label": "Red", "description": "warm"},
            {"label": "Blue", "description": "cool"},
        ],
        "multiSelect": False,
    }
]


def perm_input(tool_name="Bash", tool_input=None, sid=SID):
    return json.dumps(
        {
            "session_id": sid,
            "transcript_path": "/x.jsonl",
            "cwd": "/work",
            "hook_event_name": "PermissionRequest",
            "tool_name": tool_name,
            "tool_input": BASH_INPUT if tool_input is None else tool_input,
            "permission_suggestions": [],
        }
    )


def ask_input(questions=None, sid=SID):
    return json.dumps(
        {
            "session_id": sid,
            "transcript_path": "/x.jsonl",
            "cwd": "/work",
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": QUESTIONS if questions is None else questions},
        }
    )


def arrive(*items):
    """on_sleep: item (n, msg) appends msg on the n-th sleep."""

    def on_sleep(c):
        for n, m in items:
            if c.sleeps == n:
                state.append_inbox(SID, m)

    return on_sleep


ALLOW = {
    "hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "allow"},
    }
}
DENY = {
    "hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "deny", "message": "用户在飞书上拒绝了"},
    }
}


# ---------------------------------------------------------------- permission


def test_permission_noop_when_not_enabled(fake):
    clock = Clock()
    assert hooks.run("permission", perm_input(), clock.now, clock.sleep) == ""
    assert fake() == [] and clock.sleeps == 0


def test_permission_allow(bridged):
    clock = Clock(arrive((2, msg(1, "同意", "1"))))
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert json.loads(out) == ALLOW
    assert sent_texts(bridged()) == [format_permission("Bash", BASH_INPUT), "✅ 已允许"]
    assert state.peek_inbox(SID)[0] == []


@pytest.mark.parametrize("word", ["1", "OK", "允许。", " yes "])
def test_permission_allow_words(bridged, word):
    clock = Clock(arrive((1, msg(1, word, "1"))))
    assert (
        json.loads(hooks.run("permission", perm_input(), clock.now, clock.sleep))
        == ALLOW
    )


def test_permission_deny(bridged):
    clock = Clock(arrive((1, msg(1, "拒绝", "1"))))
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert json.loads(out) == DENY
    assert sent_texts(bridged())[-1] == "❌ 已拒绝"
    assert state.peek_inbox(SID)[0] == []


def test_permission_ambiguous_reply_keeps_waiting(bridged):
    clock = Clock(arrive((1, msg(1, "好的吧", "1")), (4, msg(2, "同意", "2"))))
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert json.loads(out) == ALLOW
    assert sent_texts(bridged())[1:] == [hooks.MSG_PERM_HINT, "✅ 已允许"]
    assert state.peek_inbox(SID)[0] == []
    assert clock.sleeps >= 4


def test_permission_ambiguous_is_committed_before_decision(bridged):
    seen = []

    def on_sleep(c):
        if c.sleeps == 1:
            state.append_inbox(SID, msg(1, "好的吧", "1"))
        if c.sleeps == 3:
            seen.append(state.peek_inbox(SID)[0])
            state.append_inbox(SID, msg(2, "no", "2"))

    clock = Clock(on_sleep)
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert json.loads(out) == DENY
    assert seen == [[]]  # "好的吧" consumed while still waiting


def test_permission_out_of_order_consumes_only_the_answer(bridged):
    clock = Clock(
        arrive(
            (1, msg(2, "顺便把测试也跑了", "300")),
            (1, msg(1, "同意", "200")),  # earlier create_time, later in file
        )
    )
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert json.loads(out) == ALLOW
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["顺便把测试也跑了"]


def test_permission_commits_only_up_to_deciding_message(bridged):
    clock = Clock(
        arrive(
            (1, msg(1, "嗯？", "100")),
            (1, msg(2, "同意", "200")),
            (1, msg(3, "然后部署", "300")),
        )
    )
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert json.loads(out) == ALLOW
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["然后部署"]
    # no hint: the batch contained a decision
    assert hooks.MSG_PERM_HINT not in sent_texts(bridged())


def test_permission_first_decision_wins(bridged):
    clock = Clock(arrive((1, msg(1, "拒绝", "100")), (1, msg(2, "同意", "200"))))
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert json.loads(out) == DENY
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["同意"]


def test_permission_ignores_replies_sent_before_prompt(bridged):
    state.append_inbox(SID, msg(1, "1", "100"))  # backlog, meant for something else
    clock = Clock(arrive((3, msg(2, "拒绝", "200"))))
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert json.loads(out) == DENY
    assert clock.sleeps >= 3  # kept waiting despite the queued "1"
    assert hooks.MSG_PERM_HINT not in sent_texts(bridged())
    # the early "1" is still delivered, once, by the next Stop hook
    out = hooks.run("stop", stop_input(None), clock.now, clock.sleep)
    assert json.loads(out)["reason"] == DELIVER_PREFIX + "1"
    assert state.peek_inbox(SID)[0] == []
    assert not (state.paths.home() / "sessions" / SID / "consumed.txt").exists()


def test_permission_backlog_only_times_out(bridged, monkeypatch):
    monkeypatch.setenv("CLAUDE_LARK_WAIT_SECONDS", "2")
    state.append_inbox(SID, msg(1, "同意", "100"))
    clock = Clock()
    assert hooks.run("permission", perm_input(), clock.now, clock.sleep) == ""
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["同意"]


def test_non_prefix_consumption_no_loss_no_duplicate(bridged):
    # file: a (backlog), 嗯 (ambiguous), b, 同意. The hook consumes 嗯 and 同意,
    # which are not a prefix of the file; Stop must deliver exactly a, b.
    state.append_inbox(SID, msg(1, "a", "100"))

    def on_sleep(c):
        if c.sleeps == 1:
            state.append_inbox(SID, msg(2, "嗯", "200"))
        if c.sleeps == 3:
            # "b" lands first in the file but was sent after "同意"
            state.append_inbox(SID, msg(3, "b", "500"))
            state.append_inbox(SID, msg(4, "同意", "400"))

    clock = Clock(on_sleep)
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert json.loads(out) == ALLOW
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["a", "b"]
    clock2 = Clock()
    out = hooks.run("stop", stop_input(None), clock2.now, clock2.sleep)
    assert json.loads(out)["reason"] == DELIVER_PREFIX + "a" + JOIN_SEP + "b"
    assert state.peek_inbox(SID)[0] == []
    assert not (state.paths.home() / "sessions" / SID / "consumed.txt").exists()
    # a new message after everything is delivered exactly once
    state.append_inbox(SID, msg(5, "c", "500"))
    out = hooks.run("stop", stop_input(None), clock2.now, clock2.sleep)
    assert json.loads(out)["reason"] == DELIVER_PREFIX + "c"


def test_permission_timeout_returns_empty(bridged, monkeypatch):
    monkeypatch.setenv("CLAUDE_LARK_WAIT_SECONDS", "5")
    clock = Clock(arrive((2, msg(1, "随便", "1"))))
    start = clock.t
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert out == ""
    assert "allow" not in out
    assert clock.t == pytest.approx(start + 5)
    assert sent_texts(bridged())[-1] == "等待超时，已交回终端"


def test_permission_default_wait_is_3540(bridged):
    clock = Clock()
    start = clock.t
    assert hooks.run("permission", perm_input(), clock.now, clock.sleep) == ""
    assert clock.t == pytest.approx(start + 3540)


def test_permission_sigterm_returns_empty(bridged):
    def on_sleep(c):
        if c.sleeps == 2:
            state.append_inbox(SID, msg(1, "同意", "1"))
            hooks._on_sigterm(signal.SIGTERM, None)

    clock = Clock(on_sleep)
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert out == ""
    assert sent_texts(bridged())[-1] == "已在终端处理"
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["同意"]
    assert not (state.paths.home() / "errors.log").exists()


def test_permission_exception_never_allows(bridged, monkeypatch):
    state.append_inbox(SID, msg(1, "同意", "1"))

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(hooks, "format_permission", boom)
    clock = Clock()
    assert hooks.run("permission", perm_input(), clock.now, clock.sleep) == ""
    assert "kaboom" in (state.paths.home() / "errors.log").read_text()
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["同意"]


def test_permission_commit_failure_never_allows(bridged, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(hooks.state, "consume", boom)
    clock = Clock(arrive((1, msg(1, "同意", "1"))))
    assert hooks.run("permission", perm_input(), clock.now, clock.sleep) == ""
    assert "✅ 已允许" not in sent_texts(bridged())


def test_permission_bad_tool_input_never_allows(bridged):
    state.append_inbox(SID, msg(1, "同意", "1"))
    clock = Clock()
    out = hooks.run("permission", perm_input(tool_input="oops"), clock.now, clock.sleep)
    assert out == ""


def test_permission_non_bash_tool(bridged):
    clock = Clock(arrive((1, msg(1, "y", "1"))))
    ti = {"file_path": "/work/a.py", "content": "x"}
    out = hooks.run("permission", perm_input("Write", ti), clock.now, clock.sleep)
    assert json.loads(out) == ALLOW
    assert sent_texts(bridged())[0] == format_permission("Write", ti)


def test_permission_disabled_while_waiting(bridged):
    def on_sleep(c):
        if c.sleeps == 2:
            state.set_enabled(SID, False)

    clock = Clock(on_sleep)
    assert hooks.run("permission", perm_input(), clock.now, clock.sleep) == ""
    assert sent_texts(bridged()) == [format_permission("Bash", BASH_INPUT)]


def _sigterm_after_commit(monkeypatch):
    """Fire SIGTERM right after the reply is consumed (cursor or by id)."""
    for name in ("commit_cursor", "consume"):
        real = getattr(state, name)

        def then_sigterm(*a, _real=real, **k):
            _real(*a, **k)
            hooks._on_sigterm(signal.SIGTERM, None)

        monkeypatch.setattr(hooks.state, name, then_sigterm)


def test_permission_sigterm_after_commit_keeps_result(bridged, monkeypatch):
    # M1: once the reply is consumed, the decision must not be dropped
    _sigterm_after_commit(monkeypatch)
    clock = Clock(arrive((1, msg(1, "同意", "1"))))
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert json.loads(out) == ALLOW
    assert sent_texts(bridged())[-1] == "✅ 已允许"
    assert hooks._term == {"defer": False, "pending": False}


# ----------------------------------------------------------------------- ask


def test_ask_noop_when_not_enabled(fake):
    clock = Clock()
    assert hooks.run("ask", ask_input(), clock.now, clock.sleep) == ""
    assert fake() == []


def test_ask_fills_answers(bridged):
    clock = Clock(arrive((2, msg(1, "2", "1"))))
    out = json.loads(hooks.run("ask", ask_input(), clock.now, clock.sleep))
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "allow"
    assert hso["updatedInput"]["questions"] == QUESTIONS
    assert hso["updatedInput"]["answers"] == {"Which color?": "Blue"}
    assert sent_texts(bridged()) == [format_questions(QUESTIONS)]
    assert state.peek_inbox(SID)[0] == []


def test_ask_free_text_and_multi_question(bridged):
    qs = QUESTIONS + [
        {
            "question": "Which sizes?",
            "header": "Size",
            "options": [{"label": "S"}, {"label": "M"}, {"label": "L"}],
            "multiSelect": True,
        }
    ]
    clock = Clock(arrive((1, msg(1, "紫色\n1,3", "1"))))
    out = json.loads(hooks.run("ask", ask_input(qs), clock.now, clock.sleep))
    assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
        "Which color?": "紫色",
        "Which sizes?": "S, L",
    }


def test_ask_uses_first_message_and_leaves_rest(bridged):
    clock = Clock(arrive((1, msg(1, "1", "100")), (1, msg(2, "另外改下标题", "200"))))
    out = json.loads(hooks.run("ask", ask_input(), clock.now, clock.sleep))
    assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
        "Which color?": "Red"
    }
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["另外改下标题"]


def test_ask_timeout_returns_empty(bridged, monkeypatch):
    monkeypatch.setenv("CLAUDE_LARK_WAIT_SECONDS", "3")
    clock = Clock()
    assert hooks.run("ask", ask_input(), clock.now, clock.sleep) == ""
    assert sent_texts(bridged())[-1] == "已交回终端"


def test_ask_sigterm_returns_empty(bridged):
    def on_sleep(c):
        if c.sleeps == 1:
            state.append_inbox(SID, msg(1, "1", "1"))
            hooks._on_sigterm(signal.SIGTERM, None)

    clock = Clock(on_sleep)
    assert hooks.run("ask", ask_input(), clock.now, clock.sleep) == ""
    assert sent_texts(bridged())[-1] == "已交回终端"
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["1"]


def test_ask_ignores_messages_sent_before_prompt(bridged):
    state.append_inbox(SID, msg(1, "1", "100"))
    clock = Clock(arrive((2, msg(2, "2", "200"))))
    out = json.loads(hooks.run("ask", ask_input(), clock.now, clock.sleep))
    assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
        "Which color?": "Blue"
    }
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["1"]


def test_ask_bad_questions_returns_empty(bridged):
    state.append_inbox(SID, msg(1, "1", "1"))
    clock = Clock()
    for qs in ([], "nope", [{"no_question": 1}]):
        assert hooks.run("ask", ask_input(qs), clock.now, clock.sleep) == ""
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["1"]


def test_ask_sigterm_after_commit_keeps_result(bridged, monkeypatch):
    _sigterm_after_commit(monkeypatch)
    clock = Clock(arrive((1, msg(1, "2", "1"))))
    out = json.loads(hooks.run("ask", ask_input(), clock.now, clock.sleep))
    assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
        "Which color?": "Blue"
    }


# ------------------------------------------------------- Task 4 review fixes


def test_stop_sigterm_after_commit_keeps_result(bridged, monkeypatch):
    # M1
    state.append_inbox(SID, msg(1, "hi", "1"))
    _sigterm_after_commit(monkeypatch)
    clock = Clock()
    out = hooks.run("stop", stop_input(), clock.now, clock.sleep)
    assert json.loads(out)["reason"] == DELIVER_PREFIX + "hi"
    assert state.get_counter(SID, "blocks") == 1
    assert hooks._term == {"defer": False, "pending": False}


def test_stop_block_cap_on_message_delivery(bridged):
    # M2
    state.set_counter(SID, "blocks", 999)
    state.append_inbox(SID, msg(1, "hi", "1"))
    clock = Clock()
    assert hooks.run("stop", stop_input("r"), clock.now, clock.sleep) == ""
    assert sent_texts(bridged()) == ["r", "续命次数已用完，会话已空闲，回到电脑前继续"]
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["hi"]
    assert state.get_counter(SID, "blocks") == 999


def test_stop_delivers_just_below_cap(bridged):
    state.set_counter(SID, "blocks", 998)
    state.append_inbox(SID, msg(1, "hi", "1"))
    clock = Clock()
    out = hooks.run("stop", stop_input("r"), clock.now, clock.sleep)
    assert json.loads(out)["reason"] == DELIVER_PREFIX + "hi"
    assert state.get_counter(SID, "blocks") == 999


def test_prompt_send_uses_short_timeout(bridged, monkeypatch):
    # M3
    seen = []
    real = feishu._run

    def spy(args, timeout=feishu.TIMEOUT):
        seen.append(timeout)
        return real(args, timeout=timeout)

    monkeypatch.setattr(feishu, "_run", spy)
    clock = Clock()
    hooks.run("prompt", prompt_input("hello"), clock.now, clock.sleep)
    assert seen == [5]
    feishu.send("oc_test_chat", "x")
    assert seen == [5, feishu.TIMEOUT]


def test_send_timeout_is_enforced(bridged, monkeypatch):
    import time

    monkeypatch.setenv("FAKE_LARK_SLEEP", "5")
    t0 = time.monotonic()
    feishu.send("oc_test_chat", "hello", timeout=0.5)
    assert time.monotonic() - t0 < 4
    assert "timed out" in (state.paths.home() / "errors.log").read_text()


def test_bin_permission_real_sigterm(bridged):
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    p = subprocess.Popen(
        [sys.executable, str(root / "bin" / "claude-lark"), "hook", "permission"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    p.stdin.write(perm_input())
    p.stdin.close()
    deadline = time.time() + 20
    while not sent_texts(bridged()) and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)  # now inside the wait loop
    p.send_signal(signal.SIGTERM)
    p.wait(timeout=15)
    out, err = p.stdout.read(), p.stderr.read()
    assert p.returncode == 0 and out == "" and err == ""
    assert sent_texts(bridged())[-1] == "已在终端处理"


# ------------------------------------------------ fix round: concurrency etc.


def _hold_interactive_lock():
    import fcntl

    p = state.paths.session_dir(SID) / "interactive.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "a+")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


@pytest.mark.parametrize("event", ["permission", "ask"])
def test_interactive_lock_wait_respects_deadline(bridged, monkeypatch, event):
    monkeypatch.setenv("CLAUDE_LARK_WAIT_SECONDS", "3")
    held = _hold_interactive_lock()
    try:
        clock = Clock(arrive((1, msg(1, "同意", "1"))))
        start = clock.t
        inp = perm_input() if event == "permission" else ask_input()
        assert hooks.run(event, inp, clock.now, clock.sleep) == ""
        assert clock.t == pytest.approx(start + 3)
        assert sent_texts(bridged()) == []  # never prompted
        assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["同意"]
    finally:
        held.close()


@pytest.mark.parametrize("event", ["permission", "ask"])
def test_interactive_lock_wait_sigterm(bridged, event):
    held = _hold_interactive_lock()
    try:

        def on_sleep(c):
            if c.sleeps == 2:
                hooks._on_sigterm(signal.SIGTERM, None)

        clock = Clock(on_sleep)
        inp = perm_input() if event == "permission" else ask_input()
        assert hooks.run(event, inp, clock.now, clock.sleep) == ""
        assert clock.sleeps == 2
        assert sent_texts(bridged()) == []
    finally:
        held.close()


def test_interactive_lock_released_after_hook(bridged):
    clock = Clock(arrive((1, msg(1, "同意", "1"))))
    hooks.run("permission", perm_input(), clock.now, clock.sleep)
    _hold_interactive_lock().close()  # would raise BlockingIOError if still held


def test_two_concurrent_permission_hooks_one_reply_allows_one(bridged, monkeypatch):
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    monkeypatch.setenv("CLAUDE_LARK_WAIT_SECONDS", "30")
    root = Path(__file__).resolve().parent.parent

    def spawn(cmd):
        p = subprocess.Popen(
            [sys.executable, str(root / "bin" / "claude-lark"), "hook", "permission"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        p.stdin.write(perm_input(tool_input={"command": cmd}))
        p.stdin.close()
        return p

    def wait_until(cond, timeout=20):
        deadline = time.time() + timeout
        while not cond() and time.time() < deadline:
            time.sleep(0.05)
        assert cond()

    def prompts():
        return [t for t in sent_texts(bridged()) if t.startswith("Claude 想执行")]

    a, b = spawn("echo A"), spawn("echo B")
    wait_until(lambda: len(prompts()) >= 1)
    time.sleep(1.0)  # give the other hook every chance to (wrongly) prompt too
    assert len(prompts()) == 1
    state.append_inbox(SID, msg(1, "同意", "1"))
    wait_until(lambda: (a.poll() is not None) or (b.poll() is not None))
    first, second = (a, b) if a.poll() is not None else (b, a)
    assert json.loads(first.stdout.read())["hookSpecificOutput"]["decision"] == {
        "behavior": "allow"
    }
    # the other one now prompts and keeps waiting; the old "同意" doesn't count
    wait_until(lambda: len(prompts()) == 2)
    time.sleep(1.0)
    assert second.poll() is None
    state.append_inbox(SID, msg(2, "拒绝", "2"))
    second.wait(timeout=20)
    out = json.loads(second.stdout.read())
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert state.peek_inbox(SID)[0] == []


def test_consumed_rewrite_is_locked(bridged):
    import fcntl
    import threading

    state.append_inbox(SID, msg(1, "x", "1"))
    p = state.paths.session_dir(SID) / "consumed.lock"
    f = open(p, "a+")
    fcntl.flock(f, fcntl.LOCK_EX)
    t = threading.Thread(target=state.consume, args=(SID, ["om_test_1"]))
    t.start()
    t.join(0.3)
    assert t.is_alive()  # blocked on the lock
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["x"]
    f.close()
    t.join(5)
    assert not t.is_alive()
    assert state.peek_inbox(SID)[0] == []


def test_ambiguous_hint_uses_short_timeout(bridged, monkeypatch):
    seen = []
    real = feishu._run

    def spy(args, timeout=feishu.TIMEOUT):
        if "--markdown" in args:
            seen.append((args[args.index("--markdown") + 1], timeout))
        return real(args, timeout=timeout)

    monkeypatch.setattr(feishu, "_run", spy)
    clock = Clock(arrive((1, msg(1, "嗯", "1")), (3, msg(2, "同意", "2"))))
    hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert (hooks.MSG_PERM_HINT, 5) in seen
    assert ("✅ 已允许", 5) in seen


def test_cli_sigterm_after_run_does_not_lose_output(bridged, monkeypatch):
    import io
    import sys

    from lark_bridge import cli

    class SigtermOnWrite(io.StringIO):
        def write(self, s):
            hooks._on_sigterm(signal.SIGTERM, None)  # signal lands right here
            return super().write(s)

    state.append_inbox(SID, msg(1, "from cli", "1"))
    out = SigtermOnWrite()
    before = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stop_input("r")))
    monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["hook", "stop"]) == 0
    assert json.loads(out.getvalue())["reason"] == DELIVER_PREFIX + "from cli"
    assert signal.getsignal(signal.SIGTERM) is before


# ------------------------------------------------ final review


HINT_IGNORED = "上一条消息不是「同意/拒绝」，已被忽略；请回复「同意」或「拒绝」"


def test_permission_ambiguous_hint_says_message_was_ignored(bridged):
    clock = Clock(arrive((1, msg(1, "顺便看下日志", "1")), (3, msg(2, "同意", "2"))))
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert json.loads(out) == ALLOW
    assert HINT_IGNORED in sent_texts(bridged())


def test_send_with_time_returns_prompt_create_time(fake, monkeypatch):
    monkeypatch.setenv("FAKE_LARK_SEND_TIME", "1761465600123")
    assert feishu.send_with_time("oc_test_chat", "hi") == (True, 1761465600123)
    assert feishu.send("oc_test_chat", "hi") is True
    monkeypatch.setenv("FAKE_LARK_SEND_TIME", "none")
    assert feishu.send_with_time("oc_test_chat", "hi") == (True, None)
    monkeypatch.setenv("FAKE_LARK_FAIL", "1")
    assert feishu.send_with_time("oc_test_chat", "hi") == (False, None)
    assert feishu.send("oc_test_chat", "hi") is False


def test_permission_ignores_late_reply_created_before_prompt(bridged, monkeypatch):
    """上一个权限请求的重复"同意"晚到（排在边界之后），但它的 create_time
    早于这条提示：不能批准这一个。它留给 Stop hook，不丢。"""
    monkeypatch.setenv("FAKE_LARK_SEND_TIME", "1761465600500")
    clock = Clock(
        arrive(
            (1, msg(1, "同意", "1761465600400")),
            (3, msg(2, "拒绝", "1761465600600")),
        )
    )
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert json.loads(out) == DENY
    assert clock.sleeps >= 3
    assert hooks.MSG_PERM_HINT not in sent_texts(bridged())
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["同意"]


def test_permission_without_prompt_time_falls_back_to_position(bridged, monkeypatch):
    monkeypatch.setenv("FAKE_LARK_SEND_TIME", "none")
    clock = Clock(arrive((1, msg(1, "同意", "1761465600400"))))
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert json.loads(out) == ALLOW


def test_ask_ignores_late_reply_created_before_prompt(bridged, monkeypatch):
    monkeypatch.setenv("FAKE_LARK_SEND_TIME", "1761465600500")
    clock = Clock(
        arrive(
            (1, msg(1, "1", "1761465600400")),
            (3, msg(2, "2", "1761465600600")),
        )
    )
    out = json.loads(hooks.run("ask", ask_input(), clock.now, clock.sleep))
    assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
        "Which color?": "Blue"
    }
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["1"]
