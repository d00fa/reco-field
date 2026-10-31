import json
import threading
from pathlib import Path

import pytest

from conftest import session_headers, valid_calibration
from reco_field.storage import atomic_write_json
from reco_field.supervisor import Supervisor, SupervisorError


def candidate(supervisor, side="left"):
    folder = supervisor.lens_calibration._folder(side)
    folder.mkdir(parents=True)
    capture = supervisor._lens_capture_config(side)
    profile = dict(valid_calibration()["left_uniforms"], fx=1810.0)
    atomic_write_json(folder / "views.json", {"capture": capture, "revision": "measurement", "views": [{}] * 16})
    atomic_write_json(folder / "candidate.json", {"capture": capture, "revision": "measurement", "profile": profile,
        "quality": {"training_rms_px": .3, "holdout_rms_px": .4, "holdout_max_view_rms_px": .7}})
    return folder, profile


def test_apply_backs_up_profile_and_invalidates_alignment(runtime):
    supervisor = Supervisor(runtime)
    old_alignment = runtime.calibration_file.read_bytes()
    old_lens = valid_calibration()["left_uniforms"]
    profile_path = runtime.data_dir / "config" / "left-lens.json"
    atomic_write_json(profile_path, old_lens)
    _, profile = candidate(supervisor)
    status = supervisor.lens_action("left", "apply")
    assert json.loads(profile_path.read_text()) == {**profile, "capture_identity": supervisor._lens_capture_config("left")}
    assert not runtime.calibration_file.exists()
    assert status["calibration"]["valid"] is False
    backups = runtime.data_dir / "config" / "lens-backups"
    assert json.loads(next(backups.glob("*-left-lens.json")).read_text()) == old_lens
    assert next(backups.glob("*-alignment.json")).read_bytes() == old_alignment
    with pytest.raises(SupervisorError, match="calibration"):
        supervisor.start_recording()


@pytest.mark.parametrize("change", ["mode", "quality", "stale"])
def test_invalid_candidate_cannot_change_working_configuration(runtime, change):
    supervisor = Supervisor(runtime)
    original = runtime.calibration_file.read_bytes()
    folder, _ = candidate(supervisor)
    data = json.loads((folder / "candidate.json").read_text())
    if change == "mode":
        data["capture"]["sensor_mode"] = 0
    elif change == "quality":
        data["quality"]["holdout_rms_px"] = 8
    else:
        data["revision"] = "older"
    atomic_write_json(folder / "candidate.json", data)
    with pytest.raises(SupervisorError):
        supervisor.lens_action("left", "apply")
    assert runtime.calibration_file.read_bytes() == original
    assert not (runtime.data_dir / "config" / "left-lens.json").exists()


@pytest.mark.parametrize("change", ["mode", "devices", "dimensions"])
def test_measured_identity_is_enforced_after_apply_and_alignment(runtime, change):
    supervisor = Supervisor(runtime)
    alignment = valid_calibration()
    for side in ("left", "right"):
        candidate(supervisor, side)
        supervisor.lens_action(side, "apply")
        saved = json.loads((runtime.data_dir / "config" / f"{side}-lens.json").read_text())
        saved.pop("capture_identity")  # Native alignment output need not preserve optional metadata.
        alignment[f"{side}_uniforms"] = saved
    atomic_write_json(runtime.calibration_file, alignment)
    assert supervisor.calibration_status()["valid"]
    camera = supervisor.settings.camera
    if change == "mode":
        camera.sensor_mode = 0
    elif change == "devices":
        camera.left_device, camera.right_device = camera.right_device, camera.left_device
    else:
        camera.capture_width, camera.capture_height = 1920, 1080
    status = supervisor.calibration_status()
    assert not status["valid"]
    assert "measured for sensor" in status["error"]
    for action in (supervisor.start_recording, supervisor.start_calibration):
        with pytest.raises(SupervisorError, match="matching left lens profile") as error:
            action()
        assert error.value.status_code == 409


def test_measured_left_profile_cannot_substitute_for_right_sensor(runtime):
    supervisor = Supervisor(runtime)
    _, profile = candidate(supervisor)
    supervisor.lens_action("left", "apply")
    alignment = valid_calibration()
    alignment["left_uniforms"] = profile
    alignment["right_uniforms"] = dict(profile)
    atomic_write_json(runtime.calibration_file, alignment)
    status = supervisor.calibration_status()
    assert not status["valid"]
    assert "matching right lens profile" in status["error"]
    with pytest.raises(SupervisorError, match="matching right lens profile"):
        supervisor.start_calibration()


def test_legacy_profile_keeps_shared_fallback_and_aspect_scaling(runtime):
    supervisor = Supervisor(runtime)
    legacy = valid_calibration()["left_uniforms"]
    supervisor.import_calibration(json.dumps(legacy).encode(), "left-lens")
    assert supervisor._validated_lens_profile("right") == legacy
    supervisor.settings.camera.capture_width = 1920
    supervisor.settings.camera.capture_height = 1080
    supervisor.settings.camera.sensor_mode = 0
    assert supervisor.calibration_status()["valid"]


@pytest.mark.parametrize("identity", [None, [], {},
    {"device": "0", "sensor_mode": True, "width": 3840, "height": 2160},
    {"device": "256", "sensor_mode": 1, "width": 3840, "height": 2160},
    {"device": 0, "sensor_mode": 1, "width": 3840, "height": 2160},
    {"device": "0", "sensor_mode": 256, "width": 3840, "height": 2160},
    {"device": "0", "sensor_mode": 1, "width": 1920, "height": 2160},
    {"device": "0", "sensor_mode": 1, "width": 3840, "height": 2160, "extra": 1}])
def test_import_rejects_malformed_capture_identity(client, runtime, identity):
    headers = session_headers(client)
    profile = dict(valid_calibration()["left_uniforms"], capture_identity=identity)
    response = client.post("/api/calibration/import?kind=left-lens", content=json.dumps(profile), headers=headers)
    assert response.status_code == 422
    assert "capture_identity" in response.text
    assert not (runtime.data_dir / "config" / "left-lens.json").exists()


def test_camera_actions_and_settings_are_locked_during_lens_work(runtime):
    supervisor = Supervisor(runtime)
    supervisor.lens_calibration.state = "running"
    actions = [supervisor.start_recording, supervisor.start_calibration, supervisor.start_snapshots,
        lambda: supervisor.save_settings(supervisor.get_settings()),
        lambda: supervisor.import_calibration(runtime.calibration_file.read_bytes(), "calibration")]
    for action in actions:
        with pytest.raises(SupervisorError) as error:
            action()
        assert error.value.status_code == 409
    supervisor.stop_lens_calibration()
    assert not supervisor.lens_calibration.active


def test_lens_routes_require_csrf_and_reject_busy_camera(client):
    headers = session_headers(client)
    assert client.post("/api/lenses/left/capture").status_code == 403
    assert client.post("/api/lenses/left/capture", headers={**headers, "Origin": "https://other.test"}).status_code == 403
    supervisor = client.app.state.supervisor
    supervisor.record_state = "running"
    assert client.post("/api/lenses/left/capture", headers=headers).status_code == 409
    supervisor.record_state = "idle"
    assert client.post("/api/lenses/left/solve", headers=headers).status_code == 422
    assert client.post("/api/lenses/unknown/reset", headers=headers).status_code == 422
    assert client.get("/api/lenses/left/preview.jpg").status_code == 404


def test_missing_ssd_refuses_lens_capture_and_board_without_writes(runtime, monkeypatch):
    supervisor = Supervisor(runtime)
    def missing(*args):
        raise OSError("Recording SSD is not mounted")
    monkeypatch.setattr("reco_field.lens_calibration.require_storage", missing)
    with pytest.raises(SupervisorError, match="not mounted"):
        supervisor.lens_action("left", "capture")
    with pytest.raises(OSError, match="not mounted"):
        supervisor.lens_calibration.board_file()
    assert not (runtime.data_dir / "config" / "lens-work").exists()


def test_full_resolution_capture_preserves_mode_and_bounds_queued_frames(runtime):
    supervisor = Supervisor(runtime)
    argv = supervisor._snapshot_argv("0", runtime.temp_dir / "frame-%03d.jpg", full_resolution=True)
    assert "sensor-mode=1" in argv
    assert "video/x-raw,format=I420,width=3840,height=2160" in argv
    assert "max-size-buffers=1" in argv and "leaky=downstream" in argv
    assert "quality=95" in argv and "max-files=1" in argv


def test_cancel_waits_for_capture_and_keeps_existing_profiles(runtime):
    started, released = threading.Event(), threading.Event()
    class Process:
        def __init__(self, argv, *args, **kwargs):
            pass
        def start(self):
            started.set()
        def wait(self, timeout):
            assert released.wait(3)
            return 0
        def stop(self, grace_seconds):
            released.set()
    supervisor = Supervisor(runtime, process_factory=Process)
    original = runtime.calibration_file.read_bytes()
    supervisor.lens_action("left", "capture")
    assert started.wait(2)
    supervisor.stop_lens_calibration()
    assert not supervisor.lens_calibration.active
    assert runtime.calibration_file.read_bytes() == original
    assert not supervisor.lens_calibration._folder("left").joinpath("candidate.json").exists()


def test_duplicate_capture_is_rejected_without_growing_dataset(runtime):
    observation = {"size": [3840, 2160], "ids": list(range(24)),
        "points": [[100 + i * 10, 200 + i * 5] for i in range(24)],
        "descriptor": [.5, .5, .2, .1, .1]}
    class Process:
        def __init__(self, argv, *args, **kwargs):
            self.argv = argv
        def start(self):
            if "nvarguscamerasrc" in self.argv:
                location = next(item[9:] for item in self.argv if item.startswith("location="))
                Path(location.replace("%03d", "029")).write_bytes(b"jpeg")
            else:
                Path(self.argv[-1]).write_text(json.dumps(observation))
        def wait(self, timeout):
            return 0
        def stop(self, grace_seconds):
            return 0
    supervisor = Supervisor(runtime, process_factory=Process)
    for _ in range(2):
        supervisor.lens_action("left", "capture")
        supervisor.lens_calibration._thread.join(3)
    status = supervisor.lens_calibration.status()
    assert status["lenses"]["left"]["views"] == 1
    assert "similar" in status["error"]
    assert not list(supervisor.lens_calibration._folder("left").glob(".capture-*.jpg"))
