"""claude-lark restart-listener：停掉正在运行的监听进程，必要时拉起当前代码的监听进程。

install.sh 升级后调用：常驻的监听进程不会自己加载新代码。"""

import os
import signal
import time

from . import listener, paths

TERM_WAIT = 20  # 旧监听进程可能正卡在一次最长 15 秒的 lark-cli 拉取里
KILL_WAIT = 5
UP_WAIT = 5
INTERVAL = 0.2


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def _fail(msg: str, fix: str) -> int:
    print(f"❌ {msg}")
    print(f"   修复：{fix}")
    return 1


_KILL_FIX = "手动结束 claude-lark listen 进程，再运行 claude-lark restart-listener"


def _wait_handover(p: int, timeout: float, interval: float) -> bool:
    """等原实例 p 交出存活锁（退出，或已被别的实例接替）；超时返回 False。"""
    end = time.monotonic() + timeout
    while True:
        held, pid, _ = listener.instance()
        if not held or pid != p:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(interval)


def _kill_if_still(p: int) -> bool:
    """存活锁仍被持有且 pid 文件仍为 p 时发 SIGKILL，返回是否发了。"""
    with listener._pid_lock():
        held, pid, _ = listener._instance_locked()
        if held and pid == p:
            _signal(p, signal.SIGKILL)
            return True
    return False


def run(
    term_wait: float = TERM_WAIT,
    kill_wait: float = KILL_WAIT,
    up_wait: float = UP_WAIT,
    interval: float = INTERVAL,
) -> int:
    with listener._pid_lock():
        held, p, _ = listener._instance_locked()
        if held and p is None:
            return _fail("监听进程在运行，但读不到它的 pid 文件，无法停止", _KILL_FIX)
        if held:
            _signal(p, signal.SIGTERM)
    if held:
        if not _wait_handover(p, term_wait, interval) and _kill_if_still(p):
            if not _wait_handover(p, kill_wait, interval):
                return _fail(f"监听进程（pid {p}）收到 SIGKILL 后仍未退出", _KILL_FIX)

    if not listener._any_enabled():
        print("✅ 没有开启中的会话，不需要监听进程")
        return 0

    want = listener.code_version()
    for _ in range(2):
        listener.ensure_running()
        end = time.monotonic() + up_wait
        while True:
            held, pid, ver = listener.instance()
            if held and ver == want:
                print(f"✅ 监听进程已重启（pid {pid}，版本 {ver}）")
                return 0
            if time.monotonic() >= end:
                break
            time.sleep(interval)
    return _fail(
        "监听进程没有拉起来，或运行的不是当前版本",
        f"查看 {paths.listener_log()} 与 {paths.home() / 'errors.log'}，"
        "再运行 claude-lark restart-listener",
    )
