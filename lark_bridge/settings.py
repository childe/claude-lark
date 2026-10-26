"""~/.claude/settings.json 的合并与移除：只动本项目自己的条目。"""

import copy
import json
import os
import shutil
import time
from pathlib import Path

# 本项目条目的识别依据：command 里含这段路径（`~` 与 `$HOME` 两种写法都匹配）
HOOK_MARK = "claude-lark"
_PATH_MARK = "/.claude/lark/app/bin/claude-lark"
_BIN = "$HOME/.claude/lark/app/bin/claude-lark"

ENV_KEY = "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"
ENV_VALUE = "1000"

# (事件, matcher 或 None, hook 子命令, timeout)
_ENTRIES = [
    ("Stop", None, "stop", 3600),
    ("PermissionRequest", "*", "permission", 3600),
    # matcher 必须精确，更宽的 matcher 会自动放行任意工具
    ("PreToolUse", "AskUserQuestion", "ask", 3600),
    ("UserPromptSubmit", None, "prompt", 30),
]


class SettingsError(Exception):
    pass


def _is_ours(hook) -> bool:
    return isinstance(hook, dict) and _PATH_MARK in str(hook.get("command", ""))


def _strip(cfg: dict) -> bool:
    """原地移除本项目的 hook 条目；返回是否移除过东西。"""
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        return False
    removed = False
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(inner, list):
                kept_groups.append(g)
                continue
            kept = [h for h in inner if not _is_ours(h)]
            if len(kept) == len(inner):
                kept_groups.append(g)
                continue
            removed = True
            if kept:
                g["hooks"] = kept
                kept_groups.append(g)
        if kept_groups:
            hooks[event] = kept_groups
        elif len(kept_groups) != len(groups):
            del hooks[event]
    if removed and not hooks:
        del cfg["hooks"]
    return removed


def merge(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    _strip(out)
    hooks = out.setdefault("hooks", {})
    for event, matcher, sub, timeout in _ENTRIES:
        group = {}
        if matcher is not None:
            group["matcher"] = matcher
        group["hooks"] = [
            {
                "type": "command",
                "command": f"{_BIN} hook {sub}",
                "timeout": timeout,
            }
        ]
        hooks.setdefault(event, []).append(group)
    env = out.setdefault("env", {})
    env[ENV_KEY] = ENV_VALUE
    return out


def remove(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    _strip(out)
    env = out.get("env")
    if isinstance(env, dict) and ENV_KEY in env:
        del env[ENV_KEY]
        if not env:
            del out["env"]
    return out


def has_hooks(cfg: dict) -> bool:
    """四个事件是否都已注册本项目的 hook。"""
    hooks = cfg.get("hooks") if isinstance(cfg, dict) else None
    if not isinstance(hooks, dict):
        return False
    for event, _m, sub, _t in _ENTRIES:
        found = False
        for g in hooks.get(event) or []:
            for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
                if _is_ours(h) and str(h.get("command", "")).endswith(f"hook {sub}"):
                    found = True
        if not found:
            return False
    return True


def settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def load(path: Path) -> dict:
    """读 settings.json；不存在当 {}；不是合法 JSON 对象则抛 SettingsError。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise SettingsError(f"{path} 不是合法的 JSON（{e}），请先修好再重试")
    if not isinstance(data, dict):
        raise SettingsError(f"{path} 的顶层不是 JSON 对象，请先修好再重试")
    return data


def apply(action: str, path=None) -> str:
    """读 → 备份 → merge/remove → 写回。返回给用户看的一行说明。
    出错（含 JSON 非法）时不改动任何文件。"""
    path = Path(path) if path else settings_path()
    fn = {"merge": merge, "remove": remove}[action]
    cfg = load(path)
    new = fn(cfg)
    if path.exists():
        if new == cfg:
            return f"{path} 无需改动"
        bak = path.with_name(f"{path.name}.bak.{time.strftime('%Y%m%d%H%M%S')}")
        shutil.copy2(path, bak)
        note = f"（已备份到 {bak}）"
    else:
        if action == "remove":
            return f"{path} 不存在，无需改动"
        path.parent.mkdir(parents=True, exist_ok=True)
        note = "（新建）"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", "utf-8")
    os.replace(tmp, path)
    return f"已更新 {path} {note}"
