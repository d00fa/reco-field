from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest


FFMPEG = os.environ.get("RECO_FIELD_TEST_FFMPEG")
MEDIAMTX = os.environ.get("RECO_FIELD_TEST_MEDIAMTX")


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


@pytest.mark.skipif(
    not FFMPEG or not MEDIAMTX, reason="real media tools not configured"
)
def test_ffmpeg_publishes_and_copies_through_mediamtx(tmp_path: Path):
    config = Path(__file__).parents[1] / "reco_field" / "mediamtx.yml"
    relay = subprocess.Popen(
        [MEDIAMTX, str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    publisher = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not _port_open(1935):
            time.sleep(0.2)
        assert _port_open(1935), "MediaMTX RTMP listener did not start"

        publisher = subprocess.Popen(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=640x360:rate=30",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-t",
                "7",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-c:a",
                "aac",
                "-f",
                "flv",
                "rtmp://127.0.0.1:1935/reco/live",
            ]
        )
        output = tmp_path / "copy.mkv"
        deadline = time.monotonic() + 8
        while True:
            consumer = subprocess.run(
                [
                    FFMPEG,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    "rtmp://127.0.0.1:1935/reco/live",
                    "-t",
                    "2",
                    "-c",
                    "copy",
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if consumer.returncode == 0 or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        assert consumer.returncode == 0, consumer.stderr
        assert output.stat().st_size > 0
    finally:
        if publisher:
            publisher.terminate()
            try:
                publisher.wait(timeout=3)
            except subprocess.TimeoutExpired:
                publisher.kill()
        relay.terminate()
        try:
            relay.wait(timeout=3)
        except subprocess.TimeoutExpired:
            relay.kill()
