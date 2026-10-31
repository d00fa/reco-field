from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import stat
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import deb_install as deb
import build_deb


class Runner:
    def __init__(self, active=False):
        self.active = active
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        result = 0
        if argv[:2] == ["systemctl", "is-active"]:
            result = 0 if self.active else 3
        return subprocess.CompletedProcess(argv, result, "", "")


def fixture_installer(tmp_path, **kwargs):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "VERSION").write_text("0.2.0\n")
    paths = deb.InstallPaths(payload, tmp_path / "app", tmp_path / "data", tmp_path / "etc",
                             tmp_path / "work", tmp_path / "systemd")
    return deb.DebianInstaller(payload, paths=paths, **kwargs)


def test_fstab_idempotent_and_preserves_other_filesystems():
    before = "# original\nUUID=aaaa-bbbb / ext4 defaults 0 1\n"
    added = deb.fstab_content(before, "11111111-2222-3333-4444-555555555555")
    assert added.startswith(before)
    assert "nofail,x-systemd.device-timeout=10s" in added
    assert deb.fstab_content(added, "11111111-2222-3333-4444-555555555555") == added


@pytest.mark.parametrize("line", [
    "UUID=other-device /srv/reco-ssd ext4 defaults 0 2\n",
    "UUID=11111111-2222 /other ext4 defaults 0 2\n",
    "UUID=11111111-2222 /srv/reco-ssd ext4 noauto 0 2\n",
    "UUID=11111111-2222 /srv/reco-ssd vfat defaults 0 2\n",
])
def test_fstab_refuses_conflicting_mounts(line):
    with pytest.raises(deb.InstallError):
        deb.fstab_content(line, "11111111-2222")


def test_fstab_checks_entries_after_an_existing_valid_entry():
    first = "UUID=11111111-2222 /srv/reco-ssd ext4 defaults 0 2\n"
    with pytest.raises(deb.InstallError):
        deb.fstab_content(first + "UUID=11111111-2222 /other ext4 defaults 0 2\n", "11111111-2222")
    with pytest.raises(deb.InstallError, match="duplicate"):
        deb.fstab_content(first + first, "11111111-2222")


@pytest.mark.parametrize("storage_disk,root_disk,accepted", [
    ("/dev/nvme0n1", "/dev/mmcblk0", True),
    ("/dev/nvme0n1", "/dev/nvme0n1", False),
    ("/dev/mmcblk1", "/dev/mmcblk0", False),
])
def test_storage_rejects_root_disk_and_all_sd_devices(tmp_path, monkeypatch, storage_disk, root_disk, accepted):
    device = tmp_path / "block-device"
    device.touch()
    original_stat = Path.stat
    block_stat = type("BlockStat", (), {"st_mode": stat.S_IFBLK})()
    monkeypatch.setattr(Path, "stat", lambda self, *a, **kw: block_stat if str(self) == str(device) else original_stat(self, *a, **kw))

    class DiskRunner(Runner):
        def run(self, argv, **kwargs):
            self.calls.append(argv)
            if argv[0] == "findmnt":
                stdout = "/dev/root-partition\n"
            else:
                stdout = (root_disk if argv[-1] == "/dev/root-partition" else storage_disk) + " disk\n"
            return subprocess.CompletedProcess(argv, 0, stdout, "")

    runner = DiskRunner()
    if accepted:
        deb.check_device(str(device), runner)
    else:
        with pytest.raises(deb.InstallError, match="system disk"):
            deb.check_device(str(device), runner)
    assert not any(call[0] in {"mkfs", "mount", "rm", "wipefs"} for call in runner.calls)


def test_environment_reader_never_interprets_shell(tmp_path):
    path = tmp_path / "env"
    path.write_text('SECRET=keep-private\nRECO_FIELD_STORAGE_MOUNT=/srv/reco-ssd\nRECO_FIELD_STORAGE_UUID=1111-2222\n')
    assert deb.read_environment(path) == {"RECO_FIELD_STORAGE_MOUNT": "/srv/reco-ssd", "RECO_FIELD_STORAGE_UUID": "1111-2222"}


def test_payload_hash_rejects_changed_missing_and_added_files(tmp_path):
    installer = fixture_installer(tmp_path)
    version = installer.payload / "VERSION"
    manifest = installer.payload / "manifest.json"
    manifest.write_text(json.dumps({"version": "0.2.0", "files": {"VERSION": hashlib.sha256(version.read_bytes()).hexdigest()}}))
    installer.verify_payload()
    version.write_text("tampered")
    with pytest.raises(deb.InstallError, match="checksum"):
        installer.verify_payload()
    version.write_text("0.2.0\n")
    (installer.payload / "secret.key").write_text("unexpected")
    with pytest.raises(deb.InstallError, match="inventory"):
        installer.verify_payload()


def test_idle_check_never_stops_any_service(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_):
            return json.dumps({key: {"state": "running" if key == "offline_job" else "idle"}
                               for key in ("capture", "calibration", "snapshots", "lens", "offline_job")}).encode()

    class Client:
        def open(self, *_args, **_kwargs): return Response()

    runner = Runner(active=True)
    monkeypatch.setattr(deb.urllib.request, "build_opener", lambda *_: Client())
    with pytest.raises(deb.InstallError, match="Stop capture"):
        deb.require_idle(runner)
    assert runner.calls == [["systemctl", "is-active", "--quiet", "reco-field.service"]]


def test_inactive_install_does_not_contact_web_server(monkeypatch):
    monkeypatch.setattr(deb.urllib.request, "build_opener", lambda *_: pytest.fail("unexpected HTTP request"))
    deb.require_idle(Runner())


def test_wifi_change_prevents_activation(monkeypatch):
    class Response:
        def __init__(self, url): self.url = url
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_):
            value = {"busy": True} if self.url.endswith("/network") else {
                key: {"state": "idle"} for key in ("capture", "calibration", "snapshots", "lens", "offline_job")}
            return json.dumps(value).encode()

    class Client:
        def open(self, url, **_kwargs): return Response(url)

    runner = Runner(active=True)
    monkeypatch.setattr(deb.urllib.request, "build_opener", lambda *_: Client())
    with pytest.raises(deb.InstallError, match="Wi-Fi change"):
        deb.require_idle(runner)
    assert not any("stop" in call for call in runner.calls)


@pytest.mark.skipif(os.name != "posix", reason="Linux maintenance lock permissions")
def test_maintenance_exclusive_and_cleared_after_failure(tmp_path, monkeypatch):
    flag = tmp_path / "maintenance"
    monkeypatch.setattr(deb, "MAINTENANCE_FILE", flag)
    with pytest.raises(ValueError, match="activation failed"):
        with deb.maintenance():
            assert flag.exists() and stat.S_IMODE(flag.stat().st_mode) == 0o644
            with pytest.raises(deb.InstallError, match="Another upgrade"):
                with deb.maintenance():
                    pytest.fail("lock was acquired twice")
            assert flag.exists()
            raise ValueError("activation failed")
    assert not flag.exists()


@pytest.mark.parametrize("value", ["", "OTHER=preserved\n", "RECO_FIELD_STORAGE_MOUNT=/srv/reco-ssd\n"])
def test_incomplete_existing_storage_config_fails_closed(tmp_path, value):
    installer = fixture_installer(tmp_path)
    installer.paths.config_dir.mkdir()
    path = installer.paths.config_dir / "reco-field.env"
    path.write_text(value)
    with pytest.raises(deb.InstallError, match="Existing storage configuration"):
        installer._check_storage()
    assert path.read_text() == value


def test_existing_configuration_and_settings_preserved(tmp_path, monkeypatch):
    installer = fixture_installer(tmp_path)
    installer.paths.config_dir.mkdir()
    (installer.paths.data_dir / "config").mkdir(parents=True)
    files = {
        installer.paths.config_dir / "reco-field.env": b"RECO_FIELD_STORAGE_MOUNT=/srv/reco-ssd\nRECO_FIELD_STORAGE_UUID=abcd-1234\nOTHER=preserve\n",
        installer.paths.config_dir / "mediamtx.yml": b"custom config\n",
        installer.paths.data_dir / "config/settings.json": b'{"custom": true}',
        installer.paths.data_dir / "config/calibration.json": b"per-unit calibration",
    }
    for path, contents in files.items(): path.write_bytes(contents)
    monkeypatch.setattr(installer, "_user_ids", lambda: (1, 1))
    monkeypatch.setattr(installer, "_run_as_app", lambda *args, **kwargs: None)
    installer._initialize_production(tmp_path / "release")
    assert all(path.read_bytes() == contents for path, contents in files.items())


def test_fresh_settings_use_full_sensor_and_never_fake_calibration(tmp_path, monkeypatch):
    installer = fixture_installer(tmp_path)
    installer.paths.config_dir.mkdir()
    (installer.paths.data_dir / "config").mkdir(parents=True)
    (installer.payload / "appliance/reco_field").mkdir(parents=True)
    (installer.payload / "appliance/reco_field/mediamtx.yml").write_text("rtmpAddress: 127.0.0.1:1935\n")
    monkeypatch.setattr(installer, "_user_ids", lambda: (1, 1))
    def run_as_app(command, **kwargs):
        if command[1] == "-c":
            Path(command[3]).write_text(command[4])
    monkeypatch.setattr(installer, "_run_as_app", run_as_app)
    monkeypatch.setattr(deb.os, "chown", lambda *args, **kwargs: None, raising=False)
    monkeypatch.setenv("RECO_FIELD_STORAGE_UUID", "abcd-1234")
    installer._initialize_production(tmp_path / "release")
    settings = json.loads((installer.paths.data_dir / "config/settings.json").read_text())
    assert settings["camera"]["sensor_mode"] == 0
    assert settings["camera"]["capture_height"] == 3040
    assert settings["camera"]["projection"] == "panorama"
    assert settings["auto_record"] is False and settings["auto_stream"] is False
    assert not (installer.paths.data_dir / "config/calibration.json").exists()


def test_debian_activation_rolls_back_previous_live_release(tmp_path, monkeypatch):
    runner = Runner(active=True)
    installer = fixture_installer(tmp_path, runner=runner, health_check=lambda: False, health_timeout_seconds=0)
    old, new = installer.paths.releases_dir / "old", installer.paths.releases_dir / "new"
    old.mkdir(parents=True)
    (new / "systemd").mkdir(parents=True)
    installer.paths.current_link.symlink_to(old, target_is_directory=True)
    installer.paths.systemd_dir.mkdir()
    for name in deb.UNITS:
        (installer.paths.systemd_dir / name).write_text("old unit")
        (new / "systemd" / name).write_text("new unit")
    monkeypatch.setattr(installer, "_port_conflicts", lambda: [])
    with pytest.raises(deb.InstallError, match="previous release restored"):
        installer._activate_release(new)
    assert installer.paths.current_link.resolve() == old.resolve()
    assert all((installer.paths.systemd_dir / unit).read_text() == "old unit" for unit in deb.UNITS)
    assert not any("recobox" in " ".join(call) or "nvargus" in " ".join(call) for call in runner.calls)


def test_package_requires_arm64_and_pinned_wheels(tmp_path):
    binary = tmp_path / "reco"
    binary.write_bytes(b"\x7fELF\x02\x01" + b"\0" * 12 + b"\xb7\0")
    build_deb.check_arm64(binary)
    binary.write_bytes(b"\x7fELF\x02\x01" + b"\0" * 12 + b"\x3e\0")
    with pytest.raises(ValueError, match="ARM64"):
        build_deb.check_arm64(binary)
    lock = tmp_path / "lock"
    lock.write_text("sample==1.0\n")
    wheel = tmp_path / "sample-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive: archive.writestr("sample.py", "value = 1\n")
    assert build_deb.wheel_inventory(tmp_path, lock) == [wheel]
    lock.write_text("sample==2.0\n")
    with pytest.raises(ValueError, match="runtime lock"):
        build_deb.wheel_inventory(tmp_path, lock)


def test_maintainer_scripts_never_install_dependencies_or_erase_state():
    directory = SCRIPTS.parents[0] / "packaging/debian"
    for name in ("preinst", "postinst", "prerm", "postrm"):
        text = (directory / name).read_text()
        assert not any(command in text for command in ("apt-get", "mkfs", "rm -rf", "nvargus", "recobox"))
    assert "upgrade)" not in (directory / "prerm").read_text()


def test_storage_symlinks_rejected_before_mutation(tmp_path):
    mount = tmp_path / "ssd"
    data = mount / "reco-field"
    data.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("preserve")
    (data / "config").symlink_to(outside, target_is_directory=True)
    before = outside.stat().st_mode
    with pytest.raises(deb.InstallError, match="symbolic links"):
        deb.safe_storage_paths(mount, data, mount / "package-work")
    assert outside.stat().st_mode == before and sentinel.read_text() == "preserve"


def test_foreign_unit_rejected_without_stopping_it(tmp_path):
    unit = tmp_path / "reco-field.service"
    unit.write_text("[Service]\nExecStart=/opt/another-app/server\n")
    runner = Runner()
    with pytest.raises(deb.InstallError, match="not a recognized"):
        deb.own_units(runner, tmp_path)
    assert not any("stop" in call or "disable" in call for call in runner.calls)
    unit.unlink()
    target = tmp_path / "foreign.service"
    target.write_text("[Service]\nExecStart=/opt/another-app/server\n")
    unit.symlink_to(target)
    with pytest.raises(deb.InstallError, match="not a recognized"):
        deb.own_units(runner, tmp_path)
