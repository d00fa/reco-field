from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from .config import FieldSettings


MAX_JSON_BYTES = 1_048_576


def require_storage(data_dir: Path, mount: Path | None, uuid: str) -> None:
    if mount is None and not uuid:
        return
    if mount is None or not re.fullmatch(r"[a-fA-F0-9-]{8,64}", uuid):
        raise OSError("SSD recording storage is not configured correctly.")
    if not mount.is_absolute() or not data_dir.resolve().is_relative_to(mount.resolve()):
        raise OSError("Recording directory must be on the configured SSD.")
    if not os.path.ismount(mount):
        raise OSError("Recording SSD is not mounted. Recording to the SD card is disabled.")
    try:
        device = os.stat(f"/dev/disk/by-uuid/{uuid}")
        volume = os.stat(mount)
        destination = os.stat(data_dir if data_dir.exists() else mount)
    except OSError as exc:
        raise OSError("Recording SSD is unavailable. Recording to the SD card is disabled.") from exc
    if not stat.S_ISBLK(device.st_mode) or volume.st_dev != device.st_rdev or destination.st_dev != device.st_rdev:
        raise OSError("Wrong recording filesystem. Only the configured SSD may hold recordings.")
    for name in ("recordings", "snapshots", "tmp", "logs", "config"):
        child = data_dir / name
        if not child.resolve().is_relative_to(mount.resolve()) or (child.exists() and child.stat().st_dev != device.st_rdev):
            raise OSError("Recording storage subdirectories must remain on the configured SSD.")


def ensure_directories(data_dir: Path) -> None:
    for path in (
        data_dir,
        data_dir / "config",
        data_dir / "recordings",
        data_dir / "logs",
        data_dir / "snapshots",
        data_dir / "tmp",
    ):
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def load_json(path: Path, max_bytes: int = MAX_JSON_BYTES) -> Any:
    if path.stat().st_size > max_bytes:
        raise ValueError("JSON file is too large")
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("JSON file is too large")
    return json.loads(raw)


def load_settings(path: Path) -> FieldSettings:
    if not path.exists():
        return FieldSettings()
    value = load_json(path)
    if isinstance(value, dict):
        camera = value.get("camera")
        if isinstance(camera, dict) and (
            camera.get("encoder") != "libx264" or camera.get("codec") != "h264"
        ):
            camera["encoder"] = "libx264"
            camera["codec"] = "h264"
    return FieldSettings.model_validate(value)


def save_settings(path: Path, settings: FieldSettings) -> None:
    atomic_write_json(path, settings.model_dump(mode="json"))


def validate_calibration(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("calibration JSON exceeds 1 MiB")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("calibration must be a JSON object")
    required = {"left_uniforms", "right_uniforms", "params"}
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError(f"calibration missing fields: {', '.join(missing)}")
    for key in ("left_uniforms", "right_uniforms"):
        camera = value.get(key)
        _validate_camera_profile(camera, key)
    params = value.get("params")
    if not isinstance(params, dict) or not all(
        key in params
        for key in ("cameraAxisOffset", "intersect", "xTy", "xRz", "zRx")
    ):
        raise ValueError("params is not a valid stereo layout")
    for key in ("cameraAxisOffset", "intersect", "xTy", "xRz", "zRx"):
        _finite_number(params[key], f"params.{key}")
    return value


def validate_lens_profile(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("lens profile JSON exceeds 1 MiB")
    value = json.loads(data)
    _validate_camera_profile(value, "lens profile")
    return value


def validate_capture_profile(
    profile: dict[str, Any], width: int, height: int, *,
    capture_identity: dict[str, Any] | None = None, label: str = "Lens",
) -> None:
    measured = profile.get("capture_identity")
    if measured is not None and capture_identity is not None and measured != capture_identity:
        raise ValueError(
            f"{label} profile was measured for sensor {measured['device']}, mode {measured['sensor_mode']}, "
            f"{measured['width']}x{measured['height']}; current capture is sensor {capture_identity['device']}, "
            f"mode {capture_identity['sensor_mode']}, {width}x{height}. Restore those settings or measure/import "
            f"a matching {label.lower()} profile, then run geometric calibration."
        )
    expected = profile["width"] / profile["height"]
    if not math.isclose(width / height, expected, rel_tol=0.01):
        raise ValueError(
            f"Lens calibration is for {profile['width']}x{profile['height']}, "
            f"but capture is {width}x{height}. Select the matching sensor mode "
            "and capture dimensions, or import a lens profile for this crop, then recalibrate."
        )


def _validate_camera_profile(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not a valid camera profile")
    for key in ("width", "height"):
        dimension = value.get(key)
        if (
            isinstance(dimension, bool)
            or not isinstance(dimension, int)
            or not 0 < dimension <= 16384
        ):
            raise ValueError(f"{label}.{key} must be an integer in 1..=16384")
    for key in ("fx", "fy", "cx", "cy"):
        number = _finite_number(value.get(key), f"{label}.{key}")
        if key in {"fx", "fy"} and number <= 0:
            raise ValueError(f"{label}.{key} must be positive")
    distortion = value.get("d")
    if not isinstance(distortion, list) or len(distortion) != 4:
        raise ValueError(f"{label}.d must contain four numeric values")
    for index, item in enumerate(distortion):
        _finite_number(item, f"{label}.d[{index}]")
    if "capture_identity" in value:
        identity = value["capture_identity"]
        if not isinstance(identity, dict) or set(identity) != {"device", "sensor_mode", "width", "height"}:
            raise ValueError(f"{label}.capture_identity must contain device, sensor_mode, width and height")
        device = identity["device"]
        if not isinstance(device, str) or not re.fullmatch(r"[0-9]{1,3}", device) or int(device) > 255:
            raise ValueError(f"{label}.capture_identity.device must be a numeric sensor ID in 0..=255")
        mode = identity["sensor_mode"]
        if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 255:
            raise ValueError(f"{label}.capture_identity.sensor_mode must be an integer in 0..=255")
        for key in ("width", "height"):
            dimension = identity[key]
            if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension != value[key]:
                raise ValueError(f"{label}.capture_identity.{key} must match the lens profile dimension")


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return float(value)


def safe_recording_path(recordings_dir: Path, filename: str) -> Path:
    if (
        not filename
        or Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
        or not filename.endswith(".mkv")
    ):
        raise ValueError("invalid recording filename")
    root = recordings_dir.resolve()
    original = root / filename
    if original.is_symlink():
        raise ValueError("recording symlinks are not served")
    candidate = original.resolve(strict=False)
    if candidate.parent != root:
        raise ValueError("recording path escapes data root")
    return candidate


def safe_session_directory(recordings_dir: Path, session_id: str) -> Path:
    if not re.fullmatch(r"reco-\d{8}-\d{6}-[a-f0-9]{12}", session_id):
        raise ValueError("invalid recording session")
    root = recordings_dir.resolve()
    path = root / session_id
    if path.is_symlink() or path.resolve(strict=False).parent != root:
        raise ValueError("recording session escapes data root")
    return path


def safe_session_file(recordings_dir: Path, session_id: str, filename: str) -> Path:
    if filename not in {"left.mkv", "right.mkv", "panorama.mkv"} and not re.fullmatch(r"export-[a-f0-9]{32}\.mkv", filename):
        raise ValueError("invalid session filename")
    directory = safe_session_directory(recordings_dir, session_id)
    path = directory / filename
    if path.is_symlink() or path.resolve(strict=False).parent != directory:
        raise ValueError("session file escapes recording directory")
    return path
