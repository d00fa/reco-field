from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import session_headers
from reco_field.capture_sessions import source_session
from reco_field.config import FieldSettings
from reco_field.storage import safe_session_directory, safe_session_file
from reco_field.supervisor import Supervisor, SupervisorError


SESSION_ID = "reco-20261031-120000-abcdef123456"


def manifest(state="complete"):
    timing = {"packets": 30, "first_pts_ns": 0, "last_pts_ns": 966666667}
    return {"state": state, "error": None, "started_unix_ms": 1793448000000,
        "capture": {"width": 4032, "height": 3040, "fps": 30},
        "source": {"width": 2016, "height": 1520, "codec": "h264"},
        "left": {"file": "left.mkv", "timing": timing},
        "right": {"file": "right.mkv", "timing": dict(timing)}}


def make_session(runtime, *, state="complete", mode="sources"):
    directory = runtime.recordings_dir / SESSION_ID
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps(manifest(state)))
    (directory / "session.json").write_text(json.dumps({"mode": mode, "state": "complete"}))
    for name in ("left.mkv", "right.mkv", *(["panorama.mkv"] if mode == "both" else [])):
        (directory / name).write_bytes(b"video")
    return directory


class Camera:
    def __init__(self, argv, log_path, *, on_line, **kwargs):
        self.argv, self.on_line = argv, on_line
        self.log = SimpleNamespace(path=log_path)
        self.done = threading.Event()
        self.code = None
        self.source_dir = Path(argv[argv.index("--source-dir") + 1]) if "--source-dir" in argv else None
        self.output = Path(argv[argv.index("--output") + 1]) if "--output" in argv else None
        self.finalize_sources = True
        self.grace_seconds = None

    def start(self):
        if self.source_dir:
            # Native owns exclusive directory creation; app must not reserve it first.
            self.source_dir.mkdir()
            (self.source_dir / "manifest.json").write_text(json.dumps(manifest("recording")))
            (self.source_dir / "left.mkv").write_bytes(b"left video")
            (self.source_dir / "right.mkv").write_bytes(b"right video")
        if self.output:
            self.output.write_bytes(b"panorama")
        self.on_line("Warmup complete, starting capture")

    def poll(self):
        return self.code

    def wait(self):
        assert self.done.wait(3)
        return self.code

    def stop(self, grace_seconds):
        self.grace_seconds = grace_seconds
        if self.source_dir:
            value = manifest("complete" if self.finalize_sources else "incomplete")
            (self.source_dir / "manifest.json").write_text(json.dumps(value))
        self.code = 0
        self.done.set()
        return self.code


@pytest.fixture
def supervisor(runtime, monkeypatch):
    value = Supervisor(runtime, process_factory=Camera)
    value._capabilities = {"preview": True, "recording_modes": ["panorama", "sources"], "offline_stitch": False}
    monkeypatch.setattr(value, "_ensure_relay", lambda: None)
    monkeypatch.setattr(value, "_relay_running", lambda: False)
    yield value
    value.shutdown()


def test_old_settings_keep_panorama_and_capabilities_require_actual_flags(runtime, monkeypatch):
    assert FieldSettings.model_validate({}).recording_mode == "panorama"
    supervisor = Supervisor(runtime)
    help_text = "--no-panorama-file --stream-url"
    monkeypatch.setattr(supervisor, "_run_text", lambda _: help_text)
    supervisor.refresh_capabilities()
    assert supervisor._capabilities == {"preview": True, "recording_modes": ["panorama"], "offline_stitch": False}
    help_text += " --source-dir --source-size --source-crf --preview-fps"
    supervisor.refresh_capabilities()
    assert supervisor._capabilities["recording_modes"] == ["panorama", "sources"]
    assert "both" not in supervisor._capabilities["recording_modes"]


def test_preview_creates_no_recording_and_owns_all_camera_operations(supervisor, runtime):
    result = supervisor.start_preview()
    camera = supervisor._camera
    assert "--no-panorama-file" in camera.argv and "--output" not in camera.argv
    assert "--stream-url" in camera.argv and "--source-dir" not in camera.argv
    assert result["capture"]["kind"] == "preview" and result["capture"]["state"] == "running"
    assert result["record"]["state"] == "idle" and result["record"]["elapsed_seconds"] == 0
    assert result["record"]["recording"] is None and supervisor.recording_path is None
    for operation in (supervisor.start_recording, supervisor.start_preview, supervisor.start_calibration,
                      supervisor.start_snapshots, lambda: supervisor.lens_action("left", "capture"),
                      lambda: supervisor.save_settings(supervisor.get_settings())):
        with pytest.raises(SupervisorError) as error:
            operation()
        assert error.value.status_code == 409
    with pytest.raises(SupervisorError, match="Stop preview"):
        supervisor.stop_recording()
    stopped = supervisor.stop_preview()
    assert stopped["capture"]["state"] == "idle" and supervisor.last_recording is None
    assert supervisor.list_recordings() == [] and supervisor.list_sessions() == []
    assert not list(runtime.recordings_dir.iterdir())


def test_sources_keep_camera_files_and_light_viewfinder_and_finalize(supervisor, runtime):
    result = supervisor.start_recording(False, "sources")
    camera, directory = supervisor._camera, supervisor._source_dir
    assert directory.is_dir() and supervisor.recording_path is None
    assert camera.argv[camera.argv.index("--preview-fps") + 1] == "10"
    assert "--no-panorama-file" in camera.argv and "--output" not in camera.argv
    assert camera.argv[camera.argv.index("--source-size") + 1] == "2016x1134"
    assert camera.argv[camera.argv.index("--width") + 1] == "768"
    assert camera.argv[camera.argv.index("--height") + 1] == "432"
    assert result["capture"]["session_id"] == directory.name
    assert result["capture"]["bytes"] > 0
    assert supervisor.list_sessions()[0]["state"] == "recording"
    with pytest.raises(SupervisorError, match="finish the session"):
        supervisor.session_file(directory.name, "left.mkv")
    assert supervisor.stop_recording()["capture"]["state"] == "idle"
    assert camera.grace_seconds == 16
    session = supervisor.list_sessions()[0]
    assert session["state"] == "complete" and session["mode"] == "sources"
    assert {item["role"] for item in session["outputs"]} == {"left", "right"}
    assert all(item["complete"] and item["download_url"] for item in session["outputs"])
    assert supervisor.session_file(directory.name, "left.mkv").read_bytes() == b"left video"
    assert supervisor.list_recordings() == []
    assert not list(directory.glob("panorama*"))


def test_native_incomplete_manifest_cannot_be_completed_by_user_stop(supervisor):
    supervisor.start_recording(False, "sources")
    directory = supervisor._source_dir
    supervisor._camera.finalize_sources = False
    stopped = supervisor.stop_recording()
    assert stopped["capture"]["state"] == "failed"
    assert supervisor.last_recording["state"] == "failed"
    session = supervisor.list_sessions()[0]
    assert session["state"] == "incomplete" and all(item["download_url"] is None for item in session["outputs"])
    assert (directory / "left.mkv").exists() and (directory / "right.mkv").exists()
    with pytest.raises(SupervisorError, match="preserved"):
        supervisor.session_file(directory.name, "left.mkv")


def test_recording_after_preview_requires_finished_stop_and_preserves_legacy_path(supervisor, runtime):
    supervisor.start_preview()
    supervisor.stop_preview()
    assert supervisor.start_recording(False)["record"]["state"] == "running"
    path = supervisor.recording_path
    assert path.parent == runtime.recordings_dir and path.suffix == ".mkv"
    with pytest.raises(SupervisorError, match="recording is active"):
        supervisor.stop_preview()
    supervisor.stop_recording()
    assert supervisor.recording_file(path.name) == path.resolve()


def test_both_mode_is_withheld_but_internal_lifecycle_handles_future_capability(supervisor):
    with pytest.raises(SupervisorError, match="does not support"):
        supervisor.start_recording(False, "both")
    supervisor._capabilities["recording_modes"].append("both")
    supervisor.start_recording(False, "both")
    directory = supervisor._source_dir
    assert supervisor.recording_path == directory / "panorama.mkv"
    supervisor.stop_recording()
    session = supervisor.list_sessions()[0]
    assert session["state"] == "complete" and len(session["outputs"]) == 3


def test_restart_reports_interrupted_bundle_without_deleting_files(runtime):
    directory = make_session(runtime, state="recording")
    supervisor = Supervisor(runtime)
    item = supervisor.list_sessions()[0]
    assert item["state"] == "interrupted" and item["size"] == 10
    assert directory.exists() and (directory / "left.mkv").exists()
    with pytest.raises(SupervisorError):
        supervisor.session_file(SESSION_ID, "right.mkv")


@pytest.mark.parametrize("damage", ["missing-right", "bad-manifest", "no-app-finalization", "zero-packets", "wrong-file"])
def test_incomplete_or_invalid_session_is_never_downloadable(runtime, damage):
    directory = make_session(runtime)
    if damage == "missing-right":
        (directory / "right.mkv").unlink()
    elif damage == "bad-manifest":
        (directory / "manifest.json").write_text("not json")
    elif damage == "no-app-finalization":
        (directory / "session.json").unlink()
    else:
        value = manifest()
        if damage == "zero-packets":
            value["left"]["timing"]["packets"] = 0
        else:
            value["left"]["file"] = "../../private.mkv"
        (directory / "manifest.json").write_text(json.dumps(value))
    item = source_session(runtime.recordings_dir, SESSION_ID)
    assert item["state"] != "complete" and all(not entry["complete"] for entry in item["outputs"])


def test_source_paths_and_downloads_reject_traversal_and_unlisted_files(runtime):
    make_session(runtime)
    for name in ("../private", SESSION_ID + "/..", "C:\\secret", ".."):
        with pytest.raises(ValueError):
            safe_session_directory(runtime.recordings_dir, name)
    for name in ("../left.mkv", "manifest.json", "left.mkv/..", "../../secrets"):
        with pytest.raises(ValueError):
            safe_session_file(runtime.recordings_dir, SESSION_ID, name)


def test_missing_ssd_blocks_all_capture_before_creating_output(supervisor, runtime, monkeypatch):
    monkeypatch.setattr("reco_field.supervisor.require_storage", lambda *args: (_ for _ in ()).throw(OSError("SSD unavailable")))
    for start in (supervisor.start_preview, lambda: supervisor.start_recording(False, "sources"), supervisor.start_recording):
        with pytest.raises(SupervisorError, match="SSD unavailable"):
            start()
    assert supervisor._camera is None and not list(runtime.recordings_dir.iterdir())


def test_source_size_preserves_sensor_aspect_and_preview_settings(supervisor):
    assert supervisor._scaled_size(4032, 3040, 2016, 1520) == (2016, 1520)
    assert supervisor._scaled_size(3840, 2160, 2016, 1520) == (2016, 1134)
    camera = supervisor.settings.camera
    original = (camera.output_width, camera.output_height)
    source = supervisor._camera_argv(None, source_dir=supervisor.runtime.recordings_dir / SESSION_ID)
    normal = supervisor._camera_argv(supervisor.runtime.recordings_dir / "legacy.mkv")
    assert (camera.output_width, camera.output_height) == original
    assert "--preview-fps" not in normal
    assert normal[normal.index("--width") + 1] == str(original[0])
    assert "--source-size" in source


def test_simulator_preview_streams_directly_without_local_file(runtime):
    supervisor = Supervisor(replace(runtime, simulator=True))
    argv = supervisor._camera_argv(None)
    assert argv[-2:] == ["flv", runtime.relay_rtmp_url]
    assert not any(".mkv" in item for item in argv)
    assert supervisor._capabilities["recording_modes"] == ["panorama"]


def test_api_session_download_and_settings_contract(client, runtime):
    headers = session_headers(client)
    make_session(runtime)
    entries = client.get("/api/sessions").json()
    assert entries[0]["id"] == SESSION_ID
    assert client.get(entries[0]["outputs"][0]["download_url"]).content == b"video"
    assert client.get(f"/api/sessions/{SESSION_ID}/manifest.json").status_code == 404
    assert client.post("/api/recordings/start", json={"mode": "raw"}, headers=headers).status_code == 422
    assert client.post("/api/preview/start", json={}, headers=headers).status_code == 409
    settings = client.get("/api/settings").json()
    settings["recording_mode"] = "sources"
    saved = client.put("/api/settings", json=settings, headers=headers)
    assert saved.status_code == 200 and saved.json()["recording_mode"] == "sources"
