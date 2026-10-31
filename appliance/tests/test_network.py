from conftest import session_headers
import io

import png
import pytest

from reco_field.network import wifi_qr_text


def test_wifi_requires_auth_and_csrf(client):
    assert client.get("/api/network").status_code == 401
    assert client.post("/api/network/scan").status_code == 401
    session_headers(client)
    assert client.post("/api/network/connect", json={"ssid": "Phone", "password": "secret123"}).status_code == 403


def test_wifi_credentials_only_reach_privileged_helper(client, monkeypatch):
    import reco_field.app as module
    calls = []

    def helper(path, action, **kwargs):
        calls.append((action, kwargs))
        return {"available": True, "accepted": True, "message": "Connecting"}

    monkeypatch.setattr(module, "network_request", helper)
    response = client.post("/api/network/connect", headers=session_headers(client), json={"ssid": "Phone", "password": "secret123"})
    assert response.status_code == 200
    assert calls == [("connect", {"ssid": "Phone", "password": "secret123"})]
    assert "secret123" not in response.text


def test_wifi_change_blocked_during_recording(client, monkeypatch):
    headers = session_headers(client)
    client.app.state.supervisor.record_state = "running"
    response = client.post("/api/network/connect", headers=headers, json={"ssid": "Phone", "password": "secret123"})
    assert response.status_code == 409
    client.app.state.supervisor.record_state = "idle"


def test_setup_wifi_requires_session_csrf_and_same_origin(client):
    assert client.get("/api/network/setup/qr.png").status_code == 401
    assert client.post("/api/network/setup", json={"ssid": "Camera"}).status_code == 401
    headers = session_headers(client)
    assert client.post("/api/network/setup", json={"ssid": "Camera"}).status_code == 403
    assert client.post("/api/network/setup", headers={**headers, "Origin": "http://elsewhere"},
                       json={"ssid": "Camera"}).status_code == 403


def test_setup_wifi_returns_generated_local_png_without_caching(client, monkeypatch):
    import reco_field.app as module
    calls = []

    def helper(path, action, **kwargs):
        calls.append((action, kwargs))
        return {"available": True, "setup": {"ssid": "Club Camera", "password": "test-only-pass"}}

    monkeypatch.setattr(module, "network_request", helper)
    session_headers(client)
    response = client.get("/api/network/setup/qr.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert "reco-field-wifi.png" in response.headers["content-disposition"]
    width, height, rows, _ = png.Reader(file=io.BytesIO(response.content)).read()
    assert width == height and width >= 232
    assert set(next(rows)) == {1}
    assert calls == [("status", {})]


def test_setup_qr_unavailable_is_explicit(client, monkeypatch):
    import reco_field.app as module
    monkeypatch.setattr(module, "network_request", lambda *args, **kwargs: {"available": False})
    session_headers(client)
    assert client.get("/api/network/setup/qr.png").status_code == 503


def test_wifi_qr_escapes_reserved_characters_and_preserves_unicode():
    assert wifi_qr_text('Club;\\,"房:1', 'pass;\\,"word:') == 'WIFI:T:WPA;S:Club\\;\\\\\\,\\"房\\:1;P:pass\\;\\\\\\,\\"word\\:;;'
    assert wifi_qr_text("ABCD", "abcdef01") == 'WIFI:T:WPA;S:"ABCD";P:"abcdef01";;'


def test_setup_wifi_blank_password_reaches_only_helper(client, monkeypatch):
    import reco_field.app as module
    calls = []
    monkeypatch.setattr(module, "network_request", lambda path, action, **kwargs:
                        calls.append((action, kwargs)) or {"available": True, "accepted": True, "message": "Updated"})
    response = client.post("/api/network/setup", headers=session_headers(client), json={"ssid": "房" * 10})
    assert response.status_code == 200
    assert calls == [("set_setup", {"ssid": "房" * 10, "password": ""})]


@pytest.mark.parametrize("ssid,password", [("房" * 11, "secret123"), ("Bad\nname", "secret123"),
                                           ("Camera", "short"), ("Camera", "secret\n123"), ("Camera", "x" * 64)])
def test_setup_wifi_rejects_invalid_values_without_echoing_password(client, monkeypatch, ssid, password):
    import reco_field.app as module
    monkeypatch.setattr(module, "network_request", lambda *args, **kwargs: pytest.fail("Invalid credentials reached helper"))
    response = client.post("/api/network/setup", headers=session_headers(client), json={"ssid": ssid, "password": password})
    assert response.status_code == 422
    assert password not in response.text
    assert all("input" not in error for error in response.json()["detail"])


@pytest.mark.parametrize("field", ["capture", "calibration", "snapshots", "lens"])
def test_setup_wifi_change_refused_during_camera_work(client, monkeypatch, field):
    headers = session_headers(client)
    supervisor = client.app.state.supervisor
    status = supervisor.status()
    status[field]["state"] = "running"
    monkeypatch.setattr(supervisor, "status", lambda: status)
    assert client.post("/api/network/setup", headers=headers, json={"ssid": "Camera"}).status_code == 409


def test_setup_wifi_helper_failure_is_visible(client, monkeypatch):
    import reco_field.app as module
    monkeypatch.setattr(module, "network_request", lambda *args, **kwargs:
                        {"available": True, "error": "Previous details restored."})
    response = client.post("/api/network/setup", headers=session_headers(client), json={"ssid": "Camera"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Previous details restored."
