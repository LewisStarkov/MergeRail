"""Build operator-owned images and seed a dedicated Engine from checked Git objects."""

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
RIVALS = Path("/Users/lama/Documents/dev/rivals")
RUNTIME = Path("/Users/lama/.local/share/mergerail-devbot")
ENGINE = ["docker", "--context", "colima-mergerail-devbot"]


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True).strip()


running = subprocess.run(
    [*ENGINE, "inspect", "mergerail-devbot-controller-1", "--format", "{{.State.Running}}"],
    capture_output=True,
    text=True,
)
if running.returncode == 0 and running.stdout.strip() == "true":
    raise SystemExit("Stop the controller before updating images; active approvals need their pins")
# Keep existing immutable references before moving the convenience :checked tags.
previous = RUNTIME / "runtime.env"
if previous.exists():
    values = dict(line.split("=", 1) for line in previous.read_text().splitlines() if "=" in line)
    for role in ("worker", "controller"):
        image = values[f"MERGERAIL_{role.upper()}_IMAGE"]
        subprocess.run(
            [*ENGINE, "image", "tag", image, f"mergerail-devbot-{role}:pin-{image[7:]}"],
            check=True,
        )

os.umask(0o077)
for name in ("secrets", "outbox", "deployer", "build/controller", "build/worker"):
    (RUNTIME / name).mkdir(parents=True, exist_ok=True, mode=0o700)
sha = run("git", "-C", str(RIVALS), "rev-parse", "main")
if run("git", "-C", str(RIVALS), "branch", "--show-current") != "main":
    raise SystemExit("Rivals bootstrap checkout must be on main")
controller = RUNTIME / "build/controller"
worker = RUNTIME / "build/worker"
shutil.copytree(
    SOURCE / "src/mergerail",
    controller / "src/mergerail",
    dirs_exist_ok=True,
    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
)
shutil.copyfile(SOURCE / "ops/devbot/controller.Dockerfile", controller / "Dockerfile")
# This is model metadata, never OpenCode credentials or the user's home directory.
catalogue = json.loads((Path.home() / ".cache/opencode/models.json").read_text())
model = catalogue["opencode"]["models"]["space-bunny-free"]
if any(model["cost"][key] != 0 for key in ("input", "output")):
    raise SystemExit("The selected OpenCode model is no longer free")
(controller / "models.json").write_text(
    json.dumps(
        {
            "opencode": {
                "models": {
                    "space-bunny-free": model,
                }
            }
        }
    )
)
for name in (
    "pyproject.toml",
    "uv.lock",
    "miniapp/package.json",
    "miniapp/package-lock.json",
    "admin/package.json",
    "admin/package-lock.json",
):
    target = worker / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        subprocess.check_output(
            [
                "git",
                "-C",
                str(RIVALS),
                "show",
                f"{sha}:{name}",
            ]
        )
    )
shutil.copyfile(SOURCE / "ops/devbot/worker.Dockerfile", worker / "Dockerfile")
shutil.copyfile(SOURCE / "ops/devbot/checks.py", worker / "checks.py")
for name, context in (("worker", worker), ("controller", controller)):
    subprocess.run(
        [
            *ENGINE,
            "build",
            "--provenance=false",
            "-t",
            f"mergerail-devbot-{name}:checked",
            str(context),
        ],
        check=True,
        timeout=1800,
    )
images = {
    name: run(
        *ENGINE, "image", "inspect", f"mergerail-devbot-{name}:checked", "--format", "{{.Id}}"
    )
    for name in ("worker", "controller")
}
for role, image in images.items():
    subprocess.run(
        [*ENGINE, "image", "tag", image, f"mergerail-devbot-{role}:pin-{image[7:]}"], check=True
    )
subprocess.run(
    [
        *ENGINE,
        "pull",
        "python@sha256:e2a5fce94bd761967528a12f16d707c2613e1522f3f2d77fa45766f45962547f",
    ],
    check=True,
    timeout=300,
)
(RUNTIME / "runtime.env").write_text(
    f"MERGERAIL_RUNTIME_DIR={RUNTIME}\nMERGERAIL_CONTROLLER_IMAGE={images['controller']}\n"
    f"MERGERAIL_WORKER_IMAGE={images['worker']}\n"
)
password = RUNTIME / "secrets/web-password"
if not password.exists():
    password.write_text(secrets.token_urlsafe(32) + "\n")
ngrok = RUNTIME / "secrets/ngrok.yml"
if not ngrok.exists():
    configured = Path.home() / "Library/Application Support/ngrok/ngrok.yml"
    shutil.copyfile(configured, ngrok)
for path in (password, ngrok, RUNTIME / "runtime.env"):
    path.chmod(0o600)
checkout = RUNTIME / "deployer/checkout"
if not checkout.exists():
    subprocess.run(
        ["git", "clone", "--no-hardlinks", "-b", "main", str(RIVALS), str(checkout)], check=True
    )
    origin = run("git", "-C", str(RIVALS), "remote", "get-url", "origin")
    subprocess.run(["git", "-C", str(checkout), "remote", "set-url", "origin", origin], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "config", "core.hooksPath", "/dev/null"], check=True
    )
    subprocess.run(
        ["git", "-C", str(checkout), "config", "branch.main.remote", "origin"], check=True
    )
    subprocess.run(
        ["git", "-C", str(checkout), "config", "branch.main.merge", "refs/heads/main"], check=True
    )
    if run("git", "-C", str(checkout), "rev-parse", "HEAD") != sha:
        raise SystemExit("Automation checkout differs from the checked bootstrap SHA")
    # Compare the release protocol before using an additional main checkout.
    for name in (
        "scripts/cpd-dev.sh",
        "scripts/cpd.sh",
        "scripts/remote-deploy.sh",
        "scripts/rollback.sh",
    ):
        if run("git", "-C", str(checkout), "show", f"HEAD:{name}") != run(
            "git",
            "-C",
            str(RIVALS),
            "show",
            f"{sha}:{name}",
        ):
            raise SystemExit("Automation checkout has a different release protocol")
bundle = RUNTIME / "bootstrap.bundle"
subprocess.run(
    ["git", "-C", str(RIVALS), "bundle", "create", str(bundle), "refs/heads/main"], check=True
)
bundle.chmod(0o600)
subprocess.run(
    [
        *ENGINE,
        "volume",
        "create",
        "--label",
        "com.mergerail.devbot=true",
        "mergerail-devbot_project",
    ],
    check=True,
)
subprocess.run(
    [
        *ENGINE,
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--cpus=1",
        "--memory=512m",
        "--pids-limit=64",
        "--tmpfs=/tmp:size=32m",
        "-v",
        "mergerail-devbot_project:/project",
        "-v",
        f"{bundle}:/bootstrap.bundle:ro",
        "--entrypoint",
        "sh",
        images["controller"],
        "-c",
        "if test -d /project/.git; then exit 0; fi; "
        "git clone -b main /bootstrap.bundle /project && cd /project && "
        "git remote remove origin && git config user.name MergeRail && "
        "git config user.email mergerail@localhost",
    ],
    check=True,
)
print(f"Prepared immutable images and bootstrap SHA {sha}; existing queue/checkouts preserved.")
