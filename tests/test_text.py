import json
from lark_bridge import text
from lark_bridge.text import (
    DELIVER_PREFIX,
    KEEPALIVE_REASON,
    format_permission,
    format_questions,
    parse_answers,
    parse_permission,
    split_message,
    strip_mentions,
)

Q1 = {
    "question": "Pick colors",
    "options": [
        {"label": "Red", "description": "warm"},
        {"label": "Blue", "description": "cool"},
    ],
    "multiSelect": False,
}
Q1_MULTI = dict(Q1, multiSelect=True)


def test_parse_permission_yes():
    for s in ["同意", " Y ", "1", "允许", "OK", "yes", "同意。", "yes!", "ok."]:
        assert parse_permission(s) is True, s


def test_parse_permission_no():
    for s in ["拒绝", "2", " N ", "no", "拒绝！"]:
        assert parse_permission(s) is False, s


def test_parse_permission_ambiguous_is_none():
    for s in ["好的吧", "嗯", "", "同意吗", "   ", "1 2", "不同意", "yes no", "。"]:
        assert parse_permission(s) is None, s


def test_parse_answers_index():
    assert parse_answers("2", [Q1]) == {"Pick colors": "Blue"}


def test_parse_answers_multiselect():
    assert parse_answers("1，2", [Q1_MULTI]) == {"Pick colors": "Red, Blue"}
    assert parse_answers("1、2", [Q1_MULTI]) == {"Pick colors": "Red, Blue"}
    assert parse_answers("1 2", [Q1_MULTI]) == {"Pick colors": "Red, Blue"}


def test_parse_answers_single_select_takes_first():
    assert parse_answers("2,1", [Q1]) == {"Pick colors": "Blue"}


def test_parse_answers_out_of_range_is_freetext():
    assert parse_answers("9", [Q1]) == {"Pick colors": "9"}
    assert parse_answers("0", [Q1]) == {"Pick colors": "0"}
    assert parse_answers("1,9", [Q1_MULTI]) == {"Pick colors": "1,9"}


def test_parse_answers_freetext():
    assert parse_answers("  green please ", [Q1]) == {"Pick colors": "green please"}


def test_parse_answers_multiple_questions_by_line():
    q2 = {"question": "Name?", "options": [], "multiSelect": False}
    q3 = {"question": "Third", "options": [], "multiSelect": False}
    got = parse_answers("1\nBob", [Q1, q2, q3])
    assert got == {"Pick colors": "Red", "Name?": "Bob", "Third": ""}


def test_split_message_short_untouched():
    assert split_message("hello", 4000) == ["hello"]


def test_split_message_prefers_newlines():
    t = "aaaaaaaa\nbbbbbbbb\ncccccccc"
    parts = split_message(t, 20)
    assert parts == ["aaaaaaaa\nbbbbbbbb", "cccccccc"]


def test_split_message_keeps_code_fences_balanced():
    body = "\n".join(f"line {i}" for i in range(300))
    t = "intro\n```\n" + body + "\n```\noutro"
    parts = split_message(t, 200)
    assert len(parts) > 1
    for p in parts:
        assert len(p) <= 200
        assert p.count("```") % 2 == 0
    joined = "\n".join(parts)
    assert "outro" in joined and "line 299" in joined


def test_split_message_long_line_and_limit():
    t = "x" * 1000 + "\n```\n" + "y" * 1000 + "\n```"
    parts = split_message(t, 100)
    assert all(len(p) <= 100 for p in parts)
    assert all(p.count("```") % 2 == 0 for p in parts)
    assert "".join(parts).count("x") == 1000
    assert "".join(parts).count("y") == 1000


def test_format_permission_bash_contains_command():
    s = format_permission("Bash", {"command": "ls -la", "description": "列目录"})
    assert "列目录" in s and "```\nls -la\n```" in s
    assert s.endswith("回复「同意」或「拒绝」（1 / 2）")


def test_format_permission_other_tool_truncates():
    s = format_permission("Write", {"content": "字" * 5000})
    assert "```" in s and "Write" in s
    assert len(s) < 1800
    assert s.endswith("回复「同意」或「拒绝」（1 / 2）")


def test_format_questions():
    s = format_questions([Q1])
    assert "Pick colors" in s
    assert "1. Red — warm" in s and "2. Blue — cool" in s
    assert "回复序号，多选用逗号分隔；也可以直接回复文字" in s


def test_strip_mentions():
    assert strip_mentions("@_user_1 你好") == "你好"
    assert strip_mentions("@_all 大家") == "大家"
    assert strip_mentions("hi @_all there") == "hi there"
    assert strip_mentions("@_user_1 @_user_22  go on ") == "go on"
    assert strip_mentions("@src/main.py 看下") == "@src/main.py 看下"
    assert strip_mentions("@claude-bot 你好") == "@claude-bot 你好"
    assert strip_mentions("@_allowed") == "@_allowed"
    assert strip_mentions("a@_user_1b") == "a@_user_1b"
    assert strip_mentions("plain") == "plain"


def test_constants():
    assert KEEPALIVE_REASON.startswith("（claude-lark 保活）")
    assert DELIVER_PREFIX == "以下是用户在飞书群里发来的消息：\n\n"
    assert text.JOIN_SEP == "\n\n---\n\n"


# ---------------- final review: fences around content with backticks


def _fence_lines(part):
    return [ln for ln in part.split("\n") if ln.startswith("```")]


def test_format_permission_fence_longer_than_backticks_in_command():
    cmd = "cat <<'EOF' > a.md\n```python\nprint(1)\n```\nEOF"
    s = format_permission("Bash", {"command": cmd, "description": "写文件"})
    assert "````\n" + cmd + "\n````" in s
    # 内容里的 ``` 不能提前关闭外层代码块：外层围栏只出现两次
    assert [ln for ln in s.split("\n") if ln == "````"] == ["````", "````"]
    s = format_permission("Bash", {"command": "echo `````x`````"})
    assert "``````\necho `````x`````\n``````" in s
    # 没有反引号时仍是 3 个
    assert "```\nls\n```" in format_permission("Bash", {"command": "ls"})


def test_format_permission_json_body_fence_longer_than_backticks():
    s = format_permission("Write", {"content": "a\n```\nb"})
    lines = s.split("\n")
    assert lines[1] == "````" and "````" in lines[2:]


def test_split_message_with_longer_fence_and_inner_backticks():
    inner = "\n".join(
        "```" if i % 7 == 0 else f"line {i} " + "z" * 40 for i in range(300)
    )
    t = format_permission("Bash", {"command": inner})
    assert len(t) > 4000
    parts = split_message(t)
    assert len(parts) > 1
    for i, p in enumerate(parts):
        assert len(p) <= 4000
        lines = p.split("\n")
        if i > 0:
            assert lines[0] == "````", lines[0]
        if i < len(parts) - 1:
            assert lines[-1] == "````", lines[-1]
        # 每段里 ```` 围栏成对
        assert lines.count("````") % 2 == 0
    joined = "\n".join(parts)
    assert joined.count("z" * 40) == inner.count("z" * 40)
    assert parts[-1].endswith("回复「同意」或「拒绝」（1 / 2）")


def test_render_content_text_and_post():
    from lark_bridge.text import render_content

    assert render_content("text", json.dumps({"text": "@_user_1 hi"})) == "@_user_1 hi"
    post = {
        "title": "",
        "content": [
            [
                {"tag": "at", "user_id": "@_user_1", "user_name": "bot"},
                {"tag": "text", "text": "看 "},
                {"tag": "a", "text": "链接", "href": "https://example.com"},
            ],
            [{"tag": "img", "image_key": "img_x"}, {"tag": "text", "text": "第二行"}],
        ],
    }
    assert render_content("post", json.dumps(post)) == "看 链接\n第二行"
    assert render_content("post", json.dumps({"zh_cn": post})) == "看 链接\n第二行"
    assert render_content("image", json.dumps({"image_key": "x"})) == ""
    assert render_content("text", "not json") == ""
    assert render_content("text", None) == ""
