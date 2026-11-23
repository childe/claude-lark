# install.sh 升级后重启监听进程 — 设计

日期：2026-11-23

## 1. 问题

监听进程（`claude-lark listen`）是常驻进程，只在没有开启中的会话时自己退出。`install.sh` 重跑不会停它，
所以升级后它一直运行旧代码。

实际故障（2026-11-23）：监听进程从 11-19 起运行。新版 `binding.json` 多了 `created_by_bridge`、
`synced_name` 两个字段，旧代码 `Binding(**json)` 抛 `TypeError`，`load_binding` 返回 `None`，
新群被当成没有绑定，群里的消息全部收不到。手动 SIGTERM 旧进程后，第一次 `ensure_running()` 返回 True
但没有拉起新进程（6 秒后没有 pid 文件，listener.log 无新行），第二次才成功。原因未查明。

## 2. 目标与非目标

目标：

1. `install.sh` 结束时，正在运行的旧监听进程被停掉；有开启中的会话时，新代码的监听进程被拉起并确认存活。
2. doctor 能发现"监听进程运行的不是当前代码"，并给出修复命令。
3. 拉起失败时能看到原因，而不是静默。

非目标：

- 不做监听进程自动发现代码变化并自我重启（方案 B，已否决）。
- 不改监听进程的轮询、路由逻辑。

## 3. 方案

| 方案 | 取舍 | 结论 |
|---|---|---|
| A. install.sh 末尾重启监听进程 + doctor 检查代码版本 | 简单、可预期；只覆盖跑了 install.sh 的升级，而 README 本来就要求升级后重跑 install.sh；doctor 兜底 | **采用**（用户选定） |
| B. 监听进程 watchdog 发现代码 hash 变化后自己交接给新进程 | 覆盖只 `git pull` 的情况；但 hook 配置变化仍要 install.sh，且文件写一半时可能连续重启，复杂 | 否 |

## 4. 设计

### 4.1 代码版本与实例信息

- `listener.code_version()`：对 `lark_bridge/*.py` 与 `bin/claude-lark` 的内容按文件名排序后算 sha256，取前 12 位。
- 实例信息文件 `~/.claude/lark/listener.version`，内容为 JSON `{"pid": N, "code_version": "V"}`。
  监听进程在 `_acquire_pid_file()` 里、与写 pid 文件**同一次 `_pid_lock` 内**原子写入；
  `_drop_life_lock_locked()` 删 pid 文件时一并删除（同样只删 pid 等于自己的那份）。
- **实例身份以 pid 文件为准，实例信息文件只提供版本。** 新旧两版代码都在拿存活锁的同一次 `_pid_lock` 内写 pid 文件
  （旧版见 `listener.py` `_acquire_pid_file()`），且只在放锁的同一次 `_pid_lock` 内删自己的 pid 文件。
  因此在 `_pid_lock` 内看到"存活锁被持有 + pid 文件为 N"时，N 就是当前锁持有者且仍存活（存活锁的 fd 不被子进程继承），
  不存在 pid 复用问题，无需另记进程启动标识。这一点对**升级前启动、没有实例信息文件的旧监听进程**同样成立。
- 辅助函数 `listener.instance()`：在 `_pid_lock` 内读取，返回 `(held, pid, code_version)`，供重启命令与 doctor 共用：
  - `held`：存活锁是否被持有；未被持有时 `pid`、`code_version` 均为 `None`。
  - `pid`：pid 文件内容；被持有但 pid 文件缺失或读不出时为 `None`（按上面的不变量不应出现，作为异常处理）。
  - `code_version`：实例信息文件存在、可解析且其 `pid` 等于 `pid` 时取其值；否则为 `None`，含义是"版本未知"
    （旧协议进程没有该文件，或文件是上一个被 SIGKILL 的实例留下的残留）。版本未知按"不是当前版本"处理。

### 4.2 重启命令

新增子命令 `claude-lark restart-listener`，`install.sh` 在 doctor 之前调用它。行为：

1. 在 `_pid_lock` 内读取存活锁状态和实例信息（`instance()`）：
   - 存活锁没被持有 → 放开短锁，跳到第 4 步。
   - 存活锁被持有但 `pid` 为 `None`（pid 文件缺失或读不出，无法核实实例身份）→ 打印 `❌` 行和修复提示
     （手动结束 `claude-lark listen` 进程），不发任何信号，退出码非 0。
   - 否则记下原实例 pid `P`（来自 pid 文件），在同一次 `_pid_lock` 内对 `P` 发 SIGTERM。
     `code_version` 为 `None`（升级前启动的旧进程，或残留的实例信息文件）**不阻止**停止，因为这正是本需求要处理的旧进程。
     旧进程已有 SIGTERM 处理（`main()` 的 `on_sigterm`），会正常放锁退出。
2. 放开短锁后等待原实例退出（等待期间不持有 `_pid_lock`，否则原实例退出时的 `_release_pid_file()` 会被阻塞）。
   每次轮询在 `_pid_lock` 内取一次 `instance()`，满足任一条件即视为交接完成，进入第 4 步：
   - 存活锁未被持有；
   - 存活锁被持有，但 `pid` 已不是 `P`（并发的 `/lark` 或 auto-connect 经 `ensure_running()` 拉起了新实例）。
3. 最多等 20 秒（监听进程可能正卡在一次最长 15 秒的 lark-cli 拉取里）。超时后在 `_pid_lock` 内重新取 `instance()`，
   仅当存活锁仍被持有且 `pid` 仍为 `P` 时才对 `P` 发 SIGKILL；否则按第 2 步的条件视为交接完成。
   SIGKILL 后再按第 2 步的条件等最多 5 秒，仍未完成 → `❌` + 修复提示，退出码非 0。
4. 没有开启中的会话（`_any_enabled()` 为假）→ 打印"没有开启中的会话，不需要监听进程"，退出码 0。
   （若第 2 步是被新实例接管的，新实例自己会按现有 watchdog 逻辑决定去留，这里不再干预。）
5. 有开启中的会话 → `ensure_running()`，然后最多等 5 秒，直到 `instance()` 显示存活锁被持有且
   `code_version`（实例信息 pid 与 pid 文件一致时才有值）等于本进程算出的 `code_version()`。不满足就再调一次 `ensure_running()` 并再等 5 秒。
   仍失败 → `❌` 行，修复提示指向 `listener.log` 与 `errors.log`，退出码非 0。
   若存活锁被持有但版本不是当前（第 2 步被一个旧版本实例接管，理论上仅在安装途中代码又变化时出现），同样按失败报告。
6. 成功打印 `✅ 监听进程已重启（pid N，版本 V）`。

`install.sh` 对这一步失败不中止安装（与 doctor 一样 `|| true`）；`restart-listener` 自己的 `❌` 行已经在安装输出里，
doctor 随后按 4.4 再报告一次。

### 4.3 拉起失败可见

为了查清"第一次拉起没起来"：

- 监听进程 `main()` 最开始（拿锁之前）写一行 `listener starting (pid N, version V)` 到 listener.log。
- 拿存活锁失败时写一行 `listener lock held by another process, exiting`（现在是静默 `return 0`）。

### 4.4 doctor

现有"监听进程"检查（`doctor.py` `_check_listener`）扩展为以下分支，全部基于 `instance()`：

- 监听进程在跑，实例信息的 `code_version` 等于 `code_version()` → `✅ 监听进程运行中（代码为当前版本）`。
- 监听进程在跑，版本不一致或版本未知（`code_version` 为 `None`，含升级前启动的旧进程）→ `❌ 监听进程运行的是旧代码`，`修复：` 重新运行 `install.sh`（或 `claude-lark restart-listener`）。
- 监听进程没在跑，且没有开启中的会话 → 维持现状：`✅ 监听进程未运行（在会话里 /lark 开启桥接时会自动拉起）`。
- 监听进程没在跑，但有开启中的会话 → `❌ 有开启中的会话但监听进程未运行`，`修复：` `claude-lark restart-listener`，
  并提示查看 `listener.log` 与 `errors.log`。

doctor 保持只读："有没有开启中的会话"不能用 `_any_enabled()` 或 `state.is_enabled()`，二者会顺手关闭/删除失效会话的
enabled 文件。新增只读判断函数（例如 `state.is_enabled_readonly()`），与 `is_enabled()` 共用解析逻辑但不删文件，
`_any_enabled()` 的判断部分也复用它。

### 4.5 文档

README 英文正文与中文摘要的升级说明：重跑 `install.sh` 会重启监听进程；doctor 会检查监听进程代码版本。
Install 一节的步骤列表补上重启监听进程这一步。

## 5. 测试要求

- `code_version()` 内容变化时变化、不变时不变。
- 监听进程启动时在 `_pid_lock` 内写实例信息（pid 与版本），退出时删除；pid 不是自己时不删。
- `restart-listener`：
  - 没有监听进程；
  - 有监听进程且有开启会话（停旧、起新、新实例的 `code_version` 为当前）；
  - 有监听进程但没有开启会话（只停不起）；
  - 旧进程不响应 SIGTERM 时走 SIGKILL；
  - 第一次拉起失败时重试一次；
  - 等待期间并发 `ensure_running()` 拉起了新实例：接受交接，不对新旧任何 pid 发 SIGKILL，结果为成功；
  - SIGKILL 前原实例刚好退出：不发 SIGKILL；
  - **迁移**：旧协议监听进程（存活锁被持有，只有 pid 文件，没有实例信息文件）且有开启会话：对 pid 文件中的 pid 发 SIGTERM，
    停旧起新，新实例 `code_version` 为当前，结果为成功；
  - 实例信息文件是残留（其 pid 与 pid 文件不一致）：按版本未知处理，照常停旧起新；
  - 存活锁被持有但 pid 文件缺失：不发信号，报告失败。
- doctor：版本一致、版本不一致、版本未知（无实例信息文件，即旧协议进程）、"无监听且无开启会话"、"有开启会话但无监听"五种情况；
  并断言 doctor 运行后失效会话的 enabled 文件未被删除。

## 6. 留给实现者

- 等待锁释放 / 被持有的轮询间隔。
- `restart-listener` 的输出文案（保持 install.sh / doctor 的 `✅` / `❌` / `修复：` 风格）。
