import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import write_meta
from lark_bridge import commands, config, listener, paths, state

SID = "00000000-0000-0000-0000-0000000000c1"
SID2 = "00000000-0000-0000-0000-0000000000c2"
ROOT = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).resolve().parent / "fake_lark_cli.py"
BIN = ROOT / "bin" / "claude-lark"


@pytest.fixture
def fake(lark_home, tmp_path, monkeypatch):
    log = tmp_path / "fake_lark.log"
    monkeypatch.setenv("CLAUDE_LARK_CLI", str(FAKE))
    monkeypatch.setenv("FAKE_LARK_LOG", str(log))
    for k in ("FAKE_LARK_FAIL", "FAKE_LARK_NOT_IN_CHAT"):
        monkeypatch.delenv(k, raising=False)
    started = []
    monkeypatch.setattr(listener, "ensure_running", lambda: started.append(1) or True)
    monkeypatch.setattr(commands.procs, "find_claude_ancestor", lambda: os.getpid())

    def calls():
        if not log.exists():
            return []
        return [json.loads(x) for x in log.read_text().splitlines() if x.strip()]

    calls.started = started
    return calls


real_ensure_running = listener.ensure_running


@pytest.fixture
def auto(fake, claude_sessions):
    write_meta(claude_sessions, os.getpid(), SID)
    config.set_auto_on(True)
    return fake


def creates(calls):
    return [c for c in calls() if c[:2] == ["im", "+chat-create"]]


def arg(call, flag):
    return call[call.index(flag) + 1]


def sent(calls):
    return [arg(c, "--markdown") for c in calls() if c[:2] == ["im", "+messages-send"]]


def bind(sid, chat="oc_test_chat", name="g", pid=None, enable=False):
    state.save_binding(state.Binding(sid, chat, name, "/work", pid, 0.0))
    if enable:
        state.set_enabled(sid, True, pid)


def test_slash_need_name_when_unbound(fake):
    assert commands.slash(SID, "") == "NEED_NAME"
    assert fake() == []


def test_slash_creates_group_with_given_name(fake):
    out = commands.slash(SID, "我的群")
    assert out.startswith("RESULT:")
    assert commands.HINT in out
    create = [c for c in fake() if c[:2] == ["im", "+chat-create"]]
    assert len(create) == 1
    assert arg(create[0], "--name") == "我的群"
    assert arg(create[0], "--users") == "ou_test_owner"
    b = state.load_binding(SID)
    assert b.chat_id == "oc_test_chat" and b.chat_name == "我的群"
    assert b.claude_pid == os.getpid()
    assert state.is_enabled(SID)
    assert fake.started == [1]
    msgs = sent(fake)
    assert msgs == [f"已连接：{os.getcwd()}，会话 {SID[:8]}"]


NO_OWNER = "RESULT: 未找到 owner 配置，请重新运行 install.sh"


@pytest.mark.parametrize("cfg", ["{}", "not json", '{"owner_open_id": ""}', None])
def test_slash_refuses_without_owner_config(fake, lark_home, cfg):
    """没有 owner 时监听进程会丢弃所有消息：不能悄悄退回 auth status 去开桥接。"""
    if cfg is None:
        (lark_home / "config.json").unlink()
    else:
        (lark_home / "config.json").write_text(cfg)
    assert commands.slash(SID, "x") == NO_OWNER
    assert commands.slash(SID2, "oc_test_other") == NO_OWNER
    assert commands.on(SID, "x") == NO_OWNER
    bind(SID, name="旧群")
    assert commands.slash(SID, "") == NO_OWNER
    assert not state.is_enabled(SID) and not state.is_enabled(SID2)
    assert fake() == [] and fake.started == []
    # off / status 不需要 owner
    assert "未绑定" not in commands.slash(SID, "status")
    assert commands.slash(SID, "off").startswith("RESULT: 已关闭桥接")


def test_slash_existing_binding_ignores_name(fake):
    bind(SID, name="旧群")
    out = commands.slash(SID, "新名字")
    assert "已有绑定群 旧群，忽略群名参数" in out
    assert not [c for c in fake() if c[:2] == ["im", "+chat-create"]]
    assert state.is_enabled(SID)
    assert state.load_binding(SID).chat_name == "旧群"
    assert state.load_binding(SID).claude_pid == os.getpid()


def test_slash_existing_binding_no_args_no_ignore_note(fake):
    bind(SID, name="旧群")
    assert "忽略" not in commands.slash(SID, "")


def test_slash_rebind_with_chat_id_refused(fake):
    bind(SID, name="旧群")
    out = commands.slash(SID, "oc_test_other")
    assert out == "RESULT: 当前会话已绑定群 旧群，先 /lark off 再换绑"
    assert state.load_binding(SID).chat_id == "oc_test_chat"
    assert fake() == []


def test_slash_rebind_with_link_refused(fake):
    bind(SID, name="旧群")
    out = commands.slash(SID, "https://x.feishu.cn/chat/abc")
    assert "先 /lark off 再换绑" in out


def test_slash_bind_existing_chat(fake):
    out = commands.slash(SID, "oc_test_other")
    assert out.startswith("RESULT: 已开启桥接")
    b = state.load_binding(SID)
    assert b.chat_id == "oc_test_other"
    assert [c for c in fake() if c[:2] == ["im", "+chat-members-list"]]
    assert not [c for c in fake() if c[:2] == ["im", "+chat-create"]]
    assert state.is_enabled(SID)


def test_slash_bind_chat_bot_not_in_chat(fake, monkeypatch):
    monkeypatch.setenv("FAKE_LARK_NOT_IN_CHAT", "1")
    out = commands.slash(SID, "oc_test_other")
    assert out.startswith("RESULT: 机器人不在这个群里")
    assert state.load_binding(SID) is None
    assert not state.is_enabled(SID)


def test_slash_link_asks_for_chat_id(fake):
    out = commands.slash(SID, "https://x.feishu.cn/chat/abc")
    assert out.startswith("RESULT:") and "oc_" in out
    assert state.load_binding(SID) is None
    assert fake() == []


def test_slash_bind_occupied_chat_refused(fake):
    bind(SID2, chat="oc_test_other", pid=os.getpid(), enable=True)
    out = commands.slash(SID, "oc_test_other")
    assert out.startswith("RESULT: 群已被会话 " + SID2[:8])
    assert "/work" in out
    assert state.load_binding(SID) is None
    assert not state.is_enabled(SID)


def test_slash_bind_takes_over_dead_occupant(fake):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    bind(SID2, chat="oc_test_other", pid=p.pid, enable=True)
    out = commands.slash(SID, "oc_test_other")
    assert "已开启桥接" in out


def test_slash_off_keeps_binding(fake):
    bind(SID, name="旧群", enable=True)
    out = commands.slash(SID, "off")
    assert out == "RESULT: 已关闭桥接（群 旧群 仍保留绑定）"
    assert not state.is_enabled(SID)
    assert state.load_binding(SID).chat_id == "oc_test_chat"
    assert sent(fake) == ["已断开，终端里敲 /lark 可重新连接"]


def test_slash_off_unbound(fake):
    assert commands.slash(SID, "off").startswith("RESULT:")
    assert fake() == []


def test_slash_status(fake, monkeypatch):
    assert "未绑定" in commands.slash(SID, "status")
    bind(SID, name="旧群", enable=True)
    monkeypatch.setattr(listener, "is_running", lambda: True)
    out = commands.slash(SID, "status")
    assert out == "RESULT: 绑定群 旧群，桥接已开启，监听进程存活；自动连接未开启"
    state.set_enabled(SID, False)
    monkeypatch.setattr(listener, "is_running", lambda: False)
    assert commands.slash(SID, "status").endswith(
        "桥接未开启，监听进程未运行；自动连接未开启"
    )


def test_on_creates_group_and_truncates_name_to_60(fake):
    out = commands.on(SID, "长" * 100)
    assert out.startswith("RESULT: 已开启桥接")
    create = [c for c in fake() if c[:2] == ["im", "+chat-create"]][0]
    assert arg(create, "--name") == "长" * 60
    assert state.load_binding(SID).chat_name == "长" * 60


def test_on_existing_binding_ignores_name(fake):
    bind(SID, name="旧群")
    assert "忽略群名参数" in commands.on(SID, "别的")
    assert not [c for c in fake() if c[:2] == ["im", "+chat-create"]]


def test_slash_args_with_quotes(fake):
    commands.slash(SID, 'a "b" c')
    create = [c for c in fake() if c[:2] == ["im", "+chat-create"]][0]
    assert arg(create, "--name") == 'a "b" c'


def test_slash_never_raises(fake, monkeypatch, lark_home):
    monkeypatch.setenv("FAKE_LARK_FAIL", "1")
    out = commands.slash(SID, "x")
    assert out.startswith("RESULT: 出错了：")
    assert "Traceback" in (lark_home / "errors.log").read_text()


def test_missing_owner_gives_result(fake, lark_home, monkeypatch):
    (lark_home / "config.json").write_text("{}")
    monkeypatch.setenv("FAKE_LARK_FAIL", "1")
    assert commands.slash(SID, "x") == NO_OWNER


def test_cli_slash_and_on(fake, lark_home):
    env = dict(os.environ)
    r = subprocess.run(
        [sys.executable, str(BIN), "slash", "--session", SID, "--args", ""],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0 and r.stdout.strip() == "NEED_NAME"
    env["CLAUDE_LARK_CLI"] = str(FAKE)
    r = subprocess.run(
        [sys.executable, str(BIN), "on", "--session", SID, "--name", "测试"],
        capture_output=True,
        text=True,
        env=env,
    )
    try:
        # 测试进程树里没有 claude，应拒绝开启；有就正常开启
        assert r.returncode == 0
        assert r.stdout.startswith(("RESULT: 已开启桥接", "RESULT: 未能识别 claude"))
    finally:
        # 在 claude 里跑测试时（真实进程树里有 claude）桥接会真的开启，
        # ensure_running 在后台拉起的监听进程要等它写出 pid 文件再杀，否则会泄漏
        if r.stdout.startswith("RESULT: 已开启桥接"):
            _kill_listener()


def _kill_listener(timeout=10):
    import time

    from lark_bridge import procs

    end = time.time() + timeout
    pid = None
    while time.time() < end and not pid:
        pid = listener._read_pid()
        time.sleep(0.05)
    assert pid, "listener never wrote its pid file"
    os.kill(pid, 15)
    while time.time() < end and procs.pid_alive(pid):
        time.sleep(0.05)
    assert not procs.pid_alive(pid)


def test_cli_bad_session_never_raises(lark_home):
    r = subprocess.run(
        [sys.executable, str(BIN), "slash", "--session", "../x", "--args", "y"],
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    assert r.returncode == 0 and r.stdout.startswith("RESULT: 出错了：")


def test_slash_refuses_without_claude_pid(fake, monkeypatch, lark_home):
    monkeypatch.setattr(commands.procs, "find_claude_ancestor", lambda: None)
    out = commands.slash(SID, "新群")
    assert out.startswith("RESULT: 未能识别 claude 进程") and "doctor" in out
    assert not [c for c in fake() if c[:2] == ["im", "+chat-create"]]
    assert state.load_binding(SID) is None and not state.is_enabled(SID)
    assert "find_claude_ancestor" in (lark_home / "errors.log").read_text()
    bind(SID)
    assert commands.slash(SID, "").startswith("RESULT: 未能识别")
    assert not state.is_enabled(SID)
    assert commands.slash(SID2, "oc_test_other").startswith("RESULT: 未能识别")


def test_url_is_never_a_group_name(fake):
    out = commands.slash(SID, "https://example.larksuite.com/chat/abc")
    assert out.startswith("RESULT:") and "oc_" in out
    assert fake() == [] and state.load_binding(SID) is None


def test_binding_saved_even_if_enable_fails(fake, monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(listener, "ensure_running", boom)
    assert commands.slash(SID, "g").startswith("RESULT: 出错了")
    assert state.load_binding(SID).chat_id == "oc_test_chat"


def test_bind_existing_occupied_skips_network(fake):
    bind(SID2, chat="oc_test_other", pid=os.getpid(), enable=True)
    commands.slash(SID, "oc_test_other")
    assert not [c for c in fake() if c[:2] == ["im", "+chat-members-list"]]


def test_off_twice_sends_once(fake):
    bind(SID, enable=True)
    commands.slash(SID, "off")
    commands.slash(SID, "off")
    assert len(sent(fake)) == 1


def test_find_claude_ancestor_recognises_node(monkeypatch):
    from lark_bridge import procs

    table = {
        os.getppid(): ("500", "zsh", ""),
        500: ("600", "/usr/bin/node", "node /x/@anthropic-ai/claude-code/cli.js"),
    }

    def ps(field, pid):
        ppid, comm, args = table[pid]
        return args if field == "args" else f"{ppid} {comm}"

    monkeypatch.setattr(procs, "_ps", ps)
    assert procs.find_claude_ancestor() == 500
    table[500] = ("600", "/usr/bin/node", "node /x/server.js")
    table[600] = ("1", "launchd", "")
    assert procs.find_claude_ancestor() is None


def test_find_claude_ancestor_real_ps(tmp_path):
    """不 mock ps：真实进程树里祖先进程名为 claude 时要能找到它。

    回归：`ps -o ppid,comm=` 会输出 PPID 表头行，导致永远找不到。"""
    import subprocess
    import sys

    from lark_bridge import procs

    claude = tmp_path / "claude"
    claude.symlink_to("/bin/bash")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = "from lark_bridge import procs; print(procs.find_claude_ancestor())"
    # 末尾的 `; echo $$` 让 bash 不能 exec 掉自己，并打印 bash 自己的 pid
    out = subprocess.run(
        [str(claude), "-c", f'"{sys.executable}" -c "{code}"; echo $$'],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.split()
    assert out[0] == out[1]
    assert procs._ps("ppid,comm", os.getpid()).split()[0].isdigit()


# ---------------- final review


def test_enable_fails_loudly_when_claude_start_time_unreadable(fake, monkeypatch):
    """记不下 claude 的启动时间，enabled 就不会生效：/lark 不能假装成功。"""
    monkeypatch.setattr(state.procs, "start_time", lambda pid: "")
    bind(SID, name="旧群")
    out = commands.slash(SID, "")
    assert out.startswith("RESULT: ") and "已开启桥接" not in out
    assert "启动时间" in out
    assert not state.is_enabled(SID)
    assert fake.started == []


# ---------------- auto on/off


def test_slash_auto_on_off(fake):
    assert commands.slash(SID, "auto on") == f"RESULT: {commands.AUTO_ON_TEXT}"
    assert config.auto_on() is True
    assert commands.slash(SID, "auto off") == f"RESULT: {commands.AUTO_OFF_TEXT}"
    assert config.auto_on() is False
    assert fake() == []


def test_auto_args_must_match_whole_string(fake):
    assert commands.slash(SID, "  auto on ") == f"RESULT: {commands.AUTO_ON_TEXT}"
    commands.slash(SID, "auto off")
    commands.slash(SID, "auto onx")  # 按群名建群
    create = [c for c in fake() if c[:2] == ["im", "+chat-create"]]
    assert arg(create[0], "--name") == "auto onx"
    assert config.auto_on() is False


def test_slash_auto_alone_is_group_name(fake):
    commands.slash(SID, "auto")
    create = [c for c in fake() if c[:2] == ["im", "+chat-create"]]
    assert arg(create[0], "--name") == "auto"
    assert config.auto_on() is False


def test_slash_status_shows_auto(fake):
    assert commands.slash(SID, "status").endswith("；自动连接未开启")
    config.set_auto_on(True)
    assert commands.slash(SID, "status").endswith("；自动连接已开启")


# ---------------- auto_connect


def test_auto_connect_creates_on_first_prompt(auto):
    assert commands.auto_connect(SID, "prompt", "  修一下登录 bug\n细节…") == "created"
    create = creates(auto)
    assert len(create) == 1 and arg(create[0], "--name") == "修一下登录 bug"
    b = state.load_binding(SID)
    assert b.created_by_bridge is True and b.synced_name == "rw-1"
    assert state.is_enabled(SID) and not state.has_auto_pending(SID)


def test_auto_connect_second_prompt_does_nothing(auto):
    commands.auto_connect(SID, "prompt", "a")
    state.set_enabled(SID, False)
    assert commands.auto_connect(SID, "prompt", "b") == ""
    assert len(creates(auto)) == 1


def test_auto_connect_retries_enable_without_recreating(auto, monkeypatch):
    real = commands._enable
    monkeypatch.setattr(commands, "_enable", lambda *a, **k: commands._result("boom"))
    assert commands.auto_connect(SID, "prompt", "a") == ""
    assert state.has_auto_pending(SID) and state.load_binding(SID) is not None
    monkeypatch.setattr(commands, "_enable", real)
    assert commands.auto_connect(SID, "prompt", "b") == "enabled"
    assert len(creates(auto)) == 1
    assert state.is_enabled(SID) and not state.has_auto_pending(SID)


def listener_popen(monkeypatch, fail):
    """只拦截拉起监听进程的 Popen（argv 以 listen 结尾）；lark-cli 等调用照常走真实 Popen。

    listener.subprocess 就是全局 subprocess 模块，整体替换会连 fake lark-cli 也拦掉。"""
    real = subprocess.Popen

    def popen(argv, *a, **k):
        if list(argv)[-1:] == ["listen"]:
            if fail:
                raise OSError("fork failed")
            return None
        return real(argv, *a, **k)

    monkeypatch.setattr(listener.subprocess, "Popen", popen)


def test_auto_connect_retries_after_listener_spawn_failure(auto, monkeypatch):
    """真实 _enable 内拉起监听进程失败：不留 enabled、保留 auto_pending，下一句只重试开启。"""
    monkeypatch.setattr(listener, "ensure_running", real_ensure_running)
    monkeypatch.setattr(listener, "_lock_held_by_other", lambda: False)
    listener_popen(monkeypatch, fail=True)
    assert commands.auto_connect(SID, "prompt", "a") == ""
    assert not state.is_enabled(SID) and state.has_auto_pending(SID)
    listener_popen(monkeypatch, fail=False)
    assert commands.auto_connect(SID, "prompt", "b") == "enabled"
    assert len(creates(auto)) == 1
    assert state.is_enabled(SID) and not state.has_auto_pending(SID)


def test_auto_pending_then_off_then_prompt_stays_off(auto, monkeypatch):
    real = commands._enable
    monkeypatch.setattr(commands, "_enable", lambda *a, **k: commands._result("boom"))
    commands.auto_connect(SID, "prompt", "a")
    monkeypatch.setattr(commands, "_enable", real)
    commands.slash(SID, "off")
    assert not state.has_auto_pending(SID)
    assert commands.auto_connect(SID, "prompt", "b") == ""
    assert not state.is_enabled(SID)


def test_manual_lark_after_failed_auto_clears_pending(auto, monkeypatch):
    real = commands._enable
    monkeypatch.setattr(commands, "_enable", lambda *a, **k: commands._result("boom"))
    commands.auto_connect(SID, "prompt", "a")
    monkeypatch.setattr(commands, "_enable", real)
    assert commands.slash(SID, "").startswith("RESULT: 已开启桥接")
    assert not state.has_auto_pending(SID) and state.is_enabled(SID)
    assert len(creates(auto)) == 1


def test_off_then_prompt_does_not_auto_enable(auto):
    commands.auto_connect(SID, "prompt", "a")
    commands.slash(SID, "off")
    assert commands.auto_connect(SID, "prompt", "b") == ""
    assert not state.is_enabled(SID)


@pytest.mark.parametrize("prompt", ["/lark", "/lark 群名", "<task-notification>x"])
def test_auto_connect_skips_special_prompts(auto, prompt):
    assert commands.auto_connect(SID, "prompt", prompt) == "" and auto() == []


def test_auto_connect_requires_switch_and_interactive(auto, claude_sessions):
    config.set_auto_on(False)
    assert commands.auto_connect(SID, "prompt", "a") == ""
    config.set_auto_on(True)
    write_meta(claude_sessions, os.getpid(), SID, kind="print")
    assert commands.auto_connect(SID, "prompt", "a") == "" and auto() == []


def test_auto_connect_skips_foreign_or_missing_meta(auto, claude_sessions):
    write_meta(claude_sessions, os.getpid(), SID2)
    assert commands.auto_connect(SID, "prompt", "a") == ""
    (claude_sessions / f"{os.getpid()}.json").write_text("garbage")
    assert commands.auto_connect(SID, "prompt", "a") == "" and auto() == []


def test_auto_connect_without_owner_logs(auto, lark_home):
    (lark_home / "config.json").write_text('{"auto_on": true}')
    assert commands.auto_connect(SID, "prompt", "a") == "" and auto() == []
    assert "owner" in (lark_home / "errors.log").read_text()


def test_auto_connect_never_raises(auto, monkeypatch, lark_home):
    monkeypatch.setenv("FAKE_LARK_FAIL", "1")
    assert commands.auto_connect(SID, "prompt", "a") == ""
    assert "commands.auto_connect" in (lark_home / "errors.log").read_text()


def test_auto_connect_resume(auto):
    assert commands.auto_connect(SID, "resume") == ""  # 没 binding 不建群
    bind(SID, name="手动群")  # 没有 auto_pending
    assert commands.auto_connect(SID, "resume") == "enabled" and state.is_enabled(SID)
    assert creates(auto) == []


def test_auto_connect_resume_failure_logged(auto, lark_home):
    bind(SID2, chat="oc_test_chat", pid=os.getpid(), enable=True)
    bind(SID, name="手动群")
    assert commands.auto_connect(SID, "resume") == ""
    assert "群已被会话" in (lark_home / "errors.log").read_text()


def test_auto_connect_and_manual_do_not_create_two_chats(auto):
    """手动 /lark 持有 connect.lock 时，自动连接等它结束后重读到 binding。"""
    entered = threading.Event()

    def manual():
        with state.connect_lock(SID):
            entered.set()
            time.sleep(0.3)
            state.save_binding(
                state.Binding(SID, "oc_manual", "手动群", "/work", None, 0.0)
            )

    t = threading.Thread(target=manual)
    t.start()
    assert entered.wait(5)
    assert commands.auto_connect(SID, "prompt", "a") == ""
    t.join(5)
    assert creates(auto) == []
    assert state.load_binding(SID).chat_id == "oc_manual"


def test_auto_name_fallbacks():
    assert commands.auto_name("\n  \n", "rw-3", "/a/proj") == "rw-3"
    assert commands.auto_name("", "", "/a/proj") == "proj"
    assert commands.auto_name("😀" * 30, "x", "/a") == "😀" * 20
    assert commands.auto_name("  hi  \nmore", "x", "/a") == "hi"
    assert commands.auto_name("", "", "/") != ""


def test_manual_create_records_baseline(auto):
    commands.slash(SID, "我的群")
    b = state.load_binding(SID)
    assert b.created_by_bridge is True and b.synced_name == "rw-1"


def test_bind_existing_not_created_by_bridge(auto):
    commands.slash(SID, "oc_existing")
    assert state.load_binding(SID).created_by_bridge is False


def test_enable_reports_listener_spawn_failure(fake, monkeypatch):
    monkeypatch.setattr(listener, "ensure_running", lambda: False)
    bind(SID, name="旧群")
    out = commands.slash(SID, "")
    assert out == "RESULT: 拉起监听进程失败，未开启桥接，详见 errors.log"
    assert not state.is_enabled(SID)
