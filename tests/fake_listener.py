#!/usr/bin/env python3
"""测试用的"旧监听进程"替身。不 import lark_bridge.listener，模拟旧版本代码。

- 在 listener.pid.lock 的 flock 内拿 listener.lock 的排他锁，写 listener.pid。
- --version V：再写 listener.version = {"pid": 自己, "code_version": V}。
- --ignore-term：忽略 SIGTERM；否则 SIGTERM 时在 listener.pid.lock 内删自己的
  pid 文件和实例信息文件、放掉存活锁，以 0 退出。
- 最多存活 120 秒。
"""

import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

HOME = Path(os.environ["CLAUDE_LARK_HOME"])
PID_LOCK = HOME / "listener.pid.lock"
LIFE_LOCK = HOME / "listener.lock"
PID_FILE = HOME / "listener.pid"
VERSION_FILE = HOME / "listener.version"


def _pid_lock():
    f = open(PID_LOCK, "a+")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def main(argv):
    version = None
    ignore_term = False
    i = 0
    while i < len(argv):
        if argv[i] == "--version":
            version = argv[i + 1]
            i += 2
        elif argv[i] == "--ignore-term":
            ignore_term = True
            i += 1
        else:
            raise SystemExit(f"unknown arg {argv[i]}")
    HOME.mkdir(parents=True, exist_ok=True)
    me = os.getpid()
    pl = _pid_lock()
    try:
        life = open(LIFE_LOCK, "a+")
        fcntl.flock(life, fcntl.LOCK_EX | fcntl.LOCK_NB)
        PID_FILE.write_text(str(me))
        if version is not None:
            VERSION_FILE.write_text(json.dumps({"pid": me, "code_version": version}))
    finally:
        pl.close()

    def on_term(signum, frame):
        lock = _pid_lock()
        try:
            try:
                if PID_FILE.read_text().strip() == str(me):
                    PID_FILE.unlink()
            except OSError:
                pass
            try:
                if json.loads(VERSION_FILE.read_text()).get("pid") == me:
                    VERSION_FILE.unlink()
            except (OSError, ValueError, AttributeError):
                pass
            life.close()
        finally:
            lock.close()
        os._exit(0)

    signal.signal(signal.SIGTERM, signal.SIG_IGN if ignore_term else on_term)
    end = time.time() + 120
    while time.time() < end:
        time.sleep(0.1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
