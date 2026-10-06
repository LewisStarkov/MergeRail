"""Install the user's launch agent; no system daemon or unrelated service changes."""

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

source = Path(__file__).resolve().parents[2]
service = Path("/Users/lama/.local/share/mergerail-devbot/service")
(service / "ops/devbot").mkdir(parents=True, exist_ok=True, mode=0o700)
shutil.copytree(
    source / "src/mergerail",
    service / "src/mergerail",
    dirs_exist_ok=True,
    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
)
for name in ("run.sh", "compose.yaml"):
    shutil.copyfile(source / "ops/devbot" / name, service / "ops/devbot" / name)
python = service / "python"
python.unlink(missing_ok=True)
python.symlink_to(Path(sys.executable).resolve())
target = Path.home() / "Library/LaunchAgents/local.mergerail-devbot.plist"
target.parent.mkdir(parents=True, exist_ok=True)
record = {
    "Label": "local.mergerail-devbot",
    "ProgramArguments": ["/bin/bash", str(service / "ops/devbot/run.sh")],
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 30,
    "StandardOutPath": "/dev/null",
    "StandardErrorPath": "/dev/null",
}
target.write_bytes(plistlib.dumps(record))
target.chmod(0o600)
domain = f"gui/{os.getuid()}"
subprocess.run(
    ["launchctl", "bootout", domain, str(target)],
    check=False,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
print("Installed local.mergerail-devbot; controller/queue persist across restarts.")
