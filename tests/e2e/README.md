# 端到端测试

`run_e2e.sh` 在 tmux 里启动一个真实的交互式 Claude Code（haiku，`--permission-mode default`），
挂上真实的 hooks 和 `/lark` 命令，用 `tests/fake_lark_cli.py` 代替飞书，逐个验证场景。

```sh
tests/e2e/run_e2e.sh            # 临时目录建在 .superpowers/e2e/ 下（已 gitignore）
tests/e2e/run_e2e.sh <基目录>    # 指定临时目录的上级目录
E2E_ONLY="2 3" tests/e2e/run_e2e.sh   # 只跑部分场景（场景 1 总会先跑）
```

前提：`tmux`、`claude`（已登录）、`python3` 在 PATH 里。会真实调用模型，消耗少量 token。

## 隔离方式

- 不碰 `~/.claude/settings.json` 和 `~/.claude/commands`：临时 project 的 `.claude/settings.json`
  由 `settings.merge({})` 生成，`.claude/commands/lark.md` 拷自 `commands/lark.md`，
  两者里的 `$HOME/.claude/lark/app/bin/claude-lark` / `~/.claude/lark/app/bin/claude-lark`
  都改写成本仓库 `bin/claude-lark` 的绝对路径。
- 状态目录 `CLAUDE_LARK_HOME`、假 `CLAUDE_LARK_CLI`、`FAKE_LARK_LOG`、`FAKE_LARK_EVENTS`、
  `CLAUDE_LARK_WAIT_SECONDS=20`、`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=1000` 都通过 tmux 里 claude 的命令行环境变量传入。
- 群消息：往 `FAKE_LARK_EVENTS` 追加一行事件 JSON；假 lark-cli 的 `event consume` 像 `tail -f` 一样跟随。
- 唯一会写到真实 `~/.claude` 的是 Claude Code 自己记录的"信任此目录"和会话 transcript。

## 场景

1. `/lark e2e测试` → `+chat-create --name e2e测试`，群里收到"已连接"，终端回复"已开启桥接"
2. 群消息"只回复 PONG" → 回复 PONG 发到群里
3. 群消息让 Claude `touch e2e_ok` → 群里收到权限请求 → 回复"同意" → 文件存在
4. 同上，`touch e2e_no` → 回复"拒绝" → 文件不存在
5. 群消息让 Claude 用 AskUserQuestion 问颜色 → 回复"2" → 最终回复含第二个选项
6. Stop hook 等待时按 Esc → 终端输入"只回复 LOCAL" → 群里收到 `💻 终端：只回复 LOCAL`
7. 非 owner 发消息 → 不进收件箱、没有新回合
8. 群里 `/off` → 绑定保留、`enabled` 消失，下一轮 Stop hook 不阻塞也不发消息
9. 续命：空闲 20 秒后出现一次 waiting 回合，群里不会收到 "waiting"
10. `/clear` → 旧会话的 SessionEnd 删掉 `enabled`，群里收到"会话已结束（/clear 或退出），桥接已断开…"，绑定保留
11. `/compact` → SessionStart(source=compact) 不断开，`enabled` 仍在（场景 10、11 在桥接没开时会先 `/lark` 重新开启）

每个场景打印 `PASS` / `FAIL <原因>`；失败时打印终端最后 30 行、`errors.log` 和假 lark-cli 日志。
退出时（trap）关掉 tmux 会话、监听进程和残留的 hook 进程。
