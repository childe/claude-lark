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

### 4.1 代码版本

- `listener.code_version()`：对 `lark_bridge/*.py` 与 `bin/claude-lark` 的内容按文件名排序后算 sha256，取前 12 位。
- 监听进程拿到存活锁后，把 `code_version()` 写到 `~/.claude/lark/listener.version`（与 pid 文件同处，原子写）；退出放锁时删掉。

### 4.2 重启命令

新增子命令 `claude-lark restart-listener`，`install.sh` 在 doctor 之前调用它。行为：

1. 监听进程没在跑（`listener.lock` 没被持有）→ 跳到第 4 步。
2. 读 pid 文件，发 SIGTERM。pid 文件缺失或读不出时，打印 `❌` 行和修复提示（手动结束 `claude-lark listen` 进程），不继续第 3、4 步，退出码非 0。
3. 等 `listener.lock` 被释放，最多 20 秒（监听进程可能正卡在一次最长 15 秒的 lark-cli 拉取里）。超时后对该 pid 发 SIGKILL，再等最多 5 秒。仍未释放 → `❌` + 修复提示，退出码非 0。
4. 没有开启中的会话（`_any_enabled()` 为假）→ 打印"没有开启中的会话，不需要监听进程"，退出码 0。
5. 有开启中的会话 → `ensure_running()`，然后等最多 5 秒确认 `listener.lock` 被持有。没被持有就再调一次 `ensure_running()` 并再等 5 秒。仍失败 → `❌` 行，修复提示指向 `listener.log` 与 `errors.log`，退出码非 0。
6. 成功打印 `✅ 监听进程已重启（pid N）`。

`install.sh` 对这一步失败不中止安装（与 doctor 一样 `|| true`），doctor 随后会报告。

### 4.3 拉起失败可见

为了查清"第一次拉起没起来"：

- 监听进程 `main()` 最开始（拿锁之前）写一行 `listener starting (pid N, version V)` 到 listener.log。
- 拿存活锁失败时写一行 `listener lock held by another process, exiting`（现在是静默 `return 0`）。

### 4.4 doctor

现有"监听进程运行中"检查扩展：监听进程在跑时读 `listener.version`，与 `code_version()` 比较。

- 一致 → `✅ 监听进程运行中（代码为当前版本）`。
- 不一致或文件不存在 → `❌ 监听进程运行的是旧代码`，`修复：` 重新运行 `install.sh`（或 `claude-lark restart-listener`）。

### 4.5 文档

README 英文正文与中文摘要的升级说明：重跑 `install.sh` 会重启监听进程；doctor 会检查监听进程代码版本。
Install 一节的步骤列表补上重启监听进程这一步。

## 5. 测试要求

- `code_version()` 内容变化时变化、不变时不变。
- 监听进程启动写 version 文件、退出删除。
- `restart-listener`：没有监听进程；有监听进程且有开启会话（停旧、起新、新进程版本为当前）；有监听进程但没有开启会话（只停不起）；旧进程不响应 SIGTERM 时走 SIGKILL；第一次拉起失败时重试一次。
- doctor：版本一致、不一致、version 文件缺失三种情况。

## 6. 留给实现者

- 等待锁释放 / 被持有的轮询间隔。
- `restart-listener` 的输出文案（保持 install.sh / doctor 的 `✅` / `❌` / `修复：` 风格）。
