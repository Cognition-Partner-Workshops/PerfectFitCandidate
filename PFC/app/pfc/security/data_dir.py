"""REQ-AUTH-06 (S6): profile data directory, legacy move, owner-only ACL and BitLocker status."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from pfc.config import LEGACY_DATA_DIR, Settings

logger = logging.getLogger(__name__)

VolumeEncryption = Literal["Encrypted", "Not encrypted", "Unknown"]
Runner = Callable[..., subprocess.CompletedProcess[Any]]

DB = Path("db") / "pfc.db"
SQLITE_FILES = {"pfc.db", "pfc.db-wal", "pfc.db-shm", "pfc.db-journal"}
SYSTEM_SID = "*S-1-5-18"
# Shell property values: 1 = BitLocker on, 2 = off; anything else (encrypting, suspended...) is
# reported as Unknown rather than guessed.
BITLOCKER_VALUES: dict[str, VolumeEncryption] = {"1": "Encrypted", "2": "Not encrypted"}
BITLOCKER_SCRIPT = (
    "$v = (New-Object -ComObject Shell.Application).NameSpace($env:PFC_VOLUME).Self"
    ".ExtendedProperty('System.Volume.BitLockerProtection'); if ($null -ne $v) { [int]$v }"
)


class DataDirMigrationError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _file_hashes(root: Path) -> dict[Path, str]:
    """Every file except the live SQLite database, which is compared by row counts."""
    return {
        path.relative_to(root): _digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not (path.parent == root / DB.parent and path.name in SQLITE_FILES)
    }


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _row_counts(path: Path) -> dict[str, int]:
    with closing(_read_only(path)) as db:
        tables = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND sql NOT LIKE 'CREATE VIRTUAL TABLE%' ORDER BY name"
            )
        ]
        return {
            table: db.execute(
                f'SELECT count(*) FROM "{table.replace(chr(34), chr(34) * 2)}"'
            ).fetchone()[0]
            for table in tables
        }


def _free_name(path: Path) -> Path:
    candidate, n = path, 1
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.name}-{n}")
    return candidate


def _rename_with_retry(source: Path, destination: Path, attempts: int = 10) -> None:
    """Windows can hold just-closed files briefly (indexer, antivirus); retry before giving up."""
    for attempt in range(attempts):
        try:
            source.rename(destination)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))


def migrate_legacy_data_dir(legacy: Path, target: Path) -> bool:
    """Copy an old package-relative data dir to the profile dir, verify it, keep the original.

    The original is renamed to ``*.migrated`` and never deleted. Logs carry basenames only,
    because profile paths can contain the user's name or email address.
    """
    if target.exists() or not legacy.is_dir():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".migrating")
    if staging.exists():
        shutil.rmtree(staging)  # an earlier start's incomplete copy, never the original data

    def skip_live_db(directory: str, names: list[str]) -> set[str]:
        return set(names) & SQLITE_FILES if Path(directory) == legacy / DB.parent else set()

    try:
        shutil.copytree(legacy, staging, ignore=skip_live_db)
        if (legacy / DB).is_file():
            (staging / DB).parent.mkdir(parents=True, exist_ok=True)
            with closing(_read_only(legacy / DB)) as source:
                with closing(sqlite3.connect(staging / DB)) as copy:
                    source.backup(copy)
            if _row_counts(legacy / DB) != _row_counts(staging / DB):
                raise DataDirMigrationError("Copied database row counts do not match")
        if _file_hashes(legacy) != _file_hashes(staging):
            raise DataDirMigrationError("Copied files do not match the originals")
        staging.rename(target)
    except BaseException as exc:
        shutil.rmtree(staging, ignore_errors=True)
        logger.error("Data directory %s was not moved; the original is unchanged", legacy.name)
        if isinstance(exc, (OSError, sqlite3.Error)):
            raise DataDirMigrationError("Could not copy the data directory") from exc
        raise
    archive = _free_name(legacy.with_name(legacy.name + ".migrated"))
    try:
        _rename_with_retry(legacy, archive)
    except OSError:
        logger.warning(
            "Moved data directory %s to %s; could not rename the original", legacy.name, target.name
        )
        return True
    logger.info(
        "Moved data directory %s to %s; original kept as %s", legacy.name, target.name, archive.name
    )
    return True


def restrict_acl(path: Path, *, run: Runner = subprocess.run, platform: str = sys.platform) -> bool:
    """Windows: limit the data dir to the current user and SYSTEM. Needs no admin rights."""
    if platform != "win32":
        return True
    user, domain = os.environ.get("USERNAME"), os.environ.get("USERDOMAIN")
    if not user:
        logger.warning("Data directory permissions not restricted: current user unknown")
        return False
    account = f"{domain}\\{user}" if domain else user
    argv = [
        "icacls",
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"{account}:(OI)(CI)F",
        f"{SYSTEM_SID}:(OI)(CI)F",
    ]
    try:
        run(argv, check=True, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        logger.warning("Data directory permissions not restricted; startup continues")
        return False
    return True


def bitlocker_status(
    path: Path, *, run: Runner = subprocess.run, platform: str = sys.platform
) -> VolumeEncryption:
    """Informational only: read the Shell BitLocker property of the data dir's volume."""
    drive = path.resolve().anchor
    if platform != "win32" or not drive:
        return "Unknown"
    try:
        result = run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", BITLOCKER_SCRIPT],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "PFC_VOLUME": drive},
        )
    except (OSError, subprocess.SubprocessError):
        return "Unknown"
    return BITLOCKER_VALUES.get(str(result.stdout).strip(), "Unknown")


def prepare_data_dir(
    settings: Settings,
    *,
    legacy: Path = LEGACY_DATA_DIR,
    run: Runner = subprocess.run,
    platform: str = sys.platform,
) -> None:
    """Before first use: move a legacy default data dir, then restrict its ACL (best effort)."""
    if "data_dir" not in settings.model_fields_set:
        migrate_legacy_data_dir(legacy, settings.data_dir)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    restrict_acl(settings.data_dir, run=run, platform=platform)
