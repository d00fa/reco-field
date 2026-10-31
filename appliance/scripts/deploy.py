#!/usr/bin/env python3
"""Transactional Reco Field source deployment for Jetson Ubuntu hosts."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


REQUIRED_PORTS = (1936, 8081, 8887, 9998)
UNITS = ("reco-field-relay.service", "reco-field-network.service", "reco-field.service")
MIN_BUILD_FREE_GIB = 12.0
MIN_INSTALL_FREE_GIB = 1.0
SOURCES = {
    "aarch64": (
        "aarch64-unknown-linux-gnu",
        "arm64",
        "a9c168dbea81a84671c6b7a5c1795cb0eb699020110d107ddfca427bd797e697",
        "27f038237cb5f77a724f6afc58eca2798f2bc4edf9989895631a80eeb41a4fe2",
    ),
    "x86_64": (
        "x86_64-unknown-linux-gnu",
        "amd64",
        "5be191e8c70a5007074254cb74d78dbfdd25cd5a61c27de7b8e5410122ec24f4",
        "0a2ea7eba96023631efa1bea7804823a9145b043b6646a8a05798d4ac24ed635",
    ),
}


class InstallError(RuntimeError):
    """Raised for an actionable installation failure."""


@dataclass(frozen=True)
class InstallPaths:
    source_root: Path
    app_root: Path
    data_dir: Path
    config_dir: Path
    build_dir: Path
    systemd_dir: Path
    app_user: str = "recofield"

    @property
    def releases_dir(self) -> Path:
        return self.app_root / "releases"

    @property
    def current_link(self) -> Path:
        return self.app_root / "current"

    @property
    def toolchain_dir(self) -> Path:
        return self.app_root / "toolchain"


class CommandRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=capture,
            check=check,
        )


class Installer:
    def __init__(
        self,
        paths: InstallPaths,
        *,
        runner: CommandRunner | None = None,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
        downloader: Callable[[str, Path], None] | None = None,
        health_check: Callable[[], bool] | None = None,
        health_timeout_seconds: float = 15.0,
    ) -> None:
        self.paths = paths
        self.runner = runner or CommandRunner()
        self.disk_usage = disk_usage
        self.downloader = downloader or self._download
        self.health_check = health_check or self._health_check
        self.health_timeout_seconds = health_timeout_seconds
        self.release_id = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        self.build_work = self.paths.build_dir / f"reco-field-{self.release_id}"
        self.staged_source = self.build_work / "source"
        self.staged_release = self.paths.releases_dir / f".{self.release_id}"

    def precheck(self) -> None:
        self._check_python()
        self._check_paths()
        self._check_platform()
        self._check_storage()
        errors = self._port_conflicts()
        errors.extend(self._space_errors())
        if errors:
            raise InstallError("\n".join(errors))

    def _check_paths(self) -> None:
        for label, path in (
            ("application", self.paths.app_root),
            ("data", self.paths.data_dir),
            ("configuration", self.paths.config_dir),
            ("systemd", self.paths.systemd_dir),
        ):
            if not path.is_absolute():
                raise InstallError(f"{label} path must be absolute: {path}")
            if any(character.isspace() for character in str(path)):
                raise InstallError(f"{label} path cannot contain whitespace: {path}")
        for label, path in (
            ("source", self.paths.source_root),
            ("build", self.paths.build_dir),
        ):
            if not path.is_absolute():
                raise InstallError(f"{label} path must be absolute: {path}")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*", self.paths.app_user):
            raise InstallError("service user name is invalid")

    def install(self) -> Path:
        self.precheck()
        self._check_runtime_media()
        self._ensure_user()
        self._prepare_directories()
        if not os.environ.get("RECO_FIELD_PREBUILT_RECO"):
            self._prepare_toolchain()
        release = self._stage_release()
        self._verify_units(release / "systemd")
        self._initialize_production(release)
        self._activate_release(release)
        self._cleanup_build()
        return release

    def _check_runtime_media(self) -> None:
        for command, argument in (
            ("ffmpeg", "-version"),
            ("gst-inspect-1.0", "nvarguscamerasrc"),
            ("gst-launch-1.0", "--version"),
        ):
            result = self.runner.run(
                [command, argument],
                check=False,
                capture=True,
            )
            if result.returncode != 0:
                raise InstallError(
                    f"required media command failed: {command} {argument}"
                )
        encoders = self.runner.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            check=False,
            capture=True,
        )
        if encoders.returncode != 0 or "libx264" not in (encoders.stdout or ""):
            raise InstallError("FFmpeg must provide the libx264 software encoder")

    def _check_storage(self) -> None:
        mount = Path(os.environ.get("RECO_FIELD_STORAGE_MOUNT", "/srv/reco-ssd"))
        if not mount.is_absolute() or any(c.isspace() for c in str(mount)):
            raise InstallError("SSD mount path must be absolute without whitespace")
        if not os.path.ismount(mount) or mount.stat().st_dev == Path("/").stat().st_dev:
            raise InstallError("Mount the recording SSD at /srv/reco-ssd before installation; SD recording is disabled")
        if not self.paths.data_dir.resolve().is_relative_to(mount.resolve()):
            raise InstallError("RECO_FIELD_DATA_DIR must be inside the recording SSD")
        detected = self.runner.run(["findmnt", "-nro", "UUID", "--mountpoint", str(mount)], capture=True).stdout.strip()
        expected = os.environ.get("RECO_FIELD_STORAGE_UUID", detected)
        if not detected or detected != expected or not re.fullmatch(r"[a-fA-F0-9-]{8,64}", expected):
            raise InstallError("Mounted recording SSD does not match its configured UUID")
        os.environ["RECO_FIELD_STORAGE_MOUNT"] = str(mount)
        os.environ["RECO_FIELD_STORAGE_UUID"] = expected

    def _check_python(self) -> None:
        if sys.version_info < (3, 10):
            raise InstallError("Python 3.10 or newer is required")

    def _check_platform(self) -> None:
        if platform.system() != "Linux":
            raise InstallError("Linux is required")
        if platform.machine() not in SOURCES:
            raise InstallError(f"unsupported architecture: {platform.machine()}")
        os_release = Path("/etc/os-release")
        if os_release.exists():
            release = {}
            for line in os_release.read_text().splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    release[key] = value.strip().strip('"')
            if release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "22.04":
                raise InstallError("Ubuntu 22.04 with JetPack 6 is required")
        if not (self.paths.source_root / "Cargo.lock").is_file():
            raise InstallError("source archive is incomplete: Cargo.lock is missing")
        if shutil.which("gst-inspect-1.0") is None:
            raise InstallError(
                "gst-inspect-1.0 is unavailable; install the Jetson camera stack"
            )
        argus = self.runner.run(
            ["gst-inspect-1.0", "nvarguscamerasrc"],
            check=False,
            capture=True,
        )
        if argus.returncode != 0:
            raise InstallError(
                "NVIDIA nvarguscamerasrc is unavailable; install the Jetson camera stack before Reco Field"
            )

    def _port_conflicts(self) -> list[str]:
        allowed = self._reco_listener_pids()
        errors: list[str] = []
        for port in REQUIRED_PORTS:
            result = self.runner.run(
                ["ss", "-H", "-ltnp", f"sport = :{port}"],
                check=False,
                capture=True,
            )
            if result.returncode != 0:
                errors.append(
                    f"cannot inspect TCP port {port}; install iproute2 and retry"
                )
                continue
            details = " ".join((result.stdout or "").split())
            if not details:
                continue
            pids = {int(value) for value in re.findall(r"\bpid=(\d+)", details)}
            if pids and pids.issubset(allowed):
                continue
            listener = details[:300]
            errors.append(
                f"TCP port {port} is already in use by an unrelated process: "
                f"{listener}. Reco Field will not stop or modify unknown services."
            )
        return errors

    def _reco_listener_pids(self) -> set[int]:
        allowed: set[int] = set()
        for unit in UNITS:
            result = self.runner.run(
                ["systemctl", "show", "-p", "MainPID", "--value", unit],
                check=False,
                capture=True,
            )
            if result.returncode == 0:
                value = (result.stdout or "").strip()
                if value.isdigit() and int(value) > 0:
                    allowed.add(int(value))
        return allowed

    def _space_errors(self) -> list[str]:
        errors = []
        build_free = self._free_gib(self._existing_parent(self.paths.build_dir))
        install_free = self._free_gib(
            self._existing_parent(self.paths.app_root.parent)
        )
        if build_free < MIN_BUILD_FREE_GIB:
            errors.append(
                f"build volume {self.paths.build_dir} has {build_free:.1f} GiB "
                f"free; {MIN_BUILD_FREE_GIB:.1f} GiB is required. Set "
                "RECO_FIELD_BUILD_DIR to a roomier mounted volume."
            )
        if install_free < MIN_INSTALL_FREE_GIB:
            errors.append(
                f"release volume {self.paths.app_root} has {install_free:.1f} GiB "
                f"free; {MIN_INSTALL_FREE_GIB:.1f} GiB is required."
            )
        return errors

    @staticmethod
    def _existing_parent(path: Path) -> Path:
        candidate = path
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return candidate

    def _free_gib(self, path: Path) -> float:
        return self.disk_usage(path).free / 1024**3

    def _ensure_user(self) -> None:
        result = self.runner.run(
            ["id", "-u", self.paths.app_user],
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            self.runner.run(
                [
                    "useradd",
                    "--system",
                    "--create-home",
                    "--home-dir",
                    str(self.paths.data_dir),
                    "--shell",
                    "/usr/sbin/nologin",
                    self.paths.app_user,
                ]
            )
        for group in ("video", "render"):
            exists = self.runner.run(
                ["getent", "group", group],
                check=False,
                capture=True,
            )
            if exists.returncode == 0:
                self.runner.run(
                    ["usermod", "-a", "-G", group, self.paths.app_user]
                )

    def _prepare_directories(self) -> None:
        uid, gid = self._user_ids()
        for path in (
            self.paths.app_root,
            self.paths.releases_dir,
            self.paths.toolchain_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o755)
            os.chown(path, 0, gid)
        for path in (
            self.paths.data_dir,
            self.paths.data_dir / "config",
            self.paths.data_dir / "recordings",
            self.paths.data_dir / "logs",
            self.paths.data_dir / "snapshots",
            self.paths.data_dir / "tmp",
        ):
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o700)
            os.chown(path, uid, gid)
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        self.paths.config_dir.chmod(0o750)
        os.chown(self.paths.config_dir, 0, gid)

    def _user_ids(self) -> tuple[int, int]:
        result = self.runner.run(
            ["id", "-u", self.paths.app_user],
            capture=True,
        )
        uid = int(result.stdout.strip())
        result = self.runner.run(
            ["id", "-g", self.paths.app_user],
            capture=True,
        )
        return uid, int(result.stdout.strip())

    def _run_as_app(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        extra_env: dict[str, str] | None = None,
        check: bool = True,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "HOME": str(self.paths.data_dir),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        if extra_env:
            env.update(extra_env)
        argv = ["runuser", "-u", self.paths.app_user, "--", "env"]
        argv.extend(f"{key}={value}" for key, value in env.items())
        argv.extend(command)
        return self.runner.run(argv, cwd=cwd, check=check, capture=capture)

    def _prepare_toolchain(self) -> None:
        uid, gid = self._user_ids()
        self.build_work.mkdir(parents=True, exist_ok=True)
        self.build_work.chmod(0o700)
        os.chown(self.build_work, uid, gid)
        for path in (
            self.paths.toolchain_dir,
            self.paths.toolchain_dir / "rustup",
            self.paths.toolchain_dir / "cargo",
        ):
            path.mkdir(parents=True, exist_ok=True)
            os.chown(path, uid, gid)
        cargo = self.paths.toolchain_dir / "cargo" / "bin" / "cargo"
        if not cargo.is_file():
            rustup_arch = SOURCES[platform.machine()][0]
            expected = SOURCES[platform.machine()][2]
            rustup = self.build_work / "rustup-init"
            self.downloader(
                "https://static.rust-lang.org/rustup/dist/"
                f"{rustup_arch}/rustup-init",
                rustup,
            )
            self._verify_hash(rustup, expected, "rustup-init")
            rustup.chmod(0o700)
            os.chown(rustup, uid, gid)
            self._run_as_app(
                [
                    str(rustup),
                    "-y",
                    "--profile",
                    "minimal",
                    "--default-toolchain",
                    "1.92.0",
                    "--no-modify-path",
                ],
                cwd=self.build_work,
                extra_env={
                    "RUSTUP_HOME": str(self.paths.toolchain_dir / "rustup"),
                    "CARGO_HOME": str(self.paths.toolchain_dir / "cargo"),
                },
            )
        cargo_env = {
            "RUSTUP_HOME": str(self.paths.toolchain_dir / "rustup"),
            "CARGO_HOME": str(self.paths.toolchain_dir / "cargo"),
        }
        result = self._run_as_app(
            [str(cargo), "--version"],
            cwd=self.build_work,
            extra_env=cargo_env,
            capture=True,
        )
        version = result.stdout.strip()
        if not version.startswith("cargo 1.92.0 "):
            raise InstallError(
                f"toolchain validation failed: expected cargo 1.92.0, got {version!r}"
            )

    def _stage_release(self) -> Path:
        uid, gid = self._user_ids()
        self.build_work.mkdir(parents=True, exist_ok=True)
        os.chown(self.build_work, uid, gid)
        self.build_work.chmod(0o700)
        shutil.copytree(self.paths.source_root, self.staged_source, ignore=self._ignore)
        self._chown_tree(self.staged_source, uid, gid)
        target_dir = self.build_work / "target"
        cargo_env = {
            "RUSTUP_HOME": str(self.paths.toolchain_dir / "rustup"),
            "CARGO_HOME": str(self.build_work / "cargo-home"),
            "CARGO_TARGET_DIR": str(target_dir),
        }
        prebuilt = os.environ.get("RECO_FIELD_PREBUILT_RECO")
        if prebuilt:
            binary = Path(prebuilt).resolve(strict=True)
            result = self.runner.run([str(binary), "camera", "--help"], capture=True)
            if "--exposure-range" not in result.stdout or "--awb-mode" not in result.stdout:
                raise InstallError("prebuilt Reco is missing the required camera controls")
        else:
            binary = target_dir / "release" / "reco"
            self._run_as_app(
                [
                    str(self.paths.toolchain_dir / "cargo" / "bin" / "cargo"),
                    "build",
                    "--release",
                    "--locked",
                    "-j", "2",
                    "-p",
                    "reco-cli",
                    "--no-default-features",
                    "--features",
                    "gstreamer,opencv-seam",
                ],
                cwd=self.staged_source,
                extra_env=cargo_env,
            )
        self.staged_release.mkdir(parents=True, exist_ok=False)
        (self.staged_release / "bin").mkdir()
        self._install_file(
            binary,
            self.staged_release / "bin" / "reco",
            0o755,
        )
        self._install_mediamtx(self.staged_release)
        self._install_file(self.staged_source / "appliance/scripts/network_service.py",
                           self.staged_release / "network_service.py", 0o640)
        self._install_python_package(self.staged_release)
        self._stage_units(self.staged_release / "systemd")
        self._chown_tree(self.staged_release, 0, gid)
        self._chmod_tree(self.staged_release)
        release = self.paths.releases_dir / self.release_id
        os.replace(self.staged_release, release)
        return release

    @staticmethod
    def _ignore(directory: str, names: list[str]) -> set[str]:
        ignored = set()
        for name in names:
            if name in {
                ".git",
                ".venv",
                ".tools",
                "target",
                "dist",
                "__pycache__",
                ".pytest_cache",
            } or name.startswith((".venv", ".qa-", ".install-smoke")) or name.endswith(
                ".egg-info"
            ):
                ignored.add(name)
        return ignored

    @staticmethod
    def _chown_tree(path: Path, uid: int, gid: int) -> None:
        for current in (path, *path.rglob("*")):
            os.chown(current, uid, gid, follow_symlinks=False)

    @staticmethod
    def _chmod_tree(path: Path) -> None:
        for current in (path, *path.rglob("*")):
            if current.is_symlink():
                continue
            if current.is_dir():
                current.chmod(0o750)
            elif current.is_file():
                executable = bool(current.stat().st_mode & stat.S_IXUSR)
                current.chmod(0o750 if executable else 0o640)

    @staticmethod
    def _install_file(source: Path, target: Path, mode: int) -> None:
        shutil.copy2(source, target)
        target.chmod(mode)

    def _install_mediamtx(self, release: Path) -> None:
        _, media_arch, _, expected = SOURCES[platform.machine()]
        archive = self.build_work / "mediamtx.tar.gz"
        self.downloader(
            "https://github.com/bluenviron/mediamtx/releases/download/"
            f"v1.21.3/mediamtx_v1.21.3_linux_{media_arch}.tar.gz",
            archive,
        )
        self._verify_hash(archive, expected, "MediaMTX")
        extract = self.build_work / "mediamtx"
        extract.mkdir()
        with tempfile.TemporaryDirectory(
            prefix="mediamtx-verify-", dir=self.build_work
        ):
            self.runner.run(["tar", "-xzf", str(archive), "-C", str(extract)])
        self._install_file(extract / "mediamtx", release / "bin" / "mediamtx", 0o755)
        self._install_file(extract / "LICENSE", release / "Mediamtx-LICENSE", 0o640)

    def _install_python_package(self, release: Path) -> None:
        uid, gid = self._user_ids()
        venv = release / "venv"
        self._chown_tree(release, uid, gid)
        self._run_as_app(["/usr/bin/python3", "-m", "venv", str(venv)], cwd=release)
        python = venv / "bin" / "python"
        self._run_as_app(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "-r",
                str(self.staged_source / "appliance" / "requirements.txt"),
            ],
            cwd=release,
        )
        self._run_as_app(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--no-deps",
                str(self.staged_source / "appliance"),
            ],
            cwd=release,
        )
        self._run_as_app(
            [str(python), "-m", "pip", "check"],
            cwd=release,
        )
        self._smoke_test_python(release)

    def _smoke_test_python(self, release: Path) -> None:
        smoke_data = self.build_work / ".smoke-data"
        smoke_log = release / ".smoke.log"
        python = release / "venv" / "bin" / "python"
        self._run_as_app(
            [str(python), "-m", "reco_field", "init", "--data-dir", str(smoke_data)],
            cwd=Path("/"),
            capture=True,
        )
        port = self._free_port()
        env = {
            "RECO_FIELD_DATA_DIR": str(smoke_data),
            "RECO_FIELD_MANAGE_RELAY": "0",
            "RECO_FIELD_SIMULATOR": "1",
            "RECO_FIELD_RECO_BIN": str(release / "bin" / "reco"),
            "RECO_FIELD_MEDIAMTX_BIN": str(release / "bin" / "mediamtx"),
            "RECO_FIELD_MEDIAMTX_CONFIG": str(
                self.staged_source / "appliance" / "reco_field" / "mediamtx.yml"
            ),
            "RECO_FIELD_FFMPEG_BIN": "/usr/bin/ffmpeg",
            "RECO_FIELD_GST_INSPECT_BIN": "/usr/bin/gst-inspect-1.0",
            "RECO_FIELD_GST_LAUNCH_BIN": "/usr/bin/gst-launch-1.0",
        }
        argv = self._as_app_argv(
            [
                str(python),
                "-m",
                "reco_field",
                "serve",
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env,
        )
        with smoke_log.open("wb") as output:
            process = subprocess.Popen(
                argv,
                cwd="/",
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        try:
            deadline = time.monotonic() + self.health_timeout_seconds
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/session", timeout=1
                    ) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.2)
            else:
                raise InstallError("installed Python package smoke test timed out")
            if process.poll() is not None:
                raise InstallError("installed Python package failed to start")
        finally:
            if process.poll() is None:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    process.wait(timeout=5)
        smoke_log.unlink(missing_ok=True)
        shutil.rmtree(smoke_data, ignore_errors=True)

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _as_app_argv(
        self, command: Sequence[str], extra_env: dict[str, str]
    ) -> list[str]:
        env = {
            "HOME": str(self.paths.data_dir),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            **extra_env,
        }
        argv = ["runuser", "-u", self.paths.app_user, "--", "env"]
        argv.extend(f"{key}={value}" for key, value in env.items())
        argv.extend(command)
        return argv

    def _stage_units(self, target: Path) -> None:
        target.mkdir()
        replacements = {
            "/opt/reco-field": str(self.paths.app_root),
            "/var/lib/reco-field": str(self.paths.data_dir),
            "/etc/reco-field": str(self.paths.config_dir),
        }
        for name in UNITS:
            text = (
                self.staged_source / "appliance" / "packaging" / name
            ).read_text()
            for old, new in replacements.items():
                text = re.sub(re.escape(old) + r"(?=[/\s]|$)", lambda _: new, text)
            if name in {"reco-field.service", "reco-field-relay.service"} and os.environ.get("RECO_FIELD_STORAGE_MOUNT"):
                mount = os.environ["RECO_FIELD_STORAGE_MOUNT"]
                unit = self.runner.run(["systemd-escape", "--path", "--suffix=mount", mount], capture=True).stdout.strip()
                text = text.replace("[Service]", f"BindsTo={unit}\nAfter={unit}\nRequiresMountsFor={mount}\n\n[Service]")
            (target / name).write_text(text)

    def _verify_units(self, unit_dir: Path) -> None:
        # The active symlink must remain untouched until activation succeeds.
        with tempfile.TemporaryDirectory(prefix="reco-field-units-") as temporary:
            verify_dir = Path(temporary)
            units = []
            for name in UNITS:
                text = (unit_dir / name).read_text().replace(
                    str(self.paths.current_link), str(unit_dir.parent)
                )
                target = verify_dir / name
                target.write_text(text)
                units.append(str(target))
            self.runner.run(["systemd-analyze", "verify", *units])

    def _initialize_production(self, release: Path) -> None:
        source_config = (
            self.staged_source / "appliance" / "reco_field" / "mediamtx.yml"
        )
        target_config = self.paths.config_dir / "mediamtx.yml"
        if not target_config.exists():
            config = source_config.read_text().replace(":1935", ":1936").replace(":8888", ":8887").replace(":9997", ":9998")
            target_config.write_text(config)
            target_config.chmod(0o640)
            _, gid = self._user_ids()
            os.chown(target_config, 0, gid)
        environment = self.paths.config_dir / "reco-field.env"
        self._atomic_write(environment, (
            f"RECO_FIELD_STORAGE_MOUNT={os.environ['RECO_FIELD_STORAGE_MOUNT']}\n"
            f"RECO_FIELD_STORAGE_UUID={os.environ['RECO_FIELD_STORAGE_UUID']}\n"
        ).encode(), 0o640)
        _, gid = self._user_ids()
        os.chown(environment, 0, gid)
        python = release / "venv" / "bin" / "python"
        self._run_as_app(
            [
                str(python),
                "-m",
                "reco_field",
                "init",
                "--data-dir",
                str(self.paths.data_dir),
            ],
            cwd=Path("/"),
            capture=True,
        )

    def _activate_release(self, release: Path) -> None:
        conflicts = self._port_conflicts()
        if conflicts:
            raise InstallError("\n".join(conflicts))
        old_release = self._current_release()
        old_active = {unit: self._service_active(unit) for unit in UNITS}
        backups = self._backup_units()
        try:
            self.runner.run(
                ["systemctl", "stop", *reversed(UNITS)],
                check=False,
            )
            self._atomic_symlink(release)
            for unit in UNITS:
                self._atomic_copy(release / "systemd" / unit, self.paths.systemd_dir / unit)
            self.runner.run(["systemctl", "daemon-reload"])
            for unit in UNITS:
                self.runner.run(["systemctl", "start", unit])
                self.runner.run(["systemctl", "is-active", "--quiet", unit])
            deadline = time.monotonic() + self.health_timeout_seconds
            while time.monotonic() < deadline:
                if self.health_check():
                    break
                time.sleep(0.5)
            else:
                raise InstallError("Reco Field did not become healthy after activation")
            self.runner.run(
                ["systemctl", "enable", *UNITS]
            )
        except Exception as exc:
            self.runner.run(
                ["systemctl", "stop", *reversed(UNITS)],
                check=False,
            )
            if old_release:
                self._atomic_symlink(old_release)
            else:
                self.paths.current_link.unlink(missing_ok=True)
            self._restore_units(backups)
            self.runner.run(["systemctl", "daemon-reload"], check=False)
            for unit in UNITS:
                if old_active[unit]:
                    self.runner.run(["systemctl", "start", unit], check=False)
            raise InstallError(
                f"activation failed; previous release restored: {exc}"
            ) from exc

    def _current_release(self) -> Path | None:
        current = self.paths.current_link
        if not current.exists() and not current.is_symlink():
            return None
        if not current.is_symlink():
            raise InstallError(f"{current} exists but is not a release symlink")
        return Path(os.readlink(current))

    def _service_active(self, unit: str) -> bool:
        return (
            self.runner.run(
                ["systemctl", "is-active", "--quiet", unit],
                check=False,
            ).returncode
            == 0
        )

    def _backup_units(self) -> dict[str, bytes | None]:
        backups: dict[str, bytes | None] = {}
        for name in UNITS:
            path = self.paths.systemd_dir / name
            backups[name] = path.read_bytes() if path.exists() else None
        return backups

    def _restore_units(self, backups: dict[str, bytes | None]) -> None:
        for name, data in backups.items():
            path = self.paths.systemd_dir / name
            if data is None:
                path.unlink(missing_ok=True)
            else:
                self._atomic_write(path, data, 0o644)

    def _atomic_symlink(self, target: Path) -> None:
        temporary = self.paths.app_root / f".current.{uuid.uuid4().hex}"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(target)
        if os.name == "posix":
            os.replace(temporary, self.paths.current_link)
        else:
            self.paths.current_link.unlink(missing_ok=True)
            temporary.rename(self.paths.current_link)

    @staticmethod
    def _atomic_copy(source: Path, target: Path) -> None:
        Installer._atomic_write(target, source.read_bytes(), 0o644)

    @staticmethod
    def _atomic_write(target: Path, data: bytes, mode: int) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _download(url: str, destination: Path) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": "Reco-Field/0.1"})
        with urllib.request.urlopen(request, timeout=60) as response:
            with destination.open("wb") as output:
                shutil.copyfileobj(response, output)

    @staticmethod
    def _verify_hash(path: Path, expected: str, label: str) -> None:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise InstallError(f"{label} checksum mismatch")

    def _cleanup_build(self) -> None:
        if self.build_work.exists():
            shutil.rmtree(self.build_work)

    @staticmethod
    def _health_check() -> bool:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:8081/api/session", timeout=2
            ) as response:
                return response.status == 200
        except OSError:
            return False


def _default_paths(source_root: Path) -> InstallPaths:
    return InstallPaths(
        source_root=source_root,
        app_root=Path(os.environ.get("RECO_FIELD_APP_ROOT", "/opt/reco-field")),
        data_dir=Path(os.environ.get("RECO_FIELD_DATA_DIR", "/srv/reco-ssd/reco-field")),
        config_dir=Path(os.environ.get("RECO_FIELD_CONFIG_DIR", "/etc/reco-field")),
        build_dir=Path(
            os.environ.get("RECO_FIELD_BUILD_DIR", "/srv/reco-ssd/reco-field-installer-build")
        ),
        systemd_dir=Path(
            os.environ.get("RECO_FIELD_SYSTEMD_DIR", "/etc/systemd/system")
        ),
        app_user=os.environ.get("RECO_FIELD_USER", "recofield"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("precheck", "install"))
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("run with sudo")
    installer = Installer(_default_paths(args.source_root.resolve()))
    try:
        if args.command == "precheck":
            installer.precheck()
        else:
            release = installer.install()
            print(f"Reco Field release installed: {release}")
    except InstallError as exc:
        print(f"Reco Field install: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
