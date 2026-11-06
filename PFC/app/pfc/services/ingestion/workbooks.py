"""REQ-IN-01..03/10..14: bounded, cached-value-only DL and CT readers."""

import re
from collections.abc import Callable, Set
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from openpyxl import load_workbook  # type: ignore[import-untyped]
from openpyxl.utils.datetime import from_excel  # type: ignore[import-untyped]

from .models import ParsedRow, Preview, RowIssue, RowKey, file_sha256, row_hash

GRADES = frozenset("A3 A4 B1 B2 C1 C2 D1 D2 E1".split())
STATUSES = frozenset(
    " ".join(status.lower().split())
    for status in (
        "Selected",
        "Unknown",
        "CV with Client - Sold - Pending Allocation",
        "Available",
        "Overall upskilling (New bench)",
        "CV with client - Demand Proposed",
        "CV with client - Proactive Proposed",
        "Expected on Bench Soon",
        "Status - x",
        "Status - y",
    )
)
DL = {
    "Code Requisition": "code_requisition",
    "Position Start Date": "position_start_date",
    "Account Name": "account_name",
    "Code Local Grade": "code_local_grade",
    "Demand Request Name": "demand_request_name",
    "Region": "region",
    "BU": "bu",
    "Owner": "owner",
    "JD": "jd_text_norm",
}
CT = {
    "ShortName": "short_name",
    "Email Address": "email_address",
    "Grade": "grade",
    "Location": "location",
    "Bench Status": "bench_status",
    "Skills": "skills_raw",
    "Available Capacity": "available_capacity",
    "Bench Start Date": "bench_start_date",
    "Bench End Date": "bench_end_date",
    "Tracker": "tracker_text",
    "Group": "group_name",
    "Group Owner": "group_owner",
}


def normalise_jd(text: str) -> str:
    """REQ-IN-03: conservative local default; callers may inject domain normalisation."""
    text = re.sub(r"(?m)^\s*```[^\n]*$", "", text)
    text = re.sub(r"(?m)^\s*[?•]\s*", "- ", text)
    return text.strip()


def _label(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def parse_date(value: Any, epoch: datetime) -> date | None:
    """REQ-IN-12: Excel epoch-aware serials and UK day-first dates."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = from_excel(value, epoch)
            return parsed.date() if isinstance(parsed, datetime) else None
        except (ValueError, OverflowError):
            return None
    text = re.sub(r"^[A-Za-z]+[,]?\s+", "", str(value or "").strip())
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def parse_workbook(
    path: Path,
    kind: Literal["DL", "CT"],
    *,
    empty_row_limit: int = 50,
    grades: Set[str] = GRADES,
    known_statuses: Set[str] = STATUSES,
    jd_normaliser: Callable[[str], str] = normalise_jd,
) -> Preview:
    """REQ-ING-01/02: parse for review; no DB, formula evaluation or side effects."""
    if empty_row_limit < 1:
        raise ValueError("empty_row_limit must be positive")
    if path.suffix.lower() != ".xlsx":
        raise ValueError("Only unencrypted .xlsx workbooks are supported")
    fields = DL if kind == "DL" else CT
    aliases = {_label(label): field for label, field in fields.items()}
    aliases.update(
        {
            "skills (comma separated)": "skills_raw",
            "available capacity (y/n)": "available_capacity",
            "short name": "short_name",
        }
    )
    required = (
        {"code_requisition", "jd_text_norm"}
        if kind == "DL"
        else {"email_address", "grade", "bench_status"}
    )
    book = load_workbook(path, read_only=True, data_only=True)
    formula_book = None
    try:
        best: tuple[int, str, int, dict[int, str]] | None = None
        for sheet in book:
            for number, cells in enumerate(sheet.iter_rows(max_row=30, values_only=True), 1):
                columns = {
                    i: aliases[_label(value)]
                    for i, value in enumerate(cells)
                    if _label(value) in aliases and aliases[_label(value)] in fields.values()
                }
                if required <= set(columns.values()):
                    if kind == "CT" and cells and not _label(cells[0]):
                        columns[0] = "short_name"
                    if best is None or len(columns) > best[0]:
                        best = (len(columns), sheet.title, number, columns)
        if best is None:
            raise ValueError(f"No {kind} header row found in first 30 rows of any sheet")
        _, sheet_name, header_row, columns = best
        formula_book = load_workbook(path, read_only=True, data_only=False)
        rows: list[ParsedRow] = []
        previous: dict[RowKey, ParsedRow] = {}
        incomplete: dict[str, list[ParsedRow]] = {}
        empty = 0
        for number, (cells, formula_cells) in enumerate(
            zip(
                book[sheet_name].iter_rows(min_row=header_row + 1, values_only=True),
                formula_book[sheet_name].iter_rows(min_row=header_row + 1, values_only=True),
                strict=False,
            ),
            header_row + 1,
        ):
            raw = {field: cells[i] if i < len(cells) else None for i, field in columns.items()}
            uncached = {
                field: formula_cells[i]
                for i, field in columns.items()
                if (raw[field] is None or str(raw[field]).strip() == "")
                and i < len(formula_cells)
                and isinstance(formula_cells[i], str)
                and formula_cells[i].startswith("=")
            }
            key_columns = {"code_requisition"} if kind == "DL" else {"short_name", "email_address"}
            if not key_columns.intersection(uncached) and all(
                value is None or str(value).strip() == "" for value in cells
            ):
                empty += 1
                if empty >= empty_row_limit:
                    break
                continue
            empty = 0
            business: dict[str, Any] = {field: None for field in fields.values()}
            business.update(
                {
                    field: str(value).strip() if value is not None else None
                    for field, value in raw.items()
                }
            )
            issues = [
                RowIssue(
                    severity="warning",
                    code="FORMULA_NO_CACHED_VALUE",
                    row_number=number,
                    column=field,
                    raw_value=formula,
                    message="Formula has no cached value; never evaluated",
                )
                for field, formula in uncached.items()
            ]

            def issue(
                code: str,
                column: str,
                message: str,
                error: bool = False,
                issues: list[RowIssue] = issues,
                number: int = number,
                raw: dict[str, Any] = raw,
            ) -> None:
                issues.append(
                    RowIssue(
                        severity="error" if error else "warning",
                        code=code,
                        row_number=number,
                        column=column,
                        raw_value=raw.get(column),
                        message=message,
                    )
                )

            for field in ("position_start_date", "bench_start_date", "bench_end_date"):
                if field not in business:
                    continue
                value = raw.get(field)
                blank = value is None or str(value).strip() == ""
                parsed = None if blank else parse_date(value, book.epoch)
                business[field] = parsed.isoformat() if parsed else None
                if field == "bench_end_date":
                    business["bench_end_date_parse_status"] = (
                        "blank" if blank else "valid" if parsed else "unparseable"
                    )
                if not blank and parsed is None:
                    issue("DATE_UNPARSEABLE", field, "Unparseable date; pending review")
            key: RowKey | None
            if kind == "DL":
                key = business["code_requisition"] or None
                business["jd_text_norm"] = jd_normaliser(business["jd_text_norm"] or "")
                business["id_format_ok"] = bool(re.fullmatch(r"T[12]\d{4}", key or ""))
                if key and not business["id_format_ok"]:
                    issue("REQ_ID_FORMAT", "code_requisition", "Expected T1XXXX or T2XXXX digits")
                grade_field = "code_local_grade"
            else:
                business["email_address"] = raw.get("email_address")
                short = str(business["short_name"] or "").strip().lower()
                email = str(raw.get("email_address") or "").strip().lower()
                business.update(short_name_key=short, email_key=email)
                key = (short, email) if short else None
                if not email:
                    business["email_address"] = None
                    issue(
                        "MISSING_EMAIL", "email_address", "Incomplete identity; email not provided"
                    )
                if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                    issue("EMAIL_INVALID", "email_address", "Email copied verbatim; syntax invalid")
                if _label(business["bench_status"]) not in {_label(s) for s in known_statuses}:
                    issue("UNMAPPED_STATUS", "bench_status", "Unmapped status; not excluded")
                grade_field = "grade"
            if key is None:
                issue(
                    "MISSING_KEY",
                    "code_requisition" if kind == "DL" else "short_name",
                    "Required business key is missing",
                    True,
                )
            if business.get(grade_field) not in grades:
                issue("UNKNOWN_GRADE", grade_field, "Grade is not in the configured grade list")
            row = ParsedRow(
                row_number=number,
                key=key,
                fields=business,
                row_hash=row_hash(business),
                issues=issues,
                rejected=key is None,
            )
            if kind == "CT" and key is not None and not business["email_key"]:
                incomplete.setdefault(business["short_name_key"], []).append(row)
            elif key is not None:
                if key in previous:
                    earlier = previous[key]
                    earlier.superseded = True
                    earlier.issues.append(
                        RowIssue(
                            severity="warning",
                            code="DUPLICATE_IN_FILE",
                            row_number=earlier.row_number,
                            column="code_requisition" if kind == "DL" else "short_name",
                            raw_value=key,
                            message="Superseded by last occurrence in file",
                        )
                    )
                previous[key] = row
            rows.append(row)
        for identities in incomplete.values():
            if len(identities) > 1:
                for row in identities:
                    row.issues.append(
                        RowIssue(
                            severity="warning",
                            code="AMBIGUOUS_IDENTITY",
                            row_number=row.row_number,
                            column="short_name",
                            raw_value=row.fields["short_name"],
                            message="Repeated ShortName without email; human confirmation required",
                        )
                    )
        return Preview(
            kind=kind,
            file_name=path.name,
            file_sha256=file_sha256(path),
            file_size=path.stat().st_size,
            sheet_name=sheet_name,
            header_row=header_row,
            rows=rows,
        )
    finally:
        if formula_book is not None:
            formula_book.close()
        book.close()
