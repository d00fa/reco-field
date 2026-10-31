from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .storage import load_json, safe_session_directory, safe_session_file, validate_calibration, validate_capture_profile


def _object(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink():
            return {}
        value = load_json(path)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _number(value: Any) -> float | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            return value
    except OverflowError:
        pass
    return None


def source_session(recordings_dir: Path, session_id: str, active_state: str | None = None) -> dict[str, Any]:
    directory = safe_session_directory(recordings_dir, session_id)
    if not directory.is_dir():
        raise FileNotFoundError("Recording session not found")
    manifest, info = _object(directory / "manifest.json"), _object(directory / "session.json")
    mode = info.get("mode") if isinstance(info.get("mode"), str) and info["mode"] in {"sources", "both"} else (
        "both" if (directory / "panorama.mkv").exists() else "sources")
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    capture = manifest.get("capture") if isinstance(manifest.get("capture"), dict) else {}
    started_at = None
    try:
        started = _number(manifest.get("started_unix_ms"))
        if started is not None:
            started_at = datetime.fromtimestamp(started / 1000, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    outputs = []
    sources_finalized = manifest.get("state") == "complete" and source.get("codec") == "h264" and all(
        isinstance(source.get(key), int) and not isinstance(source[key], bool) and 0 < source[key] <= 8192
        for key in ("width", "height"))
    for role in ("left", "right", *(["panorama"] if mode == "both" else [])):
        filename = f"{role}.mkv"
        try:
            path = safe_session_file(recordings_dir, session_id, filename)
            size = path.stat().st_size if path.is_file() else 0
        except (ValueError, OSError):
            size = 0
        item = manifest.get(role) if isinstance(manifest.get(role), dict) else {}
        timing = item.get("timing") if isinstance(item.get("timing"), dict) else {}
        packets = _number(timing.get("packets"))
        first, last = _number(timing.get("first_pts_ns")), _number(timing.get("last_pts_ns"))
        fps, duration = None, None
        capture_fps = _number(capture.get("fps"))
        if first is not None and last is not None and last > first and packets and packets > 1:
            interval = (last - first) / 1_000_000_000
            fps = (packets - 1) / interval
            duration = interval + 1 / fps
        elif first is not None and first == last and packets == 1 and capture_fps:
            duration = 1 / capture_fps
        if role != "panorama" and (not size or not packets or not isinstance(timing.get("packets"), int)
                or first is None or last is None or last < first or item.get("file") != filename):
            sources_finalized = False
        dimensions = source if role != "panorama" else info.get("panorama", {})
        outputs.append({"role": role, "name": filename, "size": size,
            "width": _number(dimensions.get("width")) if isinstance(dimensions, dict) else None,
            "height": _number(dimensions.get("height")) if isinstance(dimensions, dict) else None,
            "codec": "h264" if role == "panorama" or source.get("codec") == "h264" else None,
            "fps": fps, "duration_seconds": duration, "complete": False, "download_url": None})
    complete = sources_finalized and info.get("state") == "complete"
    if mode == "both":
        complete = complete and info.get("state") == "complete" and all(item["size"] > 0 for item in outputs)
    state = ("finishing" if active_state == "stopping" else "recording") if active_state else (
        "complete" if complete else "interrupted" if manifest.get("state") == "recording" else "incomplete")
    error = info.get("error") or manifest.get("error") or ("Capture did not finish cleanly." if state in {"incomplete", "interrupted"} else "")
    for item in outputs:
        item["complete"] = state == "complete"
        if item["complete"]:
            item["download_url"] = f"/api/sessions/{session_id}/{item['name']}"
    for metadata_file in sorted(directory.glob("export-*.json")):
        metadata = _object(metadata_file)
        name = metadata_file.with_suffix(".mkv").name
        try:
            export = safe_session_file(recordings_dir, session_id, name)
            stat = export.stat()
        except (ValueError, OSError):
            continue
        result = metadata.get("output") if isinstance(metadata.get("output"), dict) else {}
        duration = _number(result.get("duration_seconds"))
        width, height = _number(result.get("width")), _number(result.get("height"))
        valid = metadata.get("state") == "complete" and type(metadata.get("exit_code")) is int and metadata["exit_code"] == 0 and stat.st_size > 0 and (
            stat.st_size == result.get("size") and stat.st_mtime_ns == result.get("mtime_ns"))
        valid = bool(valid and result.get("name") == name and width and height and duration and result.get("codec") == "h264")
        outputs.append({"role": "export", "name": name, "size": stat.st_size,
            "width": width, "height": height, "codec": "h264" if result.get("codec") == "h264" else None,
            "fps": None, "duration_seconds": duration, "complete": valid,
            "state": metadata.get("state") if isinstance(metadata.get("state"), str) else "incomplete", "error": str(metadata.get("error", ""))[:1000],
            "download_url": f"/api/sessions/{session_id}/{name}" if valid else None})
    durations = [item["duration_seconds"] for item in outputs if item["duration_seconds"] is not None]
    return {"id": session_id, "started_at": started_at, "mode": mode, "state": state,
        "error": str(error)[:1000], "size": sum(item["size"] for item in outputs),
        "duration_seconds": max(durations) if durations else None, "outputs": outputs, "can_stitch": False}


def validate_stitch_source(recordings_dir: Path, session_id: str) -> Path:
    directory = safe_session_directory(recordings_dir, session_id)
    session = source_session(recordings_dir, session_id)
    if session["state"] != "complete":
        raise ValueError("Only a finalized camera session can be stitched.")
    manifest = _object(directory / "manifest.json")
    if manifest.get("calibration") != "calibration.json" or manifest.get("clock_comparable") is not True:
        raise ValueError("This session has no comparable camera clock and saved calibration.")
    path = directory / "calibration.json"
    if path.is_symlink():
        raise ValueError("Saved calibration must be inside the recording session.")
    if path.stat().st_size > 1_048_576:
        raise ValueError("Saved calibration is too large.")
    calibration = validate_calibration(path.read_bytes())
    capture, source, controls = manifest.get("capture", {}), manifest.get("source", {}), manifest.get("controls", {})
    if not all(isinstance(item, dict) for item in (capture, source, controls)):
        raise ValueError("Saved capture metadata is malformed.")
    for key in ("width", "height"):
        if not isinstance(capture.get(key), int) or not 0 < source[key] <= capture[key] <= 8192:
            raise ValueError("Saved camera dimensions are incompatible with this session.")
    if source["width"] * capture["height"] != source["height"] * capture["width"]:
        raise ValueError("Source recording does not preserve the calibrated field of view.")
    for side in ("left", "right"):
        validate_capture_profile(calibration[f"{side}_uniforms"], capture["width"], capture["height"],
            capture_identity={"device": str(capture.get(f"{side}_device")), "sensor_mode": controls.get("sensor_mode"),
                              "width": capture["width"], "height": capture["height"]}, label=f"Saved {side} lens")
    return directory
