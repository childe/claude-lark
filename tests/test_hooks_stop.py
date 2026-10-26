import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from lark_bridge import cli, feishu, hooks, state
from lark_bridge.text import DELIVER_PREFIX, JOIN_SEP, KEEPALIVE_REASON

SID = "00000000-0000-0000-0000-0000000000a1"
FAKE = Path(__file__).resolve().parent / "fake_lark_cli.py"


@pytest.fixture
def fake(lark_home, tmp_path, monkeypatch):
    log = tmp_path / "fake_lark.log"
    monkeypatch.setenv("CLAUDE_LARK_CLI", str(FAKE))
    monkeypatch.setenv("FAKE_LARK_LOG", str(log))
    monkeypatch.delenv("FAKE_LARK_FAIL", raising=False)
    monkeypatch.delenv("FAKE_LARK_SLEEP", raising=False)
    monkeypatch.delenv("CLAUDE_LARK_WAIT_SECONDS", raising=False)

    def calls():
        if not log.exists():
            return []
        return [json.loads(x) for x in log.read_text().splitlines() if x.strip()]

    return calls


def sent_texts(calls):
    out = []
    for argv in calls:
        if argv[:2] == ["im", "+messages-send"]:
            out.append(argv[argv.index("--markdown") + 1])
    return out


@pytest.fixture
def bridged(fake):
    state.save_binding(state.Binding(SID, "oc_test_chat", "测试群", "/work", None, 1.0))
    state.set_enabled(SID, True)
    return fake


class Clock:
    """Virtual time. `on_sleep(clock)` runs inside each sleep call."""

    def __init__(self, on_sleep=None):
        self.t = 1000.0
        self.on_sleep = on_sleep
        self.sleeps = 0

    def now(self):
        return self.t

    def sleep(self, d):
        assert 0 < d <= 0.5
        self.sleeps += 1
        self.t += d
        if self.on_sleep:
            self.on_sleep(self)


def msg(i, text, ct):
    return {"message_id": f"om_test_{i}", "text": text, "create_time": ct}


def stop_input(last="the answer", sid=SID):
    return json.dumps(
        {
            "session_id": sid,
            "transcript_path": "/x.jsonl",
            "cwd": "/work",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "last_assistant_message": last,
            "permission_mode": "default",
        }
    )


def prompt_input(prompt, sid=SID):
    return json.dumps(
        {
            "session_id": sid,
            "prompt": prompt,
            "cwd": "/work",
            "hook_event_name": "UserPromptSubmit",
        }
    )


def test_hooks_noop_when_not_enabled(fake):
    clock = Clock()
    for ev in ("stop", "permission", "ask", "prompt"):
        assert hooks.run(ev, stop_input(), clock.now, clock.sleep) == ""
    assert hooks.run("prompt", prompt_input("hi"), clock.now, clock.sleep) == ""
    assert fake() == []
    assert clock.sleeps == 0


def test_hooks_noop_on_garbage_stdin(fake):
    clock = Clock()
    for text in ("", "not json", "[]", "null", '{"session_id": "../etc"}', "{}"):
        for ev in ("stop", "prompt", "bogus"):
            assert hooks.run(ev, text, clock.now, clock.sleep) == ""
    assert fake() == []


def test_unknown_event_is_noop_even_when_enabled(bridged):
    clock = Clock()
    assert hooks.run("bogus", stop_input(), clock.now, clock.sleep) == ""
    assert bridged() == []


def test_stop_sends_reply_then_delivers_message(bridged):
    def on_sleep(c):
        if c.sleeps == 3:
            state.append_inbox(SID, msg(1, "下一步做什么", "1700000000000"))

    clock = Clock(on_sleep)
    out = hooks.run("stop", stop_input("final reply"), clock.now, clock.sleep)
    assert json.loads(out) == {
        "decision": "block",
        "reason": DELIVER_PREFIX + "下一步做什么",
    }
    calls = bridged()
    assert calls[0][:4] == ["im", "+messages-send", "--as", "bot"]
    assert calls[0][calls[0].index("--chat-id") + 1] == "oc_test_chat"
    assert sent_texts(calls) == ["final reply"]
    # consumed: cursor committed, counter bumped
    assert state.peek_inbox(SID)[0] == []
    assert state.get_counter(SID, "blocks") == 1


def test_stop_delivers_backlog_queued_before_hook_started(bridged):
    # out of order on disk; must be delivered in create_time order
    state.append_inbox(SID, msg(2, "second", "1700000000200"))
    state.append_inbox(SID, msg(1, "first", "1700000000100"))
    clock = Clock()
    out = hooks.run("stop", stop_input(), clock.now, clock.sleep)
    reason = json.loads(out)["reason"]
    assert reason == DELIVER_PREFIX + "first" + JOIN_SEP + "second"
    assert clock.sleeps == 0
    assert state.peek_inbox(SID)[0] == []


def test_stop_create_time_sort_is_numeric(bridged):
    state.append_inbox(SID, msg(1, "later", "1000"))
    state.append_inbox(SID, msg(2, "earlier", "999"))
    clock = Clock()
    reason = json.loads(hooks.run("stop", stop_input(), clock.now, clock.sleep))[
        "reason"
    ]
    assert reason == DELIVER_PREFIX + "earlier" + JOIN_SEP + "later"


def test_stop_skips_null_or_empty_reply(bridged):
    state.append_inbox(SID, msg(1, "x", "1"))
    clock = Clock()
    hooks.run("stop", stop_input(None), clock.now, clock.sleep)
    state.append_inbox(SID, msg(2, "y", "2"))
    hooks.run("stop", stop_input("   "), clock.now, clock.sleep)
    assert sent_texts(bridged()) == []


def test_stop_keepalive_on_deadline(bridged, monkeypatch):
    monkeypatch.setenv("CLAUDE_LARK_WAIT_SECONDS", "5")
    clock = Clock()
    start = clock.t
    out = hooks.run("stop", stop_input(), clock.now, clock.sleep)
    assert json.loads(out) == {"decision": "block", "reason": KEEPALIVE_REASON}
    assert clock.t == pytest.approx(start + 5)
    assert state.get_counter(SID, "blocks") == 1
    assert state.get_counter(SID, "keepalive_pending") == 1


def test_stop_default_wait_is_3540(bridged):
    clock = Clock()
    start = clock.t
    hooks.run("stop", stop_input(), clock.now, clock.sleep)
    assert clock.t == pytest.approx(start + 3540)


def test_stop_skips_sending_keepalive_reply(bridged, monkeypatch):
    monkeypatch.setenv("CLAUDE_LARK_WAIT_SECONDS", "1")
    state.set_counter(SID, "keepalive_pending", 1)
    state.append_inbox(SID, msg(1, "hello", "1"))
    clock = Clock()
    out = hooks.run("stop", stop_input("waiting"), clock.now, clock.sleep)
    assert json.loads(out)["reason"] == DELIVER_PREFIX + "hello"
    assert sent_texts(bridged()) == []
    assert state.get_counter(SID, "keepalive_pending") == 0
    # next real reply is sent again
    state.append_inbox(SID, msg(2, "again", "2"))
    hooks.run("stop", stop_input("real answer"), clock.now, clock.sleep)
    assert sent_texts(bridged()) == ["real answer"]


def test_stop_keepalive_warns_once_at_950(bridged, monkeypatch):
    monkeypatch.setenv("CLAUDE_LARK_WAIT_SECONDS", "1")
    state.set_counter(SID, "blocks", 948)
    state.set_counter(SID, "keepalive_pending", 1)
    clock = Clock()
    hooks.run("stop", stop_input("waiting"), clock.now, clock.sleep)
    assert state.get_counter(SID, "blocks") == 949
    assert sent_texts(bridged()) == []
    hooks.run("stop", stop_input("waiting"), clock.now, clock.sleep)
    assert state.get_counter(SID, "blocks") == 950
    assert sent_texts(bridged()) == ["剩余续命次数不多"]
    hooks.run("stop", stop_input("waiting"), clock.now, clock.sleep)
    assert sent_texts(bridged()) == ["剩余续命次数不多"]


def test_stop_gives_up_at_block_cap(bridged, monkeypatch):
    monkeypatch.setenv("CLAUDE_LARK_WAIT_SECONDS", "1")
    state.set_counter(SID, "blocks", 999)
    state.set_counter(SID, "keepalive_pending", 1)
    clock = Clock()
    assert hooks.run("stop", stop_input("waiting"), clock.now, clock.sleep) == ""
    assert sent_texts(bridged()) == ["续命次数已用完，会话已空闲，回到电脑前继续"]
    assert state.get_counter(SID, "blocks") == 999


def test_stop_sigterm_leaves_inbox_unconsumed(bridged):
    def on_sleep(c):
        if c.sleeps == 2:
            state.append_inbox(SID, msg(1, "late", "1"))
            hooks._on_sigterm(signal.SIGTERM, None)

    clock = Clock(on_sleep)
    assert hooks.run("stop", stop_input(), clock.now, clock.sleep) == ""
    msgs, _ = state.peek_inbox(SID)
    assert [m["text"] for m in msgs] == ["late"]
    assert state.get_counter(SID, "blocks") == 0
    assert state.get_counter(SID, "keepalive_pending") == 0
    assert not (state.paths.home() / "errors.log").exists()


def test_stop_returns_when_disabled_while_waiting(bridged):
    def on_sleep(c):
        if c.sleeps == 2:
            state.set_enabled(SID, False)

    clock = Clock(on_sleep)
    assert hooks.run("stop", stop_input(), clock.now, clock.sleep) == ""
    assert clock.sleeps == 2
    assert state.get_counter(SID, "blocks") == 0


def test_sigterm_handler_installed_and_restored(bridged, monkeypatch):
    monkeypatch.setenv("CLAUDE_LARK_WAIT_SECONDS", "1")
    before = signal.getsignal(signal.SIGTERM)
    seen = []
    clock = Clock(lambda c: seen.append(signal.getsignal(signal.SIGTERM)))
    hooks.run("stop", stop_input(), clock.now, clock.sleep)
    assert seen and seen[0] is hooks._on_sigterm
    assert signal.getsignal(signal.SIGTERM) is before


def test_stop_exception_is_logged_and_returns_empty(bridged, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(hooks.state, "peek_inbox", boom)
    clock = Clock()
    assert hooks.run("stop", stop_input(), clock.now, clock.sleep) == ""
    assert "kaboom" in (state.paths.home() / "errors.log").read_text()


def test_prompt_mirrors_terminal_input_and_resets_blocks(bridged):
    state.set_counter(SID, "blocks", 42)
    state.set_counter(SID, "keepalive_pending", 1)
    clock = Clock()
    assert hooks.run("prompt", prompt_input("修一下 bug"), clock.now, clock.sleep) == ""
    assert sent_texts(bridged()) == ["💻 终端：修一下 bug"]
    assert state.get_counter(SID, "blocks") == 0
    assert state.get_counter(SID, "keepalive_pending") == 0
    assert clock.sleeps == 0


def test_prompt_does_not_mirror_lark_command(bridged):
    state.set_counter(SID, "blocks", 3)
    clock = Clock()
    assert hooks.run("prompt", prompt_input("/lark off"), clock.now, clock.sleep) == ""
    assert hooks.run("prompt", prompt_input("/lark"), clock.now, clock.sleep) == ""
    assert sent_texts(bridged()) == []
    assert state.get_counter(SID, "blocks") == 0


def test_send_failure_does_not_raise(bridged, monkeypatch):
    monkeypatch.setenv("FAKE_LARK_FAIL", "1")
    feishu.send("oc_test_chat", "hello")
    log = (state.paths.home() / "errors.log").read_text()
    assert "messages-send" in log
    # and the stop hook still waits/delivers normally
    state.append_inbox(SID, msg(1, "hi", "1"))
    clock = Clock()
    out = hooks.run("stop", stop_input("reply"), clock.now, clock.sleep)
    assert json.loads(out)["reason"] == DELIVER_PREFIX + "hi"


def test_send_missing_binary_does_not_raise(bridged, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_LARK_CLI", str(tmp_path / "nope"))
    feishu.send("oc_test_chat", "hello")
    assert (state.paths.home() / "errors.log").exists()


def test_send_splits_long_message(bridged):
    feishu.send("oc_test_chat", ("x" * 3000 + "\n") * 3)
    parts = sent_texts(bridged())
    assert len(parts) >= 2 and all(len(p) <= 4000 for p in parts)


def test_create_chat_and_owner(fake):
    assert feishu.create_chat("名" * 80, "ou_test_owner") == "oc_test_chat"
    argv = fake()[0]
    assert argv[:4] == ["im", "+chat-create", "--as", "bot"]
    assert argv[argv.index("--name") + 1] == "名" * 60
    assert argv[argv.index("--users") + 1] == "ou_test_owner"
    assert "--set-bot-manager" in argv
    assert feishu.owner_open_id() == "ou_test_owner"


def test_create_chat_failure_raises(fake, monkeypatch):
    monkeypatch.setenv("FAKE_LARK_FAIL", "1")
    with pytest.raises(feishu.FeishuError):
        feishu.create_chat("x", "ou_test_owner")


def test_cli_hook_subcommand(bridged, monkeypatch, capsys):
    import io

    state.append_inbox(SID, msg(1, "from cli", "1"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(stop_input("r")))
    assert cli.main(["hook", "stop"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["reason"] == DELIVER_PREFIX + "from cli"


def test_bin_noop_subprocess(fake):
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run(
        [sys.executable, str(root / "bin" / "claude-lark"), "hook", "stop"],
        input=stop_input(),
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=30,
    )
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


def test_bin_real_sigterm_while_waiting(bridged):
    import time

    root = Path(__file__).resolve().parent.parent
    p = subprocess.Popen(
        [sys.executable, str(root / "bin" / "claude-lark"), "hook", "stop"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    p.stdin.write(stop_input("bye"))
    p.stdin.close()
    deadline = time.time() + 20
    while not sent_texts(bridged()) and time.time() < deadline:
        time.sleep(0.05)
    assert sent_texts(bridged()) == ["bye"]
    time.sleep(0.3)  # now inside the wait loop
    p.send_signal(signal.SIGTERM)
    p.wait(timeout=10)
    out, err = p.stdout.read(), p.stderr.read()
    assert p.returncode == 0 and out == "" and err == ""
    state.append_inbox(SID, msg(1, "after", "1"))
    assert [m["text"] for m in state.peek_inbox(SID)[0]] == ["after"]
    assert state.get_counter(SID, "blocks") == 0
