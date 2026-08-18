from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from mergerail import update

REVISION = "1" * 40
PEELED_REVISION = "2" * 40


def candidate(
    tag: str = "v0.2.0",
    revision: str = REVISION,
    repository: str = update.REPOSITORY,
) -> update.ReleaseCandidate:
    return update.ReleaseCandidate(tag, revision, repository)


def completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_latest_release_uses_the_highest_stable_semver(monkeypatch: pytest.MonkeyPatch) -> None:
    output = (
        f"{'1' * 40}\trefs/tags/v1.9.0\n"
        f"{REVISION}\trefs/tags/v1.10.0\n"
        f"{'3' * 40}\trefs/tags/v2.0.0-rc1\n"
        f"{'4' * 40}\trefs/tags/not-a-version\n"
    )
    monkeypatch.setattr(update, "_run", lambda *args, **kwargs: completed(output))

    assert update.latest_release("https://example.test/mergerail.git") == "v1.10.0"


def test_install_release_uses_uv_and_the_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return completed()

    monkeypatch.setattr("mergerail.update.shutil.which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(update, "_run", run)
    monkeypatch.setenv("MERGERAIL_UPDATE_LOCK", str(tmp_path / "install.lock"))

    update.install_release("v1.2.3", "https://example.test/mergerail.git")

    assert calls == [
        [
            "/usr/bin/uv",
            "--no-config",
            "tool",
            "install",
            "--force",
            "git+https://example.test/mergerail.git@v1.2.3",
        ]
    ]


def test_discovery_pins_the_exact_peeled_remote_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = (
        f"{REVISION}\trefs/tags/v0.2.0\n"
        f"{PEELED_REVISION}\trefs/tags/v0.2.0^{{}}\n"
    )
    monkeypatch.setattr(update, "_run", lambda *args, **kwargs: completed(output))

    assert update._discover_release("git+https://example.test/mergerail.git") == candidate(
        revision=PEELED_REVISION,
        repository="https://example.test/mergerail.git",
    )


def test_candidate_install_uses_the_pinned_revision_from_a_trusted_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], Path | None]] = []

    def run(
        command: list[str], *, timeout: int, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append((command, cwd))
        return completed()

    monkeypatch.setattr("mergerail.update.shutil.which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(update, "_run", run)
    monkeypatch.setattr(update, "_user_update_root", lambda: tmp_path / "cache")

    update._install_release(candidate(repository="https://example.test/mergerail.git"))

    assert calls == [
        (
            [
                "/usr/bin/uv",
                "--no-config",
                "tool",
                "install",
                "--force",
                f"git+https://example.test/mergerail.git@{REVISION}",
            ],
            update._trusted_cwd(),
        )
    ]
    trusted = update._trusted_cwd()
    assert trusted.is_dir()
    assert not trusted.is_relative_to(Path(update.__file__).resolve().parent)


def test_every_install_path_uses_the_same_global_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = candidate()
    install_lock = tmp_path / "global-install.lock"
    monkeypatch.setenv("MERGERAIL_UPDATE_LOCK", str(install_lock))
    monkeypatch.setenv("MERGERAIL_UPDATE_STATE", str(tmp_path / "state" / "update.json"))
    monkeypatch.setattr(update, "_discover_release", lambda repository=None: found)
    monkeypatch.setattr(update, "_read_state", lambda: {})
    monkeypatch.setattr(update, "_write_state_unlocked", lambda *args, **kwargs: None)
    entered: list[Path] = []
    active: list[Path] = []
    installed: list[str] = []

    @contextmanager
    def locked(path: Path) -> Iterator[None]:
        entered.append(path)
        active.append(path)
        try:
            yield
        finally:
            active.remove(path)

    def raw_install(tag: str, repository: str, reference: str) -> None:
        del tag, repository
        assert install_lock in active
        installed.append(reference)

    monkeypatch.setattr(update, "exclusive_file", locked)
    monkeypatch.setattr(update, "_install_release_unlocked", raw_install)

    assert update.update() == 0
    assert update.auto_update() == found.tag
    assert update.install_candidate(found)

    assert installed == [found.revision, found.revision, found.revision]
    assert entered.count(install_lock) == 3


def test_global_install_lock_is_independent_of_state_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MERGERAIL_UPDATE_LOCK", raising=False)
    monkeypatch.setenv("MERGERAIL_UPDATE_STATE", str(tmp_path / "one" / "update.json"))
    install_lock = update._install_lock_path()
    first_state_lock = update._state_lock_path()

    monkeypatch.setenv("MERGERAIL_UPDATE_STATE", str(tmp_path / "two" / "update.json"))

    assert update._install_lock_path() == install_lock
    assert update._state_lock_path() != first_state_lock


def test_auto_update_checks_only_once_per_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "update.json"
    monkeypatch.setenv("MERGERAIL_UPDATE_STATE", str(state))
    monkeypatch.setattr("mergerail.update.time.time", lambda: 100_000.0)
    monkeypatch.setattr(update, "_discover_release", lambda repository=None: candidate("v0.1.0"))

    assert update.auto_update() is None
    assert json.loads(state.read_text(encoding="utf-8"))["latest"] == "v0.1.0"

    monkeypatch.setattr(
        update,
        "_discover_release",
        lambda: pytest.fail("the cached check should have been used"),
    )
    assert update.auto_update() is None


def test_auto_update_installs_a_new_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "update.json"
    monkeypatch.setenv("MERGERAIL_UPDATE_STATE", str(state))
    found = candidate()
    monkeypatch.setattr(update, "_discover_release", lambda repository=None: found)
    installed: list[update.ReleaseCandidate] = []
    monkeypatch.setattr(update, "_install_release", installed.append)

    assert update.auto_update() == "v0.2.0"
    assert installed == [found]
    assert json.loads(state.read_text(encoding="utf-8"))["installed"] == "v0.2.0"


def test_uvx_bootstrap_relaunches_an_already_installed_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "update.json"
    state.write_text(
        json.dumps(
            {"checked_at": 100_000.0, "latest": "v0.2.0", "installed": "v0.2.0"}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MERGERAIL_UPDATE_STATE", str(state))
    monkeypatch.setattr("mergerail.update.time.time", lambda: 100_001.0)
    monkeypatch.setattr(
        update,
        "latest_release",
        lambda: pytest.fail("the cached check should have been used"),
    )

    assert update.auto_update() == "v0.2.0"


def test_manual_check_does_not_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(update, "_discover_release", lambda repository=None: candidate())
    monkeypatch.setattr(
        update, "install_release", lambda tag: pytest.fail(f"installed {tag}")
    )

    assert update.update(check_only=True) == 0
    assert "v0.2.0 is available" in capsys.readouterr().out


def test_runtime_candidate_is_read_only_and_rate_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "update.json"
    monkeypatch.setenv("MERGERAIL_UPDATE_STATE", str(state))
    monkeypatch.setattr("mergerail.update.time.time", lambda: 100_000.0)
    calls: list[str] = []

    def probe(repository: str | None = None) -> update.ReleaseCandidate:
        del repository
        calls.append("probe")
        return candidate()

    monkeypatch.setattr(update, "_discover_release", probe)
    monkeypatch.setattr(
        update, "install_release", lambda tag: pytest.fail(f"runtime probe installed {tag}")
    )

    assert update.runtime_candidate() == candidate()
    assert update.runtime_candidate() is None
    assert calls == ["probe"]
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["checked_at"] == 100_000.0
    assert saved["latest_candidate"] == {
        "tag": "v0.2.0",
        "revision": REVISION,
        "repository": update.REPOSITORY,
    }
    assert saved["repository"] == update.REPOSITORY


def test_runtime_probe_uses_process_cooldown_when_state_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = candidate()
    calls: list[str] = []
    monkeypatch.setenv("MERGERAIL_UPDATE_STATE", str(tmp_path / "update.json"))
    monkeypatch.setattr("mergerail.update.time.time", lambda: 100_000.0)
    monkeypatch.setattr("mergerail.update.time.monotonic", lambda: 500.0)
    monkeypatch.setattr(update, "_runtime_cooldown", {})

    def discover(repository: str | None = None) -> update.ReleaseCandidate:
        del repository
        calls.append("discover")
        return found

    monkeypatch.setattr(update, "_discover_release", discover)
    monkeypatch.setattr(update, "_write_state", lambda *args, **kwargs: False)

    assert update.runtime_candidate() == found
    assert update.runtime_candidate() is None
    assert calls == ["discover"]


@pytest.mark.parametrize("checked_at", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_check_times_do_not_disable_updates(checked_at: float) -> None:
    assert update._last_checked(
        {"repository": update.REPOSITORY, "checked_at": checked_at},
        update.REPOSITORY,
    ) == 0.0


def test_relaunch_sentinel_is_consumed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MERGERAIL_UPDATE_RELAUNCHED", "1")
    monkeypatch.setenv("MERGERAIL_UPDATE_STATE", str(tmp_path / "update.json"))
    monkeypatch.setattr("mergerail.update.time.time", lambda: 100_000.0)
    monkeypatch.setattr(update, "_discover_release", lambda repository=None: candidate())

    assert update.auto_update() is None
    assert "MERGERAIL_UPDATE_RELAUNCHED" not in os.environ
    assert update.runtime_candidate() == candidate()


def test_two_runners_share_install_state_without_duplicate_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "update.json"
    found = candidate()
    monkeypatch.setenv("MERGERAIL_UPDATE_STATE", str(state))
    monkeypatch.setenv("MERGERAIL_UPDATE_REPOSITORY", found.repository)
    monkeypatch.setattr("mergerail.update.time.time", lambda: 100_000.0)
    monkeypatch.setattr(update, "_discover_release", lambda repository=None: found)
    installed: list[str] = []
    monkeypatch.setenv("MERGERAIL_UPDATE_LOCK", str(tmp_path / "install.lock"))
    monkeypatch.setattr(
        update,
        "_install_release_unlocked",
        lambda tag, repository, reference: installed.append(reference),
    )

    first_runner = update.runtime_candidate()
    assert first_runner == found
    assert update.install_candidate(first_runner)

    second_runner = update.runtime_candidate()
    assert second_runner == found
    assert update.install_candidate(second_runner)
    assert installed == [found.revision]


def test_installed_candidate_activates_even_if_state_persistence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = candidate()
    installed: list[str] = []
    monkeypatch.setenv("MERGERAIL_UPDATE_STATE", str(tmp_path / "update.json"))
    monkeypatch.setenv("MERGERAIL_UPDATE_LOCK", str(tmp_path / "install.lock"))
    monkeypatch.setattr(
        update,
        "_install_release_unlocked",
        lambda tag, repository, reference: installed.append(reference),
    )

    def fail_write(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise PermissionError("read-only state")

    monkeypatch.setattr(update, "_write_state_unlocked", fail_write)

    assert update.install_candidate(found)
    assert installed == [found.revision]


def test_relaunch_execs_the_installed_executable_without_a_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / ("mergerail.exe" if sys.platform == "win32" else "mergerail")
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr("mergerail.update.shutil.which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(update, "_run", lambda *args, **kwargs: completed(f"{tmp_path}\n"))
    called: list[tuple[str, list[str], dict[str, str]]] = []

    class ExecCalled(RuntimeError):
        pass

    def execve(path: str, arguments: list[str], environment: dict[str, str]) -> None:
        called.append((path, arguments, environment))
        raise ExecCalled

    monkeypatch.setattr("mergerail.update.os.execve", execve)

    with pytest.raises(ExecCalled):
        update.relaunch(["run", "--web", "--path", "/project"])

    path, arguments, environment = called[0]
    assert path == str(executable)
    assert arguments == [str(executable), "run", "--web", "--path", "/project"]
    assert environment["MERGERAIL_UPDATE_RELAUNCHED"] == "1"


def test_windows_relaunch_hands_off_to_a_child_and_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "mergerail.exe"
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr("mergerail.update.sys.platform", "win32")
    monkeypatch.setattr("mergerail.update.shutil.which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(update, "_run", lambda *args, **kwargs: completed(f"{tmp_path}\n"))
    calls: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(
        "mergerail.update.subprocess.Popen",
        lambda arguments, env: calls.append((arguments, env)),
    )
    monkeypatch.setattr(
        "mergerail.update.os.execve",
        lambda *args: pytest.fail("Windows must not use execve"),
    )

    with pytest.raises(SystemExit) as stopped:
        update.relaunch(["run", "--web"])

    assert stopped.value.code == 0
    assert calls[0][0] == [str(executable), "run", "--web"]
    assert calls[0][1]["MERGERAIL_UPDATE_RELAUNCHED"] == "1"
