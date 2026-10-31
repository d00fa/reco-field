from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reco_field.app import create_app
from reco_field.config import RuntimeConfig
from reco_field.storage import ensure_directories


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


@pytest.fixture
def runtime(tmp_path: Path) -> RuntimeConfig:
    data = tmp_path / "data"
    ensure_directories(data)
    config = RuntimeConfig(
        data_dir=data,
        reco_bin=str(tmp_path / "reco"),
        mediamtx_bin=str(tmp_path / "mediamtx"),
        mediamtx_config=Path(__file__).parents[1]
        / "reco_field"
        / "mediamtx.yml",
        ffmpeg_bin="ffmpeg",
        gst_inspect_bin="gst-inspect-1.0",
        gst_launch_bin="gst-launch-1.0",
        bind="127.0.0.1",
        port=8080,
        manage_relay=False,
        simulator=False,
    )
    config.calibration_file.write_text(
        json.dumps(valid_calibration()), encoding="utf-8"
    )
    return config


@pytest.fixture
def client(runtime: RuntimeConfig):
    app = create_app(runtime)
    with TestClient(app) as test_client:
        yield test_client


def session_headers(client: TestClient) -> dict[str, str]:
    response = client.get(
        "/api/session",
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200, response.text
    return {
        "Origin": "http://testserver",
        "X-CSRF-Token": response.json()["csrf"],
    }
