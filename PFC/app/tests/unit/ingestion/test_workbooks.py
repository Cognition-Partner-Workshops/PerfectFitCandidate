"""Synthetic REQ-IN-01..14 and REQ-ING-01..03/06 previews; no personal data."""

from datetime import date, datetime
from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.styles import PatternFill
from openpyxl.utils.datetime import CALENDAR_WINDOWS_1900

from pfc.services.ingestion.models import diff_rows, is_no_op, row_hash
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
    sheet.cell(100000, 1).fill = PatternFill("solid", fgColor="FFFFFF")
    path = tmp_path / f"{kind}.xlsx"
    book.save(path)
    return path


def test_req_in_01_03_dl_last_wins_and_rejections(tmp_path: Path) -> None:
    path = workbook(
        tmp_path,
        "DL",
        [
            ["T11234", date(2026, 11, 4), "Example", "B1", "Role", "UK", "BU", "Owner", "old"],
            [
                "T11234",
                "4/11/2026",
                "Example",
                "B1",
                "Role",
                "UK",
                "BU",
                "Owner",
                "```markdown\n? SQL\n• Python\n```",
            ],
            ["OtherID", "bad", None, "Z9", None, None, None, None, "JD"],
            [None, None, None, None, None, None, None, None, "missing"],
        ],
    )
    preview = parse_workbook(path, "DL", empty_row_limit=3)
    assert preview.header_row == 3
    assert preview.sheet_name == "Unpredictable sheet"
    assert preview.rows_read == 4
    assert preview.rows_rejected == 2
    assert preview.rows[0].issues[0].code == "DUPLICATE_IN_FILE"
    assert preview.rows[1].fields["jd_text_norm"] == "- SQL\n- Python"
    assert preview.rows[1].fields["position_start_date"] == "2026-11-04"
    assert {i.code for i in preview.rows[2].issues} == {
        "REQ_ID_FORMAT",
        "DATE_UNPARSEABLE",
        "UNKNOWN_GRADE",
    }
    assert preview.rows[3].issues[0].severity == "error"
    assert is_no_op(preview.file_sha256, {preview.file_sha256})
    assert not is_no_op(preview.file_sha256, set())
    diff = preview.diff({"T11234": preview.rows[1].row_hash, "gone": "old", "OtherID": "old"})
    assert diff.unchanged == ["T11234"]
    assert diff.changed == ["OtherID"]
    assert diff.missing == ["gone"]
    assert diff.rejected == 2
    assert preview.diff({}).new == ["T11234", "OtherID"]
    assert diff_rows([], {"old": "hash"}).missing == ["old"]
    assert row_hash({"a": 1, "b": None}) == row_hash({"b": None, "a": 1})


def test_req_in_10_14_ct_cached_formulas_and_verbatim_email(tmp_path: Path) -> None:
    path = workbook(
        tmp_path,
        "CT",
        [
            [
                " Alex ",
                " Alex@example.com ",
                "B1",
                "UK",
                "New custom status",
                "SQL",
                "N",
                "Wed 4/11/2026",
                None,
                '=HYPERLINK("https://example.com", "Link")',
                "RG1",
                "=1+1",
            ],
            ["Taylor", "invalid", "Z9", "UK", "Available", "Python", "Y", 46330, "nonsense"],
            ["alex", "alex@example.com", "B1", "UK", "Available", "SQL", "N"],
            [None, "missing@example.com", "B1", "UK", "Available"],
        ],
    )
    preview = parse_workbook(path, "CT", empty_row_limit=2)
    first = preview.rows[0]
    assert first.key == ("alex", "alex@example.com")
    assert first.fields["email_address"] == " Alex@example.com "
    assert first.fields["tracker_text"] is None
    assert first.fields["group_owner"] is None
    assert first.fields["bench_start_date"] == "2026-11-04"
    assert first.fields["bench_end_date"] is None
    assert first.fields["bench_end_date_parse_status"] == "blank"
    assert {i.code for i in first.issues} == {
        "EMAIL_INVALID",
        "UNMAPPED_STATUS",
        "DUPLICATE_IN_FILE",
    }
    second = preview.rows[1]
    assert second.fields["bench_start_date"] is not None
    assert second.fields["bench_end_date_parse_status"] == "unparseable"
    issue = next(i for i in second.issues if i.code == "DATE_UNPARSEABLE")
    assert issue.raw_value == "nonsense"
    assert issue.row_number == 5
    assert {i.code for i in second.issues} == {"DATE_UNPARSEABLE", "EMAIL_INVALID", "UNKNOWN_GRADE"}
    assert preview.rows_rejected == 2
    assert preview.diff({}).new == [("taylor", "invalid"), ("alex", "alex@example.com")]


@pytest.mark.parametrize(
    "value", [datetime(2026, 11, 4), date(2026, 11, 4), "Wed 4/11/2026", "2026-11-04", "04-11-2026"]
)
def test_req_in_12_dates(value: object) -> None:
    assert parse_date(value, CALENDAR_WINDOWS_1900) == date(2026, 11, 4)


def test_req_ing_02_empty_and_errors(tmp_path: Path) -> None:
    path = workbook(tmp_path, "DL", [])
    assert parse_workbook(path, "DL").rows == []
    with pytest.raises(ValueError, match="No CT header"):
        parse_workbook(path, "CT")
    with pytest.raises(ValueError, match="positive"):
        parse_workbook(path, "DL", empty_row_limit=0)
    with pytest.raises(ValueError, match=".xlsx"):
        parse_workbook(tmp_path / "macro.xlsm", "DL")
    assert parse_date("bad", CALENDAR_WINDOWS_1900) is None
