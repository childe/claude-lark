import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "scan-secrets.sh"
REAL = "ou_" + "0123456789abcdef"


def scan(path):
    return subprocess.run(
        [str(SCRIPT), str(path)], capture_output=True, text=True
    ).returncode


def write(tmp_path, name, text):
    f = tmp_path / name
    f.write_text(text + "\n")
    return f


def test_mixed_line_fake_and_real_blocked(tmp_path):
    f = write(tmp_path, "a.txt", f"ou_test_owner {REAL}")
    assert scan(f) == 1


def test_filename_with_test_does_not_exempt(tmp_path):
    f = write(tmp_path, "x_test_y.txt", REAL)
    assert scan(f) == 1


def test_fake_only_allowed(tmp_path):
    f = write(tmp_path, "b.txt", "owner = ou_test_owner")
    assert scan(f) == 0


def test_real_path_blocked(tmp_path):
    f = write(tmp_path, "c.txt", "p = /Users" + "/x")
    assert scan(f) == 1
