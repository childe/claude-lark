# install.sh 升级后重启监听进程 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `install.sh` 结束前停掉正在运行的旧监听进程并（有开启中的会话时）拉起当前代码的监听进程；doctor 能发现监听进程运行的不是当前代码。

**Architecture:** 监听进程在写 pid 文件的同一次 `_pid_lock` 内写实例信息文件 `listener.version`（`{"pid", "code_version"}`），放锁时一并删除。`listener.instance()` 在 `_pid_lock` 内返回 `(held, pid, code_version)`，实例身份以 pid 文件为准、实例信息只提供版本。新模块 `lark_bridge/restart.py` 实现 `claude-lark restart-listener`（SIGTERM → 等交接 → 必要时 SIGKILL → `ensure_running()` 最多两次并核对版本）；doctor 的 `_check_listener` 改用 `instance()` 与新的只读"有无开启会话"判断。

**Tech Stack:** Python ≥ 3.9 标准库（fcntl、hashlib、signal、subprocess），pytest，`tests/fake_lark_cli.py`。

**Spec:** `docs/superpowers/specs/2026-11-23-restart-listener-on-upgrade-design.md`

## Global Constraints

- 代码版本：对 `lark_bridge/*.py` 与 `bin/claude-lark` 的内容按文件名排序后算 sha256，取前 **12** 位十六进制。
- 实例信息文件：`~/.claude/lark/listener.version`（即 `paths.home() / "listener.version"`），内容 JSON `{"pid": N, "code_version": "V"}`；与 pid 文件在**同一次 `_pid_lock` 内**写入/删除；只删 `pid` 等于自己的那份。
- `code_version` 仅在实例信息文件存在、可解析、且其 `pid` 等于 pid 文件内容时有值，否则为 `None`；`None` 按"不是当前版本"处理，但**不阻止**停止。
- 等待时长：SIGTERM 后最多 **20** 秒；SIGKILL 后最多 **5** 秒；每次 `ensure_running()` 后最多 **5** 秒，最多调用 **2** 次。
- 等待原实例退出时不持有 `_pid_lock`；发 SIGTERM / SIGKILL 前都在 `_pid_lock` 内核实"存活锁被持有且 pid 文件仍为 P"。
- 存活锁被持有但 pid 文件缺失/读不出：不发任何信号，`❌` + 修复提示，退出码非 0。
- 成功文案固定：`✅ 监听进程已重启（pid N，版本 V）`；没有开启会话：`没有开启中的会话，不需要监听进程`，退出码 0。
- 监听进程日志：`main()` 拿锁之前写 `listener starting (pid N, version V)`；拿锁失败写 `listener lock held by another process, exiting`。
- doctor 只读：不得调用 `listener._any_enabled()` 或 `state.is_enabled()`（会删失效会话的 enabled 文件）。
- `install.sh` 在 doctor 之前调用 `restart-listener`，失败不中止安装（`|| true`）。
- 输出风格沿用 `✅` / `❌` / `   修复：`。
- 每次改代码后用 black 格式化；测试命令 `uv run --with pytest pytest tests -v`。

## Review Focus

1. 旧监听进程正卡在一次最长 15 秒的 lark-cli 拉取里：SIGTERM 等待上限必须是 20 秒，不能因测试方便改小默认值（Task 3 `test_restart_default_timeouts`）。
2. `listener.version` 损坏（非 JSON、不是对象、缺字段、`pid` 是字符串）：`instance()` 返回 `code_version=None`，不抛异常（Task 1 `test_instance_corrupt_version_file`）。
3. 进程对 SIGKILL 也不退出（信号被吞，例如 D 状态）：`restart-listener` 5 秒后报 `❌` 并返回非 0，不无限等待（Task 3 `test_restart_kill_ineffective_reports_failure`）。
4. 全新机器上第一次安装（`~/.claude/lark` 下除 config 外什么都没有）：`restart-listener` 返回 0，不报错（Task 3 `test_restart_no_listener_no_session`，在空 `lark_home` 里跑）。
5. doctor 遇到失效会话（claude pid 已死）时不能顺手删 enabled 文件，否则 doctor 改变了它要报告的状态（Task 4 `test_doctor_does_not_delete_stale_enabled`）。

---

## File Structure

- Modify `lark_bridge/paths.py`：新增 `listener_version_file()`。
- Modify `lark_bridge/listener.py`：`code_version()`、实例信息读写、`instance()` / `_instance_locked()`、启动日志、`any_enabled_readonly()`。
- Modify `lark_bridge/state.py`：`is_enabled_readonly()`，与 `is_enabled()` 共用解析。
- Create `lark_bridge/restart.py`：`restart-listener` 的全部逻辑。
- Modify `lark_bridge/cli.py`：分发 `restart-listener`，更新用法串。
- Modify `lark_bridge/doctor.py`：`_check_listener` 四个分支。
- Modify `install.sh`、`README.md`。
- Create `tests/fake_listener.py`：可配置的"旧监听进程"替身。
- Modify `tests/conftest.py`：`start_fake_listener()` 辅助函数。
- Create `tests/test_restart.py`、`tests/test_doctor_listener.py`；Modify `tests/test_listener.py`、`tests/test_state.py`。

---

### Task 1: 代码版本、实例信息文件与 `instance()`

**Files:**
- Modify: `lark_bridge/paths.py`
- Modify: `lark_bridge/listener.py`（`_acquire_pid_file`、`_drop_life_lock_locked`、`main` 开头、新增函数）
- Test: `tests/test_listener.py`

**Interfaces:**
- Produces:
  - `paths.listener_version_file() -> Path`（`home() / "listener.version"`）
  - `listener.code_version(root: Optional[Path] = None) -> str`：`root` 缺省为 `Path(__file__).resolve().parent.parent`；读 `root/lark_bridge/*.py` 与 `root/bin/claude-lark`。
  - `listener._instance_locked() -> Tuple[bool, Optional[int], Optional[str]]`：调用方已持有 `_pid_lock`。
  - `listener.instance() -> Tuple[bool, Optional[int], Optional[str]]`：自己拿 `_pid_lock` 后调 `_instance_locked()`。
  - `listener._acquire_pid_file(version: str) -> bool`（新增参数）。

- [ ] **Step 1: Write the failing tests** (in `tests/test_listener.py`)

```python
def _mk_tree(root, files):
    (root / "lark_bridge").mkdir(parents=True)
    (root / "bin").mkdir()
    for rel, text in files.items():
        (root / rel).write_text(text)


def test_code_version_changes_with_content(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    files = {"lark_bridge/x.py": "1", "lark_bridge/y.py": "2", "bin/claude-lark": "3"}
    _mk_tree(a, files)
    _mk_tree(b, files)
    va = listener.code_version(a)
    assert len(va) == 12 and all(c in "0123456789abcdef" for c in va)
    assert listener.code_version(b) == va
    (b / "lark_bridge/y.py").write_text("2 changed")
    assert listener.code_version(b) != va
    (a / "lark_bridge/notes.txt").write_text("ignored")  # 非 .py 不计入
    assert listener.code_version(a) == va
    (a / "bin/claude-lark").write_text("3 changed")
    assert listener.code_version(a) != va


def test_code_version_default_root_is_repo():
    assert listener.code_version() == listener.code_version(ROOT)


def test_main_writes_and_removes_version_file(fake):
    claude = subprocess.Popen(["sleep", "60"])
    bind(claude_pid=claude.pid)
    seen = []

    def watcher():
        _wait(lambda: _polls(fake))
        seen.append(json.loads(paths.listener_version_file().read_text()))
        seen.append(listener.instance())
        claude.kill()
        claude.wait()

    th = threading.Thread(target=watcher)
    th.start()
    listener.main(check_interval=0.1, poll_interval=0.1)
    th.join()
    v = listener.code_version()
    assert seen[0] == {"pid": os.getpid(), "code_version": v}
    assert seen[1] == (True, os.getpid(), v)
    assert not paths.listener_version_file().exists()
    assert listener.instance() == (False, None, None)


def test_drop_keeps_foreign_version_file(fake):
    bind()
    assert listener._acquire_pid_file("abc")
    paths.listener_version_file().write_text(json.dumps({"pid": 1, "code_version": "x"}))
    listener._release_pid_file()
    assert json.loads(paths.listener_version_file().read_text())["pid"] == 1


def test_instance_old_protocol_and_stale_version(fake):
    other = _hold_listener_lock()
    try:
        assert listener.instance() == (True, None, None)  # 锁被持有但没有 pid 文件
        paths.listener_pid_file().write_text(str(os.getppid()))
        assert listener.instance() == (True, os.getppid(), None)  # 旧协议：无实例信息
        paths.listener_version_file().write_text(
            json.dumps({"pid": 999999, "code_version": "v"})
        )
        assert listener.instance() == (True, os.getppid(), None)  # 残留
    finally:
        other.close()


@pytest.mark.parametrize(
    "content",
    ["not json", "[1]", '{"pid": 5}', '{"code_version": "v"}', '{"pid": "5", "code_version": "v"}'],
)
def test_instance_corrupt_version_file(fake, content):
    other = _hold_listener_lock()
    try:
        paths.listener_pid_file().write_text("5")
        paths.listener_version_file().write_text(content)
        assert listener.instance() == (True, 5, None)
    finally:
        other.close()


def test_main_logs_starting_and_lock_held(fake):
    bind()
    other = _hold_listener_lock()
    try:
        assert listener.main(check_interval=0.1, poll_interval=0.1) == 0
    finally:
        other.close()
    log = paths.listener_log().read_text()
    v = listener.code_version()
    assert f"listener starting (pid {os.getpid()}, version {v})" in log
    assert "listener lock held by another process, exiting" in log
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest pytest tests/test_listener.py -k "code_version or version_file or instance or starting" -v`
Expected: FAIL（`AttributeError: ... has no attribute 'code_version'` / `listener_version_file`）

- [ ] **Step 3: Implement**

- `paths.listener_version_file()`。
- `code_version(root=None)`：文件列表 = `sorted((root/"lark_bridge").glob("*.py"))` + `[root/"bin"/"claude-lark"]`，再整体按相对路径字符串排序；对每个文件依次 `update(relpath.encode() + b"\0" + content + b"\0")`；返回 `hexdigest()[:12]`。
- `_acquire_pid_file(version)`：在写 pid 文件之后、同一 `with _pid_lock()` 内 `state._atomic_write(paths.listener_version_file(), json.dumps({"pid": os.getpid(), "code_version": version}))`。
- `_drop_life_lock_locked()`：删 pid 文件的同时，若实例信息文件可解析且 `pid == os.getpid()` 则删除它（`OSError`/`ValueError` 忽略）。
- `_instance_locked()`：`held = _lock_held_by_other()`；未持有返回 `(False, None, None)`；`pid = _read_pid()`；读实例信息，仅当为 dict、`pid` 是 `int` 且等于 `pid`、`code_version` 是非空 `str` 时取版本，否则 `None`。
- `main()`：第一行 `version = code_version()`，`_log(f"listener starting (pid {os.getpid()}, version {version})")`；`_acquire_pid_file(version)` 失败时 `_log("listener lock held by another process, exiting")` 再 `return 0`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --with pytest pytest tests/test_listener.py -v`
Expected: 全部 PASS（含原有测试）

- [ ] **Step 5: Commit**

```bash
git add lark_bridge/paths.py lark_bridge/listener.py tests/test_listener.py
git commit -m "feat: listener records its code version alongside the pid file"
```

---

### Task 2: 只读的"有无开启中的会话"判断

**Files:**
- Modify: `lark_bridge/state.py`（`is_enabled` 拆出共用解析）
- Modify: `lark_bridge/listener.py`（`_any_enabled` 复用判断）
- Test: `tests/test_state.py`、`tests/test_listener.py`

**Interfaces:**
- Produces:
  - `state.is_enabled_readonly(sid) -> bool`：与 `is_enabled()` 判定结果相同，但从不删除 enabled 文件、不写 errors.log。
  - `listener.any_enabled_readonly() -> bool`：存在某个 binding，`state.is_enabled_readonly(sid)` 为真且其 `claude_pid` 为空或存活。
  - `listener._claude_gone(b) -> bool`：`b.claude_pid and not procs.pid_alive(b.claude_pid)`；`_any_enabled()` 与 `any_enabled_readonly()` 共用。

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_state.py
def test_is_enabled_readonly_keeps_stale_file(lark_home):
    state.save_binding(mk())
    f = paths.session_dir(SID) / "enabled"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("999999\nticks:1")  # 已死的 pid
    assert state.is_enabled_readonly(SID) is False
    assert f.exists()
    f.write_text("")
    assert state.is_enabled_readonly(SID) is True
    state.set_enabled(SID, True, claude_pid=os.getpid())
    assert state.is_enabled_readonly(SID) is True
    f.unlink()
    assert state.is_enabled_readonly(SID) is False


# tests/test_listener.py
def test_any_enabled_readonly_has_no_side_effects(fake):
    dead = _dead_pid()
    bind(claude_pid=dead)  # enabled 文件为空，但 claude pid 已死
    f = paths.session_dir(SID) / "enabled"
    assert listener.any_enabled_readonly() is False
    assert f.exists()
    bind(sid=SID2, chat="oc_2")
    assert listener.any_enabled_readonly() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest pytest tests/test_state.py tests/test_listener.py -k "readonly" -v`
Expected: FAIL（`AttributeError: ... is_enabled_readonly`）

- [ ] **Step 3: Implement**

把 `is_enabled()` 里从读文件到算出 `alive` / `reason` 的部分抽成 `_enabled_check(sid) -> Optional[Tuple[bool, str, str]]`（文件不存在返回 `None`，否则 `(alive, raw, reason)`）；`is_enabled()` 在其上保留"失效时在 `_enabled_lock` 内比对 raw 后删除并记日志"；`is_enabled_readonly()` 只返回 `alive`。listener 新增 `_claude_gone(b)` 与 `any_enabled_readonly()`，`_any_enabled()` 的 claude pid 判断改用 `_claude_gone(b)`，副作用不变。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --with pytest pytest tests -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add lark_bridge/state.py lark_bridge/listener.py tests/test_state.py tests/test_listener.py
git commit -m "feat: read-only check for enabled sessions"
```

---

### Task 3: `claude-lark restart-listener`

**Files:**
- Create: `lark_bridge/restart.py`
- Modify: `lark_bridge/cli.py`
- Create: `tests/fake_listener.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_restart.py`

**Interfaces:**
- Consumes: `listener.instance()`, `listener._instance_locked()`, `listener._pid_lock()`, `listener.code_version()`, `listener._any_enabled()`, `listener.ensure_running()`（Task 1）。
- Produces:
  - 模块常量 `TERM_WAIT = 20`、`KILL_WAIT = 5`、`UP_WAIT = 5`、`INTERVAL = 0.2`。
  - `restart.run(term_wait=TERM_WAIT, kill_wait=KILL_WAIT, up_wait=UP_WAIT, interval=INTERVAL) -> int`：打印结果行，成功/无需监听返回 0，失败返回 1。
  - `restart._signal(pid: int, sig: int) -> None`：包装 `os.kill`，吞掉 `ProcessLookupError`。测试通过替换它来记录和拦截信号。
  - `restart._wait_handover(p: int, timeout: float, interval: float) -> bool`：每轮调用 `listener.instance()`，`not held or pid != p` 即返回 True；超时返回 False。
  - `restart._kill_if_still(p: int) -> bool`：在 `_pid_lock` 内 `_instance_locked()`，仅当 held 且 pid == p 时 `_signal(p, SIGKILL)` 并返回 True。
  - CLI：`claude-lark restart-listener` → `restart.run()` 的返回值。
  - 测试辅助 `conftest.start_fake_listener(*args) -> subprocess.Popen`：启动 `tests/fake_listener.py`，等到它写好 pid 文件再返回。

- [ ] **Step 1: Write the test helper `tests/fake_listener.py`**

独立脚本（不 import `lark_bridge.listener`，模拟旧版本代码）：读 `CLAUDE_LARK_HOME`；在 `listener.pid.lock` 的 flock 内对 `listener.lock` 拿 `LOCK_EX|LOCK_NB`，写 `listener.pid` 为自己的 pid；若给了 `--version V` 再写 `listener.version` 为 `{"pid": 自己, "code_version": V}`。`--ignore-term` 时 `signal(SIGTERM, SIG_IGN)`，否则 SIGTERM 处理为：在 `listener.pid.lock` 内删自己的 pid 文件（和自己的实例信息文件）、关闭存活锁 fd、`os._exit(0)`。主循环 `time.sleep(0.1)`，最多存活 120 秒。

`conftest.start_fake_listener(*args)`：`Popen([sys.executable, <tests/fake_listener.py>, *args], env=dict(os.environ), stdin=DEVNULL)`，轮询至 `listener.pid` 内容等于子进程 pid（最长 10 秒）后返回。

- [ ] **Step 2: Write the failing tests** (`tests/test_restart.py`)

公共部分：fixture `env(lark_home, tmp_path, monkeypatch)` 设置 `CLAUDE_LARK_CLI=tests/fake_lark_cli.py`、`FAKE_LARK_LOG`；fixture `sigs(monkeypatch)` 把 `restart._signal` 换成"记录 `(pid, sig)` 后调用原函数"，返回记录列表；teardown 统一对 pid 文件中的 pid 和所有 fake 进程发 SIGKILL 并 `wait()`。`bind()` 同 `tests/test_listener.py`（空 enabled 文件、`claude_pid=None`，即开启中）。下文 `V = listener.code_version()`。

```python
def test_restart_default_timeouts():
    assert (restart.TERM_WAIT, restart.KILL_WAIT, restart.UP_WAIT) == (20, 5, 5)


def test_restart_no_listener_no_session(env, sigs, capsys):
    assert restart.run() == 0
    assert "没有开启中的会话，不需要监听进程" in capsys.readouterr().out
    assert sigs == [] and not listener.is_running()


def test_restart_no_listener_with_session_starts(env, sigs, capsys):
    bind()
    assert restart.run() == 0
    held, pid, ver = listener.instance()
    assert held and ver == V
    assert f"✅ 监听进程已重启（pid {pid}，版本 {V}）" in capsys.readouterr().out


def test_restart_replaces_current_listener(env, sigs, capsys):
    bind()
    old = subprocess.Popen([sys.executable, str(BIN), "listen"], env=dict(os.environ),
                           stdin=subprocess.DEVNULL)
    _wait(lambda: listener.instance()[1] == old.pid)
    assert restart.run() == 0
    assert old.wait(timeout=5) == 0
    held, pid, ver = listener.instance()
    assert held and pid != old.pid and ver == V
    assert sigs == [(old.pid, signal.SIGTERM)]


def test_restart_migrates_old_protocol_listener(env, sigs):
    bind()
    old = start_fake_listener()  # 只有 pid 文件，没有实例信息文件
    assert listener.instance() == (True, old.pid, None)
    assert restart.run() == 0
    assert sigs == [(old.pid, signal.SIGTERM)]
    held, pid, ver = listener.instance()
    assert held and pid != old.pid and ver == V


def test_restart_stale_version_file_treated_unknown(env, sigs):
    bind()
    old = start_fake_listener()
    paths.listener_version_file().write_text(json.dumps({"pid": 999999, "code_version": V}))
    assert restart.run() == 0
    assert (old.pid, signal.SIGTERM) in sigs
    held, pid, ver = listener.instance()
    assert pid != old.pid and ver == V
    assert json.loads(paths.listener_version_file().read_text())["pid"] == pid


def test_restart_stops_without_session(env, sigs, capsys):
    old = start_fake_listener("--version", "000000000000")
    assert restart.run() == 0
    assert old.wait(timeout=5) == 0
    assert not listener.is_running()
    assert "没有开启中的会话" in capsys.readouterr().out


def test_restart_sigkill_when_term_ignored(env, sigs):
    bind()
    old = start_fake_listener("--ignore-term")
    assert restart.run(term_wait=0.5) == 0
    assert sigs[:2] == [(old.pid, signal.SIGTERM), (old.pid, signal.SIGKILL)]
    assert listener.instance()[2] == V


def test_restart_no_sigkill_if_exited_just_before(env, sigs, monkeypatch):
    bind()
    old = start_fake_listener("--ignore-term")
    calls = []

    def wait_handover(p, timeout, interval):
        calls.append(p)
        if len(calls) == 1:  # 超时那一刻原实例自己退出了
            os.kill(old.pid, signal.SIGKILL)
            old.wait()
            return False
        return real(p, timeout, interval)

    real = restart._wait_handover
    monkeypatch.setattr(restart, "_wait_handover", wait_handover)
    assert restart.run() == 0
    assert (old.pid, signal.SIGKILL) not in sigs


def test_restart_accepts_concurrent_handover(env, sigs):
    """等待期间别的路径（/lark、auto-connect）拉起了新实例：接受交接，不 SIGKILL 任何人。"""
    bind()
    old = start_fake_listener("--ignore-term")

    def take_over():
        _wait(lambda: (old.pid, signal.SIGTERM) in sigs)
        os.kill(old.pid, signal.SIGKILL)
        old.wait()
        listener.ensure_running()

    th = threading.Thread(target=take_over)
    th.start()
    assert restart.run(interval=0.5) == 0
    th.join()
    assert all(s != signal.SIGKILL for _, s in sigs)
    held, pid, ver = listener.instance()
    assert held and pid != old.pid and ver == V


def test_restart_retries_spawn_once(env, sigs, monkeypatch):
    bind()
    n = []
    real = listener.ensure_running

    def flaky():
        n.append(1)
        return True if len(n) == 1 else real()  # 第一次"返回 True 但没起来"

    monkeypatch.setattr(listener, "ensure_running", flaky)
    assert restart.run(up_wait=1) == 0
    assert len(n) == 2 and listener.instance()[2] == V


def test_restart_reports_spawn_failure(env, sigs, monkeypatch, capsys):
    bind()
    monkeypatch.setattr(listener, "ensure_running", lambda: True)
    assert restart.run(up_wait=0.3) == 1
    out = capsys.readouterr().out
    assert "❌" in out and "listener.log" in out and "errors.log" in out


def test_restart_lock_held_without_pid_file(env, sigs, capsys):
    bind()
    other = _hold_listener_lock()  # 与 test_listener.py 同名辅助，本文件复制一份
    try:
        assert restart.run() == 1
    finally:
        other.close()
    assert sigs == []
    assert "❌" in capsys.readouterr().out


def test_restart_kill_ineffective_reports_failure(env, monkeypatch, capsys):
    bind()
    old = start_fake_listener("--ignore-term")
    monkeypatch.setattr(restart, "_signal", lambda pid, sig: None)  # 信号全被吞
    assert restart.run(term_wait=0.3, kill_wait=0.3) == 1
    assert "❌" in capsys.readouterr().out
    assert old.poll() is None


def test_cli_restart_listener(env, capsys):
    from lark_bridge import cli

    assert cli.main(["restart-listener"]) == 0
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run --with pytest pytest tests/test_restart.py -v`
Expected: FAIL（`ModuleNotFoundError: lark_bridge.restart`）

- [ ] **Step 4: Implement `lark_bridge/restart.py` 与 CLI 分发**

`run()` 流程，按 spec 4.2：

1. `with listener._pid_lock()`：`held, p, _ = listener._instance_locked()`；held 且 `p is None` → 打印 `❌` 行与 `   修复：手动结束 claude-lark listen 进程，再运行 claude-lark restart-listener`，返回 1；held → `_signal(p, SIGTERM)`。
2. 若发过 SIGTERM：放锁后 `_wait_handover(p, term_wait, interval)`；为假则 `_kill_if_still(p)`；发了 SIGKILL 就再 `_wait_handover(p, kill_wait, interval)`，仍为假 → `❌` + 同样的修复提示，返回 1。
3. `listener._any_enabled()` 为假 → 打印 `✅ 没有开启中的会话，不需要监听进程`，返回 0。
4. `want = listener.code_version()`；最多两轮：`listener.ensure_running()` 后在 `up_wait` 内按 `interval` 轮询 `listener.instance()`，`held and ver == want` 即打印 `✅ 监听进程已重启（pid N，版本 V）` 返回 0。
5. 两轮都不满足 → `❌ 监听进程没有拉起来，或运行的不是当前版本` + `   修复：查看 ~/.claude/lark/listener.log 与 ~/.claude/lark/errors.log，再运行 claude-lark restart-listener`，返回 1。

修复提示里的路径用 `paths.listener_log()` 与 `paths.home() / "errors.log"` 的实际值。`ensure_running` 通过 `listener.ensure_running` 属性访问（测试会替换它）。`cli.main`：`argv == ["restart-listener"]` 时 `from . import restart; return restart.run()`；用法串加入 `restart-listener`。

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --with pytest pytest tests -v`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add lark_bridge/restart.py lark_bridge/cli.py tests/fake_listener.py tests/conftest.py tests/test_restart.py
git commit -m "feat: claude-lark restart-listener stops the old listener and starts current code"
```

---

### Task 4: doctor 检查监听进程的代码版本

**Files:**
- Modify: `lark_bridge/doctor.py`（`_check_listener`）
- Test: `tests/test_doctor_listener.py`

**Interfaces:**
- Consumes: `listener.instance()`、`listener.code_version()`（Task 1），`listener.any_enabled_readonly()`（Task 2），`conftest.start_fake_listener()`（Task 3）。
- Produces: `doctor._check_listener(report)` 的四种输出。

- [ ] **Step 1: Write the failing tests**

`SID`、`bind()` 与 `tests/test_listener.py` 相同（本文件复制）。每个测试用 `lines = []`、`report = lambda ok, msg, fix=None: lines.append((ok, msg, fix))` 调 `doctor._check_listener(report)`；teardown kill fake 进程。

```python
def test_doctor_listener_current_version(lark_home):
    p = start_fake_listener("--version", listener.code_version())
    doctor._check_listener(report)
    assert lines == [(True, "监听进程运行中（代码为当前版本）", None)]


def test_doctor_listener_old_version(lark_home):
    start_fake_listener("--version", "000000000000")
    doctor._check_listener(report)
    ok, msg, fix = lines[0]
    assert not ok and msg == "监听进程运行的是旧代码"
    assert "install.sh" in fix and "claude-lark restart-listener" in fix


def test_doctor_listener_unknown_version(lark_home):
    start_fake_listener()  # 旧协议：无实例信息文件
    doctor._check_listener(report)
    assert lines[0][:2] == (False, "监听进程运行的是旧代码")


def test_doctor_no_listener_no_session(lark_home):
    doctor._check_listener(report)
    assert lines == [(True, "监听进程未运行（在会话里 /lark 开启桥接时会自动拉起）", None)]


def test_doctor_session_but_no_listener(lark_home):
    bind()  # 开启中、claude_pid=None
    doctor._check_listener(report)
    ok, msg, fix = lines[0]
    assert not ok and msg == "有开启中的会话但监听进程未运行"
    assert "claude-lark restart-listener" in fix
    assert "listener.log" in fix and "errors.log" in fix


def test_doctor_does_not_delete_stale_enabled(lark_home):
    state.save_binding(state.Binding(SID, "oc_x", "群", "/w", None, 1.0))
    f = paths.session_dir(SID) / "enabled"
    f.write_text("999999\nticks:1")  # 已死的 claude
    doctor._check_listener(report)
    assert f.exists()
    assert lines[0][0] is True  # 失效会话不算开启
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --with pytest pytest tests/test_doctor_listener.py -v`
Expected: FAIL（文案不匹配）

- [ ] **Step 3: Implement `_check_listener`**

`held, _, ver = listener.instance()`：held 且 `ver == listener.code_version()` → `report(True, "监听进程运行中（代码为当前版本）")`；held 其他 → `report(False, "监听进程运行的是旧代码", "重新运行 ./install.sh（或 claude-lark restart-listener）")`；未 held 且 `listener.any_enabled_readonly()` → `report(False, "有开启中的会话但监听进程未运行", f"claude-lark restart-listener；仍失败时查看 {paths.listener_log()} 与 {paths.home() / 'errors.log'}")`；否则维持原 `✅` 文案。

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --with pytest pytest tests -v`
Expected: 全部 PASS（含 `tests/test_settings.py` 的 doctor 测试）

- [ ] **Step 5: Commit**

```bash
git add lark_bridge/doctor.py tests/test_doctor_listener.py
git commit -m "feat: doctor reports a listener running outdated code"
```

---

### Task 5: install.sh 与 README

**Files:**
- Modify: `install.sh`（第 6 步之前）
- Modify: `README.md`（中文摘要"从旧版本升级"一条、`## Install` 步骤列表、`## Upgrade` 一节）
- Test: `tests/test_restart.py`

- [ ] **Step 1: Write the failing test**

```python
def test_install_restarts_listener_before_doctor():
    text = (ROOT / "install.sh").read_text()
    r = text.index("claude-lark\" restart-listener || true")
    d = text.index("claude-lark\" doctor || true")
    assert r < d
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --with pytest pytest tests/test_restart.py::test_install_restarts_listener_before_doctor -v`
Expected: FAIL（`ValueError: substring not found`）

- [ ] **Step 3: Edit `install.sh`**

在 `# 6. 自检` 之前插入新步骤 `# 6. 重启监听进程（升级后让它运行新代码；失败不让安装失败）`，执行 `echo` 后 `"$LARK_DIR/app/bin/claude-lark" restart-listener || true`；原自检改为第 7 步。

- [ ] **Step 4: Edit `README.md`**

- 中文摘要"从旧版本升级"：补一句"重跑 `install.sh` 会重启正在运行的监听进程，让它运行新代码；`claude-lark doctor` 会检查监听进程运行的是不是当前代码"。
- `## Install` 列表：在 doctor 之前插入一步 "Restarts the running listener (`claude-lark restart-listener`) so it runs the new code. Output: `✅ 监听进程已重启（pid N，版本 V）`, or `没有开启中的会话，不需要监听进程` when no session is bridged. A failure prints `❌` with a `修复：` line; install still succeeds."，后续编号顺延。
- `## Upgrade`：说明 `git pull` 后重跑 `install.sh`，因为常驻的监听进程不会自己加载新代码；`claude-lark doctor` 报 `监听进程运行的是旧代码` 时运行 `install.sh` 或 `claude-lark restart-listener`。

- [ ] **Step 5: Verify**

Run: `bash -n install.sh && uv run --with pytest pytest tests -v`
Expected: 语法检查无输出；全部 PASS

- [ ] **Step 6: Commit**

```bash
git add install.sh README.md tests/test_restart.py
git commit -m "feat: install.sh restarts the listener; document upgrade behavior"
```
