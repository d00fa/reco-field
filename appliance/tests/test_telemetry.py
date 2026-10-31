from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from reco_field import telemetry
from reco_field.supervisor import Supervisor
from reco_field.telemetry import DeviceTelemetry, RecordingMetadata, _probe_json


def write(root: Path, name: str, value: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def test_device_uses_cpu_deltas_and_labeled_temperature(runtime, tmp_path, monkeypatch):
    proc, sysfs = tmp_path / "proc", tmp_path / "sys"
    write(proc, "device-tree/model", "NVIDIA Jetson Orin Nano\x00")
    write(proc, "stat", "cpu 100 0 0 100 0 0 0 0 90 0\ncpu0 1 2\ncpu1 1 2\n")
    write(proc, "meminfo", "MemTotal: 1000 kB\nMemAvailable: 400 kB\n")
    write(proc, "uptime", "1234.56 1000.0")
    write(sysfs, "class/thermal/thermal_zone0/type", "gpu-thermal")
    write(sysfs, "class/thermal/thermal_zone0/temp", "99000")
    write(sysfs, "class/thermal/thermal_zone1/type", "cpu-thermal")
    write(sysfs, "class/thermal/thermal_zone1/temp", "63500")
    monkeypatch.setattr(telemetry.shutil, "disk_usage", lambda _: SimpleNamespace(free=100, total=200))
    collector = DeviceTelemetry(runtime, proc=proc, sys=sysfs)
    collector.sample()
    first = collector.snapshot()
    assert first["cpu_percent"] is None
    write(proc, "stat", "cpu 125 0 0 175 0 0 0 0 100 0\ncpu0 1 2\ncpu1 1 2\n")
    collector.sample()
    sample = collector.snapshot()
    assert sample["cpu_percent"] == 25.0
    assert sample["cpu_cores"] == 2
    assert sample["cpu_temperature_c"] == 63.5
    assert sample["cpu_temperature_source"] == "cpu-thermal"
    assert sample["memory_used_bytes"] == 600 * 1024
    assert sample["memory_total_bytes"] == 1000 * 1024
    assert sample["uptime_seconds"] == 1234
    assert sample["model"] == "NVIDIA Jetson Orin Nano"
    assert sample["storage"] == {"state": "ready", "free_bytes": 100, "total_bytes": 200}
    assert sample["sampled_at"]
    sample["storage"]["free_bytes"] = -1
    assert collector.snapshot()["storage"]["free_bytes"] == 100


def test_missing_and_malformed_device_values_are_unavailable(runtime, tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    collector = DeviceTelemetry(runtime, proc=proc, sys=tmp_path / "sys")
    for cpu, mem, uptime in [("", "", ""), ("cpu -2 3 4 5", "MemTotal: 1 kB\nMemAvailable: 2 kB", "nan")]:
        write(proc, "stat", cpu)
        write(proc, "meminfo", mem)
        write(proc, "uptime", uptime)
        collector.sample()
        sample = collector.snapshot()
        assert all(sample[key] is None for key in ("cpu_percent", "cpu_cores", "memory_used_bytes",
                                                   "memory_total_bytes", "uptime_seconds", "cpu_temperature_c", "model"))
    monkeypatch.setattr(telemetry, "require_storage", lambda *args: (_ for _ in ()).throw(OSError("wrong SSD")))
    monkeypatch.setattr(telemetry.shutil, "disk_usage", lambda _: pytest.fail("Must not read SD usage"))
    collector.sample()
    assert collector.snapshot()["storage"] == {"state": "unavailable", "free_bytes": None, "total_bytes": None}


def test_snapshot_readers_share_one_collector_and_do_not_collect(runtime, monkeypatch):
    collector = DeviceTelemetry(runtime)
    sampled = threading.Event()
    calls = []
    def sample():
        calls.append(1)
        sampled.set()
    monkeypatch.setattr(collector, "sample", sample)
    collector.start()
    collector.start()
    assert sampled.wait(1)
    for _ in range(100):
        collector.snapshot()
    collector.stop()
    assert calls == [1]


def wait_metadata(cache, path):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = cache.get(path, path.stat())
        if result["metadata_state"] != "pending":
            return result
        time.sleep(.01)
    pytest.fail("Metadata did not finish")


def test_metadata_is_cached_and_only_changed_files_reprobe(runtime, monkeypatch):
    path = runtime.recordings_dir / "old-recording.mkv"
    path.write_bytes(b"matroska")
    calls = []
    def probe(argv):
        calls.append(argv)
        return {"streams": [{"codec_name": "h264", "width": 3072, "height": 864}], "format": {"duration": "114.8"}}
    monkeypatch.setattr(telemetry, "_probe_json", probe)
    cache = RecordingMetadata(runtime)
    try:
        first = wait_metadata(cache, path)
        assert first == {"metadata_state": "ready", "duration_seconds": 114.8, "width": 3072, "height": 864, "codec": "h264"}
        assert cache.get(path, path.stat()) == first
        assert len(calls) == 1
        assert "-count_packets" not in calls[0]
        path.write_bytes(b"changed video")
        wait_metadata(cache, path)
        assert len(calls) == 2
        assert len(cache._cache) == 1
        assert cache.get(path, path.stat(), active=True)["metadata_state"] == "recording"
        assert len(calls) == 2
    finally:
        cache.close()


def test_metadata_cache_retains_large_library_without_reprobe_loop(runtime, monkeypatch):
    cache = RecordingMetadata(runtime)
    calls = []
    monkeypatch.setattr(cache, "_probe", lambda *args: (calls.append(args), {"metadata_state": "unavailable"})[1])
    paths = []
    try:
        for index in range(270):
            path = runtime.recordings_dir / f"old-{index}.mkv"
            path.write_bytes(b"x")
            paths.append(path)
            wait_metadata(cache, path)
        for path in paths:
            assert cache.get(path, path.stat())["metadata_state"] == "unavailable"
        assert len(calls) == 270
        cache.retain({paths[-1]})
        assert len(cache._cache) == 1
    finally:
        cache.close()


def test_metadata_failure_is_cached_and_storage_guard_precedes_probe(runtime, monkeypatch):
    path = runtime.recordings_dir / "broken.mkv"
    path.write_bytes(b"bad")
    calls = []
    def failure(argv):
        calls.append(1)
        raise ValueError("Invalid media")
    monkeypatch.setattr(telemetry, "_probe_json", failure)
    cache = RecordingMetadata(runtime)
    try:
        assert wait_metadata(cache, path)["metadata_state"] == "unavailable"
        assert wait_metadata(cache, path)["duration_seconds"] is None
        assert calls == [1]
        path.write_bytes(b"changed")
        monkeypatch.setattr(telemetry, "require_storage", lambda *args: (_ for _ in ()).throw(OSError("SSD missing")))
        assert wait_metadata(cache, path)["metadata_state"] == "unavailable"
        assert calls == [1]
    finally:
        cache.close()


def test_metadata_queue_is_bounded_and_does_not_block_listing(runtime, monkeypatch):
    release = threading.Event()
    cache = RecordingMetadata(runtime)
    monkeypatch.setattr(cache, "_probe", lambda *_: (release.wait(2), {"metadata_state": "unavailable"})[1])
    try:
        for i in range(100):
            path = runtime.recordings_dir / f"{i}.mkv"
            path.write_bytes(b"x")
            assert cache.get(path, path.stat())["metadata_state"] == "pending"
        assert len(cache._cache) == 8
    finally:
        release.set()
        cache.close()


def test_probe_enforces_output_limit_and_time_limit():
    assert _probe_json([sys.executable, "-c", "print('{\"streams\":[]}')"]) == {"streams": []}
    with pytest.raises(ValueError, match="exceeded"):
        _probe_json([sys.executable, "-c", "print('x'*70000)"])
    start = time.monotonic()
    with pytest.raises(ValueError, match="unavailable"):
        _probe_json([sys.executable, "-c", "import time; time.sleep(30)"])
    assert time.monotonic() - start < 6


def test_capture_metrics_continue_after_startup_and_ignore_stale_sessions(runtime):
    supervisor = Supervisor(runtime)
    supervisor._session_id, supervisor._camera = "current", object()
    supervisor._on_camera_line("current", "Warmup complete, starting capture")
    supervisor._on_camera_line("current", "Processed 30 frames (14.9 / 29.8 fps)")
    first = supervisor._capture_metrics.copy()
    assert first["fps"] == 29.8 and first["processed_frames"] == 30
    supervisor._on_camera_line("previous", "Processed 900 frames (30.0 / 30.0 fps)")
    supervisor._on_camera_line("current", "Processed 15 frames (30.0 / 30.0 fps)")
    supervisor._on_camera_line("current", "Processed 60 frames (nan / nan fps)")
    assert supervisor._capture_metrics == first
    supervisor._on_camera_line("current", "Processed 60 frames (23.3 / 30.1 fps)")
    assert supervisor._capture_metrics["fps"] == 30.1
    supervisor._stop_requested = True
    supervisor._on_camera_line("current", "Processed 90 frames (25.0 / 30.0 fps)")
    assert supervisor._capture_metrics["processed_frames"] == 60


def test_library_preserves_old_contract_and_marks_active_metadata(runtime, monkeypatch):
    supervisor = Supervisor(runtime)
    path = runtime.recordings_dir / "existing.mkv"
    path.write_bytes(b"video")
    supervisor.record_state, supervisor.recording_path = "running", path
    monkeypatch.setattr(telemetry, "_probe_json", lambda *_: pytest.fail("Never probe active file"))
    item = supervisor.list_recordings()[0]
    assert item["name"] == path.name and item["size"] == 5 and item["modified_at"]
    assert item["download_url"] == "/api/recordings/existing.mkv"
    assert item["metadata_state"] == "recording" and item["width"] is None
    supervisor.recording_metadata.close()


def test_library_does_not_report_fallback_files_when_ssd_is_missing(runtime, monkeypatch):
    supervisor = Supervisor(runtime)
    (runtime.recordings_dir / "sd-fallback.mkv").write_bytes(b"not allowed")
    monkeypatch.setattr("reco_field.supervisor.require_storage", lambda *args: (_ for _ in ()).throw(OSError("no SSD")))
    assert supervisor.list_recordings() == []


def test_status_has_only_current_session_metrics(runtime, monkeypatch):
    supervisor = Supervisor(runtime)
    monkeypatch.setattr(supervisor, "_refresh_process_state", lambda: None)
    monkeypatch.setattr(supervisor, "_relay_running", lambda: False)
    supervisor._session_id = "new"
    supervisor.record_state = "running"
    supervisor._capture_metrics = {"session_id": "old", "processed_frames": 300, "fps": 30.0, "sampled_at": "old"}
    assert supervisor.status()["capture"]["metrics"] is None
    supervisor._capture_metrics["session_id"] = "new"
    assert supervisor.status()["capture"]["metrics"]["processed_frames"] == 300
    supervisor.record_state = "idle"
    assert supervisor.status()["capture"]["metrics"] is None
