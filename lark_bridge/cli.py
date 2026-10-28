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
    if argv == ["doctor"]:
        from . import doctor

        return doctor.run()
    if argv == ["listen"]:
        from . import listener

        return listener.main()
    if argv and argv[0] in ("slash", "on"):
        return _command(argv)
    if argv and argv[0] == "mirror":
        return _mirror(argv)
    print(
        "用法: claude-lark --version | hook <event> | listen | doctor"
        " | slash --session <sid> --args <str> | on --session <sid> --name <str>",
        file=sys.stderr,
    )
    return 2


def _hook(event: str) -> int:
    """hook 入口：读 stdin，输出 hooks.run 的结果。

    结果是 hooks.Rewake 时写 stderr 并 exit 2（asyncRewake 唤醒模型），其他情况 exit 0。"""
    saved = None
    code = 0
    try:
        import signal

        from . import hooks

        # 事先装上 hooks 的处理函数：run() 结束时恢复的仍是它，且 hold_sigterm
        # 让它保持"只记录"，结果写完 stdout 之前 SIGTERM 不会打断
        try:
            saved = (signal.signal(signal.SIGTERM, hooks._on_sigterm),)
        except (ValueError, OSError):
            pass
        out = hooks.run(event, sys.stdin.read(), hold_sigterm=True)
        if isinstance(out, hooks.Rewake):
            sys.stderr.write(out)
            sys.stderr.flush()
            code = 2
        elif out:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
    except BaseException:  # noqa: B036 - 出错时绝不能以非 0 退出
        try:
            import traceback

            from . import paths

            with open(paths.home() / "errors.log", "a") as f:
                f.write(f"hook {event} (cli): {traceback.format_exc()}\n")
        except BaseException:
            pass
    finally:
        try:
            if saved is not None:
                signal.signal(signal.SIGTERM, saved[0])
            hooks._term["defer"] = False
        except BaseException:
            pass
    return code


def _command(argv: List[str]) -> int:
    """slash / on 入口：把 RESULT 文本打到 stdout，永远 exit 0。"""
    import argparse

    ap = argparse.ArgumentParser(prog="claude-lark " + argv[0], add_help=False)
    ap.add_argument("--session", default="")
    ap.add_argument(
        "--args" if argv[0] == "slash" else "--name", dest="text", default=""
    )
    try:
        ns, _ = ap.parse_known_args(argv[1:])
        from . import commands

        fn = commands.slash if argv[0] == "slash" else commands.on
        out = fn(ns.session, ns.text)
    except BaseException as e:  # noqa: B036
        out = f"RESULT: 出错了：{type(e).__name__}"
        try:
            from . import feishu

            feishu.log_exception(f"{argv[0]} (cli)")
        except BaseException:
            pass
    print(out)
    return 0


def _mirror(argv: List[str]) -> int:
    """UserPromptSubmit 拉起的后台进程：把终端输入发到群里。永远 exit 0。"""
    import argparse

    ap = argparse.ArgumentParser(prog="claude-lark mirror", add_help=False)
    ap.add_argument("--session", default="")
    ap.add_argument("--file", default="")
    try:
        ns, _ = ap.parse_known_args(argv[1:])
        from . import hooks

        hooks.mirror(ns.session, ns.file)
    except BaseException:  # noqa: B036
        pass
    return 0
