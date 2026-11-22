import json

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
