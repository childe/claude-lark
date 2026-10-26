"""进程辅助函数。"""

import os
import subprocess


def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def start_time(pid) -> str:
    """进程启动时间（`ps -o lstart=`，C locale，空白归一）；取不到返回空串。

    和 pid 一起记下来：pid 会被复用（比如重启之后），启动时间不会一样。"""
    if not pid or pid <= 0:
        return ""
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return " ".join(out.split())


def _ps(fields: str, pid: int) -> str:
    """ps -o <f1>= -o <f2>= ... -p <pid> 的输出（无表头）；出错返回空串。

    每个字段要单独一个 `-o f=`：`-o ppid,comm=` 只去掉最后一列的表头，
    输出会多一行 "PPID"。"""
    opts = []
    for f in fields.split(","):
        opts += ["-o", f + "="]
    try:
        return subprocess.run(
            ["ps", *opts, "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _is_claude(pid: int, comm: str) -> bool:
    base = os.path.basename(comm.strip())
    if base.endswith("claude"):
        return True
    # npm 安装的 Claude Code：进程名是 node / bun，命令行里带 claude
    return base in ("node", "bun") and "claude" in _ps("args", pid)


def find_claude_ancestor():
    """沿父进程向上找 claude 进程，找不到返回 None。"""
    pid = os.getppid()
    for _ in range(64):
        if pid <= 1:
            return None
        out = _ps("ppid,comm", pid)
        if not out:
            return None
        ppid_s, _, comm = out.partition(" ")
        if _is_claude(pid, comm):
            return pid
        try:
            pid = int(ppid_s)
        except ValueError:
            return None
    return None
