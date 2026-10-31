#!/usr/bin/env python3
"""Assemble the JetPack 6 ARM64 release from tested binaries and offline wheels."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

from release_check import native_files

ROOT = Path(__file__).resolve().parents[2]
DEPENDENCIES = ", ".join((
    "python3 (>= 3.10)", "python3 (<< 3.11)", "python3-venv", "ca-certificates", "iproute2", "util-linux",
    "network-manager", "dnsmasq-base", "systemd", "ffmpeg", "libavcodec58", "libavformat58", "libavutil56", "libswscale5",
    "libstdc++6", "libvulkan1", "libgstreamer1.0-0", "libgstreamer-plugins-base1.0-0", "gstreamer1.0-tools",
    "gstreamer1.0-plugins-base", "gstreamer1.0-plugins-good", "gstreamer1.0-plugins-bad", "gstreamer1.0-plugins-ugly",
    "gstreamer1.0-libav", "libopencv (>= 4.8)", "libopencv (<< 4.9)", "libopencv-python (>= 4.8)", "libopencv-python (<< 4.9)",
    "nvidia-l4t-core (>= 36)", "nvidia-l4t-core (<< 37)", "nvidia-l4t-gstreamer", "nvidia-l4t-camera",
))


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        value = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def check_arm64(path: Path) -> None:
    with path.open("rb") as stream:
        header = stream.read(20)
    if len(header) != 20 or header[:6] != b"\x7fELF\x02\x01" or struct.unpack("<H", header[18:20])[0] != 183:
        raise ValueError(f"Expected an ARM64 little-endian ELF binary: {path.name}")


def wheel_inventory(directory: Path, lock: Path) -> list[Path]:
    expected = {re.sub(r"[-_.]+", "_", name.lower()): version for line in lock.read_text().splitlines()
                if line and not line.startswith("#") for name, version in [line.split("==", 1)]}
    found = {}
    for wheel in sorted(directory.glob("*.whl")):
        parts = wheel.name.split("-")
        if len(parts) < 5 or (not wheel.name.endswith("-any.whl") and "aarch64" not in parts[-1]):
            raise ValueError(f"Wheel is not ARM64 or universal: {wheel.name}")
        if not wheel.name.endswith("-any.whl") and not any(tag in parts[-3].split(".") for tag in ("cp310", "py3")):
            raise ValueError(f"Wheel does not target Python 3.10: {wheel.name}")
        name = re.sub(r"[-_.]+", "_", parts[0].lower())
        if name not in expected or parts[1] != expected[name] or name in found:
            raise ValueError(f"Wheel does not match the runtime lock: {wheel.name}")
        with zipfile.ZipFile(wheel) as archive:
            if archive.testzip():
                raise ValueError(f"Corrupt wheel: {wheel.name}")
        found[name] = wheel
    if found.keys() != expected.keys():
        raise ValueError(f"Missing pinned runtime wheels: {', '.join(sorted(expected.keys() - found.keys()))}")
    return list(found.values())


def copy(source: Path, destination: Path, executable: bool = False) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o755 if executable else 0o644)


def build(args: argparse.Namespace) -> Path:
    subprocess.run(["python3", str(ROOT / "appliance/scripts/release_check.py")], check=True)
    version = (ROOT / "appliance/VERSION").read_text().strip()
    for binary in (args.reco, args.mediamtx):
        check_arm64(binary)
    provenance = json.loads(args.native_source_manifest.read_text())
    if provenance.get("binary_sha256") != digest(args.reco) or provenance.get("files") != native_files(ROOT):
        raise ValueError("Native source or binary changed since the build manifest was recorded. Rebuild before packaging.")
    if platform.machine() != "aarch64":
        raise ValueError("Build on an ARM64 JetPack 6 host so native runtime features can be verified.")
    help_text = subprocess.check_output([str(args.reco.resolve()), "camera", "--help"], text=True)
    if not all(flag in help_text for flag in ("--no-panorama-file", "--source-dir", "--awb-mode", "--projection")):
        raise ValueError("Reco binary is missing required appliance features.")
    stitch_help = subprocess.check_output([str(args.reco.resolve()), "stitch", "--help"], text=True)
    if "--source-manifest" not in stitch_help:
        raise ValueError("Reco binary lacks offline source-session stitching.")
    media_version = subprocess.check_output([str(args.mediamtx.resolve()), "--version"], text=True).strip()
    if "1.21.3" not in media_version:
        raise ValueError("Expected MediaMTX 1.21.3.")
    lock = ROOT / "appliance/requirements-arm64.lock"
    wheels = wheel_inventory(args.wheelhouse, lock)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(["bash", str(ROOT / "appliance/scripts/package.sh")], cwd=ROOT, check=True)
    source = ROOT / "appliance/dist" / f"reco-field-{version}-source.tar.gz"
    if source.parent != output:
        copy(source, output / source.name)
    source_hash = digest(source)
    with tempfile.TemporaryDirectory(prefix="reco-field-deb-", dir=output) as temporary, tarfile.open(source) as archive:
        stage = Path(temporary)
        prefix = f"reco-field-{version}-source/"

        def source_copy(relative: str, target: Path, executable: bool = False) -> None:
            member = archive.getmember(prefix + relative)
            if not member.isfile():
                raise ValueError(f"Source entry must be a regular file: {relative}")
            stream = archive.extractfile(member)
            assert stream is not None
            target.parent.mkdir(parents=True, exist_ok=True)
            with stream, target.open("wb") as destination:
                shutil.copyfileobj(stream, destination)
            target.chmod(0o755 if executable else 0o644)

        for relative, expected in provenance["files"].items():
            stream = archive.extractfile(prefix + relative)
            if stream is None:
                raise ValueError(f"Missing native source in release archive: {relative}")
            with stream:
                content = stream.read()
            if not relative.endswith(".gz"):
                content = content.replace(b"\r\n", b"\n")
            if hashlib.sha256(content).hexdigest() != expected:
                raise ValueError(f"Native source changed while packaging: {relative}")

        payload = stage / "usr/lib/reco-field/package"
        payload.mkdir(parents=True)
        for name, binary in (("reco", args.reco), ("mediamtx", args.mediamtx)):
            copy(binary, payload / "bin" / name, True)
        if digest(payload / "bin/reco") != provenance["binary_sha256"]:
            raise ValueError("Native executable changed while packaging.")
        copy(args.mediamtx_license, payload / "Mediamtx-LICENSE")
        copy(args.native_source_manifest, payload / "native-source.json")
        source_copy("appliance/VERSION", payload / "VERSION")
        source_copy("appliance/requirements-arm64.lock", payload / lock.name)
        wheels = wheel_inventory(args.wheelhouse, payload / lock.name)
        for wheel in wheels:
            copy(wheel, payload / "wheels" / wheel.name)
        for member in archive.getmembers():
            relative = member.name.removeprefix(prefix)
            path = Path(relative)
            if (relative.startswith("appliance/reco_field/") and member.isfile()
                    or relative.startswith("appliance/packaging/") and path.suffix == ".service"):
                if path.suffix.lower() in {".key", ".pem", ".crt", ".mp4", ".mkv", ".pyc"} or "__pycache__" in path.parts:
                    raise ValueError(f"Unexpected file in application source: {path.name}")
                source_copy(relative, payload / relative)
        source_copy("appliance/scripts/network_service.py", payload / "appliance/scripts/network_service.py")
        for name in ("deploy.py", "deb_install.py"):
            source_copy(f"appliance/scripts/{name}", stage / "usr/lib/reco-field" / name)
        source_copy("appliance/scripts/reco-field-setup", stage / "usr/sbin/reco-field-setup", True)
        for name in ("preinst", "postinst", "prerm", "postrm"):
            source_copy(f"appliance/packaging/debian/{name}", stage / "DEBIAN" / name, True)
        for path in (ROOT / "LICENSE", ROOT / "appliance/README.md", ROOT / "appliance/THIRD_PARTY_NOTICES.md"):
            source_copy(path.relative_to(ROOT).as_posix(), stage / "usr/share/doc/reco-field" / path.name)
        for name in ("RECO-FIELD.md", "RECO-FIELD-CHANGELOG.md"):
            source_copy(f"docs/{name}", stage / "usr/share/doc/reco-field" / name)
        manifest = {"package": "reco-field", "version": version, "architecture": "arm64",
                    "target": "Ubuntu 22.04 / JetPack 6 / L4T 36 / OpenCV 4.8",
                    "source_archive": source.name, "source_sha256": source_hash,
                    "native_version": subprocess.check_output([str(args.reco.resolve()), "--version"], text=True).strip(),
                    "mediamtx_version": media_version,
                    "files": {p.relative_to(payload).as_posix(): digest(p) for p in sorted(payload.rglob("*")) if p.is_file()}}
        (payload / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        installed_size = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file()) // 1024 + 1
        control = (f"Package: reco-field\nVersion: {version}\nSection: video\nPriority: optional\nArchitecture: arm64\n"
                   f"Maintainer: Reco Field maintainers <noreply@github.com>\nInstalled-Size: {installed_size}\n"
                   f"Depends: {DEPENDENCIES}\nHomepage: https://github.com/d00fa/reco-field\n"
                   "Description: SSD-backed dual-camera Jetson panorama appliance\n"
                   " Local web recording, streaming, calibration and Wi-Fi control for\n"
                   " JetPack 6. New units require reco-field-setup and per-unit calibration.\n")
        (stage / "DEBIAN/control").write_text(control)
        for directory in (p for p in stage.rglob("*") if p.is_dir()):
            directory.chmod(0o755)
        (stage / "DEBIAN/md5sums").write_text("".join(
            f"{hashlib.md5(p.read_bytes()).hexdigest()}  {p.relative_to(stage).as_posix()}\n"
            for p in sorted((stage / "usr").rglob("*")) if p.is_file()))
        deb = output / f"reco-field_{version}_arm64.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "-z6", "--build", str(stage), str(deb)], check=True)
        (output / f"reco-field-{version}-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    release_files = [deb, output / source.name, output / f"reco-field-{version}-manifest.json"]
    (output / "SHA256SUMS").write_text("".join(f"{digest(p)}  {p.name}\n" for p in release_files))
    return deb


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("reco", "mediamtx", "mediamtx-license", "wheelhouse", "native-source-manifest"):
        parser.add_argument(f"--{option}", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "appliance/dist")
    args = parser.parse_args()
    try:
        print(build(args))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Package build failed: {exc}\n")


if __name__ == "__main__":
    main()
