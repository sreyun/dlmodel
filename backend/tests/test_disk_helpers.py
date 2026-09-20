import errno
from pathlib import Path

import pytest

from app.paths import disk_error_to_message, free_space_bytes
from app.queue import DownloadQueue


def _oserror(code: int) -> OSError:
    exc = OSError()
    exc.errno = code
    return exc


def test_disk_error_maps_enospc():
    msg = disk_error_to_message(_oserror(errno.ENOSPC), Path("/models/hf/org/m"))
    assert msg is not None
    assert "磁盘空间不足" in msg
    assert "org" in msg


def test_disk_error_maps_permission():
    msg = disk_error_to_message(_oserror(errno.EACCES), Path("/models/hf/org/m"))
    assert msg is not None
    assert "无写入权限" in msg


def test_disk_error_unrelated_returns_none():
    assert disk_error_to_message(_oserror(errno.EINTR), Path("/x")) is None


def test_free_space_bytes_positive_for_existing(tmp_path):
    assert free_space_bytes(tmp_path) > 0


# A missing deep path still resolves to the nearest existing ancestor.
def test_free_space_bytes_missing_path(tmp_path):
    assert free_space_bytes(tmp_path / "no" / "such" / "dir") > 0


def test_check_dest_writable_raises_when_full(tmp_path, monkeypatch):
    import app.queue as queue_mod

    monkeypatch.setattr(queue_mod, "free_space_bytes", lambda _p: 0)
    q = DownloadQueue(concurrency=1)
    with pytest.raises(RuntimeError, match="磁盘空间不足"):
        q._check_dest_writable(tmp_path / "dest")


def test_check_dest_writable_ok_when_space_unknown(tmp_path, monkeypatch):
    import app.queue as queue_mod

    monkeypatch.setattr(queue_mod, "free_space_bytes", lambda _p: -1)
    q = DownloadQueue(concurrency=1)
    q._check_dest_writable(tmp_path / "dest")  # must not raise
    assert (tmp_path / "dest").is_dir()
