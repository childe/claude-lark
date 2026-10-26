"""/lark 命令：开启、关闭、查看桥接。返回给 Claude 看的文本，首行是协议标记。"""

import json
import os
import re
import time

from . import feishu, listener, paths, procs, state

NEED_NAME = "NEED_NAME"
HINT = "离开前就绪；回到电脑前按 Esc 拿回终端，/lark off 停止同步"
MAX_NAME = 60
_LINK_RE = re.compile(r"https?://[^\s/]*feishu\.cn/\S*")


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


def _owner() -> str:
    try:
        oid = json.loads(paths.config_file().read_text()).get("owner_open_id")
    except (OSError, ValueError, AttributeError):
        oid = None
    if isinstance(oid, str) and oid:
        return oid
    try:
        return feishu.owner_open_id()
    except feishu.FeishuError:
        raise RuntimeError(
            "找不到绑定者的 open_id，请运行安装脚本或 claude-lark doctor"
        )


def _is_chat_ref(args: str) -> bool:
    return (args.startswith("oc_") and not args.split()[1:]) or bool(
        _LINK_RE.fullmatch(args)
    )


def _slash(sid: str, args: str) -> str:
    args = (args or "").strip()
    if args == "off":
        return _off(sid)
    if args == "status":
        return _status(sid)
    b = state.load_binding(sid)
    if _is_chat_ref(args):
        if b:
            return _result(f"当前会话已绑定群 {b.chat_name}，先 /lark off 再换绑")
        if not args.startswith("oc_"):
            return _result(
                "无法从群链接解析出 chat_id，请直接提供以 oc_ 开头的 chat_id"
            )
        return _bind_existing(sid, args)
    if b:
        return _enable(sid, b, ignored=args)
    if not args:
        return NEED_NAME
    return _create_and_enable(sid, args)


def _on(sid: str, name: str) -> str:
    name = (name or "").strip()
    b = state.load_binding(sid)
    if b:
        return _enable(sid, b, ignored=name)
    if not name:
        return _result("群名不能为空")
    return _create_and_enable(sid, name)


def _off(sid: str) -> str:
    b = state.load_binding(sid)
    if not b:
        return _result("当前会话没有绑定群，无需关闭")
    state.set_enabled(sid, False)
    feishu.send(b.chat_id, "已断开，终端里敲 /lark 可重新连接")
    return _result(f"已关闭桥接（群 {b.chat_name} 仍保留绑定）")


def _status(sid: str) -> str:
    b = state.load_binding(sid)
    if not b:
        return _result("当前会话未绑定群，桥接未开启")
    on_ = "已开启" if state.is_enabled(sid) else "未开启"
    alive = "存活" if listener.is_running() else "未运行"
    return _result(f"绑定群 {b.chat_name}，桥接{on_}，监听进程{alive}")


def _occupied(b, sid) -> str:
    o = state.chat_occupant(b.chat_id, sid)
    if o:
        return _result(f"群已被会话 {o.session_id[:8]}（{o.cwd}）占用")
    return ""


def _create_and_enable(sid: str, name: str) -> str:
    owner = _owner()
    name = name[:MAX_NAME]
    chat_id = feishu.create_chat(name, owner)
    b = state.Binding(sid, chat_id, name, os.getcwd(), None, time.time())
    return _enable(sid, b)


def _bind_existing(sid: str, chat_id: str) -> str:
    _owner()
    if not feishu.bot_in_chat(chat_id):
        return _result(
            "机器人不在这个群里（或读不到群成员）：请先在飞书群设置里把机器人添加进群，再重试"
        )
    b = state.Binding(sid, chat_id, chat_id, os.getcwd(), None, time.time())
    return _enable(sid, b)


def _enable(sid: str, b, ignored: str = "") -> str:
    busy = _occupied(b, sid)
    if busy:
        return busy
    b.claude_pid = procs.find_claude_ancestor()
    b.cwd = os.getcwd()
    state.save_binding(b)
    state.set_enabled(sid, True, b.claude_pid)
    listener.ensure_running()
    sent = feishu.send(b.chat_id, f"已连接：{b.cwd}，会话 {sid[:8]}")
    msg = f"已开启桥接，群 {b.chat_name}。{HINT}"
    if ignored:
        msg += f"（已有绑定群 {b.chat_name}，忽略群名参数）"
    if not sent:
        msg += "（往群里发“已连接”消息失败，详见 errors.log）"
    return _result(msg)
