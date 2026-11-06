"""Transaction and canonical serialization helpers (REQ-MAT-08/09)."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from pfc.db.audit import append_audit


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def timestamp(now: datetime) -> str:
    return now.isoformat()


@contextmanager
def atomic(db: sqlite3.Connection) -> Iterator[None]:
    """Keep multi-write service operations atomic without committing caller work."""
    db.execute("SAVEPOINT matching_write")
    try:
        yield
        db.execute("RELEASE matching_write")
    except BaseException:
        db.execute("ROLLBACK TO matching_write")
        db.execute("RELEASE matching_write")
        raise


def audit(
    db: sqlite3.Connection,
    action: str,
    entity: str,
    entity_id: int,
    user_id: int | None,
    now: datetime,
    **details: Any,
) -> None:
    append_audit(
        db,
        ts=timestamp(now),
        action=action,
        user_id=user_id,
        entity_type=entity,
        entity_id=str(entity_id),
        details=details,
    )
