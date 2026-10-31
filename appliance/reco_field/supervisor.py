from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import (
    CameraSettings,
    DestinationSettings,
    FieldSettings,
    RuntimeConfig,
    MOBILE_STREAM_BITRATE,
    masked_ingest_url,
)
from .processes import ManagedProcess, url_secret_fragments
from .lens_calibration import LensCalibration
from .capture_sessions import source_session, validate_stitch_source
from .offline_stitch import OfflineStitch
from .telemetry import DeviceTelemetry, RecordingMetadata
from .storage import (
    require_storage,
    atomic_write,
    load_settings,
    save_settings,
    safe_recording_path,
    safe_session_directory,
    safe_session_file,
    atomic_write_json,
    validate_calibration,
    validate_capture_profile,
    validate_lens_profile,
)

UTC = timezone.utc
MAINTENANCE_FILE = Path("/run/reco-field-upgrade")


class SupervisorError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class DestinationWorker:
    STARTUP_TIMEOUT_SECONDS = 20.0
    STALL_TIMEOUT_SECONDS = 10.0
    POLL_SECONDS = 0.5
    RETRY_SECONDS = 2.0

    def __init__(
        self,
        destination: DestinationSettings,
        runtime: RuntimeConfig,
        on_status: Any,
        process_factory: Any = ManagedProcess,
        *,
        input_url: str | None = None,
    ) -> None:
        self.destination = destination
        self.runtime = runtime
        self.on_status = on_status
        self.process_factory = process_factory
        self.input_url = input_url or runtime.relay_rtmp_url
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.state = "connecting"
        self.error = ""
        self.attempt = 0
        self._process: ManagedProcess | None = None
        self._process_lock = threading.RLock()
        self._cleanup_lock = threading.Lock()
        self._progress_lock = threading.RLock()
        self._attempt_generation = 0
        self._last_frame = 0
        self._last_progress = 0.0
        self._attempt_started = time.monotonic()

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> bool:
        self.request_stop()
        with self._process_lock:
            process = self._process
        if process and self._stop_process(process):
            with self._process_lock:
                if self._process is process:
                    self._process = None
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)
        with self._process_lock:
            return self._process is None and not self.thread.is_alive()

    def request_stop(self) -> None:
        self.stop_event.set()

    def _stop_process(self, process: Any) -> bool:
        with self._cleanup_lock:
            try:
                process.stop(grace_seconds=3)
                if process.is_running():
                    raise RuntimeError("Stream process did not stop.")
                return True
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                self.state, self.error = "failed", "Stream cleanup did not finish cleanly."
                self.stop_event.set()
                return False

    def status(self) -> dict[str, Any]:
        return {
            "id": self.destination.id,
            "name": self.destination.name,
            "provider": self.destination.provider,
            "url": masked_ingest_url(self.destination.url),
            "state": self.state,
            "error": self.error,
            "attempt": self.attempt,
        }

    def _run(self) -> None:
        backoff = self.RETRY_SECONDS
        try:
            while not self.stop_event.is_set():
                with self._progress_lock:
                    self.attempt += 1
                    self._attempt_generation += 1
                    generation = self._attempt_generation
                    self._last_frame = 0
                    self._last_progress = 0.0
                    self._attempt_started = time.monotonic()
                    self.state, self.error = "connecting", ""
                self.on_status()
                process = None
                try:
                    process = self.process_factory(self._argv(), self._log_path(),
                        secrets_to_hide=url_secret_fragments(self.destination.url or ""),
                        on_line=lambda line, generation=generation: self._on_line(line, generation),
                        log_max_bytes=self.runtime.log_max_bytes, log_backups=self.runtime.log_backups)
                    with self._process_lock:
                        if self.stop_event.is_set():
                            break
                        self._process = process
                        process.start()
                    failure = ""
                    while not self.stop_event.is_set() and process.is_running():
                        failure = self._progress_failure(time.monotonic())
                        if failure:
                            break
                        self.stop_event.wait(self.POLL_SECONDS)
                    if self.stop_event.is_set():
                        break
                    self.state = "retrying"
                    self.error = failure or f"stream process exited with status {process.poll()}"
                    if self._last_frame:
                        backoff = self.RETRY_SECONDS
                    self.on_status()
                except OSError as exc:
                    self.state, self.error = "retrying", f"cannot start FFmpeg: {exc}"
                    self.on_status()
                finally:
                    with self._progress_lock:
                        self._attempt_generation += 1
                    stopped = not process or self._stop_process(process)
                    with self._process_lock:
                        if stopped and self._process is process:
                            self._process = None
                if self.stop_event.wait(backoff):
                    break
                backoff = min(backoff * 2, 60)
        finally:
            if self.stop_event.is_set():
                if self.state != "failed":
                    self.state = "stopped"
                self.on_status()

    def _log_path(self) -> Path:
        return self.runtime.logs_dir / f"destination-{self.destination.id}.log"

    def _progress_failure(self, now: float) -> str:
        with self._progress_lock:
            if not self._last_frame and now - self._attempt_started >= self.STARTUP_TIMEOUT_SECONDS:
                return "Stream produced no video before its startup deadline."
            if self._last_frame and now - self._last_progress >= self.STALL_TIMEOUT_SECONDS:
                return "Stream video stopped advancing."
        return ""

    def _on_line(self, line: str, generation: int | None = None) -> None:
        match = re.fullmatch(r"frame=\s*(\d{1,20})\s*", line.strip())
        if not match:
            return
        changed = False
        with self._progress_lock:
            if (self.stop_event.is_set() or self.state not in {"connecting", "running"}
                or (generation is not None and generation != self._attempt_generation)):
                return
            frame = int(match.group(1))
            now = time.monotonic()
            if frame <= self._last_frame or (not self._last_frame and now - self._attempt_started >= self.STARTUP_TIMEOUT_SECONDS):
                return
            self._last_frame, self._last_progress = frame, now
            changed = self.state != "running"
            self.state, self.error = "running", ""
        if changed:
            self.on_status()

    def _argv(self) -> list[str]:
        assert self.destination.url
        scheme = urlsplit(self.destination.url).scheme.lower()
        output_format = "mpegts" if scheme == "srt" else "flv"
        return [
            self.runtime.ffmpeg_bin,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-progress",
            "pipe:1",
            "-nostats",
            "-i",
            self.input_url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-f",
            output_format,
            self.destination.url,
        ]


class MobileStreamWorker(DestinationWorker):
    def __init__(self, runtime: RuntimeConfig, capture_fps: int, on_status: Any,
                 process_factory: Any = ManagedProcess) -> None:
        super().__init__(DestinationSettings(id="mobile-publisher", name="Mobile stream", provider="custom",
            url=runtime.relay_mobile_rtmp_url), runtime, on_status, process_factory)
        self.fps = min(30, capture_fps)

    def status(self) -> dict[str, Any]:
        return {"profile": "mobile", "state": self.state, "error": self.error,
            "attempt": self.attempt, "video_bitrate": MOBILE_STREAM_BITRATE}

    def _log_path(self) -> Path:
        return self.runtime.logs_dir / "mobile-publisher.log"

    def _argv(self) -> list[str]:
        scale = "scale=w='min(1920,iw)':h='min(540,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
        return [self.runtime.ffmpeg_bin, "-nostdin", "-hide_banner", "-loglevel", "warning",
            "-progress", "pipe:1", "-nostats", "-filter_threads", "1", "-threads", "2",
            "-rw_timeout", "5000000", "-i", self.runtime.relay_rtmp_url,
            "-map", "0:v:0", "-map", "0:a:0?", "-vf", f"{scale},fps={self.fps}",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-threads", "2",
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-b:v", str(MOBILE_STREAM_BITRATE),
            "-maxrate", str(MOBILE_STREAM_BITRATE), "-bufsize", str(MOBILE_STREAM_BITRATE * 2),
            "-g", str(self.fps * 2), "-keyint_min", str(self.fps * 2), "-sc_threshold", "0", "-bf", "0",
            "-c:a", "copy", "-rw_timeout", "5000000", "-f", "flv", self.runtime.relay_mobile_rtmp_url]


class Supervisor:
    def __init__(
        self,
        runtime: RuntimeConfig,
        settings: FieldSettings | None = None,
        process_factory: Any = ManagedProcess,
        disk_usage: Any = shutil.disk_usage,
    ) -> None:
        self.runtime = runtime
        self.settings = settings or load_settings(runtime.settings_file)
        self.process_factory = process_factory
        self.disk_usage = disk_usage
        self._lock = threading.RLock()
        self._relay: ManagedProcess | None = None
        self._camera: ManagedProcess | None = None
        self._simulator_publisher: ManagedProcess | None = None
        self._calibration: ManagedProcess | None = None
        self._session_thread: threading.Thread | None = None
        self._session_id: str | None = None
        self._capture_kind: str | None = None
        self._capture_mode: str | None = None
        self._source_dir: Path | None = None
        self._capabilities = {"preview": runtime.simulator, "recording_modes": ["panorama"], "offline_stitch": False}
        self._capture_metrics: dict[str, Any] | None = None
        self.device_telemetry = DeviceTelemetry(runtime)
        self.recording_metadata = RecordingMetadata(runtime)
        self.offline_stitch = OfflineStitch(runtime, self.recording_metadata._probe, process_factory)
        self._destination_workers: dict[str, DestinationWorker] = {}
        self._mobile_publisher: MobileStreamWorker | None = None
        self.record_state = "idle"
        self.record_error = ""
        self.recording_path: Path | None = None
        self.record_started_at: datetime | None = None
        self.last_recording: dict[str, Any] | None = None
        self.stream_enabled = False
        self.calibration_state = "idle"
        self.calibration_error = ""
        self.calibration_progress = ""
        self._calibration_temp: Path | None = None
        self._calibration_thread: threading.Thread | None = None
        self._calibration_started: datetime | None = None
        self._snapshot_process: ManagedProcess | None = None
        self._snapshot_thread: threading.Thread | None = None
        self.snapshot_state = "idle"
        self.snapshot_error = ""
        self.snapshot_progress = ""
        self._stop_requested = False
        self._camera_marker_seen = False
        self._relay_managed_by_service = runtime.manage_relay
        self.lens_calibration = LensCalibration(runtime, process_factory, self._snapshot_argv)

    def get_settings(self) -> FieldSettings:
        with self._lock:
            return self.settings.model_copy(deep=True)

    def save_settings(self, settings: FieldSettings) -> FieldSettings:
        with self._lock:
            self._require_lens_idle()
            if self.record_state not in {"idle", "failed"}:
                raise SupervisorError(
                    "Stop recording before applying camera or destination settings.",
                    409,
                )
            if self.calibration_state in {"starting", "running", "stopping"}:
                raise SupervisorError(
                    "Wait for calibration to finish before changing settings.", 409
                )
            if self.snapshot_state in {"starting", "running", "stopping"}:
                raise SupervisorError(
                    "Wait for setup snapshots to finish before changing settings.", 409
                )
            self.settings = settings
            save_settings(self.runtime.settings_file, settings)
            return settings.model_copy(deep=True)

    def merge_settings(self, incoming: FieldSettings) -> FieldSettings:
        existing = {item.id: item for item in self.settings.destinations}
        merged: list[DestinationSettings] = []
        for destination in incoming.destinations:
            if destination.url is None or destination.url == masked_ingest_url(
                existing.get(destination.id).url
                if existing.get(destination.id)
                else None
            ):
                old = existing.get(destination.id)
                if not old or not old.url:
                    raise SupervisorError(
                        f"Destination {destination.name!r} requires an ingest URL."
                    )
                destination = destination.model_copy(update={"url": old.url})
            merged.append(destination)
        return incoming.model_copy(update={"destinations": merged})

    def public_settings(self) -> dict[str, Any]:
        value = self.settings.model_dump(mode="json")
        value["destinations"] = [
            {
                **destination.model_dump(mode="json", exclude={"url"}),
                "url": masked_ingest_url(destination.url),
            }
            for destination in self.settings.destinations
        ]
        return value

    def refresh_capabilities(self) -> None:
        if self.runtime.simulator:
            return
        help_text = self._run_text([self.runtime.reco_bin, "camera", "--help"])
        preview = "--no-panorama-file" in help_text and "--stream-url" in help_text
        sources = preview and all(flag in help_text for flag in ("--source-dir", "--source-size", "--source-crf", "--preview-fps"))
        stitch_help = self._run_text([self.runtime.reco_bin, "stitch", "--help"])
        with self._lock:
            self._capabilities = {"preview": preview, "recording_modes": ["panorama", "sources"] if sources else ["panorama"],
                                  "offline_stitch": "--source-manifest" in stitch_help and "--projection" in stitch_help}

    def start_recording(self, stream: bool | None = None, mode: str | None = None) -> dict[str, Any]:
        return self._start_capture("recording", stream, mode)

    def start_preview(self) -> dict[str, Any]:
        return self._start_capture("preview", False, None)

    def _start_capture(self, kind: str, stream: bool | None, mode: str | None) -> dict[str, Any]:
        with self._lock:
            mode = (mode or self.settings.recording_mode) if kind == "recording" else None
            if kind == "preview" and not self._capabilities["preview"]:
                raise SupervisorError("This Reco build does not support preview without recording.", 409)
            if mode is not None and mode not in self._capabilities["recording_modes"]:
                raise SupervisorError("This Reco build does not support the selected recording mode.", 409)
            self._require_lens_idle()
            self._refresh_process_state()
            if not self._camera and (self._destination_workers or self._mobile_publisher):
                raise SupervisorError(
                    "Previous stream cleanup has not finished. Press Stop to retry before starting another recording.",
                    409,
                )
            if self.record_state not in {"idle", "failed"}:
                raise SupervisorError("Stop the current preview or recording before starting another capture.", 409)
            if self._camera or (
                self._session_thread and self._session_thread.is_alive()
            ):
                raise SupervisorError(
                    "The previous recording is still finalizing; try again shortly.",
                    409,
                )
            if self.calibration_state in {"starting", "running", "stopping"}:
                raise SupervisorError("Camera is busy with calibration.", 409)
            if self.snapshot_state in {"starting", "running", "stopping"}:
                raise SupervisorError("Camera is busy capturing setup snapshots.", 409)
            if not self.runtime.calibration_file.exists():
                raise SupervisorError(
                    "Import or complete a stereo calibration before recording.", 409
                )
            calibration = validate_calibration(self.runtime.calibration_file.read_bytes())
            try:
                self._check_capture_profiles(calibration)
            except ValueError as exc:
                raise SupervisorError(str(exc), 409) from exc
            requested_stream = (
                self.settings.auto_stream if stream is None else bool(stream)
            )
            if requested_stream and not any(
                item.enabled and item.url for item in self.settings.destinations
            ):
                raise SupervisorError(
                    "Streaming requested, but no enabled destinations are configured.",
                    409,
                )
            self._check_disk()
            self._ensure_relay()
            if mode in {"sources", "both"}:
                camera_settings = self.settings.camera
                self._scaled_size(camera_settings.capture_width, camera_settings.capture_height, 2016, 1520)
                self._scaled_size(camera_settings.output_width, camera_settings.output_height, 1536, 432, width_multiple=4)

            self._capture_kind, self._capture_mode = kind, mode
            self._source_dir = None
            if mode in {"sources", "both"}:
                session_name = f"reco-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:12]}"
                self._source_dir = safe_session_directory(self.runtime.recordings_dir, session_name)
            self.recording_path = (self._source_dir / "panorama.mkv" if mode == "both" else
                self.runtime.recordings_dir / f"reco-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.mkv"
                if mode == "panorama" else None)
            self.record_state = "starting"
            self.record_error = ""
            self.record_started_at = datetime.now(UTC)
            self.stream_enabled = requested_stream
            self._camera_marker_seen = False
            self._stop_requested = False
            self._session_id = uuid.uuid4().hex
            self._capture_metrics = None
            secrets = tuple(
                item.url or "" for item in self.settings.destinations if item.enabled
            )
            try:
                self._camera = self.process_factory(
                    self._camera_argv(self.recording_path, source_dir=self._source_dir),
                    self.runtime.logs_dir / "camera.log",
                    secrets_to_hide=secrets,
                    on_line=lambda line, session_id=self._session_id: self._on_camera_line(
                        session_id, line
                    ),
                    log_max_bytes=self.runtime.log_max_bytes,
                    log_backups=self.runtime.log_backups,
                )
                self._camera.start()
            except OSError as exc:
                self.record_state = "failed"
                self.record_error = f"Cannot start Reco: {exc}"
                self._camera = None
                self._session_id = None
                self._session_thread = None
                raise SupervisorError(self.record_error, 503) from exc
            self._session_thread = threading.Thread(
                target=self._monitor_session,
                args=(self._session_id, self._camera, self.recording_path, self._source_dir),
                daemon=True,
            )
            self._session_thread.start()
            return self.status()

    def stop_recording(self) -> dict[str, Any]:
        with self._lock:
            if self._capture_kind == "preview" and self.record_state not in {"idle", "failed"}:
                raise SupervisorError("Use Stop preview to release the viewfinder.", 409)
        return self._stop_capture()

    def stop_preview(self) -> dict[str, Any]:
        with self._lock:
            if self._capture_kind != "preview" and self.record_state not in {"idle", "failed"}:
                raise SupervisorError("A recording is active. Use Stop recording to finish its files.", 409)
        return self._stop_capture()

    def _stop_capture(self) -> dict[str, Any]:
        with self._lock:
            if (self.record_state in {"idle", "failed"} and not self._camera
                and not self._destination_workers and not self._mobile_publisher):
                return self.status()
            self._stop_requested = True
            self.record_state = "stopping"
            workers = list(self._destination_workers.values())
            mobile = self._mobile_publisher
            camera = self._camera
            publisher = self._simulator_publisher
            thread = self._session_thread
            session_id = self._session_id
        for worker in [*workers, *([mobile] if mobile else [])]:
            worker.request_stop()
        if camera:
            camera.stop(grace_seconds=16 if self._source_dir else 12)
        if publisher:
            publisher.stop(grace_seconds=3)
        if thread and thread.is_alive():
            thread.join(timeout=15)
        elif not thread or not thread.is_alive():
            with self._lock:
                cleanup_needed = self._session_id == session_id and self._camera is camera
            if cleanup_needed:
                cleanup_complete = self._stop_stream_workers(workers, mobile)
            with self._lock:
                if cleanup_needed and self._session_id == session_id and self._camera is camera:
                    self._simulator_publisher = None
                    self._camera = None
                    self._session_id = None
                    self._session_thread = None
                    self.record_state = "idle" if cleanup_complete else "stopping"
                    self.record_error = "" if cleanup_complete else "Stream cleanup did not finish. Press Stop to retry; the recording file is preserved."
        with self._lock:
            self._refresh_process_state()
            return self.status()

    def start_calibration(self) -> dict[str, Any]:
        with self._lock:
            self._require_lens_idle()
            self._refresh_process_state()
            if self.record_state not in {"idle", "failed"}:
                raise SupervisorError("Stop recording before calibrating.", 409)
            if self.calibration_state in {"starting", "running", "stopping"}:
                raise SupervisorError("Calibration is already running.", 409)
            if self.snapshot_state in {"starting", "running", "stopping"}:
                raise SupervisorError("Wait for setup snapshots to finish.", 409)
            for side in ("left", "right"):
                try:
                    self._validated_lens_profile(side)
                except ValueError as exc:
                    raise SupervisorError(str(exc), 409) from exc
            self._check_disk()
            self.calibration_state = "starting"
            self.calibration_error = ""
            self.calibration_progress = "Starting camera capture"
            self._calibration_started = datetime.now(UTC)
            self._calibration_temp = (
                self.runtime.temp_dir
                / f"calibration-{uuid.uuid4().hex}.json"
            )
            self._seed_calibration_output()
            if self.runtime.simulator:
                self._calibration_thread = threading.Thread(
                    target=self._simulate_calibration, daemon=True
                )
                self._calibration_thread.start()
                return self.status()

            self._calibration = self.process_factory(
                self._calibration_argv(self._calibration_temp),
                self.runtime.logs_dir / "calibration.log",
                on_line=self._on_calibration_line,
                log_max_bytes=self.runtime.log_max_bytes,
                log_backups=self.runtime.log_backups,
            )
            try:
                self._calibration.start()
            except OSError as exc:
                self.calibration_state = "failed"
                self.calibration_error = f"Cannot start Reco calibration: {exc}"
                raise SupervisorError(self.calibration_error, 503) from exc
            self._calibration_thread = threading.Thread(
                target=self._monitor_calibration, daemon=True
            )
            self._calibration_thread.start()
            return self.status()

    def _seed_calibration_output(self) -> None:
        if not self._calibration_temp or not self.runtime.calibration_file.exists():
            return
        try:
            data = self.runtime.calibration_file.read_bytes()
            validate_calibration(data)
        except (OSError, ValueError, json.JSONDecodeError):
            return
        atomic_write(self._calibration_temp, data)

    def start_snapshots(self) -> dict[str, Any]:
        with self._lock:
            self._require_lens_idle()
            self._refresh_process_state()
            if self.record_state not in {"idle", "failed"}:
                raise SupervisorError("Stop recording before capturing setup images.", 409)
            if self.calibration_state in {"starting", "running", "stopping"}:
                raise SupervisorError("Camera is busy with calibration.", 409)
            if self.snapshot_state in {"starting", "running", "stopping"}:
                raise SupervisorError("Setup image capture is already running.", 409)
            self._check_disk()
            self.snapshot_state = "starting"
            self.snapshot_error = ""
            self.snapshot_progress = "Preparing camera snapshots"
            self._snapshot_thread = threading.Thread(
                target=self._capture_snapshots, daemon=True
            )
            self._snapshot_thread.start()
            return self.status()

    def stop_snapshots(self) -> dict[str, Any]:
        with self._lock:
            if self.snapshot_state not in {"starting", "running"}:
                return self.status()
            self.snapshot_state = "stopping"
            process = self._snapshot_process
        if process:
            process.stop(grace_seconds=3)
        thread = self._snapshot_thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
        with self._lock:
            if self.snapshot_state != "failed":
                self.snapshot_state = "idle"
                self.snapshot_progress = "Cancelled"
            return self.status()

    def stop_calibration(self) -> dict[str, Any]:
        with self._lock:
            if self.calibration_state not in {"starting", "running"}:
                return self.status()
            self.calibration_state = "stopping"
            process = self._calibration
        if process:
            process.stop(grace_seconds=5)
        thread = self._calibration_thread
        if thread and thread.is_alive():
            thread.join(timeout=8)
        with self._lock:
            if self.calibration_state != "failed":
                self.calibration_state = "idle"
                self.calibration_progress = "Cancelled"
            self._calibration_started = None
            return self.status()

    def import_calibration(self, data: bytes, kind: str) -> dict[str, Any]:
        with self._lock:
            self._require_lens_idle()
            if self.record_state not in {"idle", "failed"}:
                raise SupervisorError("Stop recording before importing calibration.", 409)
            if self.calibration_state in {"starting", "running", "stopping"}:
                raise SupervisorError(
                    "Wait for calibration to finish before importing a lens or calibration file.",
                    409,
                )
            if self.snapshot_state in {"starting", "running", "stopping"}:
                raise SupervisorError("Wait for setup snapshots to finish.", 409)
            if kind == "calibration":
                validate_calibration(data)
                atomic_write(self.runtime.calibration_file, data)
            elif kind in {"left-lens", "right-lens"}:
                validate_lens_profile(data)
                atomic_write(self.runtime.data_dir / "config" / f"{kind}.json", data)
            else:
                raise SupervisorError("Unknown calibration file kind.")
            return self.calibration_status()

    def _require_lens_idle(self) -> None:
        self._require_offline_idle()
        if self.lens_calibration.active:
            raise SupervisorError("Finish or cancel lens calibration before using the cameras or changing settings.", 409)

    def _require_offline_idle(self) -> None:
        if MAINTENANCE_FILE.exists():
            raise SupervisorError("Reco Field is updating. Try again after the update finishes.", 503)
        if self.offline_stitch.active:
            raise SupervisorError("Finish or cancel the offline panorama export before using the cameras or changing settings.", 409)

    def _lens_capture_config(self, side: str) -> dict[str, Any]:
        if side not in {"left", "right"}:
            raise SupervisorError("Choose the left or right lens.", 422)
        camera = self.settings.camera
        return {"device": camera.left_device if side == "left" else camera.right_device,
            "sensor_mode": camera.sensor_mode, "width": camera.capture_width, "height": camera.capture_height}

    def lens_action(self, side: str, operation: str) -> dict[str, Any]:
        with self._lock:
            self._require_offline_idle()
            self._refresh_process_state()
            if (self.record_state not in {"idle", "failed"} or self._camera
                or (self._session_thread and self._session_thread.is_alive())
                or self.calibration_state in {"starting", "running", "stopping"}
                or self.snapshot_state in {"starting", "running", "stopping"}):
                raise SupervisorError("Stop recording and camera setup before calibrating a lens.", 409)
            config = self._lens_capture_config(side)
            try:
                if operation in {"capture", "solve"}:
                    if self.runtime.simulator:
                        raise ValueError("Lens calibration requires the actual camera and printed board.")
                    self.lens_calibration.start(side, operation, config)
                elif operation == "reset":
                    self.lens_calibration.reset(side)
                elif operation == "apply":
                    self.lens_calibration.apply(side, config)
                else:
                    raise ValueError("Unknown lens operation.")
            except (OSError, ValueError) as exc:
                raise SupervisorError(str(exc), 422) from exc
            return self.status()

    def stop_lens_calibration(self) -> dict[str, Any]:
        self.lens_calibration.stop()
        return self.status()

    def _validated_lens_profile(self, side: str) -> dict[str, Any] | None:
        path = self.runtime.data_dir / "config" / f"{side}-lens.json"
        if side == "right" and not path.exists():
            path = self.runtime.data_dir / "config" / "left-lens.json"
        if not path.exists():
            return None
        profile = validate_lens_profile(path.read_bytes())
        validate_capture_profile(profile, self.settings.camera.capture_width, self.settings.camera.capture_height,
            capture_identity=self._lens_capture_config(side), label=f"{side.title()} lens")
        return profile

    def _check_capture_profiles(self, calibration: dict[str, Any]) -> None:
        for side in ("left", "right"):
            validate_capture_profile(
                calibration[f"{side}_uniforms"],
                self.settings.camera.capture_width,
                self.settings.camera.capture_height,
                capture_identity=self._lens_capture_config(side), label=f"{side.title()} lens",
            )
        for side in ("left", "right"):
            profile = self._validated_lens_profile(side)
            if profile is not None:
                saved = calibration[f"{side}_uniforms"]
                if any(profile[key] != saved[key] for key in ("width", "height", "fx", "fy", "cx", "cy", "d")):
                    raise ValueError("Lens profile changed. Run geometric calibration before recording.")

    def calibration_status(self) -> dict[str, Any]:
        path = self.runtime.calibration_file
        active = self.calibration_state in {"starting", "running", "stopping"}
        elapsed = (
            max(
                0,
                int(
                    (
                        datetime.now(UTC) - self._calibration_started
                    ).total_seconds()
                ),
            )
            if active and self._calibration_started
            else 0
        )
        info: dict[str, Any] = {
            "state": self.calibration_state,
            "error": self.calibration_error,
            "progress": self.calibration_progress,
            "elapsed_seconds": elapsed,
            "valid": False,
            "modified_at": None,
            "left_lens_profile": (
                self.runtime.data_dir / "config" / "left-lens.json"
            ).exists(),
            "right_lens_profile": (
                self.runtime.data_dir / "config" / "right-lens.json"
            ).exists(),
        }
        if path.exists():
            try:
                calibration = validate_calibration(path.read_bytes())
                self._check_capture_profiles(calibration)
                info["valid"] = True
                info["modified_at"] = datetime.fromtimestamp(
                    path.stat().st_mtime, UTC
                ).isoformat()
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                info["error"] = str(exc)
        return info

    def list_recordings(self) -> list[dict[str, Any]]:
        try:
            require_storage(self.runtime.data_dir, self.runtime.storage_mount, self.runtime.storage_uuid)
        except OSError:
            return []
        with self._lock:
            active_path = self.recording_path.resolve() if self.recording_path and self.record_state in {"starting", "running", "stopping"} else None
        result = []
        paths = set()
        for path in self.runtime.recordings_dir.glob("*.mkv"):
            try:
                safe = safe_recording_path(self.runtime.recordings_dir, path.name)
                stat = safe.stat()
            except (OSError, ValueError):
                continue
            paths.add(safe)
            result.append(
                {
                    "name": safe.name,
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime, UTC
                    ).isoformat(),
                    "download_url": f"/api/recordings/{safe.name}",
                    **self.recording_metadata.get(safe, stat, active=safe == active_path),
                }
            )
        self.recording_metadata.retain(paths)
        return sorted(result, key=lambda item: item["modified_at"], reverse=True)

    def recording_file(self, filename: str) -> Path:
        path = safe_recording_path(self.runtime.recordings_dir, filename)
        if not path.is_file():
            raise SupervisorError("Recording not found.", 404)
        if (
            self.record_state in {"starting", "running", "stopping"}
            and self.recording_path
            and path == self.recording_path
        ):
            raise SupervisorError("Stop recording before downloading this file.", 409)
        return path

    def list_sessions(self) -> list[dict[str, Any]]:
        try:
            require_storage(self.runtime.data_dir, self.runtime.storage_mount, self.runtime.storage_uuid)
        except OSError:
            return []
        with self._lock:
            active_dir = self._source_dir
            active_state = self.record_state if self.record_state in {"starting", "running", "stopping"} else None
            can_export = self._capabilities["offline_stitch"] and not self._camera_work_active() and not self.offline_stitch.active
        sessions = []
        for directory in self.runtime.recordings_dir.glob("reco-*"):
            if not directory.is_dir():
                continue
            try:
                item = source_session(self.runtime.recordings_dir, directory.name,
                    active_state if active_dir == directory else None)
                if can_export and item["state"] == "complete":
                    try:
                        validate_stitch_source(self.runtime.recordings_dir, directory.name)
                        item["can_stitch"] = True
                    except (ValueError, OSError):
                        item["stitch_error"] = "This session has no compatible saved calibration and camera timing for offline stitching."
                sessions.append(item)
            except (ValueError, OSError):
                continue
        return sorted(sessions, key=lambda item: item["started_at"] or item["id"], reverse=True)

    def session_file(self, session_id: str, filename: str) -> Path:
        try:
            require_storage(self.runtime.data_dir, self.runtime.storage_mount, self.runtime.storage_uuid)
            path = safe_session_file(self.runtime.recordings_dir, session_id, filename)
            with self._lock:
                if self._source_dir == path.parent and self.record_state in {"starting", "running", "stopping"}:
                    raise SupervisorError("Stop capture and finish the session before downloading its files.", 409)
            session = source_session(self.runtime.recordings_dir, session_id)
        except (ValueError, OSError) as exc:
            raise SupervisorError("Recording session file is unavailable.", 404) from exc
        if session["state"] != "complete":
            raise SupervisorError("This session did not finalize cleanly. Its files are preserved on the SSD.", 409)
        selected = next((item for item in session["outputs"] if item["name"] == filename), None)
        if not selected or not path.is_file():
            raise SupervisorError("Recording session file not found.", 404)
        if not selected["complete"] or not selected["download_url"]:
            raise SupervisorError("This output has not finalized successfully. Original camera files are preserved.", 409)
        return path

    def _camera_work_active(self) -> bool:
        return bool(self._camera or (self._session_thread and self._session_thread.is_alive())
            or self.record_state not in {"idle", "failed"} or self._destination_workers or self._mobile_publisher
            or self.calibration_state in {"starting", "running", "stopping"}
            or self.snapshot_state in {"starting", "running", "stopping"} or self.lens_calibration.active)

    def start_offline_stitch(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            self._require_offline_idle()
            if self._camera_work_active():
                raise SupervisorError("Stop camera capture and setup before creating an offline panorama.", 409)
            if not self._capabilities["offline_stitch"]:
                raise SupervisorError("This Reco build does not support stitching saved camera sessions.", 409)
            self._check_disk()
            try:
                directory = validate_stitch_source(self.runtime.recordings_dir, session_id)
                self.offline_stitch.start(directory, self.settings.camera.model_copy(deep=True), self.settings.min_free_gb)
            except (ValueError, OSError) as exc:
                raise SupervisorError(str(exc), 422) from exc
        return self.status()

    def cancel_offline_stitch(self, session_id: str) -> dict[str, Any]:
        if self.offline_stitch.status()["session_id"] != session_id:
            raise SupervisorError("No export job belongs to this session.", 404)
        self.offline_stitch.cancel()
        return self.status()

    def _write_source_info(self, directory: Path, state: str, error: str = "") -> bool:
        try:
            require_storage(self.runtime.data_dir, self.runtime.storage_mount, self.runtime.storage_uuid)
            safe = safe_session_directory(self.runtime.recordings_dir, directory.name)
            if not safe.is_dir():
                return False
            atomic_write_json(safe / "session.json", {"mode": self._capture_mode, "state": state, "error": error,
                "started_at": self.record_started_at.isoformat() if self.record_started_at else None,
                "panorama": {"width": self.settings.camera.output_width, "height": self.settings.camera.output_height}})
            return True
        except (OSError, ValueError):
            return False

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_process_state()
            now = datetime.now(UTC)
            elapsed = (
                int((now - self.record_started_at).total_seconds())
                if self.record_started_at
                and self.record_state in {"starting", "running", "stopping"}
                else 0
            )
            recording = None
            if self.recording_path:
                try:
                    size = self.recording_path.stat().st_size
                except OSError:
                    size = 0
                recording = {"name": self.recording_path.name, "size": size}
            active = self.record_state in {"starting", "running", "stopping"}
            outputs = []
            if active and self._source_dir:
                try:
                    outputs = source_session(self.runtime.recordings_dir, self._source_dir.name, self.record_state)["outputs"]
                except (ValueError, OSError):
                    pass
            preview = self._capture_kind == "preview"
            return {
                "capabilities": {**self._capabilities, "recording_modes": list(self._capabilities["recording_modes"])},
                "offline_job": self.offline_stitch.status(),
                "device": self.device_telemetry.snapshot(),
                "capture": {"kind": self._capture_kind if self.record_state != "idle" else None,
                            "session_id": self._source_dir.name if active and self._source_dir else None,
                            "state": self.record_state, "error": self.record_error, "mode": self._capture_mode,
                            "elapsed_seconds": elapsed, "outputs": outputs,
                            "bytes": sum(item["size"] for item in outputs) if outputs else (recording["size"] if active and recording else 0),
                            "metrics": dict(self._capture_metrics) if self._capture_metrics
                            and self._capture_metrics["session_id"] == self._session_id
                            and self.record_state in {"starting", "running", "stopping"} else None},
                "record": {
                    "state": "idle" if preview else self.record_state,
                    "error": "" if preview else self.record_error,
                    "elapsed_seconds": 0 if preview else elapsed,
                    "recording": None if preview else recording,
                    "stream_enabled": self.stream_enabled,
                },
                "calibration": self.calibration_status(),
                "snapshots": self.snapshot_status(),
                "lens": self.lens_calibration.status(),
                "relay": {
                    "state": "running" if self._relay_running() else "stopped",
                    "managed": self._relay_managed_by_service,
                },
                "destinations": [
                    worker.status() for worker in self._destination_workers.values()
                ],
                "stream_publisher": self._mobile_publisher.status() if self._mobile_publisher else {
                    "profile": self.settings.stream_profile, "state": "idle", "error": "", "attempt": 0,
                    "video_bitrate": MOBILE_STREAM_BITRATE if self.settings.stream_profile == "mobile" else None,
                },
                "last_recording": self.last_recording,
                "simulator": self.runtime.simulator,
            }

    def preflight(self) -> dict[str, Any]:
        reco = self._command_version(self.runtime.reco_bin, ["--version"])
        ffmpeg = self._command_version(self.runtime.ffmpeg_bin, ["-version"])
        encoders = ""
        if ffmpeg["available"]:
            encoders = self._run_text(
                [self.runtime.ffmpeg_bin, "-hide_banner", "-encoders"]
            )
        argus = self._run_check(
            [self.runtime.gst_inspect_bin, "nvarguscamerasrc"]
        )
        gpu = (
            self._run_check([self.runtime.reco_bin, "info"])
            if reco["available"]
            else {"ok": False}
        )
        devices = sorted(
            str(path)
            for path in Path("/dev").glob("video*")
            if path.name[5:].isdigit()
        )
        free = shutil.disk_usage(self.runtime.data_dir)
        relay_ready = self._relay_running() or (
            self.runtime.manage_relay
            and self._executable(self.runtime.mediamtx_bin)
        )
        checks = {
            "reco": {
                "state": "ready" if reco["available"] else "failed",
                "detail": reco["version"] or "Reco binary not found",
            },
            "ffmpeg": {
                "state": (
                    "ready"
                    if ffmpeg["available"] and "libx264" in encoders
                    else "failed"
                ),
                "detail": ffmpeg["version"] or "FFmpeg binary not found",
                "libx264": "libx264" in encoders,
                "h264_v4l2m2m": "h264_v4l2m2m" in encoders,
            },
            "camera": {
                "state": (
                    "ready"
                    if argus["ok"] and len(devices) >= 2
                    else "failed"
                ),
                "detail": (
                    f"{len(devices)} V4L2 device(s); nvarguscamerasrc "
                    f"{'available' if argus['ok'] else 'missing'}"
                ),
                "devices": devices,
                "argus": argus["ok"],
            },
            "vulkan": {
                "state": "ready" if gpu["ok"] else "failed",
                "detail": (
                    "Reco GPU initialization succeeded"
                    if gpu["ok"]
                    else "Reco GPU initialization failed"
                ),
            },
            "disk": {
                "state": "ready"
                if free.free >= self.settings.min_free_gb * 1024**3
                else "failed",
                "free_gb": round(free.free / 1024**3, 2),
                "required_gb": self.settings.min_free_gb,
            },
            "relay": {
                "state": "ready" if relay_ready else "failed",
                "detail": "loopback MediaMTX",
            },
            "tracking": {
                "state": "unavailable",
                "detail": "No compatible model configured; AI tracking is disabled.",
            },
        }
        if self.runtime.simulator:
            checks["reco"] = {
                "state": "simulated",
                "detail": "Synthetic FFmpeg publisher is active.",
            }
            checks["camera"] = {
                "state": "simulated",
                "detail": "Explicit simulator mode; hardware capture is disabled.",
                "devices": [],
                "argus": False,
            }
            checks["vulkan"] = {
                "state": "simulated",
                "detail": "Synthetic FFmpeg mode does not initialize Reco GPU.",
            }
        ready = all(
            checks[key]["state"] in {"ready", "simulated"}
            for key in ("reco", "ffmpeg", "camera", "vulkan", "disk", "relay")
        )
        return {
            "ready": ready,
            "simulator": self.runtime.simulator,
            "platform": {
                "machine": platform.machine(),
                "release": platform.release(),
                "l4t": self._read_text(Path("/etc/nv_tegra_release")),
            },
            "checks": checks,
        }

    def shutdown(self) -> None:
        self.offline_stitch.cancel()
        self.device_telemetry.stop()
        self.recording_metadata.close()
        self.lens_calibration.stop()
        if (self.record_state in {"starting", "running", "stopping"} or self._camera
            or self._mobile_publisher or self._destination_workers):
            self._stop_capture()
        if self.calibration_state in {"starting", "running", "stopping"}:
            self.stop_calibration()
        if self.snapshot_state in {"starting", "running", "stopping"}:
            self.stop_snapshots()
        if self._relay and self._relay_managed_by_service:
            self._relay.stop(grace_seconds=5)
            self._relay = None

    def _camera_argv(self, output: Path | None, *, source_dir: Path | None = None) -> list[str]:
        camera = self.settings.camera
        if self.runtime.simulator:
            return [
                self.runtime.ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "warning",
                "-progress",
                "pipe:1",
                "-nostats",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=1280x720:rate=30",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-c:a",
                "aac",
                "-shortest",
                "-f",
                "matroska" if output else "flv",
                str(output) if output else self.runtime.relay_rtmp_url,
            ]
        width, height = camera.output_width, camera.output_height
        if source_dir and output is None:
            width, height = self._scaled_size(width, height, 1536, 432, width_multiple=4)
        argv = [
            self.runtime.reco_bin,
            "camera",
            "--left-device",
            camera.left_device,
            "--right-device",
            camera.right_device,
            "--calibration",
            str(self.runtime.calibration_file),
            "--capture-width",
            str(camera.capture_width),
            "--capture-height",
            str(camera.capture_height),
            "--capture-fps",
            str(camera.capture_fps),
            "--width",
            str(width),
            "--height",
            str(height),
            "--projection",
            camera.projection,
            "--encoder",
            camera.encoder,
            "--codec",
            camera.codec,
            "--quality",
            camera.quality,
            "--preset",
            camera.preset,
            "--blend",
            str(camera.blend),
            "--container",
            "mkv",
            "--stream-url",
            self.runtime.relay_rtmp_url,
            *self._camera_control_args(camera),
        ]
        argv.extend(["--output", str(output)] if output else ["--no-panorama-file"])
        if source_dir:
            source_width, source_height = self._scaled_size(camera.capture_width, camera.capture_height, 2016, 1520)
            argv.extend(["--source-dir", str(source_dir), "--source-size", f"{source_width}x{source_height}", "--source-crf", "18"])
            if output is None:
                argv.extend(["--preview-fps", str(min(10, camera.capture_fps))])
        return argv

    @staticmethod
    def _scaled_size(width: int, height: int, max_width: int, max_height: int, *, width_multiple: int = 2) -> tuple[int, int]:
        divisor = math.gcd(width, height)
        unit_width, unit_height = width // divisor, height // divisor
        factor = min(divisor, max_width // unit_width, max_height // unit_height)
        while factor > 0 and (factor * unit_width % width_multiple or factor * unit_height % 2):
            factor -= 1
        if factor <= 0:
            raise SupervisorError("Camera dimensions cannot be scaled safely for separate recording.", 409)
        return unit_width * factor, unit_height * factor

    def _calibration_argv(self, output: Path) -> list[str]:
        camera = self.settings.camera
        argv = [
            self.runtime.reco_bin,
            "camera",
            "--left-device",
            camera.left_device,
            "--right-device",
            camera.right_device,
            "--calibration",
            str(output),
            "--output",
            str(self.runtime.temp_dir / "calibration-video.mkv"),
            "--capture-width",
            str(camera.capture_width),
            "--capture-height",
            str(camera.capture_height),
            "--capture-fps",
            str(camera.capture_fps),
            "--live-calibrate",
            "--calibrate-frames",
            str(self.settings.calibration_frames),
            *self._camera_control_args(camera),
        ]
        for side in ("left", "right"):
            lens = self.runtime.data_dir / "config" / f"{side}-lens.json"
            if lens.exists():
                argv.extend([f"--{side}-lens-profile", str(lens)])
        return argv

    @staticmethod
    def _camera_control_args(camera: CameraSettings) -> list[str]:
        args = [
            "--sensor-mode",
            str(camera.sensor_mode),
            "--auto-exposure",
            "true" if camera.exposure_mode == "auto" else "false",
            "--exposure-range",
            f"{camera.exposure_min_ns},{camera.exposure_max_ns}",
            "--gain-range",
            f"{camera.gain_min},{camera.gain_max}",
            "--digital-gain-range",
            f"{camera.digital_gain_min},{camera.digital_gain_max}",
            "--awb-mode",
            camera.awb_mode,
        ]
        if camera.awb_lock:
            args.append("--lock-awb")
        return args

    def _on_camera_line(self, session_id: str | None, line: str) -> None:
        progress = re.fullmatch(r"Processed ([1-9]\d{0,19}) frames \(\d+(?:\.\d+)? / (\d+(?:\.\d+)?) fps\)", line.strip())
        if (
            "Warmup complete, starting capture" not in line
            and not re.match(r"^frame=\d+", line)
            and not re.match(r"^Processed [1-9]\d* frames", line)
        ):
            return
        with self._lock:
            if (
                session_id is None
                or session_id != self._session_id
                or self._camera is None
                or self._stop_requested
            ):
                return
            if progress:
                frames, fps = int(progress.group(1)), float(progress.group(2))
                if (0 <= fps <= 10000 and (not self._capture_metrics
                    or self._capture_metrics["session_id"] != session_id
                    or frames > self._capture_metrics["processed_frames"])):
                    self._capture_metrics = {"sampled_at": datetime.now(UTC).isoformat(),
                        "session_id": session_id, "processed_frames": frames, "fps": fps}
            if self._camera_marker_seen:
                return
            self._camera_marker_seen = True
            self.record_state = "running"
            if self.runtime.simulator and self._capture_kind != "preview":
                self._start_simulator_publisher()
            if self.stream_enabled:
                self._start_streaming(session_id)

    def _monitor_session(
        self,
        session_id: str,
        camera: Any,
        recording_path: Path | None,
        source_dir: Path | None = None,
    ) -> None:
        deadline = time.monotonic() + self.runtime.camera_start_timeout_seconds
        startup_timed_out = False
        low_disk_error = ""
        disk_check_interval = max(0.05, self.runtime.recording_disk_check_seconds)
        next_disk_check = time.monotonic()
        source_info_written = False
        while camera.poll() is None:
            with self._lock:
                if self._session_id != session_id:
                    return
                marker_seen = self._camera_marker_seen
                stop_requested = self._stop_requested
            if source_dir and not source_info_written:
                source_info_written = self._write_source_info(source_dir, "recording")
            if stop_requested:
                break
            if not marker_seen:
                if time.monotonic() >= deadline:
                    startup_timed_out = True
                    with self._lock:
                        if self._session_id == session_id:
                            self.record_state = "stopping"
                            self.record_error = (
                                "Camera capture did not become ready within "
                                f"{self.runtime.camera_start_timeout_seconds}s."
                            )
                    camera.stop(grace_seconds=16 if source_dir else 12)
                    break
            elif time.monotonic() >= next_disk_check:
                next_disk_check = time.monotonic() + disk_check_interval
                if disk_error := self._recording_low_disk_error():
                    low_disk_error = disk_error
                    with self._lock:
                        if self._session_id == session_id:
                            self.record_state = "stopping"
                            self.record_error = disk_error
                    camera.stop(grace_seconds=16 if source_dir else 12)
                    break
            time.sleep(min(0.2, disk_check_interval))
        code = camera.wait()
        with self._lock:
            if self._session_id != session_id:
                return
            self.record_state = "stopping"
            workers = list(self._destination_workers.values())
            mobile = self._mobile_publisher
            publisher = self._simulator_publisher
        cleanup_complete = self._stop_stream_workers(workers, mobile)
        if publisher:
            publisher.stop(grace_seconds=3)
        with self._lock:
            if self._session_id != session_id:
                return
            try:
                size = recording_path.stat().st_size if recording_path else 0
            except OSError:
                size = 0
            recording_complete = (
                size > 0
                and self._camera_marker_seen
                and (code == 0 or self._stop_requested or bool(low_disk_error))
            )
            if low_disk_error:
                self.record_state = "failed"
                self.record_error = low_disk_error
            elif startup_timed_out:
                self.record_state = "failed"
                self.record_error = (
                    "Camera capture did not become ready within "
                    f"{self.runtime.camera_start_timeout_seconds}s."
                )
            elif self._stop_requested and self._camera_marker_seen:
                self.record_state = "idle"
                self.record_error = ""
            elif code == 0 and self._camera_marker_seen:
                self.record_state = "idle"
                self.record_error = ""
            else:
                self.record_state = "failed"
                prefix = (
                    "Reco exited before camera capture became ready."
                    if not self._camera_marker_seen
                    else f"Reco exited with status {code}."
                )
                self.record_error = f"{prefix} Last output: {self._log_tail(camera.log.path)}"
            if source_dir:
                final_error = self.record_error or (f"Capture exited with status {code}." if code != 0 else "")
                saved = self._write_source_info(source_dir, "complete" if code == 0 and self._camera_marker_seen else "incomplete", final_error)
                try:
                    session = source_session(self.runtime.recordings_dir, source_dir.name)
                    size, recording_complete = session["size"], saved and session["state"] == "complete"
                    final_error = final_error or session["error"]
                except (OSError, ValueError):
                    recording_complete = False
                if not recording_complete:
                    self.record_state = "failed"
                    self.record_error = final_error or "Separate camera files did not finalize cleanly. The session is preserved on the SSD."
                self.last_recording = {"name": source_dir.name, "session_id": source_dir.name, "size": size,
                    "state": "complete" if recording_complete else "failed"}
            elif recording_path:
                self.last_recording = {
                    "name": recording_path.name,
                    "size": size,
                    "state": "complete" if recording_complete else "failed",
                }
            if not cleanup_complete:
                self.record_state = "stopping"
                self.record_error = (
                    f"{self.record_error} Stream cleanup did not finish. Press Stop to retry; the recording file is preserved."
                ).strip()
            if self._session_id == session_id and self._camera is camera:
                self._simulator_publisher = None
                self._camera = None
                self._session_id = None
                if self._session_thread is threading.current_thread():
                    self._session_thread = None

    def _stop_stream_workers(self, workers: list[DestinationWorker], mobile: MobileStreamWorker | None) -> bool:
        all_workers = [*workers, *([mobile] if mobile else [])]
        for worker in all_workers:
            worker.request_stop()
        complete = True
        for worker in all_workers:
            try:
                stopped = worker.stop()
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                worker.state, worker.error = "failed", "Stream cleanup did not finish cleanly."
                stopped = False
            if not stopped:
                complete = False
                continue
            with self._lock:
                if self._mobile_publisher is worker:
                    self._mobile_publisher = None
                if worker is not mobile and self._destination_workers.get(worker.destination.id) is worker:
                    del self._destination_workers[worker.destination.id]
        return complete

    def _start_streaming(self, session_id: str | None) -> None:
        if (session_id != self._session_id or self._stop_requested or not self._camera_marker_seen
            or not self.stream_enabled or not any(d.enabled and d.url for d in self.settings.destinations)):
            return
        if self.settings.stream_profile == "original":
            self._start_destinations(self.runtime.relay_rtmp_url)
        elif self._mobile_publisher is None:
            publisher_fps = min(10, self.settings.camera.capture_fps) if self._capture_mode == "sources" else self.settings.camera.capture_fps
            publisher = MobileStreamWorker(self.runtime, publisher_fps,
                lambda: self._on_mobile_publisher_status(session_id), self.process_factory)
            self._mobile_publisher = publisher
            try:
                publisher.start()
            except (OSError, RuntimeError) as exc:
                publisher.state, publisher.error = "failed", f"Cannot start mobile publisher: {exc}"

    def _on_mobile_publisher_status(self, session_id: str | None) -> None:
        with self._lock:
            if (session_id != self._session_id or self._stop_requested or not self.stream_enabled
                or self._mobile_publisher is None or self._camera is None or self.record_state != "running"):
                return
            if self._mobile_publisher.state == "running":
                self._start_destinations(self.runtime.relay_mobile_rtmp_url)

    def _start_destinations(self, input_url: str) -> None:
        if self._stop_requested or not self.stream_enabled or not self._camera_marker_seen:
            return
        enabled = [
            item
            for item in self.settings.destinations
            if item.enabled and item.url
        ]
        for destination in enabled:
            if destination.id in self._destination_workers:
                continue
            worker = DestinationWorker(
                destination,
                self.runtime,
                self._destination_status_changed,
                self.process_factory,
                input_url=input_url,
            )
            self._destination_workers[destination.id] = worker
            try:
                worker.start()
            except (OSError, RuntimeError) as exc:
                worker.state, worker.error = "failed", f"Cannot start destination worker: {exc}"

    def _start_simulator_publisher(self) -> None:
        if self._simulator_publisher and self._simulator_publisher.is_running():
            return
        self._simulator_publisher = self.process_factory(
            [
                self.runtime.ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "warning",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=1280x720:rate=30",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
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
                self.runtime.relay_rtmp_url,
            ],
            self.runtime.logs_dir / "relay-publisher.log",
            log_max_bytes=self.runtime.log_max_bytes,
            log_backups=self.runtime.log_backups,
        )
        self._simulator_publisher.start()

    def _destination_status_changed(self) -> None:
        # Status is polled by the UI; this callback intentionally avoids I/O.
        pass

    def _on_calibration_line(self, line: str) -> None:
        with self._lock:
            if line.startswith('{"status":"ok"') or line.startswith('{"confidence":'):
                try:
                    result = json.loads(line)
                    confidence = float(result["confidence"]) * 100
                    self.calibration_progress = (
                        f"Calibration result: {int(result['matches'])} matches, {confidence:.0f}% confidence."
                        + (" Improve overlap and recalibrate before filming." if confidence < 50 else "")
                    )
                    return
                except (ValueError, KeyError, TypeError):
                    pass
            if "Live calibration:" in line or "warming up" in line.lower():
                self.calibration_state = "running"
            if line.startswith("  ["):
                self.calibration_progress = line.strip()[:240]
            if "confidence" in line.lower():
                self.calibration_progress = line.strip()[:240]

    def _monitor_calibration(self) -> None:
        process = self._calibration
        if not process:
            return
        try:
            code = process.wait(timeout=self.runtime.calibration_timeout_seconds)
        except subprocess.TimeoutExpired:
            process.stop(grace_seconds=5)
            code = -1
            error = (
                "Calibration exceeded the "
                f"{self.runtime.calibration_timeout_seconds}s deadline. "
                f"Last output: {self._log_tail(process.log.path)}"
            )
        else:
            error = (
                f"Calibration exited with status {code}. "
                f"Last output: {self._log_tail(process.log.path)}"
            )
        self._finish_calibration(code, error)

    def _finish_calibration(self, code: int, error: str) -> None:
        with self._lock:
            if self.calibration_state == "stopping":
                self.calibration_state = "idle"
            elif code == 0 and self._calibration_temp:
                try:
                    data = self._calibration_temp.read_bytes()
                    data = self._preserve_calibration_metadata(data)
                    validate_calibration(data)
                    atomic_write(self.runtime.calibration_file, data)
                    self.calibration_state = "complete"
                    self.calibration_error = ""
                    if self.calibration_progress.startswith("Calibration result:"):
                        self.calibration_progress += " Saved."
                    else:
                        self.calibration_progress = "Calibration validated and saved"
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    self.calibration_state = "failed"
                    self.calibration_error = f"Calibration output rejected: {exc}"
            else:
                self.calibration_state = "failed"
                self.calibration_error = error
            if self._calibration_temp and self._calibration_temp.exists():
                self._calibration_temp.unlink()
            self._calibration = None

    def _preserve_calibration_metadata(self, data: bytes) -> bytes:
        previous = self.runtime.calibration_file
        if not previous.exists():
            return data
        try:
            old_value = validate_calibration(previous.read_bytes())
            new_value = validate_calibration(data)
        except (OSError, ValueError, json.JSONDecodeError):
            return data
        changed = False
        for key in ("rig_tilt", "rig_roll", "field_roi"):
            if key in old_value and key not in new_value:
                new_value[key] = old_value[key]
                changed = True
        if not changed:
            return data
        return (json.dumps(new_value, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )

    def _simulate_calibration(self) -> None:
        time.sleep(1)
        with self._lock:
            if self.calibration_state == "stopping":
                self.calibration_state = "idle"
            else:
                self.calibration_state = "failed"
                self.calibration_error = (
                    "Simulator mode cannot perform stereo calibration. "
                    "Import a calibration to test recording."
                )

    def snapshot_status(self) -> dict[str, Any]:
        sides: dict[str, dict[str, str] | None] = {}
        for side in ("left", "right"):
            path = self.runtime.snapshots_dir / f"{side}.jpg"
            if path.is_file() and not path.is_symlink():
                sides[side] = {
                    "url": f"/api/snapshots/{side}.jpg",
                    "modified_at": datetime.fromtimestamp(
                        path.stat().st_mtime, UTC
                    ).isoformat(),
                }
            else:
                sides[side] = None
        return {
            "state": self.snapshot_state,
            "error": self.snapshot_error,
            "progress": self.snapshot_progress,
            "left": sides["left"],
            "right": sides["right"],
        }

    def snapshot_file(self, side: str) -> Path:
        if side not in {"left", "right"}:
            raise SupervisorError("Snapshot not found.", 404)
        path = self.runtime.snapshots_dir / f"{side}.jpg"
        if not path.is_file() or path.is_symlink():
            raise SupervisorError("Snapshot not found.", 404)
        return path

    def _capture_snapshots(self) -> None:
        camera = self.settings.camera
        sides = (
            ("left", camera.left_device),
            ("right", camera.right_device),
        )
        try:
            for side, device in sides:
                with self._lock:
                    if self.snapshot_state == "stopping":
                        return
                    self.snapshot_state = "running"
                    self.snapshot_progress = f"Capturing {side} camera"
                if self.runtime.simulator:
                    self._capture_simulated_snapshot(side)
                    continue
                pattern = self.runtime.snapshots_dir / f".{side}-%03d.jpg"
                for stale in self.runtime.snapshots_dir.glob(f".{side}-*.jpg"):
                    stale.unlink(missing_ok=True)
                process = self.process_factory(
                    self._snapshot_argv(device, pattern),
                    self.runtime.logs_dir / f"snapshot-{side}.log",
                    log_max_bytes=self.runtime.log_max_bytes,
                    log_backups=self.runtime.log_backups,
                )
                self._snapshot_process = process
                process.start()
                code = process.wait(timeout=60)
                self._snapshot_process = None
                if code != 0:
                    raise RuntimeError(
                        f"{side} snapshot capture exited with status {code}: "
                        f"{self._log_tail(process.log.path)}"
                    )
                candidates = sorted(
                    self.runtime.snapshots_dir.glob(f".{side}-*.jpg"),
                    key=lambda path: path.name,
                )
                if not candidates:
                    raise RuntimeError(f"{side} snapshot produced no JPEG frame.")
                os.replace(candidates[-1], self.runtime.snapshots_dir / f"{side}.jpg")
                for candidate in candidates[:-1]:
                    candidate.unlink(missing_ok=True)
            with self._lock:
                if self.snapshot_state != "stopping":
                    self.snapshot_state = "complete"
                    self.snapshot_progress = "Both camera snapshots ready"
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            with self._lock:
                if self.snapshot_state == "stopping":
                    self.snapshot_state = "idle"
                else:
                    self.snapshot_state = "failed"
                    self.snapshot_error = str(exc)
            if self._snapshot_process:
                self._snapshot_process.stop(grace_seconds=2)
                self._snapshot_process = None

    def _snapshot_argv(self, device: str, pattern: Path, *, full_resolution: bool = False) -> list[str]:
        camera = self.settings.camera
        snapshot_width = camera.capture_width if full_resolution else 960
        snapshot_height = camera.capture_height if full_resolution else max(2, round(960 * camera.capture_height / camera.capture_width / 2) * 2)
        controls = [
            f"sensor-id={device}",
            f"sensor-mode={camera.sensor_mode}",
            f"exposuretimerange={camera.exposure_min_ns} {camera.exposure_max_ns}",
            f"gainrange={camera.gain_min} {camera.gain_max}",
            f"ispdigitalgainrange={camera.digital_gain_min} {camera.digital_gain_max}",
            f"wbmode={self._awb_number(camera.awb_mode)}",
            "num-buffers=30",
        ]
        if camera.exposure_mode == "manual":
            controls.append("aelock=true")
        if camera.awb_lock:
            controls.append("awblock=true")
        return [
            self.runtime.gst_launch_bin,
            "-e",
            "nvarguscamerasrc",
            *controls,
            "!",
            (
                "video/x-raw(memory:NVMM),"
                f"width={camera.capture_width},height={camera.capture_height},"
                f"format=NV12,framerate={camera.capture_fps}/1"
            ),
            "!",
            *(["queue", "max-size-buffers=1", "max-size-bytes=0", "max-size-time=0", "leaky=downstream", "!"] if full_resolution else []),
            "nvvidconv",
            "!",
            f"video/x-raw,format=I420,width={snapshot_width},height={snapshot_height}",
            "!",
            "videoconvert",
            "!",
            "jpegenc",
            *(["quality=95"] if full_resolution else []),
            "!",
            "multifilesink",
            f"location={pattern}",
            "max-files=1",
        ]

    def _capture_simulated_snapshot(self, side: str) -> None:
        output = self.runtime.snapshots_dir / f".{side}.jpg"
        process = self.process_factory(
            [
                self.runtime.ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=960x540:rate=1:duration=1",
                "-frames:v",
                "1",
                "-y",
                str(output),
            ],
            self.runtime.logs_dir / f"snapshot-{side}.log",
            log_max_bytes=self.runtime.log_max_bytes,
            log_backups=self.runtime.log_backups,
        )
        self._snapshot_process = process
        process.start()
        code = process.wait(timeout=15)
        self._snapshot_process = None
        if code != 0:
            raise RuntimeError(f"Simulated {side} snapshot exited with status {code}.")
        os.replace(output, self.runtime.snapshots_dir / f"{side}.jpg")

    @staticmethod
    def _awb_number(mode: str) -> int:
        return {
            "off": 0,
            "auto": 1,
            "incandescent": 2,
            "fluorescent": 3,
            "tungsten": 4,
            "daylight": 5,
            "cloudy": 6,
            "twilight": 7,
            "shade": 8,
            "warm-fluorescent": 9,
        }[mode]

    @staticmethod
    def _log_tail(path: Path, limit: int = 400) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "no process log"
        text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)
        text = "".join(
            character
            for character in text
            if character in "\n\t" or ord(character) >= 32
        )
        text = " ".join(text.split())
        return text[-limit:] or "no process output"

    def _check_disk(self) -> None:
        try:
            require_storage(self.runtime.data_dir, self.runtime.storage_mount, self.runtime.storage_uuid)
        except OSError as exc:
            raise SupervisorError(str(exc), 507) from exc
        free = self.disk_usage(self.runtime.data_dir).free
        required = int(self.settings.min_free_gb * 1024**3)
        if free < required:
            raise SupervisorError(
                f"Recording refused: {free / 1024**3:.1f} GiB free; "
                f"{self.settings.min_free_gb:.1f} GiB required.",
                507,
            )

    def _recording_low_disk_error(self) -> str:
        try:
            require_storage(self.runtime.data_dir, self.runtime.storage_mount, self.runtime.storage_uuid)
            free = self.disk_usage(self.runtime.data_dir).free
        except OSError as exc:
            return f"Recording stopped because free space could not be checked: {exc}"
        required = int(self.settings.min_free_gb * 1024**3)
        if free < required:
            return (
                "Recording stopped because free space fell below "
                f"{self.settings.min_free_gb:.1f} GiB "
                f"({free / 1024**3:.1f} GiB remaining)."
            )
        return ""

    def _ensure_relay(self) -> None:
        if self._relay_running():
            return
        if not self.runtime.manage_relay:
            raise SupervisorError(
                f"MediaMTX relay is not reachable on loopback port {self.runtime.relay_rtmp_port}.", 503
            )
        if not self._executable(self.runtime.mediamtx_bin):
            raise SupervisorError("MediaMTX binary is unavailable.", 503)
        self._relay = self.process_factory(
            [self.runtime.mediamtx_bin, str(self.runtime.mediamtx_config)],
            self.runtime.logs_dir / "relay.log",
            log_max_bytes=self.runtime.log_max_bytes,
            log_backups=self.runtime.log_backups,
        )
        try:
            self._relay.start()
        except OSError as exc:
            raise SupervisorError(f"Cannot start MediaMTX: {exc}", 503) from exc
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self._port_open(self.runtime.relay_rtmp_port):
                return
            if not self._relay.is_running():
                break
            time.sleep(0.2)
        self._relay.stop(grace_seconds=2)
        self._relay = None
        raise SupervisorError(
            "MediaMTX did not open the loopback RTMP listener.", 503
        )

    def _relay_running(self) -> bool:
        return self._port_open(self.runtime.relay_rtmp_port)

    @staticmethod
    def _port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return True
        except OSError:
            return False

    @staticmethod
    def _executable(value: str) -> bool:
        return Path(value).is_file() if os.path.sep in value else shutil.which(value) is not None

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _command_version(self, command: str, args: list[str]) -> dict[str, Any]:
        if not self._executable(command):
            return {"available": False, "version": ""}
        try:
            output = subprocess.run(
                [command, *args],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"available": False, "version": ""}
        text = (output.stdout or output.stderr).strip().splitlines()
        return {
            "available": output.returncode == 0,
            "version": text[0][:240] if text else "",
        }

    @staticmethod
    def _run_text(argv: list[str]) -> str:
        try:
            return subprocess.run(
                argv, capture_output=True, text=True, timeout=10, check=False
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    @staticmethod
    def _run_check(argv: list[str]) -> dict[str, Any]:
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=10, check=False
            )
            return {"ok": result.returncode == 0}
        except (OSError, subprocess.TimeoutExpired):
            return {"ok": False}

    def _refresh_process_state(self) -> None:
        if self._camera and self.record_state in {"starting", "running"}:
            code = self._camera.poll()
            if code is not None:
                self.record_state = "stopping"
                self.record_error = f"Reco exited with status {code}; finalizing recording."
