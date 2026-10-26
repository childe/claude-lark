#!/usr/bin/env bash
# 扫描敏感信息。无参数：扫描 git diff --cached 的新增行；有参数：扫描给定文件。
# 通用规则写在下面；与具体公司/内部相关的规则放在仓库根目录 .secret-patterns
# （每行一个正则，已 gitignore，不提交）。含 _test_ 的行视为测试假值，放行。
# 注意：本脚本自身会被扫描，所以规则里用 [x] 括号写法避免命中自己。
set -u

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
GENERIC='cli_[a-z0-9]{8,}|ou_[0-9a-f]{8,}|oc_[0-9a-f]{8,}|/[U]sers/|/home/[a-z]|app_[s]ecret|tenant_[a]ccess_token'

PATTERNS_FILE="$ROOT/.secret-patterns"
EXTRA=""
if [ -f "$PATTERNS_FILE" ]; then
  EXTRA="$(grep -v -e '^[[:space:]]*$' -e '^[[:space:]]*#' "$PATTERNS_FILE" | paste -sd'|' -)"
fi
PATTERN="$GENERIC"
[ -n "$EXTRA" ] && PATTERN="$GENERIC|$EXTRA"

if [ "$#" -gt 0 ]; then
  CONTENT="$(for f in "$@"; do grep -n -H -E "$PATTERN" "$f" 2>/dev/null || true; done)"
else
  # 只看新增行（排除 +++ 文件头），带上文件名
  CONTENT="$(git diff --cached -U0 --no-color | awk '
    /^\+\+\+ b\// { file = substr($0, 7); next }
    /^\+\+\+ /    { next }
    /^\+/         { print file ":" substr($0, 2) }
  ' | grep -E "$PATTERN" || true)"
fi

# 放行测试假值
HITS="$(printf '%s\n' "$CONTENT" | grep -v '_test_' | grep -v '^$' || true)"

if [ -n "$HITS" ]; then
  echo "scan-secrets: 发现疑似敏感信息，已阻止：" >&2
  printf '%s\n' "$HITS" >&2
  exit 1
fi
exit 0
