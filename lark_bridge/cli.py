import sys
from typing import List, Optional

from . import __version__


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--version":
        print(f"claude-lark {__version__}")
        return 0
    print("用法: claude-lark --version", file=sys.stderr)
    return 2
