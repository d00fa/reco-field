from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import RuntimeConfig
from .storage import require_storage, safe_recording_path, safe_session_file


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip("\x00\n ")
    except (OSError, UnicodeError):
        return ""


class DeviceTelemetry:
    def __init__(self, runtime: RuntimeConfig, *, proc: Path = Path("/proc"),
                 sys: Path = Path("/sys")) -> None:
        self.runtime, self.proc, self.sys = runtime, proc, sys
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._previous_cpu: tuple[int, int] | None = None
        self._model = _text(proc / "device-tree/model") or None
        self._sample = self._empty()

    def _empty(self) -> dict[str, Any]:
        return {"sampled_at": None, "model": self._model, "cpu_percent": None,
                "cpu_cores": None, "cpu_temperature_c": None, "cpu_temperature_source": None,
                "memory_used_bytes": None, "memory_total_bytes": None, "uptime_seconds": None,
                "storage": {"state": "unavailable", "free_bytes": None, "total_bytes": None}}

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="device-telemetry", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {**self._sample, "storage": dict(self._sample["storage"])}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample()
            except OSError:
                pass  # Keep the previous timestamp so clients can identify a stale sample.
            self._stop.wait(2)

    def sample(self) -> None:
        value = self._empty()
        stat = _text(self.proc / "stat")
        try:
            fields = stat.splitlines()[0].split()
            if fields[0] != "cpu" or len(fields) < 5:
                raise ValueError("Missing aggregate CPU counters")
            counters = [int(item) for item in fields[1:9]]
            if min(counters) < 0:
                raise ValueError("Invalid CPU counters")
            total, idle = sum(counters), counters[3] + (counters[4] if len(counters) > 4 else 0)
            previous, self._previous_cpu = self._previous_cpu, (total, idle)
            if previous and total > previous[0] and 0 <= idle - previous[1] <= total - previous[0]:
                value["cpu_percent"] = round(100 * (1 - (idle - previous[1]) / (total - previous[0])), 1)
            value["cpu_cores"] = sum(bool(re.match(r"^cpu\d+\s", line)) for line in stat.splitlines()) or None
        except (ValueError, IndexError):
            self._previous_cpu = None
        try:
            memory = dict(re.findall(r"^(MemTotal|MemAvailable):\s+(\d+) kB$", _text(self.proc / "meminfo"), re.M))
            total, available = int(memory["MemTotal"]) * 1024, int(memory["MemAvailable"]) * 1024
            if total > 0 and 0 <= available <= total:
                value.update(memory_total_bytes=total, memory_used_bytes=total - available)
        except (KeyError, ValueError):
            pass
        try:
            uptime = float(_text(self.proc / "uptime").split()[0])
            if math.isfinite(uptime) and uptime >= 0:
                value["uptime_seconds"] = int(uptime)
        except (ValueError, IndexError):
            pass
        for zone in sorted((self.sys / "class/thermal").glob("thermal_zone*")):
            label = _text(zone / "type")
            if label.lower() not in {"cpu-thermal", "cpu_thermal", "cpu", "x86_pkg_temp"}:
                continue
            try:
                temperature = int(_text(zone / "temp")) / 1000
                if -40 <= temperature <= 150:
                    value.update(cpu_temperature_c=temperature, cpu_temperature_source=label)
                    break
            except ValueError:
                pass
        try:
            require_storage(self.runtime.data_dir, self.runtime.storage_mount, self.runtime.storage_uuid)
            disk = shutil.disk_usage(self.runtime.data_dir)
            value["storage"] = {"state": "ready", "free_bytes": disk.free, "total_bytes": disk.total}
        except OSError:
            pass
        value["sampled_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._sample = value


EMPTY_METADATA = {"duration_seconds": None, "width": None, "height": None, "codec": None}


def _probe_json(argv: list[str]) -> dict[str, Any]:
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        timer = threading.Timer(3, process.kill)
        timer.daemon = True
        timer.start()
        try:
            assert process.stdout is not None
            raw = process.stdout.read(65537)
            if len(raw) > 65536:
                process.kill()
                raise ValueError("Recording metadata output exceeded its limit")
            if process.wait(timeout=1) != 0:
                raise ValueError("Recording metadata unavailable")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("Invalid recording metadata")
            return result
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=1)


class RecordingMetadata:
    def __init__(self, runtime: RuntimeConfig) -> None:
        self.runtime = runtime
        self._lock = threading.Lock()
        self._cache: dict[Path, tuple[tuple[int, int], Future[dict[str, Any]]]] = {}
        self._pending: set[Future[dict[str, Any]]] = set()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recording-metadata")
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._executor.shutdown(wait=False, cancel_futures=True)

    def retain(self, paths: set[Path]) -> None:
        with self._lock:
            self._cache = {path: item for path, item in self._cache.items()
                           if path in paths or not item[1].done()}

    def get(self, path: Path, stat: os.stat_result, *, active: bool = False) -> dict[str, Any]:
        if active:
            return {**EMPTY_METADATA, "metadata_state": "recording"}
        signature = (stat.st_size, stat.st_mtime_ns)
        with self._lock:
            cached = self._cache.get(path)
            future = cached[1] if cached and cached[0] == signature else None
            if future is None and not self._closed:
                if cached and not cached[1].done():
                    return {**EMPTY_METADATA, "metadata_state": "pending"}
                self._pending = {item for item in self._pending if not item.done()}
                if len(self._pending) >= 8:
                    return {**EMPTY_METADATA, "metadata_state": "pending"}
                # Keep failures too: unchanged damaged files must not be probed on each refresh.
                future = self._executor.submit(self._probe, path, stat)
                self._cache[path] = (signature, future)
                self._pending.add(future)
            if future is None or future.cancelled():
                return {**EMPTY_METADATA, "metadata_state": "unavailable"}
            if not future.done():
                return {**EMPTY_METADATA, "metadata_state": "pending"}
            return dict(future.result())

    def _probe(self, path: Path, stat: os.stat_result) -> dict[str, Any]:
        result = {**EMPTY_METADATA, "metadata_state": "unavailable"}
        try:
            ffprobe = str(Path(self.runtime.ffmpeg_bin).with_name(
                "ffprobe.exe" if self.runtime.ffmpeg_bin.lower().endswith(".exe") else "ffprobe"))
            require_storage(self.runtime.data_dir, self.runtime.storage_mount, self.runtime.storage_uuid)
            safe = (safe_recording_path(self.runtime.recordings_dir, path.name)
                    if path.parent == self.runtime.recordings_dir.resolve() else
                    safe_session_file(self.runtime.recordings_dir, path.parent.name, path.name))
            if safe != path:
                return result
            data = _probe_json([ffprobe, "-v", "error", "-protocol_whitelist", "file", "-select_streams", "v:0",
                                "-probesize", "5000000", "-analyzeduration", "1000000", "-show_entries",
                                "stream=codec_name,width,height:format=duration", "-of", "json", "-i", str(path)])
            after = path.stat()
            if after.st_size != stat.st_size or after.st_mtime_ns != stat.st_mtime_ns:
                return result
            streams = data.get("streams", [])
            stream = streams[0] if isinstance(streams, list) and streams and isinstance(streams[0], dict) else {}
            for name in ("width", "height"):
                dimension = stream.get(name)
                if isinstance(dimension, int) and not isinstance(dimension, bool) and 0 < dimension <= 32768:
                    result[name] = dimension
            codec = stream.get("codec_name")
            if isinstance(codec, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,40}", codec):
                result["codec"] = codec
            duration = float(data.get("format", {}).get("duration", "nan"))
            if math.isfinite(duration) and duration > 0:
                result["duration_seconds"] = duration
            if result["width"] and result["height"] and result["codec"]:
                result["metadata_state"] = "ready"
        except (OSError, ValueError, TypeError, AttributeError, IndexError, subprocess.TimeoutExpired):
            pass
        return result
