from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "deploy.py"
SPEC = importlib.util.spec_from_file_location("reco_field_deploy", MODULE_PATH)
assert SPEC and SPEC.loader
deploy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = deploy
SPEC.loader.exec_module(deploy)


class StubRunner:
    def __init__(self, ss_outputs=None, active=True):
        self.ss_outputs = ss_outputs or {}
        self.active = active
        self.calls: list[tuple[list[str], Path | None]] = []

    def run(
        self,
        argv,
        *,
        cwd=None,
        env=None,
        check=True,
        capture=False,
    ):
        command = [str(value) for value in argv]
        self.calls.append((command, cwd))
        stdout = ""
        if command[0] == "ss":
            port = int(command[-1].rsplit(":", 1)[1])
            stdout = self.ss_outputs.get(port, "")
        elif command[:2] == ["systemctl", "show"]:
            stdout = "0\n"
        elif command[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(command, 0 if self.active else 3, "", "")
        elif command[0] == "systemd-escape":
            stdout = "srv-reco\\x2dssd.mount\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")


class RecordingInstaller(deploy.Installer):
    def __init__(self, paths):
        super().__init__(paths)
        self.order: list[str] = []

    def precheck(self):
        self.order.append("precheck")

    def _ensure_user(self):
        self.order.append("user")

    def _check_runtime_media(self):
        self.order.append("media")

    def _prepare_directories(self):
        self.order.append("directories")

    def _prepare_toolchain(self):
        self.order.append("toolchain")

    def _stage_release(self):
        self.order.append("stage-build")
        raise deploy.InstallError("injected build failure")

    def _initialize_production(self, release):
        self.order.append("initialize")

    def _activate_release(self, release):
        self.order.append("activate")


class ActivationOrderInstaller(deploy.Installer):
    def __init__(self, paths, runner):
        super().__init__(paths, runner=runner, health_check=lambda: True)
        self.switch_events: list[int] = []

    def _atomic_symlink(self, target):
        self.switch_events.append(len(self.runner.calls))
        super()._atomic_symlink(target)

    def _atomic_copy(self, source, target):
        self.switch_events.append(len(self.runner.calls))
        super()._atomic_copy(source, target)


def make_paths(tmp_path: Path) -> deploy.InstallPaths:
    return deploy.InstallPaths(
        source_root=tmp_path / "source",
        app_root=tmp_path / "app",
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        build_dir=tmp_path / "build",
        systemd_dir=tmp_path / "systemd",
    )


def test_port_conflict_fails_without_stopping_unknown_service(tmp_path):
    runner = StubRunner(
        {
            8081: (
                'LISTEN 0 4096 0.0.0.0:8081 0.0.0.0:* '
                'users:(("python3",pid=1234,fd=7))'
            )
        }
    )
    usage = namedtuple("Usage", "total used free")
    installer = deploy.Installer(
        make_paths(tmp_path),
        runner=runner,
        disk_usage=lambda _: usage(100, 1, 100 * 1024**3),
    )
    installer._check_platform = lambda: None
    installer._check_storage = lambda: None
    with pytest.raises(deploy.InstallError, match="port 8081"):
        installer.precheck()
    assert not any("stop" in command for command, _ in runner.calls)
    assert any('pid=1234' in runner.ss_outputs[8081] for command, _ in runner.calls if command[0] == "ss")


def test_build_failure_never_reaches_initialization_or_activation(tmp_path):
    installer = RecordingInstaller(make_paths(tmp_path))
    with pytest.raises(deploy.InstallError, match="build failure"):
        installer.install()
    assert installer.order == [
        "precheck",
        "media",
        "user",
        "directories",
        "toolchain",
        "stage-build",
    ]


def test_activation_failure_restores_previous_release_and_units(tmp_path):
    paths = make_paths(tmp_path)
    old_release = paths.releases_dir / "old"
    new_release = paths.releases_dir / "new"
    old_release.mkdir(parents=True)
    (new_release / "systemd").mkdir(parents=True)
    paths.app_root.mkdir(parents=True, exist_ok=True)
    paths.current_link.symlink_to(old_release)
    paths.systemd_dir.mkdir(parents=True)
    for name in deploy.UNITS:
        (paths.systemd_dir / name).write_text(f"old-{name}\n")
        (new_release / "systemd" / name).write_text(f"new-{name}\n")

    runner = StubRunner(active=True)
    installer = deploy.Installer(
        paths,
        runner=runner,
        health_check=lambda: False,
        health_timeout_seconds=0,
    )
    installer._port_conflicts = lambda: []
    with pytest.raises(deploy.InstallError, match="previous release restored"):
        installer._activate_release(new_release)

    assert paths.current_link.resolve() == old_release.resolve()
    for name in deploy.UNITS:
        assert (paths.systemd_dir / name).read_text() == f"old-{name}\n"
    starts = [
        command
        for command, _ in runner.calls
        if command[:2] == ["systemctl", "start"]
    ]
    assert len(starts) == 6
    assert not any("recobox" in " ".join(command) for command, _ in runner.calls)


def test_successful_activation_stops_app_before_switching_release(tmp_path):
    paths = make_paths(tmp_path)
    old_release = paths.releases_dir / "old"
    new_release = paths.releases_dir / "new"
    old_release.mkdir(parents=True)
    (new_release / "systemd").mkdir(parents=True)
    paths.app_root.mkdir(parents=True, exist_ok=True)
    paths.current_link.symlink_to(old_release)
    paths.systemd_dir.mkdir(parents=True)
    for name in deploy.UNITS:
        (paths.systemd_dir / name).write_text(f"old-{name}\n")
        (new_release / "systemd" / name).write_text(f"new-{name}\n")

    runner = StubRunner(active=True)
    installer = ActivationOrderInstaller(paths, runner)
    installer._port_conflicts = lambda: []

    installer._activate_release(new_release)

    stop_index = next(
        index
        for index, (command, _) in enumerate(runner.calls)
        if command[:2] == ["systemctl", "stop"]
    )
    assert installer.switch_events
    assert all(event > stop_index for event in installer.switch_events)
    assert paths.current_link.resolve() == new_release.resolve()


def test_staged_units_use_release_layout(tmp_path, monkeypatch):
    paths = make_paths(tmp_path)
    monkeypatch.setenv("RECO_FIELD_STORAGE_MOUNT", "/srv/reco-ssd")
    installer = deploy.Installer(paths, runner=StubRunner())
    packaging = paths.source_root / "appliance" / "packaging"
    packaging.mkdir(parents=True)
    source = MODULE_PATH.parents[1] / "packaging"
    for name in deploy.UNITS:
        (packaging / name).write_text((source / name).read_text())
    installer.staged_source = paths.source_root
    target = tmp_path / "units"
    installer._stage_units(target)
    app = (target / "reco-field.service").read_text()
    relay = (target / "reco-field-relay.service").read_text()
    assert f"{paths.app_root}/current/venv/bin/python" in app
    assert f"{paths.app_root}/current/bin/mediamtx" in relay
    assert f"{paths.data_dir}" in app
    assert f"Environment=MTX_HLSDIRECTORY={paths.data_dir}/tmp/hls" in relay
    assert f"EnvironmentFile={paths.config_dir}/reco-field.env" in relay
    assert f"ExecStartPre={paths.app_root}/current/venv/bin/python -m reco_field init" in relay
    assert f"ReadWritePaths={paths.data_dir}" in relay
    for unit in (app, relay):
        assert "BindsTo=srv-reco\\x2dssd.mount" in unit
        assert "RequiresMountsFor=/srv/reco-ssd" in unit


def test_space_guard_checks_configured_build_volume(tmp_path):
    paths = make_paths(tmp_path)
    paths.build_dir.mkdir()
    paths.app_root.mkdir()
    usage = namedtuple("Usage", "total used free")
    seen = []

    def disk_usage(path):
        seen.append(path)
        free = 1 * 1024**3 if path == paths.build_dir else 20 * 1024**3
        return usage(100 * 1024**3, 0, free)

    installer = deploy.Installer(paths, disk_usage=disk_usage)
    errors = installer._space_errors()
    assert paths.build_dir in seen
    assert any("build volume" in error for error in errors)
    assert not any("release volume" in error for error in errors)


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("systemd-analyze") is None,
    reason="requires the real Linux systemd unit validator",
)
def test_first_install_validates_release_without_active_symlink(tmp_path, monkeypatch):
    dependency = tmp_path / "NetworkManager.service"
    dependency.write_text("[Service]\nExecStart=/bin/true\n")
    monkeypatch.setenv("SYSTEMD_UNIT_PATH", f"{tmp_path}:/usr/lib/systemd/system:/lib/systemd/system")
    paths = make_paths(tmp_path)
    installer = deploy.Installer(paths)
    installer.staged_source = MODULE_PATH.parents[2]
    release = paths.releases_dir / "new"
    release.mkdir(parents=True)
    installer._stage_units(release / "systemd")
    executables = (release / "venv/bin/python", release / "bin/mediamtx")
    for executable in executables:
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)

    installer._verify_units(release / "systemd")
    assert not paths.current_link.exists()
    original_unit = (release / "systemd/reco-field.service").read_text()
    assert str(paths.current_link) in original_unit

    executables[1].unlink()
    with pytest.raises(subprocess.CalledProcessError):
        installer._verify_units(release / "systemd")


def test_install_wrapper_prechecks_before_apt():
    script = (MODULE_PATH.parent / "install.sh").read_text()
    precheck = script.index('deploy.py" precheck')
    apt_update = script.index("apt-get update")
    install = script.index('deploy.py" install')
    assert precheck < apt_update < install


def test_packager_excludes_generated_build_tree():
    script = (MODULE_PATH.parent / "package.sh").read_text()
    assert "--exclude='appliance/build'" in script
