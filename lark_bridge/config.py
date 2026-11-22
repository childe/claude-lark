"""~/.claude/lark/config.json：owner_open_id 与 auto_on 开关。"""

import json

from . import paths, state


def load() -> dict:
    """读不到、解析失败或不是对象时返回 {}。"""
    try:
        data = json.loads(paths.config_file().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _set(key: str, value):
    cfg = load()
    cfg[key] = value
    state._atomic_write(
        paths.config_file(), json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
    )


def auto_on() -> bool:
    return load().get("auto_on") is True


def set_auto_on(on: bool) -> None:
    _set("auto_on", bool(on))


def set_owner(open_id: str) -> None:
    _set("owner_open_id", open_id)
