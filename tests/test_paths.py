from pathlib import Path

import pytest

from lark_bridge import paths


def test_home_respects_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_LARK_HOME", str(tmp_path / "h"))
    assert paths.home() == tmp_path / "h"
    assert paths.config_file() == tmp_path / "h" / "config.json"
    assert paths.listener_pid_file() == tmp_path / "h" / "listener.pid"
    assert paths.listener_log() == tmp_path / "h" / "listener.log"


def test_home_default(monkeypatch):
    monkeypatch.delenv("CLAUDE_LARK_HOME", raising=False)
    assert paths.home() == Path.home() / ".claude" / "lark"


def test_session_dir_ok(lark_home):
    sid = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
    assert paths.session_dir(sid) == lark_home / "sessions" / sid


@pytest.mark.parametrize("bad", ["../x", "", "ABC", "a/b", "g1", "abc\n"])
def test_session_dir_rejects_traversal(lark_home, bad):
    with pytest.raises(ValueError):
        paths.session_dir(bad)


def test_lark_cli_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_LARK_CLI", raising=False)
    assert paths.lark_cli() == "lark-cli"
    monkeypatch.setenv("CLAUDE_LARK_CLI", "/x/fake")
    assert paths.lark_cli() == "/x/fake"
