from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

MOBILE_STREAM_BITRATE = 6_000_000

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


AWB_MODES = (
    "off",
    "auto",
    "incandescent",
    "fluorescent",
    "tungsten",
    "daylight",
    "cloudy",
    "twilight",
    "shade",
    "warm-fluorescent",
)


class DestinationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    name: str = Field(min_length=1, max_length=64)
    provider: Literal["youtube", "facebook", "twitch", "custom"]
    url: str | None = Field(default=None, max_length=4096)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("destination name cannot be blank")
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        return validate_ingest_url(value) if value else value


class CameraSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left_device: str = Field(default="0", pattern=r"^[0-9]+$")
    right_device: str = Field(default="1", pattern=r"^[0-9]+$")
    sensor_mode: int = Field(default=1, ge=0, le=255)
    capture_width: int = Field(default=3840, ge=320, le=8192)
    capture_height: int = Field(default=2160, ge=180, le=8192)
    capture_fps: int = Field(default=30, ge=1, le=60)
    output_width: int = Field(default=1920, ge=320, le=4096)
    output_height: int = Field(default=1080, ge=180, le=2160)
    projection: Literal["perspective", "panorama"] = "perspective"
    encoder: Literal["libx264"] = "libx264"
    codec: Literal["h264"] = "h264"
    preset: Literal[
        "ultrafast", "superfast", "veryfast", "faster", "fast", "medium"
    ] = "ultrafast"
    quality: Literal["fast", "balanced", "high"] = "high"
    blend: float = Field(default=0.15, ge=0.0, le=1.0)
    exposure_mode: Literal["auto", "manual"] = "auto"
    exposure_min_ns: int = Field(default=13_000, ge=1, le=1_000_000_000)
    exposure_max_ns: int = Field(default=683_709_000, ge=1, le=1_000_000_000)
    gain_min: float = Field(default=1.0, ge=0.1, le=64.0)
    gain_max: float = Field(default=22.25, ge=0.1, le=64.0)
    digital_gain_min: float = Field(default=1.0, ge=0.1, le=8.0)
    digital_gain_max: float = Field(default=1.0, ge=0.1, le=8.0)
    awb_mode: Literal[AWB_MODES] = "auto"
    awb_lock: bool = False

    @field_validator(
        "capture_width",
        "capture_height",
        "output_width",
        "output_height",
    )
    @classmethod
    def even_dimension(cls, value: int) -> int:
        if value % 2:
            raise ValueError("dimensions must be even")
        return value

    @model_validator(mode="after")
    def validate_ranges(self) -> CameraSettings:
        if self.output_width % 4:
            raise ValueError("output width must be divisible by 4")
        if self.left_device == self.right_device:
            raise ValueError("left and right sensors must use different cameras")
        if self.exposure_max_ns < self.exposure_min_ns:
            raise ValueError("maximum exposure must be at least the minimum")
        if self.exposure_mode == "manual" and (
            self.exposure_min_ns != self.exposure_max_ns
        ):
            raise ValueError("manual exposure requires equal minimum and maximum")
        if self.gain_max < self.gain_min:
            raise ValueError("maximum gain must be at least the minimum")
        if self.digital_gain_max < self.digital_gain_min:
            raise ValueError("maximum digital gain must be at least the minimum")
        if self.awb_mode == "off" and self.awb_lock:
            raise ValueError("AWB lock requires an active white-balance mode")
        return self


class FieldSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    camera: CameraSettings = Field(default_factory=CameraSettings)
    destinations: list[DestinationSettings] = Field(default_factory=list, max_length=8)
    auto_stream: bool = False
    stream_profile: Literal["mobile", "original"] = "mobile"
    recording_mode: Literal["panorama", "sources", "both"] = "panorama"
    auto_record: bool = False
    min_free_gb: float = Field(default=5.0, ge=1.0, le=1000.0)
    calibration_frames: int = Field(default=3, ge=1, le=16)

    @model_validator(mode="after")
    def unique_destinations(self) -> FieldSettings:
        ids = [destination.id for destination in self.destinations]
        if len(ids) != len(set(ids)):
            raise ValueError("destination ids must be unique")
        return self


def validate_ingest_url(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 4096:
        raise ValueError("ingest URL is empty or too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("ingest URL contains control characters")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"rtmp", "rtmps", "srt"}:
        raise ValueError("ingest URL must use rtmp, rtmps, or srt")
    if not parsed.hostname:
        raise ValueError("ingest URL requires a host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("ingest URL has an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("ingest URL port must be 1..65535")
    if parsed.fragment:
        raise ValueError("ingest URL fragments are not supported")
    return value


def masked_ingest_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    host = parsed.hostname or "host"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}/***"


@dataclass(frozen=True)
class RuntimeConfig:
    data_dir: Path
    reco_bin: str
    mediamtx_bin: str
    mediamtx_config: Path
    ffmpeg_bin: str
    gst_inspect_bin: str
    gst_launch_bin: str
    bind: str
    port: int
    manage_relay: bool
    simulator: bool
    session_hours: int = 12
    secure_cookies: bool = False
    max_body_bytes: int = 1_048_576
    calibration_timeout_seconds: int = 600
    camera_start_timeout_seconds: int = 60
    recording_disk_check_seconds: float = 5.0
    log_max_bytes: int = 1_048_576
    log_backups: int = 3
    relay_rtmp_port: int = 1935
    relay_hls_port: int = 8888
    storage_mount: Path | None = None
    storage_uuid: str = ""
    network_socket: Path | None = None
    cv_python_bin: str = "/usr/bin/python3"

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def calibration_file(self) -> Path:
        return self.data_dir / "config" / "calibration.json"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "config" / "settings.json"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def relay_rtmp_url(self) -> str:
        return f"rtmp://127.0.0.1:{self.relay_rtmp_port}/reco/live"

    @property
    def relay_mobile_rtmp_url(self) -> str:
        return f"rtmp://127.0.0.1:{self.relay_rtmp_port}/reco/mobile"

    @property
    def relay_hls_base(self) -> str:
        return f"http://127.0.0.1:{self.relay_hls_port}"

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        data_dir = Path(
            os.environ.get("RECO_FIELD_DATA_DIR", "~/.local/share/reco-field")
        ).expanduser()
        return cls(
            data_dir=data_dir,
            reco_bin=os.environ.get("RECO_FIELD_RECO_BIN", "/opt/reco-field/bin/reco"),
            mediamtx_bin=os.environ.get(
                "RECO_FIELD_MEDIAMTX_BIN", "/opt/reco-field/bin/mediamtx"
            ),
            mediamtx_config=Path(
                os.environ.get(
                    "RECO_FIELD_MEDIAMTX_CONFIG",
                    str(Path(__file__).with_name("mediamtx.yml")),
                )
            ),
            ffmpeg_bin=os.environ.get("RECO_FIELD_FFMPEG_BIN", "ffmpeg"),
            gst_inspect_bin=os.environ.get(
                "RECO_FIELD_GST_INSPECT_BIN", "gst-inspect-1.0"
            ),
            gst_launch_bin=os.environ.get(
                "RECO_FIELD_GST_LAUNCH_BIN", "gst-launch-1.0"
            ),
            bind=os.environ.get("RECO_FIELD_BIND", "127.0.0.1"),
            port=int(os.environ.get("RECO_FIELD_PORT", "8080")),
            manage_relay=os.environ.get("RECO_FIELD_MANAGE_RELAY", "1") == "1",
            simulator=os.environ.get("RECO_FIELD_SIMULATOR", "0") == "1",
            secure_cookies=os.environ.get("RECO_FIELD_SECURE_COOKIES", "0") == "1",
            relay_rtmp_port=int(os.environ.get("RECO_FIELD_RTMP_PORT", "1935")),
            relay_hls_port=int(os.environ.get("RECO_FIELD_HLS_PORT", "8888")),
            storage_mount=Path(os.environ["RECO_FIELD_STORAGE_MOUNT"]) if os.environ.get("RECO_FIELD_STORAGE_MOUNT") else None,
            storage_uuid=os.environ.get("RECO_FIELD_STORAGE_UUID", ""),
            network_socket=Path(os.environ["RECO_FIELD_NETWORK_SOCKET"]) if os.environ.get("RECO_FIELD_NETWORK_SOCKET") else None,
            cv_python_bin=os.environ.get("RECO_FIELD_CV_PYTHON", "/usr/bin/python3"),
        )
