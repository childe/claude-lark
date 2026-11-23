import pytest

from conftest import kill_fake_listeners, start_fake_listener
from lark_bridge import doctor, listener, paths, state

SID = "00000000-0000-0000-0000-0000000000d1"


def bind(sid=SID, chat="oc_test_chat", enabled=True, claude_pid=None):
    state.save_binding(state.Binding(sid, chat, "测试群", "/work", claude_pid, 1.0))
    if enabled:
        state.set_enabled(sid, True)


@pytest.fixture
def lines(lark_home):
    out = []
    yield out
    kill_fake_listeners()


@pytest.fixture
def report(lines):
    return lambda ok, msg, fix=None: lines.append((ok, msg, fix))


def test_doctor_listener_current_version(lines, report):
    start_fake_listener("--version", listener.code_version())
    doctor._check_listener(report)
    assert lines == [(True, "监听进程运行中（代码为当前版本）", None)]


def test_doctor_listener_old_version(lines, report):
    start_fake_listener("--version", "000000000000")
    doctor._check_listener(report)
    ok, msg, fix = lines[0]
    assert not ok and msg == "监听进程运行的是旧代码"
    assert "install.sh" in fix and "claude-lark restart-listener" in fix


def test_doctor_listener_unknown_version(lines, report):
    start_fake_listener()  # 旧协议：无实例信息文件
    doctor._check_listener(report)
    assert lines[0][:2] == (False, "监听进程运行的是旧代码")


def test_doctor_no_listener_no_session(lines, report):
    doctor._check_listener(report)
    assert lines == [
        (True, "监听进程未运行（在会话里 /lark 开启桥接时会自动拉起）", None)
    ]


def test_doctor_session_but_no_listener(lines, report):
    bind()  # 开启中、claude_pid=None
    doctor._check_listener(report)
    ok, msg, fix = lines[0]
    assert not ok and msg == "有开启中的会话但监听进程未运行"
    assert "claude-lark restart-listener" in fix
    assert "listener.log" in fix and "errors.log" in fix


def test_doctor_does_not_delete_stale_enabled(lines, report):
    state.save_binding(state.Binding(SID, "oc_x", "群", "/w", None, 1.0))
    f = paths.session_dir(SID) / "enabled"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("999999\nticks:1")  # 已死的 claude
    doctor._check_listener(report)
    assert f.exists()
    assert lines[0][0] is True  # 失效会话不算开启
