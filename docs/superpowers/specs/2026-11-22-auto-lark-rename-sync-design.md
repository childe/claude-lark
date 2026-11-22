# 自动开启 /lark，并让飞书群名跟随会话名 — 设计

日期：2026-11-22

## 1. 目的

用户希望不用每次手敲 `/lark`：开了自动模式后，交互式会话一开始干活就自动有一个飞书群可以接手。
群建出来以后，用户在终端 `/rename` 会话时，群名跟着改，方便在飞书里按名字找到对应会话。

## 2. 目标与非目标

目标：

1. 一个全局开关 `auto_on`。开启后：
   - 新开的交互式 `claude` 会话，在用户发出**第一句**之后自动建群并开启桥接。
   - `/clear` 之后的新会话，同样在**第一句**之后自动建群。
   - `/resume` 一个曾经绑过群的会话，启动时立刻接回那个群；没绑过的，第一句之后建群。
2. 会话名（`/rename` 设定的，或 Claude Code 自动起的）变化后，由 claude-lark 自己建的群跟着改名。

非目标：

- 不在会话启动时（还没有第一句时）建群：不用的会话不留空群。
- 不改用 `/lark oc_xxx` 绑定的已有群的群名：那是用户自己的群。
- 不处理 `claude -p` 等非交互式会话。
- 不在建群时调用模型总结群名（会多耗一个回合）。

反目标（技术上能跑但算失败）：

- 自动建群让终端输入卡顿。
- 手动 `/lark` 时 Claude 总结出的群名，刚建好就被自动名覆盖。
- 一个会话被反复建群（例如每句话都建、`/lark off` 之后又被自动建回来）。

## 3. 已核实的事实（Claude Code 2.1.287，实测）

1. **SessionStart hook 支持 `asyncRewake`。** 一个 `asyncRewake: true` 的 SessionStart hook 后台 sleep 8 秒后 `exit 2`，stderr 的内容唤醒了模型并被执行。终端在这期间可用。
2. **会话元数据文件** `~/.claude/sessions/<claude pid>.json`，示例字段：
   `{"pid":64289,"sessionId":"7e9c…","cwd":"…","kind":"interactive","name":"rw-33","nameSource":"derived","nameSince":…}`
   - 会话启动后立即存在，`name` 是自动名（目录名 + 编号），`nameSource` 为 `derived`。
   - `/rename 测试改名 abc` 后 1 秒内变为 `"name":"测试改名 abc","nameSource":"user"`。
   - `kind` 为 `interactive` 表示交互式会话。
3. `lark-cli im +chat-update --as bot --chat-id oc_xxx --name <名>` 可改群名，群名最多 60 字符；需要应用的 `im:chat:update` 权限（bot 身份）。
4. 现有代码：`Binding` 字段为 `session_id, chat_id, chat_name, cwd, claude_pid, created_at`；`_bind_existing` 把 `chat_name` 设成 `chat_id`，`_create_and_enable` 设成真实群名。监听进程每 2 秒轮询已开启的群。

## 4. 方案比较

自动开启：

| 方案 | 取舍 | 结论 |
|---|---|---|
| A. 在后台运行的 `UserPromptSubmit` hook（`wait`，asyncRewake）里检测"第一句"并建群；resume 的接回放在改为 asyncRewake 的 SessionStart hook 里 | 不卡终端；只在真正用了的会话建群；确定性，不耗模型回合 | **采用** |
| B. SessionStart 时就建群 | 不用的会话也会留下空群；用户已明确否决 | 否 |
| C. 用 hook 输出 additionalContext 让 Claude 自己执行 `/lark` | 每次多耗一个模型回合，且模型可能不执行 | 否 |

群名跟随：

| 方案 | 取舍 | 结论 |
|---|---|---|
| A. 监听进程每轮轮询时读 `~/.claude/sessions/<pid>.json` 的 `name` | 空闲时也能及时同步；读本地文件代价很低 | **采用** |
| B. 在 Stop / UserPromptSubmit hook 里检查 | `/rename` 不触发 hook，会话空闲时改名不会同步 | 否 |
| C. 读 transcript 里的标题记录 | 2.1.287 的 transcript 里没有改名记录 | 否 |

## 5. 设计

### 5.1 开关

- `~/.claude/lark/config.json` 新增布尔字段 `auto_on`，缺省为 `false`。写入时保留其他字段（`owner_open_id` 等），原子写。
- `/lark auto on`、`/lark auto off` 设置开关，返回 `RESULT:` 文本。参数必须**整串**等于 `auto on` / `auto off` 才算开关命令；其他以 `auto` 开头的参数仍按群名处理。
- `/lark status` 增加一行显示自动开启是否开着。
- `install.sh` 不得覆盖 / 丢失已有的 `auto_on`。

### 5.2 首句自动建群

在 `UserPromptSubmit` 的 asyncRewake hook（现有 `wait` 子命令，后台运行）里处理。条件全部满足才建群：

1. `auto_on` 为真；
2. 该 session 没有 binding（`binding.json` 不存在）；
3. 能找到 claude 祖先进程，且 `~/.claude/sessions/<pid>.json` 存在、`kind == "interactive"`、`sessionId` 等于本 session；
4. prompt 不是以 `/lark` 开头（交给手动 `/lark` 处理），也不是 `<task-notification>` 开头的后台通知；
5. 同一 session 没有另一个 hook 正在自动建群（用文件锁防并发，例如用户在建群完成前连发两句）。

建群流程：

1. 群名 = prompt 第一行去掉首尾空白后截到 20 字符；为空则用会话名；再为空用 cwd 目录名。
2. 走与手动 `/lark` 相同的建群 + 开启路径（建群、落 binding、`set_enabled`、拉起监听进程、群里发"已连接"）。binding 标记为"由 claude-lark 建的群"，并记录同步基线（见 5.4）。
3. 把这第一句按现有镜像格式（`💻 终端`）发到群里，让手机侧看到上下文。
4. 然后成为等待进程（现有 `_become_waiter`），保证开启后一定有一个等待进程在等飞书消息。

失败（lark-cli 报错、未登录、没有 owner 等）只写 `errors.log`，hook 安静退出，不影响终端。失败后同一 session 的下一句会再试一次（因为仍然没有 binding）。

已知限制：如果第一轮在群建好之前就结束了，这一轮的最终回复不会发到群里；后续回合正常。

### 5.3 resume 接回

SessionStart hook 改为 asyncRewake（后台运行，超时放长）。行为：

- `source` 为 `startup` / `resume` / `clear`：先按现有逻辑断开残留的 enabled 状态（保留现有行为）。
- 再看：`source == "resume"`、`auto_on` 为真、会话是交互式（同 5.2 第 3 条）、该 session 有 binding → 用现有 `_enable` 路径接回那个群（群被别的会话占用时不接回，记日志），然后成为等待进程。
- 其他情况立刻返回。
- `compact` 不断开，保持现有行为。

`/lark off` 保留 binding，所以 off 过的会话不会被首句自动建群；之后 resume 时会被接回（用户选择的"绑过就接回"）。

`settings.py` 的 SessionStart 条目改为带 `asyncRewake: true`；`install.sh` 重跑会更新它，README 写明升级后需重跑 `install.sh`。

### 5.4 群名跟随会话名

Binding 新增两个字段（对旧 binding 文件向后兼容，缺字段时有默认值）：

- `created_by_bridge`：是否 claude-lark 自己建的群。旧 binding 缺省时按 `chat_name != chat_id` 推断。
- `synced_name`：上一次同步/观测到的会话名。建群或绑定时写入当时的会话名（读不到则为空串）。

**基线规则**：只有会话名与 `synced_name` 不同时才改群名。这样手动 `/lark` 时 Claude 总结的群名不会被当时的自动名覆盖；首句建群后的第一次 `/rename` 会生效。旧 binding 没有 `synced_name` 时，第一次观测只记录基线、不改名。

监听进程每轮轮询（对 enabled 的 binding）：

1. 跳过 `created_by_bridge` 为假的 binding。
2. 读 `~/.claude/sessions/<binding.claude_pid>.json`；文件不存在、解析失败、`sessionId` 不等于 binding 的 session → 跳过。
3. `name` 去空白后为空 → 跳过。与 `synced_name` 相同 → 跳过。
4. 不同 → `lark-cli im +chat-update --as bot --chat-id <chat_id> --name <name 截到 60 字符>`（参数以列表传给子进程，不经 shell）。
   - 成功：更新 binding 的 `chat_name` 和 `synced_name`。
   - 失败：写 `errors.log`；同一 binding 60 秒内不再重试；每个 binding 只在群里发一次提示："改群名失败，可能缺少 im:chat:update 权限，详见 errors.log"。

`nameSource` 为 `user` 和 `derived` 都同步。

### 5.5 文档与 doctor

- README（英文正文 + 中文摘要）：权限表加 `im:chat:update`（bot，用于改群名）；Usage 加 `/lark auto on|off` 和自动开启的触发时机；写明 5.2 的已知限制；升级说明提示重跑 `install.sh`。
- doctor：如果 `lark-cli auth status` 能看到 bot 的 scope，就检查 `im:chat:update`；看不到就不检查，只在 README 写明。
- `commands/lark.md` 的参数说明加上 `auto on|off`。

## 6. 留给实现者的判断

- 自动建群的文件锁、改名重试计时的具体存放位置（会话目录或内存）。
- 5.2 里"同一 session 并发"之外的竞态（例如自动建群与手动 `/lark` 同时发生）以 binding 是否已存在为准处理，具体实现自定，但不能建出两个群。
- 测试沿用现有 `tests/fake_lark_cli.py` 的方式，给 `+chat-update` 加假实现；会话元数据文件的路径通过环境变量可替换，方便测试隔离。
