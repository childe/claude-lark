#!/usr/bin/env python3
"""测试用的假 lark-cli。

- 每次调用把 argv（JSON 数组）追加到 $FAKE_LARK_LOG。
- $FAKE_LARK_SLEEP=N 时先睡 N 秒（测超时用）。
- $FAKE_LARK_FAIL=1 时打印错误并以 1 退出。
- im +chat-create → {"data":{"chat_id":"oc_test_chat"}}
- im +messages-send → {"data":{"message_id","chat_id","create_time"}}；create_time 默认是当前
  毫秒时间，$FAKE_LARK_SEND_TIME 可固定它，设为 none 时不返回该字段
- im +chat-members-list → {"data":{"bots":[],"users":[]}}；$FAKE_LARK_NOT_IN_CHAT=1 时报错退出 1
- auth status → owner ou_test_owner
- --version → lark-cli version 1.0.92（$FAKE_LARK_PRECOND 可改 dry-run 里一项的 status）
- event consume --dry-run → preconditions 全 ok
- event consume → 逐行输出 $FAKE_LARK_EVENTS 的内容，然后像 tail -f 一样继续跟随
  文件增长（文件一开始不存在也会等它出现），只输出完整的行；
  $FAKE_LARK_IGNORE_TERM=1 时忽略 SIGTERM。
  和真实 lark-cli 一样，stdin 读到 EOF 就退出（rc=0）。
"""

import json
import os
import signal
import sys
import threading
import time


def _follow(path):
    """tail -f：输出已有的完整行，再每 0.2 秒检查一次新追加的行。永不返回。"""
    pos = 0
    buf = b""
    while True:
        if path:
            try:
                with open(path, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
            except OSError:
                chunk = b""
            if chunk:
                pos += len(chunk)
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    sys.stdout.write(line.decode("utf-8", "replace") + "\n")
                sys.stdout.flush()
        time.sleep(0.2)


def _exit_on_stdin_eof():
    """真实 lark-cli 的 event consume 把 stdin EOF 当作退出信号。"""
    while sys.stdin.buffer.read(4096):
        pass
    os._exit(0)


def main(argv):
    log = os.environ.get("FAKE_LARK_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(argv, ensure_ascii=False) + "\n")
    if os.environ.get("FAKE_LARK_SLEEP"):
        time.sleep(float(os.environ["FAKE_LARK_SLEEP"]))
    if os.environ.get("FAKE_LARK_FAIL") == "1":
        print("fake lark-cli: forced failure", file=sys.stderr)
        return 1
    if argv == ["--version"]:
        print("lark-cli version 1.0.92")
    elif argv[:2] == ["event", "consume"] and "--dry-run" in argv:
        status = os.environ.get("FAKE_LARK_PRECOND", "ok")
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "data": {
                        "decision": {
                            "preconditions": [
                                {"name": "credentials_available", "status": "ok"},
                                {"name": "console_event_published", "status": status},
                                {"name": "scopes_granted", "status": "ok"},
                            ]
                        }
                    },
                }
            )
        )
    elif argv[:2] == ["im", "+chat-create"]:
        print(json.dumps({"ok": True, "data": {"chat_id": "oc_test_chat"}}))
    elif argv[:2] == ["im", "+messages-send"]:
        data = {"message_id": "om_test_sent", "chat_id": "oc_test_chat"}
        ct = os.environ.get("FAKE_LARK_SEND_TIME") or str(int(time.time() * 1000))
        if ct != "none":
            data["create_time"] = ct
        print(json.dumps({"ok": True, "data": data}))
    elif argv[:2] == ["im", "+chat-members-list"]:
        if os.environ.get("FAKE_LARK_NOT_IN_CHAT") == "1":
            print("fake lark-cli: bot not in chat", file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "data": {"bots": [], "users": []}}))
    elif argv[:2] == ["auth", "status"]:
        print(
            json.dumps(
                {
                    "identities": {
                        "bot": {"status": "ready"},
                        "user": {"status": "ready", "openId": "ou_test_owner"},
                    }
                }
            )
        )
    elif argv[:2] == ["event", "consume"]:
        if os.environ.get("FAKE_LARK_IGNORE_TERM") == "1":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        threading.Thread(target=_exit_on_stdin_eof, daemon=True).start()
        _follow(os.environ.get("FAKE_LARK_EVENTS"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
