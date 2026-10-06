"""Build reviewed Rivals images on the dedicated VM, without deploy credentials."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path

from .execution.sync import host_git, host_git_command

ENGINE = ["docker", "--context", "colima-mergerail-devbot"]
BUILDER = "mergerail-devbot-build"
BUILD_CONTAINER = "buildx_buildkit_mergerail-devbot-build0"
BUILD_IMAGE = (
    "moby/buildkit@sha256:0039c1d47e8748b5afea56f4e85f14febaf34452bd99d9552d2daa82262b5cc5"
)
BINFMT_IMAGE = (
    "tonistiigi/binfmt@sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
)
ROOTS = (
    "Dockerfile",
    ".dockerignore",
    "Caddyfile",
    "compose.prod.yml",
    "pyproject.toml",
    "uv.lock",
    "alembic.ini",
    "bot",
    "core",
    "migrations",
    "ops",
    "webapp",
    "miniapp",
    "admin",
    "scripts",
)


def content_tag(root: Path, paths: list[str]) -> str:
    files = {}
    for name in paths:
        path = root / name
        for item in sorted(path.rglob("*")) if path.is_dir() else [path]:
            if item.is_file():
                files[str(item.relative_to(root))] = hashlib.sha256(item.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:20]


def prepare_images(
    root: Path,
    state: Path,
    sha: str,
    remote: Callable[[str], str],
    run: Callable[[list[str], Path, float | None], int],
) -> None:
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("invalid build SHA")
    # Only one leased deployer owns these disposable paths; history lives elsewhere.
    for old in state.glob("build-*"):
        if re.fullmatch(r"build-[a-f0-9]{40}", old.name) and old.is_dir() and not old.is_symlink():
            shutil.rmtree(old)
    context = state / f"build-{sha}"
    try:
        _prepare_images(root, state, sha, context, remote, run)
    finally:
        if context.is_dir() and not context.is_symlink():
            shutil.rmtree(context)
        for name in ("source.tar", "image.tar"):
            (state / name).unlink(missing_ok=True)


def _prepare_images(
    root: Path,
    state: Path,
    sha: str,
    context: Path,
    remote: Callable[[str], str],
    run: Callable[[list[str], Path, float | None], int],
) -> None:
    entries = host_git(root, "ls-tree", "-rl", sha, "--", *ROOTS).splitlines()
    size = sum(int(entry.split()[3]) for entry in entries if entry.split()[3] != "-")
    if size > 512 * 1024 * 1024 or len(entries) > 50000:
        raise ValueError("immutable build context exceeds its 512 MiB/50000-file limit")
    context.mkdir(mode=0o700, exist_ok=True)
    archive = state / "source.tar"
    command, env = host_git_command(root, "archive", "--format=tar", sha, "--", *ROOTS)
    with archive.open("wb") as handle:
        subprocess.run(command, cwd=root, env=env, stdout=handle, check=True, timeout=120)
    with tarfile.open(archive) as handle:
        handle.extractall(context, filter="data")
    archive.unlink()
    shared = ["Dockerfile", ".dockerignore"]
    images = {
        "python-app": "rivals-dev-app:"
        + content_tag(
            context,
            [
                *shared,
                "pyproject.toml",
                "uv.lock",
                "alembic.ini",
                "bot",
                "core",
                "migrations",
                "ops",
                "webapp",
            ],
        ),
        "gateway": "rivals-dev-gateway:"
        + content_tag(
            context,
            [*shared, "miniapp", "admin", "Caddyfile"],
        ),
        "db-backup": "rivals-dev-backup:"
        + content_tag(
            context,
            [
                *shared,
                "scripts/backup_db.sh",
            ],
        ),
    }
    missing = {
        target: image
        for target, image in images.items()
        if remote(
            f"docker image inspect '{image}' >/dev/null 2>&1 && echo cached || echo missing\n"
        )
        != "cached"
    }
    if not missing:
        return
    log = state / "build.log"
    config = state / "buildkitd.toml"
    config.write_text(
        "[worker.oci]\nmax-parallelism = 1\ngc = true\n"
        'reservedSpace = "512MB"\nmaxUsedSpace = "2GB"\n'
    )
    exists = (
        subprocess.run(
            [*ENGINE, "buildx", "inspect", BUILDER],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )
    if not exists:
        commands = [
            [*ENGINE, "run", "--privileged", "--rm", BINFMT_IMAGE, "--install", "amd64"],
            [
                *ENGINE,
                "buildx",
                "create",
                "--name",
                BUILDER,
                "--driver",
                "docker-container",
                "--driver-opt",
                f"image={BUILD_IMAGE}",
                "--driver-opt",
                "memory=3g",
                "--driver-opt",
                "memory-swap=3g",
                "--driver-opt",
                "cpu-quota=100000",
                "--buildkitd-config",
                str(config),
                "colima-mergerail-devbot",
            ],
        ]
        for setup in commands:
            if run(setup, log, 300):
                raise RuntimeError("dedicated build setup failed; see private build.log")
    if run([*ENGINE, "buildx", "inspect", "--bootstrap", BUILDER], log, 300):
        raise RuntimeError("dedicated builder startup failed")
    if run([*ENGINE, "update", "--pids-limit=256", BUILD_CONTAINER], log, 30):
        raise RuntimeError("cannot enforce build PID limit")
    for target, image in missing.items():
        command = [
            *ENGINE,
            "buildx",
            "build",
            "--builder",
            BUILDER,
            "--platform",
            "linux/amd64",
            "--provenance=false",
            "--load",
            "--target",
            target,
            "-t",
            image,
            str(context),
        ]
        if run(command, log, 1800):
            raise RuntimeError(
                "isolated image build failed; DevBot unchanged; see private build.log"
            )
        expected = subprocess.check_output(
            [*ENGINE, "image", "inspect", "--platform=linux/amd64", image, "--format", "{{.Id}}"],
            text=True,
        ).strip()
        # Only the trusted host transports images; no credentials enter the builder.
        output = state / "image.tar"
        if run(
            [*ENGINE, "image", "save", "--platform=linux/amd64", "-o", str(output), image], log, 300
        ):
            raise RuntimeError("cannot export the immutable build image")
        if output.stat().st_size > 4 * 1024 * 1024 * 1024:
            raise RuntimeError("build image archive exceeds the 4 GiB limit")
        with output.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        incoming = f"/opt/rivals-dev-config/mergerail-image-{sha}.tar"
        if run(
            ["rsync", "--partial", "--timeout=120", str(output), f"projects-main:{incoming}"],
            log,
            1800,
        ):
            raise RuntimeError("immutable image upload failed")
        remote(
            f"set -eu\necho '{digest}  {incoming}' | sha256sum -c -\n"
            f"docker load -i '{incoming}' >/dev/null\n"
            f"test \"$(docker image inspect '{image}' --format '{{{{.Id}}}}')\" = '{expected}'\n"
            f"rm -- '{incoming}'\n"
        )
        output.unlink()
        if run([*ENGINE, "image", "rm", image], log, 30):
            raise RuntimeError("uploaded image is verified, but VM image cleanup failed")
