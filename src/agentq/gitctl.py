"""git, and the one worktree the agents actually work in.

**The worktree is reused, and that is the whole economy of this thing.** A
directory per task means a new ``cwd``, and an agent session belongs to its
``cwd`` — so every task re-reads the codebase from nothing, at eighty-odd turns
and several million tokens, and rebuilds its dependencies before it can run a
single test. One directory means the agent walks into task #2 already knowing
where things are, exactly like a person who has been in this repository all
afternoon. Ignored files stay put too, which is why ``git clean`` here runs
without ``-x``.

Tasks are serial, so nothing is lost by sharing the directory — and a failed
task's work is not lost either: its commits live on the ``agentq/<id>`` branch,
which outlives the reset.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import log


class GitError(RuntimeError):
    pass


def git(*args: str, cwd: Path, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def is_repo(path: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True
    )
    return result.returncode == 0


def repo_root(path: Path) -> Path:
    """The top of the working tree ``path`` belongs to."""
    return Path(git("rev-parse", "--show-toplevel", cwd=path))


def detect_base_branch(root: Path) -> str:
    """``main`` if it exists, else ``master``, else whatever is checked out."""
    for name in ("main", "master"):
        found = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
            cwd=root,
            capture_output=True,
        )
        if found.returncode == 0:
            return name
    return current_branch(root) or "main"


def current_branch(root: Path) -> str:
    """The checked-out branch, or empty when detached.

    ``--show-current`` rather than ``rev-parse``: a repository with no commits
    yet has a branch but no ``HEAD`` to resolve, and answering "main" there is
    more useful than raising at the top of every command.
    """
    return git("branch", "--show-current", cwd=root, check=False)


def has_commits(root: Path) -> bool:
    return bool(git("rev-parse", "--verify", "--quiet", "HEAD", cwd=root, check=False))


def remote_url(root: Path) -> str:
    """``origin``'s URL, or empty — a repository with no remote cannot open PRs."""
    return git("remote", "get-url", "origin", cwd=root, check=False)


class Worktree:
    """One shared checkout, reset onto a fresh branch before each task."""

    def __init__(self, root: Path, path: Path) -> None:
        self.root = root
        self.path = path

    def reset(self, branch: str, base: str) -> None:
        """Put the worktree on a fresh ``branch`` cut from ``base``."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not (self.path / ".git").exists():
            git("worktree", "prune", cwd=self.root)
            git("worktree", "add", "--detach", str(self.path), base, cwd=self.root)
        # Let go of the previous task's branch *before* resetting. A `reset
        # --hard` while that branch is still checked out moves the branch
        # itself, which would quietly delete the work of the task that just
        # failed — the one case where the branch is the only copy left.
        git("checkout", "--detach", cwd=self.path, check=False)
        git("reset", "--hard", base, cwd=self.path)
        # Without ``-x``: the ignored files are the virtualenv and the caches,
        # and rebuilding those every task is minutes of wall clock and the
        # reason a first test run is so expensive.
        git("clean", "-fd", cwd=self.path)
        git("checkout", "-B", branch, base, cwd=self.path)
        log.info("worktree.ready", path=self.path, branch=branch)

    def detach(self, base: str) -> None:
        """Let go of the branch, so the repository can delete it."""
        git("checkout", "--detach", base, cwd=self.path, check=False)

    def head(self) -> str:
        return git("rev-parse", "HEAD", cwd=self.path)

    def is_dirty(self) -> bool:
        return bool(git("status", "--porcelain", cwd=self.path))

    def commit_all(self, message: str) -> None:
        git("add", "-A", cwd=self.path)
        git("commit", "-m", message, cwd=self.path)


__all__ = [
    "GitError",
    "Worktree",
    "current_branch",
    "detect_base_branch",
    "git",
    "has_commits",
    "is_repo",
    "remote_url",
    "repo_root",
]
