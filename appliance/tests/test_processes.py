from __future__ import annotations

import sys
from pathlib import Path

from reco_field.processes import ManagedProcess, redact_text, url_secret_fragments


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
