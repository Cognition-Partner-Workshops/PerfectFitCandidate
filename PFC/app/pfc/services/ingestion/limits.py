"""System-scoped ingestion limits with conservative defaults."""

import json
import math
import sqlite3
import sys
from dataclasses import dataclass


def positive_setting(db: sqlite3.Connection, key: str, default: float) -> float:
    row = db.execute(
        "SELECT value_json FROM settings WHERE scope='system' AND key=?", (key,)
    ).fetchone()
    try:
        value = json.loads(row[0]) if row else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        number = float(value)
        return (
            number
            if math.isfinite(number) and 0 < number <= sys.maxsize / (1024 * 1024)
            else default
        )
    except (ValueError, TypeError, OverflowError):
        return default


@dataclass(frozen=True)
class WorkbookLimits:
    max_rows: int = 20000
    max_sheets: int = 20
    max_uncompressed_bytes: int = 200 * 1024 * 1024

    @classmethod
    def read(cls, db: sqlite3.Connection) -> "WorkbookLimits":
        return cls(
            max_rows=max(1, int(positive_setting(db, "ingestion.max_rows", 20000))),
            max_sheets=max(1, int(positive_setting(db, "ingestion.max_sheets", 20))),
            max_uncompressed_bytes=max(
                1, int(positive_setting(db, "ingestion.max_uncompressed_mb", 200) * 1024 * 1024)
            ),
        )
