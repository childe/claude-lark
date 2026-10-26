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


def find_claude_ancestor():
    """沿父进程向上找 comm 以 claude 结尾的进程，找不到返回 None。"""
    pid = os.getppid()
    for _ in range(64):
        if pid <= 1:
            return None
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        if not out:
            return None
        ppid_s, _, comm = out.partition(" ")
        if os.path.basename(comm.strip()).endswith("claude"):
            return pid
        try:
            pid = int(ppid_s)
        except ValueError:
            return None
    return None
