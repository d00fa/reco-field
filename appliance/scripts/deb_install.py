#!/usr/bin/env python3
"""Offline Debian activation and per-unit SSD setup for Reco Field."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import http.cookiejar
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from deploy import CommandRunner, InstallError, InstallPaths, Installer, UNITS

PAYLOAD = Path("/usr/lib/reco-field/package")
MOUNT = Path("/srv/reco-ssd")
CONFIG = Path("/etc/reco-field")
FSTAB = Path("/etc/fstab")
MAINTENANCE_FILE = Path("/run/reco-field-upgrade")
IDLE_STATES = {"idle", "failed", "complete", "completed", "cancelled", "interrupted", "ready"}


def own_units(runner: CommandRunner, directory: Path = Path("/etc/systemd/system")) -> list[str]:
    owned = []
    executables = {"reco-field.service": "/opt/reco-field/current/venv/bin/python -m reco_field serve",
                   "reco-field-relay.service": "/opt/reco-field/current/bin/mediamtx ",
                   "reco-field-network.service": "/usr/bin/python3 /opt/reco-field/current/network_service.py"}
    for name in UNITS:
        path = directory / name
        loaded = runner.run(["systemctl", "show", "-p", "FragmentPath", "--value", name], check=False, capture=True).stdout.strip()
        if loaded and Path(loaded) != path:
            raise InstallError(f"{name} belongs to another installation ({loaded}); no services have been stopped.")
        if path.is_symlink() or path.exists() and (not path.is_file() or not any(
                line.startswith("ExecStart=" + executables[name]) for line in path.read_text().splitlines())):
            raise InstallError(f"{name} is not a recognized Reco Field unit; no services have been stopped.")
        if path.is_file():
            owned.append(name)
    return owned


def reject_symlink_ancestors(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink():
            raise InstallError(f"Storage paths must not contain symbolic links: {item}")


def safe_storage_paths(mount: Path, data: Path, build: Path) -> None:
    reject_symlink_ancestors(mount)
    volume = mount.stat().st_dev
    for target in (data, build, *(data / name for name in ("config", "recordings", "logs", "snapshots", "tmp"))):
        if not target.is_relative_to(mount):
            raise InstallError("Storage paths must remain inside the recording SSD.")
        reject_symlink_ancestors(target)
        for item in (target, *target.parents):
            if not item.is_relative_to(mount):
                break
            if item.exists() and (not item.is_dir() or item.stat().st_dev != volume):
                raise InstallError(f"Storage directory must remain on the recording SSD: {item}")


def read_environment(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith(("RECO_FIELD_STORAGE_MOUNT=", "RECO_FIELD_STORAGE_UUID=")):
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"')
    return values


def require_idle(runner: CommandRunner) -> None:
    if runner.run(["systemctl", "is-active", "--quiet", "reco-field.service"], check=False).returncode:
        return
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        with client.open("http://127.0.0.1:8081/api/session", timeout=3) as response:
            response.read(65536)
        with client.open("http://127.0.0.1:8081/api/status", timeout=5) as response:
            status = json.loads(response.read(1_048_577))
        for key in ("capture", "calibration", "snapshots", "lens", "offline_job"):
            if status[key]["state"] not in IDLE_STATES:
                raise InstallError("Stop capture, calibration and exports in Reco Field before installing or removing the package.")
        with client.open("http://127.0.0.1:8081/api/network", timeout=5) as response:
            network = json.loads(response.read(1_048_577))
        if not isinstance(network, dict):
            raise ValueError("Unexpected network status")
        if network.get("busy"):
            raise InstallError("Wait for the Wi-Fi change to finish before installing or removing Reco Field.")
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        raise InstallError("Cannot confirm Reco Field is idle; leave it running and retry after checking the web page.") from exc


@contextmanager
def maintenance():
    try:
        descriptor = os.open(MAINTENANCE_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise InstallError("Another upgrade is in progress. If its process has exited, reboot or remove /run/reco-field-upgrade before retrying.") from exc
    try:
        os.fchmod(descriptor, 0o644)
        os.write(descriptor, f"{os.getpid()}\n".encode())
        os.close(descriptor)
        descriptor = -1
        yield
    finally:
        if descriptor != -1:
            os.close(descriptor)
        MAINTENANCE_FILE.unlink(missing_ok=True)


def check_device(device: str, runner: CommandRunner) -> None:
    resolved = Path(device).resolve(strict=True)
    if not stat.S_ISBLK(resolved.stat().st_mode):
        raise InstallError("Recording storage must be a block device.")
    root = runner.run(["findmnt", "-nro", "SOURCE", "/"], capture=True).stdout.strip()

    def disks(path: str) -> set[str]:
        output = runner.run(["lsblk", "-snpr", "-o", "NAME,TYPE", path], capture=True).stdout
        return {parts[0] for line in output.splitlines() if len(parts := line.split()) == 2 and parts[1] == "disk"}

    storage_disks, root_disks = disks(str(resolved)), disks(root)
    if not storage_disks or not root_disks or storage_disks & root_disks or any("mmcblk" in p for p in storage_disks):
        raise InstallError("The system disk and SD/eMMC devices cannot be used for recordings. Select a separate SSD.")


def fstab_content(current: str, uuid: str) -> str:
    if not re.fullmatch(r"[a-fA-F0-9-]{8,64}", uuid):
        raise InstallError("Invalid recording SSD UUID.")
    found = False
    for line in current.splitlines():
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if len(fields) > 1 and fields[1] == MOUNT.as_posix():
            if fields[0] != f"UUID={uuid}" or len(fields) < 4 or fields[2] != "ext4" or "noauto" in fields[3].split(","):
                raise InstallError("/etc/fstab already has a different or non-automatic /srv/reco-ssd mount; resolve it before setup.")
            if found:
                raise InstallError("/etc/fstab contains duplicate recording SSD entries.")
            found = True
        elif fields[0] == f"UUID={uuid}":
            raise InstallError("This SSD UUID is already mounted elsewhere in /etc/fstab.")
    if found:
        return current
    return current.rstrip() + f"\n# Reco Field recording SSD\nUUID={uuid} {MOUNT.as_posix()} ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2\n"


class DebianInstaller(Installer):
    def __init__(self, payload: Path = PAYLOAD, *, paths: InstallPaths | None = None, **kwargs):
        self.payload = payload
        super().__init__(paths or InstallPaths(payload, Path("/opt/reco-field"), MOUNT / "reco-field", CONFIG,
                                             MOUNT / "reco-field-package-work", Path("/etc/systemd/system")), **kwargs)
        self.version = (payload / "VERSION").read_text().strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+", self.version):
            raise InstallError("Invalid package version.")
        self.release_id = f"deb-{self.version}-{self.release_id}"
        self.build_work = self.paths.build_dir / self.release_id
        self.staged_source = payload
        self.staged_release = self.paths.releases_dir / f".{self.release_id}"

    def _check_platform(self) -> None:
        if platform.system() != "Linux" or platform.machine() != "aarch64" or sys.version_info[:2] != (3, 10):
            raise InstallError("This package requires ARM64 JetPack 6 with Ubuntu 22.04 and Python 3.10.")
        release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
        if release.get("ID", "").strip('"') != "ubuntu" or release.get("VERSION_ID", "").strip('"') != "22.04":
            raise InstallError("This package supports Ubuntu 22.04 only.")
        l4t = Path("/etc/nv_tegra_release")
        if not l4t.exists() or not re.search(r"^# R36\b", l4t.read_text()):
            raise InstallError("JetPack 6 / L4T 36 camera and GPU drivers are required.")
        self.runner.run(["/usr/bin/python3", "-c", "import cv2; assert tuple(map(int,cv2.__version__.split('.')[:2])) >= (4,8); assert hasattr(cv2.aruco, 'CharucoBoard')"])
        linked = self.runner.run(["ldd", str(self.payload / "bin/reco")], capture=True).stdout
        if "not found" in linked or "libopencv_stitching.so.408" not in linked:
            raise InstallError("Native dependencies are missing; install NVIDIA OpenCV 4.8 and the JetPack 6 media runtime.")
        help_text = self.runner.run([str(self.payload / "bin/reco"), "camera", "--help"], capture=True).stdout
        if not all(flag in help_text for flag in ("--awb-mode", "--no-panorama-file", "--source-dir", "--projection")):
            raise InstallError("The bundled Reco executable lacks appliance features.")

    def _check_storage(self) -> None:
        reject_symlink_ancestors(MOUNT)
        environment = self.paths.config_dir / "reco-field.env"
        saved = read_environment(environment)
        if environment.exists():
            if saved.get("RECO_FIELD_STORAGE_MOUNT") != str(MOUNT) or not re.fullmatch(r"[a-fA-F0-9-]{8,64}", saved.get("RECO_FIELD_STORAGE_UUID", "")):
                raise InstallError("Existing storage configuration must contain /srv/reco-ssd and its valid SSD UUID; it has not been replaced.")
            os.environ.update(saved)
        super()._check_storage()
        device = self.runner.run(["findmnt", "-nro", "SOURCE", "--mountpoint", str(MOUNT)], capture=True).stdout.strip()
        check_device(device, self.runner)
        filesystem = self.runner.run(["findmnt", "-nro", "FSTYPE", "--mountpoint", str(MOUNT)], capture=True).stdout.strip()
        if filesystem != "ext4":
            raise InstallError("The recording SSD must use ext4.")
        safe_storage_paths(MOUNT, self.paths.data_dir, self.paths.build_dir)

    def precheck(self) -> None:
        own_units(self.runner, self.paths.systemd_dir)
        super().precheck()

    def _prepare_directories(self) -> None:
        uid, gid = self._user_ids()
        for path in (self.paths.app_root, self.paths.releases_dir, self.paths.toolchain_dir, self.paths.config_dir):
            reject_symlink_ancestors(path)
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o750 if path == self.paths.config_dir else 0o755)
            os.chown(path, 0, gid)
        # Open relative to directory descriptors so a symlink swap cannot redirect root's ownership changes.
        mount_fd = os.open(MOUNT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            def directory(parent: int, name: str, owner: int, mode: int) -> int:
                try:
                    os.mkdir(name, mode, dir_fd=parent)
                except FileExistsError:
                    pass
                descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                if os.fstat(descriptor).st_dev != os.fstat(mount_fd).st_dev:
                    os.close(descriptor)
                    raise InstallError("Recording directories must stay on the recording SSD.")
                os.fchown(descriptor, owner, gid)
                os.fchmod(descriptor, mode)
                return descriptor

            data_fd = directory(mount_fd, self.paths.data_dir.name, uid, 0o700)
            try:
                for name in ("config", "recordings", "logs", "snapshots", "tmp"):
                    os.close(directory(data_fd, name, uid, 0o700))
            finally:
                os.close(data_fd)
            os.close(directory(mount_fd, self.paths.build_dir.name, 0, 0o755))
        finally:
            os.close(mount_fd)

    def _space_errors(self) -> list[str]:
        return ["At least 1 GiB free is required on both the system and recording SSD volumes."] if any(
            self._free_gib(self._existing_parent(path)) < 1 for path in (self.paths.app_root, self.paths.build_dir)) else []

    def verify_payload(self) -> None:
        manifest = json.loads((self.payload / "manifest.json").read_text())
        if manifest.get("version") != self.version:
            raise InstallError("Package manifest version mismatch.")
        files = manifest.get("files", {})
        actual = {p.relative_to(self.payload).as_posix() for p in self.payload.rglob("*") if p.is_file() and p.name != "manifest.json"}
        if actual != files.keys():
            raise InstallError("Package payload inventory mismatch.")
        for name, expected in files.items():
            path = self.payload / name
            if path.is_symlink() or not path.resolve().is_relative_to(self.payload.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise InstallError(f"Package payload checksum mismatch: {name}")

    def _stage_release(self) -> Path:
        uid, gid = self._user_ids()
        self.build_work.mkdir(parents=True, mode=0o700)
        os.chown(self.build_work, uid, gid)
        self.staged_release.mkdir(mode=0o750)
        shutil.copytree(self.payload / "bin", self.staged_release / "bin")
        shutil.copy2(self.payload / "Mediamtx-LICENSE", self.staged_release / "Mediamtx-LICENSE")
        shutil.copy2(self.payload / "appliance/scripts/network_service.py", self.staged_release / "network_service.py")
        shutil.copy2(self.payload / "manifest.json", self.staged_release / "manifest.json")
        venv = self.staged_release / "venv"
        self.runner.run(["/usr/bin/python3", "-m", "venv", str(venv)])
        python = str(venv / "bin/python")
        self.runner.run([python, "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir", "--no-index",
                         "--find-links", str(self.payload / "wheels"), "-r", str(self.payload / "requirements-arm64.lock")])
        self.runner.run([python, "-m", "pip", "check"])
        site = self.runner.run([python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], capture=True).stdout.strip()
        if not Path(site).resolve().is_relative_to(venv.resolve()):
            raise InstallError("Unexpected Python installation path.")
        shutil.copytree(self.payload / "appliance/reco_field", Path(site) / "reco_field")
        self._stage_units(self.staged_release / "systemd")
        self._chown_tree(self.staged_release, 0, gid)
        self._chmod_tree(self.staged_release)
        self._smoke_test_python(self.staged_release)
        release = self.paths.releases_dir / self.release_id
        os.replace(self.staged_release, release)
        return release

    def _initialize_production(self, release: Path) -> None:
        _, gid = self._user_ids()
        environment = self.paths.config_dir / "reco-field.env"
        if not environment.exists():
            self._atomic_write(environment, (f"RECO_FIELD_STORAGE_MOUNT={MOUNT}\nRECO_FIELD_STORAGE_UUID={os.environ['RECO_FIELD_STORAGE_UUID']}\n").encode(), 0o640)
            os.chown(environment, 0, gid)
        target = self.paths.config_dir / "mediamtx.yml"
        if not target.exists():
            config = (self.payload / "appliance/reco_field/mediamtx.yml").read_text().replace(":1935", ":1936").replace(":8888", ":8887").replace(":9997", ":9998")
            self._atomic_write(target, config.encode(), 0o640)
            os.chown(target, 0, gid)
        settings = self.paths.data_dir / "config/settings.json"
        if not settings.exists():
            preset = {"camera": {"sensor_mode": 0, "capture_width": 4032, "capture_height": 3040,
                                 "capture_fps": 30, "projection": "panorama", "output_width": 3072, "output_height": 864},
                      "auto_record": False, "auto_stream": False}
            script = ("import os,sys; from pathlib import Path; from reco_field.storage import require_storage; "
                      "p=Path(sys.argv[1]); require_storage(p.parent.parent,Path(os.environ['RECO_FIELD_STORAGE_MOUNT']),os.environ['RECO_FIELD_STORAGE_UUID']); "
                      "f=p.open('x'); f.write(sys.argv[2]); f.close(); p.chmod(0o600)")
            self._run_as_app([str(release / "venv/bin/python"), "-c", script, str(settings), json.dumps(preset, indent=2) + "\n"],
                             cwd=Path("/"), extra_env=read_environment(environment))
        self._run_as_app([str(release / "venv/bin/python"), "-m", "reco_field", "init"], cwd=Path("/"),
                         extra_env={"RECO_FIELD_DATA_DIR": str(self.paths.data_dir), **read_environment(environment)})

    def install(self) -> Path:
        self.verify_payload()
        self.precheck()
        require_idle(self.runner)
        self._check_runtime_media()
        self._ensure_user()
        self._prepare_directories()
        release = self._stage_release()
        self._verify_units(release / "systemd")
        self._initialize_production(release)
        with maintenance():
            require_idle(self.runner)
            self._activate_release(release)
        self._cleanup_build()
        return release


def setup_storage(device: str | None, runner: CommandRunner) -> None:
    reject_symlink_ancestors(MOUNT)
    if device and not os.path.ismount(MOUNT):
        check_device(device, runner)
        filesystem = runner.run(["blkid", "-s", "TYPE", "-o", "value", device], capture=True).stdout.strip()
        if filesystem != "ext4":
            raise InstallError("The selected partition is not ext4. Format the intended SSD separately before setup; no disk has been erased.")
        mounted = runner.run(["findmnt", "-rn", "-S", device], capture=True, check=False)
        if mounted.stdout.strip():
            raise InstallError("The selected partition is already mounted elsewhere.")
        MOUNT.mkdir(parents=True, exist_ok=True)
        if any(MOUNT.iterdir()):
            raise InstallError("/srv/reco-ssd is not empty; refusing to hide existing files beneath a mount.")
        runner.run(["mount", "-t", "ext4", device, str(MOUNT)])
    elif device:
        mounted = runner.run(["findmnt", "-nro", "SOURCE", "--mountpoint", str(MOUNT)], capture=True).stdout.strip()
        if Path(device).resolve() != Path(mounted).resolve():
            raise InstallError("A different recording SSD is already mounted.")
    installer = DebianInstaller(runner=runner)
    installer._check_storage()
    before = FSTAB.read_text()
    after = fstab_content(before, os.environ["RECO_FIELD_STORAGE_UUID"])
    if after != before:
        backup = FSTAB.with_name("fstab.reco-field-before")
        if not backup.exists():
            Installer._atomic_write(backup, before.encode(), 0o600)
        Installer._atomic_write(FSTAB, after.encode(), 0o644)
        runner.run(["systemctl", "daemon-reload"])


def remove(runner: CommandRunner) -> None:
    installed = own_units(runner)
    with maintenance():
        require_idle(runner)
        if installed:
            runner.run(["systemctl", "disable", "--now", *reversed(installed)])
        for unit in installed:
            target = Path("/etc/systemd/system") / unit
            if target.is_file() and "/opt/reco-field/current/" in target.read_text():
                target.unlink()
        runner.run(["systemctl", "daemon-reload"])
    print("Reco Field stopped. Recordings, calibration, Wi-Fi settings and runtime releases have been retained.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "setup", "check-idle", "remove"))
    parser.add_argument("--device", help="Existing ext4 SSD partition to mount; never formats it")
    parser.add_argument("--keep-desktop", action="store_true", help="Keep the current boot target instead of selecting headless boot")
    parser.add_argument("--hostname", help="Optional unique host name for this unit")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("run with sudo")
    runner = CommandRunner()
    try:
        if args.action == "check-idle":
            require_idle(runner)
            return 0
        if args.action == "remove":
            remove(runner)
            return 0
        if args.hostname and not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", args.hostname):
            raise InstallError("Use a lowercase hostname of 1–63 letters, digits or hyphens.")
        if args.action == "configure" and not (CONFIG / "reco-field.env").exists():
            print("Reco Field installed. Mount the ext4 recording SSD, then run: sudo reco-field-setup [--device /dev/nvme0n1p1]")
            return 0
        if args.action == "setup":
            require_idle(runner)
            setup_storage(args.device, runner)
        release = DebianInstaller(runner=runner).install()
        if args.action == "setup":
            if args.hostname:
                runner.run(["hostnamectl", "set-hostname", args.hostname])
            if not args.keep_desktop:
                runner.run(["systemctl", "set-default", "multi-user.target"])
        print(f"Reco Field activated: {release.name}. Open http://{platform.node()}:8081 on your local network.")
        return 0
    except (InstallError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"Reco Field: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
