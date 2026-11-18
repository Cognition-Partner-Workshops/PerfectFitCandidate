import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pfc.adapters.sources.watch_folder import WatchFolderSource
from pfc.api.schemas.settings import WatchSource


def test_settling_changes_and_case_insensitive_pattern(tmp_path: Path) -> None:
    file = tmp_path / "dl_TEST.XLSX"
    file.write_bytes(b"one")
    clock = [0.0]
    source = WatchFolderSource(tmp_path, "DL_*.xlsx", clock=lambda: clock[0])
    assert source.scan() == []
    assert source.pending == {file.name: "settling"}
    clock[0] = 9
    assert source.scan() == []
    file.write_bytes(b"changed")
    clock[0] = 10
    assert source.scan() == []
    assert source.scan(19) == []
    assert source.scan(20) == [(file.name, b"changed")]
    assert source.pending == {}
    file.unlink()
    assert source.scan(21) == []
    assert source.observed == {}


def test_lock_files_nonregular_and_retry(tmp_path: Path) -> None:
    (tmp_path / "DL_folder.xlsx").mkdir()
    file = tmp_path / "DL_file.xlsx"
    file.write_bytes(b"data")
    lock = tmp_path / "~$DL_file.xlsx"
    lock.write_bytes(b"lock")
    source = WatchFolderSource(tmp_path, "*.xlsx")
    assert source.scan(0) == []
    assert source.pending == {file.name: "locked"}
    lock.unlink()
    assert source.scan(10) == []
    assert source.scan(20) == [(file.name, b"data")]


@pytest.mark.parametrize("attribute", [0x1000, 0x40000, 0x400000, 0x400])
def test_windows_attributes_never_read(tmp_path: Path, attribute: int) -> None:
    file = tmp_path / "DL.xlsx"
    file.touch()

    def fake_stat(path):
        return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=attribute)

    def no_read(path):
        pytest.fail("Must not download placeholders or follow reparse points")

    source = WatchFolderSource(tmp_path, "*.xlsx", stat_file=fake_stat, read_file=no_read)
    assert source.scan(0) == source.scan(20) == []
    assert source.pending == ({} if attribute == 0x400 else {file.name: "not synced"})


def test_symlink_stat_never_read(tmp_path: Path) -> None:
    file = tmp_path / "DL.xlsx"
    file.touch()
    source = WatchFolderSource(
        tmp_path,
        "*.xlsx",
        stat_file=lambda p: os.stat_result((stat.S_IFLNK, 0, 0, 0, 0, 0, 0, 0, 0, 0)),
    )
    assert source.scan(0) == source.scan(20) == []
    assert source.pending == {}


def test_unreadable_and_partial_read_retry(tmp_path: Path) -> None:
    file = tmp_path / "DL.xlsx"
    file.write_bytes(b"data")

    def unreadable(path):
        raise PermissionError("private path")

    source = WatchFolderSource(tmp_path, "*.xlsx", read_file=unreadable)
    assert source.scan(0) == source.scan(10) == []
    assert source.pending == {file.name: "unreadable"}

    def changing(path):
        payload = path.read_bytes()
        path.write_bytes(payload + b"more")
        return payload

    source.read_file = changing
    assert source.scan(20) == source.scan(30) == []
    assert source.pending == {file.name: "settling"}
    source.read_file = lambda p: p.read_bytes()
    assert source.scan(40) == [(file.name, b"datamore")]


@pytest.mark.parametrize("minutes", [0, -1, 1441])
def test_poll_minutes_validation(minutes: int) -> None:
    with pytest.raises(ValidationError):
        WatchSource(poll_minutes=minutes)
