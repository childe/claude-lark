# 自动开启 /lark 与群名跟随会话名 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 加一个全局开关 `auto_on`，开启后交互式会话在第一句之后自动建群（resume 时接回已绑的群）；claude-lark 自己建的群在会话改名后跟着改群名。

**Architecture:** 自动建群放在已有的后台 `wait` hook（UserPromptSubmit，asyncRewake）里，在 `hooks.run()` 的 "未开启就返回" 判断之前分流；resume 接回放在新增的后台 `session-resume` SessionStart hook。两者都走 `commands.auto_connect()`，它与手动 `/lark` 共用会话目录里的 `connect.lock`。群名同步放在监听进程每轮消息轮询之后，读 `~/.claude/sessions/<pid>.json` 的 `name`。binding 与 enabled 的读改写分别加 `binding.lock`、`enabled.lock`，锁序固定 `connect.lock` → `binding.lock` → `enabled.lock`。

**Tech Stack:** Python ≥ 3.9 标准库（fcntl、dataclasses、subprocess），pytest，`lark-cli` 子进程（测试用 `tests/fake_lark_cli.py`）。

**Spec:** `docs/superpowers/specs/2026-11-22-auto-lark-rename-sync-design.md`

## Global Constraints

- 开关字段：`~/.claude/lark/config.json` 的布尔 `auto_on`，缺省 `false`；写入保留其他字段（`owner_open_id` 等），原子写。
- 开关命令：参数**整串**等于 `auto on` / `auto off` 才算；其他以 `auto` 开头的参数按群名处理。
- 自动群名：prompt 第一行去首尾空白后截到 **20** 字符；为空用会话名；再为空用 cwd 目录名。
- 改群名命令：`lark-cli im +chat-update --as bot --chat-id <chat_id> --name <name 截到 60 字符>`，参数以列表传，超时用 `listener.SEND_TIMEOUT`（10 秒）。
- 改名失败：写 `errors.log`；同一 binding **60 秒**内不重试；每个 binding 只在群里发一次 `"改群名失败，可能缺少 im:chat:update 权限，详见 errors.log"`。
- `nameSource` 为 `user` 和 `derived` 都同步；只改 `created_by_bridge` 为真的群。
- 不处理 `kind != "interactive"` 的会话；不在会话启动（还没有第一句）时建群；resume 路径从不建群。
- 新 hook 条目：`("SessionStart", None, "session-resume", 86400, _ASYNC)`；原 `session-start` 条目（同步，超时 10）不变。
- 锁序 `connect.lock` → `binding.lock` → `enabled.lock`；`enabled.lock` 是叶子锁；三把锁都不跨越等待飞书消息的阶段；`enabled.lock` 与 `binding.lock` 内不做网络调用和子进程。
- hook 出任何错只写 `errors.log`，安静退出（沿用 `hooks.run` 的约定）。
- 每次改代码后用 black 格式化；测试命令 `uv run --with pytest pytest tests -v`。

## Review Focus

1. 首句第一行为空白、只有换行、超长或含 emoji：群名按 20 字符截断，空时退回会话名再退回目录名，不能建出空名群（Task 4 `test_auto_name_fallbacks`）。
2. 元数据文件损坏、属于别的 session（pid 被复用）、`kind` 不是 `interactive`：自动建群与改名都跳过，不抛异常（Task 2 `test_meta_rejects_*`、Task 6 `test_rename_skips_foreign_meta`）。
3. 升级前留下的旧 binding 文件（没有新字段）：照常加载，`created_by_bridge` 按 `chat_name != chat_id` 推断，第一次观测只记基线不改名（Task 3 `test_legacy_binding_defaults`、Task 6 `test_rename_legacy_binding_records_baseline_only`）。
4. `/lark auto`、`/lark auto onx`、`/lark  auto on `（首尾空白）：前两个按群名处理，最后一个去空白后是开关命令（Task 1 `test_auto_args_must_match_whole_string`）。
5. 同一轮里连续两次 `/rename`、或改名接口持续失败：只同步最新的名字；失败 60 秒内不重试、群里只提示一次（Task 6 `test_rename_failure_cooldown_and_single_notice`）。
6. 自动开启时拉起监听进程失败：不留 enabled、保留 `auto_pending`，下一句只重试开启（Task 4 `test_auto_connect_retries_after_listener_spawn_failure`、Task 5 `test_wait_hook_recovers_after_listener_spawn_failure`）。
7. `prompt` 与 `wait` 两个 hook 任意先后、交错：首句恰好镜像一次；认领过的首句不吞掉之后文本相同的新输入（Task 3 `test_first_mirror_*`、Task 5 `test_first_prompt_mirrored_once_*`、`test_later_identical_prompt_still_mirrored`）。

---

### Task 1: 配置开关 `auto_on` 与 `/lark auto on|off`

**Files:**
- Create: `lark_bridge/config.py`
- Modify: `lark_bridge/commands.py`（`_slash`、`_status`、`_owner`）
- Modify: `install.sh`（第 5 步改用 `config.set_owner`）
- Test: `tests/test_config.py`、`tests/test_commands.py`

**Interfaces:**
- Produces:
  - `config.load() -> dict`：读不到 / 不是对象时返回 `{}`。
  - `config.auto_on() -> bool`：`load().get("auto_on") is True`。
  - `config.set_auto_on(on: bool) -> None`：重读、改字段、`state._atomic_write` 写回（`indent=2, ensure_ascii=False`）。
  - `config.set_owner(open_id: str) -> None`：同上，只改 `owner_open_id`。
  - `commands.AUTO_ON_TEXT = "已开启自动连接：新会话发出第一句后自动建群并开启桥接"`，`commands.AUTO_OFF_TEXT = "已关闭自动连接"`。

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py`：

```python
def test_auto_on_defaults_false(lark_home):
    assert config.auto_on() is False

def test_set_auto_on_preserves_other_fields(lark_home):
    config.set_auto_on(True)
    data = json.loads((lark_home / "config.json").read_text())
    assert data == {"owner_open_id": "ou_test_owner", "auto_on": True}
    config.set_auto_on(False)
    assert config.auto_on() is False
    assert config.load()["owner_open_id"] == "ou_test_owner"

def test_set_owner_keeps_auto_on(lark_home):
    config.set_auto_on(True)
    config.set_owner("ou_new")
    assert config.load() == {"owner_open_id": "ou_new", "auto_on": True}

def test_load_tolerates_garbage(lark_home):
    (lark_home / "config.json").write_text("[1,2]")
    assert config.load() == {} and config.auto_on() is False
    config.set_auto_on(True)
    assert config.load() == {"auto_on": True}
```

`tests/test_commands.py` 追加：

```python
def test_slash_auto_on_off(fake):
    assert commands.slash(SID, "auto on") == f"RESULT: {commands.AUTO_ON_TEXT}"
    assert config.auto_on() is True
    assert commands.slash(SID, "auto off") == f"RESULT: {commands.AUTO_OFF_TEXT}"
    assert config.auto_on() is False
    assert fake() == []

def test_auto_args_must_match_whole_string(fake):
    assert commands.slash(SID, "  auto on ") == f"RESULT: {commands.AUTO_ON_TEXT}"
    commands.slash(SID, "auto off")
    commands.slash(SID, "auto onx")           # 按群名建群
    create = [c for c in fake() if c[:2] == ["im", "+chat-create"]]
    assert arg(create[0], "--name") == "auto onx"
    assert config.auto_on() is False

def test_slash_status_shows_auto(fake):
    assert commands.slash(SID, "status").endswith("；自动连接未开启")
    config.set_auto_on(True)
    assert commands.slash(SID, "status").endswith("；自动连接已开启")
```

同时把现有 `test_slash_status` 第 189 行的 `endswith("桥接未开启，监听进程未运行")` 改成 `endswith("桥接未开启，监听进程未运行；自动连接未开启")`。

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --with pytest pytest tests/test_config.py tests/test_commands.py -v`
Expected: FAIL（`ModuleNotFoundError: lark_bridge.config`）

- [ ] **Step 3: Implement**

`config.py` 按 Interfaces 实现。`commands._slash` 在 `off` / `status` 判断旁加 `args == "auto on"` / `args == "auto off"` 分支（`args` 已 strip），不需要 owner。`_status` 两种返回都追加 `f"；自动连接{'已开启' if config.auto_on() else '未开启'}"`。`_owner()` 改为读 `config.load()`。`install.sh` 第 5 步的 Python 片段改为 `config.set_owner(oid)` 后打印 `已写入 <路径>`。

- [ ] **Step 4: Run to verify they pass**

Run: `uv run --with pytest pytest tests -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add lark_bridge/config.py lark_bridge/commands.py install.sh tests/test_config.py tests/test_commands.py
git commit -m "feat: add auto_on switch and /lark auto on|off"
```

---

### Task 2: 读取 Claude Code 会话元数据

**Files:**
- Modify: `lark_bridge/paths.py`
- Create: `lark_bridge/meta.py`
- Test: `tests/test_meta.py`

**Interfaces:**
- Produces:
  - `paths.claude_sessions_dir() -> Path`：环境变量 `CLAUDE_LARK_CLAUDE_SESSIONS` 优先，否则 `~/.claude/sessions`。
  - `meta.read(pid: int, sid: str) -> Optional[dict]`：读 `<dir>/<pid>.json`；不存在、解析失败、不是对象、`sessionId != sid` 时返回 `None`。
  - `meta.is_interactive(pid: int, sid: str) -> bool`：`read()` 非 None 且 `kind == "interactive"`。
  - `meta.session_name(pid: int, sid: str) -> str`：`name` 去首尾空白；取不到或非字符串返回 `""`。
- 后续测试共用的写元数据辅助函数放在 `tests/conftest.py`：`write_meta(dir, pid, sid, name="rw-1", kind="interactive", source="derived")`，以及 fixture `claude_sessions(tmp_path, monkeypatch) -> Path`（建目录并设置 `CLAUDE_LARK_CLAUDE_SESSIONS`）。

- [ ] **Step 1: Write the failing tests**

```python
def test_meta_reads_name(claude_sessions):
    write_meta(claude_sessions, 4242, SID, name="  测试改名 abc ", source="user")
    assert meta.is_interactive(4242, SID)
    assert meta.session_name(4242, SID) == "测试改名 abc"

def test_meta_rejects_other_session(claude_sessions):
    write_meta(claude_sessions, 4242, "ffffffff-0000-0000-0000-000000000000")
    assert meta.read(4242, SID) is None
    assert not meta.is_interactive(4242, SID) and meta.session_name(4242, SID) == ""

@pytest.mark.parametrize("content", ["", "not json", "[]", '{"sessionId": 1}'])
def test_meta_rejects_garbage(claude_sessions, content):
    (claude_sessions / "4242.json").write_text(content)
    assert meta.read(4242, SID) is None

def test_meta_non_interactive(claude_sessions):
    write_meta(claude_sessions, 4242, SID, kind="print")
    assert not meta.is_interactive(4242, SID)

def test_meta_missing_file(claude_sessions):
    assert meta.read(99999, SID) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --with pytest pytest tests/test_meta.py -v`
Expected: FAIL（`ImportError`）

- [ ] **Step 3: Implement `paths.claude_sessions_dir` and `lark_bridge/meta.py` per Interfaces**

- [ ] **Step 4: Run to verify they pass**

Run: `uv run --with pytest pytest tests/test_meta.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lark_bridge/paths.py lark_bridge/meta.py tests/conftest.py tests/test_meta.py
git commit -m "feat: read Claude Code session metadata"
```

---

### Task 3: state 的新字段、三把锁与 `auto_pending`

**Files:**
- Modify: `lark_bridge/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces:
  - `Binding` 新增 `created_by_bridge: Optional[bool] = None`、`synced_name: Optional[str] = None`；`__post_init__` 里 `created_by_bridge is None` 时设为 `chat_name != chat_id`。`synced_name is None` 表示"旧 binding，还没有基线"。
  - `state.connect_lock(sid)`：上下文管理器，会话目录 `connect.lock` 的阻塞 flock。
  - `state.save_binding(b)`：改为在 `binding.lock` 里写。
  - `state.update_binding(sid, fn, default: Optional[Binding] = None) -> Optional[Binding]`：`binding.lock` 内重读；读不到时用 `default`，两者都没有返回 `None` 且不写；否则 `fn(b)` 原地修改后原子写回并返回 `b`。`fn` 内不得做网络调用。
  - `state.set_enabled(sid, on, claude_pid=None)`：写入和删除都在 `enabled.lock` 里；`procs.start_time()` 在拿锁**之前**算好。
  - `state.disable_if_foreign(sid, pid: int, start_time: str) -> bool`：`enabled.lock` 内重读 enabled；内容等于 `f"{pid}\n{start_time}"`（空白归一后比较）就保留并返回 `False`；否则删除并返回 `True`（文件不存在返回 `False`）。读完之后、判断之前调用模块级钩子 `state._after_foreign_read`（默认 `None`，仅测试注入）。
  - `is_enabled()` 判定失效后的删除改为：`enabled.lock` 内重读，内容仍等于当初读到的才删。
  - `state.mark_auto_pending(sid)`、`state.has_auto_pending(sid) -> bool`、`state.clear_auto_pending(sid)`：会话目录里的 `auto_pending` 文件，`clear` 对不存在的文件不报错。
  - 首句镜像认领（保证首句恰好镜像一次，不依赖 `prompt` 与 `wait` 两个 hook 的执行顺序）：
    - 输入事件标识：`(text, at)`。`at` 是该次输入触发 hook 时的墙钟时间（`time.time()`，在各 hook 入口取，早于建群等耗时步骤）。Claude Code 对同一次输入**同时**拉起 `prompt` 与 `wait` 两个 hook 进程，二者的 `at` 只差进程启动时间；UserPromptSubmit 的 hook 输入没有每次输入唯一的 id，所以用"文本相同且 `at` 相差不超过 `state.FIRST_MIRROR_WINDOW = 10.0` 秒"判定为同一次输入。
    - 状态文件两个，都在会话目录：`first_mirror`（待认领）与 `first_mirror.claimed`（已被一方认领、另一方还没确认），内容都是 JSON `{"text": ..., "at": ...}`。
    - `state.write_first_mirror(sid, text, at)`：先删除残留的 `first_mirror.claimed`，再原子写 `first_mirror`。
    - `state.claim_first_mirror(sid, text, at) -> Optional[bool]`，按顺序：
      1. 读 `first_mirror`：存在且与 `(text, at)` 匹配时 `os.rename(first_mirror, first_mirror.claimed)`；成功返回 `True`（本调用方负责镜像）；`FileNotFoundError`（对方先改名）转第 2 步。存在但不匹配返回 `None`。不存在转第 2 步。
      2. 读 `first_mirror.claimed`：存在且与 `(text, at)` 匹配时删除它（两方都已见过这次输入，容忍 `FileNotFoundError`）并返回 `False`（另一方已认领）；存在但不匹配（过期残留）时删除它并返回 `None`；不存在返回 `None`。
      `rename` 与 `unlink` 都是原子的：两方并发认领只有一方得到 `True`，后到的一方一定读到 `first_mirror.claimed` 而返回 `False`，不会因为"文件已不存在"误判为无关输入。
      清理规则：第二方确认时删除 `first_mirror.claimed`；`prompt` hook 在开启前就跑完（从不认领）时，`first_mirror.claimed` 留到下一次已开启的 `prompt` hook，因 `at` 超出窗口被当作过期残留删除，该次输入照常镜像；`write_first_mirror` 与 `drop_first_mirror` 也会清掉它。残余风险：开启后 10 秒内再次提交与首句完全相同的文本，这一句不镜像（至多漏镜像一条，不重复发送）。
    - `state.drop_first_mirror(sid)`：删除 `first_mirror` 与 `first_mirror.claimed`，对不存在的文件不报错。

- [ ] **Step 1: Write the failing tests**

```python
def test_legacy_binding_defaults(lark_home):
    p = paths.session_dir(SID) / "binding.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"session_id": SID, "chat_id": "oc_x", "chat_name": "群",
                             "cwd": "/w", "claude_pid": 1, "created_at": 0}))
    b = state.load_binding(SID)
    assert b.created_by_bridge is True and b.synced_name is None
    p.write_text(json.dumps({"session_id": SID, "chat_id": "oc_x", "chat_name": "oc_x",
                             "cwd": "/w", "claude_pid": 1, "created_at": 0}))
    assert state.load_binding(SID).created_by_bridge is False

def test_update_binding_rereads(lark_home):
    state.save_binding(mk(pid=1))
    stale = state.load_binding(SID)
    state.update_binding(SID, lambda b: setattr(b, "claude_pid", 2))
    state.update_binding(SID, lambda b: setattr(b, "synced_name", "n"))
    b = state.load_binding(SID)
    assert (b.claude_pid, b.synced_name) == (2, "n") and stale.claude_pid == 1

def test_update_binding_missing_without_default(lark_home):
    assert state.update_binding(SID, lambda b: None) is None
    assert state.load_binding(SID) is None
    assert state.update_binding(SID, lambda b: None, default=mk()).chat_id == "oc_test_chat"

def test_disable_if_foreign_keeps_own(lark_home):
    me = os.getpid()
    state.set_enabled(SID, True, me)
    assert state.disable_if_foreign(SID, me, procs.start_time(me)) is False
    assert state.is_enabled(SID)

def test_disable_if_foreign_removes_other(lark_home):
    state.set_enabled(SID, True, os.getppid())
    me = os.getpid()
    assert state.disable_if_foreign(SID, me, procs.start_time(me)) is True
    assert not (paths.session_dir(SID) / "enabled").exists()

def test_disable_if_foreign_interleaved_with_enable(lark_home, monkeypatch):
    """清理读到旧值后暂停；接回写入当前进程的值必须等清理结束。"""
    me = os.getpid()
    state.set_enabled(SID, True, os.getppid())          # 旧进程留下的
    paused, go = threading.Event(), threading.Event()
    def hook():
        paused.set(); go.wait(5)
    monkeypatch.setattr(state, "_after_foreign_read", hook)
    t = threading.Thread(target=state.disable_if_foreign, args=(SID, me, procs.start_time(me)))
    t.start(); assert paused.wait(5)
    w = threading.Thread(target=state.set_enabled, args=(SID, True, me)); w.start()
    w.join(0.3); assert w.is_alive()                    # 被 enabled.lock 挡住
    go.set(); t.join(5); w.join(5)
    content = (paths.session_dir(SID) / "enabled").read_text()
    assert content.split("\n")[0] == str(me) and state.is_enabled(SID)

def test_auto_pending_marker(lark_home):
    assert not state.has_auto_pending(SID)
    state.mark_auto_pending(SID); assert state.has_auto_pending(SID)
    state.clear_auto_pending(SID); state.clear_auto_pending(SID)
    assert not state.has_auto_pending(SID)

T0 = 1000.0

def test_first_mirror_claim(lark_home):
    assert state.claim_first_mirror(SID, "a", T0) is None       # 没有标记
    state.write_first_mirror(SID, "a", T0)
    assert state.claim_first_mirror(SID, "b", T0) is None       # 文本不同
    assert state.claim_first_mirror(SID, "a", T0 + 60) is None  # 不是同一次输入
    assert state.claim_first_mirror(SID, "a", T0 + 1) is True   # 第一方认领
    assert state.claim_first_mirror(SID, "a", T0 + 2) is False  # 第二方：已被认领
    assert state.claim_first_mirror(SID, "a", T0 + 3) is None   # 两方都确认过，状态已清
    d = paths.session_dir(SID)
    assert not (d / "first_mirror").exists() and not (d / "first_mirror.claimed").exists()

def test_first_mirror_stale_claimed_does_not_swallow_later_input(lark_home):
    """prompt hook 在开启前跑完、从不认领：残留的 claimed 不吞掉之后文本相同的新输入。"""
    state.write_first_mirror(SID, "a", T0)
    assert state.claim_first_mirror(SID, "a", T0) is True       # 只有 wait 一方认领
    assert state.claim_first_mirror(SID, "a", T0 + 60) is None  # 下一次输入：过期残留
    assert not (paths.session_dir(SID) / "first_mirror.claimed").exists()

def test_first_mirror_write_and_drop_clear_claimed(lark_home):
    state.write_first_mirror(SID, "a", T0); state.claim_first_mirror(SID, "a", T0)
    state.write_first_mirror(SID, "b", T0 + 60)
    assert not (paths.session_dir(SID) / "first_mirror.claimed").exists()
    state.claim_first_mirror(SID, "b", T0 + 60)
    state.drop_first_mirror(SID); state.drop_first_mirror(SID)
    d = paths.session_dir(SID)
    assert not (d / "first_mirror").exists() and not (d / "first_mirror.claimed").exists()

def test_first_mirror_claim_race(lark_home, monkeypatch):
    """两方都读到匹配的 first_mirror 后，只有先 rename 的一方得到 True，后到的一方得到 False。"""
    state.write_first_mirror(SID, "a", T0)
    real_rename = os.rename
    seen = []
    def rename_after_other(src, dst, *a, **k):
        monkeypatch.setattr(os, "rename", real_rename)
        seen.append(state.claim_first_mirror(SID, "a", T0))     # 对方抢先改名
        real_rename(src, dst, *a, **k)                          # 本方改名时源已不存在
    monkeypatch.setattr(os, "rename", rename_after_other)
    assert state.claim_first_mirror(SID, "a", T0) is False
    assert seen == [True]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --with pytest pytest tests/test_state.py -v`
Expected: FAIL（`AttributeError: ... update_binding` 等）

- [ ] **Step 3: Implement per Interfaces**

三把锁用同一个内部辅助 `_blocking_lock(sid, name)`（与现有 `_consumed_lock` 同形，可把 `_consumed_lock` 改为调用它）。`save_binding` 内部写入抽成 `_write_binding(b)`，由 `save_binding` 和 `update_binding` 在 `binding.lock` 内调用，避免重入同一把锁。

- [ ] **Step 4: Run to verify they pass**

Run: `uv run --with pytest pytest tests -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add lark_bridge/state.py tests/test_state.py
git commit -m "feat: binding sync fields, connect/binding/enabled locks, auto_pending and first_mirror markers"
```

---

### Task 4: `commands.auto_connect` 与手动路径加锁

**Files:**
- Modify: `lark_bridge/commands.py`、`lark_bridge/listener.py`（`ensure_running` 返回值）
- Test: `tests/test_commands.py`、`tests/test_listener.py`

**Interfaces:**
- Consumes: `config.auto_on()`、`meta.is_interactive()`、`meta.session_name()`（Task 1、2）；`state.connect_lock`、`update_binding`、`mark_auto_pending`、`has_auto_pending`、`clear_auto_pending`、`write_first_mirror`、`drop_first_mirror`（Task 3）。
- Produces:
  - `listener.ensure_running() -> bool`：监听进程已在跑（`_lock_held_by_other()` 为真）或本次 `Popen` 成功返回 `True`；捕获到异常时照旧 `log_exception` 并返回 `False`。仍然不抛出。
  - `_enable` 在 `set_enabled` 且 `is_enabled` 通过之后调用 `listener.ensure_running()`；返回 `False` 时 `state.set_enabled(sid, False)`、`log_error`，并返回 `_result("拉起监听进程失败，未开启桥接，详见 errors.log")`。这样开启失败时 enabled 不留下，`auto_pending` 不被清除，下一句的 `wait` hook 仍走未开启分支（`_auto_wait`）重试。
  - `commands.AUTO_NAME_MAX = 20`
  - `commands.auto_name(prompt: str, session_name: str, cwd: str) -> str`：按 Global Constraints 的群名规则。
  - `commands.auto_connect(sid: str, source: str, prompt: str = "", at: Optional[float] = None) -> str`：`at` 是输入事件时间，`None` 时取 `time.time()`；返回 `"created"`（本次建群并开启）、`"enabled"`（复用已有 binding 并开启）或 `""`（没做或失败）。`source` 取 `"prompt"` 或 `"resume"`。自身不抛出，异常走 `feishu.log_exception("commands.auto_connect")`。
  - `_create_and_enable(sid, name, owner, pid, auto=False)`：binding 写入 `created_by_bridge=True`、`synced_name=meta.session_name(pid, sid)`；`auto=True` 时**先** `mark_auto_pending` 再落 binding（都在调用方持有的 `connect.lock` 内）。
  - `_bind_existing` 写入 `created_by_bridge=False`、`synced_name=meta.session_name(pid, sid)`。
  - `_enable` 写 `claude_pid` / `cwd` 改用 `state.update_binding(sid, fn, default=b)`；开启成功（`state.is_enabled(sid)` 为真**且** `listener.ensure_running()` 返回 `True`）后调用 `state.clear_auto_pending(sid)`。
  - `auto_connect` 在 `source == "prompt"` 的"建群并开启"与"只重试开启"两条路径上，于锁内、调用 `_enable` **之前**执行 `state.write_first_mirror(sid, prompt, at)`；开启失败（返回 `""`）时 `state.drop_first_mirror(sid)`。resume 路径不写。
  - `_slash` 与 `_on` 的 "读 binding → 决定 → 建群/绑定/开启" 整段放进 `state.connect_lock(sid)`。
  - `_off` 在 `connect_lock` 内先 `clear_auto_pending`，再读 binding、断开。

`auto_connect` 的判定顺序（任何一条不满足返回 `""`）：

1. `config.auto_on()`；
2. `source == "prompt"` 时：`prompt.lstrip()` 不以 `/lark` 或 `<task-notification>` 开头；
3. `pid = _claude_pid()` 非 None 且 `meta.is_interactive(pid, sid)`；
4. `source == "prompt"` 时 `owner = _owner()` 非 None（否则 `log_error`）；
5. 进入 `connect_lock`，重读 binding：

| source | binding | auto_pending | 动作 | 返回 |
|---|---|---|---|---|
| prompt | 无 | — | `_create_and_enable(..., auto=True)` | 开启成功 `"created"`，否则 `""` |
| prompt | 有 | 有 | `_enable(sid, b, pid=pid)` | 成功 `"enabled"` |
| prompt | 有 | 无 | 无 | `""` |
| resume | 有 | 不看 | `_enable(sid, b, pid=pid)` | 成功 `"enabled"`；失败时 `log_error` 记下 `_enable` 返回的文本 |
| resume | 无 | — | 无 | `""` |

- [ ] **Step 1: Write the failing tests**

测试前置：`fake` fixture 之外再用 Task 2 的 `claude_sessions`，并 `write_meta(claude_sessions, os.getpid(), SID)`、`config.set_auto_on(True)`。

```python
def test_auto_connect_creates_on_first_prompt(auto):
    assert commands.auto_connect(SID, "prompt", "  修一下登录 bug\n细节…") == "created"
    create = [c for c in auto() if c[:2] == ["im", "+chat-create"]]
    assert len(create) == 1 and arg(create[0], "--name") == "修一下登录 bug"
    b = state.load_binding(SID)
    assert b.created_by_bridge is True and b.synced_name == "rw-1"
    assert state.is_enabled(SID) and not state.has_auto_pending(SID)

def test_auto_connect_second_prompt_does_nothing(auto):
    commands.auto_connect(SID, "prompt", "a")
    state.set_enabled(SID, False)
    assert commands.auto_connect(SID, "prompt", "b") == ""
    assert len([c for c in auto() if c[:2] == ["im", "+chat-create"]]) == 1

def test_auto_connect_retries_enable_without_recreating(auto, monkeypatch):
    real = commands._enable
    monkeypatch.setattr(commands, "_enable", lambda *a, **k: commands._result("boom"))
    assert commands.auto_connect(SID, "prompt", "a") == ""
    assert state.has_auto_pending(SID) and state.load_binding(SID) is not None
    monkeypatch.setattr(commands, "_enable", real)
    assert commands.auto_connect(SID, "prompt", "b") == "enabled"
    assert len([c for c in auto() if c[:2] == ["im", "+chat-create"]]) == 1
    assert state.is_enabled(SID) and not state.has_auto_pending(SID)

def test_auto_connect_retries_after_listener_spawn_failure(auto, monkeypatch):
    """真实 _enable 内拉起监听进程失败：不留 enabled、保留 auto_pending，下一句只重试开启。"""
    def boom(*a, **k):
        raise OSError("fork failed")
    monkeypatch.setattr(listener, "_lock_held_by_other", lambda: False)
    monkeypatch.setattr(listener.subprocess, "Popen", boom)
    assert commands.auto_connect(SID, "prompt", "a") == ""
    assert not state.is_enabled(SID) and state.has_auto_pending(SID)
    monkeypatch.setattr(listener.subprocess, "Popen", lambda *a, **k: None)
    assert commands.auto_connect(SID, "prompt", "b") == "enabled"
    assert len([c for c in auto() if c[:2] == ["im", "+chat-create"]]) == 1
    assert state.is_enabled(SID) and not state.has_auto_pending(SID)

def test_auto_connect_writes_first_mirror_only_on_success(auto, monkeypatch):
    commands.auto_connect(SID, "prompt", "a", at=1000.0)
    assert state.claim_first_mirror(SID, "a", 1000.0) is True
    state.set_enabled(SID, False); state.mark_auto_pending(SID)
    monkeypatch.setattr(commands, "_enable", lambda *a, **k: commands._result("boom"))
    commands.auto_connect(SID, "prompt", "b", at=2000.0)
    assert state.claim_first_mirror(SID, "b", 2000.0) is None  # 失败时已删除

def test_auto_pending_then_off_then_prompt_stays_off(auto, monkeypatch):
    real = commands._enable
    monkeypatch.setattr(commands, "_enable", lambda *a, **k: commands._result("boom"))
    commands.auto_connect(SID, "prompt", "a")
    monkeypatch.setattr(commands, "_enable", real)
    commands.slash(SID, "off")
    assert not state.has_auto_pending(SID)
    assert commands.auto_connect(SID, "prompt", "b") == "" and not state.is_enabled(SID)

def test_manual_lark_after_failed_auto_clears_pending(auto, monkeypatch):
    real = commands._enable
    monkeypatch.setattr(commands, "_enable", lambda *a, **k: commands._result("boom"))
    commands.auto_connect(SID, "prompt", "a")
    monkeypatch.setattr(commands, "_enable", real)
    assert commands.slash(SID, "").startswith("RESULT: 已开启桥接")
    assert not state.has_auto_pending(SID) and state.is_enabled(SID)
    assert len([c for c in auto() if c[:2] == ["im", "+chat-create"]]) == 1

def test_off_then_prompt_does_not_auto_enable(auto):
    commands.auto_connect(SID, "prompt", "a"); commands.slash(SID, "off")
    assert commands.auto_connect(SID, "prompt", "b") == "" and not state.is_enabled(SID)

@pytest.mark.parametrize("prompt", ["/lark", "/lark 群名", "<task-notification>x"])
def test_auto_connect_skips_special_prompts(auto, prompt):
    assert commands.auto_connect(SID, "prompt", prompt) == "" and auto() == []

def test_auto_connect_requires_switch_and_interactive(auto, claude_sessions):
    config.set_auto_on(False)
    assert commands.auto_connect(SID, "prompt", "a") == ""
    config.set_auto_on(True)
    write_meta(claude_sessions, os.getpid(), SID, kind="print")
    assert commands.auto_connect(SID, "prompt", "a") == "" and auto() == []

def test_auto_connect_resume(auto):
    assert commands.auto_connect(SID, "resume") == ""          # 没 binding 不建群
    bind(SID, name="手动群")                                   # 没有 auto_pending
    assert commands.auto_connect(SID, "resume") == "enabled" and state.is_enabled(SID)
    assert [c for c in auto() if c[:2] == ["im", "+chat-create"]] == []

def test_auto_connect_and_manual_do_not_create_two_chats(auto):
    """手动 /lark 持有 connect.lock 时，自动连接等它结束后重读到 binding。"""
    # 线程 A：with state.connect_lock(SID) 里 sleep 0.3 后 save_binding
    # 主线程：auto_connect(SID, "prompt", "a") → 返回 ""，+chat-create 次数为 0

def test_auto_name_fallbacks():
    assert commands.auto_name("\n  \n", "rw-3", "/a/proj") == "rw-3"
    assert commands.auto_name("", "", "/a/proj") == "proj"
    assert commands.auto_name("😀" * 30, "x", "/a") == "😀" * 20

def test_manual_create_records_baseline(auto):
    commands.slash(SID, "我的群")
    b = state.load_binding(SID)
    assert b.created_by_bridge is True and b.synced_name == "rw-1"

def test_bind_existing_not_created_by_bridge(auto):
    commands.slash(SID, "oc_existing")
    assert state.load_binding(SID).created_by_bridge is False
```

`auto` fixture：基于 `fake`，加 `claude_sessions`、`write_meta(..., os.getpid(), SID)`、`config.set_auto_on(True)`，并把 `listener.ensure_running` patch 为返回 `True`（`test_auto_connect_retries_after_listener_spawn_failure` 在用例内恢复真实函数：`monkeypatch.setattr(listener, "ensure_running", real_ensure_running)`，`real_ensure_running` 在 fixture patch 之前保存）。`test_auto_connect_and_manual_do_not_create_two_chats` 按注释里的步骤与断言写全。

`tests/test_listener.py` 追加：`test_ensure_running_reports_spawn_failure`（patch `Popen` 抛 `OSError` → 返回 `False`，errors.log 有 `listener.ensure_running`）与 `test_ensure_running_true_when_running`（`_lock_held_by_other` 为真 → 返回 `True` 且不调用 `Popen`）。

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --with pytest pytest tests/test_commands.py -v`
Expected: FAIL（`AttributeError: auto_connect`）

- [ ] **Step 3: Implement per Interfaces**

`_create_and_enable` / `_bind_existing` 改为接收调用方已算好的 `pid`（`_slash` / `_on` 在拿锁前调用 `_claude_pid()`，为 None 直接返回 `NO_CLAUDE`）。

- [ ] **Step 4: Run to verify they pass**

Run: `uv run --with pytest pytest tests -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add lark_bridge/commands.py lark_bridge/listener.py tests/test_commands.py tests/test_listener.py
git commit -m "feat: auto_connect for first prompt and resume, serialized with manual /lark"
```

---

### Task 5: hook 接线：首句自动建群、resume 接回、条件清理

**Files:**
- Modify: `lark_bridge/hooks.py`
- Modify: `lark_bridge/settings.py`
- Test: `tests/test_hooks_auto.py`（新建）、`tests/test_hooks_stop.py`、`tests/test_settings.py`

**Interfaces:**
- Consumes: `commands.auto_connect(sid, source, prompt, at) -> str`（Task 4）；`state.claim_first_mirror`（Task 3）；`state.disable_if_foreign`（Task 3）。
- Produces:
  - `hooks._wall()`：返回 `time.time()`，模块级函数，测试 patch 它控制输入事件时间。
  - `hooks._auto_wait(sid, data, start, now, sleep) -> str`：`event == "wait"` 且未开启时调用。入口处 `at = _wall()`（早于建群），`r = commands.auto_connect(sid, "prompt", prompt, at=at)`；`r` 非空时：`set_counter(sid, "busy", 1)`、`set_counter(sid, "keepalive_pending", 0)`、`state.claim_first_mirror(sid, prompt, at) is True` 时才 `feishu.send(chat_id, TERMINAL_PREFIX + prompt)`；然后 `state.is_enabled(sid)` 为真时 `_become_waiter(...)`。
  - `_prompt`（已开启时的同步 hook）：入口处 `at = _wall()`；镜像前调用 `state.claim_first_mirror(sid, prompt, at)`，返回 `False`（`wait` 一侧已认领并发送）时跳过 `_spawn_mirror`；返回 `True` 或 `None` 照常镜像。`prompt` 与 `wait` 是 settings 里两个独立条目，Claude Code 不保证二者先后：`prompt` 先跑时它看不到开启、不镜像，由 `_auto_wait` 认领发送；`prompt` 在开启之后才跑时，两方按 Task 3 的协议认领，先到的一方镜像，后到的一方读到 `first_mirror.claimed` 而跳过。
  - `hooks._session_resume(sid, data, start, now, sleep) -> str`：`data["source"] == "resume"` 时 `commands.auto_connect(sid, "resume")`；开启后 `_become_waiter(...)`；其他 source 立刻返回 `""`。
  - `run()` 分流：`event == "wait"` 且未开启 → `_auto_wait`；`event == "session-resume"` → `_session_resume`（不受 is_enabled 门控）。两者都先 `_install_sigterm()`。
  - `_session_start`：source 在 `_START_DISCONNECT` 时，`pid = procs.find_claude_ancestor()`；pid 非 None 用 `state.disable_if_foreign(sid, pid, procs.start_time(pid))`，返回 True 才写原有日志；pid 为 None 退回 `state.set_enabled(sid, False)`。
  - `settings._ENTRIES` 在 `session-start` 条目之后加 `("SessionStart", None, "session-resume", 86400, _ASYNC)`。
- `hooks` 引入 `commands` 用函数内 import 或模块顶 import 均可，但不得造成循环（`commands` 不 import `hooks`）。

- [ ] **Step 1: Write the failing tests**

`tests/test_hooks_auto.py`（复用 `test_hooks_stop` 的 `fake`、`Clock`、`prompt_input`、`sent_texts`，以及 Task 2/4 的 meta 写法；`commands.procs.find_claude_ancestor` 和 `hooks` 用到的 `procs.find_claude_ancestor` 都 patch 为 `os.getpid`；`listener.ensure_running` patch 为返回 `True`（下面注明恢复真实函数的用例除外）；`hooks._spawn_mirror` patch 为记录调用（`mirrors` 列表），首句镜像次数 = `sent_texts` 里 `"💻 终端：<prompt>"` 的条数 + `mirrors` 里等于 `<prompt>` 的条数；`CLAUDE_LARK_IDLE_SECONDS` 设为 `"1"`，等待进程在虚拟时钟上 1 秒后到期；因为 `busy == 1`，到期返回 `""`）。辅助函数 `saved_binding()` 写 `state.Binding(SID, "oc_test_chat", "测试群", "/work", None, 1.0)`，不开启：

```python
def test_wait_hook_auto_creates_and_mirrors_first_prompt(auto):
    clock = Clock()
    out = hooks.run("wait", prompt_input("修 bug"), clock.now, clock.sleep)
    assert state.is_enabled(SID)
    assert "💻 终端：修 bug" in sent_texts(auto())
    assert state.get_counter(SID, "busy") == 1
    assert out == "" and clock.sleeps > 0          # 成为了等待进程，等到 deadline

def test_first_prompt_mirrored_once_prompt_hook_first(auto, mirrors):
    """常见顺序：同步 prompt hook 先跑（未开启，不镜像），wait 再自动开启并发送。"""
    clock = Clock()
    hooks.run("prompt", prompt_input("修 bug"), clock.now, clock.sleep)
    hooks.run("wait", prompt_input("修 bug"), clock.now, clock.sleep)
    assert first_mirror_count(auto, mirrors, "修 bug") == 1

def test_first_prompt_mirrored_once_prompt_hook_after_enable(auto, mirrors):
    """prompt hook 在 wait 完成开启之后才跑：wait 已认领，prompt hook 不再镜像。"""
    clock = Clock()
    hooks.run("wait", prompt_input("修 bug"), clock.now, clock.sleep)
    hooks.run("prompt", prompt_input("修 bug"), clock.now, clock.sleep)
    assert first_mirror_count(auto, mirrors, "修 bug") == 1

def test_first_prompt_mirrored_once_interleaved(auto, mirrors, monkeypatch):
    """开启之后、wait 认领之前插入 prompt hook：prompt hook 认领，wait 不再发送。"""
    clock = Clock()
    real = commands.auto_connect
    def connect_then_prompt(*a, **k):
        r = real(*a, **k)
        hooks.run("prompt", prompt_input("修 bug"), clock.now, clock.sleep)
        return r
    monkeypatch.setattr(commands, "auto_connect", connect_then_prompt)
    hooks.run("wait", prompt_input("修 bug"), clock.now, clock.sleep)
    assert first_mirror_count(auto, mirrors, "修 bug") == 1
    assert mirrors == ["修 bug"]

def test_later_identical_prompt_still_mirrored(auto, mirrors, monkeypatch):
    """首句由 wait 认领（prompt hook 开启前已跑完）；之后再输入相同文本，照常镜像。"""
    t = [1000.0]
    monkeypatch.setattr(hooks, "_wall", lambda: t[0])
    clock = Clock()
    hooks.run("prompt", prompt_input("修 bug"), clock.now, clock.sleep)
    hooks.run("wait", prompt_input("修 bug"), clock.now, clock.sleep)
    t[0] += 60
    hooks.run("prompt", prompt_input("修 bug"), clock.now, clock.sleep)
    assert first_mirror_count(auto, mirrors, "修 bug") == 2

def test_wait_hook_recovers_after_listener_spawn_failure(auto, monkeypatch):
    """真实 ensure_running 拉起失败 → 不开启；下一句的 wait hook 走 _auto_wait 重试，不重复建群。"""
    monkeypatch.setattr(listener, "ensure_running", real_ensure_running)
    monkeypatch.setattr(listener, "_lock_held_by_other", lambda: False)
    def boom(*a, **k):
        raise OSError("fork failed")
    monkeypatch.setattr(listener.subprocess, "Popen", boom)
    clock = Clock()
    hooks.run("wait", prompt_input("a"), clock.now, clock.sleep)
    assert not state.is_enabled(SID) and state.has_auto_pending(SID)
    monkeypatch.setattr(listener.subprocess, "Popen", lambda *a, **k: None)
    hooks.run("wait", prompt_input("b"), clock.now, clock.sleep)
    assert state.is_enabled(SID) and not state.has_auto_pending(SID)
    assert len([c for c in auto() if c[:2] == ["im", "+chat-create"]]) == 1

def test_wait_hook_noop_when_auto_off(auto):
    config.set_auto_on(False)
    clock = Clock()
    assert hooks.run("wait", prompt_input("x"), clock.now, clock.sleep) == ""
    assert auto() == [] and clock.sleeps == 0

def test_session_resume_reattaches_bound_chat(auto):
    saved_binding()
    hooks.run("session-resume", session_input("SessionStart", "source", "resume"), ...)
    assert state.is_enabled(SID)
    assert [c for c in auto() if c[:2] == ["im", "+chat-create"]] == []

@pytest.mark.parametrize("source", ["startup", "clear", "compact"])
def test_session_resume_ignores_other_sources(auto, source):
    saved_binding()
    hooks.run("session-resume", session_input("SessionStart", "source", source), ...)
    assert not state.is_enabled(SID) and auto() == []

def test_session_resume_without_binding_does_not_create(auto):
    hooks.run("session-resume", session_input("SessionStart", "source", "resume"), ...)
    assert state.load_binding(SID) is None and auto() == []

def test_session_start_keeps_enabled_of_current_process(auto):
    state.save_binding(...); state.set_enabled(SID, True, os.getpid())
    hooks.run("session-start", session_input("SessionStart", "source", "resume"), ...)
    assert state.is_enabled(SID)

def test_session_start_removes_enabled_of_other_process(auto):
    state.save_binding(...); state.set_enabled(SID, True, os.getppid())
    hooks.run("session-start", session_input("SessionStart", "source", "resume"), ...)
    assert not state.is_enabled(SID)
```

`...` 处用 `Clock()` 的 `now`、`sleep`，与 `test_hooks_stop` 的调用方式相同。`mirrors` fixture 返回记录 `_spawn_mirror` 文本的列表；`first_mirror_count(auto, mirrors, p)` 按上面的定义计数；`real_ensure_running` 是模块加载时保存的 `listener.ensure_running` 原函数。现有 `test_session_start_disables_silently` 用的 `bridged` fixture 写的是无 pid 的 enabled（空文件），它与当前进程不符，仍应被删除——保持该测试不改并确认通过。

`tests/test_settings.py`：`test_merge_into_empty` 的 `len(_ours(cfg)) == 6` 改为 `7`，并加：

```python
    resume = hooks["SessionStart"][1]["hooks"][0]
    assert resume["command"].endswith("hook session-resume")
    assert resume["asyncRewake"] is True and resume["timeout"] == 86400
    assert hooks["SessionStart"][0]["hooks"][0]["timeout"] == 10
    assert "asyncRewake" not in hooks["SessionStart"][0]["hooks"][0]
```

另加 `test_has_hooks_false_without_session_resume`：从 `merge({})` 结果里删掉 `SessionStart[1]`，`has_hooks` 返回 False。

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --with pytest pytest tests/test_hooks_auto.py tests/test_settings.py -v`
Expected: FAIL

- [ ] **Step 3: Implement per Interfaces**

- [ ] **Step 4: Run to verify they pass**

Run: `uv run --with pytest pytest tests -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add lark_bridge/hooks.py lark_bridge/settings.py tests/test_hooks_auto.py tests/test_hooks_stop.py tests/test_settings.py
git commit -m "feat: auto-connect from wait hook, resume reattach hook, foreign-only cleanup"
```

---

### Task 6: 监听进程同步群名

**Files:**
- Modify: `lark_bridge/feishu.py`
- Modify: `lark_bridge/listener.py`
- Modify: `tests/fake_lark_cli.py`
- Test: `tests/test_listener.py`

**Interfaces:**
- Consumes: `meta.read()`（Task 2）；`state.update_binding`、`Binding.created_by_bridge / synced_name`（Task 3）。
- Produces:
  - `feishu.update_chat_name(chat_id: str, name: str, timeout: float) -> None`：`_run(["im", "+chat-update", "--as", "bot", "--chat-id", chat_id, "--name", name[:60]], timeout=timeout)`；失败抛 `FeishuError`。
  - `listener.RENAME_RETRY = 60`、`listener.RENAME_FAILED = "改群名失败，可能缺少 im:chat:update 权限，详见 errors.log"`。
  - `_Poller.sync_names() -> None`：在 `poll_once` 的消息轮询循环**之后**调用（`stop` 已置位就跳过）。重试计时与"已提示"存在内存：`self.rename_retry_at: dict[str, float]`（key 为 session_id，值为 monotonic）、`self.rename_warned: set[str]`。
  - fake：`im +chat-update` 打印 `{"ok": true}`；`FAKE_LARK_UPDATE_FAIL=1` 时只让 `+chat-update` 报错退出 1。

`sync_names` 对每个 `is_enabled` 的 binding：

1. `created_by_bridge` 为假 → 跳过。
2. `m = meta.read(b.claude_pid, b.session_id)`，`None` → 跳过；`name = m["name"].strip()`（非字符串当空），空 → 跳过。
3. `b.synced_name is None` → `update_binding` 只写 `synced_name = name`，不改名。
4. `name == b.synced_name` → 跳过；冷却未到 → 跳过。
5. 调 `feishu.update_chat_name(b.chat_id, name, SEND_TIMEOUT)`。成功：`update_binding` 锁内若 `chat_id` 仍相同，写 `chat_name = name[:60]`、`synced_name = name`，清掉该 session 的重试计时；失败：`feishu.log_error(...)`，`rename_retry_at[sid] = _now() + RENAME_RETRY`，若 sid 不在 `rename_warned` 则 `_reply(chat_id, RENAME_FAILED)` 并加入集合。

- [ ] **Step 1: Write the failing tests**

测试辅助：`bind_auto(synced="rw-1", created=True)` 写 `state.Binding(SID, "oc_test_chat", "rw-1", "/work", os.getpid(), 1.0, created_by_bridge=created, synced_name=synced)` 并 `set_enabled(SID, True, os.getpid())`；`poller = listener._Poller(60)`；`arg(c, flag)` 同 `test_commands`；用 Task 2 的 `claude_sessions` 写元数据；`listener._now` 用可控时钟替换（`monkeypatch.setattr(listener, "_now", lambda: t[0])`）。

```python
def renames(calls):
    return [arg(c, "--name") for c in calls() if c[:2] == ["im", "+chat-update"]]

def test_rename_syncs_user_name(fake, claude_sessions):
    bind_auto(synced="rw-1"); write_meta(claude_sessions, os.getpid(), SID, name="新名字", source="user")
    _poll(poller)
    assert renames(fake) == ["新名字"]
    b = state.load_binding(SID); assert (b.chat_name, b.synced_name) == ("新名字", "新名字")
    _poll(poller); assert renames(fake) == ["新名字"]          # 不重复改

def test_rename_syncs_derived_name(...)                       # source="derived" 同样改名

def test_rename_baseline_equal_does_nothing(...)              # name == synced_name → 无 +chat-update

def test_rename_skips_bound_existing_chat(...)                # created_by_bridge=False → 无 +chat-update

def test_rename_skips_foreign_meta(...)                       # meta sessionId 不同 / 文件损坏 → 无调用

def test_rename_legacy_binding_records_baseline_only(...):
    # synced_name=None → 第一轮无 +chat-update，synced_name 变为当前名；
    # 再改元数据名 → 第二轮改名

def test_rename_truncates_to_60(...):
    # name = "名" * 70 → --name 为 "名" * 60

def test_rename_failure_cooldown_and_single_notice(fake, claude_sessions, monkeypatch):
    monkeypatch.setenv("FAKE_LARK_UPDATE_FAIL", "1")
    # t=0 轮询 → 1 次 +chat-update，群里 1 条 RENAME_FAILED
    # t=30 轮询 → 仍 1 次
    # t=61 轮询 → 2 次 +chat-update，RENAME_FAILED 仍只有 1 条
    # 期间把元数据名改成 "名2"，t=122 → 最后一次调用的 --name 是 "名2"
    # errors.log 里有 "+chat-update"

def test_rename_writeback_keeps_new_claude_pid(fake, claude_sessions, monkeypatch):
    real = feishu.update_chat_name
    def racing(chat_id, name, timeout):
        state.update_binding(SID, lambda b: setattr(b, "claude_pid", 99999))
        real(chat_id, name, timeout)
    monkeypatch.setattr(feishu, "update_chat_name", racing)
    # 轮询后：claude_pid == 99999 且 synced_name == 新名字

def test_rename_writeback_dropped_when_chat_changed(...):
    # racing 里把 chat_id 改成 "oc_other" → 写回放弃，synced_name 仍是旧值

def test_rename_runs_after_message_poll(fake, claude_sessions, tmp_path, monkeypatch):
    # FAKE_LARK_LOG 中本轮的 "api GET /open-apis/im/v1/messages" 出现在 "+chat-update" 之前
```

上面写 `(...)` 的测试按注释里的条件与断言写全，fixture 与第一个测试相同。

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --with pytest pytest tests/test_listener.py -v -k rename`
Expected: FAIL

- [ ] **Step 3: Implement fake `+chat-update`, `feishu.update_chat_name`, `_Poller.sync_names` per Interfaces**

- [ ] **Step 4: Run to verify they pass**

Run: `uv run --with pytest pytest tests -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add lark_bridge/feishu.py lark_bridge/listener.py tests/fake_lark_cli.py tests/test_listener.py
git commit -m "feat: listener renames bridge-created chats to follow session name"
```

---

### Task 7: doctor 检查 `im:chat:update`、README 与命令说明

**Files:**
- Modify: `lark_bridge/doctor.py`（`_check_auth`）
- Modify: `tests/fake_lark_cli.py`（`auth status` 的 bot 身份支持 `$FAKE_LARK_BOT_SCOPE`）
- Modify: `README.md`、`commands/lark.md`
- Test: `tests/test_settings.py`

**Interfaces:**
- Produces: `doctor.RENAME_SCOPE = "im:chat:update"`。bot 身份 ready 且 `identities.bot` 里有 `scope` 字段时：含 `RENAME_SCOPE` → `report(True, "bot 身份有改群名权限（im:chat:update）")`；不含 → `report(False, "bot 身份缺少改群名权限 im:chat:update", "在飞书开放平台给应用开通 im:chat:update 并发布版本")`。没有 `scope` 字段时不报告。
- fake：设置了 `FAKE_LARK_BOT_SCOPE` 时 bot 身份带 `"scope": <值>`，未设置时不带（保持现有输出）。

- [ ] **Step 1: Write the failing tests**

```python
def test_doctor_rename_scope_missing(monkeypatch, tmp_path, lark_home, capsys):
    rc = _doctor(monkeypatch, tmp_path, lark_home, FAKE_LARK_BOT_SCOPE="im:message")
    out = capsys.readouterr().out
    assert "缺少改群名权限 im:chat:update" in out and rc != 0

def test_doctor_rename_scope_present(monkeypatch, tmp_path, lark_home, capsys):
    _doctor(monkeypatch, tmp_path, lark_home, FAKE_LARK_BOT_SCOPE="im:message im:chat:update")
    assert "有改群名权限" in capsys.readouterr().out

def test_doctor_rename_scope_unknown_not_reported(monkeypatch, tmp_path, lark_home, capsys):
    _doctor(monkeypatch, tmp_path, lark_home)
    assert "im:chat:update" not in capsys.readouterr().out
```

（`rc` 的期望按现有 `_doctor` 辅助函数与 `test_doctor_reports_missing_read_scope` 的写法对齐。）

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --with pytest pytest tests/test_settings.py -v -k doctor`
Expected: FAIL

- [ ] **Step 3: Implement doctor check and fake field**

- [ ] **Step 4: Update docs**

`README.md`：
- 权限表（"The Feishu/Lark app" 一节）加一行 `im:chat:update`，身份 bot，用途"改群名，群名跟随会话名"。
- Usage 加 `/lark auto on` / `/lark auto off`：开启后新会话和 `/clear` 后的会话在第一句之后自动建群；`/resume` 一个绑过群的会话时启动即接回；只改 claude-lark 自己建的群，`/lark oc_xxx` 绑定的群不改名；`/rename` 和自动起的会话名都会同步。
- Limitations 加：第一轮在群建好之前就结束时，这一轮的最终回复不会发到群里，后续回合正常。
- Upgrade 加：升级后重跑 `install.sh`，写入新的 `session-resume` hook。
- 中文摘要同步一句话说明 `auto on|off` 和群名跟随。

`commands/lark.md`：description 改为 `把当前会话桥接到飞书群（/lark [群名|chat_id|off|status|auto on|auto off]）`。

- [ ] **Step 5: Run full suite and format**

Run: `black lark_bridge tests && uv run --with pytest pytest tests -v`
Expected: black 无报错，全部 PASS

- [ ] **Step 6: Commit**

```bash
git add lark_bridge/doctor.py tests/fake_lark_cli.py tests/test_settings.py README.md commands/lark.md
git commit -m "docs: auto on/off and chat rename; doctor checks im:chat:update"
```
