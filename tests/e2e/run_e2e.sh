#!/usr/bin/env bash
# claude-lark 端到端测试：在 tmux 里驱动真实的交互式 Claude Code，
# 挂上真实的 hooks 和 /lark 命令，用假 lark-cli 代替飞书。
#
# 用法：tests/e2e/run_e2e.sh [基目录]
#   基目录默认是仓库里（已 gitignore）的 .superpowers/e2e；每次运行在下面新建 e2e-<时间戳>-XXXX。
#   E2E_ONLY="2 3" 只跑这些场景（场景 1 总会先跑，其余场景都依赖它的绑定）。
#   E2E_MODEL 覆盖模型（默认 sonnet；haiku 不执行从飞书唤醒进来的 prompt）。临时目录不会删除，方便排查。
#
# 不碰真实的 ~/.claude/settings.json、~/.claude/commands：hooks 和 /lark 都写在临时 project 的 .claude/ 里。
# claude 以 --setting-sources project,local 启动，用户级 settings 里的 hooks（包括已安装的 claude-lark）不加载。
set -u

REPO="$(builtin cd "$(dirname "$0")/../.." && pwd)"
BIN="$REPO/bin/claude-lark"
FAKE="$REPO/tests/fake_lark_cli.py"
MODEL="${E2E_MODEL:-claude-sonnet-5-5}"
# 权限 / 提问 hook 等飞书回复的时长
WAIT_SECONDS=20
# 后台等待进程空闲多久后用保活唤醒一次
IDLE_SECONDS=30
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

# 本次运行拥有的进程：tmux pane 的全部后代（claude、hook、/lark 命令）
# 加上 pid 文件里的监听进程及其后代（监听进程用 start_new_session 脱离了 pane）。
# 只按 pid 关系认领，不按命令行模式匹配，免得误杀同一仓库里的其他进程。
PANE_PID=""
LISTENER_PID=""

descendants() {
    local p c
    for p in "$@"; do
        for c in $(pgrep -P "$p" 2>/dev/null); do
            echo "$c"
            descendants "$c"
        done
    done
}

listener_pid() {
    local pidf="$CLAUDE_LARK_HOME/listener.pid" p
    p="$(cat "$pidf" 2>/dev/null)"
    [ -n "$p" ] && LISTENER_PID="$p"
    echo "$LISTENER_PID"
}

run_pids() {
    local l
    l="$(listener_pid)"
    [ -n "$PANE_PID" ] && descendants "$PANE_PID"
    if [ -n "$l" ] && kill -0 "$l" 2>/dev/null; then
        echo "$l"
        descendants "$l"
    fi
}

alive() {
    local p
    for p in "$@"; do
        kill -0 "$p" 2>/dev/null && echo "$p"
    done
}

cleanup() {
    local pids lpid left
    # 先收集 pid 再关 tmux：claude 一死，hook 进程就被 init 收养，查不到父子关系了
    pids="$(run_pids | sort -u)"
    lpid="$(listener_pid)"
    tmux kill-session -t "$TMUX_S" 2>/dev/null
    [ -n "$lpid" ] && kill "$lpid" 2>/dev/null
    [ -n "$pids" ] && kill $pids 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        [ -z "$(alive $pids $lpid)" ] && break
        sleep 0.5
    done
    left="$(alive $pids $lpid)"
    [ -n "$left" ] && kill -9 $left 2>/dev/null && sleep 0.5
    left="$(alive $pids $lpid)"
    if [ -n "$left" ]; then
        echo "WARN: 清理后本次运行的进程仍在：" >&2
        ps -o pid=,args= -p "$(echo $left | tr ' ' ',')" >&2
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

# sent_since <起始行号> <子串>...：起始行之后有一条 +messages-send 同时含全部子串
sent_since() {
    python3 - "$FAKE_LARK_LOG" "$@" <<'PY'
import json, sys
path, start, needles = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
try:
    lines = open(path, encoding="utf-8").read().splitlines()[start:]
except OSError:
    sys.exit(1)
for line in lines:
    argv = json.loads(line)
    if argv[:2] == ["im", "+messages-send"] and "--markdown" in argv:
        body = argv[argv.index("--markdown") + 1]
        if all(n in body for n in needles):
            sys.exit(0)
sys.exit(1)
PY
}

# send_count_since <起始行号>：起始行之后 +messages-send 的条数
send_count_since() { sends_since "$1" | wc -l | tr -d ' '; }

# reply_after <起始行号> <正文>：起始行之后、正文为 <正文> 的那条之后还有别的 +messages-send
reply_after() {
    python3 - "$FAKE_LARK_LOG" "$1" "$2" <<'PY'
import json, sys
path, start, marker = sys.argv[1], int(sys.argv[2]), sys.argv[3]
seen = False
for line in open(path, encoding="utf-8").read().splitlines()[start:]:
    argv = json.loads(line)
    if argv[:2] != ["im", "+messages-send"] or "--markdown" not in argv:
        continue
    body = argv[argv.index("--markdown") + 1]
    if seen and body != marker:
        sys.exit(0)
    seen = seen or body == marker
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
# 本次 pane 的后代里后台等待进程（Stop / UserPromptSubmit 上的 asyncRewake hook）的数量
stop_hook_count() {
    local p n=0
    for p in $(descendants "$PANE_PID"); do
        # 只数 claude-lark 本身，不数外面那层 sh -c
        case "$(ps -o args= -p "$p" 2>/dev/null)" in
        *" -c "*) ;;
        *"$BIN hook stop"* | *"$BIN hook wait"*) n=$((n + 1)) ;;
        esac
    done
    echo "$n"
}
stop_hook_waiting() { [ "$(stop_hook_count)" -ge 1 ]; }
no_stop_hook() { ! stop_hook_waiting; }
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
CLAUDE_LARK_IDLE_SECONDS=${IDLE_SECONDS} \
claude --model $MODEL --permission-mode default --setting-sources project,local"

PANE_PID="$(tmux list-panes -t "$TMUX_S" -F '#{pane_pid}' | head -1)"

# 启动时可能依次出现：信任目录对话框、外部 import 对话框。只看当前屏幕，逐个处理。
screen() { tmux capture-pane -p -t "$TMUX_S" 2>/dev/null; }
end_dialogs=$((SECONDS + 60))
while [ "$SECONDS" -lt "$end_dialogs" ]; do
    cur="$(screen)"
    if grep -qF "Yes, I trust this folder" <<<"$cur"; then
        if grep -qE '❯ *(1\. *)?Yes, I trust' <<<"$cur"; then
            tmux send-keys -t "$TMUX_S" Enter
        else
            tmux send-keys -t "$TMUX_S" Down
            sleep 0.3
            tmux send-keys -t "$TMUX_S" Enter
        fi
        sleep 2
    elif grep -qF "external imports" <<<"$cur"; then
        # 默认选项是"不加载外部 import"，测试不需要它们
        tmux send-keys -t "$TMUX_S" Enter
        sleep 2
    elif grep -qF "? for shortcuts" <<<"$cur"; then
        break
    else
        sleep 1
    fi
done
# 输入框刚出现时按键可能被吞掉，多等一会儿
sleep 5

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
    # 场景 1 之后会话空闲，后台等待进程等 IDLE_SECONDS 秒后应唤醒一次保活回合
    local tr n0
    n0=$(log_lines)
    tr="$(ls "$HOME"/.claude/projects/*/"$(session_id)".jsonl 2>/dev/null | head -1)"
    keepalive_seen() { grep -qF "claude-lark 保活" "${tr}"; }
    if [ -z "${tr}" ] || ! wait_for $((IDLE_SECONDS + TIMEOUT)) keepalive_seen; then
        fail "9 保活" "transcript 里没有保活回合（${tr}）"
        return
    fi
    # 保活回合结束后，Stop hook 不能把它的回复发到群里，并重新进入等待
    sleep 5
    if ! wait_idle_bridged; then
        fail "9 保活" "保活回合之后没有等待进程"
        return
    fi
    if [ "$(send_count_since "$n0")" != 0 ]; then
        fail "9 保活" "保活回合的回复发到了群里：$(sends_since "$n0" | tr '\n' ' ')"
        return
    fi
    pass "9 保活（${IDLE_SECONDS}s 后出现保活回合，回复没有发到群里）"
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
    if ! wait_for $TIMEOUT reply_after "$n1" "${ack}"; then
        fail "${name}" "${ack} 之后 Claude 的回复没有发到群里"
        return
    fi
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
    # "答案是" 只会出现在 Claude 按要求写的最终回复里，提问消息和 hook 都不会发它
    if wait_for $TIMEOUT sent_since "$n1" "答案是" "绿色"; then
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
    # 后台在等飞书消息时，终端直接输入（不按 Esc）
    local n0
    if ! wait_idle_bridged; then
        fail "6 终端直接输入" "后台等待进程不在"
        return
    fi
    n0=$(log_lines)
    send_text "只回复 LOCAL"
    if ! wait_for $TIMEOUT sent_since "$n0" "💻 终端：只回复 LOCAL"; then
        fail "6 终端直接输入" "群里没有收到 💻 终端：只回复 LOCAL"
        return
    fi
    if ! wait_for $TIMEOUT reply_after "$n0" "💻 终端：只回复 LOCAL"; then
        fail "6 终端直接输入" "终端这一轮的回复没有发到群里"
        return
    fi
    sleep 3
    if [ "$(stop_hook_count)" != 1 ]; then
        fail "6 终端直接输入" "等待进程数是 $(stop_hook_count)，应为 1"
        return
    fi
    pass "6 终端直接输入（不按 Esc，输入已镜像，回复已发到群里，等待进程只有 1 个）"
}

s12() {
    # 终端一轮还在进行时，飞书消息到达：两边都要被处理
    local n0
    wait_idle_bridged
    n0=$(log_lines)
    send_text "写一段大约 300 字的介绍杭州的文字，最后一行只写 LOCAL2"
    sleep 2
    inject "只回复 FEISHU2"
    if ! wait_for $TIMEOUT sent_since "$n0" "LOCAL2"; then
        fail "12 终端与飞书同时输入" "群里没有收到含 LOCAL2 的回复"
        return
    fi
    if ! wait_for $TIMEOUT sent_since "$n0" "FEISHU2"; then
        fail "12 终端与飞书同时输入" "飞书消息没有被处理：$(sends_since "$n0" | tr '\n' ' ')"
        return
    fi
    wait_idle_bridged
    sleep 3
    if [ "$(stop_hook_count)" != 1 ]; then
        fail "12 终端与飞书同时输入" "等待进程数是 $(stop_hook_count)，应为 1"
        return
    fi
    pass "12 终端与飞书同时输入（两条都处理了，等待进程只有 1 个）"
}

s14() {
    # 飞书消息唤醒的回合被 Esc 打断（Stop hook 不触发），之后的飞书消息仍要投递
    local n0 n1
    wait_idle_bridged
    n0=$(log_lines)
    inject "写一篇大约 1500 字的文章介绍西湖的历史，分成 6 节"
    working() { pane | grep -qF "esc to interrupt"; }
    if ! wait_for $TIMEOUT working; then
        fail "14 Esc 打断飞书回合" "回合没有开始"
        return
    fi
    sleep 3
    tmux send-keys -t "$TMUX_S" Escape
    if ! wait_for 20 pane_has "Interrupted"; then
        fail "14 Esc 打断飞书回合" "终端没有出现 Interrupted"
        return
    fi
    sleep 3
    if ! stop_hook_waiting; then
        fail "14 Esc 打断飞书回合" "打断后没有后台等待进程"
        return
    fi
    n1=$(log_lines)
    inject "只回复 AFTER14"
    if ! wait_for $TIMEOUT sent_since "$n1" "AFTER14"; then
        fail "14 Esc 打断飞书回合" "打断后的飞书消息没有投递"
        return
    fi
    wait_idle_bridged
    pass "14 Esc 打断飞书回合（之后的飞书消息照常投递）"
}

s13() {
    # 选择题在终端作答：飞书侧收到"已在终端处理"，下一条飞书消息照常作为 prompt
    local n0 n1
    wait_idle_bridged
    n0=$(log_lines)
    inject "用 AskUserQuestion 工具问我一个单选题：喜欢什么颜色，选项依次是 红色、绿色、蓝色。拿到答案后只回复：答案是<颜色>"
    if ! wait_for $TIMEOUT sent_since "$n0" "回复序号"; then
        fail "13 选择题终端作答" "群里没有收到提问"
        return
    fi
    if ! wait_for 20 pane_has "蓝色"; then
        fail "13 选择题终端作答" "终端没有显示选项框"
        return
    fi
    sleep 1
    tmux send-keys -t "$TMUX_S" Enter
    if ! wait_for $TIMEOUT sent_since "$n0" "已在终端处理"; then
        sleep 2
        tmux send-keys -t "$TMUX_S" Enter  # 有的版本多一步提交确认
    fi
    if ! wait_for $TIMEOUT sent_since "$n0" "已在终端处理"; then
        fail "13 选择题终端作答" "群里没有收到 已在终端处理"
        return
    fi
    if ! wait_for $TIMEOUT sent_since "$n0" "答案是" "红色"; then
        fail "13 选择题终端作答" "最终回复里没有第一个选项 红色：$(sends_since "$n0" | tr '\n' ' ')"
        return
    fi
    wait_idle_bridged
    n1=$(log_lines)
    inject "只回复 AFTER13"
    if ! wait_for $TIMEOUT sent_since "$n1" "AFTER13"; then
        fail "13 选择题终端作答" "之后的飞书消息没有作为 prompt 处理"
        return
    fi
    pass "13 选择题终端作答（飞书收到 已在终端处理，之后的消息照常投递）"
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
    if ! wait_for 15 no_stop_hook; then
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
    elif [ "$(send_count_since "$n1")" != 0 ]; then
        fail "8 群里 /off" "关闭后仍往群里发消息：$(sends_since "$n1" | tr '\n' ' ')"
    else
        pass "8 群里 /off（绑定保留，enabled 消失，下一轮不阻塞也不发消息）"
    fi
}

# 桥接开着、会话空闲。桥接没开就先 /lark 重新开启。
bridged_at_terminal() {
    local sd n0
    sd="$(sdir)"
    if [ ! -e "$sd/enabled" ]; then
        n0=$(log_lines)
        send_text "/lark"
        if ! wait_for $TIMEOUT sent_since "$n0" "已连接"; then
            echo "群里没有收到 已连接"
            return 1
        fi
        if ! wait_idle_bridged; then
            echo "Stop hook 没有进入等待"
            return 1
        fi
    fi
    # 后台等待进程不占终端，直接输入即可
    sleep 1
    [ -e "$sd/enabled" ] || { echo "enabled 不存在"; return 1; }
}

s11() {
    # /compact 是同一个活着的会话：SessionStart(source=compact) 不能断开桥接
    local sd why sid0
    sd="$(sdir)"
    sid0="$(session_id)"
    if ! why="$(bridged_at_terminal)"; then
        fail "11 /compact 保留桥接" "$why"
        return
    fi
    send_text "/compact"
    compacted() { pane | grep -qi "compacted"; }
    if ! wait_for 180 compacted; then
        fail "11 /compact 保留桥接" "终端没有出现 Compacted"
        return
    fi
    sleep 3
    if [ ! -e "$sd/enabled" ]; then
        fail "11 /compact 保留桥接" "compact 之后 enabled 消失了"
    elif [ "$(ls "$CLAUDE_LARK_HOME/sessions" | wc -l | tr -d ' ')" != 1 ] || [ "$(session_id)" != "$sid0" ]; then
        fail "11 /compact 保留桥接" "出现了别的 session 目录"
    else
        pass "11 /compact 保留桥接（enabled 仍在）"
    fi
}

s10() {
    # /clear 后旧会话的 SessionEnd 断开桥接并告知群里
    local sd why n0
    sd="$(sdir)"
    if ! why="$(bridged_at_terminal)"; then
        fail "10 /clear 断开桥接" "$why"
        return
    fi
    n0=$(log_lines)
    send_text "/clear"
    if ! wait_for 30 test ! -e "$sd/enabled"; then
        fail "10 /clear 断开桥接" "/clear 后 enabled 仍在"
        return
    fi
    if ! wait_for 30 sent_since "$n0" "会话已结束（/clear 或退出），桥接已断开。终端里敲 /lark 可重新连接"; then
        fail "10 /clear 断开桥接" "群里没有收到 会话已结束 的通知：$(sends_since "$n0" | tr '\n' ' ')"
        return
    fi
    if [ ! -f "$sd/binding.json" ]; then
        fail "10 /clear 断开桥接" "binding.json 被删了"
        return
    fi
    pass "10 /clear 断开桥接（enabled 消失，群里收到通知，绑定保留）"
}

s1
if [ "$FAILED" -eq 0 ]; then
    for s in 9 2 3 4 5 13 14 7 6 12 8 11 10; do
        want "$s" && "s$s"
    done
fi

echo
echo "===== 结果 ====="
printf '%s\n' "${RESULTS[@]}"
echo "PASS $PASSED / $((PASSED + FAILED))"
[ "$FAILED" -eq 0 ]
