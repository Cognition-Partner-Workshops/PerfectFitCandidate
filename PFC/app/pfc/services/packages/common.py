"""Shared package service storage and transaction boundaries (REQ-OCC-01..03)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from filelock import FileLock

from pfc.config import Settings
from pfc.db.connection import connect


class PackageServiceError(ValueError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def app_version() -> str:
    return version("pfc")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pfc-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def package_lock(settings: Settings) -> FileLock:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return FileLock(settings.data_dir / ".packages.lock", timeout=30)


@contextmanager
def transaction(settings: Settings) -> Iterator[sqlite3.Connection]:
    db = connect(settings)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("PRAGMA defer_foreign_keys=ON")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def active_package(db: sqlite3.Connection) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM settings_packages WHERE is_active=1").fetchone()


@contextmanager
def readonly_database(settings: Settings) -> Iterator[sqlite3.Connection]:
    path = settings.data_dir / "db" / "pfc.db"
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        yield db
    finally:
        db.close()


def source_bytes(source: Path | bytes) -> bytes:
    return source.read_bytes() if isinstance(source, Path) else source
