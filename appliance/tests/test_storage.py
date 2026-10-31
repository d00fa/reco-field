from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reco_field.storage import (
    atomic_write,
    load_settings,
    safe_recording_path,
    validate_calibration,
    validate_capture_profile,
)


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


def test_recording_path_rejects_traversal(tmp_path: Path):
    with pytest.raises(ValueError):
        safe_recording_path(tmp_path, "../outside.mkv")
    with pytest.raises(ValueError):
        safe_recording_path(tmp_path, "C:\\outside.mkv")
    with pytest.raises(ValueError):
        safe_recording_path(tmp_path, "recording.mp4")


def test_capture_profile_accepts_resize_but_rejects_sensor_crop():
    profile = {"width": 4032, "height": 3040}
    validate_capture_profile(profile, 4032, 3040)
    validate_capture_profile(profile, 2016, 1520)
    with pytest.raises(ValueError, match="matching sensor mode"):
        validate_capture_profile(profile, 3840, 2160)


def test_recording_symlink_rejected(tmp_path: Path):
    target = tmp_path / "target.mkv"
    target.write_bytes(b"video")
    link = tmp_path / "link.mkv"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError):
        safe_recording_path(tmp_path, "link.mkv")


def test_atomic_write_and_calibration_validation(tmp_path: Path):
    path = tmp_path / "calibration.json"
    payload = json.dumps(valid_calibration()).encode()
    atomic_write(path, payload)
    assert path.read_bytes() == payload
    assert validate_calibration(payload)["params"]["intersect"] == 0.54
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_calibration_size_and_shape_limits():
    with pytest.raises(ValueError):
        validate_calibration(b"{}")
    with pytest.raises(ValueError):
        validate_calibration(b"{" + b" " * 1_048_577 + b"}")
    value = valid_calibration()
    value["left_uniforms"]["fx"] = "not-a-number"
    with pytest.raises(ValueError, match="must be numeric"):
        validate_calibration(json.dumps(value).encode())
    value = valid_calibration()
    value["left_uniforms"]["d"] = [0.0, 0.0]
    with pytest.raises(ValueError, match="four numeric"):
        validate_calibration(json.dumps(value).encode())


def test_unsupported_encoder_settings_migrate_to_h264(tmp_path: Path):
    path = tmp_path / "settings.json"
    value = {
        "camera": {"encoder": "libx265", "codec": "hevc", "output_width": 1280},
        "destinations": [],
    }
    path.write_text(json.dumps(value))
    settings = load_settings(path)
    assert settings.camera.encoder == "libx264"
    assert settings.camera.codec == "h264"
    assert settings.camera.output_width == 1280
