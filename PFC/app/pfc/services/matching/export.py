"""Audited, masked-by-default result downloads safe for spreadsheet consumers."""

import csv
import io
import json
import sqlite3
from datetime import date, datetime
from typing import Any, Literal

from openpyxl import Workbook  # type: ignore[import-untyped]

from pfc.db.audit import append_audit
from pfc.security.problems import Problem
from pfc.services.pii import mask_emails_in

from .common import atomic
from .data_changes import REASONS
from .results import get_run, list_results

HEADERS = (
    "Requisition",
    "Start Date",
    "Account",
    "Role",
    "Demand Grade",
    "Role Location",
    "Potential Person",
    "Person Email",
    "Person Grade",
    "Person Location",
    "Match %",
    "Match Band",
    "Location Assessment",
    "Matching Evidence",
    "Gaps / Checks",
    "Eligibility Evidence",
)
DISCLAIMER = "Scores are relative matching metrics for human review, not hiring decisions."


def safe_cell(value: str) -> str:
    for character in value:
        if character in "=+-@\t\r":
            return "'" + value
        if not character.isspace():
            break
    return value


def evidence_parts(value: str) -> list[str]:
    return [part.strip() for part in value.split("; ") if part.strip()]


def evidence(value: str) -> str:
    return "; ".join(part for part in value.split("; ") if part.strip())


def display_percentage(score: float) -> str:
    """REQ-OUT-01/10: CSV percentage display shared with the validation panel."""
    return f"{score:.1f}%"


def display_start_date(value: str | None) -> str:
    """REQ-OUT-02/10: consistent genuine dates in serialized output."""
    try:
        return date.fromisoformat(value).isoformat() if value else ""
    except ValueError as exc:
        raise Problem(422, "INVALID_RESULT_DATE", "Result Start Date is not ISO") from exc


def export_results(
    db: sqlite3.Connection,
    run_id: int | None,
    format: Literal["csv", "xlsx"],
    filters: dict[str, Any],
    *,
    user_id: int,
    now: datetime,
    include_emails: bool = False,
    sort: str = "rank",
    descending: bool = False,
    limit: int | None = None,
    offset: int = 0,
    run_pairs: dict[int, int] | None = None,
) -> tuple[bytes, str]:
    if run_pairs is None:
        if run_id is None:
            raise ValueError("run_id is required for a per-run export")
        run = get_run(db, run_id)
        if run is None:
            raise Problem(404, "RUN_NOT_FOUND", "Match run not found")
        run_ids = [run_id]
        metadata = (
            f"Run {run_id} · Finished {run['finished_at'] or ''} · "
            f"Model {run['llm_model'] or ''} · Template v{run['template_version']} · "
            f"dataChangedSinceRun: {json.dumps(run['data_changed_since_run'])} · "
            f"dataChangedReasons: {json.dumps(run['data_changed_reasons'])}"
        )
    else:
        if run_id is not None:
            raise ValueError("run_id and run_pairs are mutually exclusive")
        run_ids = sorted(set(run_pairs.values()), reverse=True)
        runs = [get_run(db, contributing_run_id) for contributing_run_id in run_ids]
        contributing = [run for run in runs if run is not None]
        newest = contributing[0] if contributing else None
        changed_reasons = [
            reason
            for reason in REASONS
            if any(reason in run["data_changed_reasons"] for run in contributing)
        ]
        finished_at = newest["finished_at"] or "" if newest else ""
        model = newest["llm_model"] or "" if newest else ""
        template_version = newest["template_version"] if newest else ""
        data_changed = any(run["data_changed_since_run"] for run in contributing)
        metadata = (
            f"Current results · Runs {', '.join(map(str, run_ids))} · "
            f"Finished {finished_at} · Model {model} · Template v{template_version} · "
            f"dataChangedSinceRun: {json.dumps(data_changed)} · "
            f"dataChangedReasons: {json.dumps(changed_reasons)}"
        )
    page = list_results(
        db,
        run_id,
        filters,
        sort=sort,
        descending=descending,
        limit=None,
        offset=0,
        run_pairs=run_pairs,
    )
    results = [row for row in page["items"] if not row["effective_excluded"]]
    results = results[offset : None if limit is None else offset + limit]
    rows: list[list[Any]] = [[metadata], [DISCLAIMER], list(HEADERS)]
    for row in results:
        email = row["person_email"]
        if include_emails:
            email = db.execute(
                "SELECT email_address FROM resources WHERE resource_id=?", (row["resource_id"],)
            ).fetchone()[0]
        start = display_start_date(row["start_date"])
        rows.append(
            [
                row["requisition"],
                start,
                row["account"] or "",
                row["role"] or "",
                row["demand_grade"] or "",
                row["role_location"],
                row["potential_person"],
                email or "",
                row["person_grade"] or "",
                row["person_location"] or "",
                display_percentage(row["score"]) if format == "csv" else row["score"] / 100,
                row["effective_band"],
                row["location_assessment"],
                mask_emails_in(evidence(row["matching_evidence"])),
                mask_emails_in(evidence(row["gaps_checks"])),
                mask_emails_in(evidence(row["eligibility_evidence"])),
            ]
        )
    rows = [
        [safe_cell(value) if isinstance(value, str) else value for value in row] for row in rows
    ]
    if format == "csv":
        stream = io.StringIO(newline="")
        csv.writer(stream).writerows(rows)
        content = stream.getvalue().encode("utf-8")
        media = "text/csv; charset=utf-8"
    else:
        book = Workbook()
        sheet = book.active
        assert sheet is not None
        sheet.title = "Match results"
        for index, values in enumerate(rows, 1):
            for column, value in enumerate(values, 1):
                cell = sheet.cell(index, column, value)
                if isinstance(value, str):
                    cell.data_type = "s"
                elif index > 3 and column == 11:
                    cell.number_format = "0.0%"
        output = io.BytesIO()
        book.save(output)
        content = output.getvalue()
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    audit_filters = {
        key: value[0] if isinstance(value, list) and len(value) == 1 else value
        for key, value in filters.items()
    }
    details: dict[str, Any] = {
        "run_id": run_id,
        "format": format,
        "rows": len(results),
        "filters": {
            **audit_filters,
            "sort": sort,
            "descending": descending,
            "limit": limit,
            "offset": offset,
        },
        "includeEmails": include_emails,
    }
    if run_pairs is not None:
        details["run_ids"] = run_ids
    with atomic(db):
        if include_emails:
            append_audit(
                db,
                ts=now.isoformat(),
                action="unmask",
                user_id=user_id,
                entity_type="match_run",
                entity_id=(
                    str(run_id) if run_id is not None else ",".join(map(str, run_ids)) or "current"
                ),
                details=details,
            )
        append_audit(
            db,
            ts=now.isoformat(),
            action="export_results",
            user_id=user_id,
            entity_type="match_run",
            entity_id=(
                str(run_id) if run_id is not None else ",".join(map(str, run_ids)) or "current"
            ),
            details=details,
        )
    return content, media
