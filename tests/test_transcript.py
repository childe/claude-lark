import json

from lark_bridge.transcript import ToolWatch

BASH = {"command": "ls", "description": "List"}


def use(tid, name="Bash", inp=None):
    content = [{"type": "tool_use", "id": tid, "name": name, "input": inp or BASH}]
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def result(tid):
    content = [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]
    return {"type": "user", "message": {"role": "user", "content": content}}


def write(p, *entries, partial=""):
    with open(p, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
        f.write(partial)


def test_resolved_when_result_appears(tmp_path):
    p = tmp_path / "t.jsonl"
    write(p, use("a"))
    w = ToolWatch(str(p), "Bash", BASH)
    assert not w.resolved()
    assert not w.resolved()
    write(p, result("a"))
    assert w.resolved()


def test_tool_use_written_after_watch_started(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("")
    w = ToolWatch(str(p), "Bash", BASH)
    assert not w.resolved()
    write(p, use("a"))
    assert not w.resolved()
    write(p, result("a"))
    assert w.resolved()


def test_earlier_identical_call_with_result_does_not_count(tmp_path):
    p = tmp_path / "t.jsonl"
    write(p, use("old"), result("old"), use("new"))
    w = ToolWatch(str(p), "Bash", BASH)
    assert not w.resolved()
    assert not w.resolved()
    write(p, result("new"))
    assert w.resolved()


def test_other_tool_results_do_not_count(tmp_path):
    p = tmp_path / "t.jsonl"
    write(p, use("a"), use("b", inp={"command": "pwd"}))
    w = ToolWatch(str(p), "Bash", BASH)
    assert not w.resolved()
    write(p, result("b"), use("c", name="Read", inp=BASH), result("c"))
    assert not w.resolved()


def test_partial_line_is_kept_until_complete(tmp_path):
    p = tmp_path / "t.jsonl"
    write(p, use("a"))
    w = ToolWatch(str(p), "Bash", BASH)
    assert not w.resolved()
    line = json.dumps(result("a")) + "\n"
    write(p, partial=line[:20])
    assert not w.resolved()
    write(p, partial=line[20:])
    assert w.resolved()


def test_missing_or_bad_path_never_resolves(tmp_path):
    for path in (None, "", str(tmp_path / "nope.jsonl"), 42):
        w = ToolWatch(path, "Bash", BASH)
        assert not w.resolved()
        assert not w.resolved()


def test_large_transcript_reads_only_the_tail(tmp_path, monkeypatch):
    from lark_bridge import transcript

    monkeypatch.setattr(transcript, "TAIL_BYTES", 200)
    p = tmp_path / "t.jsonl"
    write(p, *[result(f"x{i}") for i in range(50)], use("a"))
    w = ToolWatch(str(p), "Bash", BASH)
    assert not w.resolved()
    write(p, result("a"))
    assert w.resolved()
