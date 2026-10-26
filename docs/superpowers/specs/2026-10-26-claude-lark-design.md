# claude-lark 设计文档

日期：2026-10-26
状态：草稿，待审批

## 1. 意图

用户离开电脑时，手机上只有飞书。他想在手机上继续驱动一个**已经在跑的** Claude Code 会话：

1. 在飞书群里发 prompt。
2. 在飞书群里看到 Claude 的最终回复。
3. 再发下一句，循环。

用户在终端里敲 `/lark`，当前会话就绑定到一个飞书群，开始双向同步。会话里需要用户确认的事情，也要转到飞书上处理：权限确认（是否允许执行某条命令），以及 Claude 用选择题向用户提问。

项目放在 GitHub 上，别人可以一键安装。README 要写到 AI 读完就能替人装好的程度。

## 2. 目标

- G1：在一个已经运行的交互式会话里敲 `/lark` 就生效，不用重启 claude，也不用加启动参数。
- G2：飞书群里用户发的消息作为新 prompt 进入同一个会话，保留完整上下文。
- G3：每轮的最终回复发到飞书群。
- G4：权限确认转到飞书，用户回"同意 / 拒绝"。终端上的确认框同时存在，谁先答算谁的。
- G5：Claude 的选择题提问（`AskUserQuestion` 工具）转到飞书，用户在飞书上作答。
- G6：用户在终端里输入的 prompt 也同步到群里，标注"💻 终端"，让手机上能看到完整对话。
- G7：会话和群的绑定关系持久化。`claude --resume` / `--continue` 之后敲 `/lark`，回到原来的群。
- G8：别人能按 README 一键安装、卸载。

## 3. 非目标

- 不发中途进度（执行了哪些命令、读了哪些文件）。用户明确说只要最终回复。
- resume 后不自动接上飞书。resume 时会记住群，但要用户敲 `/lark` 才开启桥接。理由：在终端里 resume，说明人就在电脑前。
- 不支持从飞书远程启动 claude。电脑重启后，用户需要先在电脑上 `claude --resume`。
- 不支持多人协作。群里只认绑定者本人。
- 不支持 Windows。目标平台是 macOS，Linux 能跑最好。
- 不使用 Claude Code 的 channels 机制。它需要在启动时加参数，违背 G1。

## 4. 反目标（技术上能跑，但出现以下情况就算失败）

- 桥接没开时，hook 拖慢或干扰正常会话。没开桥接的会话里，每个 hook 必须立刻放行（目标 < 100ms），不输出任何东西。
- 自动批准权限请求。权限只能由用户在飞书或终端上明确批准。hook 出错或超时，按"未批准"处理，交回终端的确认框。
- 执行非绑定者发来的消息。
- 丢消息：用户在飞书上发的话，没进会话，也没有任何提示。
- 仓库里出现任何公司内部信息（app id、open_id、内部域名、公司邮箱）。git 提交身份必须是 GitHub 身份。

## 5. 已验证的事实（2026-10-26，Claude Code 2.1.287 实测）

用临时目录和 tmux 驱动的真实交互式会话做了试验，结论如下：

| # | 事实 |
|---|------|
| F1 | Stop hook 输出 `{"decision":"block","reason":"<文本>"}`，Claude 会把 `<文本>` 当作下一条指令继续干活。 |
| F2 | Stop hook 的输入 JSON 包含 `session_id`、`transcript_path`、`cwd`、`stop_hook_active`、`last_assistant_message`。`last_assistant_message` 就是最终回复的文本，不用解析 transcript。 |
| F3 | hook 的 `timeout` 设为 3600 后，Stop hook 阻塞 12 分钟仍然存活，之后投递消息正常。默认超时是 10 分钟。更大的上限没测。 |
| F4 | hook 超时时，进程收到 SIGTERM，会话正常结束，回到空闲状态。 |
| F5 | 连续 block 有上限，默认 8 次，第 9 次会被强制停止。在 settings.json 的 `env` 里设置 `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=1000` 后**热加载生效**，在已经运行的会话里也有效，实测连续 11 次都通过。 |
| F6 | Stop hook 阻塞时，用户在终端按 Esc：hook 进程收到 SIGTERM，界面显示 "Interrupted"，用户可以正常输入。用户在终端发出下一条 prompt 后，那一轮结束时 Stop hook 会再次触发。 |
| F7 | Stop hook 阻塞时，用户在终端直接打字回车不会打断 hook。消息会排队，界面提示 `ctrl+x ctrl+s to send now`。 |
| F8 | PermissionRequest hook 只在真要弹确认框时触发。输入包含 `tool_name`、`tool_input`（Bash 的话包含完整的 `command` 和 `description`）和 `permission_suggestions`。输出 `{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}` 后命令被执行，终端显示 "Allowed by PermissionRequest hook"。 |
| F9 | PermissionRequest hook 运行时，终端的确认框**同时显示**。 |
| F10 | PreToolUse hook（matcher 为 `AskUserQuestion`）能在选项框出现前拦下提问。输入的 `tool_input.questions[]` 包含 question、header、options、multiSelect。输出 `permissionDecision:"allow"`，并在 `updatedInput` 里加上 `answers: {<question 文本>: <答案>}`，Claude 收到的就是"User answered: … → Blue"，终端不弹选项框。 |
| F11 | `claude --resume <id>` 和 `claude --continue` 之后，`session_id` 不变。 |
| F12 | project 级 `.claude/settings.json` 里的 hooks 和 env 修改会热加载。 |
| F13 | 本机 `lark-cli` 1.0.92：`lark-cli event consume im.message.receive_v1 --as bot` 的 dry-run 前置检查（凭据、开发者后台事件、权限）全部通过。事件输出字段包含 `chat_id`、`chat_type`、`content`（已渲染成可读文本）、`message_id`、发送者信息。 |

**未验证，需要在实现时确认：**

- U1：机器人在群里不被 @ 时，能不能收到消息。这通常需要"获取群组中所有消息"类权限。如果拿不到，退路是要求用户每条都 @机器人，体验会变差，要在 README 里写清楚。
- U2：Stop hook 的 `timeout` 最大能设多少（比如 86400 秒）。
- U3：用户在终端上处理权限确认框时，正在阻塞的 PermissionRequest hook 会怎样（推测是被 SIGTERM）。
- U4：slash 命令怎么拿到当前会话的 `session_id`。见第 7.1 节。
- U5：用户级 `~/.claude/settings.json` 的热加载行为是否和 project 级一致（F12 只测了 project 级）。

## 6. 用户可见的行为

### 6.1 终端里的命令

| 命令 | 行为 |
|------|------|
| `/lark [群名]` | 如果当前会话已有绑定的群，复用这个群并开启桥接，**忽略传入的群名**（提示一句"已有绑定群 xxx，忽略群名参数"）。没有绑定就新建一个群再开启。群名规则（用户的决定）：用户传了群名就用它；没传就由 Claude 根据当前会话内容总结一个简短的群名（建议不超过 20 个字，概括这个会话在做什么）。成员是用户本人和机器人。开启后往群里发一条"已连接"消息，包含 cwd 和 session_id 前 8 位。 |
| `/lark <群链接或 chat_id>` | 绑定到指定的已有群并开启桥接。机器人必须已经在这个群里，不在就报错并说明怎么把机器人拉进群。 |
| （参数解析，我的决定） | 参数是 `off` / `status` → 对应子命令。参数以 `oc_` 开头，或者是飞书群链接 → 绑定已有群。其他任何文字 → 当作群名。当前会话已有绑定时，传 chat_id 或链接视为"换绑"，要报错提示先 `/lark off`，不静默忽略。 |
| `/lark off` | 关闭桥接，保留绑定。往群里发一条"已断开"消息。 |
| （约束，我的决定） | 一个群同一时刻只能被一个**开启中**的会话占用。`/lark <chat_id>` 指向一个正被别的会话占用的群时，报错并说明是哪个会话（cwd、session_id 前 8 位）。如果占用它的会话进程已经不在了，允许接管。 |
| `/lark status` | 显示当前会话的绑定群、桥接是否开启、监听进程是否存活。 |

### 6.2 桥接开启后

- **每轮结束**：最终回复发到群里，用飞书富文本（post 或卡片，由实现决定），代码块和列表要能正常显示。超过飞书单条长度限制时分段发。然后等待群里的下一条消息。
- **群里收到用户的消息**：作为下一条指令进入会话。
- **Claude 还在干活时群里来了消息**：先排队，这一轮结束时一次性投递，多条按时间顺序合并。不能丢。
- **终端输入**：用户在终端里输入的 prompt 同步到群里，前缀"💻 终端："。
- **权限确认**：群里收到一条消息，写清工具名和完整命令，Bash 的话附上 description。用户回"同意"/"1"/"y" 表示允许，"拒绝"/"2"/"n" 表示拒绝。其他回复按什么处理由实现决定，但不能当作同意。终端的确认框同时存在。任一侧作答后，往群里发一条结果（"✅ 已允许" / "❌ 已拒绝" / "已在终端处理"）。
- **选择题提问**：群里收到问题和带编号的选项。用户回编号，多选用逗号分隔；回编号以外的文字，按自由文本答案处理（对应选项框里的 "Other"）。桥接开启时，终端不弹选项框。
- **用户回到电脑前**：按 Esc 打断正在等待的 hook，然后正常在终端使用。在终端发出的 prompt 照常同步到飞书，这一轮结束时 Stop hook 会重新等待飞书消息。用户想停止同步就敲 `/lark off`。README 和 `/lark` 开启时的提示都要说明这一点。
- **群里的控制命令**（我的决定，范围最小）：群里发 `/off` 等同于终端里的 `/lark off`，给用户一个在手机上断开的办法。

### 6.3 安全

- 只接受绑定者本人发来的消息。绑定者是执行 `/lark` 时 `lark-cli` 用户身份对应的 open_id。其他人的消息丢弃，可以在群里回一条提示。
- 发消息用**机器人身份**。用用户本人身份发，用户手机上收不到通知。
- 权限请求永远不默认同意。

## 7. 架构

```
终端 claude 会话
  ├─ /lark 命令 ─────────────→ lark.py on/off/bind/status
  ├─ UserPromptSubmit hook ──→ 把终端输入同步到群
  ├─ Stop hook ──────────────→ 发最终回复；阻塞读收件箱 → block + reason
  ├─ PermissionRequest hook ─→ 发确认请求；阻塞读收件箱 → allow/deny
  └─ PreToolUse(AskUserQuestion) → 发问题；阻塞读收件箱 → updatedInput.answers

每个已开启的会话一个监听进程（后台）
  lark-cli event consume im.message.receive_v1 --as bot
    → 按 chat_id 和发送者过滤 → 追加到 ~/.claude/lark/sessions/<session_id>/inbox
```

### 7.1 组件

1. **`commands/lark.md`**：用户级 slash 命令，装到 `~/.claude/commands/lark.md`。它调用 `lark.py`，并传入当前会话的 `session_id`。拿 session_id 的方式需要实现时验证（U4）。候选方案：
   - 命令内容里的 `${CLAUDE_SESSION_ID}` 变量替换（如果当前版本支持）。
   - UserPromptSubmit hook 识别 `/lark` 开头的输入，在 hook 里完成操作，因为 hook 输入带 session_id。
   - 选能用的、最不依赖 Claude 自觉的方案。
   - 约束：没传群名时要由 Claude 总结群名，所以 `/lark` 至少在"新建群且没传群名"这条路径上要让 Claude 参与（命令内容让 Claude 总结后调用 `lark.py`）。其他路径能不经过 Claude 就不经过。

2. **`lark.py`**：主程序，Python 3，只用标准库。子命令：
   - `on|off|bind|status`：给 slash 命令调用。
   - `hook <event>`：给各 hook 调用，从 stdin 读 hook 输入 JSON。
   - `listen <session_id>`：后台监听进程。
   - `doctor`：检查安装和飞书配置，给出可执行的修复建议。
   - 每个 hook 入口的第一件事：检查 `sessions/<session_id>/enabled` 是否存在。不存在就以 exit 0 退出，不输出任何东西。

3. **监听进程**（本节是我的决定）：**整台机器只有一个**监听进程，不是每个会话一个。原因：飞书长连接对同一个应用的多个连接是随机投递，不是广播。多个监听进程会互相抢消息，按 chat_id 过滤掉后消息就无声丢失了。
   - 监听进程按 chat_id 查绑定表，把消息写进对应会话的收件箱。chat_id 没有开启中的会话就丢弃，同时在群里回一条"当前没有会话在桥接"。
   - 第一个 `/lark` 开启时启动；没有任何开启中的会话时退出。用 pid 文件保证全机只有一个。
   - 实现时要确认 `lark-cli` 本地事件总线的行为：同一台机器上多个 `event consume` 是不是各自收到全部事件。只用一个 consumer 的话，这个问题可以绕开。
   - README 要写明：同一个飞书应用的 `im.message.receive_v1` 事件，不能同时被别的机器或别的程序消费，否则会丢消息。`doctor` 尽量检测这种情况。

4. **收件箱**：每个会话一个，追加写入。每条消息带 message_id，按它去重。阻塞中的 hook 从收件箱里消费消息：
   - 等待权限确认或选择题答案时，收件箱里的下一条用户消息就是答案。
   - 其他时候，消息作为 prompt，由 Stop hook 投递。
   - 消费位置要持久化，hook 被 SIGTERM 杀掉后不能重复投递，也不能丢。

### 7.2 状态目录

```
~/.claude/lark/
  config.json            # 安装时生成：owner open_id 等
  sessions/<session_id>/
    binding.json         # chat_id、群名、cwd、创建时间
    enabled              # 存在 = 桥接开启
    listener.pid
    inbox.jsonl
    cursor               # 已消费位置
```

### 7.3 settings.json 注册内容

安装脚本往 `~/.claude/settings.json` 合并以下内容，其他内容保持不变：

- hooks：Stop、PermissionRequest（matcher `*`）、PreToolUse（matcher `AskUserQuestion`）、UserPromptSubmit，以及第 7.1 节需要的其他 hook。阻塞类 hook 的 `timeout` 尽量设大（见 U2）。
- `env.CLAUDE_CODE_STOP_HOOK_BLOCK_CAP = "1000"`（F5）。

### 7.4 超时

阻塞中的 hook 快到超时时，必须在被 SIGTERM 之前自己处理：

- **Stop hook：自动续命（用户的决定）**。快到超时时返回 `block`，reason 是一条空转指令，比如"没有新消息，只回复 `waiting`，不做任何其他事"。Claude 回一句后，这一轮结束，Stop hook 再次触发，开始新一轮等待。
  - 空转轮次的回复**不发到群里**。Stop hook 要能识别出上一轮是空转（比如在会话状态里记一个标记），然后跳过发送。
  - 续命次数受 `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=1000` 限制。还剩不多时（阈值由实现决定），往群里发一条提醒；真到上限后，往群里发一条"会话已空闲，回到电脑前继续"。
  - 单次等待尽量长（取决于 U2），这样续命次数和 token 消耗都更少。
  - 用户敲 `/lark off` 或在群里发 `/off` 后，不再续命。
- **PermissionRequest / AskUserQuestion**：不做决定，交回终端的确认框或选项框，同时往群里发一条"等待超时，已交回终端"。

## 8. 安装和分发

- 仓库：GitHub 公开仓库 `claude-lark`。所有提交内容不能包含敏感信息：app id、app secret、open_id、chat_id、token、内部域名、公司邮箱、本机绝对路径。提交前用 grep 扫一遍，CI 或 pre-commit 检查由实现决定。
- `install.sh`：
  - 检查依赖：python3、`lark-cli`（已登录，机器人和用户身份都可用）、jq（如果用到）。
  - 把 `commands/lark.md` 和程序文件装到 `~/.claude/`。用软链接还是复制，由实现决定；README 要写清楚升级方式。
  - 合并 settings.json 前先备份，合并要幂等，重复执行结果不变。
  - 最后运行 `lark.py doctor`。
  - 安装完要提示：**已经开着的 claude 会话需要重启一次**才能用 `/lark`；之后的会话里随时敲 `/lark` 都能生效（G1 从这时开始成立）。这是我的决定：不往用户项目里写 `.claude/settings.json`，避免改动用户仓库。如果实测用户级 settings 也能热加载（U5），就去掉这条提示。
- `uninstall.sh`：删除安装的文件，从 settings.json 里只移除本项目加的条目。
- **README** 面向两类读者：人和 AI。要包含：
  - 这是什么，一张交互示意。
  - 前置条件，写成能逐项检查的清单：飞书开放平台自建应用、开通机器人能力、需要的权限 scope 列表、需要在后台订阅的事件、`lark-cli` 的安装和登录命令。
  - 一键安装命令，以及每一步预期看到什么。
  - 一节"给 AI 的安装说明"：AI 照着执行就能装好，包括怎么判断每一步成功、失败时看什么。
  - 使用说明，特别写清楚"回到电脑前要先按 Esc"。
  - 已知限制（第 3 节、U1 的退路）。
  - 卸载方法。

## 9. 设计过程中的决定（供审阅）

用户的决定：群名可由参数指定，不传则 Claude 总结，已有绑定时忽略群名；Hook 方案（不用 channels）；权限确认和选择题都转到飞书；不发中途进度；resume 后不自动接飞书；超时后自动续命；仓库公开，不能有敏感信息；README 支持 AI 一键安装。

我的决定（用户可推翻）：一个会话一个群；`/lark` 参数解析规则；群里只认绑定者；机器人身份发消息；终端输入同步到群；群里 `/off` 命令；全机单监听进程；一个群同时只能被一个会话占用；安装后已有会话重启一次；Python 标准库实现。

## 10. 留给实现者决定的

- 飞书消息格式：post 还是卡片，以及分段的具体长度。
- 收件箱的具体格式和锁机制。
- 监听进程的守护方式和日志位置。
- 测试方案：纯逻辑部分要有单元测试；端到端可以参考试验时的做法，用 tmux 驱动一个真实会话，用 haiku 模型，在临时目录里跑。
- 代码按用户规范用 black 格式化。
