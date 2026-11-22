"""/lark 命令：开启、关闭、查看桥接。返回给 Claude 看的文本，首行是协议标记。"""

import os
import re
import time

from . import config, feishu, listener, meta, procs, state

NEED_NAME = "NEED_NAME"
HINT = "在群里发消息就能继续这个会话，终端也照常可用；/lark off 停止同步"
MAX_NAME = 60
NO_CLAUDE = (
    "未能识别 claude 进程，无法开启桥接（会话退出后无法自动断开）。"
    "请把 `claude-lark doctor` 的输出反馈给开发者。"
)


def _result(text: str) -> str:
    return f"RESULT: {text}"


def _safe(where: str, fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        feishu.log_exception(where)
        reason = " ".join(f"{type(e).__name__} {e}".split())[:100]
        return _result(f"出错了：{reason}")


def slash(sid: str, args: str) -> str:
    return _safe("commands.slash", _slash, sid, args)


def on(sid: str, name: str) -> str:
    return _safe("commands.on", _on, sid, name)


NO_OWNER = "未找到 owner 配置，请重新运行 install.sh"
AUTO_ON_TEXT = "已开启自动连接：新会话发出第一句后自动建群并开启桥接"
AUTO_OFF_TEXT = "已关闭自动连接"


def _owner():
    """config.json 里的 owner_open_id；没有返回 None。

    不退回 auth status：监听进程只认 config.json，没有 owner 时它会丢弃所有消息，
    桥接开了也收不到东西。"""
    oid = config.load().get("owner_open_id")
    return oid if isinstance(oid, str) and oid else None


def _is_chat_ref(args: str) -> bool:
    return (args.startswith("oc_") and not args.split()[1:]) or _is_link(args)


def _is_link(args: str) -> bool:
    return args.startswith(("http://", "https://"))


def _slash(sid: str, args: str) -> str:
    args = (args or "").strip()
    if args == "off":
        return _off(sid)
    if args == "status":
        return _status(sid)
    if args == "auto on":
        config.set_auto_on(True)
        return _result(AUTO_ON_TEXT)
    if args == "auto off":
        config.set_auto_on(False)
        return _result(AUTO_OFF_TEXT)
    owner = _owner()
    if owner is None:
        return _result(NO_OWNER)
    pid = _claude_pid()
    with state.connect_lock(sid):
        b = state.load_binding(sid)
        if _is_chat_ref(args):
            if b:
                return _result(f"当前会话已绑定群 {b.chat_name}，先 /lark off 再换绑")
            if _is_link(args):
                return _result(
                    "无法从群链接解析出 chat_id，请直接提供以 oc_ 开头的 chat_id"
                )
            if pid is None:
                return _result(NO_CLAUDE)
            return _bind_existing(sid, args, pid)
        if b:
            if pid is None:
                return _result(NO_CLAUDE)
            return _enable(sid, b, ignored=args, pid=pid)
        if not args:
            return NEED_NAME
        if pid is None:
            return _result(NO_CLAUDE)
        return _create_and_enable(sid, args, owner, pid)


def _on(sid: str, name: str) -> str:
    name = (name or "").strip()
    owner = _owner()
    if owner is None:
        return _result(NO_OWNER)
    pid = _claude_pid()
    with state.connect_lock(sid):
        b = state.load_binding(sid)
        if b:
            if pid is None:
                return _result(NO_CLAUDE)
            return _enable(sid, b, ignored=name, pid=pid)
        if not name:
            return _result("群名不能为空")
        if pid is None:
            return _result(NO_CLAUDE)
        return _create_and_enable(sid, name, owner, pid)


def _off(sid: str) -> str:
    with state.connect_lock(sid):
        # 自动建群后开启失败留下的标记也清掉：/lark off 之后下一句不能再自动开启
        state.clear_auto_pending(sid)
        b = state.load_binding(sid)
        if not b:
            return _result("当前会话没有绑定群，无需关闭")
        was_on = state.is_enabled(sid)
        state.set_enabled(sid, False)
        if was_on:
            feishu.send(b.chat_id, "已断开，终端里敲 /lark 可重新连接")
        return _result(f"已关闭桥接（群 {b.chat_name} 仍保留绑定）")


def _status(sid: str) -> str:
    auto = f"；自动连接{'已开启' if config.auto_on() else '未开启'}"
    b = state.load_binding(sid)
    if not b:
        return _result("当前会话未绑定群，桥接未开启" + auto)
    on_ = "已开启" if state.is_enabled(sid) else "未开启"
    alive = "存活" if listener.is_running() else "未运行"
    return _result(f"绑定群 {b.chat_name}，桥接{on_}，监听进程{alive}" + auto)


def _occupied(b, sid) -> str:
    o = state.chat_occupant(b.chat_id, sid)
    if o:
        return _result(f"群已被会话 {o.session_id[:8]}（{o.cwd}）占用")
    return ""


def _claude_pid():
    pid = procs.find_claude_ancestor()
    if pid is None:
        feishu.log_error(f"find_claude_ancestor returned None (ppid={os.getppid()})")
    return pid


def _create_and_enable(sid: str, name: str, owner: str, pid: int, auto=False) -> str:
    """调用方持有 connect_lock。auto=True 时先记 auto_pending 再落 binding：
    开启失败时下一句只重试开启，不会再建一个群。"""
    name = name[:MAX_NAME]
    chat_id = feishu.create_chat(name, owner)
    b = state.Binding(
        sid,
        chat_id,
        name,
        os.getcwd(),
        pid,
        time.time(),
        created_by_bridge=True,
        synced_name=meta.session_name(pid, sid),
    )
    if auto:
        state.mark_auto_pending(sid)
    state.save_binding(b)  # 群已建出来，先落绑定，免得后面出错留下孤儿群
    return _enable(sid, b, pid=pid)


def _bind_existing(sid: str, chat_id: str, pid: int) -> str:
    b = state.Binding(
        sid,
        chat_id,
        chat_id,
        os.getcwd(),
        pid,
        time.time(),
        created_by_bridge=False,
        synced_name=meta.session_name(pid, sid),
    )
    busy = _occupied(b, sid)
    if busy:
        return busy
    if not feishu.bot_in_chat(chat_id):
        return _result(
            "机器人不在这个群里（或读不到群成员）：请先在飞书群设置里把机器人添加进群，再重试"
        )
    return _enable(sid, b, pid=pid)


def _enable(sid: str, b, ignored: str = "", pid=None) -> str:
    if pid is None:
        pid = _claude_pid()
        if pid is None:
            return _result(NO_CLAUDE)
    busy = _occupied(b, sid)
    if busy:
        return busy
    cwd = os.getcwd()

    def _update(x):
        x.claude_pid = pid
        x.cwd = cwd

    b = state.update_binding(sid, _update, default=b)
    state.set_enabled(sid, True, pid)
    if not state.is_enabled(sid):
        feishu.log_error(f"enable {sid}: start time of claude pid {pid} unreadable")
        return _result(
            f"读不到 claude 进程（pid {pid}）的启动时间，无法开启桥接；"
            "请把 `claude-lark doctor` 的输出反馈给开发者。"
        )
    if not listener.ensure_running():
        state.set_enabled(sid, False)
        feishu.log_error(f"enable {sid}: failed to spawn listener, bridge not enabled")
        return _result("拉起监听进程失败，未开启桥接，详见 errors.log")
    state.clear_auto_pending(sid)
    sent = feishu.send(b.chat_id, f"已连接：{b.cwd}，会话 {sid[:8]}")
    msg = f"已开启桥接，群 {b.chat_name}。{HINT}"
    if ignored:
        msg += f"（已有绑定群 {b.chat_name}，忽略群名参数）"
    if not sent:
        msg += "（往群里发“已连接”消息失败，详见 errors.log）"
    return _result(msg)


# ---------------- 自动连接

AUTO_NAME_MAX = 20
_SKIP_PREFIXES = ("/lark", "<task-notification>")


def auto_name(prompt: str, session_name: str, cwd: str) -> str:
    """自动建群的群名：prompt 第一行去首尾空白截到 20 字符；为空用会话名，
    再为空用 cwd 的目录名。"""
    lines = (prompt or "").splitlines()
    first = lines[0].strip() if lines else ""
    if first:
        return first[:AUTO_NAME_MAX]
    name = (session_name or "").strip()
    if name:
        return name
    return os.path.basename((cwd or "").rstrip("/")) or "claude"


def _opened(r: str, sid: str) -> bool:
    return r.startswith(_result("已开启桥接")) and state.is_enabled(sid)


def auto_connect(sid: str, source: str, prompt: str = "") -> str:
    """auto_on 时自动开启桥接。source 为 "prompt"（第一句之后）或 "resume"。

    返回 "created"（本次建群并开启）、"enabled"（复用已有 binding 开启）或 ""。
    自身不抛出。"""
    try:
        return _auto_connect(sid, source, prompt)
    except Exception:
        feishu.log_exception("commands.auto_connect")
        return ""


def _auto_connect(sid: str, source: str, prompt: str) -> str:
    if not config.auto_on():
        return ""
    if not isinstance(prompt, str):
        prompt = ""
    if source == "prompt" and prompt.lstrip().startswith(_SKIP_PREFIXES):
        return ""
    pid = _claude_pid()
    if pid is None or not meta.is_interactive(pid, sid):
        return ""
    owner = None
    if source == "prompt":
        owner = _owner()
        if owner is None:
            feishu.log_error(f"auto_connect {sid}: no owner_open_id in config.json")
            return ""
    with state.connect_lock(sid):
        b = state.load_binding(sid)
        if source == "prompt":
            if b is None:
                name = auto_name(prompt, meta.session_name(pid, sid), os.getcwd())
                r = _create_and_enable(sid, name, owner, pid, auto=True)
                return "created" if _opened(r, sid) else ""
            if not state.has_auto_pending(sid):
                return ""
            r = _enable(sid, b, pid=pid)
            return "enabled" if _opened(r, sid) else ""
        if source == "resume" and b is not None:
            r = _enable(sid, b, pid=pid)
            if _opened(r, sid):
                return "enabled"
            feishu.log_error(f"auto_connect resume {sid}: {r}")
    return ""
