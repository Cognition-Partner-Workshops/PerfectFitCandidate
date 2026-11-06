"""REQ-AUTH-06 (S6): profile data dir, legacy move, ACL and BitLocker status."""

import logging
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pfc import config
from pfc.config import Settings, default_data_dir
from pfc.db.migrate import migrate
from pfc.launcher import acquire_lock
from pfc.main import create_app
from pfc.security import data_dir
from pfc.security.data_dir import (
    DataDirMigrationError,
    bitlocker_status,
    migrate_legacy_data_dir,
    prepare_data_dir,
    restrict_acl,
)


class Recorder:
    def __init__(self, stdout: str = "", error: BaseException | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.stdout, self.error = stdout, error

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, 0, self.stdout, "")


def legacy_dir(root: Path) -> Path:
    legacy = root / "package" / "data"
    migrate(Settings(data_dir=legacy))
    with closing(sqlite3.connect(legacy / "db" / "pfc.db")) as db, db:
        db.executemany(
            "INSERT INTO settings(scope,key,value_json,updated_at) VALUES ('system',?,'1','now')",
            [(f"ui.flag_{n}",) for n in range(5)],
        )
    (legacy / "cv").mkdir()
    (legacy / "cv" / "resume.txt").write_text("Synthetic CV", encoding="utf-8")
    return legacy


def counts(path: Path) -> int:
    with closing(sqlite3.connect(path)) as db:
        return int(db.execute("SELECT count(*) FROM settings").fetchone()[0])


def test_default_data_dir_uses_the_user_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert default_data_dir("win32") == tmp_path / "Local" / "PFC" / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert default_data_dir("linux") == tmp_path / "share" / "PFC" / "data"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("LOCALAPPDATA", "relative")
    assert default_data_dir("linux") == Path.home() / ".local" / "share" / "PFC" / "data"
    assert default_data_dir("win32") == Path.home() / "AppData" / "Local" / "PFC" / "data"
    assert not default_data_dir().is_relative_to(config.LEGACY_DATA_DIR.parent)
    monkeypatch.setenv("PFC_DATA_DIR", str(tmp_path / "override"))
    assert Settings().data_dir == tmp_path / "override"


def test_legacy_dir_is_copied_verified_and_kept(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    legacy = legacy_dir(tmp_path)
    before = counts(legacy / "db" / "pfc.db")
    target = tmp_path / "profile" / "PFC" / "data"
    assert migrate_legacy_data_dir(legacy, target) is True
    assert counts(target / "db" / "pfc.db") == before
    assert (target / "cv" / "resume.txt").read_text(encoding="utf-8") == "Synthetic CV"
    kept = legacy.with_name("data.migrated")
    assert not legacy.exists() and counts(kept / "db" / "pfc.db") == before
    assert str(tmp_path) not in caplog.text and "data.migrated" in caplog.text
    # Idempotent: the profile dir exists now, so a second start changes nothing.
    legacy.mkdir()
    assert migrate_legacy_data_dir(legacy, target) is False
    assert counts(target / "db" / "pfc.db") == before
    assert not legacy.with_name("data.migrated-2").exists()


def test_failed_verification_leaves_the_original_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = legacy_dir(tmp_path)
    results = iter([{"settings": 5}, {"settings": 4}])
    monkeypatch.setattr(data_dir, "_row_counts", lambda path: next(results))
    target = tmp_path / "profile" / "data"
    with pytest.raises(DataDirMigrationError):
        migrate_legacy_data_dir(legacy, target)
    assert legacy.is_dir() and not target.exists()
    assert not target.with_name("data.migrating").exists()
    assert not legacy.with_name("data.migrated").exists()


def test_empty_legacy_dir_is_not_migrated(tmp_path: Path) -> None:
    legacy = tmp_path / "package" / "data"
    (legacy / "logs").mkdir(parents=True)
    target = tmp_path / "profile" / "data"
    assert migrate_legacy_data_dir(legacy, target) is False
    assert legacy.is_dir() and not target.exists()


def test_running_old_instance_blocks_the_move(tmp_path: Path) -> None:
    legacy = legacy_dir(tmp_path)
    held = acquire_lock(legacy / "run" / "pfc.lock", 8765)
    try:
        assert held.acquired
        with pytest.raises(DataDirMigrationError):
            migrate_legacy_data_dir(legacy, tmp_path / "profile" / "data")
    finally:
        held.close()
    assert legacy.is_dir() and not (tmp_path / "profile" / "data").exists()
    assert migrate_legacy_data_dir(legacy, tmp_path / "profile" / "data") is True


def test_prepare_moves_only_the_default_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = legacy_dir(tmp_path)
    prepare_data_dir(Settings(data_dir=tmp_path / "explicit"), legacy=legacy, platform="linux")
    assert legacy.is_dir()
    monkeypatch.delenv("PFC_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "profile"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "profile"))
    default = Settings()
    assert "data_dir" not in default.model_fields_set
    assert default.data_dir == tmp_path / "profile" / "PFC" / "data"
    prepare_data_dir(default, legacy=legacy, platform="linux")
    assert not legacy.exists() and (default.data_dir / "db" / "pfc.db").exists()


def test_windows_acl_uses_icacls_argv_for_user_and_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USERNAME", "analyst")
    monkeypatch.setenv("USERDOMAIN", "CORP")
    run = Recorder()
    assert restrict_acl(tmp_path, run=run, platform="win32") is True
    (reset, _), (argv, kwargs) = run.calls
    assert reset == ["icacls", str(tmp_path), "/reset", "/T", "/C", "/Q"]
    assert argv == [
        "icacls",
        str(tmp_path),
        "/inheritance:r",
        "/grant:r",
        "CORP\\analyst:(OI)(CI)F",
        "*S-1-5-18:(OI)(CI)F",
    ]
    assert "shell" not in kwargs and kwargs["check"] is True
    assert restrict_acl(tmp_path, run=run, platform="linux") is True and len(run.calls) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_posix_data_dir_is_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "explicit"
    prepare_data_dir(Settings(data_dir=target))
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_failing_acl_step_warns_and_startup_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("USERNAME", "analyst")
    run = Recorder(error=subprocess.CalledProcessError(5, "icacls"))
    target = tmp_path / "explicit"
    prepare_data_dir(Settings(data_dir=target), run=run, platform="win32")
    assert target.is_dir() and len(run.calls) == 1
    assert "permissions not restricted" in caplog.text and str(tmp_path) not in caplog.text


@pytest.mark.parametrize(
    ("stdout", "error", "expected"),
    [
        ("1\r\n", None, "Encrypted"),
        ("2", None, "Not encrypted"),
        ("3", None, "Unknown"),
        ("", None, "Unknown"),
        ("", FileNotFoundError("powershell"), "Unknown"),
        ("", subprocess.TimeoutExpired("powershell", 15), "Unknown"),
    ],
)
def test_bitlocker_status_from_shell_property(
    tmp_path: Path, stdout: str, error: BaseException | None, expected: str
) -> None:
    run = Recorder(stdout, error)
    assert bitlocker_status(tmp_path, run=run, platform="win32") == expected
    ((argv, kwargs),) = run.calls
    assert argv[0] == "powershell.exe" and str(tmp_path) not in " ".join(argv)
    assert kwargs["env"]["PFC_VOLUME"] == tmp_path.resolve().anchor


def test_bitlocker_status_is_unknown_off_windows(tmp_path: Path) -> None:
    run = Recorder("1")
    assert bitlocker_status(tmp_path, run=run, platform="linux") == "Unknown" and not run.calls


def test_health_reports_volume_encryption(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, frontend_dir=tmp_path, allowed_hosts=["testserver"])
    app = create_app(settings)
    app.state.volume_probe = lambda path: "Encrypted" if path == tmp_path else "Unknown"
    with TestClient(app) as client:
        deadline = time.monotonic() + 5
        while True:
            details = client.get("/api/v1/health").json()["details"]
            if details["dataVolumeEncryption"] != "Unknown" or time.monotonic() > deadline:
                break
            time.sleep(0.05)
    assert details == {"dataVolumeEncryption": "Encrypted"}
