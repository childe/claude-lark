import pytest

from conftest import write_meta
from lark_bridge import meta

SID = "00000000-0000-0000-0000-0000000000e1"


def test_meta_reads_name(claude_sessions):
    write_meta(claude_sessions, 4242, SID, name="  测试改名 abc ", source="user")
    assert meta.is_interactive(4242, SID)
    assert meta.session_name(4242, SID) == "测试改名 abc"


def test_meta_rejects_other_session(claude_sessions):
    write_meta(claude_sessions, 4242, "ffffffff-0000-0000-0000-000000000000")
    assert meta.read(4242, SID) is None
    assert not meta.is_interactive(4242, SID) and meta.session_name(4242, SID) == ""


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"sessionId": 1}'])
def test_meta_rejects_garbage(claude_sessions, content):
    (claude_sessions / "4242.json").write_text(content)
    assert meta.read(4242, SID) is None


def test_meta_non_interactive(claude_sessions):
    write_meta(claude_sessions, 4242, SID, kind="print")
    assert not meta.is_interactive(4242, SID)


def test_meta_missing_file(claude_sessions):
    assert meta.read(99999, SID) is None


def test_meta_non_string_name(claude_sessions):
    write_meta(claude_sessions, 4242, SID, name=None)
    assert meta.session_name(4242, SID) == ""


def test_meta_pid_none(claude_sessions):
    assert meta.read(None, SID) is None and meta.session_name(None, SID) == ""
