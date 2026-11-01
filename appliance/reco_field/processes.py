from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


URL_PATTERN = re.compile(r"(?:rtmps?|srt)://[^\s\"']+", re.IGNORECASE)


def redact_text(text: str, secrets_to_hide: Sequence[str]) -> str:
    for secret in sorted((value for value in secrets_to_hide if value), key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return URL_PATTERN.sub("<redacted-url>", text)


def url_secret_fragments(url: str) -> tuple[str, ...]:
    parsed = urlsplit(url)
    fragments = {url}
    if parsed.password:
        fragments.add(parsed.password)
    fragments.update(part for part in parsed.path.split("/") if len(part) >= 8)
    fragments.update(
        value for _, value in parse_qsl(parsed.query) if len(value) >= 8
    )
    return tuple(fragments)


class BoundedLog:
    def __init__(self, path: Path, max_bytes: int, backups: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, text: str) -> None:
        with self._lock:
            self._rotate_if_needed()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(text)
                if not text.endswith("\n"):
                    handle.write("\n")

    def _rotate_if_needed(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size < self.max_bytes:
            return
        for index in range(self.backups - 1, 0, -1):
            source = self.path.with_suffix(self.path.suffix + f".{index}")
            target = self.path.with_suffix(self.path.suffix + f".{index + 1}")
            if source.exists():
                os.replace(source, target)
        os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))


class ManagedProcess:
    def __init__(
        self,
        argv: Sequence[str],
        log_path: Path,
        *,
        secrets_to_hide: Sequence[str] = (),
        on_line: Callable[[str], None] | None = None,
        env: dict[str, str] | None = None,
        log_max_bytes: int = 1_048_576,
        log_backups: int = 3,
    ) -> None:
        self.argv = list(argv)
        self.log = BoundedLog(log_path, log_max_bytes, log_backups)
        self.secrets_to_hide = tuple(secrets_to_hide)
        self.on_line = on_line
        self.env = env
        self.process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        if self.process is not None:
            raise RuntimeError("process already started")
        kwargs: dict[str, object] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
            "env": self.env,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self.log.write(f"$ {redact_text(' '.join(self.argv), self.secrets_to_hide)}")
        self.process = subprocess.Popen(self.argv, **kwargs)
        self._reader = threading.Thread(target=self._read_output, daemon=True)
        self._reader.start()

    def _read_output(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            safe = redact_text(line.rstrip(), self.secrets_to_hide)
            self.log.write(safe)
            if self.on_line:
                self.on_line(safe)

    def poll(self) -> int | None:
        return self.process.poll() if self.process else None

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def wait(self, timeout: float | None = None) -> int:
        if self.process is None:
            raise RuntimeError("process not started")
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        code = self.process.wait(timeout=timeout)
        if self._reader and self._reader is not threading.current_thread():
            # A child or a blocked callback may retain the pipe after the process exits.
            remaining = 2.0 if deadline is None else min(2.0, max(0.0, deadline - time.monotonic()))
            self._reader.join(timeout=remaining)
        return code

    def stop(self, grace_seconds: float = 10.0) -> int:
        process = self.process
        if process is None:
            return 0
        if process.poll() is not None:
            return self.wait()
        self._signal_group(process, signal.SIGINT if os.name == "posix" else signal.CTRL_BREAK_EVENT)
        try:
            return self.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            self._signal_group(process, signal.SIGTERM)
            try:
                return self.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._signal_group(process, signal.SIGKILL)
                return self.wait(timeout=3)

    @staticmethod
    def _signal_group(process: subprocess.Popen[str], sig: signal.Signals) -> None:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), sig)
            elif sig == signal.CTRL_BREAK_EVENT:
                process.send_signal(sig)
            else:
                process.kill()
        except (ProcessLookupError, OSError):
            pass
