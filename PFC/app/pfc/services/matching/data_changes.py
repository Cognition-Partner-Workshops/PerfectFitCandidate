"""Global post-run change metadata, computed without modifying stored runs."""

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

SOURCES = (
    (
        "DL/CT committed",
        "SELECT committed_at FROM ingestion_runs WHERE status='committed' "
        "AND kind IN ('DL','CT') AND julianday(committed_at)>=julianday(?)",
    ),
    (
        "OCC approval edit",
        "SELECT ts FROM audit_log WHERE action IN "
        "('OCC_EDIT','OCC_APPROVE','OCC_MODIFY','OCC_REVERT') "
        "AND julianday(ts)>=julianday(?)",
    ),
    (
        "JD confirmed",
        "SELECT confirmed_at FROM jd_requirements WHERE julianday(confirmed_at)>=julianday(?)",
    ),
    (
        "CV synced",
        "SELECT finished_at FROM jobs WHERE kind='cv_sync' AND status='completed' "
        "AND julianday(finished_at)>=julianday(?)",
    ),
)


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _event_boundary(value: str) -> datetime:
    """Whole-second stamps cover their entire second under the strict comparison."""
    at = _time(value)
    if re.search(r"[T ]\d{2}:\d{2}:\d{2}[.,]\d+", value):
        return at
    return at + timedelta(seconds=1)


def annotate_data_changes(db: sqlite3.Connection, runs: list[dict[str, Any]]) -> None:
    finished = []
    for run in runs:
        run.update(data_changed_since_run=False, data_changed_reasons=[])
        if run["finished_at"] and run["status"] in {"completed", "failed", "cancelled"}:
            finished.append((run, _time(run["finished_at"])))
    if not finished:
        return
    cutoff = min(at for _, at in finished).replace(microsecond=0).isoformat()
    latest = [
        (
            reason,
            max((_event_boundary(row[0]) for row in db.execute(query, (cutoff,))), default=None),
        )
        for reason, query in SOURCES
    ]
    for run, at in finished:
        reasons = [reason for reason, event in latest if event is not None and event > at]
        run.update(data_changed_since_run=bool(reasons), data_changed_reasons=reasons)
