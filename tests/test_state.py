import json
import os
import threading

import pytest

from lark_bridge import paths, procs, state

SID = "00000000-0000-0000-0000-000000000001"
SID2 = "00000000-0000-0000-0000-000000000002"


def mk(sid=SID, pid=None, chat="oc_test_chat"):
    return state.Binding(sid, chat, "name", "/work", pid, 1.0)


def test_binding_roundtrip(lark_home):
    assert state.load_binding(SID) is None
    b = mk(pid=123)
    state.save_binding(b)
    assert state.load_binding(SID) == b
    assert state.all_bindings() == [b]


def test_enable_toggle(lark_home):
    state.save_binding(mk())
    assert not state.is_enabled(SID)
    state.set_enabled(SID, True)
    assert state.is_enabled(SID)
    state.set_enabled(SID, False)
    assert not state.is_enabled(SID)


def test_enabled_false_when_claude_pid_dead(lark_home):
    state.set_enabled(SID, True, 999999)
    f = lark_home / "sessions" / SID / "enabled"
    assert f.exists()
    assert state.is_enabled(SID) is False
    assert not f.exists()


def test_inbox_dedup_by_message_id(lark_home):
    m = {"message_id": "om_test_1", "text": "hi", "create_time": 1}
    state.append_inbox(SID, m)
    state.append_inbox(SID, m)
    state.append_inbox(SID, {**m, "message_id": "om_test_2"})
    msgs, _ = state.peek_inbox(SID)
    assert [x["message_id"] for x in msgs] == ["om_test_1", "om_test_2"]


def test_cursor_not_advanced_without_commit(lark_home):
    state.append_inbox(SID, {"message_id": "om_test_1", "text": "a", "create_time": 1})
    a, off = state.peek_inbox(SID)
    b, off2 = state.peek_inbox(SID)
    assert a == b and len(a) == 1 and off == off2
    state.commit_cursor(SID, off)
    assert state.peek_inbox(SID)[0] == []


def test_peek_ignores_partial_trailing_line(lark_home):
    state.append_inbox(SID, {"message_id": "om_test_1", "text": "a", "create_time": 1})
    p = lark_home / "sessions" / SID / "inbox.jsonl"
    full = p.stat().st_size
    with open(p, "a") as f:
        f.write('{"message_id": "om_test_2", "te')
    msgs, off = state.peek_inbox(SID)
    assert len(msgs) == 1 and off == full


def test_chat_occupant_ignores_dead_pid(lark_home):
    state.save_binding(mk(pid=999999))
    state.set_enabled(SID, True, 999999)
    assert state.chat_occupant("oc_test_chat", SID2) is None


def test_chat_occupant_returns_live(lark_home):
    b = mk(pid=os.getpid())
    state.save_binding(b)
    state.set_enabled(SID, True, os.getpid())
    assert state.chat_occupant("oc_test_chat", SID2) == b
    assert state.chat_occupant("oc_test_chat", SID) is None
    assert state.find_enabled_by_chat("oc_test_chat") == b


def test_counters(lark_home):
    assert state.get_counter(SID, "blocks") == 0
    state.set_counter(SID, "blocks", 5)
    assert state.get_counter(SID, "blocks") == 5


def test_procs():
    assert procs.pid_alive(os.getpid())
    assert not procs.pid_alive(None)
    assert not procs.pid_alive(999999)
    r = procs.find_claude_ancestor()
    assert r is None or isinstance(r, int)


def test_append_repairs_partial_trailing_line(lark_home):
    state.append_inbox(SID, {"message_id": "om_test_1", "text": "a", "create_time": 1})
    p = lark_home / "sessions" / SID / "inbox.jsonl"
    with open(p, "a") as f:
        f.write('{"message_id": "om_test_2", "te')
    state.append_inbox(SID, {"message_id": "om_test_3", "text": "c", "create_time": 3})
    msgs, _ = state.peek_inbox(SID)
    assert [m["message_id"] for m in msgs] == ["om_test_1", "om_test_3"]


def _add(i, text=None):
    state.append_inbox(
        SID, {"message_id": f"om_test_{i}", "text": text or f"m{i}", "create_time": i}
    )


def _texts(**kw):
    return [m["text"] for m in state.peek_inbox(SID, **kw)[0]]


def test_peek_since_skips_earlier_lines(lark_home):
    _add(1)
    boundary = state.inbox_size(SID)
    _add(2)
    assert _texts() == ["m1", "m2"]
    assert _texts(since=boundary) == ["m2"]
    assert state.inbox_size("00000000-0000-0000-0000-00000000dead") == 0


def test_consume_non_prefix_then_commit_prunes(lark_home):
    for i in (1, 2, 3, 4):
        _add(i)
    p = lark_home / "sessions" / SID / "consumed.txt"
    state.consume(SID, ["om_test_2", "om_test_4"])
    assert _texts() == ["m1", "m3"]
    assert state._cursor(SID) == 0  # m1 not consumed: cursor can't move
    _, off = state.peek_inbox(SID)
    state.commit_cursor(SID, off)  # Stop delivered m1, m3
    assert _texts() == []
    assert not p.exists()  # all consumed ids passed by the cursor


def test_consume_prefix_advances_cursor(lark_home):
    for i in (1, 2, 3):
        _add(i)
    with open(lark_home / "sessions" / SID / "inbox.jsonl", "ab") as f:
        f.write(b"not json\n")
    _add(4)
    state.consume(SID, ["om_test_1", "om_test_2"])
    assert _texts() == ["m3", "m4"]
    first_end = len(
        (lark_home / "sessions" / SID / "inbox.jsonl").read_bytes().split(b"\n")[0]
    )
    assert state._cursor(SID) > first_end
    state.consume(SID, ["om_test_3"])  # also skips the garbage line
    assert _texts() == ["m4"]
    assert state._consumed(SID) == set()
    state.consume(SID, ["om_test_4"])
    size = state.inbox_size(SID)
    assert state._cursor(SID) == size and _texts() == []


def test_consume_non_string_id(lark_home):
    state.append_inbox(SID, {"message_id": 7, "text": "x", "create_time": 1})
    state.consume(SID, [7])
    assert _texts() == []


# ---------------- final review: pid reuse


def test_set_enabled_records_claude_start_time(lark_home):
    state.set_enabled(SID, True, os.getpid())
    content = (lark_home / "sessions" / SID / "enabled").read_text()
    pid, start = content.split("\n", 1)
    assert int(pid) == os.getpid()
    assert start.strip() and start.strip() == procs.start_time(os.getpid())
    assert state.is_enabled(SID)


def test_enabled_false_when_pid_reused_by_another_process(lark_home):
    """重启后旧 claude 的 pid 被别的进程占用：pid 活着但启动时间对不上。"""
    f = lark_home / "sessions" / SID / "enabled"
    f.parent.mkdir(parents=True)
    f.write_text(f"{os.getpid()}\nThu Jan  1 00:00:00 1970")
    assert state.is_enabled(SID) is False
    assert not f.exists()


def test_enabled_old_pid_only_format_is_not_enabled(lark_home):
    f = lark_home / "sessions" / SID / "enabled"
    f.parent.mkdir(parents=True)
    f.write_text(str(os.getpid()))
    assert state.is_enabled(SID) is False


def test_start_time_of_dead_pid_is_empty():
    assert procs.start_time(999999) == ""
    assert procs.start_time(None) == ""


@pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="needs /proc")
def test_start_time_uses_proc_ticks_immune_to_clock_changes(lark_home, monkeypatch):
    """调系统时间会让 ps lstart 整秒漂移；/proc 的启动滴答数不受影响。"""
    st = procs.start_time(os.getpid())
    assert st.startswith("ticks:") and st[len("ticks:") :].isdigit()
    state.set_enabled(SID, True, os.getpid())
    monkeypatch.setattr(procs, "lstart", lambda pid: "Thu Jan  1 00:00:01 1970")
    assert state.is_enabled(SID)


def test_enabled_legacy_lstart_format_still_matches(lark_home):
    """升级前写下的 lstart 格式仍按 lstart 比，不会一升级就断开。"""
    f = lark_home / "sessions" / SID / "enabled"
    f.parent.mkdir(parents=True)
    f.write_text(f"{os.getpid()}\n{procs.lstart(os.getpid())}")
    assert state.is_enabled(SID)


def test_enabled_false_when_proc_ticks_differ(lark_home):
    f = lark_home / "sessions" / SID / "enabled"
    f.parent.mkdir(parents=True)
    f.write_text(f"{os.getpid()}\nticks:1")
    assert state.is_enabled(SID) is False
    assert not f.exists()
    log = (lark_home / "errors.log").read_text()
    assert f"session {SID} disabled: claude pid {os.getpid()} start time changed" in log


# ---------------- binding sync fields, locks, auto_pending


def test_legacy_binding_defaults(lark_home):
    p = paths.session_dir(SID) / "binding.json"
    p.parent.mkdir(parents=True)
    legacy = {
        "session_id": SID,
        "chat_id": "oc_x",
        "chat_name": "群",
        "cwd": "/w",
        "claude_pid": 1,
        "created_at": 0,
    }
    p.write_text(json.dumps(legacy))
    b = state.load_binding(SID)
    assert b.created_by_bridge is True and b.synced_name is None
    p.write_text(json.dumps({**legacy, "chat_name": "oc_x"}))
    assert state.load_binding(SID).created_by_bridge is False


def test_new_fields_roundtrip(lark_home):
    b = state.Binding(SID, "oc_x", "oc_x", "/w", 1, 0.0, True, "n")
    state.save_binding(b)
    assert state.load_binding(SID) == b


def test_update_binding_rereads(lark_home):
    state.save_binding(mk(pid=1))
    stale = state.load_binding(SID)
    state.update_binding(SID, lambda b: setattr(b, "claude_pid", 2))
    state.update_binding(SID, lambda b: setattr(b, "synced_name", "n"))
    b = state.load_binding(SID)
    assert (b.claude_pid, b.synced_name) == (2, "n") and stale.claude_pid == 1


def test_update_binding_missing_without_default(lark_home):
    assert state.update_binding(SID, lambda b: None) is None
    assert state.load_binding(SID) is None
    assert (
        state.update_binding(SID, lambda b: None, default=mk()).chat_id
        == "oc_test_chat"
    )
    assert state.load_binding(SID).chat_id == "oc_test_chat"


def test_disable_if_foreign_keeps_own(lark_home):
    me = os.getpid()
    state.set_enabled(SID, True, me)
    assert state.disable_if_foreign(SID, me, procs.start_time(me)) is False
    assert state.is_enabled(SID)


def test_disable_if_foreign_removes_other(lark_home):
    state.set_enabled(SID, True, os.getppid())
    me = os.getpid()
    assert state.disable_if_foreign(SID, me, procs.start_time(me)) is True
    assert not (paths.session_dir(SID) / "enabled").exists()


def test_disable_if_foreign_missing_file(lark_home):
    me = os.getpid()
    assert state.disable_if_foreign(SID, me, procs.start_time(me)) is False


def test_disable_if_foreign_interleaved_with_enable(lark_home, monkeypatch):
    """清理读到旧值后暂停；接回写入当前进程的值必须等清理结束。"""
    me = os.getpid()
    state.set_enabled(SID, True, os.getppid())  # 旧进程留下的
    paused, go = threading.Event(), threading.Event()

    def hook():
        paused.set()
        go.wait(5)

    monkeypatch.setattr(state, "_after_foreign_read", hook)
    t = threading.Thread(
        target=state.disable_if_foreign, args=(SID, me, procs.start_time(me))
    )
    t.start()
    assert paused.wait(5)
    w = threading.Thread(target=state.set_enabled, args=(SID, True, me))
    w.start()
    w.join(0.3)
    assert w.is_alive()  # 被 enabled.lock 挡住
    go.set()
    t.join(5)
    w.join(5)
    content = (paths.session_dir(SID) / "enabled").read_text()
    assert content.split("\n")[0] == str(me) and state.is_enabled(SID)


def test_is_enabled_stale_delete_spares_rewritten_file(lark_home, monkeypatch):
    """判定失效后、删除之前文件被重写：不能删掉新内容。"""
    f = paths.session_dir(SID) / "enabled"
    f.parent.mkdir(parents=True)
    f.write_text("999999\nticks:1")  # 已退出的旧进程
    real = procs.pid_alive

    def alive(pid):
        r = real(pid)
        if pid == 999999:
            state.set_enabled(SID, True, os.getpid())
        return r

    monkeypatch.setattr(state.procs, "pid_alive", alive)
    assert state.is_enabled(SID) is False
    monkeypatch.setattr(state.procs, "pid_alive", real)
    assert state.is_enabled(SID)


def test_auto_pending_marker(lark_home):
    assert not state.has_auto_pending(SID)
    state.mark_auto_pending(SID)
    assert state.has_auto_pending(SID)
    state.clear_auto_pending(SID)
    state.clear_auto_pending(SID)
    assert not state.has_auto_pending(SID)


def test_connect_lock_serializes(lark_home):
    order = []
    entered = threading.Event()

    def holder():
        with state.connect_lock(SID):
            entered.set()
            threading.Event().wait(0.2)
            order.append("a")

    t = threading.Thread(target=holder)
    t.start()
    assert entered.wait(5)
    with state.connect_lock(SID):
        order.append("b")
    t.join(5)
    assert order == ["a", "b"]
