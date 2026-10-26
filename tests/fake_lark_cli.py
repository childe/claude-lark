#!/usr/bin/env python3
"""测试用的假 lark-cli。

- 每次调用把 argv（JSON 数组）追加到 $FAKE_LARK_LOG。
- $FAKE_LARK_SLEEP=N 时先睡 N 秒（测超时用）。
- $FAKE_LARK_FAIL=1 时打印错误并以 1 退出。
- im +chat-create → {"data":{"chat_id":"oc_test_chat"}}
- im +messages-send → {"data":{"message_id","chat_id","create_time"}}；create_time 默认是当前
  毫秒时间，$FAKE_LARK_SEND_TIME 可固定它，设为 none 时不返回该字段
- im +chat-members-list → {"data":{"bots":[],"users":[]}}；$FAKE_LARK_NOT_IN_CHAT=1 时报错退出 1
- auth status → owner ou_test_owner；user scope 默认含 im:message.group_msg:get_as_user，
  $FAKE_LARK_USER_SCOPE 可覆盖
- --version → lark-cli version 1.0.92
- api GET /open-apis/im/v1/messages → 把 $FAKE_LARK_EVENTS（每行一个事件格式的 JSON）当作
  群消息记录，按真实接口格式返回 container_id 那个群的消息，新的在前；
  message_position 是行号，text/post 的 content 包装成接口的 body.content。
"""

import json
import os
import sys
import time


def _body(ev):
    content = ev.get("content")
    if not isinstance(content, str):
        return {}
    if ev.get("message_type") == "post":
        return {
            "content": json.dumps(
                {"title": "", "content": [[{"tag": "text", "text": content}]]},
                ensure_ascii=False,
            )
        }
    return {"content": json.dumps({"text": content}, ensure_ascii=False)}


def _list_messages(params):
    path = os.environ.get("FAKE_LARK_EVENTS")
    items = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().split("\n")[:-1]  # 只要完整的行
    except (OSError, TypeError):
        lines = []
    for i, line in enumerate(lines, 1):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("chat_id") != params["container_id"]:
            continue
        stype = ev.get("sender_type")
        item = {
            "chat_id": ev.get("chat_id"),
            "create_time": ev.get("create_time"),
            "deleted": False,
            "message_position": str(i),
            "msg_type": ev.get("message_type"),
            "body": _body(ev),
            "sender": {
                "id": ev.get("sender_id"),
                "id_type": "open_id" if stype == "user" else "app_id",
                "sender_type": stype,
            },
        }
        if ev.get("message_id") is not None:
            item["message_id"] = ev["message_id"]
        if ev.get("mentions"):
            item["mentions"] = ev["mentions"]
        items.append(item)
    items.reverse()
    size = int(params.get("page_size", 50))
    return {
        "ok": True,
        "identity": "user",
        "data": {"has_more": len(items) > size, "items": items[:size]},
    }


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
    elif argv[:3] == ["api", "GET", "/open-apis/im/v1/messages"]:
        params = json.loads(argv[argv.index("--params") + 1])
        print(json.dumps(_list_messages(params), ensure_ascii=False))
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
                        "user": {
                            "status": "ready",
                            "openId": "ou_test_owner",
                            "scope": os.environ.get(
                                "FAKE_LARK_USER_SCOPE",
                                "im:message im:message.group_msg:get_as_user",
                            ),
                        },
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
