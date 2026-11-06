"""P1-EX-03 / BRD §8 KPI-02/05: read-only, privacy-minimal baseline capture."""

import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from math import ceil
from statistics import median
from typing import Any

from pfc.config import Settings
from pfc.services.ingestion.models import file_sha256
from pfc.services.ingestion.repository import parse, upload_path
from pfc.services.ingestion.workbooks import valid_ct_email, valid_requisition

PROXY = "first Send to Email Approval"
INTERVALS = ("upload_to_confirm", "confirm_to_reviewed", "upload_to_reviewed")


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp.astimezone(UTC)
    except ValueError:
        raise ValueError("KPI timestamps must be valid and timezone-aware") from None


def _share(count: int, total: int) -> dict[str, Any]:
    return {"count": count, "percent": 100 * count / total if total else None}


def _quality(db: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """KPI-05 / REQ-ING-02/06: use logged denominators and original CT upload rows."""
    ct_runs = []
    errors = []
    latest: tuple[datetime, int] | None = None
    headline = None
    for run in db.execute(
        "SELECT ingestion_run_id,kind,rows_read,rows_rejected,committed_at,file_sha256 "
        "FROM ingestion_runs WHERE status='committed' AND kind IN ('CT','DL') "
        "ORDER BY ingestion_run_id"
    ).fetchall():
        run_id, total, rejected = run["ingestion_run_id"], run["rows_read"], run["rows_rejected"]
        if total is None or rejected is None or not 0 <= rejected <= total:
            raise ValueError(f"Run {run_id}: missing or inconsistent ingestion counts")
        errors.append(
            {
                "run_id": run_id,
                "kind": run["kind"],
                "total_rows": total,
                "rejected_rows": rejected,
                "percent": _share(rejected, total)["percent"],
            }
        )
        if run["kind"] != "CT":
            continue
        counts = dict.fromkeys(("grade", "skills", "valid_email", "all_three"), 0)
        if total:
            path = upload_path(settings, run_id)
            try:
                if file_sha256(path) != run["file_sha256"]:
                    raise ValueError
                preview = parse(db, path, "CT")
                if preview.rows_read != total:
                    raise ValueError
            except (OSError, ValueError):
                raise ValueError(
                    f"CT run {run_id}: original upload missing, changed or invalid"
                ) from None
            for row in preview.rows:
                fields = row.fields
                grade = bool(str(fields.get("grade") or "").strip())
                skills = bool(str(fields.get("skills_raw") or "").strip())
                email = valid_ct_email(fields["email_key"])
                counts["grade"] += grade
                counts["skills"] += skills
                counts["valid_email"] += email
                counts["all_three"] += grade and skills and email
        item = {
            "run_id": run_id,
            "total_rows": total,
            **{key: _share(count, total) for key, count in counts.items()},
        }
        ct_runs.append(item)
        order = (_time(run["committed_at"]) or datetime.min.replace(tzinfo=UTC), run_id)
        if latest is None or order > latest:
            latest, headline = order, item
    return {"headline_ct": headline, "ct_runs": ct_runs, "parse_errors": errors}


def _first(db: sqlite3.Connection, query: str) -> dict[int, datetime]:
    first: dict[int, datetime] = {}
    for row in db.execute(query):
        value = _time(row[1])
        if value is not None and (row[0] not in first or value < first[row[0]]):
            first[row[0]] = value
    return first


def _effort(db: sqlite3.Connection) -> dict[str, Any]:
    """KPI-02 / REQ-MAT-02, REQ-EML-01: first persisted milestones, current JD version."""
    confirmations = _first(
        db,
        "SELECT d.demand_id,q.confirmed_at FROM demands d JOIN jd_requirements q "
        "ON q.demand_version_id=d.current_version_id WHERE q.confirmed_at IS NOT NULL",
    )
    reviews = _first(db, "SELECT demand_id,created_at FROM email_messages")
    demands = []
    groups: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for row in db.execute(
        "SELECT d.demand_id,d.code_requisition,r.ingestion_run_id,r.committed_at "
        "FROM demands d LEFT JOIN ingestion_runs r ON r.ingestion_run_id=d.first_ingestion_id "
        "AND r.kind='DL' AND r.status='committed' ORDER BY d.code_requisition"
    ):
        upload, confirm, reviewed = (
            _time(row["committed_at"]),
            confirmations.get(row["demand_id"]),
            reviews.get(row["demand_id"]),
        )
        stamps = {"upload": upload, "confirm": confirm, "reviewed": reviewed}
        elapsed = {}
        for key, start, end in (
            ("upload_to_confirm", upload, confirm),
            ("confirm_to_reviewed", confirm, reviewed),
            ("upload_to_reviewed", upload, reviewed),
        ):
            elapsed[key] = (end - start).total_seconds() / 3600 if start and end else None
        item = {
            "requisition": (
                row["code_requisition"]
                if valid_requisition(row["code_requisition"])
                else f"demand:{row['demand_id']}"
            ),
            "cycle_run_id": row["ingestion_run_id"],
            **{f"{key}_at": value.isoformat() if value else None for key, value in stamps.items()},
            "missing_stages": [key for key, value in stamps.items() if value is None],
            "elapsed_hours": elapsed,
        }
        demands.append(item)
        groups[row["ingestion_run_id"]].append(item)
    cycles = []
    for run_id, rows in sorted(groups.items(), key=lambda pair: (pair[0] is None, pair[0] or 0)):
        summaries = {}
        for key in INTERVALS:
            values = sorted(
                row["elapsed_hours"][key] for row in rows if row["elapsed_hours"][key] is not None
            )
            summaries[key] = {
                "count": len(values),
                "median": median(values) if values else None,
                "p90": values[ceil(0.9 * len(values)) - 1] if values else None,
            }
        cycles.append(
            {
                "run_id": run_id,
                "demand_count": len(rows),
                "missing_stages": {
                    key: sum(key in row["missing_stages"] for row in rows)
                    for key in ("upload", "confirm", "reviewed")
                },
                "elapsed_hours": summaries,
            }
        )
    return {
        "reviewed_proxy": PROXY,
        "p90_method": "nearest-rank: ceil(0.9*n), 1-based",
        "demands": demands,
        "cycles": cycles,
    }


def measure(
    db: sqlite3.Connection, settings: Settings, *, now: datetime | None = None
) -> dict[str, Any]:
    """P1-EX-03: only SELECTs and bounded reads; never emit names, emails or source paths."""
    stamp = now if now is not None else datetime.now(UTC)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return {
        "generated_at": stamp.astimezone(UTC).isoformat(),
        "kpi05": _quality(db, settings),
        "kpi02": _effort(db),
    }


def _display(value: float | None, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:.2f}{suffix}"


def _cell(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def markdown(report: dict[str, Any]) -> str:
    """KPI-02/05: same figures as JSON; no individual CT data or draft content."""
    quality, effort = report["kpi05"], report["kpi02"]
    headline = quality["headline_ct"]
    lines = [
        "# KPI-02 / KPI-05 baselines (P1-EX-03)",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "## KPI-05: data quality",
        "",
        f"Latest committed CT run: {headline['run_id'] if headline else 'n/a'}",
        "",
        "| CT run | Total rows | Grade count (%) | Skills count (%) | "
        "Valid email count (%) | All three count (%) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in quality["ct_runs"]:
        cells = [
            f"{row[key]['count']} ({_display(row[key]['percent'], '%')})"
            for key in ("grade", "skills", "valid_email", "all_three")
        ]
        lines.append(f"| {row['run_id']} | {row['total_rows']} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "| Run | Kind | Total rows | Rejected rows | Parse error % |",
        "|---|---|---:|---:|---:|",
    ]
    for row in quality["parse_errors"]:
        lines.append(
            f"| {row['run_id']} | {row['kind']} | {row['total_rows']} | "
            f"{row['rejected_rows']} | {_display(row['percent'], '%')} |"
        )
    lines += [
        "",
        "## KPI-02: PFC timestamps only",
        "",
        f"Shortlist reviewed proxy: **{PROXY}**.",
        "The current Excel-process time-and-motion baseline is manual and out of scope.",
        f"p90 method: {effort['p90_method']}.",
        "",
        "| DL cycle run | Demands | Missing upload | Missing confirm | Missing reviewed |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in effort["cycles"]:
        missing = row["missing_stages"]
        lines.append(
            f"| {row['run_id'] if row['run_id'] is not None else 'unknown'} | "
            f"{row['demand_count']} | {missing['upload']} | {missing['confirm']} | "
            f"{missing['reviewed']} |"
        )
    lines += [
        "",
        "| DL cycle run | Interval (hours) | n | Median | p90 |",
        "|---|---|---:|---:|---:|",
    ]
    for row in effort["cycles"]:
        for key in INTERVALS:
            value = row["elapsed_hours"][key]
            lines.append(
                f"| {row['run_id'] if row['run_id'] is not None else 'unknown'} | "
                f"{key} | {value['count']} | {_display(value['median'])} | "
                f"{_display(value['p90'])} |"
            )
    lines += [
        "",
        "| Requisition | DL cycle run | Upload → confirm h | Confirm → proxy h | "
        "Upload → proxy h | Missing stages |",
        "|---|---|---:|---:|---:|---|",
    ]
    for row in effort["demands"]:
        values = " | ".join(_display(row["elapsed_hours"][key]) for key in INTERVALS)
        lines.append(
            f"| {_cell(row['requisition'])} | "
            f"{row['cycle_run_id'] if row['cycle_run_id'] is not None else 'unknown'} | "
            f"{values} | {', '.join(row['missing_stages']) or 'none'} |"
        )
    return "\n".join(lines) + "\n"
