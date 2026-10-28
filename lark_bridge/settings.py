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

# 旧版本（同步阻塞的 Stop hook）写进 env 的条目；merge / remove 时清掉
ENV_KEY = "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"
ENV_VALUE = "1000"

# Stop hook 在后台等飞书消息，不占终端；exit 2 时唤醒模型（Claude Code 2.1.287 实测）
_ASYNC = {"asyncRewake": True}

# (事件, matcher 或 None, hook 子命令, timeout, 额外字段)
_ENTRIES = [
    ("Stop", None, "stop", 86400, _ASYNC),
    # AskUserQuestion 也走这里：选项框和飞书同时作答，谁先答算谁的
    ("PermissionRequest", "*", "permission", 3600, {}),
    ("UserPromptSubmit", None, "prompt", 30, {}),
    # 每个回合开始时补一个后台等待进程（已有就立刻退出），见 hooks._wait
    ("UserPromptSubmit", None, "wait", 86400, _ASYNC),
    # /clear、/resume、退出时断开桥接；不阻塞，超时给短一点
    ("SessionStart", None, "session-start", 10, {}),
    ("SessionEnd", None, "session-end", 10, {}),
]


class SettingsError(Exception):
    pass


def _validate(cfg) -> None:
    """形状不对（用户手改过）就抛 SettingsError，让调用方干净地中止。"""
    if not isinstance(cfg, dict):
        raise SettingsError("settings.json 的顶层不是 JSON 对象")
    env = cfg.get("env")
    if env is not None and not isinstance(env, dict):
        raise SettingsError('settings.json 里的 "env" 不是对象，请先修好再重试')
    hooks = cfg.get("hooks")
    if hooks is None:
        return
    if not isinstance(hooks, dict):
        raise SettingsError('settings.json 里的 "hooks" 不是对象，请先修好再重试')
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise SettingsError(
                f"settings.json 里 hooks.{event} 不是数组，请先修好再重试"
            )
        for g in groups:
            if not isinstance(g, dict) or not isinstance(g.get("hooks", []), list):
                raise SettingsError(
                    f"settings.json 里 hooks.{event} 的条目格式不对，请先修好再重试"
                )


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
    _validate(cfg)
    out = copy.deepcopy(cfg)
    _strip(out)
    hooks = out.setdefault("hooks", {})
    for event, matcher, sub, timeout, extra in _ENTRIES:
        group = {}
        if matcher is not None:
            group["matcher"] = matcher
        group["hooks"] = [
            {
                "type": "command",
                "command": f"{_BIN} hook {sub}",
                "timeout": timeout,
                **extra,
            }
        ]
        hooks.setdefault(event, []).append(group)
    _strip_env(out)
    return out


def _strip_env(cfg: dict) -> None:
    """删掉旧版本写入的 env 条目（只删值和我们写的一样的）。"""
    env = cfg.get("env")
    if isinstance(env, dict) and env.get(ENV_KEY) == ENV_VALUE:
        del env[ENV_KEY]
        if not env:
            del cfg["env"]


def remove(cfg: dict) -> dict:
    _validate(cfg)
    out = copy.deepcopy(cfg)
    _strip(out)
    _strip_env(out)
    return out


def has_hooks(cfg: dict) -> bool:
    """所有事件是否都已注册本项目的 hook（含 asyncRewake 等字段，旧版本的配置算没有）。"""
    hooks = cfg.get("hooks") if isinstance(cfg, dict) else None
    if not isinstance(hooks, dict):
        return False
    for event, matcher, sub, _t, extra in _ENTRIES:
        found = False
        for g in hooks.get(event) or []:
            if not isinstance(g, dict) or g.get("matcher") != matcher:
                continue
            for h in g.get("hooks") or []:
                if (
                    _is_ours(h)
                    and str(h.get("command", "")).endswith(f"hook {sub}")
                    and all(h.get(k) == v for k, v in extra.items())
                ):
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
    出错（含 JSON 非法）时不改动任何文件。settings.json 是软链接时写到真实目标，
    保持软链接和文件权限不变。"""
    path = Path(path) if path else settings_path()
    fn = {"merge": merge, "remove": remove}[action]
    cfg = load(path)
    new = fn(cfg)
    target = path.resolve()
    if path.exists():
        if new == cfg:
            return f"{path} 无需改动"
        stamp = f"{time.strftime('%Y%m%d%H%M%S')}.{time.time_ns() % 10**9:09d}"
        bak = path.with_name(f"{path.name}.bak.{stamp}.{os.getpid()}")
        shutil.copy2(path, bak)
        note = f"（已备份到 {bak}）"
    else:
        if action == "remove":
            return f"{path} 不存在，无需改动"
        target.parent.mkdir(parents=True, exist_ok=True)
        note = "（新建）"
    tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", "utf-8")
        if target.exists():
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    return f"已更新 {path} {note}"
