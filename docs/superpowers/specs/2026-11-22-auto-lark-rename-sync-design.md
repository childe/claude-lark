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

需求来源说明：`.xdev` 的 issue-body 最初写的是"SessionStart 时建群"。用户在设计讨论中改为"首句之后建群"（含新会话与 `/clear` 后），resume 有 binding 则启动时立刻接回。本文以这一决定为准，issue-body 中与之冲突的部分作废。

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
| A. 在后台运行的 `UserPromptSubmit` hook（`wait`，asyncRewake）里检测"第一句"并建群；resume 的接回放在新增的 asyncRewake SessionStart hook 里（清理仍留在同步 hook，见 5.3） | 不卡终端；只在真正用了的会话建群；确定性，不耗模型回合 | **采用** |
| B. SessionStart 时就建群（issue-body 的原始写法） | 不用的会话也会留下空群；用户在设计讨论中已否决（见第 2 节需求来源说明） | 否 |
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

在 `UserPromptSubmit` 的 asyncRewake hook（现有 `wait` 子命令，后台运行）里处理。注意现有 `hooks.run()` 在会话未开启时直接返回，自动连接要在这个判断之前单独分流。

**统一入口**：自动建群（本节）和 resume 接回（5.3）都调用同一个函数 `commands.auto_connect(sid, source, prompt)`。它在会话目录的 `connect.lock`（阻塞 flock）里依次做：重读 binding → 按下面的规则决定"建群 / 只重试开启 / 什么都不做" → 执行。手动 `/lark` 的建群路径（`_create_and_enable`、`_bind_existing`）也在同一把锁里重读 binding 再决定，这样自动与手动、连发两句之间都不会建出两个群。锁只包住建群和开启，不包住之后的等待。

前置条件（全部满足才继续）：

1. `auto_on` 为真；
2. 能找到 claude 祖先进程，且 `~/.claude/sessions/<pid>.json` 存在、`kind == "interactive"`、`sessionId` 等于本 session；
3. prompt 不是以 `/lark` 开头（交给手动 `/lark` 处理），也不是 `<task-notification>` 开头的后台通知。

锁内按 binding 状态决定（`source` 为首句时；`source == "resume"` 的规则见 5.3）：

| 状态 | 动作 |
|---|---|
| 没有 binding | 建群 + 开启（见下面的建群流程） |
| 有 binding，且会话目录里有 `auto_pending` 标记 | 群已建出、上次开启失败：复用这个 binding 只重试 `_enable`，**不再建群** |
| 有 binding，没有 `auto_pending` 标记 | 什么都不做（手动绑定过、或用户 `/lark off` 过） |

`auto_pending` 标记：自动建群在 `create_chat` 成功、落 binding 的**同时**写入（先写标记再写 binding，两者都在锁内）。以下三处删除它：

- 自动连接里 `_enable` 成功后；
- 手动 `/lark`（建群或绑定已有群）开启成功后——用户已经手动接管；
- `/lark off`：`_off()` 改为在同一把 `connect.lock` 里先删除 `auto_pending`、再断开 enabled。

手动 `/lark`、`/lark off` 都不写这个标记。这样"自动开启失败留下标记 → 用户 `/lark off` → 下一句"命中表格第三行"什么都不做"，off 过的会话不会被自动重新开启（反目标第 3 条）；`_off()` 持 `connect.lock` 也保证它不会与正在进行的自动连接交错（自动连接先完成，off 再断开；或 off 先完成，自动连接锁内重读时已无标记）。

建群流程：

1. 群名 = prompt 第一行去掉首尾空白后截到 20 字符；为空则用会话名；再为空用 cwd 目录名。
2. 走与手动 `/lark` 相同的建群 + 开启路径（建群、落 binding、`set_enabled`、拉起监听进程、群里发"已连接"）。binding 标记为"由 claude-lark 建的群"，并记录同步基线（见 5.4）。
3. 把这第一句按现有镜像格式（`💻 终端`）发到群里，让手机侧看到上下文。
4. 然后成为等待进程（现有 `_become_waiter`），保证开启后一定有一个等待进程在等飞书消息。

失败只写 `errors.log`，hook 安静退出，不影响终端。按失败发生的位置分两类：

- **建群失败**（`create_chat` 报错、未登录、没有 owner、找不到 claude 进程）：没有 binding，下一句按"没有 binding"重新建群。
- **群已建出、开启失败**（`_enable` 里读不到 claude 进程启动时间、拉起监听进程出错等）：binding 与 `auto_pending` 都在，下一句只重试开启，不重新建群。

测试要覆盖：开启失败后下一句只调用一次 `+chat-create`（即不重复建群）且最终开启；`/lark off` 之后的下一句不自动开启；**自动开启失败（留下 `auto_pending`）→ `/lark off` → 下一句**，不自动开启且 `auto_pending` 已删除；自动开启失败后手动 `/lark` 成功，`auto_pending` 已删除。

已知限制：如果第一轮在群建好之前就结束了，这一轮的最终回复不会发到群里；后续回合正常。

### 5.3 resume 接回

SessionStart 拆成两个 hook 条目，清理与接回分开：

1. **现有的同步 `session-start` 条目保留**（不改成 asyncRewake，超时仍为 10 秒）：`source` 为 `startup` / `resume` / `clear` 时断开残留的 enabled 状态；`compact` 不断开。改动一处：**只断开不属于当前 claude 进程的 enabled**——enabled 文件里记的 pid 与启动时间等于当前 claude 祖先进程的，不删。找不到 claude 祖先进程时退回现有的无条件断开。

   "读 enabled → 判断 → 删除"必须是原子的，否则会出现：清理读到旧进程的 enabled、判断该删 → 并行的 `session-resume` 调用 `_enable` 写入当前进程的 enabled → 清理按路径删除，把新连接关掉。因此新增 `state.disable_if_foreign(sid, pid, start_time)`：在会话目录的 `enabled.lock`（阻塞 flock）里重读 enabled、判断、删除。`set_enabled` 的写入和删除也都在 `enabled.lock` 里完成。两者互斥后只剩两种顺序：清理先完成（删掉旧值）再由接回写入新值；或接回先写入新值，清理锁内重读到当前进程的值而不删。

   这里用只包住 enabled 文件读写的 `enabled.lock`，而不用 `connect.lock`：`connect.lock` 在自动建群时包住 `create_chat` 等网络调用，同步的 `session-start` 条目只有 10 秒超时、且会阻塞终端启动，不能等它。`enabled.lock` 内没有网络调用和子进程，持锁时间是毫秒级。
2. **新增 asyncRewake 的 `session-resume` 条目**（后台运行，超时 86400）：`source == "resume"`、`auto_on` 为真、会话是交互式（同 5.2 前置条件第 2 条）、该 session 有 binding → 调用 `auto_connect(source="resume")`：锁内重读到 binding 就调用 `_enable` 接回那个群，不看 `auto_pending`（"绑过就接回"）；没有 binding 则什么都不做，留给首句（resume 路径从不建群；群被别的会话占用时不接回，记日志），然后成为等待进程。其他情况立刻返回。

为什么这样拆：两个 SessionStart 条目可能并行执行，后台进程的调度时间也不确定。如果清理在后台、且无条件删除，它可能晚于接回、首句自动开启或手动 `/lark` 执行，把刚开启的桥接关掉。条件断开之后，当前进程刚写下的 enabled（pid 与启动时间都是当前进程的）不会被清理删掉；残留的 enabled 一定来自别的进程（同一 session 在另一个终端里的旧进程，或已退出的进程），照旧断开。同一进程内切走会话时 SessionEnd 已经断开，不依赖 SessionStart 清理。

测试要覆盖：先开启（写入当前进程的 pid 与启动时间）再执行清理，enabled 仍在；enabled 记的是别的 pid 时，清理会删掉它；**交错**：清理读到旧值后暂停（测试里用钩子注入），接回写入当前进程的值，清理继续——最终 enabled 仍是当前进程的值（`enabled.lock` 使接回的写入等到清理结束后才发生）。

`/lark off` 保留 binding，所以 off 过的会话不会被首句自动建群；之后 resume 时会被接回（用户选择的"绑过就接回"）。

`settings.py` 新增 `("SessionStart", None, "session-resume", 86400, _ASYNC)` 条目，原 `session-start` 条目不变；`install.sh` 重跑会写入新条目，README 写明升级后需重跑 `install.sh`。

### 5.4 群名跟随会话名

Binding 新增两个字段（对旧 binding 文件向后兼容，缺字段时有默认值）：

- `created_by_bridge`：是否 claude-lark 自己建的群。旧 binding 缺省时按 `chat_name != chat_id` 推断。
- `synced_name`：上一次同步/观测到的会话名。建群或绑定时写入当时的会话名（读不到则为空串）。

**基线规则**：只有会话名与 `synced_name` 不同时才改群名。这样手动 `/lark` 时 Claude 总结的群名不会被当时的自动名覆盖；首句建群后的第一次 `/rename` 会生效。旧 binding 没有 `synced_name` 时，第一次观测只记录基线、不改名。

监听进程每轮**先完成全部群的消息轮询，再**做改名检查。这是串行方案的取舍：改名不会推迟本轮的消息拉取，但会推迟**下一轮**拉取；最坏延迟约为"本轮需要改名的群数 × 10 秒"（每次改名的超时）。只有会话名刚变化、或失败后 60 秒冷却到期的群才会调用改名，平常这一项为 0。不另起改名线程，是为了不增加调度与 binding 写回的协调。对 enabled 的 binding：

1. 跳过 `created_by_bridge` 为假的 binding。
2. 读 `~/.claude/sessions/<binding.claude_pid>.json`；文件不存在、解析失败、`sessionId` 不等于 binding 的 session → 跳过。
3. `name` 去空白后为空 → 跳过。与 `synced_name` 相同 → 跳过。
4. 不同 → `lark-cli im +chat-update --as bot --chat-id <chat_id> --name <name 截到 60 字符>`（参数以列表传给子进程，不经 shell）。超时用监听进程现有的 `SEND_TIMEOUT`（10 秒），不用 `feishu` 默认的 30 秒。每轮读的都是最新的会话名，期间多次改名只会同步最后一个。
   - 成功：用 `state.update_binding()`（见下）只写 `chat_name` 和 `synced_name`。
   - 失败：写 `errors.log`；同一 binding 60 秒内不再重试；每个 binding 只在群里发一次提示："改群名失败，可能缺少 im:chat:update 权限，详见 errors.log"。

`nameSource` 为 `user` 和 `derived` 都同步。

**binding 写入协议**：现有 `save_binding()` 整份替换。监听进程拿着旧 binding 调网络改名期间，另一个 claude 进程 resume 同一 session 并通过 `_enable` 写入新的 `claude_pid` / `cwd`；监听进程再整份保存就会把 pid 改回旧值，之后元数据读取跳过、watchdog 还可能按旧 pid 已退出断开会话。因此新增 `state.update_binding(sid, fn)`：在会话目录的 `binding.lock`（阻塞 flock）里重读 binding、调用 `fn` 修改、原子写回；`fn` 内不做网络调用。

- 监听进程的名称写回：锁内重读，`chat_id` 与发起改名时不同就放弃写回；否则只改 `chat_name`、`synced_name`。
- `_enable` 写 `claude_pid` / `cwd`、自动建群首次落 binding，也都通过 `update_binding`（或在同一把锁里 `save_binding`）完成。
- 测试要覆盖：改名进行中 binding 的 `claude_pid` 被改，写回后 `claude_pid` 仍是新值。

### 5.5 文档与 doctor

- README（英文正文 + 中文摘要）：权限表加 `im:chat:update`（bot，用于改群名）；Usage 加 `/lark auto on|off` 和自动开启的触发时机；写明 5.2 的已知限制；升级说明提示重跑 `install.sh`。
- doctor：如果 `lark-cli auth status` 能看到 bot 的 scope，就检查 `im:chat:update`；看不到就不检查，只在 README 写明。
- `commands/lark.md` 的参数说明加上 `auto on|off`。

## 6. 留给实现者的判断

- 改名重试计时的具体存放位置（会话目录或内存）。
- 锁的获取顺序固定为 `connect.lock` → `binding.lock` → `enabled.lock`，避免死锁；`enabled.lock` 是叶子锁，持有时不再取其他锁。三把锁都不跨越等待飞书消息的阶段。
- 测试沿用现有 `tests/fake_lark_cli.py` 的方式，给 `+chat-update` 加假实现；会话元数据文件的路径通过环境变量可替换，方便测试隔离。
