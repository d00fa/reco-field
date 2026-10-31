import json
import threading
from types import SimpleNamespace

import pytest

from conftest import session_headers
from reco_field import supervisor as module
from reco_field.config import DestinationSettings, FieldSettings, MOBILE_STREAM_BITRATE
from reco_field.storage import atomic_write_json, load_settings
from reco_field.supervisor import DestinationWorker, MobileStreamWorker, Supervisor, SupervisorError


def destinations(count=2):
    return [DestinationSettings(id=f"target{i}", name=f"Target {i}", provider="custom",
        url=f"rtmp://example.test/live/private{i}") for i in range(count)]


class Camera:
    def __init__(self, runtime, events=None):
        self.returncode = None
        self.events = events if events is not None else []
        self.log = SimpleNamespace(path=runtime.logs_dir / "fake-camera.log")
    def poll(self):
        return self.returncode
    def stop(self, grace_seconds=12):
        self.events.append("camera-stop")
        self.returncode = 0
        return 0
    def wait(self, timeout=None):
        self.events.append("camera-finalized")
        return self.returncode


@pytest.fixture
def workers(monkeypatch):
    events, publishers, copies = [], [], []
    class Copy:
        def __init__(self, destination, runtime, callback, factory, *, input_url):
            self.destination, self.input_url = destination, input_url
            self.state, self.error = "connecting", ""
            copies.append(self)
        def start(self):
            events.append("copy-start")
        def request_stop(self):
            events.append("copy-cancel")
        def stop(self):
            events.append("copy-stop")
            self.state = "stopped"
            return True
        def status(self):
            return {"id": self.destination.id, "state": self.state}
    class Publisher:
        def __init__(self, runtime, fps, callback, factory):
            self.on_status, self.state, self.error = callback, "connecting", ""
            self.attempt = 1
            publishers.append(self)
        def start(self):
            events.append("publisher-start")
        def request_stop(self):
            events.append("publisher-cancel")
        def stop(self):
            events.append("publisher-stop")
            self.state = "stopped"
            return True
        def status(self):
            return {"profile": "mobile", "state": self.state, "error": self.error,
                "attempt": self.attempt, "video_bitrate": MOBILE_STREAM_BITRATE}
    monkeypatch.setattr(module, "DestinationWorker", Copy)
    monkeypatch.setattr(module, "MobileStreamWorker", Publisher)
    return SimpleNamespace(events=events, publishers=publishers, copies=copies)


def prepared(runtime, workers, *, profile="mobile", enabled=True, count=2):
    supervisor = Supervisor(runtime)
    supervisor.settings.destinations = destinations(count)
    supervisor.settings.stream_profile = profile
    supervisor.stream_enabled = enabled
    supervisor._session_id = "session"
    supervisor._camera = Camera(runtime, workers.events)
    supervisor.record_state = "starting"
    return supervisor


def test_old_settings_default_mobile_and_profile_roundtrips(client, runtime):
    old = FieldSettings().model_dump(mode="json")
    old.pop("stream_profile")
    atomic_write_json(runtime.settings_file, old)
    assert load_settings(runtime.settings_file).stream_profile == "mobile"
    headers = session_headers(client)
    settings = client.get("/api/settings").json()
    assert settings["stream_profile"] == "mobile"
    settings["stream_profile"] = "original"
    result = client.put("/api/settings", json=settings, headers=headers)
    assert result.status_code == 200
    assert client.get("/api/settings").json()["stream_profile"] == "original"
    settings["stream_profile"] = "unknown"
    assert client.put("/api/settings", json=settings, headers=headers).status_code == 422
    assert load_settings(runtime.settings_file).stream_profile == "original"


def test_one_publisher_feeds_all_destinations_only_after_video_ready(runtime, workers):
    supervisor = prepared(runtime, workers)
    assert not workers.publishers
    supervisor._on_camera_line("session", "Processed 30 frames (30 fps)")
    assert len(workers.publishers) == 1 and not workers.copies
    publisher = workers.publishers[0]
    publisher.state = "running"
    publisher.on_status()
    assert len(workers.copies) == 2
    assert all(w.input_url == runtime.relay_mobile_rtmp_url for w in workers.copies)
    supervisor._start_streaming("session")
    publisher.on_status()
    assert len(workers.publishers) == 1 and len(workers.copies) == 2
    status = supervisor.status()["stream_publisher"]
    assert status["profile"] == "mobile" and "url" not in status
    assert "private" not in json.dumps(status)


@pytest.mark.parametrize("enabled,count", [(False, 2), (True, 0)])
def test_no_publisher_without_requested_stream_and_enabled_targets(runtime, workers, enabled, count):
    supervisor = prepared(runtime, workers, enabled=enabled, count=count)
    supervisor._on_camera_line("session", "Processed 30 frames (30 fps)")
    assert not workers.publishers and not workers.copies


def test_disabled_destinations_do_not_start_publisher(runtime, workers):
    supervisor = prepared(runtime, workers)
    for destination in supervisor.settings.destinations:
        destination.enabled = False
    supervisor._on_camera_line("session", "Processed 30 frames (30 fps)")
    assert not workers.publishers and not workers.copies


def test_original_profile_copies_full_stream_without_mobile_encoder(runtime, workers):
    supervisor = prepared(runtime, workers, profile="original")
    supervisor._on_camera_line("session", "Processed 30 frames (30 fps)")
    assert not workers.publishers and len(workers.copies) == 2
    assert all(w.input_url == runtime.relay_rtmp_url for w in workers.copies)
    copy = DestinationWorker(destinations(1)[0], runtime, lambda: None)
    argv = copy._argv()
    assert argv[argv.index("-c") + 1] == "copy" and "-c:v" not in argv


def test_stream_profile_does_not_change_camera_or_preview_input(runtime):
    supervisor = Supervisor(runtime)
    output = runtime.recordings_dir / "record.mkv"
    original = supervisor._camera_argv(output)
    supervisor.settings.stream_profile = "original"
    assert supervisor._camera_argv(output) == original
    assert original[original.index("--stream-url") + 1] == runtime.relay_rtmp_url


@pytest.mark.parametrize("fps,expected", [(60, 30), (20, 20)])
def test_mobile_encoder_bounds_size_rate_threads_and_network_io(runtime, fps, expected):
    publisher = MobileStreamWorker(runtime, fps, lambda: None)
    argv = publisher._argv()
    assert argv[argv.index("-i") + 1] == runtime.relay_rtmp_url
    assert argv[-1] == runtime.relay_mobile_rtmp_url
    assert argv.count("-rw_timeout") == 2
    assert argv[argv.index("-filter_threads") + 1] == "1"
    assert [argv[i + 1] for i, arg in enumerate(argv) if arg == "-threads"] == ["2", "2"]
    assert argv[argv.index("-b:v") + 1] == str(MOBILE_STREAM_BITRATE)
    assert argv[argv.index("-maxrate") + 1] == str(MOBILE_STREAM_BITRATE)
    assert argv[argv.index("-g") + 1] == str(expected * 2)
    filters = argv[argv.index("-vf") + 1]
    assert "min(1920,iw)" in filters and "min(540,ih)" in filters
    assert "force_divisible_by=2" in filters and f"fps={expected}" in filters
    assert argv[argv.index("-c:a") + 1] == "copy"


def test_only_advancing_positive_frames_update_readiness_and_deadlines(runtime, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    publisher = MobileStreamWorker(runtime, 30, lambda: None)
    publisher._on_line("frame=0")
    assert publisher.state == "connecting" and publisher._last_frame == 0
    assert "startup deadline" in publisher._progress_failure(121)
    clock[0] = 121
    publisher._on_line("frame=1")
    assert publisher.state == "connecting"
    fresh = MobileStreamWorker(runtime, 30, lambda: None)
    fresh._on_line("frame=1")
    assert fresh.state == "running"
    clock[0] = 126
    fresh._on_line("frame=1")
    fresh._on_line("frame=0")
    assert fresh._last_progress == 121
    assert "stopped advancing" in fresh._progress_failure(132)
    fresh._on_line("frame=2")
    assert fresh._progress_failure(132) == ""
    fresh._attempt_generation = 4
    fresh._on_line("frame=99", 3)
    assert fresh._last_frame == 2
    fresh.request_stop()
    fresh._on_line("frame=100", 4)
    assert fresh._last_frame == 2


@pytest.mark.parametrize("first_frame,error", [(0, "startup deadline"), (1, "stopped advancing")])
def test_stalled_publisher_retries_without_failing_recording(runtime, first_frame, error):
    supervisor = Supervisor(runtime)
    supervisor.record_state = "running"
    failures, stopped = [], []
    class Process:
        def __init__(self, *args, on_line, **kwargs):
            self.callback, self.running = on_line, True
        def start(self):
            self.callback(f"frame={first_frame}")
        def is_running(self):
            return self.running
        def poll(self):
            return None if self.running else 1
        def stop(self, grace_seconds):
            self.running = False
            stopped.append(True)
    def status_changed():
        if publisher.state == "retrying":
            failures.append(publisher.error)
            publisher.request_stop()
    publisher = MobileStreamWorker(runtime, 30, status_changed, Process)
    publisher.STARTUP_TIMEOUT_SECONDS = publisher.STALL_TIMEOUT_SECONDS = .03
    publisher.POLL_SECONDS = .005
    publisher.start()
    publisher.thread.join(2)
    assert not publisher.thread.is_alive() and stopped
    assert error in failures[0]
    assert supervisor.record_state == "running"


def test_cancel_during_process_construction_prevents_late_start(runtime):
    entered, release = threading.Event(), threading.Event()
    starts = []
    class Process:
        def start(self):
            starts.append(True)
        def stop(self, grace_seconds):
            return 0
        def is_running(self):
            return False
    def factory(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return Process()
    publisher = MobileStreamWorker(runtime, 30, lambda: None, factory)
    publisher.start()
    assert entered.wait(2)
    publisher.request_stop()
    release.set()
    publisher.stop()
    assert not publisher.thread.is_alive() and not starts


def test_publisher_retries_stalled_start_and_accepts_next_attempt(runtime):
    attempts = []
    class Process:
        def __init__(self, *args, on_line, **kwargs):
            self.callback, self.running = on_line, True
            attempts.append(self)
        def start(self):
            self.callback(f"frame={2 if len(attempts) > 1 else 0}")
        def is_running(self):
            return self.running
        def poll(self):
            return None if self.running else 1
        def stop(self, grace_seconds):
            self.running = False
    def changed():
        if publisher.state == "running":
            publisher.request_stop()
    publisher = MobileStreamWorker(runtime, 30, changed, Process)
    publisher.STARTUP_TIMEOUT_SECONDS = .02
    publisher.POLL_SECONDS = publisher.RETRY_SECONDS = .005
    publisher.start()
    publisher.thread.join(2)
    assert not publisher.thread.is_alive() and len(attempts) == 2
    assert publisher.attempt == 2 and publisher._last_frame == 2


def test_failed_process_cleanup_does_not_launch_duplicate_publishers(runtime):
    attempts = []
    class Process:
        def __init__(self, *args, **kwargs):
            attempts.append(self)
        def start(self):
            pass
        def is_running(self):
            return True
        def poll(self):
            return None
        def stop(self, grace_seconds):
            raise OSError("private stream URL must not appear in status")
    publisher = MobileStreamWorker(runtime, 30, lambda: None, Process)
    publisher.STARTUP_TIMEOUT_SECONDS = .02
    publisher.POLL_SECONDS = .005
    publisher.start()
    publisher.thread.join(2)
    assert not publisher.thread.is_alive() and len(attempts) == 1
    assert publisher.state == "failed"
    assert "private" not in json.dumps(publisher.status())


def test_stop_finalizes_camera_before_stream_cleanup_and_ignores_stale_ready(runtime, workers):
    supervisor = prepared(runtime, workers)
    supervisor._on_camera_line("session", "Processed 30 frames (30 fps)")
    publisher = workers.publishers[0]
    publisher.state = "running"
    publisher.on_status()
    path = runtime.recordings_dir / "finished.mkv"
    path.write_bytes(b"video")
    supervisor.recording_path = path
    monitor = threading.Thread(target=supervisor._monitor_session, args=("session", supervisor._camera, path))
    supervisor._session_thread = monitor
    monitor.start()
    result = supervisor.stop_recording()
    assert result["record"]["state"] == "idle"
    assert supervisor.last_recording["state"] == "complete"
    assert workers.events.index("camera-finalized") < workers.events.index("copy-stop")
    assert supervisor._mobile_publisher is None and not supervisor._destination_workers
    supervisor._session_id = "new"
    supervisor._stop_requested = False
    supervisor._camera = Camera(runtime)
    supervisor.record_state = "running"
    publisher.state = "running"
    supervisor._mobile_publisher = publisher
    copies = len(workers.copies)
    publisher.on_status()
    assert len(workers.copies) == copies


def test_camera_failure_and_shutdown_clean_up_publisher(runtime, workers):
    supervisor = prepared(runtime, workers)
    supervisor._on_camera_line("session", "Processed 30 frames (30 fps)")
    supervisor._camera.returncode = 7
    path = runtime.recordings_dir / "failed.mkv"
    path.write_bytes(b"header")
    supervisor._monitor_session("session", supervisor._camera, path)
    assert supervisor.record_state == "failed" and supervisor._mobile_publisher is None
    assert "publisher-stop" in workers.events
    orphan = workers.publishers[0]
    supervisor._mobile_publisher = orphan
    supervisor.shutdown()
    assert supervisor._mobile_publisher is None and orphan.state == "stopped"


@pytest.mark.parametrize("failure", ["raises", "still-running"])
def test_failed_cleanup_retains_workers_blocks_capture_and_can_be_retried(runtime, failure):
    class Process:
        def __init__(self):
            self.running, self.allow_stop = True, False
        def stop(self, grace_seconds):
            if not self.allow_stop:
                if failure == "raises":
                    raise OSError("Cannot reap the stream process")
                return 0
            self.running = False
            return 0
        def is_running(self):
            return self.running

    supervisor = Supervisor(runtime)
    camera = Camera(runtime)
    camera.returncode = 0
    supervisor._camera = camera
    supervisor._session_id = "cleanup"
    supervisor._camera_marker_seen = True
    supervisor.record_state = "running"
    path = runtime.recordings_dir / "complete-before-cleanup.mkv"
    path.write_bytes(b"finalized recording")
    supervisor.recording_path = path
    publisher = MobileStreamWorker(runtime, 30, lambda: None)
    copy = DestinationWorker(destinations(1)[0], runtime, lambda: None)
    processes = [Process(), Process()]
    publisher._process, copy._process = processes
    supervisor._mobile_publisher = publisher
    supervisor._destination_workers[copy.destination.id] = copy

    supervisor._monitor_session("cleanup", camera, path)
    assert supervisor.record_state == "stopping"
    assert supervisor.last_recording["state"] == "complete"
    assert supervisor._camera is None
    assert supervisor._mobile_publisher is publisher
    assert supervisor._destination_workers[copy.destination.id] is copy
    assert all(process.is_running() for process in processes)
    for stream in (False, True):
        with pytest.raises(SupervisorError, match="Previous stream cleanup"):
            supervisor.start_recording(stream)

    for process in processes:
        process.allow_stop = True
    result = supervisor.stop_recording()
    assert result["record"]["state"] == "idle"
    assert not result["record"]["error"]
    assert not supervisor._destination_workers and supervisor._mobile_publisher is None
    assert all(not process.is_running() for process in processes)
    assert supervisor.last_recording["state"] == "complete"
    assert path.read_bytes() == b"finalized recording"


def test_camera_exit_rejects_late_mobile_readiness_during_cleanup(runtime, workers):
    supervisor = prepared(runtime, workers)
    supervisor._on_camera_line("session", "Processed 30 frames (30 fps)")
    publisher = workers.publishers[0]
    def late_ready():
        publisher.state = "running"
        publisher.on_status()
    publisher.request_stop = late_ready
    supervisor._camera.returncode = 0
    path = runtime.recordings_dir / "late-ready.mkv"
    path.write_bytes(b"video")
    supervisor._monitor_session("session", supervisor._camera, path)
    assert not workers.copies
    assert not supervisor._destination_workers and supervisor._mobile_publisher is None
    assert supervisor.last_recording["state"] == "complete"
