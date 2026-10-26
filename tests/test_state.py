import os

from lark_bridge import procs, state

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
