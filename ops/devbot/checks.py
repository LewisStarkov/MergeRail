"""Operator-owned offline checks, installed in the pinned worker image."""

import ast
import os
import subprocess
from pathlib import Path

root = Path("/work/repo")
python = "/opt/rivals-deps/.venv/bin/python"
for directory in ("bot", "core", "ops", "webapp"):
    for path in (root / directory).rglob("*.py"):
        ast.parse(path.read_text(), filename=str(path))
for project in ("miniapp", "admin"):
    modules = root / project / "node_modules"
    if modules.exists() or modules.is_symlink():
        raise SystemExit("node_modules must be absent from the immutable source snapshot")
    modules.symlink_to(f"/opt/{project}-deps/node_modules", target_is_directory=True)
    subprocess.run(
        [f"/opt/{project}-deps/node_modules/.bin/tsc", "--noEmit"],
        cwd=root / project,
        check=True,
        timeout=180,
    )
temporary = Path("/work/test-tmp")
temporary.mkdir()
env = {
    **os.environ,
    "PYTHONPATH": str(root),
    "TMPDIR": str(temporary),
    "PLAYWRIGHT_BROWSERS_PATH": "/opt/rivals-browsers",
}
subprocess.run(
    [python, "-m", "ruff", "check", "bot", "core", "ops", "webapp", "tests"],
    cwd=root,
    env=env,
    check=True,
    timeout=180,
)
subprocess.run(
    [python, "-m", "mypy", "bot", "core", "ops", "webapp"],
    cwd=root,
    env=env,
    check=True,
    timeout=300,
)
subprocess.run(
    [python, "-m", "pytest", "-q", "--tb=short", "--disable-warnings"],
    cwd=root,
    env=env,
    check=True,
    timeout=900,
)
