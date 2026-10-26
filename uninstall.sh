#!/usr/bin/env bash
# claude-lark 卸载脚本。--purge：连同 ~/.claude/lark/sessions 一起删，不再询问。
set -euo pipefail

PURGE=0
for a in "$@"; do
  case "$a" in
    --purge) PURGE=1 ;;
    *) echo "用法: ./uninstall.sh [--purge]" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
LARK_DIR="${CLAUDE_LARK_HOME:-$HOME/.claude/lark}"
LINK_DIR="$HOME/.claude/lark"

# 1. 关掉所有会话的桥接：删 enabled 文件，再停监听进程
if [ -d "$LARK_DIR/sessions" ]; then
  find "$LARK_DIR/sessions" -mindepth 2 -maxdepth 2 -name enabled -type f -exec rm -f {} +
fi
if [ -f "$LARK_DIR/listener.pid" ]; then
  pid="$(tr -dc '0-9' < "$LARK_DIR/listener.pid" || true)"
  # 确认这个 pid 确实还是监听进程，避免 pid 被复用后误杀别的进程
  if [ -n "$pid" ] && ps -p "$pid" -o command= 2>/dev/null | grep -q "claude-lark"; then
    kill -TERM "$pid" 2>/dev/null || true
    echo "已停止监听进程 $pid"
  fi
fi

# 2. 从 settings.json 里移除本项目的 hooks（自动备份）
cd "$REPO"
python3 -c '
from lark_bridge import settings
print(settings.apply("remove"))
'

# 3. 删软链接（只删软链接，不碰真实文件）
[ -L "$HOME/.claude/commands/lark.md" ] && rm -f "$HOME/.claude/commands/lark.md" && echo "已删除 ~/.claude/commands/lark.md"
[ -L "$LINK_DIR/app" ] && rm -f "$LINK_DIR/app" && echo "已删除 $LINK_DIR/app"

# 4. 会话数据
if [ -d "$LARK_DIR/sessions" ]; then
  if [ "$PURGE" = 1 ]; then
    rm -rf "$LARK_DIR/sessions"
    echo "已删除 $LARK_DIR/sessions"
  elif [ -t 0 ]; then
    read -r -p "删除会话数据 $LARK_DIR/sessions？[y/N] " ans
    case "$ans" in
      y|Y|yes) rm -rf "$LARK_DIR/sessions"; echo "已删除" ;;
      *) echo "已保留 $LARK_DIR/sessions" ;;
    esac
  else
    echo "已保留 $LARK_DIR/sessions（加 --purge 可删除）"
  fi
fi

echo "卸载完成。已经开着的 claude 会话重启后才会彻底不再加载 /lark"
