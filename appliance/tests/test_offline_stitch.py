from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import session_headers
from reco_field.capture_sessions import source_session
from reco_field.supervisor import Supervisor, SupervisorError
from test_capture_sessions import SESSION_ID, make_session, manifest


SYNC = {"output_frames": 30, "left_duplicates": 0, "right_duplicates": 1, "left_skipped": 0, "right_skipped": 0}


def ready_session(runtime):
    directory = make_session(runtime)
    value = manifest()
    value.update(calibration="calibration.json", clock_comparable=True, controls={"sensor_mode": 1})
    value["capture"].update(width=3840, height=2160, left_device="0", right_device="1")
    value["source"].update(width=1920, height=1080)
    (directory / "manifest.json").write_text(json.dumps(value))
    (directory / "calibration.json").write_bytes(runtime.calibration_file.read_bytes())
    return directory


class StitchProcess:
    instances = []

    def __init__(self, argv, log_path, *, on_line, **kwargs):
        self.argv, self.on_line = argv, on_line
        self.env = kwargs.get("env")
        self.log = SimpleNamespace(path=log_path)
        self.output = Path(argv[argv.index("-o") + 1])
        self.code, self.started, self.stop_fails = None, False, False
        self.done = threading.Event()
        self.instances.append(self)

    def start(self):
        self.started = True
        self.output.write_bytes(b"verified export")
        self.on_line("Processed 20 frames (10.0 / 12.0 fps)")
        self.on_line("Source sync: " + json.dumps(SYNC))

    def finish(self, code=0):
        self.code = code
        self.done.set()

    def poll(self):
        return self.code

    def wait(self):
        assert self.done.wait(3)
        return self.code

    def is_running(self):
        return self.started and self.code is None

    def stop(self, grace_seconds):
        if self.stop_fails:
            raise OSError("Could not stop process")
        self.finish(130)
        return self.code


@pytest.fixture
def supervisor(runtime, monkeypatch):
    ready_session(runtime)
    StitchProcess.instances = []
    value = Supervisor(runtime, process_factory=StitchProcess)
    value._capabilities = {"preview": True, "recording_modes": ["panorama", "sources"], "offline_stitch": True}
    monkeypatch.setattr(value, "_relay_running", lambda: False)
    value.offline_stitch.probe = lambda path, stat: {"metadata_state": "ready", "width": 1920,
        "height": 1080, "codec": "h264", "duration_seconds": 1.0}
    yield value
    for process in StitchProcess.instances:
        process.stop_fails = False
    value.shutdown()


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    pytest.fail("Operation did not reach expected state")


def start(supervisor):
    supervisor.start_offline_stitch(SESSION_ID)
    wait_for(lambda: bool(StitchProcess.instances) and StitchProcess.instances[-1].started)
    return StitchProcess.instances[-1]


def hashes(directory):
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in ("left.mkv", "right.mkv", "calibration.json", "manifest.json")}


def test_export_uses_saved_files_real_progress_and_validates_completion(supervisor, runtime):
    directory = runtime.recordings_dir / SESSION_ID
    before = hashes(directory)
    assert supervisor.list_sessions()[0]["can_stitch"]
    process = start(supervisor)
    assert process.argv[1:4] == ["stitch", str(directory / "left.mkv"), str(directory / "right.mkv")]
    assert process.argv[process.argv.index("-c") + 1] == str(directory / "calibration.json")
    assert process.argv[process.argv.index("--source-manifest") + 1] == str(directory / "manifest.json")
    assert process.argv[process.argv.index("--container") + 1] == "mkv"
    assert process.argv[process.argv.index("--projection") + 1] == "panorama"
    assert process.argv[process.argv.index("--quality-value") + 1] == "80"
    assert process.argv[process.argv.index("--preset") + 1] == "veryfast"
    assert process.env["RECO_NO_HWACCEL"] == "1"
    job = supervisor.status()["offline_job"]
    assert job["processed_frames"] == 30 and job["fps"] == 12
    assert job["sync"] == SYNC
    assert not supervisor.list_sessions()[0]["can_stitch"]
    process.finish()
    wait_for(lambda: not supervisor.offline_stitch.active)
    result = supervisor.status()["offline_job"]
    assert result["state"] == "complete" and result["output"]["download_url"]
    session = supervisor.list_sessions()[0]
    export = [item for item in session["outputs"] if item["role"] == "export"][0]
    assert export["complete"] and export["duration_seconds"] == 1
    assert supervisor.session_file(SESSION_ID, export["name"]) == process.output
    assert hashes(directory) == before
    second = start(supervisor)
    assert second.output != process.output and process.output.exists()
    supervisor.cancel_offline_stitch(SESSION_ID)


def test_camera_setup_and_second_export_are_blocked_until_cancel_finishes(supervisor):
    process = start(supervisor)
    for operation in (supervisor.start_preview, supervisor.start_recording, supervisor.start_calibration,
        supervisor.start_snapshots, lambda: supervisor.lens_action("left", "capture"),
        lambda: supervisor.save_settings(supervisor.get_settings()), lambda: supervisor.start_offline_stitch(SESSION_ID)):
        with pytest.raises(SupervisorError) as error:
            operation()
        assert error.value.status_code == 409
    assert supervisor.cancel_offline_stitch(SESSION_ID)["offline_job"]["state"] == "cancelled"
    assert not supervisor.offline_stitch.active and process.output.exists()
    assert supervisor.session_file(SESSION_ID, "left.mkv").is_file()
    with pytest.raises(SupervisorError, match="not finalized"):
        supervisor.session_file(SESSION_ID, process.output.name)


def test_failed_cleanup_keeps_ownership_until_cancel_retry(supervisor):
    process = start(supervisor)
    process.stop_fails = True
    supervisor.cancel_offline_stitch(SESSION_ID)
    assert supervisor.offline_stitch.active and supervisor.offline_stitch.status()["state"] == "stopping"
    with pytest.raises(SupervisorError):
        supervisor.start_preview()
    process.stop_fails = False
    supervisor.cancel_offline_stitch(SESSION_ID)
    wait_for(lambda: not supervisor.offline_stitch.active)
    assert supervisor.offline_stitch.status()["state"] == "cancelled"


@pytest.mark.parametrize("failure", ["native", "probe", "sync"])
def test_invalid_exports_are_retained_without_download(supervisor, failure):
    process = start(supervisor)
    if failure == "probe":
        supervisor.offline_stitch.probe = lambda *_: {"metadata_state": "unavailable"}
    if failure == "sync":
        supervisor.offline_stitch._job["sync"] = None
    if failure == "native":
        process.on_line("Error: source camera timestamps have a gap exceeding 0.5 seconds")
    process.finish(1 if failure == "native" else 0)
    wait_for(lambda: not supervisor.offline_stitch.active)
    result = supervisor.offline_stitch.status()
    assert result["state"] == "failed" and not result["output"]["download_url"]
    if failure == "native":
        assert "gap exceeding" in result["error"]
    assert process.output.exists()
    with pytest.raises(SupervisorError):
        supervisor.session_file(SESSION_ID, process.output.name)
    assert supervisor.session_file(SESSION_ID, "right.mkv").is_file()


def test_export_cannot_start_while_capture_active_or_without_saved_calibration(supervisor, runtime):
    supervisor.record_state = "running"
    with pytest.raises(SupervisorError, match="Stop camera capture"):
        supervisor.start_offline_stitch(SESSION_ID)
    supervisor.record_state = "idle"
    (runtime.recordings_dir / SESSION_ID / "calibration.json").unlink()
    assert not supervisor.list_sessions()[0]["can_stitch"]
    with pytest.raises(SupervisorError):
        supervisor.start_offline_stitch(SESSION_ID)
    assert not StitchProcess.instances


def test_low_disk_stops_export_and_preserves_sources(supervisor, runtime, monkeypatch):
    before = hashes(runtime.recordings_dir / SESSION_ID)
    monkeypatch.setattr("reco_field.offline_stitch.shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    monkeypatch.setattr(supervisor, "_check_disk", lambda: None)
    process = start(supervisor)
    wait_for(lambda: not supervisor.offline_stitch.active)
    assert supervisor.offline_stitch.status()["state"] == "failed"
    assert "SSD free space" in supervisor.offline_stitch.status()["error"]
    assert hashes(runtime.recordings_dir / SESSION_ID) == before
    assert process.output.exists()


def test_changed_export_identity_is_not_downloadable(supervisor):
    process = start(supervisor)
    process.finish()
    wait_for(lambda: not supervisor.offline_stitch.active)
    process.output.write_bytes(b"different video")
    with pytest.raises(SupervisorError):
        supervisor.session_file(SESSION_ID, process.output.name)


def test_export_changed_during_probe_is_not_published(supervisor):
    process = start(supervisor)
    def probe(path, stat):
        path.write_bytes(b"a different file during validation")
        return {"metadata_state": "ready", "width": 1920, "height": 1080, "codec": "h264", "duration_seconds": 1}
    supervisor.offline_stitch.probe = probe
    process.finish()
    wait_for(lambda: not supervisor.offline_stitch.active)
    assert supervisor.offline_stitch.status()["state"] == "failed"
    assert not supervisor.offline_stitch.status()["output"]["download_url"]


def test_late_cancel_does_not_revoke_completed_export(supervisor):
    process = start(supervisor)
    process.finish()
    wait_for(lambda: not supervisor.offline_stitch.active)
    supervisor.offline_stitch._thread = SimpleNamespace(is_alive=lambda: True)
    supervisor.cancel_offline_stitch(SESSION_ID)
    assert supervisor.offline_stitch.status()["state"] == "complete"
    supervisor.offline_stitch._thread = None


def test_restart_marks_interrupted_export_and_bad_metadata_is_safe(runtime):
    directory = ready_session(runtime)
    name = "export-" + "a" * 32 + ".mkv"
    output = directory / name
    output.write_bytes(b"partial")
    metadata = {"id": output.stem, "session_id": SESSION_ID, "state": "running", "output": {"name": name}}
    output.with_suffix(".json").write_text(json.dumps(metadata))
    (runtime.data_dir / "config/offline-job.json").write_text(json.dumps({"session_id": SESSION_ID, "name": name}))
    supervisor = Supervisor(runtime)
    assert supervisor.offline_stitch.status()["state"] == "interrupted"
    assert not supervisor.offline_stitch.active and output.exists()
    metadata.update(state="complete", exit_code=0, output={"size": [], "duration_seconds": [1, 2], "width": {}, "height": "bad"})
    output.with_suffix(".json").write_text(json.dumps(metadata))
    session = source_session(runtime.recordings_dir, SESSION_ID)
    export = next(item for item in session["outputs"] if item["role"] == "export")
    assert not export["complete"] and export["duration_seconds"] is None
    with pytest.raises(SupervisorError):
        supervisor.session_file(SESSION_ID, name)
    assert supervisor.session_file(SESSION_ID, "left.mkv").exists()


def test_api_export_returns_full_status_and_requires_csrf(client, monkeypatch):
    headers = session_headers(client)
    supervisor = client.app.state.supervisor
    monkeypatch.setattr(supervisor, "start_offline_stitch", lambda _: {"capture": {}, "offline_job": {"state": "starting"}})
    monkeypatch.setattr(supervisor, "cancel_offline_stitch", lambda _: {"capture": {}, "offline_job": {"state": "cancelled"}})
    assert client.post(f"/api/sessions/{SESSION_ID}/stitch", json={}).status_code == 403
    started = client.post(f"/api/sessions/{SESSION_ID}/stitch", json={}, headers=headers)
    assert started.status_code == 200 and started.json()["offline_job"]["state"] == "starting"
    stopped = client.post(f"/api/sessions/{SESSION_ID}/stitch/cancel", json={}, headers=headers)
    assert stopped.status_code == 200 and stopped.json()["offline_job"]["state"] == "cancelled"
