import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import kill_fake_listeners, start_fake_listener
from lark_bridge import listener, paths, restart, state

SID = "00000000-0000-0000-0000-0000000000c1"
ROOT = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).resolve().parent / "fake_lark_cli.py"
BIN = ROOT / "bin" / "claude-lark"
V = listener.code_version()


def bind(sid=SID, chat="oc_test_chat", enabled=True, claude_pid=None):
    state.save_binding(state.Binding(sid, chat, "测试群", "/work", claude_pid, 1.0))
    if enabled:
        state.set_enabled(sid, True)


def _wait(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _hold_listener_lock():
    """模拟另一个正在运行的监听进程：拿住 listener.lock。close() 即释放。"""
    import fcntl

    f = open(paths.home() / "listener.lock", "a+")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def _kill_running_listener():
    for _ in range(3):
        held, pid, _ = listener.instance()
        if not held or pid is None or pid == os.getpid():
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _wait(lambda: not listener.is_running(), timeout=5)


@pytest.fixture
def env(lark_home, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_LARK_CLI", str(FAKE))
    monkeypatch.setenv("FAKE_LARK_LOG", str(tmp_path / "fake_lark.log"))
    for k in ("FAKE_LARK_FAIL", "FAKE_LARK_SLEEP", "FAKE_LARK_EVENTS"):
        monkeypatch.delenv(k, raising=False)
    procs = []
    yield procs
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait()
    kill_fake_listeners()
    _kill_running_listener()


@pytest.fixture
def sigs(monkeypatch):
    rec = []
    real = restart._signal

    def record(pid, sig):
        rec.append((pid, sig))
        real(pid, sig)

    monkeypatch.setattr(restart, "_signal", record)
    return rec


def test_restart_default_timeouts():
    assert (restart.TERM_WAIT, restart.KILL_WAIT, restart.UP_WAIT) == (20, 5, 5)


def test_restart_no_listener_no_session(env, sigs, capsys):
    assert restart.run() == 0
    assert "没有开启中的会话，不需要监听进程" in capsys.readouterr().out
    assert sigs == [] and not listener.is_running()


def test_restart_no_listener_with_session_starts(env, sigs, capsys):
    bind()
    assert restart.run() == 0
    held, pid, ver = listener.instance()
    assert held and ver == V
    assert f"✅ 监听进程已重启（pid {pid}，版本 {V}）" in capsys.readouterr().out


def test_restart_replaces_current_listener(env, sigs, capsys):
    bind()
    old = subprocess.Popen(
        [sys.executable, str(BIN), "listen"],
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
    )
    env.append(old)
    assert _wait(lambda: listener.instance()[1] == old.pid)
    assert restart.run() == 0
    assert old.wait(timeout=5) == 0
    held, pid, ver = listener.instance()
    assert held and pid != old.pid and ver == V
    assert sigs == [(old.pid, signal.SIGTERM)]


def test_restart_migrates_old_protocol_listener(env, sigs):
    bind()
    old = start_fake_listener()  # 只有 pid 文件，没有实例信息文件
    assert listener.instance() == (True, old.pid, None)
    assert restart.run() == 0
    assert sigs == [(old.pid, signal.SIGTERM)]
    held, pid, ver = listener.instance()
    assert held and pid != old.pid and ver == V


def test_restart_stale_version_file_treated_unknown(env, sigs):
    bind()
    old = start_fake_listener()
    paths.listener_version_file().write_text(
        json.dumps({"pid": 999999, "code_version": V})
    )
    assert restart.run() == 0
    assert (old.pid, signal.SIGTERM) in sigs
    held, pid, ver = listener.instance()
    assert pid != old.pid and ver == V
    assert json.loads(paths.listener_version_file().read_text())["pid"] == pid


def test_restart_stops_without_session(env, sigs, capsys):
    old = start_fake_listener("--version", "000000000000")
    assert restart.run() == 0
    assert old.wait(timeout=5) == 0
    assert not listener.is_running()
    assert "没有开启中的会话" in capsys.readouterr().out


def test_restart_sigkill_when_term_ignored(env, sigs):
    bind()
    old = start_fake_listener("--ignore-term")
    assert restart.run(term_wait=0.5) == 0
    assert sigs[:2] == [(old.pid, signal.SIGTERM), (old.pid, signal.SIGKILL)]
    assert listener.instance()[2] == V


def test_restart_no_sigkill_if_exited_just_before(env, sigs, monkeypatch):
    bind()
    old = start_fake_listener("--ignore-term")
    calls = []

    def wait_handover(p, timeout, interval):
        calls.append(p)
        if len(calls) == 1:  # 超时那一刻原实例自己退出了
            os.kill(old.pid, signal.SIGKILL)
            old.wait()
            return False
        return real(p, timeout, interval)

    real = restart._wait_handover
    monkeypatch.setattr(restart, "_wait_handover", wait_handover)
    assert restart.run() == 0
    assert (old.pid, signal.SIGKILL) not in sigs


def test_restart_accepts_concurrent_handover(env, sigs):
    """等待期间别的路径（/lark、auto-connect）拉起了新实例：接受交接，不 SIGKILL 任何人。"""
    bind()
    old = start_fake_listener("--ignore-term")

    def take_over():
        _wait(lambda: (old.pid, signal.SIGTERM) in sigs)
        os.kill(old.pid, signal.SIGKILL)
        old.wait()
        listener.ensure_running()

    th = threading.Thread(target=take_over)
    th.start()
    assert restart.run(interval=0.5) == 0
    th.join()
    assert all(s != signal.SIGKILL for _, s in sigs)
    held, pid, ver = listener.instance()
    assert held and pid != old.pid and ver == V


def test_restart_retries_spawn_once(env, sigs, monkeypatch):
    bind()
    n = []
    real = listener.ensure_running

    def flaky():
        n.append(1)
        return True if len(n) == 1 else real()  # 第一次"返回 True 但没起来"

    monkeypatch.setattr(listener, "ensure_running", flaky)
    assert restart.run(up_wait=1) == 0
    assert len(n) == 2 and listener.instance()[2] == V


def test_restart_reports_spawn_failure(env, sigs, monkeypatch, capsys):
    bind()
    monkeypatch.setattr(listener, "ensure_running", lambda: True)
    assert restart.run(up_wait=0.3) == 1
    out = capsys.readouterr().out
    assert "❌" in out and "listener.log" in out and "errors.log" in out


def test_restart_lock_held_without_pid_file(env, sigs, capsys):
    bind()
    other = _hold_listener_lock()
    try:
        assert restart.run() == 1
    finally:
        other.close()
    assert sigs == []
    assert "❌" in capsys.readouterr().out


def test_restart_kill_ineffective_reports_failure(env, monkeypatch, capsys):
    bind()
    old = start_fake_listener("--ignore-term")
    monkeypatch.setattr(restart, "_signal", lambda pid, sig: None)  # 信号全被吞
    assert restart.run(term_wait=0.3, kill_wait=0.3) == 1
    assert "❌" in capsys.readouterr().out
    assert old.poll() is None


def test_cli_restart_listener(env, capsys):
    from lark_bridge import cli

    assert cli.main(["restart-listener"]) == 0
