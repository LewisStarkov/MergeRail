from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from mergerail.execution.docker import MIB, DockerExecution, DockerExecutionError
from mergerail.execution.policy import ExecutionPolicy
from mergerail.execution.sync import (
    SyncError,
    _run_bounded,
    bundle_refs,
    create_bundle,
    host_git,
    host_git_command,
    import_result_bundle,
    resolve_commit,
    validate_bundle_header,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _repo(root: Path, *, content: str = "initial\n") -> tuple[str, str]:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.name", "Docker runtime tests")
    _git(root, "config", "user.email", "docker-runtime@example.invalid")
    (root / "source.txt").write_text(content, encoding="utf-8")
    _git(root, "add", "source.txt")
    _git(root, "commit", "-qm", "initial")
    return _git(root, "rev-parse", "HEAD"), "source.txt"


def _result_bundle(source: Path, sha: str, target: Path) -> bytes:
    _git(source, "update-ref", "refs/heads/mergerail-result", sha)
    subprocess.run(
        ["git", "-C", str(source), "bundle", "create", str(target), "refs/heads/mergerail-result"],
        check=True,
        capture_output=True,
    )
    return target.read_bytes()


def test_host_git_disables_included_case_sensitive_filter_driver(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _repo(root)
    (root / ".gitattributes").write_text("*.txt filter=Evil_Bad\n", encoding="utf-8")
    _git(root, "add", ".gitattributes")
    _git(root, "commit", "-qm", "attributes")

    marker = tmp_path / "filter-was-run"
    include = tmp_path / "included-driver.cfg"
    payload = (
        f"from pathlib import Path;Path({str(marker)!r}).write_text('ran');"
        "import sys;sys.stdout.write(sys.stdin.read())"
    )
    executable = shlex.quote(sys.executable) + " -c " + shlex.quote(payload)
    _git(root, "config", "--file", str(include), "filter.Evil_Bad.smudge", executable)
    _git(root, "config", "--file", str(include), "filter.Evil_Bad.required", "true")
    _git(root, "config", "--local", "include.path", str(include))
    (root / "source.txt").unlink()
    _git(root, "checkout-index", "--all", "--force")
    assert marker.read_text() == "ran"
    marker.unlink()
    (root / "source.txt").unlink()
    command, env = host_git_command(root, "checkout-index", "--all", "--force")
    overrides = [arg for arg in command if arg.startswith("filter.Evil_Bad.")]

    assert "filter.Evil_Bad.clean=" in overrides
    assert "filter.Evil_Bad.smudge=" in overrides
    assert "filter.Evil_Bad.process=" in overrides
    assert "filter.Evil_Bad.required=false" in overrides
    assert subprocess.run(command, cwd=root, env=env, capture_output=True).returncode == 0
    assert not marker.exists()


def test_worktree_filters_are_disabled_but_normal_diff_settings_are_accepted(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    _repo(root)
    (root / ".gitattributes").write_text("*.txt filter=Worktree_Evil\n")
    _git(root, "add", ".gitattributes")
    _git(root, "commit", "-qm", "attributes")
    _git(root, "config", "extensions.worktreeConfig", "true")
    _git(root, "config", "diff.algorithm", "histogram")
    marker = tmp_path / "executed"
    payload = (
        f"from pathlib import Path;Path({str(marker)!r}).write_text('ran');"
        "import sys;sys.stdout.write(sys.stdin.read())"
    )
    command = shlex.quote(sys.executable) + " -c " + shlex.quote(payload)
    _git(root, "config", "--worktree", "filter.Worktree_Evil.smudge", command)
    (root / "source.txt").unlink()
    _git(root, "checkout-index", "--all", "--force")
    assert marker.read_text() == "ran"
    marker.unlink()
    (root / "source.txt").unlink()
    host_git(root, "checkout-index", "--all", "--force")
    assert (root / "source.txt").read_text() == "initial\n"
    assert not marker.exists()
    assert host_git(root, "--version").startswith("git version")


def test_host_git_command_scrubs_inherited_git_execution_overrides(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _repo(root)
    command, env = host_git_command(root, "status", "--short")

    assert "GIT_EXEC_PATH" not in env
    assert "GIT_DIR" not in env
    assert "GIT_CONFIG_PARAMETERS" not in env
    assert "core.hooksPath=" + os.devnull in command
    assert "core.attributesFile=" + os.devnull in command


def test_host_git_output_has_a_total_hard_limit(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _repo(root)
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}

    with pytest.raises(SyncError, match="output exceeded"):
        _run_bounded(
            [
                sys.executable,
                "-c",
                "import sys;sys.stdout.write('x'*4096);sys.stderr.write('y'*4096)",
            ],
            cwd=root,
            env=env,
            timeout=5,
            output_limit=1024,
        )


def test_create_and_import_only_expected_result_commit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    base, _ = _repo(source)
    (source / "source.txt").write_text("checkpoint\n", encoding="utf-8")
    _git(source, "commit", "-qam", "checkpoint")
    result = resolve_commit(source, "HEAD")
    frozen = create_bundle(source, (base, result), max_bytes=1024 * 1024)
    assert set(bundle_refs(frozen)) == {base, result}

    bundle = _result_bundle(source, result, tmp_path / "result.bundle")
    receiver = tmp_path / "receiver"
    receiver.mkdir()
    subprocess.run(["git", "init", "-q", str(receiver)], check=True)
    imported = import_result_bundle(
        receiver,
        bundle,
        expected_sha=result,
        base_sha=base,
        branch="mergerail-candidate/task-uuid",
        max_bytes=1024 * 1024,
    )
    assert imported == result
    assert host_git(receiver, "rev-parse", "refs/heads/mergerail-candidate/task-uuid") == result
    with pytest.raises(SyncError, match="advertise only"):
        validate_bundle_header(bundle, base)


def test_result_bundle_rejects_prerequisite_and_extra_refs() -> None:
    sha = "a" * 40
    with pytest.raises(SyncError, match="prerequisite"):
        validate_bundle_header(
            f"# v3 git bundle\n-{sha} missing-base\n\nPACK".encode(),
            sha,
        )
    with pytest.raises(SyncError, match="advertise only"):
        result_header = (
            f"# v2 git bundle\n{sha} refs/heads/mergerail-result\n{sha} refs/heads/extra\n\nPACK"
        )
        validate_bundle_header(
            result_header.encode(),
            sha,
        )


def test_docker_refuses_remote_host_environment_before_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _repo(root)
    state = tmp_path / "state"
    policy = ExecutionPolicy(image="mergerail-runtime@sha256:" + "a" * 64)
    execution = DockerExecution(policy, root, state)
    monkeypatch.setattr("mergerail.execution.docker.platform.system", lambda: "Linux")
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
    monkeypatch.setattr(
        execution, "_docker", lambda *args, **kwargs: pytest.fail("Docker CLI must not run")
    )

    with pytest.raises(DockerExecutionError, match="DOCKER_HOST"):
        execution.preflight()
    assert execution.metadata["validated"] is False


def test_container_options_keep_worker_isolation_and_tmpfs_caps(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _repo(root)
    policy = ExecutionPolicy(image="mergerail-runtime@sha256:" + "a" * 64)
    execution = DockerExecution(policy, root, tmp_path / "state")
    args = execution._container_options(name="fixture")

    assert "--read-only" in args
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert args[args.index("--security-opt") + 1] == "no-new-privileges=true"
    assert args[args.index("--memory-swap") + 1] == str(policy.memory_mib * MIB)
    mounts = [args[index + 1] for index, arg in enumerate(args[:-1]) if arg == "--tmpfs"]
    assert any(item.startswith("/work:rw,nosuid,nodev,exec,size=") for item in mounts)
    assert any(item.startswith("/tmp:rw,nosuid,nodev,noexec,size=") for item in mounts)
    assert "-v" not in args and "--mount" not in args
