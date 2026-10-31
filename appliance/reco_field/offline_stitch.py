from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CameraSettings, RuntimeConfig
from .processes import ManagedProcess
from .storage import atomic_write_json, require_storage, safe_session_directory, safe_session_file
from .capture_sessions import _object


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OfflineStitch:
    def __init__(self, runtime: RuntimeConfig, probe: Any, process_factory: Any = ManagedProcess) -> None:
        self.runtime, self.probe, self.process_factory = runtime, probe, process_factory
        self._lock = threading.RLock()
        self._cleanup_lock = threading.Lock()
        self._cancel = threading.Event()
        self._process: Any = None
        self._thread: threading.Thread | None = None
        self._metadata: Path | None = None
        self._last_error = ""
        self._job: dict[str, Any] = {"id": None, "session_id": None, "state": "idle", "error": "",
            "processed_frames": None, "fps": None, "started_at": None, "finished_at": None, "output": None, "sync": None}
        pointer = _object(runtime.data_dir / "config/offline-job.json")
        try:
            directory = safe_session_directory(runtime.recordings_dir, pointer.get("session_id", ""))
            output = safe_session_file(runtime.recordings_dir, directory.name, pointer.get("name", ""))
            if not output.name.startswith("export-"):
                return
            saved = _object(output.with_suffix(".json"))
            if (saved.get("id") != output.stem or saved.get("session_id") != directory.name
                or not isinstance(saved.get("state"), str)
                or saved["state"] not in {"starting", "running", "stopping", "complete", "failed", "cancelled", "interrupted"}
                or not isinstance(saved.get("output"), dict) or saved["output"].get("name") != output.name):
                return
            self._job, self._metadata = {**self._job, **saved}, output.with_suffix(".json")
            if self._job.get("state") in {"starting", "running", "stopping"}:
                self._job.update(state="interrupted", error="Export was interrupted by an application restart. Original camera files are preserved.", finished_at=_now())
                self._persist()
        except (OSError, ValueError, TypeError):
            pass

    @property
    def active(self) -> bool:
        with self._lock:
            return self._process is not None or bool(self._thread and self._thread.is_alive()) or self._job["state"] in {"starting", "running", "stopping"}

    def status(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._job)
            if result.get("output"):
                result["output"] = dict(result["output"])
                if self._metadata:
                    try:
                        result["output"]["size"] = self._metadata.with_suffix(".mkv").stat().st_size
                    except OSError:
                        pass
            return result

    def _persist(self) -> None:
        require_storage(self.runtime.data_dir, self.runtime.storage_mount, self.runtime.storage_uuid)
        if self._metadata:
            atomic_write_json(self._metadata, self._job)

    def start(self, directory: Path, camera: CameraSettings, reserve_gb: float) -> None:
        with self._lock:
            if self.active:
                raise ValueError("An offline export is already active.")
            require_storage(self.runtime.data_dir, self.runtime.storage_mount, self.runtime.storage_uuid)
            name = f"export-{uuid.uuid4().hex}.mkv"
            output = safe_session_file(self.runtime.recordings_dir, directory.name, name)
            metadata = output.with_suffix(".json")
            if output.exists() or metadata.exists():
                raise ValueError("Export destination already exists; no files were changed.")
            self._metadata = metadata
            self._cancel.clear()
            self._last_error = ""
            self._job = {"id": output.stem, "session_id": directory.name, "state": "starting", "error": "",
                "processed_frames": None, "fps": None, "started_at": _now(), "finished_at": None, "sync": None,
                "exit_code": None, "output": {"name": name, "size": 0, "width": None, "height": None,
                    "codec": None, "duration_seconds": None, "download_url": None}}
            try:
                self._persist()
                atomic_write_json(self.runtime.data_dir / "config/offline-job.json", {"session_id": directory.name, "name": name})
            except OSError:
                self._job.update(state="failed", error="Could not save export metadata on the SSD.", finished_at=_now())
                raise
            argv = [self.runtime.reco_bin, "stitch", str(directory / "left.mkv"), str(directory / "right.mkv"),
                "-c", str(directory / "calibration.json"), "--projection", "panorama", "--source-manifest", str(directory / "manifest.json"),
                "-o", str(output), "--container", "mkv", "--width", str(camera.output_width), "--height", str(camera.output_height),
                "--encoder", "libx264", "--codec", "h264", "--quality-value", "80", "--preset", "veryfast", "--blend", str(camera.blend)]
            self._thread = threading.Thread(target=self._run, args=(output, argv, camera.output_width, camera.output_height, reserve_gb), daemon=True)
            try:
                self._thread.start()
            except RuntimeError:
                self._job.update(state="failed", error="Could not start the export worker.", finished_at=_now())
                self._persist()
                raise ValueError(self._job["error"])

    def cancel(self) -> None:
        with self._lock:
            if not self.active:
                return
            if self._job["state"] not in {"starting", "running", "stopping"} and (not self._process or not self._process.is_running()):
                return
            self._cancel.set()
            self._job["state"] = "stopping"
            process, thread = self._process, self._thread
        if process:
            self._stop_process(process)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._lock:
            if (not self._thread or not self._thread.is_alive()) and (not self._process or not self._process.is_running()):
                self._process = None
                if self._job["state"] == "stopping":
                    self._job.update(state="cancelled", error="", finished_at=_now())
                    try:
                        self._persist()
                    except OSError:
                        self._job.update(state="failed", error="Export stopped, but its final status could not be saved on the SSD.")

    def _stop_process(self, process: Any) -> bool:
        with self._cleanup_lock:
            try:
                process.stop(grace_seconds=10)
                if process.is_running():
                    raise RuntimeError("Process remains active")
                return True
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                with self._lock:
                    self._job.update(state="stopping", error="Export cleanup did not finish. Press Cancel again; original files are preserved.")
                return False

    def _on_line(self, job_id: str, line: str) -> None:
        progress = re.fullmatch(r"Processed ([1-9]\d{0,19}) frames \(\d+(?:\.\d+)? / (\d+(?:\.\d+)?) fps\)", line.strip())
        with self._lock:
            if job_id != self._job["id"] or self._job["state"] not in {"starting", "running"}:
                return
            error = re.search(r"(?:^|\s)(?:Error|error):\s*(.+)", line)
            if error:
                self._last_error = error.group(1)[:500]
            if progress:
                frames, fps = int(progress.group(1)), float(progress.group(2))
                if frames > (self._job["processed_frames"] or 0) and 0 <= fps <= 10000:
                    self._job.update(processed_frames=frames, fps=fps)
            if line.startswith("Source sync: ") and len(line) < 4096:
                try:
                    sync = json.loads(line[len("Source sync: "):])
                    keys = {"output_frames", "left_duplicates", "right_duplicates", "left_skipped", "right_skipped"}
                    if isinstance(sync, dict) and set(sync) == keys and all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in sync.values()):
                        self._job.update(sync=sync, processed_frames=sync["output_frames"])
                except ValueError:
                    pass

    def _run(self, output: Path, argv: list[str], width: int, height: int, reserve_gb: float) -> None:
        failure, process, code = "", None, None
        try:
            process = self.process_factory(argv, self.runtime.logs_dir / "offline-stitch.log",
                on_line=lambda line: self._on_line(output.stem, line),
                env={**os.environ, "RECO_NO_HWACCEL": "1"},
                log_max_bytes=self.runtime.log_max_bytes, log_backups=self.runtime.log_backups)
            with self._lock:
                if self._cancel.is_set():
                    self._job.update(state="cancelled", finished_at=_now())
                    self._persist()
                    return
                self._process = process
                process.start()
                self._job["state"] = "running"
                self._persist()
            next_disk_check, stop_attempted = 0.0, False
            while process.poll() is None:
                if time.monotonic() >= next_disk_check:
                    next_disk_check = time.monotonic() + 5
                    try:
                        require_storage(self.runtime.data_dir, self.runtime.storage_mount, self.runtime.storage_uuid)
                        if shutil.disk_usage(self.runtime.data_dir).free < reserve_gb * 1024**3:
                            failure = "Export stopped because SSD free space reached the configured reserve."
                    except OSError:
                        failure = "Export stopped because the recording SSD became unavailable."
                if (self._cancel.is_set() or failure) and not stop_attempted:
                    stop_attempted = True
                    self._stop_process(process)
                time.sleep(.2)
            code = process.wait()
            metadata, verified_stat = None, None
            if code == 0 and not failure and not self._cancel.is_set():
                verified_stat = output.stat()
                metadata = self.probe(output, verified_stat)
                after = output.stat()
                if (after.st_size, after.st_mtime_ns) != (verified_stat.st_size, verified_stat.st_mtime_ns):
                    failure = "Export changed while it was being verified. Original files are preserved."
                if (metadata.get("metadata_state") != "ready" or metadata.get("width") != width
                    or metadata.get("height") != height or metadata.get("codec") != "h264" or not metadata.get("duration_seconds")):
                    failure = "Export finished but its video metadata could not be verified. Original files are preserved."
                if not self._job["sync"] or not self._job["sync"]["output_frames"]:
                    failure = "Export did not report verified source synchronization. Original files are preserved."
            elif code != 0 and not failure and not self._cancel.is_set():
                failure = self._last_error or f"Offline stitch exited with status {code}; original camera files are preserved."
            with self._lock:
                state = "failed" if failure else "cancelled" if self._cancel.is_set() else "complete"
                self._job.update(state=state, error=failure, exit_code=code, finished_at=_now())
                if state == "complete" and metadata and verified_stat:
                    self._job["output"].update({key: metadata.get(key) for key in ("width", "height", "codec", "duration_seconds")})
                    self._job["output"].update(size=verified_stat.st_size, mtime_ns=verified_stat.st_mtime_ns,
                        download_url=f"/api/sessions/{output.parent.name}/{output.name}")
                self._persist()
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            if process and process.is_running() and not self._stop_process(process):
                return
            with self._lock:
                self._job.update(state="cancelled" if self._cancel.is_set() else "failed", error=str(exc)[:500], exit_code=code, finished_at=_now())
                if self._job.get("output"):
                    self._job["output"]["download_url"] = None
                try:
                    self._persist()
                except OSError:
                    pass
        finally:
            with self._lock:
                if process is None or not process.is_running():
                    self._process = None
