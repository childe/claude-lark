import json

from lark_bridge import config


def test_auto_on_defaults_false(lark_home):
    assert config.auto_on() is False


def test_set_auto_on_preserves_other_fields(lark_home):
    config.set_auto_on(True)
    data = json.loads((lark_home / "config.json").read_text())
    assert data == {"owner_open_id": "ou_test_owner", "auto_on": True}
    config.set_auto_on(False)
    assert config.auto_on() is False
    assert config.load()["owner_open_id"] == "ou_test_owner"


def test_set_owner_keeps_auto_on(lark_home):
    config.set_auto_on(True)
    config.set_owner("ou_new")
    assert config.load() == {"owner_open_id": "ou_new", "auto_on": True}


def test_load_tolerates_garbage(lark_home):
    (lark_home / "config.json").write_text("[1,2]")
    assert config.load() == {} and config.auto_on() is False
    config.set_auto_on(True)
    assert config.load() == {"auto_on": True}


def test_load_missing_file(lark_home):
    (lark_home / "config.json").unlink()
    assert config.load() == {} and config.auto_on() is False
