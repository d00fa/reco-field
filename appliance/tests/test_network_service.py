import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("pwd")
spec = importlib.util.spec_from_file_location("network_service", Path(__file__).parents[1] / "scripts/network_service.py")
service = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = service
spec.loader.exec_module(service)


@pytest.mark.parametrize("hostname", ["field-unit-2", "field-unit-2.local"])
def test_device_url_uses_this_units_hostname(monkeypatch, hostname):
    monkeypatch.setattr(service.socket, "gethostname", lambda: hostname)
    assert service.device_url() == "http://field-unit-2.local:8081"


def network(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "STATE", tmp_path)
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    result = service.Network.__new__(service.Network)
    result.interface = "wlan0"
    result.lock = threading.Lock()
    result.busy = True
    result.target = None
    result.setup = {"uuid": "setup", "ssid": "Setup", "password": "test-only-secret"}
    return result


@pytest.mark.parametrize("ssid,password", [("bad\n[wifi-security]", "password1"), ("🙂" * 9, "password1"), ("Phone", "short"), ("Phone", "bad\nvalue")])
def test_network_input_cannot_inject_keyfile(ssid, password):
    with pytest.raises(ValueError):
        service.validate_wifi(ssid, password)


def test_keyfile_escapes_and_keeps_password_out_of_commands():
    profile = service.profile_text("c9a3b642-9c0f-432a-8a23-fd856535f72c", "My Phone", "pass word", "wlan0", False)
    assert "ssid=My\\sPhone" in profile
    assert "psk=pass\\sword" in profile
    assert "autoconnect=true" in profile


def test_failed_join_restores_previous_network(tmp_path, monkeypatch):
    net = network(tmp_path, monkeypatch)
    calls = []

    def nm(*args, **kwargs):
        calls.append(args)
        if "up" in args and "new" in args:
            raise RuntimeError("failure")
        return ""

    monkeypatch.setattr(service, "nm", nm)
    net.activate("new", "previous", "Phone")
    assert net.state == "failed"
    assert not net.busy
    assert any("up" in item and "previous" in item for item in calls)
    assert not (tmp_path / "target.json").exists()


def test_success_remembers_hotspot_without_password(tmp_path, monkeypatch):
    net = network(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "nm", lambda *args, **kwargs: "")
    net.activate("new", "previous", "Phone")
    assert net.state == "connected"
    assert json.loads((tmp_path / "target.json").read_text()) == {"uuid": "new", "ssid": "Phone"}
    assert not net.busy


def test_failed_reconnect_does_not_delete_saved_profile(tmp_path, monkeypatch):
    net = network(tmp_path, monkeypatch)
    calls = []

    def nm(*args, **kwargs):
        calls.append(args)
        if "up" in args and "saved" in args:
            raise RuntimeError("failure")
        return ""

    monkeypatch.setattr(service, "nm", nm)
    net.activate("saved", "setup", "Phone", False)
    assert not any("delete" in item for item in calls)
    assert any("up" in item and "setup" in item for item in calls)


def editable_network(tmp_path, monkeypatch, active=False):
    net = network(tmp_path, monkeypatch)
    net.setup["uuid"] = "c9a3b642-9c0f-432a-8a23-fd856535f72c"
    net.busy, net.state, net.result = False, "idle", ""
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(service, "PROFILES", profiles)
    profile = profiles / f"reco-field-{net.setup['uuid']}.nmconnection"
    service.private_write(profile, service.profile_text(**{
        "identity": net.setup["uuid"], "ssid": net.setup["ssid"], "password": net.setup["password"],
        "interface": net.interface, "ap": True}))
    service.private_write(tmp_path / "setup.json", json.dumps(net.setup))
    service.private_write(tmp_path / "target.json", '{"uuid":"phone","ssid":"Phone"}')
    net.target = {"uuid": "phone", "ssid": "Phone"}
    monkeypatch.setattr(net, "current_uuid", lambda: net.setup["uuid"] if active else "router")
    return net, profile


def test_setup_edit_keeps_router_target_uuid_and_private_files(tmp_path, monkeypatch):
    net, profile = editable_network(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(service, "nm", lambda *args, **kwargs: calls.append(args) or "")
    previous_uuid = net.setup["uuid"]
    result = net.set_setup("Club Camera", "new-password")
    assert result["accepted"] and not result["setup"]["active"]
    assert net.setup["uuid"] == previous_uuid
    assert net.setup["revision"] == result["setup"]["revision"]
    assert json.loads((tmp_path / "setup.json").read_text()) == net.setup
    assert net.target == json.loads((tmp_path / "target.json").read_text()) == {"uuid": "phone", "ssid": "Phone"}
    assert not any("up" in args or "delete" in args or "new-password" in args for args in calls)
    assert profile.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "setup.json").stat().st_mode & 0o777 == 0o600
    assert not net.busy


def test_setup_no_change_is_noop_and_blank_keeps_password(tmp_path, monkeypatch):
    net, profile = editable_network(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "nm", lambda *args, **kwargs: pytest.fail("Unchanged profile reloaded"))
    assert net.set_setup("Setup", "")["accepted"]
    monkeypatch.setattr(service, "nm", lambda *args, **kwargs: "")
    assert net.set_setup("Renamed", "")["setup"]["password"] == "test-only-secret"


def test_setup_edit_rejects_busy_or_invalid_without_mutation(tmp_path, monkeypatch):
    net, profile = editable_network(tmp_path, monkeypatch)
    old = profile.read_text()
    net.busy = True
    with pytest.raises(ValueError, match="already in progress"):
        net.set_setup("Camera", "password1")
    net.busy = False
    for ssid, password in [("🙂" * 9, "password1"), ("Bad\nname", "password1"), ("Camera", "secret\n123"), ("Camera", "x" * 64)]:
        with pytest.raises(ValueError):
            net.set_setup(ssid, password)
    assert profile.read_text() == old
    assert not net.busy


@pytest.mark.parametrize("failure", ["load", "persist"])
def test_setup_edit_rolls_back_load_or_persist_failure(tmp_path, monkeypatch, failure):
    net, profile = editable_network(tmp_path, monkeypatch)
    old_profile, old_config = profile.read_text(), (tmp_path / "setup.json").read_text()
    calls = []

    def nm(*args, **kwargs):
        calls.append(args)
        if failure == "load" and len(calls) == 1:
            raise RuntimeError("failure")
        return ""

    original_write = service.private_write

    def write(path, text):
        if failure == "persist" and path == tmp_path / "setup.json" and "New Camera" in text:
            raise OSError("disk full")
        original_write(path, text)

    monkeypatch.setattr(service, "nm", nm)
    monkeypatch.setattr(service, "private_write", write)
    with pytest.raises(ValueError, match="Previous details restored"):
        net.set_setup("New Camera", "new-password")
    assert profile.read_text() == old_profile
    assert (tmp_path / "setup.json").read_text() == old_config
    assert net.setup["ssid"] == "Setup" and not net.busy
    assert not any("up" in args or "delete" in args for args in calls)


@pytest.mark.parametrize("activation_error", [RuntimeError("failure"), OSError("failed spawn"),
                                             service.subprocess.TimeoutExpired("nmcli", 30)])
def test_active_setup_restarts_after_response_and_rolls_back_failed_activation(tmp_path, monkeypatch, activation_error):
    net, profile = editable_network(tmp_path, monkeypatch, active=True)
    old_profile, old_config = profile.read_text(), (tmp_path / "setup.json").read_text()
    scheduled, calls = [], []

    class Thread:
        def __init__(self, target, args, daemon):
            scheduled.append((target, args))
        def start(self):
            pass

    def nm(*args, **kwargs):
        calls.append(args)
        if "up" in args and sum("up" in item for item in calls) == 1:
            raise activation_error
        return ""

    monkeypatch.setattr(service.threading, "Thread", Thread)
    monkeypatch.setattr(service, "nm", nm)
    result = net.set_setup("New Camera", "new-password")
    assert result["setup"]["ssid"] == "New Camera" and result["setup"]["active"]
    assert net.busy and not any("up" in args for args in calls)
    target, args = scheduled.pop()
    target(*args)
    assert sum("up" in args for args in calls) == 2
    assert profile.read_text() == old_profile
    assert (tmp_path / "setup.json").read_text() == old_config
    assert net.state == "failed" and "Previous details restored" in net.result and not net.busy
    assert net.target["uuid"] == "phone"


def test_active_setup_does_not_interrupt_a_new_router_connection(tmp_path, monkeypatch):
    net, profile = editable_network(tmp_path, monkeypatch, active=True)
    previous, old_profile, old_config = dict(net.setup), profile.read_text(), (tmp_path / "setup.json").read_text()
    calls = []
    monkeypatch.setattr(service, "nm", lambda *args, **kwargs: calls.append(args) or "")
    monkeypatch.setattr(net, "current_uuid", lambda: "router")
    net.busy = True
    net.reactivate_setup(previous, old_profile, old_config)
    assert not calls and not net.busy and net.state == "saved"


def test_active_setup_reports_failed_recovery_when_old_ap_cannot_restart(tmp_path, monkeypatch):
    net, profile = editable_network(tmp_path, monkeypatch, active=True)
    previous, old_profile, old_config = dict(net.setup), profile.read_text(), (tmp_path / "setup.json").read_text()

    def nm(*args, **kwargs):
        if "up" in args:
            raise OSError("failed spawn")
        return ""

    monkeypatch.setattr(service, "nm", nm)
    net.busy = True
    net.reactivate_setup(previous, old_profile, old_config)
    assert not net.busy and net.state == "failed" and "Connect Ethernet" in net.result


def test_setup_rollback_failure_requires_explicit_recovery(tmp_path, monkeypatch):
    net, profile = editable_network(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "nm", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("failure")))
    with pytest.raises(ValueError, match="Connect Ethernet"):
        net.set_setup("New Camera", "new-password")
    assert net.state == "failed" and not net.busy


def test_setup_status_uses_active_uuid_and_revision(tmp_path, monkeypatch):
    net, _ = editable_network(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "nm", lambda *args, **kwargs:
                        f"GENERAL.STATE:100 (connected)\nGENERAL.CONNECTION:Camera\nGENERAL.CON-UUID:{net.setup['uuid']}\nIP4.ADDRESS[1]:10.42.0.1/24")
    result = net.status()["setup"]
    assert result["active"]
    assert result["revision"] == net.setup["uuid"]
    assert result["url"] == "http://10.42.0.1:8081"
