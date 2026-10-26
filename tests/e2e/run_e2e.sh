#!/usr/bin/env bash
# claude-lark 端到端测试：在 tmux 里驱动真实的交互式 Claude Code，
# 挂上真实的 hooks 和 /lark 命令，用假 lark-cli 代替飞书。
#
# 用法：tests/e2e/run_e2e.sh [基目录]
#   基目录默认是仓库里（已 gitignore）的 .superpowers/e2e；每次运行在下面新建 e2e-<时间戳>-XXXX。
#   E2E_ONLY="2 3" 只跑这些场景（场景 1 总会先跑，其余场景都依赖它的绑定）。
#   E2E_MODEL 覆盖模型（默认 haiku）。临时目录不会删除，方便排查。
#
# 不碰真实的 ~/.claude/settings.json、~/.claude/commands：hooks 和 /lark 都写在临时 project 的 .claude/ 里。
set -u

REPO="$(builtin cd "$(dirname "$0")/../.." && pwd)"
BIN="$REPO/bin/claude-lark"
FAKE="$REPO/tests/fake_lark_cli.py"
MODEL="${E2E_MODEL:-claude-haiku-4-5-20251001}"
WAIT_SECONDS=20
TIMEOUT=90

BASE="${1:-$REPO/.superpowers/e2e}"
mkdir -p "$BASE"
BASE="$(builtin cd "$BASE" && pwd)"
RUN="$(mktemp -d "$BASE/e2e-$(date +%Y%m%d-%H%M%S)-XXXX")"
PROJ="$RUN/project"
export CLAUDE_LARK_HOME="$RUN/lark_home"
export CLAUDE_LARK_CLI="$FAKE"
export FAKE_LARK_LOG="$RUN/fake_lark.log"
export FAKE_LARK_EVENTS="$RUN/events.ndjson"
TMUX_S="claude-lark-e2e-$$"
OWNER="ou_test_owner"
CHAT="oc_test_chat"

PASSED=0
FAILED=0
RESULTS=()
MSG_SEQ=0

# ---------- 清理 ----------

cleanup() {
    tmux kill-session -t "$TMUX_S" 2>/dev/null
    local pidf="$CLAUDE_LARK_HOME/listener.pid" lpid kids
    if [ -f "$pidf" ]; then
        lpid="$(cat "$pidf" 2>/dev/null)"
        if [ -n "$lpid" ]; then
            kids="$(pgrep -P "$lpid" 2>/dev/null)"
            kill "$lpid" 2>/dev/null
            for _ in 1 2 3 4 5 6 7 8 9 10; do
                kill -0 "$lpid" 2>/dev/null || break
                sleep 0.5
            done
            kill -9 "$lpid" $kids 2>/dev/null
        fi
    fi
    # 被 tmux 杀掉的 claude 留下的 hook 进程（命令行里带本仓库的 bin 路径）
    pkill -f "$BIN hook" 2>/dev/null
    pkill -f "$BIN listen" 2>/dev/null
    pkill -f "$FAKE event consume" 2>/dev/null
    sleep 0.5
    local left
    left="$(pgrep -fl "$REPO/(bin/claude-lark|tests/fake_lark_cli)" 2>/dev/null)"
    if [ -n "$left" ]; then
        echo "WARN: 清理后仍有进程：" >&2
        echo "$left" >&2
    fi
    echo "临时目录：$RUN"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# ---------- 辅助函数 ----------

pane() { tmux capture-pane -p -t "$TMUX_S" -S -300 2>/dev/null; }
send_text() {
    tmux send-keys -t "$TMUX_S" -l "$1"
    sleep 0.3
    tmux send-keys -t "$TMUX_S" Enter
}
log_lines() { [ -f "$FAKE_LARK_LOG" ] && wc -l <"$FAKE_LARK_LOG" | tr -d ' ' || echo 0; }

# sent_since <起始行号> <子串>：起始行之后有内容含该子串的 +messages-send
sent_since() {
    python3 - "$FAKE_LARK_LOG" "$1" "$2" <<'PY'
import json, sys
path, start, needle = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    lines = open(path, encoding="utf-8").read().splitlines()[start:]
except OSError:
    sys.exit(1)
for line in lines:
    argv = json.loads(line)
    if argv[:2] == ["im", "+messages-send"] and "--markdown" in argv:
        if needle in argv[argv.index("--markdown") + 1]:
            sys.exit(0)
sys.exit(1)
PY
}

# sends_since <起始行号>：起始行之后的 +messages-send 正文，每条一行（JSON 字符串）
sends_since() {
    python3 - "$FAKE_LARK_LOG" "$1" <<'PY'
import json, sys
try:
    lines = open(sys.argv[1], encoding="utf-8").read().splitlines()[int(sys.argv[2]):]
except OSError:
    sys.exit(0)
for line in lines:
    argv = json.loads(line)
    if argv[:2] == ["im", "+messages-send"] and "--markdown" in argv:
        print(json.dumps(argv[argv.index("--markdown") + 1], ensure_ascii=False))
PY
}

# inject <文本> [sender]：模拟一条群消息（追加到假 lark-cli 跟随的事件文件）
inject() {
    MSG_SEQ=$((MSG_SEQ + 1))
    LAST_MID="om_test_e2e_${MSG_SEQ}"
    python3 - "$FAKE_LARK_EVENTS" "$1" "${2:-$OWNER}" "$CHAT" "$LAST_MID" <<'PY'
import json, sys, time
path, text, sender, chat, mid = sys.argv[1:6]
ev = {
    "type": "im.message.receive_v1", "chat_id": chat, "chat_type": "group",
    "content": text, "create_time": str(int(time.time() * 1000)),
    "event_id": "ev_test_" + mid, "message_id": mid, "message_type": "text",
    "mentions": [], "sender_id": sender, "sender_type": "user",
}
with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
PY
}

# wait_for <超时秒> <命令...>：每秒执行一次命令，成功返回 0，超时返回 1
wait_for() {
    local t="$1"
    shift
    local end=$((SECONDS + t))
    while [ "$SECONDS" -lt "$end" ]; do
        if "$@" >/dev/null 2>&1; then return 0; fi
        sleep 1
    done
    "$@" >/dev/null 2>&1
}

pane_has() { pane | grep -qF -- "$1"; }
stop_hook_waiting() { pgrep -f "$BIN hook stop" >/dev/null; }
session_id() { ls "$CLAUDE_LARK_HOME/sessions" 2>/dev/null | head -1; }
sdir() { echo "$CLAUDE_LARK_HOME/sessions/$(session_id)"; }

# 等 Stop hook 把一轮回复发到群里并进入等待（即会话空闲、桥接在等飞书消息）
wait_idle_bridged() { wait_for "${1:-$TIMEOUT}" stop_hook_waiting; }

pass() {
    PASSED=$((PASSED + 1))
    RESULTS+=("PASS $1")
    echo "PASS $1"
}
fail() {
    FAILED=$((FAILED + 1))
    RESULTS+=("FAIL $1 — $2")
    echo "FAIL $1 — $2"
    echo "----- pane (last 30 lines) -----"
    pane | grep -v '^[[:space:]]*$' | tail -30
    echo "----- errors.log -----"
    cat "$CLAUDE_LARK_HOME/errors.log" 2>/dev/null | tail -30
    echo "----- fake lark log (last 10) -----"
    tail -10 "$FAKE_LARK_LOG" 2>/dev/null
    echo "--------------------------------"
}
want() { [ -z "${E2E_ONLY:-}" ] || [[ " $E2E_ONLY " == *" $1 "* ]]; }

# ---------- 准备临时 project ----------

mkdir -p "$PROJ/.claude/commands" "$CLAUDE_LARK_HOME"
: >"$FAKE_LARK_EVENTS"
printf '{"owner_open_id": "%s"}\n' "$OWNER" >"$CLAUDE_LARK_HOME/config.json"
(builtin cd "$PROJ" && git init -q .)

PYTHONPATH="$REPO" python3 - "$PROJ/.claude/settings.json" "$BIN" <<'PY'
import json, sys
from lark_bridge import settings
out, bin_path = sys.argv[1], sys.argv[2]
text = json.dumps(settings.merge({}), indent=2, ensure_ascii=False)
text = text.replace("$HOME/.claude/lark/app/bin/claude-lark", bin_path)
assert "$HOME" not in text
open(out, "w", encoding="utf-8").write(text + "\n")
PY
sed "s#~/.claude/lark/app/bin/claude-lark#$BIN#g" "$REPO/commands/lark.md" \
    >"$PROJ/.claude/commands/lark.md"
grep -q '~/' "$PROJ/.claude/commands/lark.md" && { echo "lark.md 路径改写失败"; exit 1; }

echo "运行目录：$RUN"
tmux new-session -d -s "$TMUX_S" -x 160 -y 50 -c "$PROJ" \
    "CLAUDE_LARK_HOME='$CLAUDE_LARK_HOME' CLAUDE_LARK_CLI='$FAKE' FAKE_LARK_LOG='$FAKE_LARK_LOG' \
FAKE_LARK_EVENTS='$FAKE_LARK_EVENTS' CLAUDE_LARK_WAIT_SECONDS=${WAIT_SECONDS} \
CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=1000 claude --model $MODEL --permission-mode default"

# 新目录首次启动有信任对话框
if wait_for 30 pane_has "trust"; then
    sleep 1
    if pane | grep -qE '❯ *1\. *Yes'; then
        tmux send-keys -t "$TMUX_S" Enter
    else
        tmux send-keys -t "$TMUX_S" Down
        sleep 0.3
        tmux send-keys -t "$TMUX_S" Enter
    fi
fi
if ! wait_for 30 pane_has "? for shortcuts"; then
    sleep 3
fi

# ---------- 场景 ----------

s1() {
    local n0
    n0=$(log_lines)
    send_text "/lark e2e测试"
    if ! wait_for $TIMEOUT grep -qF '"+chat-create"' "$FAKE_LARK_LOG"; then
        fail "1 /lark 建群" "fake 日志里没有 +chat-create"
        return
    fi
    if ! grep -F '"+chat-create"' "$FAKE_LARK_LOG" | grep -qF '"--name", "e2e测试"'; then
        fail "1 /lark 建群" "+chat-create 的 --name 不是 e2e测试"
        return
    fi
    if ! wait_for $TIMEOUT sent_since "$n0" "已连接"; then
        fail "1 /lark 建群" "群里没有收到 已连接"
        return
    fi
    if ! wait_for $TIMEOUT pane_has "已开启桥接"; then
        fail "1 /lark 建群" "终端回复里没有 已开启桥接"
        return
    fi
    if ! wait_idle_bridged; then
        fail "1 /lark 建群" "Stop hook 没有进入等待"
        return
    fi
    pass "1 /lark 建群（+chat-create --name e2e测试，已连接）"
}

s9() {
    # 场景 1 之后会话空闲，Stop hook 等 WAIT_SECONDS 秒后应出现一次 waiting 回合
    local sd tr n0
    sd="$(sdir)"
    n0=$(log_lines)
    blocks_ge1() { [ "$(cat "$sd/counter_blocks" 2>/dev/null || echo 0)" -ge 1 ]; }
    if ! wait_for $((WAIT_SECONDS + TIMEOUT)) blocks_ge1; then
        fail "9 续命" "counter_blocks 没有增加"
        return
    fi
    tr="$(ls "$HOME"/.claude/projects/*/"$(session_id)".jsonl 2>/dev/null | head -1)"
    keepalive_answered() {
        python3 - "${tr}" <<'PY'
import json, sys
seen_reason = False
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        d = json.loads(line)
    except ValueError:
        continue
    s = json.dumps(d, ensure_ascii=False)
    if "claude-lark 保活" in s:
        seen_reason = True
        continue
    msg = d.get("message") or {}
    if seen_reason and d.get("type") == "assistant":
        for c in msg.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text" and "waiting" in c.get("text", "").lower():
                sys.exit(0)
sys.exit(1)
PY
    }
    if [ -z "${tr}" ] || ! wait_for $TIMEOUT keepalive_answered; then
        fail "9 续命" "transcript 里没有保活回合的 waiting 回复（${tr}）"
        return
    fi
    # waiting 回合结束后的 Stop hook 不能把 waiting 发到群里
    wait_idle_bridged
    sleep 2
    if sends_since "$n0" | grep -qi waiting; then
        fail "9 续命" "fake 日志里发送了 waiting"
        return
    fi
    pass "9 续命（${WAIT_SECONDS}s 后出现 waiting 回合，未发送 waiting）"
}

s2() {
    local n0
    wait_idle_bridged
    n0=$(log_lines)
    inject "只回复 PONG"
    if wait_for $TIMEOUT sent_since "$n0" "PONG"; then
        pass "2 群消息投递（回复 PONG 已发到群里）"
    else
        fail "2 群消息投递" "fake 日志里没有含 PONG 的 +messages-send"
    fi
    wait_idle_bridged
}

perm_case() {
    # perm_case <场景名> <文件名> <回复> <期望文件存在 1/0>
    local name="$1" file="$2" reply="$3" expect="$4" n0 n1
    wait_idle_bridged
    rm -f "$PROJ/${file}"
    n0=$(log_lines)
    inject "用 Bash 执行 touch ${file}，不要做别的"
    if ! wait_for $TIMEOUT sent_since "$n0" "Claude 想执行 Bash 命令"; then
        fail "${name}" "群里没有收到权限请求"
        return
    fi
    n1=$(log_lines)
    inject "$reply"
    local ack="✅ 已允许"
    [ "$expect" = 1 ] || ack="❌ 已拒绝"
    if ! wait_for $TIMEOUT sent_since "$n1" "${ack}"; then
        fail "${name}" "群里没有收到 ${ack}"
        return
    fi
    # 等这一轮结束（Stop hook 发出回复并重新进入等待）
    wait_for $TIMEOUT sent_since "$((n1 + 1))" ""
    wait_idle_bridged
    if [ "$expect" = 1 ]; then
        if wait_for 10 test -e "$PROJ/${file}"; then
            pass "${name}（同意后 ${file} 已创建）"
        else
            fail "${name}" "同意后 ${file} 不存在"
        fi
    else
        sleep 2
        if [ -e "$PROJ/${file}" ]; then
            fail "${name}" "拒绝后 ${file} 仍被创建"
        else
            pass "${name}（拒绝后 ${file} 不存在）"
        fi
    fi
}

s3() { perm_case "3 权限同意" e2e_ok "同意" 1; }
s4() { perm_case "4 权限拒绝" e2e_no "拒绝" 0; }

s5() {
    local n0 n1
    wait_idle_bridged
    n0=$(log_lines)
    inject "用 AskUserQuestion 工具问我一个单选题：喜欢什么颜色，选项依次是 红色、绿色、蓝色。拿到答案后只回复：答案是<颜色>"
    if ! wait_for $TIMEOUT sent_since "$n0" "回复序号"; then
        fail "5 提问转发" "群里没有收到提问"
        return
    fi
    n1=$(log_lines)
    inject "2"
    if wait_for $TIMEOUT sent_since "$n1" "绿"; then
        pass "5 提问转发（回复 2 → 最终回复含 绿色）"
    else
        fail "5 提问转发" "最终回复里没有第二个选项 绿色：$(sends_since "$n1" | tr '\n' ' ')"
    fi
    wait_idle_bridged
}

s7() {
    local n0 sd
    wait_idle_bridged
    sd="$(sdir)"
    n0=$(log_lines)
    inject "只回复 STRANGER" "ou_test_stranger"
    local mid="$LAST_MID"
    sleep 15
    if grep -qF "${mid}" "$sd/inbox.jsonl" 2>/dev/null; then
        fail "7 非 owner 过滤" "非 owner 消息进了收件箱"
    elif sent_since "$n0" "STRANGER"; then
        fail "7 非 owner 过滤" "Claude 回复了非 owner 消息"
    elif grep -rqF "STRANGER" "$(ls "$HOME"/.claude/projects/*/"$(session_id)".jsonl | head -1)"; then
        fail "7 非 owner 过滤" "非 owner 消息出现在 transcript 里"
    else
        pass "7 非 owner 过滤（消息没有投递，无新回合）"
    fi
}

s6() {
    local n0
    if ! wait_idle_bridged; then
        fail "6 Esc 拿回终端" "Stop hook 不在等待"
        return
    fi
    n0=$(log_lines)
    tmux send-keys -t "$TMUX_S" Escape
    if ! wait_for 15 bash -c "! pgrep -f '$BIN hook stop'"; then
        fail "6 Esc 拿回终端" "Esc 后 Stop hook 仍在运行"
        return
    fi
    sleep 1
    send_text "只回复 LOCAL"
    if ! wait_for $TIMEOUT sent_since "$n0" "💻 终端：只回复 LOCAL"; then
        fail "6 Esc 拿回终端" "群里没有收到 💻 终端：只回复 LOCAL"
        return
    fi
    if ! wait_for $TIMEOUT sent_since "$((n0 + 1))" "LOCAL"; then
        fail "6 Esc 拿回终端" "LOCAL 的回复没有发到群里"
        return
    fi
    wait_idle_bridged
    pass "6 Esc 拿回终端（终端输入已镜像到群）"
}

s8() {
    local n0 sd
    wait_idle_bridged
    sd="$(sdir)"
    n0=$(log_lines)
    inject "/off"
    if ! wait_for $TIMEOUT test ! -e "$sd/enabled"; then
        fail "8 群里 /off" "enabled 没有消失"
        return
    fi
    if [ ! -f "$sd/binding.json" ]; then
        fail "8 群里 /off" "binding.json 被删了"
        return
    fi
    if ! wait_for $TIMEOUT sent_since "$n0" "已断开"; then
        fail "8 群里 /off" "群里没有收到 已断开"
        return
    fi
    if ! wait_for 15 bash -c "! pgrep -f '$BIN hook stop'"; then
        fail "8 群里 /off" "/off 后 Stop hook 仍在等待"
        return
    fi
    local n1
    n1=$(log_lines)
    send_text "只回复 DONE"
    done_replied() { pane | grep -qE '⏺ *DONE'; }
    if ! wait_for $TIMEOUT done_replied; then
        fail "8 群里 /off" "终端没有出现 DONE 回复"
        return
    fi
    sleep 5
    if stop_hook_waiting; then
        fail "8 群里 /off" "下一轮 Stop hook 仍在阻塞"
    elif [ "$(log_lines)" != "$n1" ]; then
        fail "8 群里 /off" "关闭后仍往群里发消息：$(sends_since "$n1" | tr '\n' ' ')"
    else
        pass "8 群里 /off（绑定保留，enabled 消失，下一轮不阻塞也不发消息）"
    fi
}

s1
if [ "$FAILED" -eq 0 ]; then
    for s in 9 2 3 4 5 7 6 8; do
        want "$s" && "s$s"
    done
fi

echo
echo "===== 结果 ====="
printf '%s\n' "${RESULTS[@]}"
echo "PASS $PASSED / $((PASSED + FAILED))"
[ "$FAILED" -eq 0 ]
