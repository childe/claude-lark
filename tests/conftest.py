import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture
def lark_home(tmp_path, monkeypatch):
    home = tmp_path / "lark_home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_LARK_HOME", str(home))
    (home / "config.json").write_text(json.dumps({"owner_open_id": "ou_test_owner"}))
    return home


def write_meta(
    dir, pid, sid, name="rw-1", kind="interactive", source="derived"
):  # noqa: A002
    """写一份 Claude Code 的会话元数据 <dir>/<pid>.json。"""
    data = {
        "pid": pid,
        "sessionId": sid,
        "cwd": "/work",
        "startedAt": 0,
        "kind": kind,
        "name": name,
        "nameSource": source,
    }
    (dir / f"{pid}.json").write_text(json.dumps(data, ensure_ascii=False))


@pytest.fixture
def claude_sessions(tmp_path, monkeypatch):
    d = tmp_path / "claude_sessions"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_LARK_CLAUDE_SESSIONS", str(d))
    return d


_FAKE_LISTENER = Path(__file__).resolve().parent / "fake_listener.py"
fake_listeners = []  # start_fake_listener 拉起的进程，测试 teardown 负责杀掉


def start_fake_listener(*args) -> subprocess.Popen:
    """拉起 tests/fake_listener.py（模拟旧监听进程），等它写好 pid 文件再返回。"""
    p = subprocess.Popen(
        [sys.executable, str(_FAKE_LISTENER), *args],
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
    )
    fake_listeners.append(p)
    pidf = Path(os.environ["CLAUDE_LARK_HOME"]) / "listener.pid"
    end = time.time() + 10
    while time.time() < end:
        try:
            if pidf.read_text().strip() == str(p.pid):
                return p
        except OSError:
            pass
        if p.poll() is not None:
            break
        time.sleep(0.05)
    p.kill()
    p.wait()
    raise RuntimeError("fake listener did not start")


def kill_fake_listeners() -> None:
    while fake_listeners:
        p = fake_listeners.pop()
        if p.poll() is None:
            p.kill()
        p.wait()
