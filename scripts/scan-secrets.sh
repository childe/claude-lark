#!/usr/bin/env bash
# 扫描敏感信息。无参数：扫描 git diff --cached 的新增行；有参数：扫描给定文件。
# 通用规则写在下面；与具体公司/内部相关的规则放在仓库根目录 .secret-patterns
# （每行一个正则，已 gitignore，不提交）。形如 (ou|oc|cli)_test_xxx 的假值 token 会先从行内容中剔除再匹配（只剔 token，不放行整行）。
# 注意：本脚本自身会被扫描，所以规则里用 [x] 括号写法避免命中自己。
set -u

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
GENERIC='cli_[a-z0-9]{8,}|ou_[0-9a-f]{8,}|oc_[0-9a-f]{8,}|/[U]sers/|/home/[a-z]|app_[s]ecret|tenant_[a]ccess_token'

PATTERNS_FILE="$ROOT/.secret-patterns"
EXTRA=""
if [ -f "$PATTERNS_FILE" ]; then
  EXTRA="$(tr -d '\r' < "$PATTERNS_FILE" | grep -v -e '^[[:space:]]*$' -e '^[[:space:]]*#' | paste -sd'|' -)"
fi
PATTERN="$GENERIC"
[ -n "$EXTRA" ] && PATTERN="$GENERIC|$EXTRA"

# 产生 "标签<TAB>内容" 记录；标签（文件名等）不参与匹配
if [ "$#" -gt 0 ]; then
  RECORDS="$(for f in "$@"; do
    n=0
    while IFS= read -r line || [ -n "$line" ]; do
      n=$((n + 1))
      printf '%s:%s\t%s\n' "$f" "$n" "$line"
    done < "$f"
  done)"
else
  RECORDS="$(git diff --cached -U0 --no-color | awk '
    /^\+\+\+ b\// { file = substr($0, 7); next }
    /^\+\+\+ /    { next }
    /^\+/         { printf "%s\t%s\n", file, substr($0, 2) }
  ')"
fi

HITS=""
while IFS=$'\t' read -r label content; do
  [ -z "$label" ] && continue
  stripped="$(printf '%s' "$content" | sed -E 's/(ou|oc|cli)_test_[A-Za-z0-9_]*//g')"
  if printf '%s' "$stripped" | grep -q -E "$PATTERN"; then
    HITS="$HITS$label: $content"$'\n'
  fi
done <<< "$RECORDS"

if [ -n "$HITS" ]; then
  echo "scan-secrets: 发现疑似敏感信息，已阻止：" >&2
  printf '%s' "$HITS" >&2
  exit 1
fi
exit 0
