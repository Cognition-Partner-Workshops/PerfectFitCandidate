"""Audited, masked-by-default result downloads safe for spreadsheet consumers."""

import csv
import io
import sqlite3
from datetime import date, datetime
from typing import Any, Literal

from openpyxl import Workbook  # type: ignore[import-untyped]

from pfc.db.audit import append_audit
from pfc.security.problems import Problem

from .common import atomic
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


def evidence(value: str) -> str:
    return "; ".join(part for part in value.split("; ") if part.strip())


def export_results(
    db: sqlite3.Connection,
    run_id: int,
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
) -> tuple[bytes, str]:
    run = get_run(db, run_id)
    if run is None:
        raise Problem(404, "RUN_NOT_FOUND", "Match run not found")
    page = list_results(
        db, run_id, filters, sort=sort, descending=descending, limit=limit, offset=offset
    )
    results = [row for row in page["items"] if not row["effective_excluded"]]
    metadata = (
        f"Run {run_id} · Finished {run['finished_at'] or ''} · "
        f"Model {run['llm_model'] or ''} · Template v{run['template_version']}"
    )
    rows: list[list[Any]] = [[metadata], [DISCLAIMER], list(HEADERS)]
    for row in results:
        email = row["person_email"]
        if include_emails:
            email = db.execute(
                "SELECT email_address FROM resources WHERE resource_id=?", (row["resource_id"],)
            ).fetchone()[0]
        try:
            start = date.fromisoformat(row["start_date"]).isoformat() if row["start_date"] else ""
        except ValueError as exc:
            raise Problem(422, "INVALID_RESULT_DATE", "Result Start Date is not ISO") from exc
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
                f"{row['score']:.1f}%" if format == "csv" else row["score"] / 100,
                row["effective_band"],
                row["location_assessment"],
                evidence(row["matching_evidence"]),
                evidence(row["gaps_checks"]),
                evidence(row["eligibility_evidence"]),
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
    details = {
        "run_id": run_id,
        "format": format,
        "rows": len(results),
        "filters": {
            **filters,
            "sort": sort,
            "descending": descending,
            "limit": limit,
            "offset": offset,
        },
        "includeEmails": include_emails,
    }
    with atomic(db):
        if include_emails:
            append_audit(
                db,
                ts=now.isoformat(),
                action="unmask",
                user_id=user_id,
                entity_type="match_run",
                entity_id=str(run_id),
                details=details,
            )
        append_audit(
            db,
            ts=now.isoformat(),
            action="export_results",
            user_id=user_id,
            entity_type="match_run",
            entity_id=str(run_id),
            details=details,
        )
    return content, media
