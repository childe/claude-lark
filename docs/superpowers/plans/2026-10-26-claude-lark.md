# claude-lark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户在运行中的 Claude Code 会话里敲 `/lark`，把会话桥接到一个飞书群：群里的消息变成 prompt，最终回复、权限确认和选择题都在群里收发。

**Architecture:** 一个 Python 包 `lark_bridge`，入口是 `bin/claude-lark`。Claude Code 的 hooks（Stop / PermissionRequest / PreToolUse(AskUserQuestion) / UserPromptSubmit）调用它；全机唯一的监听进程跑 `lark-cli event consume`，把消息写入每个会话的收件箱；阻塞中的 hook 从收件箱读消息。飞书收发全部通过 `lark-cli` 子进程，测试时替换成假的 `lark-cli`。

**Tech Stack:** Python ≥ 3.9，运行时只用标准库；测试用 pytest（`uv run --with pytest pytest`）；格式化用 black；`lark-cli` ≥ 1.0.92；tmux（只用于端到端测试）。

**Spec:** `docs/superpowers/specs/2026-10-26-claude-lark-design.md`。实现者必须先读 spec 第 5 节"已验证的事实"。

## Global Constraints

- 运行时只用 Python 标准库；不引入任何第三方运行时依赖。
- 没开启桥接的会话里，每个 hook 必须 exit 0、stdout 为空，耗时 < 100ms。
- 权限请求永不默认同意：hook 出错、超时、收到无法识别的回复，都不能输出 `allow`。
- 只处理绑定者（`config.json` 里的 `owner_open_id`）发来的消息。
- 发飞书消息一律 `--as bot`。
- 仓库是 GitHub 公开仓库：任何提交不得包含 app id（`cli_` 开头）、open_id（`ou_`）、chat_id（`oc_`）、token、公司名和内部域名、本机绝对路径（`/Users/`）。公司相关关键词**不得出现在仓库里，包括扫描脚本本身**：它们放在本地不提交的 `.secret-patterns` 文件里（已 gitignore），实现者向用户索取内容。测试数据用明显的假值，例如 `ou_test_owner`、`oc_test_chat`（扫描规则对 `_test_` 放行）。
- git 提交身份是 `childe <rmself@qq.com>`（仓库已配置），提交信息以 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` 结尾。
- 每次改代码后运行 `black .`（有 venv 用 venv 的，否则 `/opt/homebrew/bin/black`）。
- 用户可见文案用中文。
- 状态根目录：环境变量 `CLAUDE_LARK_HOME`，默认 `~/.claude/lark`。`lark-cli` 可执行文件：环境变量 `CLAUDE_LARK_CLI`，默认 `lark-cli`。测试通过这两个环境变量隔离。
- 固定数值：hook `timeout` = `3600`；hook 内部等待截止 = 启动后 `3540` 秒（可用环境变量 `CLAUDE_LARK_WAIT_SECONDS` 覆盖，仅测试用）；`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` = `"1000"`；续命提醒阈值 = 连续 block 第 `950` 次；单条飞书消息正文最长 `4000` 字符；群名最长 `60` 字符；自动总结的群名建议 ≤ `20` 字。

## Review Focus

1. **Claude 干活期间、或用户按 Esc 之后，群里来了消息**：消息必须进收件箱，在下一次 Stop 时投递，一条不丢。→ Task 4 `test_stop_delivers_backlog_queued_before_hook_started`。
2. **hook 在读收件箱时被 SIGTERM（用户按 Esc）**：已投递的不重复，未投递的不丢。→ Task 2 `test_cursor_not_advanced_without_commit`，Task 4 `test_stop_sigterm_leaves_inbox_unconsumed`。
3. **权限确认收到模糊回复**（"好的吧"、"嗯"、空串）：不能当作同意，要提示重答并继续等待。→ Task 3 `test_parse_permission_ambiguous_is_none`，Task 5 `test_permission_ambiguous_reply_keeps_waiting`。
4. **群里有非绑定者发言，或机器人自己发的消息被回推**：一律丢弃。→ Task 6 `test_listener_drops_non_owner_and_bot`。
5. **非桥接会话、stdin 不是合法 JSON、状态目录不存在**：hook 静默放行。→ Task 4 `test_hooks_noop_when_not_enabled`、`test_hooks_noop_on_garbage_stdin`。

---

## 文件结构

```
bin/claude-lark                 # 入口：sys.path 指向仓库根，调用 lark_bridge.cli.main
lark_bridge/
  __init__.py
  paths.py        # 状态目录与文件路径
  state.py        # 绑定、开关、收件箱/游标、计数器、占用检查
  text.py         # 回复解析、消息模板、分段
  feishu.py       # lark-cli 子进程封装
  procs.py        # 找 claude 祖先进程 pid、pid 存活检查
  hooks.py        # 四个 hook 处理函数 + 等待循环
  listener.py     # 全机唯一监听进程
  commands.py     # /lark 的 on/off/status/bind 与 slash 参数解析
  settings.py     # settings.json 合并/移除（安装用）
  cli.py          # argparse 分发 + doctor
commands/lark.md
install.sh / uninstall.sh
scripts/scan-secrets.sh         # 敏感信息扫描，pre-commit 调用
tests/
  conftest.py     # tmp CLAUDE_LARK_HOME、假 lark-cli fixture
  fake_lark_cli.py
  test_*.py
  e2e/run_e2e.sh  # tmux 驱动真实 claude + 假 lark-cli
README.md
```

---

### Task 0: 真实飞书可行性验证（U1）

不写产品代码。会在飞书上真实建一个群，**执行前要用户确认**。

- [ ] **Step 1**：向用户说明将新建一个测试群 `claude-lark 测试`（成员：用户本人 + 机器人），取得确认。
- [ ] **Step 2**：`lark-cli im +chat-create --as bot --name "claude-lark 测试" --users <owner_open_id> --set-bot-manager`，记录 chat_id（只记在会话里，不写进仓库）。
- [ ] **Step 3**：后台运行 `lark-cli event consume im.message.receive_v1 --as bot --timeout 3m`，请用户在群里发一条**不 @ 机器人**的消息、再发一条 @ 机器人的消息。
- [ ] **Step 4**：记录结论：不 @ 的消息能否收到；`content` 里 @ 部分长什么样（Task 6 要剥离）；`sender_type` 的取值；机器人自己发的消息是否也会推回来。结论写进 spec 第 5 节 F14，U1 标为已验证。如果不 @ 收不到，在 spec 第 3 节加一条限制"每条消息需 @机器人"，并检查应用是否能开通"获取群组中所有消息"权限，把开通方法写进 README 素材。
- [ ] **Step 5**：提交 spec 修改。

### Task 1: 项目骨架、路径、敏感信息扫描

**Files:** Create `bin/claude-lark`, `lark_bridge/__init__.py`, `lark_bridge/paths.py`, `lark_bridge/cli.py`（只有骨架）, `scripts/scan-secrets.sh`, `.gitignore`, `pyproject.toml`（只放 black/pytest 配置）, `tests/conftest.py`, `tests/test_paths.py`

**Interfaces — Produces:**
- `paths.home() -> Path`：`CLAUDE_LARK_HOME` 或 `~/.claude/lark`。
- `paths.session_dir(session_id: str) -> Path`：`home()/sessions/<session_id>`；session_id 只允许 `[0-9a-f-]`，否则 `ValueError`。
- `paths.config_file()`, `paths.listener_pid_file()`, `paths.listener_log()` → `home()/config.json`、`home()/listener.pid`、`home()/listener.log`。
- `paths.lark_cli() -> str`：`CLAUDE_LARK_CLI` 或 `"lark-cli"`。
- conftest fixture `lark_home(tmp_path, monkeypatch)`：设置 `CLAUDE_LARK_HOME`，写入 `config.json` = `{"owner_open_id": "ou_test_owner"}`。

- [ ] **Step 1**：写 `tests/test_paths.py`：`test_home_respects_env`、`test_session_dir_rejects_traversal`（`"../x"` → ValueError）。
- [ ] **Step 2**：`uv run --with pytest pytest tests/test_paths.py -v`，预期 FAIL（模块不存在）。
- [ ] **Step 3**：实现 `paths.py`；`bin/claude-lark` 把自身所在仓库根（`Path(__file__).resolve().parent.parent`）加入 `sys.path` 后调用 `lark_bridge.cli.main(sys.argv[1:])`；`cli.main` 暂时只认 `--version`。
- [ ] **Step 4**：测试 PASS。
- [ ] **Step 5**：写 `scripts/scan-secrets.sh`：对 `git diff --cached` 的新增行（或传入的文件）用 grep -E 匹配通用规则 `cli_[a-z0-9]{8,}|ou_[0-9a-f]{8,}|oc_[0-9a-f]{8,}|/Users/|/home/[a-z]|app_secret|tenant_access_token`，再加上仓库根目录 `.secret-patterns`（存在时，每行一个正则，gitignore）里的规则，命中则打印并 exit 1。安装为 `.git/hooks/pre-commit`（README 的开发者章节说明；本地执行 `ln -s ../../scripts/scan-secrets.sh .git/hooks/pre-commit`）。
- [ ] **Step 6**：验证：在 scratchpad 建文件写入 `ou_0123456789abcdef`，`scripts/scan-secrets.sh <file>` 预期 exit 1；写入 `ou_test_owner` 预期 exit 0。
- [ ] **Step 7**：black，提交 `feat: project skeleton and secret scan`。

### Task 2: 会话状态（state.py）

**Files:** Create `lark_bridge/state.py`, `tests/test_state.py`

**Interfaces — Produces:**
- `@dataclass Binding(session_id: str, chat_id: str, chat_name: str, cwd: str, claude_pid: int | None, created_at: float)`
- `load_binding(sid) -> Binding | None`；`save_binding(b: Binding)`（原子写：写临时文件再 `os.replace`）。
- `is_enabled(sid) -> bool`；`set_enabled(sid, on: bool, claude_pid: int | None = None)`。开启时 `enabled` 文件内容是 claude_pid；`is_enabled` 在文件存在且该 pid 存活（或 pid 为空）时才返回 True，pid 已死则删除文件并返回 False。这保证 claude 重启后 `--resume` 不会自动接上飞书（spec 第 3 节）。
- `all_bindings() -> list[Binding]`；`find_enabled_by_chat(chat_id) -> Binding | None`。
- `append_inbox(sid, msg: dict)`：一行一个 JSON，`fcntl.flock` 加锁；`msg` 至少含 `message_id`、`text`、`create_time`；同一 `message_id` 已存在则跳过。
- `peek_inbox(sid) -> tuple[list[dict], int]`：返回游标之后的完整行和读到的新偏移；**不移动游标**。
- `commit_cursor(sid, offset: int)`：原子写游标。
- `get_counter(sid, name) -> int`、`set_counter(sid, name, v)`；用到的计数器名：`"blocks"`（连续 block 次数）、`"keepalive_pending"`（0/1，上一次 block 是否续命）。
- `chat_occupant(chat_id, except_sid) -> Binding | None`：别的**已开启且 claude_pid 存活**的会话占用该群则返回它。存活判断调用 `procs.pid_alive`（Task 2 一并实现 `lark_bridge/procs.py`：`pid_alive(pid: int | None) -> bool`，`find_claude_ancestor() -> int | None` 沿 `ps -o ppid=,comm=` 向上找 comm 以 `claude` 结尾的进程）。

- [ ] **Step 1**：写测试：`test_binding_roundtrip`、`test_enable_toggle`、`test_enabled_false_when_claude_pid_dead`（set_enabled(sid, True, 999999) → is_enabled 为 False 且文件被删）、`test_inbox_dedup_by_message_id`、`test_cursor_not_advanced_without_commit`（peek 两次返回相同消息；commit 后再 peek 为空）、`test_peek_ignores_partial_trailing_line`（文件末尾没有换行的半行不返回，游标不越过它）、`test_chat_occupant_ignores_dead_pid`（claude_pid=999999 → None）、`test_chat_occupant_returns_live`（claude_pid=os.getpid() → 返回）。
- [ ] **Step 2**：运行，预期 FAIL。
- [ ] **Step 3**：实现 `state.py`、`procs.py`。
- [ ] **Step 4**：运行，预期 PASS。
- [ ] **Step 5**：black，提交 `feat: session state, inbox and cursor`。

### Task 3: 文本解析与消息模板（text.py）

**Files:** Create `lark_bridge/text.py`, `tests/test_text.py`

**Interfaces — Produces:**
- `parse_permission(reply: str) -> bool | None`：去首尾空白、小写后，属于 `{"同意","允许","1","y","yes","ok"}` → True；属于 `{"拒绝","2","n","no"}` → False；其他 → None。
- `parse_answers(reply: str, questions: list[dict]) -> dict[str, str]`：只有一个问题时，回复是用 `,`/`，`/`、`/空格分隔的序号且都在 1..len(options) 内 → 对应 label 用 `", "` 连接（单选题只取第一个）；否则整段回复作为自由文本答案。多个问题时，按行拆分，第 i 行回答第 i 题，行数不够的题答 `""`（Claude 会再问）。键是 `question` 原文。
- `format_permission(tool_name: str, tool_input: dict) -> str`：Bash 显示 `description` 和代码块包裹的 `command`；其他工具显示代码块包裹的 `json.dumps(tool_input, ensure_ascii=False, indent=2)`（截断到 1500 字符）；结尾固定一行 `回复「同意」或「拒绝」（1 / 2）`。
- `format_questions(questions: list[dict]) -> str`：每题标题 + 编号选项（`1. label — description`）；结尾说明"回复序号，多选用逗号分隔；也可以直接回复文字"。
- `split_message(text: str, limit: int = 4000) -> list[str]`：优先在换行处切；不在 ``` 代码块中间切，必须切时在前段末尾补 ``` 并在后段开头补 ```。
- `strip_mentions(text: str) -> str`：去掉 Task 0 结论里 @ 机器人的文本形式。
- 常量：`KEEPALIVE_REASON = "（claude-lark 保活）没有新的飞书消息。只回复 waiting 这一个词，不要调用任何工具，不要做其他事。"`；`DELIVER_PREFIX = "以下是用户在飞书群里发来的消息：\n\n"`；多条消息用 `"\n\n---\n\n"` 连接。

- [ ] **Step 1**：写测试：`test_parse_permission_yes`（"同意"、" Y "、"1"）、`test_parse_permission_no`、`test_parse_permission_ambiguous_is_none`（"好的吧"、"嗯"、""、"同意吗"）、`test_parse_answers_index`（"2" → "Blue"）、`test_parse_answers_multiselect`（"1，2" → "Red, Blue"）、`test_parse_answers_out_of_range_is_freetext`（"9" → "9"）、`test_parse_answers_freetext`、`test_split_message_keeps_code_fences_balanced`（每段 ``` 个数为偶数）、`test_split_message_short_untouched`、`test_format_permission_bash_contains_command`。
- [ ] **Step 2**：运行，预期 FAIL。
- [ ] **Step 3**：实现。
- [ ] **Step 4**：运行，预期 PASS。
- [ ] **Step 5**：black，提交 `feat: reply parsing and message templates`。

### Task 4: lark-cli 封装 + Stop / UserPromptSubmit hook

**Files:** Create `lark_bridge/feishu.py`, `lark_bridge/hooks.py`, `tests/fake_lark_cli.py`, `tests/test_hooks_stop.py`; Modify `lark_bridge/cli.py`

**Interfaces:**
- Consumes: Task 2 全部，Task 3 的 `split_message`、`KEEPALIVE_REASON`、`DELIVER_PREFIX`。
- Produces:
  - `feishu.send(chat_id: str, markdown: str) -> None`：对 `split_message` 的每段调用 `lark-cli im +messages-send --as bot --chat-id <id> --markdown <段>`；失败时写 `home()/errors.log`，不抛出（hook 不能因为发送失败而卡死会话）。
  - `feishu.create_chat(name: str, owner_open_id: str) -> str`：`lark-cli im +chat-create --as bot --name <name[:60]> --users <owner> --set-bot-manager`，返回 chat_id；失败抛 `FeishuError(str)`。
  - `feishu.owner_open_id() -> str`：解析 `lark-cli auth status` 的 `identities.user.openId`。
  - `hooks.run(event: str, stdin_text: str, now=time.monotonic, sleep=time.sleep) -> str`：返回要写到 stdout 的文本（可能为空）。`event ∈ {"stop","permission","ask","prompt"}`。任何异常 → 记 `errors.log`，返回 `""`。
  - `hooks.wait_for_messages(sid, deadline: float, now, sleep) -> list[dict] | None`：每 0.5 秒 peek；有消息返回（不 commit）；到 deadline 返回 None。
  - `cli.main` 支持 `hook <event>`：读 stdin，调用 `hooks.run`，打印结果，exit 0。
  - `tests/fake_lark_cli.py`：可执行脚本；把每次调用的 argv 以 JSON 追加到 `$FAKE_LARK_LOG`；`+chat-create` 返回 `{"data":{"chat_id":"oc_test_chat"}}`；`auth status` 返回 owner `ou_test_owner`；`event consume` 逐行输出 `$FAKE_LARK_EVENTS` 文件内容后阻塞。

Stop hook 行为（`event="stop"`）：
1. 未开启 → 返回 `""`。
2. 若 `keepalive_pending == 1`：不发送 `last_assistant_message`，把它置 0；否则把 `last_assistant_message`（为 null 或空则跳过）发到群里。
3. `wait_for_messages(deadline = 启动时刻 + WAIT_SECONDS)`，`WAIT_SECONDS` 取 `CLAUDE_LARK_WAIT_SECONDS`，默认 3540；Task 5 的两个 hook 同样使用它。
4. 有消息：commit 游标，`blocks += 1`，返回 `{"decision":"block","reason": DELIVER_PREFIX + 合并文本}`。
5. 超时：若 `blocks + 1 >= 1000`，发"续命次数已用完，会话已空闲，回到电脑前继续"，返回 `""`；否则 `blocks += 1`、`keepalive_pending = 1`；`blocks == 950` 时发一次提醒"剩余续命次数不多"；返回 `{"decision":"block","reason": KEEPALIVE_REASON}`。
6. SIGTERM（`signal.signal` 注册，抛出自定义异常跳出循环）：不 commit 游标，返回 `""`。

UserPromptSubmit（`event="prompt"`）：未开启 → `""`；开启 → `blocks = 0`，若 prompt 不以 `/lark` 开头则发 `"💻 终端：" + prompt`；返回 `""`（不阻塞、不改 prompt）。

- [ ] **Step 1**：写测试（用 fake lark-cli + 注入的 `now`/`sleep` 推进虚拟时间）：`test_hooks_noop_when_not_enabled`（四个 event 都返回 ""，且 fake 日志为空）、`test_hooks_noop_on_garbage_stdin`、`test_stop_sends_reply_then_delivers_message`、`test_stop_delivers_backlog_queued_before_hook_started`（两条消息提前写入收件箱 → reason 包含两条，按 create_time 顺序）、`test_stop_keepalive_on_deadline`、`test_stop_skips_sending_keepalive_reply`（keepalive_pending=1 时 "waiting" 不发送）、`test_stop_gives_up_at_block_cap`、`test_stop_sigterm_leaves_inbox_unconsumed`（在 `sleep` 里触发 SIGTERM 处理函数 → 返回 ""，之后 peek 仍能拿到消息）、`test_prompt_mirrors_terminal_input_and_resets_blocks`、`test_prompt_does_not_mirror_lark_command`、`test_send_failure_does_not_raise`（fake 返回非 0）。
- [ ] **Step 2**：运行，预期 FAIL。
- [ ] **Step 3**：实现 `feishu.py`、`hooks.py` 的 stop/prompt 分支、`cli.py` 的 `hook` 子命令。
- [ ] **Step 4**：运行，预期 PASS。
- [ ] **Step 5**：测量非桥接会话的开销：`time (echo '{"session_id":"00000000-0000-0000-0000-000000000000"}' | bin/claude-lark hook stop)`，预期 real < 0.1s。超过就把 `bin/claude-lark` 改成"先检查 enabled 文件、再 import 其余模块"。
- [ ] **Step 6**：black，提交 `feat: stop and prompt hooks`。

### Task 5: PermissionRequest 与 AskUserQuestion hook

**Files:** Modify `lark_bridge/hooks.py`; Create `tests/test_hooks_interactive.py`

**Interfaces:** Consumes `parse_permission`、`parse_answers`、`format_permission`、`format_questions`、`wait_for_messages`。

PermissionRequest（`event="permission"`）：
1. 未开启 → `""`。
2. 发 `format_permission(tool_name, tool_input)`。
3. 循环：等消息；拿到后逐条处理并 commit 到这一条为止：`parse_permission` 为 True/False → 发"✅ 已允许"/"❌ 已拒绝"，返回 `{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"|"deny","message":"用户在飞书上拒绝了"}}}`（allow 时不带 message）；为 None → 发"请回复「同意」或「拒绝」"，继续等。
4. 截止 3540 秒到 → 发"等待超时，已交回终端"，返回 `""`。
5. SIGTERM（用户在终端处理了确认框，U3）→ 发"已在终端处理"，返回 `""`。不 commit 未处理的消息。

AskUserQuestion（`event="ask"`，PreToolUse）：
1. 未开启 → `""`。
2. 发 `format_questions(tool_input["questions"])`。
3. 等到第一条消息，commit，返回 `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","updatedInput": {**tool_input, "answers": parse_answers(...)}}}`。
4. 超时 / SIGTERM → 发"已交回终端"，返回 `""`。

- [ ] **Step 1**：写测试：`test_permission_allow`、`test_permission_deny`、`test_permission_ambiguous_reply_keeps_waiting`（先"好的吧"再"同意" → 发出提示且最终 allow，"好的吧"已被 commit）、`test_permission_timeout_returns_empty`（结果中绝不出现 `"allow"`）、`test_permission_sigterm_returns_empty`、`test_permission_exception_never_allows`（让 `format_permission` 抛异常 → `""`）、`test_ask_fills_answers`（"2" → `answers == {"Which color?": "Blue"}`）、`test_ask_timeout_returns_empty`。
- [ ] **Step 2**：运行，预期 FAIL。
- [ ] **Step 3**：实现。
- [ ] **Step 4**：运行，预期 PASS。
- [ ] **Step 5**：black，提交 `feat: permission and question relay`。

### Task 6: 全机监听进程

**Files:** Create `lark_bridge/listener.py`, `tests/test_listener.py`; Modify `lark_bridge/cli.py`

**Interfaces — Produces:**
- `listener.handle_event(ev: dict, owner: str) -> None`：
  - `sender_id != owner` 或 `sender_type` 不是用户（取值按 Task 0 结论）→ 丢弃。
  - `find_enabled_by_chat(chat_id)` 为空 → 若该 chat_id 有任何绑定（未开启），发"当前没有会话在桥接，回到电脑前在终端敲 /lark"；否则忽略。
  - 文本 `strip_mentions` 后为 `/off` → `set_enabled(sid, False)`，发"已断开，终端里敲 /lark 可重新连接"。
  - 其他 → `append_inbox(sid, {"message_id","text","create_time"})`。
- `listener.ensure_running() -> None`：读 `listener.pid`，进程活着就返回；否则 `subprocess.Popen([sys.executable, bin/claude-lark, "listen"], start_new_session=True, stdout/stderr → listener.log)`。
- `listener.main() -> int`（`claude-lark listen`）：用 `O_EXCL` 写 pid 文件保证唯一（已存在且进程存活 → 退出 0）；启动 `lark-cli event consume im.message.receive_v1 --as bot`，逐行解析 stdout 调 `handle_event`；后台线程每 30 秒检查：对所有已开启绑定，`claude_pid` 已死的 → `set_enabled(False)`；没有已开启绑定 → 终止子进程、删 pid 文件、退出。子进程意外退出 → 等 5 秒重启，日志记一行。

- [ ] **Step 1**：写测试：`test_listener_routes_to_enabled_session`、`test_listener_drops_non_owner_and_bot`、`test_listener_off_command_disables`、`test_listener_notifies_when_chat_not_enabled`、`test_listener_strips_mention`、`test_listener_main_exits_when_no_enabled_sessions`（fake lark-cli + 检查间隔通过参数缩短到 0.1 秒）、`test_listener_single_instance`（pid 文件指向存活进程 → main 立即返回 0）。
- [ ] **Step 2**：运行，预期 FAIL。
- [ ] **Step 3**：实现；`cli.main` 支持 `listen`。
- [ ] **Step 4**：运行，预期 PASS。
- [ ] **Step 5**：black，提交 `feat: machine-wide event listener`。

### Task 7: /lark 命令

**Files:** Create `lark_bridge/commands.py`, `commands/lark.md`, `tests/test_commands.py`; Modify `lark_bridge/cli.py`

**Interfaces — Produces:**
- `commands.slash(sid: str, args: str) -> str`：返回给 Claude 看的文本，首行是协议标记：
  - `off` → 关闭，发"已断开"，返回 `RESULT: 已关闭桥接（群 <name> 仍保留绑定）`。
  - `status` → `RESULT: <绑定群名/是否开启/监听进程是否存活>`。
  - 参数以 `oc_` 开头或匹配 `https://*.feishu.cn/*` 群链接 → 已有绑定则 `RESULT: 当前会话已绑定群 <name>，先 /lark off 再换绑`；`chat_occupant` 非空 → `RESULT: 群已被会话 <sid[:8]>（<cwd>）占用`；否则绑定、开启。链接解析 chat_id 的方式在实现时查 `lark-cli im chats link --help`，解析不了就报错要求用户给 chat_id。
  - 已有绑定（且非上一条）→ 开启，参数非空时附加"已有绑定群 <name>，忽略群名参数"。
  - 无绑定、参数为群名 → 建群、绑定、开启。
  - 无绑定、无参数 → 返回 `NEED_NAME`。
  - 每次开启：`claude_pid = find_claude_ancestor()`，写入 binding 并 `set_enabled(sid, True, claude_pid)`，检查 `chat_occupant`，`ensure_running()`，往群里发"已连接：<cwd>，会话 <sid[:8]>"，返回文本附带提示"离开前就绪；回到电脑前按 Esc 拿回终端，/lark off 停止同步"。
- `commands.on(sid: str, name: str) -> str`：`NEED_NAME` 之后由 Claude 调用，等价于"无绑定、参数为群名"。
- `cli.main` 支持 `slash --session <sid> --args <str>` 和 `on --session <sid> --name <str>`。
- `commands/lark.md`：

````markdown
---
description: 把当前会话桥接到飞书群（/lark [群名|chat_id|off|status]）
allowed-tools: Bash(~/.claude/lark/app/bin/claude-lark:*)
---
!`~/.claude/lark/app/bin/claude-lark slash --session ${CLAUDE_SESSION_ID} --args "$ARGUMENTS"`

如果上面的输出以 `RESULT:` 开头：把它原样转述给用户，不要做其他事。
如果上面的输出是 `NEED_NAME`：根据本会话到目前为止的内容，总结一个不超过 20 个字的中文群名，概括这个会话在做什么；然后用 Bash 执行
`~/.claude/lark/app/bin/claude-lark on --session ${CLAUDE_SESSION_ID} --name "<群名>"`，把输出转述给用户。
````

- [ ] **Step 1**：写测试：`test_slash_need_name_when_unbound`、`test_slash_creates_group_with_given_name`（fake 日志里 `--name` 等于参数，且 `--users ou_test_owner`）、`test_slash_existing_binding_ignores_name`、`test_slash_rebind_with_chat_id_refused`、`test_slash_bind_occupied_chat_refused`、`test_slash_off_keeps_binding`、`test_on_truncates_name_to_60`、`test_slash_args_with_quotes`（`args='a "b" c'` 作为群名原样使用）。
- [ ] **Step 2**：运行，预期 FAIL。
- [ ] **Step 3**：实现 `commands.py`、`commands/lark.md`、cli 分发。
- [ ] **Step 4**：运行，预期 PASS。
- [ ] **Step 5**：black，提交 `feat: /lark command`。

### Task 8: 安装、卸载、doctor

**Files:** Create `lark_bridge/settings.py`, `install.sh`, `uninstall.sh`, `tests/test_settings.py`; Modify `lark_bridge/cli.py`

**Interfaces — Produces:**
- `settings.HOOK_MARK = "claude-lark"`：每条注册的 hook command 都包含路径 `~/.claude/lark/app/bin/claude-lark`，以此识别"本项目加的条目"。
- `settings.merge(cfg: dict) -> dict`：加入
  - `hooks.Stop`、`hooks.PermissionRequest`（matcher `"*"`）、`hooks.PreToolUse`（matcher `"AskUserQuestion"`）、`hooks.UserPromptSubmit`，各一条 `{"type":"command","command":"$HOME/.claude/lark/app/bin/claude-lark hook <event>","timeout":3600}`（prompt 那条 timeout 用 30）；
  - `env.CLAUDE_CODE_STOP_HOOK_BLOCK_CAP = "1000"`。
  - 已存在的本项目条目先移除再加，保证幂等；其他条目原样保留，顺序不变。
- `settings.remove(cfg: dict) -> dict`：只移除本项目条目；移除后空的 matcher 组和空的事件键一并删除；`env` 里只删这一个键。
- `cli doctor`：逐项检查并打印 ✅/❌ 和修复命令：python 版本；`lark-cli` 存在且版本 ≥ 1.0.92；`auth status` 中 bot 与 user 均 ready；`event consume im.message.receive_v1 --as bot --dry-run` 的 preconditions 全 ok；`~/.claude/commands/lark.md` 存在；settings.json 已包含本项目 hooks；`config.json` 的 owner 与当前 `auth status` 一致；监听进程状态。全部通过 exit 0，否则 exit 1。
- `install.sh`（`set -euo pipefail`）：检查 python3 ≥ 3.9 和 lark-cli；`mkdir -p ~/.claude/lark ~/.claude/commands`；`ln -sfn <仓库绝对路径> ~/.claude/lark/app`；`ln -sf ~/.claude/lark/app/commands/lark.md ~/.claude/commands/lark.md`；备份 `~/.claude/settings.json` 到 `settings.json.bak.<时间戳>`，用 `python3 -c` 调 `settings.merge` 写回；用 `feishu.owner_open_id()` 写 `config.json`；运行 `claude-lark doctor`；最后打印"已经开着的 claude 会话需要重启一次才能使用 /lark"。
- `uninstall.sh`：`/lark off` 所有会话（停监听进程）、备份并 `settings.remove`、删两个软链接；`~/.claude/lark/sessions` 询问后再删（`--purge` 参数直接删）。

- [ ] **Step 1**：写测试：`test_merge_into_empty`、`test_merge_idempotent`（merge 两次结果相同）、`test_merge_preserves_foreign_hooks`（已有一个别的 Stop hook 和 superpowers 的 SessionStart → 都保留）、`test_remove_only_ours`、`test_remove_cleans_empty_groups`、`test_merge_then_remove_roundtrip`（等于原始配置）。
- [ ] **Step 2**：运行，预期 FAIL。
- [ ] **Step 3**：实现 `settings.py`、doctor、两个脚本。
- [ ] **Step 4**：运行，预期 PASS。
- [ ] **Step 5**：在临时 HOME 下验证脚本：`HOME=<scratchpad>/fakehome CLAUDE_LARK_CLI=<repo>/tests/fake_lark_cli.py ./install.sh` 两次，`diff` 两次产生的 settings.json 无差异；再 `./uninstall.sh --purge`，settings.json 回到安装前内容。
- [ ] **Step 6**：black，提交 `feat: install, uninstall and doctor`。

### Task 9: 端到端测试（tmux + 真实 claude + 假 lark-cli）

**Files:** Create `tests/e2e/run_e2e.sh`, `tests/e2e/README.md`

做法照搬 spec 第 5 节的试验：在 scratchpad 临时目录里建 project 级 `.claude/settings.json`（内容由 `settings.merge({})` 生成，env 里加 `CLAUDE_LARK_HOME` 和 `CLAUDE_LARK_CLI` 指向假实现）和 `.claude/commands/lark.md`；`tmux new-session ... "claude --model claude-haiku-4-5-20251001 --permission-mode default"`；用往 `$FAKE_LARK_EVENTS` 追加事件的方式模拟群消息。每个场景打印 PASS/FAIL，脚本最后 kill 掉 tmux 会话和监听进程。

场景（全部要 PASS）：
1. `/lark e2e测试` → fake 日志出现 `+chat-create --name e2e测试`；回复里有"已连接"。（同时验证 `allowed-tools` 的 `~` 路径写法能用；不能用就改成 `$HOME` 绝对路径或 `Bash(*claude-lark*)` 并回写 Task 7。）
2. 模拟群消息"只回复 PONG" → fake 日志里出现内容含 `PONG` 的 `+messages-send`。
3. 模拟群消息"用 Bash 执行 touch e2e_ok"→ 出现权限消息 → 模拟"同意" → 文件存在。
4. 同 3，模拟"拒绝"→ 文件不存在。
5. 模拟群消息让 Claude 用 AskUserQuestion 问颜色 → 模拟"2" → 最终回复含第二个选项。
6. 在 Stop hook 等待时发 Escape → 终端可输入；终端输入"只回复 LOCAL" → fake 日志出现 `💻 终端：只回复 LOCAL`。
7. 非 owner 的事件 → 没有被投递（Claude 没有新回合）。
8. 群里 `/off` → 绑定保留、`enabled` 消失，下一轮 Stop hook 不再阻塞。
9. 续命：设 `CLAUDE_LARK_WAIT_SECONDS=20` → 20 秒后出现一次 waiting 回合，fake 日志里没有发送 "waiting"。

- [ ] **Step 1**：写脚本。
- [ ] **Step 2**：运行 `tests/e2e/run_e2e.sh`，预期全部 PASS；失败则修代码（回到对应 Task 补单元测试）。
- [ ] **Step 3**：black，提交 `test: tmux end-to-end suite`。

### Task 10: README

**Files:** Create `README.md`（英文为主，附中文摘要）, `LICENSE`（MIT）

必须包含的章节，按顺序：
1. What it is：一段话 + 一张 ASCII 交互示意（终端 ↔ hooks ↔ 监听进程 ↔ 飞书群）。
2. Prerequisites：可逐项检查的清单——macOS/Linux、Python ≥ 3.9、Claude Code ≥ 2.1.287、`npm i -g @larksuite/cli` 且 ≥ 1.0.92、飞书开放平台自建应用：开启机器人能力；权限 scope 清单（以 Task 0 实测和 `event schema` 的 Required Scopes 为准，至少 `im:message.p2p_msg:readonly`、群消息接收权限、`im:message:send_as_bot`、`im:chat:create`）；事件订阅 `im.message.receive_v1`（长连接方式）；`lark-cli auth login` 让 bot 和 user 身份都 ready。每项附检查命令和预期输出。
3. Install：`git clone https://github.com/childe/claude-lark ~/.claude/lark-src && ~/.claude/lark-src/install.sh`，每一步预期看到什么；"已开会话需重启一次"。
4. **For AI agents**：一个编号步骤列表，AI 照做即可：检查前置 → clone → install → 跑 `claude-lark doctor` → 按 ❌ 项的修复命令处理 → 再跑 doctor 直到全 ✅。写明哪些步骤需要人类在飞书后台操作，AI 必须停下来请人做。
5. Usage：`/lark [群名]`、`/lark <chat_id>`、`/lark off`、`/lark status`、群里 `/off`；**回到电脑前先按 Esc**；权限和选择题怎么回。
6. Limitations：spec 第 3 节的非目标 + U1 结论 + "同一个飞书应用的消息事件不能被别的机器/程序同时消费" + 续命大约 41 天上限。
7. Uninstall、Upgrade（`git pull` 即可，软链接）、Development（跑测试、pre-commit 扫描、e2e）。

- [ ] **Step 1**：写 README 和 LICENSE。
- [ ] **Step 2**：`scripts/scan-secrets.sh README.md` exit 0。
- [ ] **Step 3**：在临时 HOME 下让一个**新的** subagent 只读 README 的 "For AI agents" 章节并按步骤装一遍（用假 lark-cli），能装好、doctor 全 ✅ 即通过；卡住的地方回改 README。
- [ ] **Step 4**：提交 `docs: README with AI install guide`。

### Task 11: 真实环境验收与发布

需要用户在场。

- [ ] **Step 1**：在本机执行 `./install.sh`（真实 HOME），doctor 全 ✅。
- [ ] **Step 2**：重启一个 claude 会话，`/lark`，请用户在手机上完成：发 prompt、收回复、批准一次权限、拒绝一次权限、回答一次选择题、按 Esc 回到终端、群里 `/off`。逐项记录结果。
- [ ] **Step 3**：`scripts/scan-secrets.sh $(git ls-files)` exit 0；`git log --format='%an <%ae>' | sort -u` 只有 `childe <rmself@qq.com>`。
- [ ] **Step 4**：`git push`。
