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


def send(chat_id: str, markdown: str) -> None:
    """分段发送；任何失败只记 errors.log，不抛出。某段失败后不再发后续段。"""
    try:
        for part in split_message(markdown):
            _run(
                [
                    "im",
                    "+messages-send",
                    "--as",
                    "bot",
                    "--chat-id",
                    chat_id,
                    "--markdown",
                    part,
                ]
            )
    except FeishuError as e:
        log_error(f"send to {chat_id}: {e}")
    except Exception:
        log_exception(f"send to {chat_id}")


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
