from __future__ import annotations

import sys
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from reco_field.processes import ManagedProcess, redact_text, url_secret_fragments
from reco_field import processes as processes_module


@pytest.mark.parametrize("finish", ["wait", "stop"])
def test_finished_process_drains_delayed_output_and_callbacks(tmp_path, monkeypatch, finish):
    delivered = []
    process = ManagedProcess(
        [sys.executable, "-c", "print('Error: stream private-stream-key failed'); raise SystemExit(1)"],
        tmp_path / "failure.log", secrets_to_hide=["private-stream-key"], on_line=delivered.append,
    )
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    read_output = process._read_output

    def delayed_reader():
        entered.set()
        assert release.wait(3)
        read_output()

    monkeypatch.setattr(process, "_read_output", delayed_reader)
    process.start()
    result = []
    waiter = threading.Thread(target=lambda: (result.append(getattr(process, finish)()), finished.set()))
    try:
        assert entered.wait(2)
        assert process.process.wait(timeout=3) == 1
        waiter.start()
        assert not finished.wait(0.05)
        release.set()
        assert finished.wait(3)
        assert result == [1]
        assert delivered == ["Error: stream <redacted> failed"]
        text = process.log.path.read_text()
        assert "Error: stream <redacted> failed" in text
        assert "private-stream-key" not in text
    finally:
        release.set()
        if waiter.ident:
            waiter.join(timeout=3)
        process.stop(grace_seconds=0.5)


@pytest.mark.parametrize("timeout,expected", [(None, 2.0), (0.2, 0.05), (0.0, 0.0)])
def test_output_drain_is_bounded_by_wait_timeout(tmp_path, monkeypatch, timeout, expected):
    process = ManagedProcess([], tmp_path / "wait.log")
    process.process = SimpleNamespace(wait=lambda timeout: 1)
    joins = []
    process._reader = SimpleNamespace(join=lambda timeout: joins.append(timeout))
    ticks = iter([10.0, 10.15])
    monkeypatch.setattr(processes_module.time, "monotonic", lambda: next(ticks))
    assert process.wait(timeout=timeout) == 1
    assert joins == pytest.approx([expected])


def test_wait_does_not_join_its_own_output_callback(tmp_path):
    process = ManagedProcess([], tmp_path / "wait.log")
    process.process = SimpleNamespace(wait=lambda timeout: 1)
    process._reader = threading.current_thread()
    assert process.wait() == 1


def test_wait_timeout_does_not_wait_for_reader(tmp_path):
    process = ManagedProcess([], tmp_path / "wait.log")
    def timeout(timeout):
        raise subprocess.TimeoutExpired("reco", timeout)
    process.process = SimpleNamespace(wait=timeout)
    process._reader = SimpleNamespace(join=lambda timeout: pytest.fail("child has not exited"))
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(timeout=0.01)


def test_actual_subprocess_lifecycle_and_bounded_log(tmp_path: Path):
    process = ManagedProcess(
        [
            sys.executable,
            "-c",
            "import time; print('process-ready', flush=True); time.sleep(30)",
        ],
        tmp_path / "process.log",
        log_max_bytes=128,
        log_backups=2,
    )
    process.start()
    assert process.is_running()
    process.stop(grace_seconds=0.5)
    assert process.poll() is not None
    assert (tmp_path / "process.log").exists()


def test_secret_redaction():
    secret = "rtmp://example.test/live2/private-stream-key"
    text = f"connecting {secret} with key private-stream-key"
    redacted = redact_text(text, [secret, "private-stream-key"])
    assert "private-stream-key" not in redacted
    assert "<redacted" in redacted


def test_url_fragments_redact_standalone_keys():
    url = "srt://example.test:9000?streamid=private-stream-key"
    fragments = url_secret_fragments(url)
    assert "private-stream-key" in fragments
    assert "private-stream-key" not in redact_text(
        "streamid=private-stream-key", fragments
    )
