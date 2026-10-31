"""Loopback-only dashboard fixture; never starts cameras, streams or device jobs."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parents[1]))
from reco_field.config import CameraSettings, FieldSettings
from reco_field.app import SetupWifiRequest
from reco_field.network import wifi_qr_png


STATIC = Path(__file__).parents[1] / "reco_field" / "static"
PITCH = b'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 360"><rect width="1280" height="360" fill="#315a43"/><g fill="none" stroke="#cedfd2" stroke-width="2" opacity=".8"><path d="M80 32H1200V328H80ZM640 32V328"/><circle cx="640" cy="180" r="62"/><path d="M80 92H230V268H80M1200 92H1050V268H1200M80 132H140V228H80M1200 132H1140V228H1200"/></g></svg>'''
VIEWPORTS = b'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reco Field viewport fixture</title>
<style>:root{font:16px/1.5 system-ui;color-scheme:light}body{margin:0;background:#e8e8ed;color:#1d1d1f}header{position:sticky;top:0;z-index:2;padding:16px 24px;background:#fff;border-bottom:1px solid #c5c5ca}h1{margin:0 0 12px;font-size:18px}.controls{display:flex;flex-wrap:wrap;gap:16px}label{display:flex;gap:8px;align-items:center}select{font:inherit;min-height:44px;min-width:90px}#viewport-result{font-size:13px;color:#515157;margin:12px 0 0}#stage{padding:24px;overflow:auto}iframe{display:block;width:375px;height:812px;border:0;box-shadow:0 8px 30px #0002;background:white;color-scheme:light}</style>
<header><h1>Dashboard viewport fixture</h1><div class="controls"><label>Width<select id="width"><option>320</option><option selected>375</option><option>768</option><option>1024</option><option>1440</option></select></label><label>Height<select id="height"><option>375</option><option>768</option><option selected>812</option><option>900</option><option>1024</option></select></label><label>Appearance<select id="theme"><option value="light">Light</option><option value="dark">Dark</option></select></label></div><p id="viewport-result" role="status">Loading the fixture</p></header><div id="stage"><iframe id="dashboard" src="/#live" title="Reco Field dashboard test viewport"></iframe></div>
<script>const frame=document.querySelector('#dashboard');function report(){try{const doc=frame.contentDocument;const view=frame.contentWindow;document.querySelector('#viewport-result').textContent=`Viewport ${view.innerWidth} x ${view.innerHeight} | Scheme: ${view.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'} | Document width: ${doc.documentElement.scrollWidth}px`;}catch{}}function apply(){frame.style.width=document.querySelector('#width').value+'px';frame.style.height=document.querySelector('#height').value+'px';frame.style.colorScheme=document.querySelector('#theme').value;requestAnimationFrame(report);}for(const control of document.querySelectorAll('select'))control.addEventListener('change',apply);frame.addEventListener('load',apply);setInterval(report,1000);</script></html>'''
LOCK = threading.Lock()
SCENARIO = "idle"
LIBRARY_READS = 0
OFFLINE_SESSION = "fixture-earlier"
SETUP_WIFI = {"ssid": "Reco Field Demo", "password": "CameraDemo123", "revision": "fixture-initial",
              "url": "http://10.42.0.1:8081", "address": "10.42.0.1", "active": False}
SETTINGS = FieldSettings(camera=CameraSettings(
    sensor_mode=0, capture_width=4032, capture_height=3040, capture_fps=30,
    output_width=3072, output_height=864, projection="panorama",
)).model_dump(mode="json")
SETTINGS["destinations"] = [
    {"id": "club", "name": "Club channel", "provider": "youtube", "enabled": True,
     "url": "rtmps://example.invalid/live/***"},
    {"id": "backup", "name": "Match backup", "provider": "custom", "enabled": False,
     "url": "rtmp://example.invalid/match/***"},
]


def status():
    now = datetime.now(timezone.utc)
    sample = (now - timedelta(seconds=30) if SCENARIO == "stale" else now).isoformat()
    active = SCENARIO in {"recording", "stream-failed", "sources", "sources-finishing"}
    source_recording = SCENARIO in {"sources", "sources-finishing"}
    previewing = SCENARIO == "preview"
    capture_state = "stopping" if SCENARIO == "sources-finishing" else "running" if active or previewing else "failed" if SCENARIO == "failed" else "idle"
    data = {
        "record": {"state": capture_state if active else "failed" if SCENARIO == "failed" else "idle",
                   "error": "The camera is unavailable. Check the connection and try again." if SCENARIO == "failed" else "",
                   "elapsed_seconds": 167 if active else 0,
                   "recording": {"name": "reco-20261031-080000.mkv", "size": 1_428_000_000} if active and not source_recording else None,
                   "stream_enabled": SCENARIO == "stream-failed"},
        "calibration": {"state": "idle", "valid": True, "error": "", "progress": "",
                        "elapsed_seconds": 0, "modified_at": now.isoformat(),
                        "left_lens_profile": True, "right_lens_profile": True},
        "snapshots": {"state": "idle", "error": "", "progress": "",
                      "left": {"url": "/api/snapshots/left.jpg", "modified_at": now.isoformat()},
                      "right": {"url": "/api/snapshots/right.jpg", "modified_at": now.isoformat()}},
        "lens": {"state": "idle", "side": "left", "error": "", "progress": "",
                 "minimum_views": 16, "maximum_views": 32,
                 "lenses": {side: {"views": 0, "quality": None, "ready": False,
                                    "preview": None, "preview_revision": "", "revision": ""}
                            for side in ("left", "right")}},
        "relay": {"state": "running", "managed": False},
        "destinations": [{"id": "club", "state": "retrying", "attempt": 2,
                          "error": "The destination is not responding."}] if SCENARIO == "stream-failed" else [],
        "stream_publisher": {"profile": "mobile", "state": "running" if active else "idle",
                             "error": "", "attempt": 1 if active else 0, "video_bitrate": 6_000_000},
        "last_recording": None, "simulator": True,
        "device": {"sampled_at": sample, "cpu_percent": 41.3, "cpu_cores": 6,
                   "cpu_temperature_c": 63.2, "memory_used_bytes": 2_468_000_000,
                   "memory_total_bytes": 7_979_000_000, "uptime_seconds": 18429,
                   "model": "Jetson Orin Nano Super · UI fixture",
                   "storage": {"state": "ready", "free_bytes": 978_000_000_000,
                               "total_bytes": 1_024_000_000_000}},
        "capture": {"kind": "preview" if previewing else "recording" if active else None,
                    "state": capture_state, "error": "", "mode": "sources" if source_recording else "panorama" if active else None,
                    "session_id": "fixture-active" if source_recording else None,
                    "elapsed_seconds": 167 if active else 42 if previewing else 0,
                    "bytes": 87_000_000 if source_recording else 1_428_000_000 if active else 0,
                    "outputs": source_session("recording")["outputs"] if source_recording else [],
                    "metrics": {"sampled_at": sample, "session_id": "fixture-process",
                                "processed_frames": 4896, "fps": 9.8 if source_recording else 29.36}},
        "capabilities": {"preview": SCENARIO != "no-capabilities",
                         "recording_modes": ["panorama"] if SCENARIO == "no-capabilities" else ["panorama", "sources"],
                         "offline_stitch": SCENARIO != "no-capabilities"},
        "offline_job": {"id": "fixture-export" if SCENARIO.startswith("offline-") else None,
                        "session_id": OFFLINE_SESSION if SCENARIO.startswith("offline-") else None,
                        "state": SCENARIO.removeprefix("offline-") if SCENARIO.startswith("offline-") else "idle",
                        "error": "The source could not be decoded. Original camera files are preserved." if SCENARIO == "offline-failed" else "",
                        "processed_frames": 1234 if SCENARIO.startswith("offline-") else None,
                        "fps": 4.8 if SCENARIO == "offline-running" else None,
                        "started_at": now.isoformat() if SCENARIO.startswith("offline-") else None,
                        "finished_at": now.isoformat() if SCENARIO in {"offline-complete", "offline-failed", "offline-cancelled"} else None,
                        "output": export_output(OFFLINE_SESSION, True) if SCENARIO == "offline-complete" else None,
                        "sync": None},
    }
    if SCENARIO == "missing":
        data.pop("device")
        data.pop("capture")
    return data


def source_session(state="complete", identity="fixture-active"):
    complete = state == "complete"
    result = {"id": identity, "started_at": "2026-10-31T00:00:00Z", "mode": "sources", "state": state,
            "error": "Capture was interrupted; the files have been preserved." if state == "interrupted" else "",
            "size": 87_000_000, "duration_seconds": 71.2 if complete else None,
            "can_stitch": complete and SCENARIO not in {"preview", "recording", "sources", "sources-finishing", "stream-failed", "offline-starting", "offline-running", "offline-stopping", "no-capabilities"},
            "outputs": [{"role": side, "name": f"{side}.mkv", "size": 43_500_000,
                         "width": 2016, "height": 1520, "codec": "mjpeg", "fps": 30,
                         "duration_seconds": 71.2 if complete else None, "complete": complete,
                         "download_url": f"/api/sessions/{identity}/{side}.mkv" if complete else None}
                        for side in ("left", "right")]}
    if identity == OFFLINE_SESSION and SCENARIO in {"offline-complete", "offline-failed", "offline-cancelled"}:
        result["outputs"].append(export_output(identity, SCENARIO == "offline-complete"))
        result["size"] += result["outputs"][-1]["size"]
    return result


def export_output(identity, complete):
    name = "export-0123456789abcdef0123456789abcdef.mkv"
    return {"role": "export", "name": name, "size": 18_000_000, "width": 3072, "height": 864,
            "codec": "h264", "fps": 30, "duration_seconds": 71.2 if complete else None,
            "complete": complete, "download_url": f"/api/sessions/{identity}/{name}" if complete else None}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, data, content_type="application/json", code=200):
        body = json.dumps(data).encode() if content_type == "application/json" else data
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global LIBRARY_READS
        route = urlsplit(self.path).path
        with LOCK:
            if route == "/api/session":
                return self.send({"csrf": "dashboard-fixture"})
            if route == "/api/settings":
                return self.send(SETTINGS)
            if route == "/api/status":
                if SCENARIO == "disconnected":
                    return self.send({"detail": "Fixture disconnected"}, code=503)
                return self.send(status())
            if route == "/api/preflight":
                return self.send({"ready": True, "simulator": True,
                                  "platform": {"machine": "aarch64", "release": "5.15 · UI fixture"},
                                  "checks": {name: {"state": "ready", "detail": detail}
                                             for name, detail in {"camera": "Two cameras available",
                                                                  "vulkan": "GPU renderer ready", "reco": "reco 0.5.4",
                                                                  "ffmpeg": "H.264 encoder available", "disk": "SSD ready",
                                                                  "relay": "Local preview ready"}.items()}})
            if route == "/api/network":
                return self.send({"available": True, "connected": True, "busy": False,
                                  "connection": "Club hotspot", "interface": "wlan0", "address": "192.0.2.10",
                                  "setup": {**SETUP_WIFI, "active": SCENARIO == "wifi-active"}})
            if route == "/api/network/setup/qr.png":
                return self.send(wifi_qr_png(SETUP_WIFI["ssid"], SETUP_WIFI["password"]), "image/png")
            if route == "/api/recordings":
                LIBRARY_READS += 1
                return self.send([
                    {"name": "reco-20261030-160000.mkv", "size": 1_270_261_996,
                     "modified_at": "2026-10-30T16:02:00+08:00", "download_url": "/api/recordings/reco-20261030-160000.mkv",
                     "duration_seconds": 114.8, "width": 3072, "height": 864, "codec": "h264", "metadata_state": "ready"},
                    {"name": "reco-20261029-180000.mkv", "size": 435_872_688,
                     "modified_at": "2026-10-29T18:01:00+08:00", "download_url": "/api/recordings/reco-20261029-180000.mkv",
                     "duration_seconds": 41.3 if LIBRARY_READS > 1 else None,
                     "width": 3072 if LIBRARY_READS > 1 else None, "height": 864 if LIBRARY_READS > 1 else None,
                     "codec": "h264" if LIBRARY_READS > 1 else None,
                     "metadata_state": "ready" if LIBRARY_READS > 1 else "pending"},
                    {"name": "older-recording.mkv", "size": 58_000_000,
                     "modified_at": "2026-10-28T12:01:00+08:00", "download_url": "/api/recordings/older-recording.mkv"},
                ])
            if route == "/api/sessions":
                sessions = [source_session("complete", "fixture-earlier")]
                if SCENARIO in {"sources", "sources-finishing", "complete-sources", "interrupted"}:
                    session_state = {"sources": "recording", "sources-finishing": "finishing",
                                     "complete-sources": "complete", "interrupted": "interrupted"}[SCENARIO]
                    sessions.insert(0, source_session(session_state))
                return self.send(sessions)
        if route.startswith("/api/snapshots/"):
            return self.send(PITCH, "image/svg+xml")
        if route == "/__fixture/pitch.svg":
            return self.send(PITCH, "image/svg+xml")
        if route == "/__fixture/viewports":
            return self.send(VIEWPORTS, "text/html; charset=utf-8")
        if route == "/assets/hls.min.js":
            return self.send(b"window.Hls=class{static isSupported(){return true}static Events={MANIFEST_PARSED:'manifest',ERROR:'error'};static ErrorTypes={MEDIA_ERROR:'media'};loadSource(){}attachMedia(){}on(){}destroy(){}startLoad(){}recoverMediaError(){}};", "text/javascript")
        if route == "/":
            html = (STATIC / "index.html").read_text(encoding="utf-8").replace('id="preview-video"', 'poster="/__fixture/pitch.svg" id="preview-video"')
            return self.send(html.encode(), "text/html; charset=utf-8")
        if route.startswith("/assets/"):
            filename = route.removeprefix("/assets/")
            if filename not in {"app.js", "app.css"}:
                return self.send({"detail": "Not found"}, code=404)
            return self.send((STATIC / filename).read_bytes(), "text/javascript" if filename.endswith(".js") else "text/css")
        self.send({"detail": "Not found"}, code=404)

    def do_POST(self):
        global SCENARIO, OFFLINE_SESSION, SETUP_WIFI
        route = urlsplit(self.path).path
        length = min(int(self.headers.get("Content-Length", 0)), 65536)
        payload = json.loads(self.rfile.read(length) or b"{}")
        with LOCK:
            if route == "/__fixture/scenario":
                scenario = payload.get("scenario")
                if scenario not in {"idle", "preview", "recording", "sources", "sources-finishing", "complete-sources", "interrupted", "no-capabilities", "stream-failed", "missing", "stale", "failed", "disconnected", "offline-starting", "offline-running", "offline-stopping", "offline-complete", "offline-failed", "offline-cancelled", "wifi-active"}:
                    return self.send({"detail": "Unknown fixture scenario"}, code=400)
                SCENARIO = scenario
                return self.send({"scenario": SCENARIO})
            if self.headers.get("X-CSRF-Token") != "dashboard-fixture":
                return self.send({"detail": "Missing fixture CSRF token"}, code=403)
            if route in {"/api/recordings/start", "/api/recordings/stop"}:
                SCENARIO = ("sources" if payload.get("mode") == "sources" else "recording") if route.endswith("start") else "complete-sources" if SCENARIO in {"sources", "sources-finishing"} else "idle"
                return self.send(status())
            if route in {"/api/preview/start", "/api/preview/stop"}:
                SCENARIO = "preview" if route.endswith("start") else "idle"
                return self.send(status())
            if route.startswith("/api/sessions/") and (route.endswith("/stitch") or route.endswith("/stitch/cancel")):
                OFFLINE_SESSION = route.split("/")[3]
                if OFFLINE_SESSION not in {"fixture-active", "fixture-earlier"}:
                    return self.send({"detail": "Unknown fixture session"}, code=404)
                SCENARIO = "offline-cancelled" if route.endswith("/cancel") else "offline-running"
                return self.send(status())
            if route == "/api/network/scan":
                return self.send({"networks": [{"ssid": "Club hotspot", "signal": 90, "security": "WPA2"}]})
            if route == "/api/network/connect":
                return self.send({"accepted": True, "message": "Fixture saved; no network changes were made."})
            if route == "/api/network/setup":
                if status()["capture"]["state"] in {"starting", "running", "stopping"}:
                    return self.send({"detail": "Stop recording before changing Wi-Fi."}, code=409)
                try:
                    update = SetupWifiRequest(**payload)
                except ValueError:
                    return self.send({"detail": "Check the network name and password."}, code=400)
                SETUP_WIFI = {**SETUP_WIFI, "ssid": update.ssid, "password": update.password or SETUP_WIFI["password"],
                              "revision": uuid4().hex, "active": SCENARIO == "wifi-active"}
                return self.send({"accepted": True, "setup": SETUP_WIFI,
                                  "message": "Camera Wi-Fi updated. Fixture only; no device network changed."})
        self.send({"detail": "Operation is unavailable in this UI fixture"}, code=400)

    def do_PUT(self):
        global SETTINGS
        if self.path != "/api/settings" or self.headers.get("X-CSRF-Token") != "dashboard-fixture":
            return self.send({"detail": "Invalid fixture request"}, code=403)
        payload = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 65536)))
        with LOCK:
            SETTINGS = payload
        self.send(SETTINGS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Dashboard fixture: http://127.0.0.1:{server.server_port}/", flush=True)
    server.serve_forever()
