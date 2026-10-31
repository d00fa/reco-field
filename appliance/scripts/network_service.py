#!/usr/bin/env python3
"""Restricted local NetworkManager bridge for Reco Field."""
from __future__ import annotations

import grp
import json
import os
import pwd
import re
import secrets
import socket
import socketserver
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path

STATE = Path("/var/lib/reco-field-network")
PROFILES = Path("/etc/NetworkManager/system-connections")
SOCKET = Path("/run/reco-field-network/control.sock")


def device_url() -> str:
    return f"http://{socket.gethostname().split('.')[0]}.local:8081"


def nm(*args: str, timeout: int = 15) -> str:
    result = subprocess.run(["/usr/bin/nmcli", *args], capture_output=True,
                            text=True, timeout=timeout, env={**os.environ, "LC_ALL": "C"})
    if result.returncode:
        raise RuntimeError("NetworkManager could not complete this operation.")
    return result.stdout.strip()


def validate_wifi(ssid: str, password: str) -> None:
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32:
        raise ValueError("Wi-Fi name must contain 1–32 UTF-8 bytes.")
    if any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        raise ValueError("Wi-Fi name contains an unsupported control character.")
    if not isinstance(password, str) or not (
        (8 <= len(password) <= 63 and all(32 <= ord(c) <= 126 for c in password))
        or re.fullmatch(r"[0-9a-fA-F]{64}", password)
    ):
        raise ValueError("Use a WPA2/WPA3-personal hotspot password of 8–63 characters.")


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(" ", "\\s")


def profile_text(identity: str, ssid: str, password: str, interface: str, ap: bool) -> str:
    validate_wifi(ssid, password)
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,32}", interface):
        raise ValueError("Unsupported Wi-Fi interface.")
    uuid.UUID(identity)
    return (
        f"[connection]\nid=Reco Field {'setup' if ap else 'hotspot'}\nuuid={identity}\n"
        f"type=wifi\ninterface-name={interface}\nautoconnect={'false' if ap else 'true'}\n"
        "autoconnect-priority=100\nautoconnect-retries=2\n\n"
        f"[wifi]\nmode={'ap' if ap else 'infrastructure'}\nssid={escape(ssid)}\n"
        + ("band=bg\n" if ap else "hidden=true\n")
        + f"\n[wifi-security]\nkey-mgmt=wpa-psk\npsk={escape(password)}\npsk-flags=0\n\n"
        + ("[ipv4]\nmethod=shared\naddress1=10.42.0.1/24\n" if ap else "[ipv4]\nmethod=auto\n")
        + "\n[ipv6]\nmethod=disabled\n"
    )


def private_write(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


class Network:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.busy = False
        self.result = ""
        self.state = "idle"
        self.target = None
        self.interface = self.find_interface()
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (STATE / "target.json").exists():
            self.target = json.loads((STATE / "target.json").read_text())
        config = STATE / "setup.json"
        if config.exists():
            self.setup = json.loads(config.read_text())
        else:
            self.setup = {"uuid": str(uuid.uuid4()), "ssid": "RecoField-" + secrets.token_hex(2),
                          "password": secrets.token_urlsafe(12)}
            private_write(config, json.dumps(self.setup))
        if self.interface:
            self.install_profile(self.setup["uuid"], self.setup["ssid"], self.setup["password"], True)

    @staticmethod
    def find_interface() -> str:
        for line in nm("-t", "-f", "DEVICE,TYPE", "device", "status").splitlines():
            name, _, kind = line.partition(":")
            if kind == "wifi" and re.fullmatch(r"[a-zA-Z0-9_.-]{1,32}", name):
                return name
        return ""

    def install_profile(self, identity: str, ssid: str, password: str, ap: bool) -> Path:
        path = PROFILES / f"reco-field-{identity}.nmconnection"
        private_write(path, profile_text(identity, ssid, password, self.interface, ap))
        nm("connection", "load", str(path))
        return path

    def current_uuid(self) -> str:
        return nm("-g", "GENERAL.CON-UUID", "device", "show", self.interface)

    def status(self) -> dict:
        if not self.interface:
            return {"available": False, "error": "No Wi-Fi adapter found."}
        details = nm("-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION,GENERAL.CON-UUID,IP4.ADDRESS", "device", "show", self.interface)
        fields = {}
        for line in details.splitlines():
            key, _, value = line.partition(":")
            fields.setdefault(key, value.replace("\\:", ":").replace("\\\\", "\\"))
        return {"available": True, "interface": self.interface,
                "connected": fields.get("GENERAL.STATE", "").startswith("100"),
                "connection": fields.get("GENERAL.CONNECTION", ""),
                "address": fields.get("IP4.ADDRESS[1]", "").split("/")[0],
                "busy": self.busy, "state": self.state, "message": self.result,
                "setup": self.setup_status(fields.get("GENERAL.CON-UUID") == self.setup["uuid"]),
                "url": device_url()}

    def setup_status(self, active: bool) -> dict:
        return {"ssid": self.setup["ssid"], "password": self.setup["password"],
                "url": "http://10.42.0.1:8081", "address": "10.42.0.1",
                "active": active, "revision": self.setup.get("revision", self.setup["uuid"])}

    def set_setup(self, ssid: str, password: str) -> dict:
        if not self.interface:
            raise ValueError("No Wi-Fi adapter found.")
        with self.lock:
            if self.busy:
                raise ValueError("A Wi-Fi change is already in progress.")
            if not isinstance(password, str):
                raise ValueError("Use a Wi-Fi password of 8–63 characters.")
            if password and not 8 <= len(password) <= 63:
                raise ValueError("Use a Wi-Fi password of 8–63 characters.")
            password = password or self.setup["password"]
            validate_wifi(ssid, password)
            active = self.current_uuid() == self.setup["uuid"]
            if ssid == self.setup["ssid"] and password == self.setup["password"]:
                return {"available": True, "accepted": True, "message": "Camera Wi-Fi is unchanged.",
                        "setup": self.setup_status(active)}
            previous = dict(self.setup)
            path = PROFILES / f"reco-field-{previous['uuid']}.nmconnection"
            old_profile = path.read_text(encoding="utf-8")
            old_config = (STATE / "setup.json").read_text(encoding="utf-8")
            self.busy, self.state, self.result = True, "updating_setup", "Saving camera Wi-Fi."
            try:
                self.install_profile(previous["uuid"], ssid, password, True)
                updated = {**previous, "ssid": ssid, "password": password, "revision": uuid.uuid4().hex}
                private_write(STATE / "setup.json", json.dumps(updated))
                self.setup = updated
                if active:
                    self.result = "Saved. Camera Wi-Fi will restart; scan the new code to reconnect."
                    threading.Thread(target=self.reactivate_setup,
                                     args=(previous, old_profile, old_config), daemon=True).start()
                else:
                    self.busy, self.state, self.result = False, "saved", "Camera Wi-Fi updated."
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                self.restore_setup(previous, old_profile, old_config, active)
                self.busy = False
                raise ValueError(self.result) from None
            return {"available": True, "accepted": True, "message": self.result,
                    "setup": self.setup_status(active)}

    def restore_setup(self, previous: dict, old_profile: str, old_config: str, active: bool) -> None:
        path = PROFILES / f"reco-field-{previous['uuid']}.nmconnection"
        restored = True
        profile_ready = False
        try:
            private_write(path, old_profile)
            nm("connection", "load", str(path))
            profile_ready = True
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            restored = False
        try:
            private_write(STATE / "setup.json", old_config)
        except OSError:
            restored = False
        if active and profile_ready:
            try:
                nm("--wait", "25", "connection", "up", "uuid", previous["uuid"], "ifname", self.interface, timeout=30)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                restored = False
        self.setup = previous
        self.state = "failed"
        self.result = ("Camera Wi-Fi could not be updated. Previous details restored." if restored else
                       "Camera Wi-Fi could not be restored. Connect Ethernet and retry.")

    def reactivate_setup(self, previous: dict, old_profile: str, old_config: str) -> None:
        time.sleep(4)
        try:
            if self.current_uuid() == self.setup["uuid"]:
                nm("--wait", "25", "connection", "up", "uuid", self.setup["uuid"], "ifname", self.interface, timeout=30)
            self.state, self.result = "saved", "Camera Wi-Fi updated."
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            self.restore_setup(previous, old_profile, old_config, True)
        finally:
            with self.lock:
                self.busy = False

    def scan(self) -> dict:
        if self.busy:
            raise ValueError("A Wi-Fi change is already in progress.")
        output = nm("-t", "--escape", "no", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                    "ifname", self.interface, "--rescan", "yes", timeout=18)
        networks = {}
        for line in output.splitlines():
            parts = line.rsplit(":", 2)
            if len(parts) == 3 and parts[0] and parts[1].isdigit():
                item = {"ssid": parts[0], "signal": int(parts[1]), "security": parts[2]}
                if item["signal"] > networks.get(parts[0], {"signal": -1})["signal"]:
                    networks[parts[0]] = item
        return {"available": True, "networks": sorted(networks.values(), key=lambda n: -n["signal"])}

    def connect(self, ssid: str, password: str) -> dict:
        validate_wifi(ssid, password)
        if not self.interface:
            raise ValueError("No Wi-Fi adapter found.")
        with self.lock:
            if self.busy:
                raise ValueError("A Wi-Fi change is already in progress.")
            previous = self.current_uuid()
            identity = str(uuid.uuid4())
            self.install_profile(identity, ssid, password, False)
            self.busy, self.state, self.result = True, "connecting", "Connecting to the phone hotspot."
        threading.Thread(target=self.activate, args=(identity, previous, ssid), daemon=True).start()
        return {"available": True, "accepted": True, "message": f"Saved. Enable your phone hotspot now. The page may disconnect; reopen {device_url()}."}

    def activate(self, identity: str, previous: str, ssid: str, new_profile: bool = True) -> None:
        time.sleep(4)
        try:
            nm("--wait", "55", "connection", "up", "uuid", identity, "ifname", self.interface, timeout=60)
            self.target = {"uuid": identity, "ssid": ssid}
            private_write(STATE / "target.json", json.dumps(self.target))
            self.state, self.result = "connected", "Phone hotspot connected."
        except (RuntimeError, subprocess.TimeoutExpired):
            self.state, self.result = "failed", "Could not connect. Check the hotspot name/password and enable compatibility mode on your phone."
            if new_profile:
                try:
                    nm("connection", "delete", "uuid", identity)
                except (RuntimeError, subprocess.TimeoutExpired):
                    pass
            try:
                if previous and previous != "--":
                    nm("--wait", "25", "connection", "up", "uuid", previous, "ifname", self.interface, timeout=30)
                else:
                    self.start_setup()
            except (RuntimeError, subprocess.TimeoutExpired):
                self.start_setup()
        finally:
            with self.lock:
                self.busy = False

    def start_setup(self) -> None:
        try:
            nm("--wait", "25", "connection", "up", "uuid", self.setup["uuid"], "ifname", self.interface, timeout=30)
        except (RuntimeError, subprocess.TimeoutExpired):
            self.result = "Setup Wi-Fi could not start. Connect Ethernet to recover."

    def watchdog(self) -> None:
        disconnected_at = None
        last_scan = 0.0
        while True:
            time.sleep(10)
            if not self.interface or self.busy:
                disconnected_at = None
                continue
            try:
                connected = self.status()["connected"]
            except (RuntimeError, subprocess.TimeoutExpired):
                continue
            if connected:
                disconnected_at = None
                if self.target and time.monotonic() - last_scan > 30:
                    last_scan = time.monotonic()
                    try:
                        if self.current_uuid() == self.setup["uuid"]:
                            networks = self.scan()["networks"]
                            if any(item["ssid"] == self.target["ssid"] for item in networks):
                                with self.lock:
                                    if self.busy:
                                        continue
                                    self.busy = True
                                self.activate(self.target["uuid"], self.setup["uuid"], self.target["ssid"], False)
                    except (RuntimeError, ValueError, subprocess.TimeoutExpired):
                        pass
            else:
                disconnected_at = disconnected_at or time.monotonic()
                if time.monotonic() - disconnected_at >= 60:
                    with self.lock:
                        if self.busy:
                            continue
                        self.busy = True
                    try:
                        self.start_setup()
                    finally:
                        self.busy = False
                        disconnected_at = None


def main() -> None:
    allowed_uid = pwd.getpwnam("recofield").pw_uid
    group = grp.getgrnam("recofield").gr_gid
    network = Network()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            _, uid, _ = struct.unpack("3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid not in (0, allowed_uid):
                return
            try:
                raw = self.rfile.readline(4097)
                if len(raw) > 4096:
                    raise ValueError("Request too large.")
                data = json.loads(raw)
                action = data.get("action")
                if action == "status":
                    result = network.status()
                elif action == "scan":
                    result = network.scan()
                elif action == "connect":
                    result = network.connect(data.get("ssid"), data.get("password"))
                elif action == "set_setup":
                    result = network.set_setup(data.get("ssid"), data.get("password", ""))
                else:
                    raise ValueError("Unsupported network operation.")
            except ValueError as exc:
                result = {"available": True, "error": str(exc)}
            except Exception:
                result = {"available": False, "error": "Network setup could not complete. Please retry."}
            try:
                self.wfile.write(json.dumps(result).encode() + b"\n")
            except OSError:
                pass

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    SOCKET.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(SOCKET.parent, 0, group)
    SOCKET.unlink(missing_ok=True)
    with Server(str(SOCKET), Handler) as server:
        os.chown(SOCKET, 0, group)
        os.chmod(SOCKET, 0o660)
        threading.Thread(target=network.watchdog, daemon=True).start()
        server.serve_forever()


if __name__ == "__main__":
    main()
