import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import write_meta
from lark_bridge import feishu, listener, paths, procs, state

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
    monkeypatch.delenv("FAKE_LARK_USER_SCOPE", raising=False)
    monkeypatch.delenv("FAKE_LARK_UPDATE_FAIL", raising=False)
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


def now_ms():
    return str(int(time.time() * 1000))


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


def _polls(fake):
    """监听进程拉消息的次数（假 lark-cli 记下的 api 调用）。"""
    return [c for c in fake() if c[:3] == ["api", "GET", "/open-apis/im/v1/messages"]]


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
            json.dumps(ev(message_id="om_test_bad", create_time=now_ms())),
            json.dumps(ev(content="first", create_time=now_ms())),
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
    rc = listener.main(check_interval=0.1, poll_interval=0.1)
    th.join()
    claude.kill()
    claude.wait()
    assert rc == 0
    assert [m["text"] for m in inbox()] == ["first"]
    assert not state.is_enabled(SID)
    assert not paths.listener_pid_file().exists()
    assert "boom" in paths.listener_log().read_text()
    poll = _polls(fake)[0]
    assert poll[3:5] == ["--as", "user"]
    assert (
        json.loads(poll[poll.index("--params") + 1])["container_id"] == "oc_test_chat"
    )


def test_listener_main_exits_immediately_without_enabled_sessions(fake):
    bind(enabled=False)
    assert listener.main(check_interval=0.1, poll_interval=0.1) == 0
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
        assert listener.main(check_interval=0.1, poll_interval=0.1) == 0
    finally:
        other.close()
    assert fake() == []
    # 别人的 pid 文件不能被删
    assert paths.listener_pid_file().read_text() == str(os.getppid())


def test_listener_keeps_polling_after_failures(fake, monkeypatch):
    monkeypatch.setenv("FAKE_LARK_FAIL", "1")
    claude = subprocess.Popen(["sleep", "60"])
    bind(claude_pid=claude.pid)

    def killer():
        _wait(lambda: len(_polls(fake)) >= 3)
        claude.kill()
        claude.wait()

    th = threading.Thread(target=killer)
    th.start()
    assert listener.main(check_interval=0.1, poll_interval=0.1) == 0
    th.join()
    assert len(_polls(fake)) >= 3
    # 连续失败只记一次
    assert paths.listener_log().read_text().count("poll oc_test_chat failed") == 1


def _env():
    return dict(os.environ)


def test_listener_sigterm_and_cli(fake, tmp_path, monkeypatch):
    bind()
    _write_events(tmp_path, monkeypatch, [json.dumps(ev(create_time=now_ms()))])
    p = subprocess.Popen(
        [sys.executable, str(BIN), "listen"],
        env=_env(),
        stdin=subprocess.DEVNULL,
    )
    try:
        assert _wait(lambda: inbox())
        assert paths.listener_pid_file().read_text().strip() == str(p.pid)
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=10) == 0
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    assert not paths.listener_pid_file().exists()


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
        assert paths.listener_log().read_text().count("listener started") == 1
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
    listener._process_event(ev())
    listener._process_event(ev(message_id="om_test_2"))
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
            "rc", listener.main(check_interval=0.1, poll_interval=0.1)
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
        n = len(_polls(fake))
        assert _wait(lambda: len(_polls(fake)) > n)
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


def test_listener_hung_poll_is_bounded(fake, monkeypatch):
    """lark-cli 卡住不返回：单次拉取有超时，监听进程照样能退出。"""
    monkeypatch.setenv("FAKE_LARK_SLEEP", "30")
    monkeypatch.setattr(listener, "POLL_TIMEOUT", 0.5)
    claude = subprocess.Popen(["sleep", "60"])
    bind(claude_pid=claude.pid)

    def killer():
        _wait(lambda: _polls(fake))
        claude.kill()
        claude.wait()

    k = threading.Thread(target=killer)
    k.start()
    out = {}
    th = threading.Thread(
        target=lambda: out.setdefault(
            "rc", listener.main(check_interval=0.1, poll_interval=0.1)
        ),
        daemon=True,
    )
    th.start()
    th.join(8)
    k.join()
    assert not th.is_alive(), "listener stayed blocked on a hung lark-cli"
    assert out["rc"] == 0
    assert "timed out" in paths.listener_log().read_text()


# ---------- final review: pid reuse ----------


def test_stale_pid_file_with_reused_live_pid_does_not_block_listener(fake):
    """pid 文件里的数字现在属于别的活进程（pid 复用）：不能当成监听进程在跑。"""
    claude = subprocess.Popen(["sleep", "60"])
    bind(claude_pid=claude.pid)
    paths.listener_pid_file().write_text(str(os.getppid()))
    assert not listener.is_running()

    def killer():
        _wait(lambda: _polls(fake))
        claude.kill()
        claude.wait()

    th = threading.Thread(target=killer)
    th.start()
    rc = listener.main(check_interval=0.1, poll_interval=0.1)
    th.join()
    claude.kill()
    claude.wait()
    assert rc == 0
    assert _polls(fake), "listener never polled"
    assert not listener.is_running()


def test_listener_holds_lock_while_running(fake):
    claude = subprocess.Popen(["sleep", "60"])
    bind(claude_pid=claude.pid)
    seen = []

    def watcher():
        _wait(lambda: _polls(fake))
        seen.append(listener.is_running())
        claude.kill()
        claude.wait()

    th = threading.Thread(target=watcher)
    th.start()
    listener.main(check_interval=0.1, poll_interval=0.1)
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


# ---------- polling as user ----------


def _poll(poller):
    poller.poll_once(threading.Event())


def test_poller_skips_messages_before_enable(fake, tmp_path, monkeypatch):
    bind()
    old = str(int(time.time() * 1000) - 60_000)
    p = _write_events(
        tmp_path,
        monkeypatch,
        [json.dumps(ev(message_id="om_test_old", content="old", create_time=old))],
    )
    poller = listener._Poller(idle_interval=60)
    _poll(poller)
    assert inbox() == []
    with open(p, "a") as f:
        f.write(
            json.dumps(
                ev(message_id="om_test_new", content="new", create_time=now_ms())
            )
            + "\n"
        )
    _poll(poller)
    _poll(poller)  # 同一条不重复投递
    assert [m["text"] for m in inbox()] == ["new"]


def test_poller_cursor_survives_restart(fake, tmp_path, monkeypatch):
    bind()
    _write_events(tmp_path, monkeypatch, [json.dumps(ev(create_time=now_ms()))])
    _poll(listener._Poller(idle_interval=60))
    _poll(listener._Poller(idle_interval=60))  # 新的监听进程读到持久化的游标
    assert [m["text"] for m in inbox()] == ["hello"]
    assert json.loads((paths.home() / "poll_cursors.json").read_text()) == {
        "oc_test_chat": 1
    }


def test_poller_notices_disabled_chat_but_not_history(fake, tmp_path, monkeypatch):
    bind(enabled=False)
    old = str(int(time.time() * 1000) - 60_000)
    p = _write_events(
        tmp_path,
        monkeypatch,
        [json.dumps(ev(message_id="om_test_old", create_time=old))],
    )
    poller = listener._Poller(idle_interval=0)
    _poll(poller)
    assert sent_texts(fake()) == []
    with open(p, "a") as f:
        f.write(json.dumps(ev(message_id="om_test_new", create_time=now_ms())) + "\n")
    _poll(poller)
    assert sent_texts(fake()) == [listener.NOT_BRIDGED]
    assert inbox() == []


def test_poller_polls_disabled_chats_rarely(fake, tmp_path, monkeypatch):
    bind(enabled=False)
    _write_events(tmp_path, monkeypatch, [])
    poller = listener._Poller(idle_interval=60)
    _poll(poller)
    _poll(poller)
    assert len(_polls(fake)) == 1


def test_poller_routes_post_and_strips_mentions(fake, tmp_path, monkeypatch):
    bind()
    _write_events(
        tmp_path,
        monkeypatch,
        [
            json.dumps(
                ev(
                    message_id="om_test_m",
                    content="@_user_1 看下日志",
                    mentions=[{"key": "@_user_1", "name": "test-bot"}],
                    create_time=now_ms(),
                )
            ),
            json.dumps(
                ev(
                    message_id="om_test_p",
                    message_type="post",
                    content="富文本",
                    create_time=now_ms(),
                )
            ),
            json.dumps(
                ev(
                    message_id="om_test_bot",
                    sender_id="cli_test_app",
                    sender_type="app",
                    content="机器人自己的回复",
                    create_time=now_ms(),
                )
            ),
        ],
    )
    _poll(listener._Poller(idle_interval=60))
    assert [m["text"] for m in inbox()] == ["看下日志", "富文本"]


def test_item_to_event_maps_api_fields():
    item = {
        "chat_id": "oc_test_chat",
        "message_id": "om_test_1",
        "msg_type": "text",
        "create_time": "1761465600000",
        "message_position": "3",
        "body": {"content": json.dumps({"text": "hi"})},
        "sender": {"id": "ou_test_owner", "id_type": "open_id", "sender_type": "user"},
    }
    e = listener.item_to_event(item, "oc_test_chat")
    assert e["sender_id"] == "ou_test_owner"
    assert e["content"] == "hi" and e["message_type"] == "text"
    assert e["create_time"] == "1761465600000" and e["mentions"] == []
    item["sender"] = {"id": "cli_test_app", "id_type": "app_id", "sender_type": "app"}
    assert listener.item_to_event(item, "oc_test_chat")["sender_id"] is None


# ---------- ensure_running result ----------


def test_ensure_running_reports_spawn_failure(fake, lark_home, monkeypatch):
    def boom(*a, **k):
        raise OSError("fork failed")

    monkeypatch.setattr(listener, "_lock_held_by_other", lambda: False)
    monkeypatch.setattr(listener.subprocess, "Popen", boom)
    assert listener.ensure_running() is False
    assert "listener.ensure_running" in (lark_home / "errors.log").read_text()


def test_ensure_running_true_when_running(fake, monkeypatch):
    spawned = []
    monkeypatch.setattr(listener, "_lock_held_by_other", lambda: True)
    monkeypatch.setattr(listener.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    assert listener.ensure_running() is True
    assert spawned == []


def test_ensure_running_true_after_spawn(fake, monkeypatch):
    spawned = []
    monkeypatch.setattr(listener, "_lock_held_by_other", lambda: False)
    monkeypatch.setattr(listener.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    assert listener.ensure_running() is True
    assert len(spawned) == 1


# ---------- chat name follows session name ----------


def _arg(c, flag):
    return c[c.index(flag) + 1]


def renames(calls):
    return [_arg(c, "--name") for c in calls() if c[:2] == ["im", "+chat-update"]]


def bind_auto(synced="rw-1", created=True, name="rw-1"):
    state.save_binding(
        state.Binding(
            SID,
            "oc_test_chat",
            name,
            "/work",
            os.getpid(),
            1.0,
            created_by_bridge=created,
            synced_name=synced,
        )
    )
    state.set_enabled(SID, True, os.getpid())


@pytest.fixture
def clock(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(listener, "_now", lambda: t[0])
    return t


def test_rename_syncs_user_name(fake, claude_sessions, clock):
    bind_auto(synced="rw-1")
    write_meta(claude_sessions, os.getpid(), SID, name="新名字", source="user")
    poller = listener._Poller(60)
    _poll(poller)
    assert renames(fake) == ["新名字"]
    c = [c for c in fake() if c[:2] == ["im", "+chat-update"]][0]
    assert c[:6] == ["im", "+chat-update", "--as", "bot", "--chat-id", "oc_test_chat"]
    b = state.load_binding(SID)
    assert (b.chat_name, b.synced_name) == ("新名字", "新名字")
    _poll(poller)
    assert renames(fake) == ["新名字"]  # 不重复改


def test_rename_syncs_derived_name(fake, claude_sessions, clock):
    bind_auto(synced="rw-1")
    write_meta(claude_sessions, os.getpid(), SID, name="fix-login", source="derived")
    _poll(listener._Poller(60))
    assert renames(fake) == ["fix-login"]


def test_rename_baseline_equal_does_nothing(fake, claude_sessions, clock):
    bind_auto(synced="rw-1")
    write_meta(claude_sessions, os.getpid(), SID, name="rw-1")
    _poll(listener._Poller(60))
    assert renames(fake) == []


def test_rename_skips_bound_existing_chat(fake, claude_sessions, clock):
    bind_auto(synced="rw-1", created=False)
    write_meta(claude_sessions, os.getpid(), SID, name="新名字")
    _poll(listener._Poller(60))
    assert renames(fake) == []


def test_rename_skips_disabled_session(fake, claude_sessions, clock):
    bind_auto(synced="rw-1")
    state.set_enabled(SID, False)
    write_meta(claude_sessions, os.getpid(), SID, name="新名字")
    _poll(listener._Poller(60))
    assert renames(fake) == []


@pytest.mark.parametrize("bad", ["foreign", "garbage", "missing", "blank"])
def test_rename_skips_foreign_meta(fake, claude_sessions, clock, bad):
    bind_auto(synced="rw-1")
    f = claude_sessions / f"{os.getpid()}.json"
    if bad == "foreign":
        write_meta(claude_sessions, os.getpid(), SID2, name="别人的")
    elif bad == "garbage":
        f.write_text("{not json")
    elif bad == "blank":
        write_meta(claude_sessions, os.getpid(), SID, name="   ")
    _poll(listener._Poller(60))
    assert renames(fake) == []
    assert state.load_binding(SID).synced_name == "rw-1"


def test_rename_legacy_binding_records_baseline_only(fake, claude_sessions, clock):
    bind_auto(synced=None)
    write_meta(claude_sessions, os.getpid(), SID, name="当前名")
    poller = listener._Poller(60)
    _poll(poller)
    assert renames(fake) == []
    assert state.load_binding(SID).synced_name == "当前名"
    write_meta(claude_sessions, os.getpid(), SID, name="改过的")
    _poll(poller)
    assert renames(fake) == ["改过的"]


def test_rename_truncates_to_60(fake, claude_sessions, clock):
    bind_auto(synced="rw-1")
    write_meta(claude_sessions, os.getpid(), SID, name="名" * 70)
    _poll(listener._Poller(60))
    assert renames(fake) == ["名" * 60]
    b = state.load_binding(SID)
    assert b.chat_name == "名" * 60 and b.synced_name == "名" * 70


def test_rename_failure_cooldown_and_single_notice(
    fake, claude_sessions, clock, monkeypatch, lark_home
):
    monkeypatch.setenv("FAKE_LARK_UPDATE_FAIL", "1")
    bind_auto(synced="rw-1")
    write_meta(claude_sessions, os.getpid(), SID, name="名1")
    poller = listener._Poller(60)

    def notices():
        return [t for t in sent_texts(fake()) if t == listener.RENAME_FAILED]

    clock[0] = 0
    _poll(poller)
    assert renames(fake) == ["名1"] and len(notices()) == 1
    clock[0] = 30
    _poll(poller)
    assert len(renames(fake)) == 1
    clock[0] = 61
    _poll(poller)
    assert len(renames(fake)) == 2 and len(notices()) == 1
    write_meta(claude_sessions, os.getpid(), SID, name="名2")
    clock[0] = 122
    _poll(poller)
    assert renames(fake)[-1] == "名2" and len(notices()) == 1
    assert "+chat-update" in (lark_home / "errors.log").read_text()
    assert state.load_binding(SID).synced_name == "rw-1"
    assert listener.RENAME_FAILED == (
        "改群名失败，可能缺少 im:chat:update 权限，详见 errors.log"
    )


def test_rename_recovers_after_failure(fake, claude_sessions, clock, monkeypatch):
    monkeypatch.setenv("FAKE_LARK_UPDATE_FAIL", "1")
    bind_auto(synced="rw-1")
    write_meta(claude_sessions, os.getpid(), SID, name="名1")
    poller = listener._Poller(60)
    _poll(poller)
    monkeypatch.delenv("FAKE_LARK_UPDATE_FAIL")
    clock[0] = 61
    _poll(poller)
    assert state.load_binding(SID).synced_name == "名1"
    assert SID not in poller.rename_retry_at


def test_rename_writeback_keeps_new_claude_pid(
    fake, claude_sessions, clock, monkeypatch
):
    bind_auto(synced="rw-1")
    write_meta(claude_sessions, os.getpid(), SID, name="新名字")
    real = feishu.update_chat_name

    def racing(chat_id, name, timeout):
        state.update_binding(SID, lambda b: setattr(b, "claude_pid", 99999))
        real(chat_id, name, timeout)

    monkeypatch.setattr(feishu, "update_chat_name", racing)
    _poll(listener._Poller(60))
    b = state.load_binding(SID)
    assert b.claude_pid == 99999 and b.synced_name == "新名字"


def test_rename_writeback_dropped_when_chat_changed(
    fake, claude_sessions, clock, monkeypatch
):
    bind_auto(synced="rw-1")
    write_meta(claude_sessions, os.getpid(), SID, name="新名字")
    real = feishu.update_chat_name

    def racing(chat_id, name, timeout):
        state.update_binding(SID, lambda b: setattr(b, "chat_id", "oc_other"))
        real(chat_id, name, timeout)

    monkeypatch.setattr(feishu, "update_chat_name", racing)
    _poll(listener._Poller(60))
    b = state.load_binding(SID)
    assert b.chat_id == "oc_other" and b.synced_name == "rw-1"
    assert b.chat_name == "rw-1"


def test_rename_runs_after_message_poll(fake, claude_sessions, clock):
    bind_auto(synced="rw-1")
    write_meta(claude_sessions, os.getpid(), SID, name="新名字")
    _poll(listener._Poller(60))
    heads = [" ".join(c[:3]) for c in fake()]
    get = heads.index("api GET /open-apis/im/v1/messages")
    upd = next(i for i, h in enumerate(heads) if h.startswith("im +chat-update"))
    assert get < upd


def test_rename_skipped_when_stopped(fake, claude_sessions, clock):
    bind_auto(synced="rw-1")
    write_meta(claude_sessions, os.getpid(), SID, name="新名字")
    stop = threading.Event()
    stop.set()
    listener._Poller(60).poll_once(stop)
    assert renames(fake) == []


# ---------- 代码版本与实例信息 ----------


def _mk_tree(root, files):
    (root / "lark_bridge").mkdir(parents=True)
    (root / "bin").mkdir()
    for rel, text in files.items():
        (root / rel).write_text(text)


def test_code_version_changes_with_content(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    files = {"lark_bridge/x.py": "1", "lark_bridge/y.py": "2", "bin/claude-lark": "3"}
    _mk_tree(a, files)
    _mk_tree(b, files)
    va = listener.code_version(a)
    assert len(va) == 12 and all(c in "0123456789abcdef" for c in va)
    assert listener.code_version(b) == va
    (b / "lark_bridge/y.py").write_text("2 changed")
    assert listener.code_version(b) != va
    (a / "lark_bridge/notes.txt").write_text("ignored")  # 非 .py 不计入
    assert listener.code_version(a) == va
    (a / "bin/claude-lark").write_text("3 changed")
    assert listener.code_version(a) != va


def test_code_version_default_root_is_repo():
    assert listener.code_version() == listener.code_version(ROOT)


def test_main_writes_and_removes_version_file(fake):
    claude = subprocess.Popen(["sleep", "60"])
    bind(claude_pid=claude.pid)
    seen = []

    def watcher():
        _wait(lambda: _polls(fake))
        seen.append(json.loads(paths.listener_version_file().read_text()))
        seen.append(listener.instance())
        claude.kill()
        claude.wait()

    th = threading.Thread(target=watcher)
    th.start()
    listener.main(check_interval=0.1, poll_interval=0.1)
    th.join()
    v = listener.code_version()
    assert seen[0] == {"pid": os.getpid(), "code_version": v}
    assert seen[1] == (True, os.getpid(), v)
    assert not paths.listener_version_file().exists()
    assert listener.instance() == (False, None, None)


def test_drop_keeps_foreign_version_file(fake):
    bind()
    assert listener._acquire_pid_file("abc")
    paths.listener_version_file().write_text(
        json.dumps({"pid": 1, "code_version": "x"})
    )
    listener._release_pid_file()
    assert json.loads(paths.listener_version_file().read_text())["pid"] == 1


def test_instance_old_protocol_and_stale_version(fake):
    other = _hold_listener_lock()
    try:
        assert listener.instance() == (True, None, None)  # 锁被持有但没有 pid 文件
        paths.listener_pid_file().write_text(str(os.getppid()))
        assert listener.instance() == (True, os.getppid(), None)  # 旧协议：无实例信息
        paths.listener_version_file().write_text(
            json.dumps({"pid": 999999, "code_version": "v"})
        )
        assert listener.instance() == (True, os.getppid(), None)  # 残留
    finally:
        other.close()


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[1]",
        '{"pid": 5}',
        '{"code_version": "v"}',
        '{"pid": "5", "code_version": "v"}',
    ],
)
def test_instance_corrupt_version_file(fake, content):
    other = _hold_listener_lock()
    try:
        paths.listener_pid_file().write_text("5")
        paths.listener_version_file().write_text(content)
        assert listener.instance() == (True, 5, None)
    finally:
        other.close()


def test_main_logs_starting_and_lock_held(fake):
    bind()
    other = _hold_listener_lock()
    try:
        assert listener.main(check_interval=0.1, poll_interval=0.1) == 0
    finally:
        other.close()
    log = paths.listener_log().read_text()
    v = listener.code_version()
    assert f"listener starting (pid {os.getpid()}, version {v})" in log
    assert "listener lock held by another process, exiting" in log
