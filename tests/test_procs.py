from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from agentq import procs


def test_kill_tree_ends_a_spawned_process(tmp_path: Path) -> None:
    process = procs.spawn(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    procs.kill_tree(process)
    assert process.wait(10) != 0


def test_kill_tree_reaches_the_grandchild(tmp_path: Path) -> None:
    """The whole point: killing the wrapper alone would orphan the real work."""
    marker = tmp_path / "survived"
    grandchild = (
        "import time\n"
        f"time.sleep(3)\n"
        f"open({str(marker)!r}, 'w').close()\n"
    )
    script = tmp_path / "child.py"
    script.write_text(grandchild, encoding="utf-8")
    parent = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(script)!r}])\n"
        "time.sleep(60)\n"
    )
    process = procs.spawn(
        [sys.executable, "-c", parent],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    import time

    time.sleep(0.8)  # let the grandchild exist
    procs.kill_tree(process)
    process.wait(10)
    time.sleep(3.5)  # past the grandchild's sleep — it must be dead, not late
    assert not marker.exists()


def test_dead_processes_are_left_alone(tmp_path: Path) -> None:
    process = procs.spawn(
        [sys.executable, "-c", "pass"],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    process.wait(10)
    procs.kill_tree(process)  # must not raise
    procs.terminate_tree(process)
