from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

from .storage import atomic_write, atomic_write_json, load_json, require_storage, validate_lens_profile

ACTIVE = {"starting", "running", "stopping"}
MAX_VIEWS = 32
MIN_VIEWS = 16


class LensCalibration:
    def __init__(self, runtime, process_factory, capture_argv) -> None:
        self.runtime = runtime
        self.process_factory = process_factory
        self.capture_argv = capture_argv
        self.state = "idle"
        self.error = ""
        self.progress = ""
        self.side = "left"
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._process = None

    @property
    def active(self) -> bool:
        return self.state in ACTIVE or bool(self._thread and self._thread.is_alive())

    def _storage(self) -> None:
        require_storage(self.runtime.data_dir, self.runtime.storage_mount, self.runtime.storage_uuid)

    def _folder(self, side: str) -> Path:
        if side not in {"left", "right"}:
            raise ValueError("Choose the left or right lens.")
        path = self.runtime.data_dir / "config" / "lens-work" / side
        if path.is_symlink() or not path.resolve().is_relative_to(self.runtime.data_dir.resolve()):
            raise ValueError("Invalid lens calibration directory.")
        return path

    @staticmethod
    def _read(path: Path) -> dict:
        if path.is_symlink():
            raise ValueError("Invalid lens calibration file.")
        value = load_json(path) if path.exists() else {}
        if not isinstance(value, dict):
            raise ValueError("Invalid lens calibration data.")
        return value

    def status(self) -> dict[str, Any]:
        with self._lock:
            lenses = {}
            for side in ("left", "right"):
                folder = self._folder(side)
                try:
                    manifest = self._read(folder / "views.json")
                    candidate = self._read(folder / "candidate.json")
                    lenses[side] = {"views": len(manifest.get("views", [])),
                        "quality": candidate.get("quality"), "ready": bool(candidate),
                        "preview": f"/api/lenses/{side}/preview.jpg" if (folder / "preview.jpg").is_file() else None,
                        "preview_revision": str((folder / "preview.jpg").stat().st_mtime_ns) if (folder / "preview.jpg").is_file() else "",
                        "revision": manifest.get("revision", "")}
                except (OSError, ValueError, TypeError):
                    lenses[side] = {"views": 0, "quality": None, "ready": False, "preview": None, "revision": ""}
            return {"state": self.state, "side": self.side, "error": self.error,
                "progress": self.progress, "minimum_views": MIN_VIEWS, "maximum_views": MAX_VIEWS,
                "lenses": lenses}

    def _env(self) -> dict[str, str]:
        return {**os.environ, "TMPDIR": str(self.runtime.temp_dir), "PYTHONNOUSERSITE": "1",
            "OPENCV_IO_MAX_IMAGE_PIXELS": "34000000"}

    def _worker_args(self, command: str, source: Path, output: Path) -> list[str]:
        return [self.runtime.cv_python_bin, str(Path(__file__).with_name("lens_cv.py")),
            command, str(source), str(output)]

    def board_file(self) -> Path:
        self._storage()
        output = self.runtime.temp_dir / "reco-charuco-30mm.svg"
        if output.is_file() and not output.is_symlink():
            return output
        temporary = self.runtime.temp_dir / f"board-{uuid.uuid4().hex}.svg"
        try:
            result = subprocess.run(self._worker_args("board", temporary, temporary),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self._env(), timeout=20)
            if result.returncode or not temporary.is_file() or temporary.stat().st_size > 1_048_576:
                raise ValueError("OpenCV ChArUco is unavailable in the system Python.")
            atomic_write(output, temporary.read_bytes())
            return output
        finally:
            temporary.unlink(missing_ok=True)

    def preview_file(self, side: str) -> Path:
        path = self._folder(side) / "preview.jpg"
        if not path.is_file() or path.is_symlink():
            raise ValueError("Capture a board view first.")
        return path

    def start(self, side: str, operation: str, config: dict) -> None:
        with self._lock:
            self._storage()
            if self.active:
                raise ValueError("Wait for the current lens operation to finish.")
            if operation not in {"capture", "solve"}:
                raise ValueError("Unknown lens operation.")
            folder = self._folder(side)
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            manifest = self._read(folder / "views.json")
            if manifest and manifest.get("capture") != config:
                raise ValueError("Camera mode or sensor changed. Reset these lens views before continuing.")
            views = manifest.get("views", [])
            if operation == "capture" and len(views) >= MAX_VIEWS:
                raise ValueError("32 views collected. Solve the lens or reset its views.")
            if operation == "solve" and len(views) < MIN_VIEWS:
                raise ValueError(f"Collect at least {MIN_VIEWS} varied board views first.")
            (folder / "candidate.json").unlink(missing_ok=True)
            self.side, self.state, self.error = side, "starting", ""
            self.progress = "Capturing a full-resolution board view" if operation == "capture" else "Fitting lens and checking held-out views"
            self._cancel.clear()
            self._thread = threading.Thread(target=self._run, args=(side, operation, config), daemon=True)
            self._thread.start()

    def _execute(self, argv: list[str], timeout: int) -> None:
        process = self.process_factory(argv, self.runtime.logs_dir / "lens-calibration.log",
            env=self._env(), log_max_bytes=self.runtime.log_max_bytes, log_backups=self.runtime.log_backups)
        with self._lock:
            if self._cancel.is_set():
                raise ValueError("Cancelled")
            self._process = process
            process.start()
        try:
            code = process.wait(timeout=timeout)
            if self._cancel.is_set():
                raise ValueError("Cancelled")
            if code:
                raise ValueError(f"Lens operation exited with status {code}.")
        except subprocess.TimeoutExpired as exc:
            process.stop(grace_seconds=3)
            raise ValueError("Lens operation exceeded its time limit.") from exc
        finally:
            with self._lock:
                self._process = None

    def _cv(self, command: str, source: Path, output: Path, timeout: int) -> dict:
        try:
            self._execute(self._worker_args(command, source, output), timeout)
        except ValueError:
            if output.is_file():
                message = self._read(output).get("error")
                if message:
                    raise ValueError(message) from None
            raise
        return self._read(output)

    def _run(self, side: str, operation: str, config: dict) -> None:
        folder = self._folder(side)
        result_path = folder / "result.json"
        failure = ""
        try:
            result_path.unlink(missing_ok=True)
            with self._lock:
                self.state = "running"
            if operation == "capture":
                pattern = folder / ".capture-%03d.jpg"
                self._execute(self.capture_argv(config["device"], pattern, full_resolution=True), 60)
                images = sorted(folder.glob(".capture-*.jpg"))
                if not images:
                    raise ValueError("Camera produced no calibration image.")
                atomic_write(folder / "preview.jpg", images[-1].read_bytes())
                observation = self._cv("detect", folder / "preview.jpg", result_path, 45)
                size = [config["width"], config["height"]]
                if observation.get("size") != size:
                    raise ValueError("Captured dimensions do not match the selected sensor mode.")
                manifest = self._read(folder / "views.json") or {"capture": config, "size": size, "views": []}
                descriptor = observation["descriptor"]
                for view in manifest["views"]:
                    old = view["descriptor"]
                    if (math.hypot(old[0] - descriptor[0], old[1] - descriptor[1]) < .07
                        and abs(math.log(max(old[2], 1e-6) / max(descriptor[2], 1e-6))) < .18
                        and math.hypot(old[3] - descriptor[3], old[4] - descriptor[4]) < .12):
                        raise ValueError("View too similar to an existing one. Move the board toward another edge, change distance or tilt it.")
                manifest["views"].append(observation)
                manifest["revision"] = uuid.uuid4().hex
                atomic_write_json(folder / "views.json", manifest)
                self.progress = f"Accepted {len(observation['ids'])}/24 corners · {len(manifest['views'])}/{MIN_VIEWS} views"
            else:
                result = self._cv("solve", folder / "views.json", result_path, 120)
                validate_lens_profile(json.dumps(result["profile"]).encode())
                result["capture"] = config
                result["revision"] = self._read(folder / "views.json")["revision"]
                atomic_write_json(folder / "candidate.json", result)
                self.progress = "Lens passed validation. Review the errors, then apply the profile."
        except (OSError, ValueError, KeyError, TypeError) as exc:
            failure = str(exc)[:600]
        finally:
            try:
                result_path.unlink(missing_ok=True)
                for image in folder.glob(".capture-*.jpg"):
                    image.unlink(missing_ok=True)
                if self._cancel.is_set():
                    (folder / "candidate.json").unlink(missing_ok=True)
            except OSError as exc:
                failure = failure or str(exc)[:600]
            with self._lock:
                self.state = "idle" if self._cancel.is_set() else "failed" if failure else "complete"
                self.error = "" if self._cancel.is_set() else failure
                if self._cancel.is_set():
                    self.progress = "Cancelled"
                elif failure:
                    self.progress = "Board view rejected" if operation == "capture" else "Lens fit rejected"

    def stop(self) -> None:
        with self._lock:
            if not self.active:
                return
            self.state = "stopping"
            self._cancel.set()
            process, thread = self._process, self._thread
        if process:
            process.stop(grace_seconds=3)
        if thread:
            thread.join(timeout=10)
        with self._lock:
            if not thread or not thread.is_alive():
                self.state, self.progress = "idle", "Cancelled"

    def reset(self, side: str) -> None:
        self._storage()
        with self._lock:
            if self.active:
                raise ValueError("Cancel the current lens operation before resetting.")
            folder = self._folder(side)
            for name in ("views.json", "candidate.json", "preview.jpg", "result.json"):
                (folder / name).unlink(missing_ok=True)
            self.side, self.state, self.error, self.progress = side, "idle", "", "Lens views reset"

    def apply(self, side: str, config: dict) -> None:
        self._storage()
        with self._lock:
            if self.active:
                raise ValueError("Wait for the lens operation to finish before applying.")
            folder = self._folder(side)
            candidate = self._read(folder / "candidate.json")
            manifest = self._read(folder / "views.json")
            if (not candidate or candidate.get("capture") != config
                or candidate.get("revision") != manifest.get("revision")):
                raise ValueError("Solve a lens profile for the current camera mode first.")
            profile = validate_lens_profile(json.dumps(candidate["profile"]).encode())
            if [profile["width"], profile["height"]] != [config["width"], config["height"]]:
                raise ValueError("Lens profile dimensions do not match the selected capture mode.")
            profile["capture_identity"] = dict(config)
            validate_lens_profile(json.dumps(profile).encode())
            quality = candidate.get("quality", {})
            for key, limit in (("training_rms_px", 1.5), ("holdout_rms_px", 2.0), ("holdout_max_view_rms_px", 3.0)):
                value = quality.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= limit:
                    raise ValueError("Lens candidate has not passed quality validation.")
            destination = self.runtime.data_dir / "config" / f"{side}-lens.json"
            backup = self.runtime.data_dir / "config" / "lens-backups" / uuid.uuid4().hex
            if destination.exists():
                atomic_write(backup.with_name(backup.name + f"-{side}-lens.json"), destination.read_bytes())
            if self.runtime.calibration_file.exists():
                atomic_write(backup.with_name(backup.name + "-alignment.json"), self.runtime.calibration_file.read_bytes())
            atomic_write_json(destination, profile)
            self.runtime.calibration_file.unlink(missing_ok=True)
            (folder / "candidate.json").unlink(missing_ok=True)
            self.side, self.state, self.error = side, "complete", ""
            self.progress = f"{side.title()} lens saved. Run geometric calibration before recording."
