#!/usr/bin/env bash
# claude-lark 安装脚本：可重复运行（幂等）。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
LARK_DIR="$HOME/.claude/lark"
CMD_DIR="$HOME/.claude/commands"
LARK_CLI="${CLAUDE_LARK_CLI:-lark-cli}"
export CLAUDE_LARK_CLI="$LARK_CLI"

die() { echo "❌ $*" >&2; exit 1; }

# 1. 依赖检查
command -v python3 >/dev/null 2>&1 || die "找不到 python3，请先安装 Python ≥ 3.9"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "python3 版本过低，需要 ≥ 3.9（当前 $(python3 -V 2>&1)）"
command -v "$LARK_CLI" >/dev/null 2>&1 || [ -x "$LARK_CLI" ] \
  || die "找不到 lark-cli，请先安装：npm i -g @larksuite/cli"

# 2. 先做完所有校验，任何一项不过就中止，不留下任何残留
if [ -e "$LARK_DIR/app" ] && [ ! -L "$LARK_DIR/app" ]; then
  die "$LARK_DIR/app 已存在且不是软链接，请先移走它再重试"
fi
if [ -e "$CMD_DIR/lark.md" ] && [ ! -L "$CMD_DIR/lark.md" ]; then
  die "$CMD_DIR/lark.md 已存在且不是软链接，请先移走它再重试"
fi
cd "$REPO"
python3 -c '
import sys
from lark_bridge import settings
try:
    settings.merge(settings.load(settings.settings_path()))
except settings.SettingsError as e:
    print("❌ " + str(e), file=sys.stderr)
    sys.exit(1)
'

# 3. 目录与软链接
mkdir -p "$LARK_DIR" "$CMD_DIR"
ln -sfn "$REPO" "$LARK_DIR/app"
ln -sf "$LARK_DIR/app/commands/lark.md" "$CMD_DIR/lark.md"

# 4. 合并 settings.json（自动备份；已有的无关配置原样保留）
python3 -c '
import sys
from lark_bridge import settings
print(settings.apply("merge"))
'

# 5. 记录 owner（当前登录的飞书用户；只有这个人发的消息会被处理）
python3 -c '
import json, sys
from lark_bridge import feishu, paths
try:
    oid = feishu.owner_open_id()
except Exception as e:
    print("❌ 读不到当前登录用户（%s），请先运行 lark-cli auth login 再重新安装" % e, file=sys.stderr)
    sys.exit(1)
cfg_path = paths.config_file()
cfg_path.parent.mkdir(parents=True, exist_ok=True)
try:
    cfg = json.loads(cfg_path.read_text())
    if not isinstance(cfg, dict):
        cfg = {}
except (OSError, ValueError):
    cfg = {}
cfg["owner_open_id"] = oid
cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
print("已写入 %s" % cfg_path)
'

# 6. 自检（失败不让安装失败：doctor 会打印怎么修）
echo
"$LARK_DIR/app/bin/claude-lark" doctor || true

echo
echo "安装完成。已经开着的 claude 会话需要重启一次才能使用 /lark"
