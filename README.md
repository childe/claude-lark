# claude-lark

Keep driving a running Claude Code session from your phone, through a Feishu/Lark group.

[中文摘要](#中文摘要) · [For AI agents](#for-ai-agents) · [Install](#install) · [Usage](#usage) · [Limitations](#limitations)

## What it is

You type `/lark` in an interactive Claude Code session. The session is bound to a Feishu/Lark group
(created for you, or one you name). From then on, messages you send in that group become the next
prompt of the same session (full context kept), Claude's final reply of every turn is posted back to
the group, and permission prompts and `AskUserQuestion` multiple-choice questions are forwarded to
the group so you can answer them from your phone. Prompts you type in the terminal are mirrored to
the group too, marked `💻 终端`. The terminal stays usable the whole time: the hook that waits for
group messages runs in the background, so you can type in the terminal or in the group, in any
order, without pressing anything first. No restart and no launch flags: it is built on Claude Code hooks plus
one slash command, with a small background listener process that is shared by all sessions. Runtime
is the Python standard library only.

```
 terminal (claude)            hooks                    listener                 Feishu group
 ----------------        ---------------        ----------------------        ----------------
 /lark  -----------------> bind session  ----->  polls the group as you <---- you (phone): "run the tests"
                           to a group            (one per machine)
 turn ends ------ Stop hook: send final reply ------------------------------> final reply
                       |    then wait in the background (terminal stays free)
 wakes up <------------|<------ inbox <------ message from you ------------- "now fix the failures"
 tool needs approval -- PermissionRequest hook ------------------------------> "Allow `rm -rf build`?"
 (terminal dialog too) |<------ inbox <---------------------------------------- "同意" / "拒绝"
```

Sessions with the bridge off are untouched: every hook exits 0 immediately with empty output.

## 中文摘要

claude-lark 把一个**正在运行**的 Claude Code 会话桥接到飞书群，方便离开电脑后在手机上继续指挥它。

- 在终端里敲 `/lark [群名]`：自动建群（或绑定已有群，需提供 `oc_` 开头的 chat_id）并开启同步。
- 群里发的消息进入同一个会话；每轮的最终回复发回群里；权限确认和选择题也转到群里，回"同意/拒绝"或编号即可。终端里的确认框、选项框同时显示，哪边先答算哪边。
- 终端和飞书可以同时用：等飞书消息的 hook 在后台运行，不占终端，回到电脑前直接在终端输入即可，不用按 `Esc`。想停止同步就 `/lark off`（群里发 `/off` 也行）。
- 模型：Sonnet、Opus 实测可用；Haiku 会拒绝执行从飞书转进来的消息，不支持。
- 安装：`git clone https://github.com/childe/claude-lark ~/.claude/lark-src && ~/.claude/lark-src/install.sh`，然后重启已经开着的 claude 会话。
- 前置：Python ≥ 3.9、Claude Code ≥ 2.1.287、`lark-cli` ≥ 1.0.92，以及一个开了机器人能力的飞书自建应用（不需要订阅事件）。
- 收消息：监听进程以**你的 user 身份**每 2 秒拉一次已开启群的消息，不占用应用的事件长连接，所以同一个应用被别的后端订阅着也不影响。
- 限制：resume 后不自动重连；不能从飞书远程启动 claude。
- 从旧版本升级：`git pull` 之后**重新运行 `install.sh`**（hook 配置变了），再重启已经开着的 claude 会话。
- 把仓库链接贴给你的 AI 助手说"帮我装"也可以，见 [For AI agents](#for-ai-agents)。

## Prerequisites

Check each item; the command and the expected result are given.

| # | Requirement | Check | Expected |
|---|-------------|-------|----------|
| 1 | macOS (tested) or Linux (expected to work) | `uname -s` | `Darwin` or `Linux` |
| 2 | Python >= 3.9 | `python3 --version` | `Python 3.9.x` or newer |
| 3 | Claude Code >= 2.1.287 | `claude --version` | `2.1.287` or newer |
| 4 | `lark-cli` >= 1.0.92 | `npm i -g @larksuite/cli`, then `lark-cli --version` | `lark-cli version 1.0.92` or newer |
| 5 | A Feishu/Lark self-built app with the bot capability (see below) | `lark-cli auth status` | `identities.bot.status` is `ready` |
| 6 | `lark-cli` logged in as user, with the group-read scope | `lark-cli auth status` | `identities.user.status` is `ready` (or `needs_refresh`) and `identities.user.scope` contains `im:message.group_msg:get_as_user` |

claude-lark does **not** subscribe to events. The listener reads the bound groups by polling
`GET /open-apis/im/v1/messages` with your user identity every 2 seconds. Reason: Feishu delivers each
event to only one of the connections of an app, so if another backend already holds a long
connection for the same app, an event consumer here would silently miss most messages.

### The Feishu/Lark app (done once, by a human, in the developer console)

1. Create a self-built app in the Feishu Open Platform developer console, and **enable the Bot capability**.
2. Give it the permissions (scopes) below.

   | Scope | Identity | Why it is needed |
   |-------|----------|------------------|
   | `im:message.group_msg:get_as_user` | user | The listener reads the bound groups' messages as you. |
   | `im:message:send_as_bot` | bot | `lark-cli im +messages-send --as bot`: replies, permission prompts, "connected" notices. |
   | `im:chat:create` | bot | `lark-cli im +chat-create --as bot`: `/lark` creates the group. |
   | `im:chat.members:read` | bot | `lark-cli im +chat-members-list --as bot`: `/lark oc_...` checks that the bot is in the group. |
   | `im:chat:read` (optional) | user | `lark-cli im +chat-search`, only needed for the "find a chat_id by name" tip below. |

3. **Create a version and publish the app** (scopes only take effect after publishing and, for a company tenant, admin approval). No event subscription is needed.
4. Run `lark-cli auth login --scope im:message.group_msg:get_as_user` so that both the bot and the user identity are ready and the user token carries the read scope. The user identity is also how claude-lark knows who you are: only messages sent by that person are ever processed.

Never paste the App ID, App Secret or tokens into a chat, an issue or a commit.

## Install

```sh
git clone https://github.com/childe/claude-lark ~/.claude/lark-src && ~/.claude/lark-src/install.sh
```

What `install.sh` does, and what you should see (it is idempotent; run it again any time):

1. Checks `python3` >= 3.9 and `lark-cli`. Failure: a line starting with `❌` and exit code 1, nothing is changed.
2. Validates your `~/.claude/settings.json` before touching anything. A hand-broken file aborts the install untouched.
3. Symlinks the repo to `~/.claude/lark/app` and `commands/lark.md` to `~/.claude/commands/lark.md`.
4. Merges six hook entries (`Stop` with `asyncRewake`, `PermissionRequest`, two `UserPromptSubmit` entries of which one is `asyncRewake`, `SessionStart`, `SessionEnd`) into `~/.claude/settings.json`, leaving everything else as it was. Entries written by older versions (a `PreToolUse` hook for `AskUserQuestion`, the env var `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=1000`) are removed. Output: `已更新 ... （已备份到 ...）` or `无需改动`.
5. Records the logged-in Feishu user as the only allowed owner: `已写入 ~/.claude/lark/config.json`.
6. Runs `claude-lark doctor`. Every check line should start with `✅` and the last line should be `全部通过`. A `❌` line is followed by a `修复：` line telling you what to do; install itself still succeeds.

**Restart every already-running `claude` session once.** Hooks and the `/lark` command are only loaded at session start.

Check at any time: `~/.claude/lark/app/bin/claude-lark doctor`.

## For AI agents

If a human pasted this repo URL and asked you to install it, follow these steps in order. Run the
command, check the success criterion, and use the failure action if it does not hold. Messages and
doctor output are in Chinese; the `❌` / `✅` markers and the `修复：` (fix) lines are what matter.

**Rules.** Never ask the human to paste an App Secret, token, App ID or open_id into the chat, and
never print or commit them. Steps marked **[HUMAN]** need a person in the Feishu developer console
(or a browser login): stop, tell the human exactly what to do, and wait for them to confirm. Do not
try to work around those. Do not edit `~/.claude/settings.json` by hand: `install.sh` does it safely
with a backup.

1. **Check the platform and tools.**
   Run `uname -s; python3 --version; claude --version; node --version; lark-cli --version`.
   Success: `Darwin` or `Linux`; Python >= 3.9; Claude Code >= 2.1.287; `lark-cli` >= 1.0.92.
   On failure: install what is missing (`brew install python`; `npm i -g @larksuite/cli` or `npm i -g @larksuite/cli@latest`; update Claude Code). If Node/npm is missing, install Node first. On Windows, stop: it is not supported.

2. **Clone.**
   Run `git clone https://github.com/childe/claude-lark ~/.claude/lark-src`.
   Success: `~/.claude/lark-src/install.sh` exists. On failure: if the directory already exists, run `git -C ~/.claude/lark-src pull` instead; if the network fails, tell the human.

3. **Check the Feishu login.** Run `lark-cli auth status`.
   Success: `identities.bot.status` and `identities.user.status` both `ready`.
   On failure: **[HUMAN]** `lark-cli auth login` opens a browser or prints a link that the human must complete; if no app is configured yet, the human must first do the "Feishu/Lark app" section above (steps 1 to 3 in the developer console: enable the bot, add scopes, publish). Stop and ask the human; resume once they confirm, then re-run `lark-cli auth status`.

4. **Install.** Run `~/.claude/lark-src/install.sh`.
   Success: exit code 0 and the last line says to restart running claude sessions. On failure: read the `❌` line; typical causes are a missing `python3`/`lark-cli` (go back to step 1), an existing non-symlink `~/.claude/lark/app` or `~/.claude/commands/lark.md` (ask the human before moving it away), an invalid `~/.claude/settings.json` (ask the human to fix the JSON; do not overwrite it), or not logged in (step 3).

5. **Run doctor.** Run `~/.claude/lark/app/bin/claude-lark doctor`.
   Success: exit code 0 and the last line `全部通过`, every check line `✅`. On failure: for each `❌` line run the command in the `修复：` line that follows it, and re-run doctor until it is clean. Special cases:
   - `user 身份缺少读群消息权限` (user token lacks the group-read scope): **[HUMAN]** run `lark-cli auth login --scope im:message.group_msg:get_as_user` and complete the browser login. If the login reports the scope is not available, the human must first add it to the app in the developer console and publish a new version. Stop and ask the human; after they confirm, re-run doctor.
   - `owner 与当前登录用户不一致`: run `~/.claude/lark-src/install.sh` again.
   - A line about `settings.json` or `lark.md`: run `~/.claude/lark-src/install.sh` again.

6. **Tell the human to restart running claude sessions.** Existing sessions do not have the hooks or `/lark` yet. Say clearly: "restart your claude sessions once; then type `/lark` in one".
   Success: the human confirms. Do not try to restart their sessions yourself.

7. **Optional live check, with the human.** Ask the human to run `/lark` in a fresh session and send a message in the new group. If the group is created but their messages never reach the session, see the group-message warning in Prerequisites (@-mention the bot, or grant the read-all-group-messages permission and republish).

Finish by reporting which steps ran, the doctor result, and the remaining human to-dos.

## Usage

Type these in an interactive Claude Code session:

| Command | What it does |
|---------|--------------|
| `/lark` | Create a group (Claude summarizes the session into a short name), bind it, turn the bridge on. |
| `/lark <name>` | Same, with your group name (at most 60 characters). Avoid quotes, `$` and backticks in names: the text goes through a shell command line. |
| `/lark oc_xxxxxxxx` | Bind an existing group by its `chat_id` (starts with `oc_`). The bot must already be in the group (add it in the group settings). **Group links are not supported**: pass the `oc_` id. Find it with `lark-cli im +chat-search --query <name>` (needs `im:chat:read`) or from the group's settings in Feishu. |
| `/lark off` | Stop syncing (the binding is kept; the group gets a "disconnected" message). |
| `/lark status` | Show the bound group, whether the bridge is on, and whether the listener process is alive. |

If the session already has a bound group, `/lark` reuses it and ignores a group name argument.
`/lark <chat_id>` on an already bound session is refused: run `/lark off` first. A group can be
used by only one active session at a time.

In the group (only the person who ran `lark-cli auth login` is listened to; others are ignored):

- Any message becomes the next prompt. When the session is idle, it wakes up right away. When Claude is busy, the message is handed to it during the turn (or at the end of it); several messages are merged in order. Nothing is dropped.
- Permission prompts show the tool and the full command. Reply `同意`, `1` or `y` to allow; `拒绝`, `2` or `n` to deny. Anything else is **not** treated as approval. The terminal dialog stays open too: whoever answers first wins. If you answer in the terminal, the group gets `已在终端处理`.
- Multiple-choice questions arrive with numbered options: reply with the number (several: separated by commas), or free text for "Other". The terminal shows the same options at the same time; whoever answers first wins.
- `/off` ends the sync, same as `/lark off`.

**The terminal stays usable.** The hook that waits for group messages runs in the background, so
when you are back at the computer just type in the terminal; what you type is mirrored to the group
and the reply goes to both. Type `/lark off` when you want syncing to stop.

**Model.** Messages from the group reach Claude as hook output. Sonnet and Opus treat them as your
prompts (the `/lark` command tells them to). Haiku refuses to act on them, so the bridge does not
work with Haiku.

## Limitations

- **One listener per Feishu app.** A Feishu app's message events must not be consumed by another machine or program at the same time, or each side receives only part of the messages. Use a separate app per machine, or stop the other consumer.
- **A keep-alive turn about once a day.** A background hook can wait at most 24 hours. After about 24 hours of silence it wakes Claude once with a keep-alive prompt (Claude replies `waiting`, which is not sent to the group), and the next wait starts.
- **Haiku is not supported.** See [Usage](#usage).
- **Resume does not reconnect automatically.** After `claude --resume` the group binding is remembered, but you must type `/lark` to turn the bridge on again (if you resumed in the terminal, you are at the computer).
- **`/clear`, `/resume` and exiting disconnect.** When the bridged conversation ends (`/clear`, switching with `/resume`, or quitting claude), the bridge is turned off and the group gets a notice: 会话已结束（/clear 或退出），桥接已断开。终端里敲 /lark 可重新连接. `/compact` keeps the bridge.
- **No remote start.** Feishu cannot start `claude`. After a reboot, run `claude --resume` at the computer first.
- **Single user.** Only the bound owner is listened to; no multi-person collaboration.
- **Final replies only.** No mid-turn progress (commands run, files read) is sent.
- **Group messages without @:** see the warning in Prerequisites; not yet verified on a live group.
- **Versions and platforms.** Claude Code >= 2.1.287 (the version it was tested on; the background wait relies on the `asyncRewake` hook option, and hook behaviour is version-sensitive; `claude-lark doctor` checks the version), `lark-cli` >= 1.0.92. macOS is tested; Linux is expected to work; Windows is not supported.
- **Process detection.** claude installed via npm (a `node` or `bun` process running the claude script) is detected from the process name plus arguments; a differently packaged build that matches neither may make `/lark` answer that it cannot identify the claude process. Please run `claude-lark doctor` and open an issue.
- It does not use Claude Code's channels mechanism (it needs launch flags).

## Uninstall

```sh
~/.claude/lark-src/uninstall.sh           # asks before deleting session data
~/.claude/lark-src/uninstall.sh --purge   # also deletes ~/.claude/lark/sessions without asking
```

It disables the bridge in all sessions, stops the listener, removes only claude-lark's hooks (and the
`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` entry older versions wrote) from `~/.claude/settings.json` (with a backup), and removes
the two symlinks. Restart running claude sessions afterwards. Delete `~/.claude/lark-src` yourself if you like.

## Upgrade

```sh
git -C ~/.claude/lark-src pull
```

Since `~/.claude/lark/app` is a symlink to the clone, code changes take effect right away. When the
hook configuration changed (for example the upgrade to the background wait), re-run `install.sh` and
restart running claude sessions; `claude-lark doctor` reports outdated hooks.

## Development

Unit tests (stdlib only at runtime; pytest for tests):

```sh
uv run --with pytest pytest tests -v
```

End-to-end suite: drives a real interactive Claude Code in tmux (model: sonnet) with a fake `lark-cli`.
**It makes real model calls and costs tokens.** Needs `tmux`, a logged-in `claude`, `python3`.

```sh
tests/e2e/run_e2e.sh                  # all scenarios
E2E_ONLY="2 3" tests/e2e/run_e2e.sh   # a subset (scenario 1 always runs first)
```

See `tests/e2e/README.md` for the scenarios and the isolation it uses (it does not touch your real `~/.claude/settings.json`).

Format with `black .`.

Secret scanner: `scripts/scan-secrets.sh` rejects app ids, open/chat ids, tokens, local absolute paths
and anything listed in a local, git-ignored `.secret-patterns` file (one regex per line; put
organisation-specific words there, never in the repo). With no arguments it scans the staged diff;
with arguments it scans those files. Use obviously fake test values such as `ou_test_owner`.

Install it as a pre-commit hook. In a git worktree `.git` is a file and a relative symlink in the
hooks directory does not resolve, so use a small wrapper script instead of `ln -s`:

```sh
HOOKS="$(git rev-parse --git-path hooks)"
ROOT="$(git rev-parse --show-toplevel)"
cat > "$HOOKS/pre-commit" <<HOOK
#!/usr/bin/env bash
exec "$ROOT/scripts/scan-secrets.sh"
HOOK
chmod +x "$HOOKS/pre-commit"
```

## License

MIT, see [LICENSE](LICENSE).
