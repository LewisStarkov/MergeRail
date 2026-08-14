"""Temporary public access to the local web front."""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Protocol

from . import log
from .fronts.base import Front
from .fronts.web import LOCAL_HOSTS, WebFront
from .procs import kill_tree, spawn, terminate_tree


class Share(Protocol):
    """A public endpoint whose lifetime follows the front."""

    def start(self, front: Front) -> str: ...

    def stop(self) -> None: ...


class NgrokTunnel:
    """Run the ngrok agent and discover the HTTPS endpoint it creates."""

    def __init__(
        self,
        root: Path,
        state_dir: Path,
        *,
        policy: Path | None = None,
        unsafe: bool = False,
        startup_timeout: float = 15.0,
    ) -> None:
        self.root = root
        self.state_dir = state_dir
        self.policy = policy
        self.unsafe = unsafe
        self.startup_timeout = startup_timeout
        self.process: subprocess.Popen[str] | None = None
        self.url = ""
        self.username = ""
        self.password = ""
        self._name = f"mergerail-{secrets.token_hex(4)}"
        self._log_path = state_dir / "ngrok.log"

    def start(self, front: Front) -> str:
        if not isinstance(front, WebFront):
            raise SystemExit("mergerail: ngrok sharing requires the web front")
        if front.host not in LOCAL_HOSTS:
            raise SystemExit("mergerail: ngrok sharing requires the web front to bind to localhost")
        binary = shutil.which("ngrok")
        if not binary:
            raise SystemExit(
                "mergerail: ngrok is not installed; install it from https://ngrok.com/download"
            )
        if self.policy is not None and not self.policy.is_file():
            raise SystemExit(f"mergerail: ngrok traffic policy not found: {self.policy}")

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._log_path.unlink(missing_ok=True)
        command = [
            binary,
            "http",
            self._target(front),
            "--name",
            self._name,
            "--log",
            str(self._log_path),
            "--log-format",
            "json",
        ]
        if self.policy is not None:
            command.extend(["--traffic-policy-file", str(self.policy)])
        elif not self.unsafe:
            self.username = "mergerail"
            self.password = secrets.token_urlsafe(12)
            front.enable_auth(self.username, self.password)

        try:
            self.process = spawn(
                command,
                cwd=self.root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            raise SystemExit(f"mergerail: cannot start ngrok — {exc}") from exc
        try:
            self.url = self._wait_for_url()
        except BaseException:
            self.stop()
            raise

        log.info("ngrok.listening", url=self.url)
        if self.username:
            log.info(
                "ngrok.login",
                username=self.username,
                password=self.password,
                note="generated for this session",
            )
        elif self.unsafe:
            log.warn("ngrok.unprotected", note="anyone with the URL can control MergeRail")
        return self.url

    def stop(self) -> None:
        process, self.process = self.process, None
        self.url = ""
        if process is None:
            return
        terminate_tree(process)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            kill_tree(process)
            process.wait(timeout=3)
        log.info("ngrok.stopped")

    def _wait_for_url(self) -> str:
        assert self.process is not None
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            code = self.process.poll()
            if code is not None:
                detail = self._log_tail()
                suffix = f" — {detail}" if detail else ""
                raise SystemExit(f"mergerail: ngrok exited during startup ({code}){suffix}")
            url = self._public_url()
            if url:
                return url
            time.sleep(0.1)
        detail = self._log_tail()
        suffix = f" — {detail}" if detail else ""
        raise SystemExit(
            "mergerail: ngrok did not publish a URL within "
            f"{self.startup_timeout:g} seconds{suffix}"
        )

    def _public_url(self) -> str:
        try:
            lines = self._log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("name") != self._name:
                continue
            url = record.get("url")
            if isinstance(url, str) and url.startswith("https://"):
                return url
        return ""

    def _log_tail(self) -> str:
        try:
            lines = self._log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        for line in reversed(lines[-20:]):
            try:
                record = json.loads(line)
            except ValueError:
                return log.clip(line, 240)
            message = record.get("err") or record.get("msg")
            if message:
                return log.clip(message, 240)
        return ""

    @staticmethod
    def _target(front: WebFront) -> str:
        host = f"[{front.host}]" if ":" in front.host else front.host
        return f"http://{host}:{front.port}"


__all__ = ["NgrokTunnel", "Share"]
