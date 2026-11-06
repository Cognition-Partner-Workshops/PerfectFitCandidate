"""Focused REQ-IN-01..14 and REQ-ING-01..03/06 tests using synthetic workbooks."""

import hashlib
from datetime import date, datetime
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill
from openpyxl.utils.datetime import CALENDAR_WINDOWS_1900, to_excel

from pfc.services.ingestion.models import ParsedRow, diff_rows, file_sha256, is_no_op, row_hash
from pfc.services.ingestion.workbooks import CT, DL, parse_date, parse_workbook


def workbook(tmp_path: Path, kind: str, rows: list[list[object]]) -> Path:
    book = Workbook()
    book.active.title = "Not the data"
    sheet = book.create_sheet("Unpredictable sheet")
    sheet.append(["Bench Team – Front Sheet"])
    sheet.append([])
    headers = list(DL if kind == "DL" else CT)
    if kind == "CT":
        headers[0] = None
        headers[5] = " Skills (comma separated) "
        headers[6] = "Available Capacity (Y/N)"
    else:
        headers[0] = " code   REQUISITION "
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    path = tmp_path / f"{kind}.xlsx"
    book.save(path)
    book.close()
    return path


def dl_row(**changes: object) -> list[object]:
    fields: dict[str, object] = dict.fromkeys(DL, None)
    fields.update({"Code Requisition": "T11234", "Code Local Grade": "B1", "JD": "SQL"})
    fields.update(changes)
    return list(fields.values())


def ct_row(**changes: object) -> list[object]:
    fields: dict[str, object] = dict.fromkeys(CT, None)
    fields.update(
        {
            "ShortName": "Alex",
            "Email Address": "alex@example.com",
            "Grade": "B1",
            "Bench Status": "Available",
            "Skills": "SQL",
            "Available Capacity": "N",
        }
    )
    fields.update(changes)
    return list(fields.values())


def test_req_in_01_arbitrary_nonfirst_sheet(tmp_path: Path) -> None:
    preview = parse_workbook(workbook(tmp_path, "DL", [dl_row()]), "DL")
    assert preview.sheet_name == "Unpredictable sheet"
    assert preview.rows[0].key == "T11234"


def test_req_in_01_header_offset_and_case_whitespace(tmp_path: Path) -> None:
    preview = parse_workbook(workbook(tmp_path, "DL", [dl_row()]), "DL")
    assert preview.header_row == 3
    assert preview.rows[0].row_number == 4


def test_req_in_02_nonstandard_id_is_warning_only(tmp_path: Path) -> None:
    row = parse_workbook(
        workbook(tmp_path, "DL", [dl_row(**{"Code Requisition": "OtherID"})]), "DL"
    ).rows[0]
    assert row.key == "OtherID"
    assert not row.rejected
    assert not row.fields["id_format_ok"]
    assert [(i.code, i.severity) for i in row.issues] == [("REQ_ID_FORMAT", "warning")]


def test_req_in_02_missing_requisition_rejected(tmp_path: Path) -> None:
    row = parse_workbook(
        workbook(tmp_path, "DL", [dl_row(**{"Code Requisition": None})]), "DL"
    ).rows[0]
    assert row.rejected and row.key is None
    assert [(i.code, i.severity) for i in row.issues] == [("MISSING_KEY", "error")]


def test_req_in_03_jd_bullets_and_fences(tmp_path: Path) -> None:
    row = parse_workbook(
        workbook(tmp_path, "DL", [dl_row(JD=" ```markdown\n? SQL\n• Python\n``` ")]), "DL"
    ).rows[0]
    assert row.fields["jd_text_norm"] == "- SQL\n- Python"


def test_req_in_10_title_and_blank_shortname_header(tmp_path: Path) -> None:
    preview = parse_workbook(workbook(tmp_path, "CT", [ct_row()]), "CT")
    assert preview.header_row == 3
    assert preview.rows[0].fields["short_name"] == "Alex"


def test_req_in_10_aliases(tmp_path: Path) -> None:
    row = parse_workbook(workbook(tmp_path, "CT", [ct_row()]), "CT").rows[0]
    assert row.fields["skills_raw"] == "SQL"
    assert row.fields["available_capacity"] == "N"


@pytest.mark.parametrize(
    "value",
    [
        "Wed 4/11/2026",
        datetime(2026, 11, 4),
        date(2026, 11, 4),
        to_excel(datetime(2026, 11, 4)),
        "2026-11-04",
        "04-11-2026",
    ],
)
def test_req_in_12_workbook_date_formats(tmp_path: Path, value: object) -> None:
    row = parse_workbook(
        workbook(tmp_path, "CT", [ct_row(**{"Bench Start Date": value})]), "CT"
    ).rows[0]
    assert row.fields["bench_start_date"] == "2026-11-04"
    assert not row.issues


def test_req_in_12_unparseable_date_raw_issue(tmp_path: Path) -> None:
    row = parse_workbook(
        workbook(tmp_path, "CT", [ct_row(**{"Bench End Date": "nonsense"})]), "CT"
    ).rows[0]
    assert row.fields["bench_end_date"] is None
    assert row.fields["bench_end_date_parse_status"] == "unparseable"
    (issue,) = row.issues
    assert (issue.code, issue.severity, issue.raw_value, issue.column, issue.row_number) == (
        "DATE_UNPARSEABLE",
        "warning",
        "nonsense",
        "bench_end_date",
        4,
    )
    assert not row.rejected


def test_req_in_12_blank_end_date_no_issue(tmp_path: Path) -> None:
    row = parse_workbook(workbook(tmp_path, "CT", [ct_row()]), "CT").rows[0]
    assert row.fields["bench_end_date"] is None
    assert row.fields["bench_end_date_parse_status"] == "blank"
    assert not row.issues


def test_req_elig_07_unmapped_status_kept(tmp_path: Path) -> None:
    row = parse_workbook(
        workbook(tmp_path, "CT", [ct_row(**{"Bench Status": "Custom"})]), "CT"
    ).rows[0]
    assert not row.rejected
    assert row.fields["bench_status"] == "Custom"
    assert [(i.code, i.severity) for i in row.issues] == [("UNMAPPED_STATUS", "warning")]


@pytest.mark.parametrize("kind,field", [("CT", "Grade"), ("DL", "Code Local Grade")])
def test_req_ing_02_unknown_grade_warning(tmp_path: Path, kind: str, field: str) -> None:
    values = (ct_row if kind == "CT" else dl_row)(**{field: "Z9"})
    row = parse_workbook(workbook(tmp_path, kind, [values]), kind).rows[0]
    assert not row.rejected
    assert [(i.code, i.severity) for i in row.issues] == [("UNKNOWN_GRADE", "warning")]


def test_req_in_13_formula_cached_none(tmp_path: Path) -> None:
    row = parse_workbook(
        workbook(
            tmp_path,
            "CT",
            [
                ct_row(
                    **{
                        "Tracker": '=HYPERLINK("https://example.com", "Link")',
                        "Group Owner": "=1+1",
                    }
                )
            ],
        ),
        "CT",
    ).rows[0]
    assert row.fields["tracker_text"] is None
    assert row.fields["group_owner"] is None
    assert not row.rejected
    assert {i.column for i in row.issues if i.code == "FORMULA_NO_CACHED_VALUE"} == {
        "tracker_text",
        "group_owner",
    }


@pytest.mark.parametrize(
    "kind,field,column", [("DL", "Owner", "owner"), ("CT", "Tracker", "tracker_text")]
)
def test_req_in_13_uncached_formula_warning_bounded(
    tmp_path: Path, kind: str, field: str, column: str
) -> None:
    make_row = dl_row if kind == "DL" else ct_row
    formula = '=HYPERLINK("https://example.com", "Link")'
    path = workbook(
        tmp_path, kind, [make_row(**{field: formula}), [], [], [], make_row(**{field: formula})]
    )
    preview = parse_workbook(path, kind, empty_row_limit=2)
    assert preview.rows_read == 1
    (issue,) = preview.issues
    assert (issue.code, issue.severity, issue.column, issue.raw_value, issue.row_number) == (
        "FORMULA_NO_CACHED_VALUE",
        "warning",
        column,
        formula,
        4,
    )
    assert preview.rows[0].fields[column] is None
    assert not preview.rows[0].rejected


def test_req_in_13_formula_only_row_not_silently_skipped(tmp_path: Path) -> None:
    path = workbook(tmp_path, "DL", [["=1+1"]])
    preview = parse_workbook(path, "DL")
    assert preview.rows_read == 1
    assert preview.rows[0].rejected
    assert any(
        i.code == "FORMULA_NO_CACHED_VALUE" and i.raw_value == "=1+1" for i in preview.issues
    )


def test_req_in_14_verbatim_email_normalised_key(tmp_path: Path) -> None:
    row = parse_workbook(
        workbook(
            tmp_path,
            "CT",
            [ct_row(**{"ShortName": " Alex ", "Email Address": " AlEx@Example.com "})],
        ),
        "CT",
    ).rows[0]
    assert row.fields["email_address"] == " AlEx@Example.com "
    assert row.key == ("alex", "alex@example.com")
    assert not row.issues


def test_req_in_14_invalid_email_warning_only(tmp_path: Path) -> None:
    row = parse_workbook(
        workbook(tmp_path, "CT", [ct_row(**{"Email Address": "invalid"})]), "CT"
    ).rows[0]
    assert row.fields["email_address"] == "invalid"
    assert not row.rejected
    assert [(i.code, i.severity) for i in row.issues] == [("EMAIL_INVALID", "warning")]


@pytest.mark.parametrize("email", [None, "", "   "])
def test_req_in_23_missing_email_keeps_incomplete_identity(tmp_path: Path, email: object) -> None:
    row = parse_workbook(workbook(tmp_path, "CT", [ct_row(**{"Email Address": email})]), "CT").rows[
        0
    ]
    assert row.key == ("alex", "")
    assert row.fields["email_address"] is None
    assert not row.rejected
    assert [(i.code, i.severity) for i in row.issues] == [("MISSING_EMAIL", "warning")]


def test_req_in_11_missing_shortname_rejects(tmp_path: Path) -> None:
    row = parse_workbook(workbook(tmp_path, "CT", [ct_row(**{"ShortName": " "})]), "CT").rows[0]
    assert row.key is None
    assert row.rejected
    assert [(i.code, i.severity) for i in row.issues] == [("MISSING_KEY", "error")]


@pytest.mark.parametrize("gap,expected", [(2, 2), (4, 1)])
def test_req_ing_02_bounded_formatted_rows(tmp_path: Path, gap: int, expected: int) -> None:
    path = workbook(tmp_path, "DL", [dl_row()])
    book = load_workbook(path)
    sheet = book["Unpredictable sheet"]
    for number in range(5, 2001):
        sheet.cell(number, 1).fill = PatternFill("solid", fgColor="FFFFFF")
    for column, value in enumerate(dl_row(**{"Code Requisition": "T21234"}), 1):
        sheet.cell(5 + gap, column, value)
    book.save(path)
    book.close()
    assert parse_workbook(path, "DL", empty_row_limit=3).rows_read == expected


@pytest.mark.parametrize("limit", [0, -1])
def test_req_ing_02_empty_limit_validation(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        parse_workbook(tmp_path / "any.xlsx", "DL", empty_row_limit=limit)


def test_req_ing_01_reject_non_xlsx(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=".xlsx"):
        parse_workbook(tmp_path / "macro.xlsm", "DL")


def test_req_in_01_missing_header(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No CT header"):
        parse_workbook(workbook(tmp_path, "DL", []), "CT")


def test_req_ing_02_empty_preview_serialises(tmp_path: Path) -> None:
    preview = parse_workbook(workbook(tmp_path, "DL", []), "DL")
    dumped = preview.model_dump()
    assert dumped["rows_read"] == dumped["rows_rejected"] == dumped["rows_superseded"] == 0
    assert dumped["warnings"] == dumped["errors"] == 0
    assert dumped["issues"] == []


@pytest.mark.parametrize("kind", ["DL", "CT"])
def test_req_in_02_11_duplicate_last_wins_superseded(tmp_path: Path, kind: str) -> None:
    make_row = dl_row if kind == "DL" else ct_row
    preview = parse_workbook(workbook(tmp_path, kind, [make_row(), make_row(), make_row()]), kind)
    assert [r.superseded for r in preview.rows] == [True, True, False]
    assert not any(r.rejected for r in preview.rows)
    assert [i.code for i in preview.rows[0].issues] == ["DUPLICATE_IN_FILE"]
    diff = preview.diff({})
    assert diff.new == [preview.rows[-1].key]
    assert diff.rejected == 0
    dumped = preview.model_dump()
    assert dumped["rows_read"] == 3
    assert dumped["rows_superseded"] == dumped["warnings"] == 2
    assert dumped["rows_rejected"] == dumped["errors"] == 0
    assert len(dumped["issues"]) == 2


def test_req_ing_02_diff_all_categories() -> None:
    def row(key: str | None, digest: str, **flags: bool) -> ParsedRow:
        return ParsedRow(row_number=1, key=key, fields={}, row_hash=digest, **flags)

    rows = [
        row("new", "n"),
        row("changed", "c"),
        row("same", "s"),
        row(None, "r", rejected=True),
        row("skipped", "x", superseded=True),
    ]
    diff = diff_rows(rows, {"changed": "old", "same": "s", "missing": "m"})
    assert diff.model_dump() == {
        "new": ["new"],
        "changed": ["changed"],
        "unchanged": ["same"],
        "missing": ["missing"],
        "rejected": 1,
    }
    assert diff_rows([], {"old": "hash"}).missing == ["old"]


def test_req_ing_02_preview_serialised_errors_and_warnings(tmp_path: Path) -> None:
    preview = parse_workbook(
        workbook(tmp_path, "DL", [dl_row(**{"Code Requisition": None, "Code Local Grade": "Z9"})]),
        "DL",
    )
    dumped = preview.model_dump()
    assert dumped["rows_rejected"] == dumped["errors"] == dumped["warnings"] == 1
    assert {issue["code"] for issue in dumped["issues"]} == {"MISSING_KEY", "UNKNOWN_GRADE"}
    assert '"rows_rejected":1' in preview.model_dump_json()


def test_req_ing_02_canonical_hash_order() -> None:
    assert row_hash({"a": 1, "b": None}) == row_hash({"b": None, "a": 1})
    assert row_hash({"a": 1}) != row_hash({"a": 2})


def test_req_ing_06_file_hash_no_op(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_bytes(b"synthetic")
    digest = file_sha256(path)
    assert digest == hashlib.sha256(b"synthetic").hexdigest()
    assert is_no_op(digest, {digest})
    assert not is_no_op(digest, set())


def test_req_in_12_invalid_date() -> None:
    assert parse_date("bad", CALENDAR_WINDOWS_1900) is None
