import json

import pytest


@pytest.fixture
def lark_home(tmp_path, monkeypatch):
    home = tmp_path / "lark_home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_LARK_HOME", str(home))
    (home / "config.json").write_text(json.dumps({"owner_open_id": "ou_test_owner"}))
    return home
