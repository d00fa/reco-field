from __future__ import annotations

import json
import threading
import time
from collections import namedtuple
from dataclasses import replace

import pytest

from reco_field.config import DestinationSettings
from reco_field.supervisor import DestinationWorker, Supervisor, SupervisorError
from reco_field import supervisor as supervisor_module


@pytest.mark.parametrize("operation,args", [
    ("start_recording", ()), ("start_preview", ()), ("start_calibration", ()),
    ("start_snapshots", ()), ("lens_action", ("left", "capture")),
    ("start_offline_stitch", ("session",)),
])
def test_upgrade_blocks_new_work_but_keeps_status_available(runtime, tmp_path, monkeypatch, operation, args):
    gate = tmp_path / "upgrading"
    gate.touch()
    monkeypatch.setattr(supervisor_module, "MAINTENANCE_FILE", gate)
    supervisor = Supervisor(runtime)
    supervisor._capabilities = {"preview": True, "recording_modes": ["panorama"], "offline_stitch": True}
    with pytest.raises(SupervisorError, match="updating") as exc:
        getattr(supervisor, operation)(*args)
    assert exc.value.status_code == 503
    assert supervisor.status()["capture"]["state"] == "idle"
    gate.unlink()
    supervisor._require_offline_idle()


def valid_calibration() -> dict:
    camera = {
        "width": 3840,
        "height": 2160,
        "fx": 1800.0,
        "fy": 1800.0,
        "cx": 1920.0,
        "cy": 1080.0,
        "d": [0.0, 0.0, 0.0, 0.0],
    }
    return {
        "left_uniforms": camera,
        "right_uniforms": dict(camera),
        "params": {
            "cameraAxisOffset": 0.24,
            "intersect": 0.54,
            "xTy": 0.0,
            "xRz": 0.0,
            "zRx": 0.0,
        },
    }


def test_camera_argv_matches_cli_and_has_typed_controls(runtime):
    supervisor = Supervisor(runtime)
    argv = supervisor._camera_argv(runtime.recordings_dir / "record.mkv")
    assert argv[0] == runtime.reco_bin
    assert argv[1] == "camera"
    assert argv[argv.index("--container") + 1] == "mkv"
    assert argv[argv.index("--encoder") + 1] == "libx264"
    assert argv[argv.index("--codec") + 1] == "h264"
    assert "--stream-url" in argv
    assert argv[argv.index("--sensor-mode") + 1] == "1"
    assert argv[argv.index("--gain-range") + 1] == "1.0,22.25"
    assert argv[argv.index("--digital-gain-range") + 1] == "1.0,1.0"


def test_snapshot_argv_uses_warmup_and_safe_jpeg_output(runtime):
    supervisor = Supervisor(runtime)
    pattern = runtime.snapshots_dir / ".left-%03d.jpg"
    argv = supervisor._snapshot_argv("0", pattern)
    assert argv[0] == "gst-launch-1.0"
    assert "num-buffers=30" in argv
    assert "jpegenc" in argv
    assert "multifilesink" in argv
    assert "max-files=1" in argv
    assert any(item == f"location={pattern}" for item in argv)


@pytest.mark.parametrize("projection", ["perspective", "panorama"])
def test_camera_argv_projection(runtime, projection):
    supervisor = Supervisor(runtime)
    supervisor.settings.camera.projection = projection
    argv = supervisor._camera_argv(runtime.recordings_dir / "record.mkv")
    assert argv[argv.index("--projection") + 1] == projection


def test_full_sensor_snapshots_keep_camera_aspect(runtime):
    supervisor = Supervisor(runtime)
    supervisor.settings.camera.capture_width = 4032
    supervisor.settings.camera.capture_height = 3040
    argv = supervisor._snapshot_argv("0", runtime.snapshots_dir / ".left-%03d.jpg")
    assert "video/x-raw,format=I420,width=960,height=724" in argv


def test_separate_lens_profiles_reach_calibration_and_invalidate_old_alignment(runtime):
    supervisor = Supervisor(runtime)
    original = runtime.calibration_file.read_bytes()
    profile = valid_calibration()["right_uniforms"]
    profile["fx"] += 10
    result = supervisor.import_calibration(json.dumps(profile).encode(), "right-lens")
    assert result["right_lens_profile"]
    assert not result["valid"]
    assert "Run geometric calibration" in result["error"]
    assert runtime.calibration_file.read_bytes() == original
    with pytest.raises(SupervisorError, match="Lens profile changed"):
        supervisor.start_recording()
    argv = supervisor._calibration_argv(runtime.temp_dir / "calibration.json")
    right = runtime.data_dir / "config" / "right-lens.json"
    assert argv[argv.index("--right-lens-profile") + 1] == str(right)
    supervisor.import_calibration(json.dumps(valid_calibration()["left_uniforms"]).encode(), "left-lens")
    argv = supervisor._calibration_argv(runtime.temp_dir / "calibration.json")
    assert "--left-lens-profile" in argv and "--right-lens-profile" in argv
    updated = valid_calibration()
    updated["right_uniforms"] = profile
    assert supervisor.import_calibration(json.dumps(updated).encode(), "calibration")["valid"]


def test_right_profile_crop_checked_before_calibration(runtime):
    supervisor = Supervisor(runtime)
    profile = valid_calibration()["right_uniforms"]
    profile.update(width=4032, height=3040)
    supervisor.import_calibration(json.dumps(profile).encode(), "right-lens")
    with pytest.raises(SupervisorError, match="matching sensor mode"):
        supervisor.start_calibration()


def test_absent_right_profile_keeps_cli_fallback(runtime):
    supervisor = Supervisor(runtime)
    argv = supervisor._calibration_argv(runtime.temp_dir / "calibration.json")
    assert "--right-lens-profile" not in argv


def test_calibration_failure_preserves_previous_file(runtime):
    original = runtime.calibration_file.read_bytes()
    supervisor = Supervisor(runtime)
    supervisor._calibration_temp = runtime.temp_dir / "failed.json"
    supervisor._finish_calibration(1, "matching failed")
    assert runtime.calibration_file.read_bytes() == original
    assert supervisor.calibration_state == "failed"


def test_mismatched_lens_crop_blocks_capture_and_reports_invalid_calibration(runtime):
    calibration = valid_calibration()
    for side in ("left_uniforms", "right_uniforms"):
        calibration[side].update(width=4032, height=3040)
    runtime.calibration_file.write_text(json.dumps(calibration))
    (runtime.data_dir / "config" / "left-lens.json").write_text(
        json.dumps(calibration["left_uniforms"])
    )
    supervisor = Supervisor(runtime)
    assert not supervisor.calibration_status()["valid"]
    assert "4032x3040" in supervisor.calibration_status()["error"]
    for start in (supervisor.start_recording, supervisor.start_calibration):
        with pytest.raises(SupervisorError, match="matching sensor mode") as error:
            start()
        assert error.value.status_code == 409
    assert not list(runtime.recordings_dir.iterdir())


def test_calibration_success_is_validated_before_replace(runtime):
    supervisor = Supervisor(runtime)
    supervisor._calibration_temp = runtime.temp_dir / "success.json"
    value = valid_calibration()
    value["params"]["intersect"] = 0.61
    supervisor._calibration_temp.write_text(json.dumps(value))
    supervisor._finish_calibration(0, "ok")
    assert (
        json.loads(runtime.calibration_file.read_text())["params"]["intersect"]
        == 0.61
    )
    assert supervisor.calibration_state == "complete"


def test_successful_calibration_preserves_optional_metadata(runtime):
    existing = valid_calibration()
    existing["rig_tilt"] = 0.22
    existing["rig_roll"] = -0.03
    existing["field_roi"] = {"points": [[0.1, 0.2], [0.8, 0.2]]}
    runtime.calibration_file.write_text(json.dumps(existing))
    supervisor = Supervisor(runtime)
    supervisor._calibration_temp = runtime.temp_dir / "metadata.json"
    replacement = valid_calibration()
    replacement["params"]["intersect"] = 0.59
    supervisor._calibration_temp.write_text(json.dumps(replacement))
    supervisor._finish_calibration(0, "ok")
    saved = json.loads(runtime.calibration_file.read_text())
    assert saved["params"]["intersect"] == 0.59
    assert saved["rig_tilt"] == 0.22
    assert saved["rig_roll"] == -0.03
    assert saved["field_roi"] == existing["field_roi"]


def test_cancelled_calibration_preserves_previous_file(runtime):
    original = runtime.calibration_file.read_bytes()
    supervisor = Supervisor(runtime)
    supervisor.calibration_state = "stopping"
    supervisor._calibration_temp = runtime.temp_dir / "cancelled.json"
    supervisor._calibration_temp.write_text(json.dumps(valid_calibration()))
    supervisor._finish_calibration(0, "cancelled")
    assert runtime.calibration_file.read_bytes() == original
    assert supervisor.calibration_state == "idle"


def test_calibration_error_includes_bounded_log_tail(runtime):
    class FailedCalibration:
        log = type(
            "Log",
            (),
            {"path": runtime.logs_dir / "calibration-failure.log"},
        )()

        def wait(self, timeout=None):
            return 1

        def stop(self, grace_seconds=5):
            return 1

    FailedCalibration.log.path.write_text(
        "\x1b[31mcalibration: no usable frame pairs (all frames failed matching)\x1b[0m\n"
    )
    supervisor = Supervisor(runtime)
    supervisor._calibration = FailedCalibration()
    supervisor._calibration_temp = runtime.temp_dir / "missing.json"
    supervisor._monitor_calibration()
    assert "no usable frame pairs" in supervisor.calibration_error
    assert "\x1b" not in supervisor.calibration_error


def test_low_disk_refusal(runtime):
    usage = namedtuple("Usage", "total used free")
    supervisor = Supervisor(runtime, disk_usage=lambda _: usage(100, 100, 1024))
    supervisor.settings = supervisor.settings.model_copy(
        update={"min_free_gb": 2.0}
    )
    with pytest.raises(SupervisorError) as exc:
        supervisor.start_recording(False)
    assert exc.value.status_code == 507


def test_streaming_requires_an_enabled_destination(runtime):
    supervisor = Supervisor(runtime, disk_usage=lambda _: type(
        "Usage", (), {"total": 10, "used": 0, "free": 10 * 1024**3}
    )())
    with pytest.raises(SupervisorError, match="no enabled destinations"):
        supervisor.start_recording(True)


def test_active_recording_cannot_be_downloaded(runtime):
    supervisor = Supervisor(runtime)
    path = runtime.recordings_dir / "active.mkv"
    path.write_bytes(b"partial")
    supervisor.record_state = "running"
    supervisor.recording_path = path
    with pytest.raises(SupervisorError, match="Stop recording"):
        supervisor.recording_file(path.name)
    supervisor.record_state = "idle"
    assert supervisor.recording_file(path.name) == path


def test_calibration_import_is_rejected_while_calibration_is_active(runtime):
    supervisor = Supervisor(runtime)
    supervisor.calibration_state = "running"
    with pytest.raises(SupervisorError, match="finish before importing"):
        supervisor.import_calibration(
            json.dumps(valid_calibration()).encode(),
            "calibration",
        )


def test_crashed_recording_blocks_restart_until_monitor_finishes(runtime):
    class ExitedCamera:
        log = type("Log", (), {"path": runtime.logs_dir / "camera-crash.log"})()

        @staticmethod
        def poll():
            return 1

    release = threading.Event()
    monitor = threading.Thread(target=release.wait)
    monitor.start()
    supervisor = Supervisor(runtime)
    supervisor._camera = ExitedCamera()
    supervisor._session_id = "old"
    supervisor._session_thread = monitor
    supervisor.record_state = "failed"
    with pytest.raises(SupervisorError, match="still finalizing"):
        supervisor.start_recording(False)
    release.set()
    monitor.join(timeout=2)


def test_stale_monitor_cannot_clear_a_new_session(runtime):
    class ExitedCamera:
        log = type("Log", (), {"path": runtime.logs_dir / "camera-old.log"})()

        @staticmethod
        def poll():
            return 1

        @staticmethod
        def wait(timeout=None):
            return 1

    supervisor = Supervisor(runtime)
    new_camera = object()
    supervisor._camera = new_camera
    supervisor._session_id = "new"
    supervisor.record_state = "running"
    recording_path = runtime.recordings_dir / "old.mkv"
    recording_path.write_bytes(b"old")
    supervisor._monitor_session("old", ExitedCamera(), recording_path)
    assert supervisor._camera is new_camera
    assert supervisor._session_id == "new"
    assert supervisor.record_state == "running"


def test_camera_error_includes_bounded_log_tail(runtime):
    class FailedCamera:
        log = type("Log", (), {"path": runtime.logs_dir / "camera-failure.log"})()

        @staticmethod
        def poll():
            return 7

        @staticmethod
        def wait(timeout=None):
            return 7

    FailedCamera.log.path.write_text(
        "ERROR: nvarguscamerasrc: failed to create capture session\n"
    )
    supervisor = Supervisor(runtime)
    recording_path = runtime.recordings_dir / "failed.mkv"
    recording_path.write_bytes(b"header")
    supervisor._camera = FailedCamera()
    supervisor._session_id = "session"
    supervisor._camera_marker_seen = True
    supervisor.record_state = "running"
    monitor = threading.Thread(
        target=supervisor._monitor_session,
        args=("session", FailedCamera(), recording_path),
    )
    supervisor._session_thread = monitor
    monitor.start()
    monitor.join(timeout=2)
    assert supervisor.record_state == "failed"
    assert "failed to create capture session" in supervisor.record_error
    assert len(supervisor.record_error) < 500


def test_recording_stops_when_free_space_crosses_threshold(runtime):
    class RunningCamera:
        def __init__(self):
            self.returncode = None
            self.log = type(
                "Log", (), {"path": runtime.logs_dir / "camera-low-disk.log"}
            )()

        def poll(self):
            return self.returncode

        def stop(self, grace_seconds=12):
            self.returncode = -2
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    usage = namedtuple("Usage", "total used free")
    free_values = iter([10 * 1024**3, 10 * 1024**3, 1 * 1024**3])
    disk_usage = lambda _: usage(
        20 * 1024**3,
        10 * 1024**3,
        next(free_values, 1 * 1024**3),
    )
    runtime = replace(runtime, recording_disk_check_seconds=0.01)
    supervisor = Supervisor(runtime, disk_usage=disk_usage)
    camera = RunningCamera()
    recording_path = runtime.recordings_dir / "low-disk.mkv"
    recording_path.write_bytes(b"video-data")
    supervisor._camera = camera
    supervisor._session_id = "session"
    supervisor._camera_marker_seen = True
    supervisor.record_state = "running"
    monitor = threading.Thread(
        target=supervisor._monitor_session,
        args=("session", camera, recording_path),
    )
    supervisor._session_thread = monitor
    monitor.start()
    monitor.join(timeout=3)
    assert not monitor.is_alive()
    assert supervisor.record_state == "failed"
    assert "free space fell below" in supervisor.record_error
    assert supervisor.last_recording["state"] == "complete"


def test_settings_are_locked_while_camera_busy(runtime):
    supervisor = Supervisor(runtime)
    supervisor.record_state = "running"
    with pytest.raises(SupervisorError) as exc:
        supervisor.save_settings(supervisor.settings)
    assert exc.value.status_code == 409


def test_snapshot_capture_is_serialized_with_recording(runtime):
    supervisor = Supervisor(runtime)
    supervisor.record_state = "running"
    with pytest.raises(SupervisorError) as exc:
        supervisor.start_snapshots()
    assert exc.value.status_code == 409


def test_stream_worker_retries_without_touching_recording_state(runtime):
    class FakeProcess:
        attempts = 0

        def __init__(self, *args, **kwargs):
            type(self).attempts += 1
            self.log = type("Log", (), {"path": runtime.logs_dir / "fake.log"})()

        def start(self):
            pass

        def is_running(self):
            return False

        def poll(self):
            return 1

        def stop(self, grace_seconds=3):
            return 0

    destination = DestinationSettings(
        id="test",
        name="Test",
        provider="custom",
        url="rtmp://example.test/live/key",
        enabled=True,
    )
    worker = DestinationWorker(
        destination,
        runtime,
        lambda: worker.stop_event.set() if FakeProcess.attempts >= 2 else None,
        FakeProcess,
    )
    worker.start()
    worker.thread.join(timeout=5)
    assert FakeProcess.attempts >= 2
    assert worker.state == "stopped"


def test_destination_worker_copies_video_and_audio_without_transcoding(runtime):
    destination = DestinationSettings(
        id="test",
        name="Test",
        provider="custom",
        url="srt://example.test:9000?streamid=private",
        enabled=True,
    )
    worker = DestinationWorker(destination, runtime, lambda: None)
    argv = worker._argv()
    assert argv[argv.index("-c") + 1] == "copy"
    assert "0:a:0?" in argv
    assert argv[-1] == destination.url


def test_native_nvmm_progress_confirms_recording(runtime):
    supervisor = Supervisor(runtime)
    supervisor._session_id = "native"
    supervisor._camera = object()
    supervisor._on_camera_line("native", "Processed 30 frames (29.9 / 29.9 fps)")
    assert supervisor.record_state == "running"
    assert supervisor._camera_marker_seen


def test_calibration_summary_is_readable_and_warns_about_weak_matches(runtime):
    supervisor = Supervisor(runtime)
    supervisor._on_calibration_line(json.dumps({"status": "ok", "confidence": .42, "matches": 21, "path": "/private/tmp.json"}, separators=(",", ":")))
    assert "42% confidence" in supervisor.calibration_progress
    assert "recalibrate before filming" in supervisor.calibration_progress
    assert "/private" not in supervisor.calibration_progress
