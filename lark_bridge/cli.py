import sys
from typing import List, Optional

from . import __version__


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--version":
        print(f"claude-lark {__version__}")
        return 0
    if len(argv) == 2 and argv[0] == "hook":
        return _hook(argv[1])
    print("用法: claude-lark --version | hook <event>", file=sys.stderr)
    return 2


def _hook(event: str) -> int:
    """hook 入口：读 stdin，输出 hooks.run 的结果，永远 exit 0。"""
    try:
        from . import hooks

        out = hooks.run(event, sys.stdin.read())
        if out:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
    except BaseException:  # noqa: B036 - hook 绝不能以非 0 退出
        try:
            import traceback

            from . import paths

            with open(paths.home() / "errors.log", "a") as f:
                f.write(f"hook {event} (cli): {traceback.format_exc()}\n")
        except BaseException:
            pass
    return 0
