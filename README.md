# claude-lark

Keep driving a running Claude Code session from your phone, through a Feishu/Lark group.

[中文摘要](#中文摘要) · [For AI agents](#for-ai-agents) · [Install](#install) · [Usage](#usage) · [Limitations](#limitations)

## What it is

You type `/lark` in an interactive Claude Code session. The session is bound to a Feishu/Lark group
(created for you, or one you name). From then on, messages you send in that group become the next
prompt of the same session (full context kept), Claude's final reply of every turn is posted back to
the group, and permission prompts and `AskUserQuestion` multiple-choice questions are forwarded to
the group so you can answer them from your phone. Prompts you type in the terminal are mirrored to
the group too, marked `💻 终端`. No restart and no launch flags: it is built on Claude Code hooks plus
one slash command, with a small background listener process that is shared by all sessions. Runtime
is the Python standard library only.

```
 terminal (claude)            hooks                    listener                 Feishu group
 ----------------        ---------------        ----------------------        ----------------
 /lark  -----------------> bind session  ----->  lark-cli event consume  <---- you (phone): "run the tests"
                           to a group            (one per machine)
 turn ends ------ Stop hook: send final reply ------------------------------> final reply
                       |    then wait for the next group message
                       |<------ inbox <------ message from you ------------- "now fix the failures"
 tool needs approval -- PermissionRequest hook ------------------------------> "Allow `rm -rf build`?"
                       |<------ inbox <---------------------------------------- "同意" / "拒绝"
```

Sessions with the bridge off are untouched: every hook exits 0 immediately with empty output.

## 中文摘要

claude-lark 把一个**正在运行**的 Claude Code 会话桥接到飞书群，方便离开电脑后在手机上继续指挥它。

- 在终端里敲 `/lark [群名]`：自动建群（或绑定已有群，需提供 `oc_` 开头的 chat_id）并开启同步。
- 群里发的消息进入同一个会话；每轮的最终回复发回群里；权限确认和选择题也转到群里，回"同意/拒绝"或编号即可。
- 回到电脑前：先按 `Esc` 拿回终端，再 `/lark off` 停止同步（群里发 `/off` 也行）。
- 安装：`git clone https://github.com/childe/claude-lark ~/.claude/lark-src && ~/.claude/lark-src/install.sh`，然后重启已经开着的 claude 会话。
- 前置：Python ≥ 3.9、Claude Code ≥ 2.1.287、`lark-cli` ≥ 1.0.92，以及一个开了机器人能力、订阅了 `im.message.receive_v1`（长连接）的飞书自建应用。
- 限制：同一个飞书应用的消息事件同一时刻只能被一个监听方消费；续命上限约 41 天；resume 后不自动重连；不能从飞书远程启动 claude。
- 把仓库链接贴给你的 AI 助手说"帮我装"也可以，见 [For AI agents](#for-ai-agents)。

## Prerequisites

Check each item; the command and the expected result are given.

| # | Requirement | Check | Expected |
|---|-------------|-------|----------|
| 1 | macOS (tested) or Linux (expected to work) | `uname -s` | `Darwin` or `Linux` |
| 2 | Python >= 3.9 | `python3 --version` | `Python 3.9.x` or newer |
| 3 | Claude Code >= 2.1.287 | `claude --version` | `2.1.287` or newer |
| 4 | `lark-cli` >= 1.0.92 | `npm i -g @larksuite/cli`, then `lark-cli --version` | `lark-cli version 1.0.92` or newer |
| 5 | A Feishu/Lark self-built app (see below) | `lark-cli event consume im.message.receive_v1 --as bot --dry-run` | JSON where every entry of `preconditions` has `"status": "ok"` |
| 6 | `lark-cli` logged in as both bot and user | `lark-cli auth status` | `identities.bot.status` and `identities.user.status` are both `ready` |

### The Feishu/Lark app (done once, by a human, in the developer console)

1. Create a self-built app in the Feishu Open Platform developer console, and **enable the Bot capability**.
2. Give it the permissions (scopes) below. The list was derived from the real CLI: `lark-cli event schema im.message.receive_v1` (Required Scopes) and the `lark-cli skills read lark-im/...` reference pages of the commands claude-lark calls.

   | Scope | Why it is needed |
   |-------|------------------|
   | `im:message.p2p_msg:readonly` | Required by `im.message.receive_v1` (printed by `lark-cli event schema im.message.receive_v1`). |
   | A group-message receive scope, see warning below | Receiving your messages in the group. |
   | `im:message:send_as_bot` | `lark-cli im +messages-send --as bot`: replies, permission prompts, "connected" notices. |
   | `im:chat:create` | `lark-cli im +chat-create --as bot`: `/lark` creates the group. |
   | `im:chat.members:read` | `lark-cli im +chat-members-list --as bot`: `/lark oc_...` checks that the bot is in the group. |
   | `im:chat:read` (optional) | `lark-cli im +chat-search`, only needed for the "find a chat_id by name" tip below. |

   > **Warning, not verified on a live group yet.** `im:message.p2p_msg:readonly` only covers
   > one-to-one chats. To receive group messages that do **not** @ the bot, the app needs the
   > "read all group messages" style permission (in the console it is listed as receiving all
   > messages of the groups the bot is in). The exact scope name was not confirmed by the CLI and
   > this path has not been exercised against a real group. If it is not available to your app,
   > fall back to **@-mentioning the bot in every message** (the @-only group permission), and
   > please report back what worked.

3. Subscribe to the event **`im.message.receive_v1`**, using the **long connection** (WebSocket) mode, no public URL needed.
4. **Create a version and publish the app** (scopes and events only take effect after publishing and, for a company tenant, admin approval).
5. Run `lark-cli auth login` so that both the bot and the user identity are ready. The user identity is only used to find out who you are: only messages sent by that person are ever processed.

Never paste the App ID, App Secret or tokens into a chat, an issue or a commit.

## Install

```sh
git clone https://github.com/childe/claude-lark ~/.claude/lark-src && ~/.claude/lark-src/install.sh
```

What `install.sh` does, and what you should see (it is idempotent; run it again any time):

1. Checks `python3` >= 3.9 and `lark-cli`. Failure: a line starting with `❌` and exit code 1, nothing is changed.
2. Validates your `~/.claude/settings.json` before touching anything. A hand-broken file aborts the install untouched.
3. Symlinks the repo to `~/.claude/lark/app` and `commands/lark.md` to `~/.claude/commands/lark.md`.
4. Merges six hooks (`Stop`, `PermissionRequest`, `PreToolUse` with matcher `AskUserQuestion`, `UserPromptSubmit`, `SessionStart`, `SessionEnd`) and the env var `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=1000` into `~/.claude/settings.json`, leaving everything else as it was. Output: `已更新 ... （已备份到 ...）` or `无需改动`.
5. Records the logged-in Feishu user as the only allowed owner: `已写入 ~/.claude/lark/config.json`.
6. Runs `claude-lark doctor`. Every line should start with `✅` and the last line should be `全部通过`. A `❌` line is followed by a `修复：` line telling you what to do; install itself still succeeds.

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
   On failure: **[HUMAN]** `lark-cli auth login` opens a browser or prints a link that the human must complete; if no app is configured yet, the human must first do the "Feishu/Lark app" section above (steps 1 to 4 in the developer console: enable the bot, add scopes, subscribe to `im.message.receive_v1` by long connection, publish). Stop and ask the human; resume once they confirm, then re-run `lark-cli auth status`.

4. **Install.** Run `~/.claude/lark-src/install.sh`.
   Success: exit code 0 and the last line says to restart running claude sessions. On failure: read the `❌` line; typical causes are a missing `python3`/`lark-cli` (go back to step 1), an existing non-symlink `~/.claude/lark/app` or `~/.claude/commands/lark.md` (ask the human before moving it away), an invalid `~/.claude/settings.json` (ask the human to fix the JSON; do not overwrite it), or not logged in (step 3).

5. **Run doctor.** Run `~/.claude/lark/app/bin/claude-lark doctor`.
   Success: exit code 0 and the last line `全部通过`, all lines `✅`. On failure: for each `❌` line run the command in the `修复：` line that follows it, and re-run doctor until it is clean. Special cases:
   - `事件订阅前置条件未通过` (event subscription preconditions failed): **[HUMAN]** in the developer console, publish the app, subscribe to `im.message.receive_v1` with the long connection, and grant the scopes from the table above. This cannot be done from the command line. Stop and ask the human; after they confirm, re-run doctor.
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

- Any message becomes the next prompt. Messages that arrive while Claude is busy are queued and delivered together at the end of the turn, in order.
- Permission prompts show the tool and the full command. Reply `同意`, `1` or `y` to allow; `拒绝`, `2` or `n` to deny. Anything else is **not** treated as approval. The terminal dialog stays open too: whoever answers first wins.
- Multiple-choice questions arrive with numbered options: reply with the number (several: separated by commas), or free text for "Other".
- `/off` ends the sync, same as `/lark off`.

**Coming back to your computer: press `Esc` first** to take the terminal back from the waiting hook,
then use the terminal as usual (what you type is still mirrored to the group). Type `/lark off` when
you want syncing to stop.

## Limitations

- **One listener per Feishu app.** A Feishu app's message events must not be consumed by another machine or program at the same time, or each side receives only part of the messages. Use a separate app per machine, or stop the other consumer.
- **Keep-alive limit of about 41 days.** To stay in the waiting state, the Stop hook keeps blocking turn after turn (cap 1000, with a reminder at the 950th); after about 41 days of continuous waiting the session will end the wait and you need to `/lark` again.
- **Resume does not reconnect automatically.** After `claude --resume` the group binding is remembered, but you must type `/lark` to turn the bridge on again (if you resumed in the terminal, you are at the computer).
- **`/clear`, `/resume` and exiting disconnect.** When the bridged conversation ends (`/clear`, switching with `/resume`, or quitting claude), the bridge is turned off and the group gets a notice: 会话已结束（/clear 或退出），桥接已断开。终端里敲 /lark 可重新连接. `/compact` keeps the bridge.
- **No remote start.** Feishu cannot start `claude`. After a reboot, run `claude --resume` at the computer first.
- **Single user.** Only the bound owner is listened to; no multi-person collaboration.
- **Final replies only.** No mid-turn progress (commands run, files read) is sent.
- **Group messages without @:** see the warning in Prerequisites; not yet verified on a live group.
- **Versions and platforms.** Claude Code >= 2.1.287 (the versions it was tested on; hook behaviour is version-sensitive), `lark-cli` >= 1.0.92. macOS is tested; Linux is expected to work; Windows is not supported.
- **Process detection.** claude installed via npm (a `node` process running the claude script) is detected from the process name plus arguments; a differently packaged build that matches neither may make `/lark` answer that it cannot identify the claude process. Please run `claude-lark doctor` and open an issue.
- It does not use Claude Code's channels mechanism (it needs launch flags).

## Uninstall

```sh
~/.claude/lark-src/uninstall.sh           # asks before deleting session data
~/.claude/lark-src/uninstall.sh --purge   # also deletes ~/.claude/lark/sessions without asking
```

It disables the bridge in all sessions, stops the listener, removes only claude-lark's hooks and the
`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` entry from `~/.claude/settings.json` (with a backup), and removes
the two symlinks. Restart running claude sessions afterwards. Delete `~/.claude/lark-src` yourself if you like.

## Upgrade

```sh
git -C ~/.claude/lark-src pull
```

Since `~/.claude/lark/app` is a symlink to the clone, that is all. Re-run `install.sh` if a release
note says hooks changed, and run `claude-lark doctor`.

## Development

Unit tests (stdlib only at runtime; pytest for tests):

```sh
uv run --with pytest pytest tests -v
```

End-to-end suite: drives a real interactive Claude Code in tmux (model: haiku) with a fake `lark-cli`.
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
