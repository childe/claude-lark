#!/usr/bin/env python3
"""测试用的假 lark-cli。

- 每次调用把 argv（JSON 数组）追加到 $FAKE_LARK_LOG。
- $FAKE_LARK_SLEEP=N 时先睡 N 秒（测超时用）。
- $FAKE_LARK_FAIL=1 时打印错误并以 1 退出。
- im +chat-create → {"data":{"chat_id":"oc_test_chat"}}
- im +chat-members-list → {"data":{"bots":[],"users":[]}}；$FAKE_LARK_NOT_IN_CHAT=1 时报错退出 1
- auth status → owner ou_test_owner
- --version → lark-cli version 1.0.92（$FAKE_LARK_PRECOND 可改 dry-run 里一项的 status）
- event consume --dry-run → preconditions 全 ok
- event consume → 逐行输出 $FAKE_LARK_EVENTS 的内容，然后阻塞；
  $FAKE_LARK_IGNORE_TERM=1 时忽略 SIGTERM。
"""

import json
import os
import signal
import sys
import time


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
        print(json.dumps({"ok": True, "data": {"message_id": "om_test_sent"}}))
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
        events = os.environ.get("FAKE_LARK_EVENTS")
        if events and os.path.exists(events):
            with open(events) as f:
                for line in f:
                    sys.stdout.write(line if line.endswith("\n") else line + "\n")
                    sys.stdout.flush()
        while True:
            time.sleep(3600)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
