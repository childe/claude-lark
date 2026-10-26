"""lark-cli 子进程封装。发消息一律 --as bot。"""

import json
import subprocess
import time
import traceback

from . import paths
from .text import split_message

TIMEOUT = 30


class FeishuError(Exception):
    pass


def log_error(msg: str) -> None:
    """追加一行到 home()/errors.log，自身绝不抛出。"""
    try:
        p = paths.home() / "errors.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(p, "a") as f:
            f.write(f"{stamp} {msg.rstrip()}\n")
    except Exception:
        pass


def log_exception(where: str) -> None:
    log_error(f"{where}: {traceback.format_exc()}")


def _run(args, timeout=TIMEOUT) -> str:
    """运行 lark-cli，返回 stdout；非 0 退出、超时、找不到可执行文件都抛 FeishuError。"""
    cmd = [paths.lark_cli(), *args]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise FeishuError(f"lark-cli {args[:2]} timed out after {timeout}s")
    except OSError as e:
        raise FeishuError(f"lark-cli {args[:2]} failed to start: {e}")
    if r.returncode != 0:
        raise FeishuError(
            f"lark-cli {args[:2]} exit {r.returncode}: {(r.stderr or r.stdout).strip()}"
        )
    return r.stdout


def to_ms(v):
    """飞书 create_time → 毫秒整数；取不到返回 None。

    事件里是毫秒字符串；万一某处给的是秒（< 1e11），按秒换算，保证两边可比。"""
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n * 1000 if n < 10**11 else n


def _sent_create_time(out: str):
    """+messages-send 输出里的 create_time（顶层或 data 下），取不到返回 None。"""
    try:
        data = json.loads(out)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    inner = data.get("data")
    ct = data.get("create_time")
    if ct is None and isinstance(inner, dict):
        ct = inner.get("create_time")
    return to_ms(ct)


def send_with_time(chat_id: str, markdown: str, timeout: float = TIMEOUT):
    """同 send，另外返回第一段消息在飞书服务端的 create_time（毫秒，取不到为 None）。

    返回 (是否全部发出, create_time)。"""
    first = None
    try:
        for i, part in enumerate(split_message(markdown)):
            out = _run(
                [
                    "im",
                    "+messages-send",
                    "--as",
                    "bot",
                    "--chat-id",
                    chat_id,
                    "--markdown",
                    part,
                ],
                timeout=timeout,
            )
            if i == 0:
                first = _sent_create_time(out)
        return True, first
    except FeishuError as e:
        log_error(f"send to {chat_id}: {e}")
    except Exception:
        log_exception(f"send to {chat_id}")
    return False, first


def send(chat_id: str, markdown: str, timeout: float = TIMEOUT) -> bool:
    """分段发送；任何失败只记 errors.log，不抛出。某段失败后不再发后续段。

    timeout 是每一段的 lark-cli 超时秒数。全部发出返回 True，否则 False。"""
    return send_with_time(chat_id, markdown, timeout)[0]


def _json(out: str) -> dict:
    try:
        data = json.loads(out)
    except ValueError as e:
        raise FeishuError(f"lark-cli output is not JSON: {out[:200]!r}") from e
    if not isinstance(data, dict):
        raise FeishuError(f"lark-cli output is not an object: {out[:200]!r}")
    return data


def create_chat(name: str, owner_open_id: str) -> str:
    out = _run(
        [
            "im",
            "+chat-create",
            "--as",
            "bot",
            "--name",
            name[:60],
            "--users",
            owner_open_id,
            "--set-bot-manager",
        ]
    )
    chat_id = (_json(out).get("data") or {}).get("chat_id")
    if not isinstance(chat_id, str) or not chat_id:
        raise FeishuError(f"+chat-create returned no chat_id: {out[:200]!r}")
    return chat_id


def owner_open_id() -> str:
    data = _json(_run(["auth", "status"]))
    oid = ((data.get("identities") or {}).get("user") or {}).get("openId")
    if not isinstance(oid, str) or not oid:
        raise FeishuError("auth status has no identities.user.openId")
    return oid


def bot_in_chat(chat_id: str) -> bool:
    """机器人能否读到该群的成员列表（不在群里时 lark-cli 会报错）。"""
    try:
        _run(["im", "+chat-members-list", "--as", "bot", "--chat-id", chat_id])
    except FeishuError as e:
        log_error(f"bot_in_chat {chat_id}: {e}")
        return False
    return True
