"""Global post-run change metadata, computed without modifying stored runs."""

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

REASONS = (
    "DL/CT committed",
    "OCC approval decision",
    "OCC settings edit",
    "JD confirmed",
    "CV synced",
)

GLOBAL_REASONS = frozenset(REASONS) - {"JD confirmed"}

SOURCES = (
    "SELECT 'DL/CT committed',committed_at FROM ingestion_runs WHERE status='committed' "
    "AND kind IN ('DL','CT') AND julianday(committed_at)>=julianday(?)",
    "SELECT CASE WHEN action='OCC_EDIT' THEN 'OCC settings edit' "
    "ELSE 'OCC approval decision' END,ts FROM audit_log WHERE action IN "
    "('OCC_EDIT','OCC_APPROVE','OCC_MODIFY','OCC_REVERT') "
    "AND julianday(ts)>=julianday(?)",
    "SELECT 'JD confirmed',confirmed_at FROM jd_requirements "
    "WHERE julianday(confirmed_at)>=julianday(?)",
    "SELECT 'CV synced',finished_at FROM jobs WHERE kind='cv_sync' AND status='completed' "
    "AND julianday(finished_at)>=julianday(?)",
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
    latest: dict[str, datetime] = {}
    for query in SOURCES:
        for reason, value in db.execute(query, (cutoff,)):
            event = _event_boundary(value)
            previous = latest.get(reason)
            if previous is None or event > previous:
                latest[reason] = event
    for run, at in finished:
        reasons = [reason for reason in REASONS if reason in latest and latest[reason] > at]
        run.update(data_changed_since_run=bool(reasons), data_changed_reasons=reasons)


def stale_demand_ids_for_pairs(
    db: sqlite3.Connection,
    pairs: dict[int, int],
    runs: dict[int, dict[str, Any]],
) -> tuple[list[int], list[str]]:
    stale: set[int] = set()
    stale_reasons: set[str] = set()
    finished_by_demand: dict[int, datetime] = {}
    for demand_id, run_id in pairs.items():
        run = runs.get(run_id)
        if run is None:
            continue
        reasons = set(run.get("data_changed_reasons") or [])
        global_reasons = reasons & GLOBAL_REASONS
        if global_reasons:
            stale.add(demand_id)
            stale_reasons.update(global_reasons)
        if run.get("finished_at"):
            finished_by_demand[demand_id] = _time(str(run["finished_at"]))
    if not finished_by_demand:
        return sorted(stale), [reason for reason in REASONS if reason in stale_reasons]

    cutoff = min(finished_by_demand.values()).replace(microsecond=0).isoformat()
    events = db.execute(
        "SELECT v.demand_id AS demand_id,q.confirmed_at AS event_at "
        "FROM jd_requirements q JOIN demand_versions v USING(demand_version_id) "
        "WHERE q.confirmed_at IS NOT NULL AND julianday(q.confirmed_at)>=julianday(?)",
        (cutoff,),
    )
    for row in events:
        if row["demand_id"] is None or row["event_at"] is None:
            continue
        try:
            demand_id = int(row["demand_id"])
        except (TypeError, ValueError):
            continue
        finished_at = finished_by_demand.get(demand_id)
        if finished_at and _event_boundary(str(row["event_at"])) > finished_at:
            stale.add(demand_id)
            stale_reasons.add("JD confirmed")
    return sorted(stale), [reason for reason in REASONS if reason in stale_reasons]
