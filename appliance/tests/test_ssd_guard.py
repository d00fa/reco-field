import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from reco_field.storage import require_storage

UUID = "12345678-1234-1234-1234-123456789abc"


def test_missing_ssd_refuses_without_creating_fallback(tmp_path, monkeypatch):
    mount = tmp_path / "ssd"
    data = mount / "reco-field"
    monkeypatch.setattr(os.path, "ismount", lambda _: False)
    with pytest.raises(OSError, match="not mounted"):
        require_storage(data, mount, UUID)
    assert not data.exists()


def test_recordings_cannot_escape_ssd(tmp_path):
    with pytest.raises(OSError, match="configured SSD"):
        require_storage(tmp_path / "sd", tmp_path / "ssd", UUID)


@pytest.mark.parametrize("matching", [True, False])
def test_ssd_device_identity_must_match(tmp_path, monkeypatch, matching):
    mount = tmp_path / "ssd"
    mount.mkdir()
    real_stat = os.stat
    device = mount.stat().st_dev
    monkeypatch.setattr(os.path, "ismount", lambda _: True)

    def fake_stat(path, *args, **kwargs):
        if str(path) == f"/dev/disk/by-uuid/{UUID}":
            return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=device if matching else device + 1)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fake_stat)
    if matching:
        require_storage(mount / "reco-field", mount, UUID)
    else:
        with pytest.raises(OSError, match="Wrong recording filesystem"):
            require_storage(mount / "reco-field", mount, UUID)


def test_recording_subdirectory_cannot_mount_a_different_device(tmp_path, monkeypatch):
    mount = tmp_path / "ssd"
    data = mount / "reco-field"
    recordings = data / "recordings"
    recordings.mkdir(parents=True)
    device = mount.stat().st_dev
    real_stat = os.stat
    monkeypatch.setattr(os.path, "ismount", lambda _: True)
    def fake_stat(path, *args, **kwargs):
        if str(path) == f"/dev/disk/by-uuid/{UUID}":
            return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=device)
        result = real_stat(path, *args, **kwargs)
        if str(path) == str(recordings):
            return SimpleNamespace(st_mode=result.st_mode, st_dev=device + 1)
        return result
    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(Path, "stat", lambda path, **kwargs: fake_stat(path, **kwargs))
    with pytest.raises(OSError, match="subdirectories"):
        require_storage(data, mount, UUID)
