import json
import signal

import pytest

from lark_bridge import feishu, hooks, state
from lark_bridge.text import DELIVER_PREFIX, format_permission, format_questions
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
    state.append_inbox(SID, msg(1, word, "1"))
    clock = Clock()
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
    assert sent_texts(bridged())[1:] == ["请回复「同意」或「拒绝」", "✅ 已允许"]
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


def test_permission_leaves_later_messages_for_stop(bridged):
    state.append_inbox(SID, msg(2, "顺便把测试也跑了", "300"))
    state.append_inbox(SID, msg(1, "同意", "200"))  # earlier create_time
    clock = Clock()
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert json.loads(out) == ALLOW
    # file order is [later, 同意]; the cursor can only commit a prefix, so the
    # later message must not be skipped
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["顺便把测试也跑了", "同意"]


def test_permission_commits_only_up_to_deciding_message(bridged):
    state.append_inbox(SID, msg(1, "嗯？", "100"))
    state.append_inbox(SID, msg(2, "同意", "200"))
    state.append_inbox(SID, msg(3, "然后部署", "300"))
    clock = Clock()
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert json.loads(out) == ALLOW
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["然后部署"]
    # no hint: the batch contained a decision
    assert "请回复「同意」或「拒绝」" not in sent_texts(bridged())


def test_permission_first_decision_wins(bridged):
    state.append_inbox(SID, msg(1, "拒绝", "100"))
    state.append_inbox(SID, msg(2, "同意", "200"))
    clock = Clock()
    out = hooks.run("permission", perm_input(), clock.now, clock.sleep)
    assert json.loads(out) == DENY
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["同意"]


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
    state.append_inbox(SID, msg(1, "同意", "1"))

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(hooks.state, "commit_cursor", boom)
    clock = Clock()
    assert hooks.run("permission", perm_input(), clock.now, clock.sleep) == ""
    assert "✅ 已允许" not in sent_texts(bridged())


def test_permission_bad_tool_input_never_allows(bridged):
    state.append_inbox(SID, msg(1, "同意", "1"))
    clock = Clock()
    out = hooks.run("permission", perm_input(tool_input="oops"), clock.now, clock.sleep)
    assert out == ""


def test_permission_non_bash_tool(bridged):
    state.append_inbox(SID, msg(1, "y", "1"))
    clock = Clock()
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
    real = state.commit_cursor

    def commit_then_sigterm(sid, offset):
        real(sid, offset)
        hooks._on_sigterm(signal.SIGTERM, None)

    monkeypatch.setattr(hooks.state, "commit_cursor", commit_then_sigterm)


def test_permission_sigterm_after_commit_keeps_result(bridged, monkeypatch):
    # M1: once the reply is consumed, the decision must not be dropped
    state.append_inbox(SID, msg(1, "同意", "1"))
    _sigterm_after_commit(monkeypatch)
    clock = Clock()
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
    state.append_inbox(SID, msg(1, "紫色\n1,3", "1"))
    clock = Clock()
    out = json.loads(hooks.run("ask", ask_input(qs), clock.now, clock.sleep))
    assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {
        "Which color?": "紫色",
        "Which sizes?": "S, L",
    }


def test_ask_uses_first_message_and_leaves_rest(bridged):
    state.append_inbox(SID, msg(1, "1", "100"))
    state.append_inbox(SID, msg(2, "另外改下标题", "200"))
    clock = Clock()
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


def test_ask_bad_questions_returns_empty(bridged):
    state.append_inbox(SID, msg(1, "1", "1"))
    clock = Clock()
    for qs in ([], "nope", [{"no_question": 1}]):
        assert hooks.run("ask", ask_input(qs), clock.now, clock.sleep) == ""
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["1"]


def test_ask_sigterm_after_commit_keeps_result(bridged, monkeypatch):
    state.append_inbox(SID, msg(1, "2", "1"))
    _sigterm_after_commit(monkeypatch)
    clock = Clock()
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
