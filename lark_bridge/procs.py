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
    """进程启动时间的标识；取不到返回空串。

    和 pid 一起记下来：pid 会被复用（比如重启之后），启动时间不会一样。

    Linux 上用 /proc/<pid>/stat 第 22 个字段（开机后的时钟滴答数），返回
    "ticks:<n>"。它不随系统时间变化；`ps -o lstart` 是用 当前时间 − uptime
    推出来的，调系统时间（WSL 里常见）会让它整秒跳动，误判进程已换。
    没有 /proc（macOS）时退回 lstart()。"""
    ticks = _proc_start_ticks(pid)
    if ticks:
        return f"ticks:{ticks}"
    return lstart(pid)


def _proc_start_ticks(pid) -> str:
    if not pid or pid <= 0:
        return ""
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            data = f.read()
    except (OSError, ValueError):
        return ""
    # comm（第 2 个字段）可能含空格和括号，从最后一个 ')' 之后从第 3 个字段数起
    fields = data[data.rfind(")") + 1 :].split()
    if len(fields) < 20 or not fields[19].isdigit():
        return ""
    return fields[19]


def lstart(pid) -> str:
    """`ps -o lstart=`（C locale，空白归一）；取不到返回空串。会随系统时间漂移，
    只用于没有 /proc 的系统，以及兼容旧格式的 enabled 文件。"""
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
