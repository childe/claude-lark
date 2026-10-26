"""claude-lark doctor：逐项自检，打印 ✅/❌ 和修复命令。只读，绝不向飞书发写请求。"""

import json
import re
import subprocess
import sys
from pathlib import Path

from . import paths, settings

MIN_CLI = (1, 0, 92)
MIN_PY = (3, 9)
_TIMEOUT = 30


def _run(args):
    """跑 lark-cli，返回 (rc, stdout, stderr)；找不到/超时返回 (None, "", 原因)。"""
    try:
        r = subprocess.run(
            [paths.lark_cli(), *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return None, "", f"超过 {_TIMEOUT} 秒没有响应"
    except OSError as e:
        return None, "", str(e)
    return r.returncode, r.stdout, r.stderr


def _json(text):
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _check_python(report):
    v = sys.version_info[:2]
    if v >= MIN_PY:
        report(True, f"python {v[0]}.{v[1]}")
    else:
        report(
            False,
            f"python {v[0]}.{v[1]} 太旧，需要 ≥ 3.9",
            "安装 python3.9 或更新版本（macOS: brew install python）",
        )


def _check_cli(report):
    rc, out, err = _run(["--version"])
    if rc is None or rc != 0:
        report(
            False,
            f"找不到可用的 lark-cli（{paths.lark_cli()}）：{err.strip()[:100]}",
            "npm i -g @larksuite/cli",
        )
        return False
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    ver = tuple(int(x) for x in m.groups()) if m else None
    want = ".".join(map(str, MIN_CLI))
    if ver is None or ver < MIN_CLI:
        report(
            False,
            f"lark-cli 版本 {out.strip() or '未知'} 低于 {want}",
            "npm i -g @larksuite/cli@latest",
        )
        return False
    report(True, f"lark-cli {'.'.join(map(str, ver))}")
    return True


def _check_auth(report):
    """返回当前 auth status 里的 user open_id（取不到为 None）。"""
    rc, out, err = _run(["auth", "status"])
    if rc != 0:
        report(
            False,
            f"auth status 失败：{(err or out).strip()[:100]}",
            "lark-cli auth login",
        )
        return None
    ids = _json(out).get("identities") or {}
    bad = [n for n in ("bot", "user") if (ids.get(n) or {}).get("status") != "ready"]
    if bad:
        report(
            False,
            f"身份未就绪：{', '.join(bad)}",
            "lark-cli auth login（bot 和 user 都要 ready）",
        )
    else:
        report(True, "bot 与 user 身份均 ready")
    oid = (ids.get("user") or {}).get("openId")
    return oid if isinstance(oid, str) and oid else None


def _check_events(report):
    rc, out, err = _run(
        ["event", "consume", "im.message.receive_v1", "--as", "bot", "--dry-run"]
    )
    if rc != 0:
        report(
            False,
            f"事件订阅 dry-run 失败：{(err or out).strip()[:100]}",
            "lark-cli event consume im.message.receive_v1 --as bot --dry-run",
        )
        return
    pre = ((_json(out).get("data") or {}).get("decision") or {}).get("preconditions")
    if not isinstance(pre, list) or not pre:
        report(
            False,
            "dry-run 没返回 preconditions",
            "lark-cli event consume im.message.receive_v1 --as bot --dry-run",
        )
        return
    bad = [p.get("name", "?") for p in pre if (p or {}).get("status") != "ok"]
    if bad:
        report(
            False,
            f"事件订阅前置条件未通过：{', '.join(bad)}",
            "到飞书开放平台检查：发布应用、订阅 im.message.receive_v1（长连接）、开通所需权限",
        )
    else:
        report(True, "事件订阅 im.message.receive_v1 前置条件全部通过")


def _check_files(report):
    cmd = Path.home() / ".claude" / "commands" / "lark.md"
    if cmd.exists():
        report(True, f"{cmd} 存在")
    else:
        report(False, f"{cmd} 不存在", "运行仓库里的 ./install.sh")
    sp = settings.settings_path()
    try:
        ok = settings.has_hooks(settings.load(sp))
    except settings.SettingsError as e:
        report(False, str(e), f"修复 {sp} 后重新运行 ./install.sh")
        return
    if ok:
        report(True, f"{sp} 已包含 claude-lark 的 hooks")
    else:
        report(False, f"{sp} 缺少 claude-lark 的 hooks", "运行仓库里的 ./install.sh")


def _check_owner(report, current):
    cfg = paths.config_file()
    try:
        saved = json.loads(cfg.read_text()).get("owner_open_id")
    except (OSError, ValueError, AttributeError):
        saved = None
    if not saved:
        report(False, f"{cfg} 里没有 owner_open_id", "运行仓库里的 ./install.sh")
    elif current is None:
        report(False, "无法确认当前登录用户，跳过 owner 对比", "lark-cli auth login")
    elif saved != current:
        report(
            False,
            "config.json 的 owner 与当前登录用户不一致",
            "重新运行 ./install.sh 以重新绑定 owner",
        )
    else:
        report(True, "config.json 的 owner 与当前登录用户一致")


def _check_listener(report):
    from . import listener

    if listener.is_running():
        report(True, "监听进程运行中")
    else:
        # 没有会话开着桥接时监听进程本来就不会常驻，这不算故障
        report(True, "监听进程未运行（在会话里 /lark 开启桥接时会自动拉起）")


def run() -> int:
    failed = [0]

    def report(ok, msg, fix=None):
        print(f"{'✅' if ok else '❌'} {msg}")
        if not ok:
            failed[0] += 1
            if fix:
                print(f"   修复：{fix}")

    _check_python(report)
    if _check_cli(report):
        oid = _check_auth(report)
        _check_events(report)
    else:
        oid = None
    _check_files(report)
    _check_owner(report, oid)
    _check_listener(report)
    if failed[0]:
        print(
            f"\n有 {failed[0]} 项未通过，按上面的修复命令处理后重新运行 claude-lark doctor"
        )
        return 1
    print("\n全部通过")
    return 0
