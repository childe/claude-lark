"""lark-cli 子进程封装。发消息一律 --as bot。"""

import json
import os
import signal
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


def _kill_group(p) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass


def _run(args, timeout=TIMEOUT) -> str:
    """运行 lark-cli，返回 stdout；非 0 退出、超时、找不到可执行文件都抛 FeishuError。

    lark-cli 是 node 包装脚本再拉起真正的二进制：放进独立进程组，超时时整组杀掉，
    否则孙进程攥着输出管道，读取会一直等下去。"""
    cmd = [paths.lark_cli(), *args]
    try:
        p = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError as e:
        raise FeishuError(f"lark-cli {args[:2]} failed to start: {e}")
    try:
        stdout, stderr = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(p)
        p.communicate()
        raise FeishuError(f"lark-cli {args[:2]} timed out after {timeout}s")
    except BaseException:
        _kill_group(p)
        p.communicate()
        raise
    r = subprocess.CompletedProcess(cmd, p.returncode, stdout, stderr)
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


def list_messages(
    chat_id: str,
    after_position=None,
    page_size: int = 50,
    max_pages: int = 5,
    timeout: float = 15,
):
    """以 user 身份拉群消息，按 message_position 从旧到新返回原始 item 列表。

    after_position 为 None 时只拉最新一页；否则一直往前翻页，直到翻过
    after_position 或达到 max_pages。失败抛 FeishuError。"""
    items = []
    token = None
    for _ in range(max_pages):
        params = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "sort_type": "ByCreateTimeDesc",
            "page_size": page_size,
        }
        if token:
            params["page_token"] = token
        data = _json(
            _run(
                [
                    "api",
                    "GET",
                    "/open-apis/im/v1/messages",
                    "--as",
                    "user",
                    "--params",
                    json.dumps(params),
                ],
                timeout=timeout,
            )
        )
        if data.get("ok") is False:
            raise FeishuError(f"list messages failed: {json.dumps(data)[:200]}")
        page = data.get("data") or {}
        batch = [x for x in page.get("items") or [] if isinstance(x, dict)]
        items.extend(batch)
        token = page.get("page_token")
        if after_position is None or not page.get("has_more") or not token:
            break
        if any(position(x) <= after_position for x in batch):
            break
    items.sort(key=position)
    return items


def position(item: dict) -> int:
    try:
        return int(item.get("message_position"))
    except (TypeError, ValueError):
        return -1
