import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lark_bridge import listener, paths, procs, state

SID = "00000000-0000-0000-0000-0000000000b1"
SID2 = "00000000-0000-0000-0000-0000000000b2"
ROOT = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).resolve().parent / "fake_lark_cli.py"
BIN = ROOT / "bin" / "claude-lark"


@pytest.fixture
def fake(lark_home, tmp_path, monkeypatch):
    log = tmp_path / "fake_lark.log"
    monkeypatch.setenv("CLAUDE_LARK_CLI", str(FAKE))
    monkeypatch.setenv("FAKE_LARK_LOG", str(log))
    monkeypatch.delenv("FAKE_LARK_FAIL", raising=False)
    monkeypatch.delenv("FAKE_LARK_SLEEP", raising=False)
    monkeypatch.delenv("FAKE_LARK_EVENTS", raising=False)
    monkeypatch.delenv("FAKE_LARK_IGNORE_TERM", raising=False)
    listener._notice_at.clear()
    listener._owner_missing["logged"] = False

    def calls():
        if not log.exists():
            return []
        return [json.loads(x) for x in log.read_text().splitlines() if x.strip()]

    return calls


def sent_texts(calls):
    return [
        argv[argv.index("--markdown") + 1]
        for argv in calls
        if argv[:2] == ["im", "+messages-send"]
    ]


def bind(sid=SID, chat="oc_test_chat", enabled=True, claude_pid=None):
    state.save_binding(state.Binding(sid, chat, "测试群", "/work", claude_pid, 1.0))
    if enabled:
        state.set_enabled(sid, True)


def ev(**kw):
    base = {
        "type": "im.message.receive_v1",
        "chat_id": "oc_test_chat",
        "chat_type": "group",
        "content": "hello",
        "create_time": "1761465600000",
        "event_id": "ev_test_1",
        "message_id": "om_test_1",
        "message_type": "text",
        "mentions": [],
        "sender_id": "ou_test_owner",
        "sender_type": "user",
    }
    base.update(kw)
    return base


def inbox(sid=SID):
    return state.peek_inbox(sid)[0]


# ---------- handle_event ----------


def test_listener_routes_to_enabled_session(fake):
    bind()
    bind(SID2, chat="oc_test_other")
    listener.handle_event(ev(), "ou_test_owner")
    assert inbox() == [
        {"message_id": "om_test_1", "text": "hello", "create_time": "1761465600000"}
    ]
    assert inbox(SID2) == []
    assert sent_texts(fake()) == []


def test_listener_post_and_missing_type_are_routed(fake):
    bind()
    listener.handle_event(
        ev(message_type="post", message_id="om_test_p"), "ou_test_owner"
    )
    e = ev(message_id="om_test_m")
    del e["message_type"]
    listener.handle_event(e, "ou_test_owner")
    assert [m["message_id"] for m in inbox()] == ["om_test_p", "om_test_m"]


def test_listener_drops_non_owner_and_bot(fake):
    bind()
    listener.handle_event(ev(sender_id="ou_test_stranger"), "ou_test_owner")
    listener.handle_event(
        ev(sender_id="ou_test_bot", sender_type="app", message_id="om_test_2"),
        "ou_test_owner",
    )
    e = ev(message_id="om_test_3")
    del e["sender_id"]
    listener.handle_event(e, "ou_test_owner")
    # 没配置 owner 时什么都不收
    listener.handle_event(ev(message_id="om_test_4"), "")
    listener.handle_event(ev(message_id="om_test_5"), None)
    assert inbox() == []
    assert sent_texts(fake()) == []


def test_listener_off_command_disables(fake):
    bind()
    listener.handle_event(ev(content="@_user_1 /off "), "ou_test_owner")
    assert not state.is_enabled(SID)
    assert inbox() == []
    assert sent_texts(fake()) == ["已断开，终端里敲 /lark 可重新连接"]


def test_listener_notifies_when_chat_not_enabled(fake, monkeypatch):
    bind(enabled=False)
    t = [1000.0]
    monkeypatch.setattr(listener, "_now", lambda: t[0])
    listener.handle_event(ev(), "ou_test_owner")
    listener.handle_event(ev(message_id="om_test_2"), "ou_test_owner")
    t[0] += 599
    listener.handle_event(ev(message_id="om_test_3"), "ou_test_owner")
    assert sent_texts(fake()) == ["当前没有会话在桥接，回到电脑前在终端敲 /lark"]
    t[0] += 2
    listener.handle_event(ev(message_id="om_test_4"), "ou_test_owner")
    assert len(sent_texts(fake())) == 2
    # 完全没绑定过的群：静默忽略
    listener.handle_event(ev(chat_id="oc_test_unknown"), "ou_test_owner")
    assert len(sent_texts(fake())) == 2
    assert inbox() == []


def test_listener_strips_mention(fake):
    bind()
    listener.handle_event(ev(content="@_user_1 hello there"), "ou_test_owner")
    listener.handle_event(
        ev(
            content="@Claude Bot 你好 @path/to/file",
            message_id="om_test_2",
            mentions=[{"key": "@_user_1", "name": "Claude Bot", "id": "ou_test_bot"}],
        ),
        "ou_test_owner",
    )
    # 只有 @ 的消息丢弃
    listener.handle_event(
        ev(
            content="@Claude Bot ",
            message_id="om_test_3",
            mentions=[{"name": "Claude Bot"}],
        ),
        "ou_test_owner",
    )
    # 奇怪的 mentions 形状不报错
    listener.handle_event(
        ev(content="x", message_id="om_test_4", mentions=[1, None, {"name": 5}]),
        "ou_test_owner",
    )
    listener.handle_event(
        ev(content="y", message_id="om_test_5", mentions="oops"), "ou_test_owner"
    )
    assert [m["text"] for m in inbox()] == [
        "hello there",
        "你好 @path/to/file",
        "x",
        "y",
    ]


def test_listener_unsupported_type_replies(fake):
    bind()
    listener.handle_event(ev(message_type="image", content="[图片]"), "ou_test_owner")
    assert inbox() == []
    assert sent_texts(fake()) == ["暂不支持该类型消息，请发文字"]


def test_listener_drops_malformed_events(fake):
    bind()
    listener.handle_event(ev(content=None), "ou_test_owner")
    listener.handle_event(ev(message_id=None), "ou_test_owner")
    listener.handle_event(ev(content="   "), "ou_test_owner")
    listener.handle_event({"sender_id": "ou_test_owner"}, "ou_test_owner")
    assert inbox() == []


# ---------- main ----------


def _write_events(tmp_path, monkeypatch, lines):
    p = tmp_path / "events.ndjson"
    p.write_text("".join(x + "\n" for x in lines))
    monkeypatch.setenv("FAKE_LARK_EVENTS", str(p))
    return p


def _wait(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _child_pids():
    """listener.log 里记录的 lark-cli 子进程 pid。"""
    try:
        text = paths.listener_log().read_text()
    except OSError:
        return []
    return [
        int(line.rsplit("pid=", 1)[1].split()[0])
        for line in text.splitlines()
        if "started lark-cli pid=" in line
    ]


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_listener_main_exits_when_no_enabled_sessions(fake, tmp_path, monkeypatch):
    claude = subprocess.Popen(["sleep", "60"])
    bind(claude_pid=claude.pid)
    _write_events(
        tmp_path,
        monkeypatch,
        [
            "not json at all",
            "[1, 2]",
            json.dumps(ev(message_id="om_test_bad")),
            json.dumps(ev(content="first")),
        ],
    )
    real = listener.handle_event

    def flaky(e, owner):
        if e.get("message_id") == "om_test_bad":
            raise RuntimeError("boom")
        return real(e, owner)

    monkeypatch.setattr(listener, "handle_event", flaky)
    # 残留的 pid 文件指向已死进程：应被替换
    paths.listener_pid_file().write_text(str(_dead_pid()))

    def killer():
        # 超时也要杀，否则实现有 bug 时 main 永不返回
        _wait(lambda: inbox())
        claude.kill()
        claude.wait()

    th = threading.Thread(target=killer)
    th.start()
    rc = listener.main(check_interval=0.1, restart_delay=0.1)
    th.join()
    claude.kill()
    claude.wait()
    assert rc == 0
    assert [m["text"] for m in inbox()] == ["first"]
    assert not state.is_enabled(SID)
    assert not paths.listener_pid_file().exists()
    kids = _child_pids()
    assert kids and all(not procs.pid_alive(k) for k in kids)
    assert "boom" in paths.listener_log().read_text()
    consume = [c for c in fake() if c[:2] == ["event", "consume"]]
    assert consume[0] == ["event", "consume", "im.message.receive_v1", "--as", "bot"]


def test_listener_main_exits_immediately_without_enabled_sessions(fake):
    bind(enabled=False)
    assert listener.main(check_interval=0.1, restart_delay=0.1) == 0
    assert not paths.listener_pid_file().exists()
    assert fake() == []


def _hold_listener_lock():
    """模拟另一个正在运行的监听进程：拿住 listener.lock。close() 即释放。"""
    import fcntl

    f = open(paths.home() / "listener.lock", "a+")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def test_listener_single_instance(fake):
    bind()
    paths.listener_pid_file().write_text(str(os.getppid()))
    other = _hold_listener_lock()
    try:
        assert listener.is_running()
        assert listener.main(check_interval=0.1, restart_delay=0.1) == 0
    finally:
        other.close()
    assert fake() == []
    # 别人的 pid 文件不能被删
    assert paths.listener_pid_file().read_text() == str(os.getppid())


def test_listener_restarts_crashed_lark_cli(fake, monkeypatch):
    monkeypatch.setenv("FAKE_LARK_FAIL", "1")
    claude = subprocess.Popen(["sleep", "60"])
    bind(claude_pid=claude.pid)

    def killer():
        _wait(lambda: len(_child_pids()) >= 3)
        claude.kill()
        claude.wait()

    th = threading.Thread(target=killer)
    th.start()
    assert listener.main(check_interval=0.1, restart_delay=0.1) == 0
    th.join()
    assert len(_child_pids()) >= 3
    assert "restarting" in paths.listener_log().read_text()


def _env():
    return dict(os.environ)


def test_listener_sigterm_and_cli(fake, tmp_path, monkeypatch):
    bind()
    _write_events(tmp_path, monkeypatch, [json.dumps(ev())])
    p = subprocess.Popen(
        [sys.executable, str(BIN), "listen"],
        env=_env(),
        stdin=subprocess.DEVNULL,
    )
    try:
        assert _wait(lambda: inbox())
        assert paths.listener_pid_file().read_text().strip() == str(p.pid)
        kids = _child_pids()
        assert kids
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=10) == 0
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    assert not paths.listener_pid_file().exists()
    assert _wait(lambda: all(not procs.pid_alive(k) for k in kids))


def test_ensure_running_spawns_once(fake):
    bind()
    pidf = paths.listener_pid_file()
    listener.ensure_running()
    try:
        assert _wait(lambda: pidf.exists() and pidf.read_text().strip())
        pid = int(pidf.read_text())
        assert procs.pid_alive(pid)
        listener.ensure_running()
        time.sleep(0.5)
        assert int(pidf.read_text()) == pid
        assert len(_child_pids()) == 1
    finally:
        try:
            os.kill(int(pidf.read_text()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    assert _wait(lambda: not pidf.exists())


# ---------- fix round 1 ----------


def test_listener_handler_error_asks_resend(fake, monkeypatch):
    bind()

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(state, "append_inbox", boom)
    listener.handle_event(ev(), "ou_test_owner")
    assert sent_texts(fake()) == ["处理失败，请重发"]
    assert "disk full" in paths.listener_log().read_text()
    # 非绑定者的消息出错前就被丢弃，不回复
    monkeypatch.setattr(state, "find_enabled_by_chat", boom)
    listener.handle_event(ev(sender_id="ou_test_stranger"), "ou_test_owner")
    assert len(sent_texts(fake())) == 1


def test_listener_logs_missing_owner_once(fake, lark_home):
    bind()
    (lark_home / "config.json").write_text("{}")
    listener._process_line(json.dumps(ev()))
    listener._process_line(json.dumps(ev(message_id="om_test_2")))
    assert paths.listener_log().read_text().count("owner_open_id missing") == 1
    assert inbox() == []


def test_listener_notice_retried_after_failed_send(fake, monkeypatch):
    bind(enabled=False)
    monkeypatch.setenv("FAKE_LARK_FAIL", "1")
    listener.handle_event(ev(), "ou_test_owner")
    monkeypatch.delenv("FAKE_LARK_FAIL")
    listener.handle_event(ev(message_id="om_test_2"), "ou_test_owner")
    listener.handle_event(ev(message_id="om_test_3"), "ou_test_owner")
    sends = [c for c in fake() if c[:2] == ["im", "+messages-send"]]
    assert len(sends) == 2  # 失败的一次 + 重试成功的一次，之后被限流


def test_listener_keeps_running_if_enabled_during_shutdown(fake, monkeypatch):
    """watchdog 已判定"没有会话"，在 pid 锁内复查前有会话开启 → 不退出。"""
    bind()
    script = [True, False, True]  # 启动检查 / watchdog 判定 / 锁内复查
    default = {"v": True}

    def fake_any_enabled():
        return script.pop(0) if script else default["v"]

    monkeypatch.setattr(listener, "_any_enabled", fake_any_enabled)
    out = {}
    th = threading.Thread(
        target=lambda: out.setdefault(
            "rc", listener.main(check_interval=0.1, restart_delay=0.1)
        ),
        daemon=True,
    )
    th.start()
    try:
        assert _wait(
            lambda: paths.listener_log().exists()
            and "keep running" in paths.listener_log().read_text()
        )
        time.sleep(0.3)
        assert th.is_alive()
        assert paths.listener_pid_file().read_text() == str(os.getpid())
        kids = _child_pids()
        assert len(kids) == 1 and procs.pid_alive(kids[0])
    finally:
        default["v"] = False
        th.join(10)
    assert not th.is_alive() and out["rc"] == 0
    assert not paths.listener_pid_file().exists()


def test_ensure_running_waits_for_pid_lock(fake, monkeypatch):
    """ensure_running 与退出决定互斥：持锁期间它不能读 pid 文件、不能拉起。"""
    spawned = []
    monkeypatch.setattr(
        listener.subprocess, "Popen", lambda *a, **k: spawned.append(a[0])
    )
    paths.listener_pid_file().write_text(str(os.getppid()))
    other = _hold_listener_lock()
    with listener._pid_lock():
        th = threading.Thread(target=listener.ensure_running)
        th.start()
        time.sleep(0.3)
        assert th.is_alive() and spawned == []
        # 模拟退出中的监听进程在锁内删掉 pid 文件、放掉存活锁
        paths.listener_pid_file().unlink()
        other.close()
    th.join(5)
    assert len(spawned) == 1 and spawned[0][-1] == "listen"


def test_listener_kills_child_ignoring_sigterm(fake, monkeypatch):
    monkeypatch.setenv("FAKE_LARK_IGNORE_TERM", "1")
    monkeypatch.setattr(listener, "KILL_GRACE", 0.3)
    claude = subprocess.Popen(["sleep", "60"])
    bind(claude_pid=claude.pid)

    def killer():
        _wait(lambda: _child_pids())
        time.sleep(0.3)  # 让子进程装好 SIG_IGN
        claude.kill()
        claude.wait()

    k = threading.Thread(target=killer)
    k.start()
    out = {}
    th = threading.Thread(
        target=lambda: out.setdefault(
            "rc", listener.main(check_interval=0.1, restart_delay=0.1)
        ),
        daemon=True,
    )
    th.start()
    th.join(4)
    k.join()
    alive = th.is_alive()
    for pid in _child_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    th.join(5)
    assert not alive, "read loop stayed blocked on a child ignoring SIGTERM"
    assert out["rc"] == 0
    assert "ignored SIGTERM" in paths.listener_log().read_text()


# ---------- final review: pid reuse ----------


def test_stale_pid_file_with_reused_live_pid_does_not_block_listener(fake):
    """pid 文件里的数字现在属于别的活进程（pid 复用）：不能当成监听进程在跑。"""
    claude = subprocess.Popen(["sleep", "60"])
    bind(claude_pid=claude.pid)
    paths.listener_pid_file().write_text(str(os.getppid()))
    assert not listener.is_running()

    def killer():
        _wait(lambda: _child_pids())
        claude.kill()
        claude.wait()

    th = threading.Thread(target=killer)
    th.start()
    rc = listener.main(check_interval=0.1, restart_delay=0.1)
    th.join()
    claude.kill()
    claude.wait()
    assert rc == 0
    assert _child_pids(), "listener never started lark-cli"
    assert not listener.is_running()


def test_listener_holds_lock_while_running(fake):
    claude = subprocess.Popen(["sleep", "60"])
    bind(claude_pid=claude.pid)
    seen = []

    def watcher():
        _wait(lambda: _child_pids())
        seen.append(listener.is_running())
        claude.kill()
        claude.wait()

    th = threading.Thread(target=watcher)
    th.start()
    listener.main(check_interval=0.1, restart_delay=0.1)
    th.join()
    assert seen == [True]
    assert not listener.is_running()


def test_ensure_running_skips_spawn_while_lock_held(fake, monkeypatch):
    spawned = []
    monkeypatch.setattr(
        listener.subprocess, "Popen", lambda *a, **k: spawned.append(a[0])
    )
    other = _hold_listener_lock()
    try:
        listener.ensure_running()  # 没有 pid 文件，但锁被占着
        assert spawned == []
    finally:
        other.close()
    paths.listener_pid_file().write_text(str(os.getppid()))  # 活着但不是监听进程
    listener.ensure_running()
    assert len(spawned) == 1
