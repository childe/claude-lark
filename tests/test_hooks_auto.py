"""auto_on 下 hook 的接线：首句自动建群并镜像、resume 接回、只清理别的进程的 enabled。"""

import os

import pytest

from conftest import write_meta
from lark_bridge import commands, config, hooks, listener, state
from test_hooks_stop import (  # noqa: F401 - fixtures
    SID,
    Clock,
    fake,
    prompt_input,
    session_input,
)

real_ensure_running = listener.ensure_running


@pytest.fixture
def auto(fake, claude_sessions, monkeypatch):
    write_meta(claude_sessions, os.getpid(), SID)
    config.set_auto_on(True)
    monkeypatch.setattr(commands.procs, "find_claude_ancestor", lambda: os.getpid())
    monkeypatch.setattr(listener, "ensure_running", lambda: True)
    monkeypatch.setenv("CLAUDE_LARK_IDLE_SECONDS", "1")
    return fake


@pytest.fixture
def mirrors(monkeypatch):
    out = []
    monkeypatch.setattr(hooks, "_spawn_mirror", lambda sid, text: out.append(text))
    return out


def creates(calls):
    return [c for c in calls() if c[:2] == ["im", "+chat-create"]]


def saved_binding():
    state.save_binding(state.Binding(SID, "oc_test_chat", "测试群", "/work", None, 1.0))


def run(event, text, clock=None):
    clock = clock or Clock()
    return hooks.run(event, text, clock.now, clock.sleep)


def resume_input(source="resume"):
    return session_input("SessionStart", "source", source)


def listener_popen(monkeypatch, fail):
    """只拦截拉起监听进程的 Popen；fake lark-cli 照常走真实 Popen。"""
    real = listener.subprocess.Popen

    def popen(argv, *a, **k):
        if list(argv)[-1:] == ["listen"]:
            if fail:
                raise OSError("fork failed")
            return None
        return real(argv, *a, **k)

    monkeypatch.setattr(listener.subprocess, "Popen", popen)


def test_wait_hook_auto_creates_and_mirrors_first_prompt(auto, mirrors):
    clock = Clock()
    out = hooks.run("wait", prompt_input("修 bug"), clock.now, clock.sleep)
    assert state.is_enabled(SID)
    assert mirrors == ["修 bug"]
    assert state.get_counter(SID, "busy") == 1
    assert out == "" and clock.sleeps > 0  # 成为了等待进程，等到 deadline


def test_wait_hook_resets_keepalive_pending(auto, mirrors):
    state.set_counter(SID, "keepalive_pending", 1)
    run("wait", prompt_input("修 bug"))
    assert state.get_counter(SID, "keepalive_pending") == 0


def test_first_prompt_mirrored_once_prompt_hook_first(auto, mirrors):
    """常见顺序：同步 prompt hook 先跑（未开启，什么都不做），wait 再自动开启并镜像。"""
    clock = Clock()
    hooks.run("prompt", prompt_input("修 bug"), clock.now, clock.sleep)
    hooks.run("wait", prompt_input("修 bug"), clock.now, clock.sleep)
    assert mirrors == ["修 bug"]


def test_first_prompt_mirrored_once_prompt_hook_after_enable(auto, mirrors):
    """prompt hook 在 wait 完成开启之后才跑：prompt hook 只改计数器，不镜像。"""
    clock = Clock()
    hooks.run("wait", prompt_input("修 bug"), clock.now, clock.sleep)
    hooks.run("prompt", prompt_input("修 bug"), clock.now, clock.sleep)
    assert mirrors == ["修 bug"]
    assert state.get_counter(SID, "busy") == 1


def test_first_prompt_mirrored_once_interleaved(auto, mirrors, monkeypatch):
    """开启之后、wait 镜像之前插入 prompt hook：仍只由 wait 镜像一次。"""
    clock = Clock()
    real = commands.auto_connect

    def connect_then_prompt(*a, **k):
        r = real(*a, **k)
        hooks.run("prompt", prompt_input("修 bug"), clock.now, clock.sleep)
        return r

    monkeypatch.setattr(commands, "auto_connect", connect_then_prompt)
    hooks.run("wait", prompt_input("修 bug"), clock.now, clock.sleep)
    assert mirrors == ["修 bug"]


def test_rapid_identical_prompts_each_mirrored(auto, mirrors):
    """首句之后立刻（同一虚拟时刻，远小于 10 秒）再次提交相同文本：两次输入各镜像一次。"""
    clock = Clock()
    for _ in range(2):  # 每次输入：prompt 与 wait 各跑一次
        hooks.run("prompt", prompt_input("修 bug"), clock.now, clock.sleep)
        hooks.run("wait", prompt_input("修 bug"), clock.now, clock.sleep)
    assert mirrors == ["修 bug", "修 bug"]
    assert len(creates(auto)) == 1


def test_wait_hook_does_not_mirror_special_prompts(auto, mirrors):
    clock = Clock()
    hooks.run("wait", prompt_input("修 bug"), clock.now, clock.sleep)  # 开启
    for p in ["/lark status", "<task-notification>x", "   "]:
        hooks.run("wait", prompt_input(p), clock.now, clock.sleep)
    assert mirrors == ["修 bug"]


def test_wait_hook_lark_command_does_not_auto_create(auto, mirrors):
    run("wait", prompt_input("/lark 我的群"))
    assert creates(auto) == [] and mirrors == [] and not state.is_enabled(SID)


def test_wait_hook_recovers_after_listener_spawn_failure(auto, monkeypatch):
    """真实 ensure_running 拉起失败 → 不开启；下一句的 wait hook 走 _auto_wait 重试，不重复建群。"""
    monkeypatch.setattr(listener, "ensure_running", real_ensure_running)
    monkeypatch.setattr(listener, "_lock_held_by_other", lambda: False)
    listener_popen(monkeypatch, fail=True)
    clock = Clock()
    hooks.run("wait", prompt_input("a"), clock.now, clock.sleep)
    assert not state.is_enabled(SID) and state.has_auto_pending(SID)
    listener_popen(monkeypatch, fail=False)
    hooks.run("wait", prompt_input("b"), clock.now, clock.sleep)
    assert state.is_enabled(SID) and not state.has_auto_pending(SID)
    assert len(creates(auto)) == 1


def test_wait_hook_noop_when_auto_off(auto):
    config.set_auto_on(False)
    clock = Clock()
    assert hooks.run("wait", prompt_input("x"), clock.now, clock.sleep) == ""
    assert auto() == [] and clock.sleeps == 0


def test_session_resume_reattaches_bound_chat(auto):
    saved_binding()
    run("session-resume", resume_input())
    assert state.is_enabled(SID)
    assert creates(auto) == []


@pytest.mark.parametrize("source", ["startup", "clear", "compact"])
def test_session_resume_ignores_other_sources(auto, source):
    saved_binding()
    clock = Clock()
    assert (
        hooks.run("session-resume", resume_input(source), clock.now, clock.sleep) == ""
    )
    assert not state.is_enabled(SID) and auto() == [] and clock.sleeps == 0


def test_session_resume_without_binding_does_not_create(auto):
    run("session-resume", resume_input())
    assert state.load_binding(SID) is None and auto() == []


def test_session_resume_noop_when_auto_off(auto):
    config.set_auto_on(False)
    saved_binding()
    run("session-resume", resume_input())
    assert not state.is_enabled(SID) and auto() == []


def test_session_start_keeps_enabled_of_current_process(auto):
    saved_binding()
    state.set_enabled(SID, True, os.getpid())
    run("session-start", resume_input())
    assert state.is_enabled(SID)


def test_session_start_removes_enabled_of_other_process(auto):
    saved_binding()
    state.set_enabled(SID, True, os.getppid())
    run("session-start", resume_input())
    assert not state.is_enabled(SID)


def test_session_start_without_claude_pid_disables(auto, monkeypatch):
    saved_binding()
    state.set_enabled(SID, True, os.getpid())
    monkeypatch.setattr(commands.procs, "find_claude_ancestor", lambda: None)
    run("session-start", resume_input())
    assert not state.is_enabled(SID)


def connected_first_by(monkeypatch, other):
    """本次 wait 的 auto_connect 拿到 connect_lock 之前，另一条连接路径（other）已完成开启。"""
    real = commands.auto_connect

    def connect(sid, source, prompt=""):
        assert other(real, sid) in ("created", "enabled")
        return real(sid, source, prompt)

    monkeypatch.setattr(commands, "auto_connect", connect)


def test_input_mirrored_when_concurrent_create_connects_first(
    auto, mirrors, monkeypatch
):
    """建群尚未完成时再次输入：本次 auto_connect 返回空，但已开启，本次输入仍镜像一次。"""
    connected_first_by(monkeypatch, lambda real, sid: real(sid, "prompt", "修 bug"))
    state.set_counter(SID, "keepalive_pending", 1)
    clock = Clock()
    hooks.run("prompt", prompt_input("再来一句"), clock.now, clock.sleep)  # 未开启
    out = hooks.run("wait", prompt_input("再来一句"), clock.now, clock.sleep)
    assert state.is_enabled(SID) and len(creates(auto)) == 1
    assert mirrors == ["再来一句"]
    assert state.get_counter(SID, "busy") == 1
    assert state.get_counter(SID, "keepalive_pending") == 0
    assert out == "" and clock.sleeps > 0  # 成为了等待进程


def test_input_mirrored_when_resume_connects_first(auto, mirrors, monkeypatch):
    """resume 接回期间立即输入：本次输入镜像一次，不建群。"""
    saved_binding()
    connected_first_by(monkeypatch, lambda real, sid: real(sid, "resume"))
    clock = Clock()
    hooks.run("prompt", prompt_input("接着干"), clock.now, clock.sleep)  # 未开启
    hooks.run("wait", prompt_input("接着干"), clock.now, clock.sleep)
    assert state.is_enabled(SID) and creates(auto) == []
    assert mirrors == ["接着干"]
    assert state.get_counter(SID, "busy") == 1


def test_notification_not_mirrored_when_other_path_connects_first(
    auto, mirrors, monkeypatch
):
    """另一条路径先开启时，后台通知仍按特殊 prompt 处理：不镜像，计数器不动。"""
    saved_binding()
    connected_first_by(monkeypatch, lambda real, sid: real(sid, "resume"))
    state.set_counter(SID, "keepalive_pending", 1)
    clock = Clock()
    hooks.run("wait", prompt_input("<task-notification>x"), clock.now, clock.sleep)
    assert state.is_enabled(SID) and mirrors == []
    assert state.get_counter(SID, "busy") == 0
    assert state.get_counter(SID, "keepalive_pending") == 1
