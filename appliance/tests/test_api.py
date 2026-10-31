from __future__ import annotations

import json

import pytest

from conftest import session_headers, valid_calibration
from reco_field import app as app_module
from types import SimpleNamespace
from reco_field.supervisor import SupervisorError


def test_auto_start_waits_for_package_activation(tmp_path, monkeypatch):
    gate = tmp_path / "upgrading"
    gate.touch()
    monkeypatch.setattr(app_module, "MAINTENANCE_FILE", gate)
    monkeypatch.setattr(app_module.time, "sleep", lambda _: gate.unlink())
    starts = []
    def start(stream):
        if gate.exists():
            raise SupervisorError("updating", 503)
        starts.append(stream)
    supervisor = SimpleNamespace(settings=SimpleNamespace(auto_stream=False), start_recording=start, record_error="")
    app_module._try_auto_start(supervisor)
    assert starts == [False]
    assert supervisor.record_error == ""


def test_upgrade_blocks_network_changes_but_allows_status_and_stop(client, tmp_path, monkeypatch):
    headers = session_headers(client)
    gate = tmp_path / "upgrading"
    gate.touch()
    monkeypatch.setattr(app_module, "MAINTENANCE_FILE", gate)
    response = client.post("/api/network/connect", json={"ssid": "Phone", "password": "test-password"}, headers=headers)
    assert response.status_code == 503
    assert client.get("/api/status").status_code == 200
    assert client.post("/api/recordings/stop", json={}, headers=headers).status_code == 200


@pytest.mark.parametrize("field,value", [("projection", "invalid"), ("output_width", 1922)])
def test_output_geometry_settings_validation(client, field, value):
    headers = session_headers(client)
    settings = client.get("/api/settings").json()
    settings["camera"][field] = value
    assert client.put("/api/settings", json=settings, headers=headers).status_code == 422


def test_right_lens_import_validation(client, runtime):
    headers = session_headers(client)
    path = runtime.data_dir / "config" / "right-lens.json"
    endpoint = "/api/calibration/import?kind=right-lens"
    assert client.post(endpoint, json={"bad": True}, headers=headers).status_code == 422
    assert not path.exists()
    profile = valid_calibration()["right_uniforms"]
    response = client.post(endpoint, json=profile, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["right_lens_profile"]
    assert json.loads(path.read_text()) == profile


def test_settings_validation_and_secret_masking(client):
    headers = session_headers(client)
    settings = client.get("/api/settings").json()
    settings["camera"]["capture_width"] = 3841
    response = client.put("/api/settings", json=settings, headers=headers)
    assert response.status_code == 422

    settings = client.get("/api/settings").json()
    settings["destinations"] = [
        {
            "id": "youtube-main",
            "name": "Main field",
            "provider": "youtube",
            "url": "rtmp://a.rtmp.youtube.com/live2/private-key",
            "enabled": True,
        }
    ]
    response = client.put("/api/settings", json=settings, headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["destinations"][0]["url"] == "rtmp://a.rtmp.youtube.com/***"
    assert "private-key" not in response.text

    # The browser only ever sees a masked URL. Saving unrelated settings with
    # that masked value must preserve the private ingest key in storage.
    response = client.put("/api/settings", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    stored = client.app.state.supervisor.get_settings().destinations[0]
    assert stored.url == "rtmp://a.rtmp.youtube.com/live2/private-key"


def test_manual_exposure_requires_equal_bounds(client):
    headers = session_headers(client)
    settings = client.get("/api/settings").json()
    settings["camera"]["exposure_mode"] = "manual"
    settings["camera"]["exposure_min_ns"] = 33_000_000
    settings["camera"]["exposure_max_ns"] = 34_000_000
    response = client.put("/api/settings", json=settings, headers=headers)
    assert response.status_code == 422


def test_left_and_right_sensors_must_differ(client):
    headers = session_headers(client)
    settings = client.get("/api/settings").json()
    settings["camera"]["right_device"] = settings["camera"]["left_device"]
    response = client.put("/api/settings", json=settings, headers=headers)
    assert response.status_code == 422


def test_appliance_encoder_is_h264_only(client):
    headers = session_headers(client)
    settings = client.get("/api/settings").json()
    settings["camera"]["encoder"] = "libx265"
    response = client.put("/api/settings", json=settings, headers=headers)
    assert response.status_code == 422


def test_calibration_import_is_validated_and_atomic(client, runtime):
    headers = session_headers(client)
    original = runtime.calibration_file.read_bytes()
    response = client.post(
        "/api/calibration/import?kind=calibration",
        content=b'{"bad":true}',
        headers={**headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert runtime.calibration_file.read_bytes() == original

    imported = valid_calibration()
    imported["params"]["intersect"] = 0.6
    response = client.post(
        "/api/calibration/import?kind=calibration",
        content=json.dumps(imported).encode(),
        headers={**headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert (
        json.loads(runtime.calibration_file.read_text())["params"]["intersect"]
        == 0.6
    )


def test_recording_download_rejects_traversal(client):
    session_headers(client)
    response = client.get("/api/recordings/..%2Fsettings.json")
    assert response.status_code in {404, 422}


def test_static_ui_has_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"


def test_request_body_limit(client):
    headers = session_headers(client)
    response = client.post(
        "/api/calibration/import?kind=calibration",
        content=b"x" * (1_048_576 + 1),
        headers={**headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_preview_proxy_keeps_only_validated_mediamtx_query(
    client, monkeypatch
):
    headers = session_headers(client)
    supervisor = client.app.state.supervisor
    supervisor.record_state = "running"
    captured = {}

    def fake_fetch(url: str):
        captured["url"] = url
        return b"#EXTM3U\n", "application/vnd.apple.mpegurl"

    monkeypatch.setattr(app_module, "_fetch_preview", fake_fetch)
    session = "b46859b2-fada-4c0a-a2cc-ab705acd989e"
    response = client.get(
        (
            "/api/preview/reco/live/video1_stream.m3u8"
            f"?session={session}&_HLS_msn=2&_HLS_part=1"
            "&_HLS_skip=YES&evil=https://example.test"
        ),
        headers=headers,
    )
    assert response.status_code == 200
    assert f"session={session}" in captured["url"]
    assert "_HLS_msn=2" in captured["url"]
    assert "evil=" not in captured["url"]
