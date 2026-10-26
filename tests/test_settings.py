import json
import copy

from lark_bridge import settings

MARK = "/.claude/lark/app/bin/claude-lark"


def _ours(cfg):
    out = []
    for groups in (cfg.get("hooks") or {}).values():
        for g in groups:
            for h in g.get("hooks", []):
                if MARK in h.get("command", ""):
                    out.append(h)
    return out


def _foreign():
    return {
        "model": "opus",
        "env": {"FOO": "1"},
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo other-stop"}]}],
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [{"type": "command", "command": "sh superpowers.sh"}],
                }
            ],
        },
        "permissions": {"allow": ["Bash(ls:*)"]},
    }


def test_merge_into_empty():
    cfg = settings.merge({})
    hooks = cfg["hooks"]
    assert set(hooks) == {"Stop", "PermissionRequest", "PreToolUse", "UserPromptSubmit"}
    assert hooks["PermissionRequest"][0]["matcher"] == "*"
    assert "matcher" not in hooks["Stop"][0]
    assert "matcher" not in hooks["UserPromptSubmit"][0]
    stop = hooks["Stop"][0]["hooks"][0]
    assert stop == {
        "type": "command",
        "command": "$HOME/.claude/lark/app/bin/claude-lark hook stop",
        "timeout": 3600,
    }
    assert hooks["UserPromptSubmit"][0]["hooks"][0]["timeout"] == 30
    assert hooks["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("hook prompt")
    assert hooks["PermissionRequest"][0]["hooks"][0]["command"].endswith(
        "hook permission"
    )
    assert cfg["env"] == {"CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "1000"}
    assert len(_ours(cfg)) == 4


def test_ask_matcher_is_exactly_ask_user_question():
    pre = settings.merge({})["hooks"]["PreToolUse"]
    assert len(pre) == 1
    assert pre[0]["matcher"] == "AskUserQuestion"
    assert pre[0]["hooks"][0]["command"].endswith("hook ask")


def test_merge_idempotent():
    once = settings.merge(_foreign())
    twice = settings.merge(copy.deepcopy(once))
    assert once == twice
    assert len(_ours(twice)) == 4


def test_merge_does_not_mutate_input():
    src = _foreign()
    snap = copy.deepcopy(src)
    settings.merge(src)
    assert src == snap


def test_merge_preserves_foreign_hooks():
    cfg = settings.merge(_foreign())
    assert cfg["hooks"]["SessionStart"] == _foreign()["hooks"]["SessionStart"]
    stop = cfg["hooks"]["Stop"]
    assert stop[0] == _foreign()["hooks"]["Stop"][0]
    assert len(stop) == 2
    assert cfg["env"]["FOO"] == "1"
    assert cfg["model"] == "opus"
    assert cfg["permissions"] == {"allow": ["Bash(ls:*)"]}


def test_merge_replaces_tilde_spelling():
    old = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "~/.claude/lark/app/bin/claude-lark hook stop",
                            "timeout": 1,
                        }
                    ]
                }
            ]
        }
    }
    cfg = settings.merge(old)
    assert len(_ours(cfg)) == 4
    assert cfg["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 3600


def test_remove_only_ours():
    cfg = settings.remove(settings.merge(_foreign()))
    assert cfg == _foreign()


def test_remove_keeps_foreign_hook_sharing_a_group():
    cfg = settings.merge({})
    cfg["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "echo hi"})
    out = settings.remove(cfg)
    assert out["hooks"] == {
        "Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]
    }


def test_remove_cleans_empty_groups():
    out = settings.remove(settings.merge({}))
    assert out == {}


def test_remove_env_only_our_key():
    cfg = settings.merge({"env": {"FOO": "1"}})
    assert settings.remove(cfg)["env"] == {"FOO": "1"}


def test_remove_without_hooks_or_env_is_noop():
    assert settings.remove({"model": "x"}) == {"model": "x"}


def test_merge_then_remove_roundtrip():
    for orig in ({}, _foreign(), {"env": {"A": "b"}}):
        assert settings.remove(settings.merge(copy.deepcopy(orig))) == orig


def test_has_hooks():
    assert not settings.has_hooks({})
    assert settings.has_hooks(settings.merge({}))
    partial = settings.merge({})
    del partial["hooks"]["Stop"]
    assert not settings.has_hooks(partial)


def test_apply_rejects_invalid_json_and_changes_nothing(tmp_path):
    import pytest

    p = tmp_path / "settings.json"
    p.write_text("{ not json")
    with pytest.raises(settings.SettingsError):
        settings.apply("merge", p)
    assert p.read_text() == "{ not json"
    assert [f.name for f in tmp_path.iterdir()] == ["settings.json"]


def test_apply_creates_file_and_format(tmp_path):
    p = tmp_path / ".claude" / "settings.json"
    settings.apply("merge", p)
    text = p.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert text == json.dumps(settings.merge({}), indent=2, ensure_ascii=False) + "\n"


def test_apply_backup_and_idempotent_no_second_backup(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"note": "中文"}, ensure_ascii=False))
    settings.apply("merge", p)
    first = p.read_text(encoding="utf-8")
    assert "中文" in first
    settings.apply("merge", p)
    assert p.read_text(encoding="utf-8") == first
    baks = [f for f in tmp_path.iterdir() if ".bak." in f.name]
    assert len(baks) == 1
    assert json.loads(baks[0].read_text()) == {"note": "中文"}


def _doctor(monkeypatch, tmp_path, lark_home, fake_cli=True, **env):
    import os

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    if fake_cli:
        fake = os.path.join(os.path.dirname(__file__), "fake_lark_cli.py")
        monkeypatch.setenv("CLAUDE_LARK_CLI", fake)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from lark_bridge import cli

    return home, cli


def _install_fake(home):
    (home / ".claude" / "commands").mkdir(parents=True)
    (home / ".claude" / "commands" / "lark.md").write_text("x")
    settings.apply("merge", home / ".claude" / "settings.json")


def test_doctor_all_ok(monkeypatch, tmp_path, lark_home, capsys):
    home, cli = _doctor(monkeypatch, tmp_path, lark_home)
    _install_fake(home)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "❌" not in out
    assert "✅" in out


def test_doctor_reports_missing_install(monkeypatch, tmp_path, lark_home, capsys):
    home, cli = _doctor(monkeypatch, tmp_path, lark_home)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "❌" in out and "install.sh" in out


def test_doctor_precondition_failure(monkeypatch, tmp_path, lark_home, capsys):
    home, cli = _doctor(monkeypatch, tmp_path, lark_home, FAKE_LARK_PRECOND="missing")
    _install_fake(home)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "console_event_published" in out


def test_doctor_owner_mismatch(monkeypatch, tmp_path, lark_home, capsys):
    home, cli = _doctor(monkeypatch, tmp_path, lark_home)
    _install_fake(home)
    (lark_home / "config.json").write_text(json.dumps({"owner_open_id": "ou_test_x"}))
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "owner" in out


def test_doctor_missing_cli(monkeypatch, tmp_path, lark_home, capsys):
    home, cli = _doctor(
        monkeypatch,
        tmp_path,
        lark_home,
        fake_cli=False,
        CLAUDE_LARK_CLI=str(tmp_path / "nope"),
    )
    _install_fake(home)
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "npm i -g @larksuite/cli" in out


def test_doctor_never_writes_to_feishu(monkeypatch, tmp_path, lark_home, capsys):
    log = tmp_path / "fake.log"
    home, cli = _doctor(monkeypatch, tmp_path, lark_home, FAKE_LARK_LOG=str(log))
    _install_fake(home)
    cli.main(["doctor"])
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    for argv in calls:
        assert (
            argv == ["--version"]
            or argv[:2] == ["auth", "status"]
            or (argv[:2] == ["event", "consume"] and "--dry-run" in argv)
        ), argv
