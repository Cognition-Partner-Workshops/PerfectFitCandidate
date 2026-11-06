"""P1-EX-03 / KPI-02/05: hand-computed baselines, REQ-ING-02/06, MAT-02, EML-01."""

import json
import os
import sqlite3
import stat
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook
from test_matching_service import NOW, seed_demand, seed_resource
from test_matching_service import settings as settings

from pfc.config import Settings
from pfc.db.connection import database
from pfc.services.ingestion.models import file_sha256
from pfc.services.ingestion.repository import upload_path
from pfc.services.ingestion.workbooks import CT, parse_workbook, valid_ct_email
from pfc.services.kpi.baselines import markdown, measure

PRIVATE = (
    "Private Alice",
    "Private Bob",
    "Private Carol",
    "Private Rejected",
    "Private Dave",
    "alice@example.com",
    "bob@example.com",
    "rejected@example.com",
    "Private Resource",
    "private.resource@example.com",
    "Secret Name",
    "hidden@example.com",
    "Private Sender",
    "AlexBorder@capgemini.com",
    "Analyst",
)


def ingestion(
    db: sqlite3.Connection,
    settings: Settings,
    kind: str,
    *,
    total: int = 0,
    rejected: int = 0,
    rows: list[dict[str, Any]] | None = None,
    day: int = 0,
    status: str = "committed",
) -> int:
    stamp = (NOW + timedelta(days=day)).isoformat()
    run_id = int(
        db.execute(
            "INSERT INTO ingestion_runs(kind,source,file_name,file_sha256,file_size,status,"
            "rows_read,rows_rejected,started_at,committed_at) "
            "VALUES (?,'manual_upload','Secret Name hidden@example.com.xlsx',"
            "'fixture',0,?,?,?,?,?)",
            (kind, status, total, rejected, stamp, stamp if status == "committed" else None),
        ).lastrowid
        or 0
    )
    if kind == "CT":
        path = upload_path(settings, run_id)
        path.parent.mkdir(exist_ok=True)
        book = Workbook()
        sheet = book.active
        sheet.append(list(CT))
        for row in rows or []:
            sheet.append([row.get(header) for header in CT])
        book.save(path)
        book.close()
        db.execute(
            "UPDATE ingestion_runs SET file_sha256=? WHERE ingestion_run_id=?",
            (file_sha256(path), run_id),
        )
    return run_id


def demand(
    db: sqlite3.Connection,
    code: str,
    cycle: int | None,
    confirm: float | None,
    reviewed: float | None,
) -> int:
    demand_id = seed_demand(db, code, confirmed=False)
    db.execute(
        "UPDATE demands SET first_ingestion_id=?,last_ingestion_id=? WHERE demand_id=?",
        (cycle, cycle, demand_id),
    )
    version = db.execute(
        "SELECT current_version_id FROM demands WHERE demand_id=?", (demand_id,)
    ).fetchone()[0]
    if confirm is not None:
        db.execute(
            "INSERT INTO jd_requirements(demand_version_id,revision,status,"
            "confirmed_at,created_at) "
            "VALUES (?,1,'confirmed',?,?)",
            (version, (NOW + timedelta(hours=confirm)).isoformat(), NOW.isoformat()),
        )
    if reviewed is not None:
        db.execute(
            "INSERT INTO email_messages(demand_id,resource_id,email_template_id,to_address,"
            "cc_json,subject,body_html,status,idempotency_key,created_by,created_at,updated_at) "
            "VALUES (?,1,1,'hidden@example.com','[]','Private Sender','Private Sender',"
            "'Discarded',?,1,?,?)",
            (demand_id, code, (NOW + timedelta(hours=reviewed)).isoformat(), NOW.isoformat()),
        )
    return demand_id


@pytest.fixture
def sample(settings: Settings) -> tuple[Settings, dict[str, int]]:
    with database(settings) as db:
        seed_resource(db, "Private Resource", email="private.resource@example.com")
        db.execute(
            "INSERT INTO email_templates(email_template_id,name,version,subject,"
            "body_html,created_at) "
            "VALUES (1,'Private Sender',1,'Private Sender','Private Sender',?)",
            (NOW.isoformat(),),
        )
        cycles = {
            "dl": ingestion(db, settings, "DL", total=5, rejected=1),
            "dl_ten": ingestion(db, settings, "DL", total=10, day=1),
            "dl_zero": ingestion(db, settings, "DL", day=2),
        }
        rows = [
            {
                "ShortName": "Private Alice",
                "Email Address": "alice@example.com",
                "Grade": "B1",
                "Skills": "SQL",
            },
            {"ShortName": "Private Bob", "Email Address": "bob@example.com", "Skills": "SQL"},
            {"ShortName": "Private Carol", "Email Address": "bad-email", "Grade": "B2"},
            {"Email Address": "rejected@example.com", "Grade": "B1", "Skills": "SQL"},
            {"ShortName": "Private Dave", "Grade": "   ", "Skills": "   "},
        ]
        for row in rows:
            row["Bench Status"] = "Available"
        cycles["ct"] = ingestion(db, settings, "CT", total=5, rejected=1, rows=rows, day=3)
        cycles["ct_later"] = ingestion(
            db,
            settings,
            "CT",
            total=2,
            day=4,
            rows=[
                {
                    "ShortName": "Private Alice",
                    "Email Address": " ALICE@EXAMPLE.COM ",
                    "Grade": "B1",
                    "Skills": "SQL",
                    "Bench Status": "Available",
                },
                {
                    "ShortName": "Private Bob",
                    "Email Address": "invalid",
                    "Skills": "SQL",
                    "Bench Status": "Available",
                },
            ],
        )
        cycles["ct_zero"] = ingestion(db, settings, "CT", day=5)
        ingestion(db, settings, "CT", status="preview", day=9)
        ingestion(db, settings, "CT", status="no_op", day=10)
        for code, confirm, reviewed in (
            ("T10001", 2, 5),
            ("T10002", 5, 8),
            ("T10003", None, 10),
            ("T10004", 10, None),
        ):
            demand(db, code, cycles["dl"], confirm, reviewed)
        for i in range(1, 11):
            demand(db, f"T12{i:03d}", cycles["dl_ten"], 24 + i, 24 + 2 * i)
        demand(db, "T19998", None, 1, 2)
        demand(db, "T19999", None, None, None)
        for _ in range(2):
            db.execute(
                "INSERT INTO ingestion_row_issues(ingestion_run_id,row_number,severity,"
                "code,message) "
                "VALUES (?,3,'error','MISSING_KEY','Secret Name')",
                (cycles["ct"],),
            )
    return settings, cycles


def report(sample: tuple[Settings, dict[str, int]]) -> dict[str, Any]:
    with database(sample[0]) as db:
        db.execute("PRAGMA query_only=ON")
        before = db.total_changes
        result = measure(db, sample[0], now=NOW)
        assert db.total_changes == before
    return result


def test_kpi05_hand_computed_per_run(sample: tuple[Settings, dict[str, int]]) -> None:
    """KPI-05 / REQ-ING-02/06: include rejected rows; never count issues as rejected rows."""
    result = report(sample)["kpi05"]
    ids = sample[1]
    assert result["ct_runs"] == [
        {
            "run_id": ids["ct"],
            "total_rows": 5,
            "grade": {"count": 3, "percent": 60},
            "skills": {"count": 3, "percent": 60},
            "valid_email": {"count": 3, "percent": 60},
            "all_three": {"count": 2, "percent": 40},
        },
        {
            "run_id": ids["ct_later"],
            "total_rows": 2,
            "grade": {"count": 1, "percent": 50},
            "skills": {"count": 2, "percent": 100},
            "valid_email": {"count": 1, "percent": 50},
            "all_three": {"count": 1, "percent": 50},
        },
        {
            "run_id": ids["ct_zero"],
            "total_rows": 0,
            **{
                key: {"count": 0, "percent": None}
                for key in ("grade", "skills", "valid_email", "all_three")
            },
        },
    ]
    assert result["headline_ct"] == result["ct_runs"][-1]
    assert result["parse_errors"] == [
        {
            "run_id": ids[key],
            "kind": kind,
            "total_rows": total,
            "rejected_rows": rejected,
            "percent": percent,
        }
        for key, kind, total, rejected, percent in (
            ("dl", "DL", 5, 1, 20),
            ("dl_ten", "DL", 10, 0, 0),
            ("dl_zero", "DL", 0, 0, None),
            ("ct", "CT", 5, 1, 20),
            ("ct_later", "CT", 2, 0, 0),
            ("ct_zero", "CT", 0, 0, None),
        )
    ]


def test_kpi02_hand_computed_cycles(sample: tuple[Settings, dict[str, int]]) -> None:
    """KPI-02: nearest-rank p90 of 1..10 is rank ceil(9)=9, not max or interpolation."""
    result = report(sample)["kpi02"]
    assert result["reviewed_proxy"] == "first Send to Email Approval"
    assert result["p90_method"] == "nearest-rank: ceil(0.9*n), 1-based"
    first, ten, unknown = result["cycles"]
    assert first == {
        "run_id": sample[1]["dl"],
        "demand_count": 4,
        "missing_stages": {"upload": 0, "confirm": 1, "reviewed": 1},
        "elapsed_hours": {
            "upload_to_confirm": {"count": 3, "median": 5, "p90": 10},
            "confirm_to_reviewed": {"count": 2, "median": 3, "p90": 3},
            "upload_to_reviewed": {"count": 3, "median": 8, "p90": 10},
        },
    }
    assert ten == {
        "run_id": sample[1]["dl_ten"],
        "demand_count": 10,
        "missing_stages": {"upload": 0, "confirm": 0, "reviewed": 0},
        "elapsed_hours": {
            "upload_to_confirm": {"count": 10, "median": 5.5, "p90": 9},
            "confirm_to_reviewed": {"count": 10, "median": 5.5, "p90": 9},
            "upload_to_reviewed": {"count": 10, "median": 11, "p90": 18},
        },
    }
    assert unknown == {
        "run_id": None,
        "demand_count": 2,
        "missing_stages": {"upload": 2, "confirm": 1, "reviewed": 1},
        "elapsed_hours": {
            "upload_to_confirm": {"count": 0, "median": None, "p90": None},
            "confirm_to_reviewed": {"count": 1, "median": 1, "p90": 1},
            "upload_to_reviewed": {"count": 0, "median": None, "p90": None},
        },
    }
    by_code = {row["requisition"]: row for row in result["demands"]}
    for code, expected in {
        "T10001": (2, 3, 5),
        "T10002": (5, 3, 8),
        "T10003": (None, None, 10),
        "T10004": (10, None, None),
        "T19998": (None, 1, None),
        "T19999": (None, None, None),
        **{f"T12{i:03d}": (i, i, 2 * i) for i in range(1, 11)},
    }.items():
        assert tuple(by_code[code]["elapsed_hours"].values()) == expected
    assert by_code["T10001"]["upload_at"] == NOW.isoformat()
    assert by_code["T10001"]["confirm_at"] == (NOW + timedelta(hours=2)).isoformat()
    assert by_code["T10001"]["reviewed_at"] == (NOW + timedelta(hours=5)).isoformat()


def test_kpi02_current_version_first_confirmation_and_first_draft(
    sample: tuple[Settings, dict[str, int]],
) -> None:
    """REQ-MAT-02 / REQ-EML-01: old version ignored, superseded confirmations retained."""
    with database(sample[0]) as db:
        demand_id, old = db.execute(
            "SELECT demand_id,current_version_id FROM demands WHERE code_requisition='T10001'"
        ).fetchone()
        current = db.execute(
            "INSERT INTO demand_versions(demand_id,version_no,jd_text_raw,jd_text_norm,jd_sha256,"
            "created_at) VALUES (?,2,'changed','changed','changed',?)",
            (demand_id, NOW.isoformat()),
        ).lastrowid
        db.execute(
            "UPDATE demands SET current_version_id=?,last_ingestion_id=? WHERE demand_id=?",
            (current, sample[1]["dl_ten"], demand_id),
        )
        for revision, hour, status in ((1, 3, "superseded"), (2, 4, "confirmed")):
            db.execute(
                "INSERT INTO jd_requirements(demand_version_id,revision,status,"
                "confirmed_at,created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    current,
                    revision,
                    status,
                    (NOW + timedelta(hours=hour)).isoformat(),
                    NOW.isoformat(),
                ),
            )
        db.execute(
            "INSERT INTO email_messages(demand_id,resource_id,email_template_id,to_address,cc_json,"
            "subject,body_html,status,idempotency_key,send_seq,created_by,created_at,updated_at) "
            "VALUES (?,1,1,'hidden@example.com','[]','Private Sender','Private Sender','Draft',"
            "'second-draft',2,1,?,?)",
            (demand_id, (NOW + timedelta(hours=8)).isoformat(), NOW.isoformat()),
        )
        assert old != current
    row = report(sample)["kpi02"]["demands"][0]
    assert row["cycle_run_id"] == sample[1]["dl"]
    assert row["elapsed_hours"] == {
        "upload_to_confirm": 3,
        "confirm_to_reviewed": 2,
        "upload_to_reviewed": 5,
    }


def test_kpi05_headline_uses_commit_time_not_id(sample: tuple[Settings, dict[str, int]]) -> None:
    with database(sample[0]) as db:
        db.execute(
            "UPDATE ingestion_runs SET committed_at=? WHERE ingestion_run_id=?",
            ((NOW + timedelta(days=20)).isoformat(), sample[1]["ct"]),
        )
    assert report(sample)["kpi05"]["headline_ct"]["run_id"] == sample[1]["ct"]


@pytest.mark.parametrize(
    "email,valid",
    [
        ("Mixed.Case+tag@Example.COM", True),
        ("a_b@example.com", True),
        ("bad", False),
        ("", False),
        ("x@@example.com", False),
        ("x y@example.com", False),
    ],
)
def test_kpi05_reuses_ct_email_predicate(tmp_path: Path, email: str, valid: bool) -> None:
    """REQ-IN-14 / KPI-05: one validity predicate, also used by workbook parsing."""
    path = tmp_path / "ct.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.append(["ShortName", "Email Address", "Grade", "Bench Status"])
    sheet.append(["Private Alice", email, "B1", "Available"])
    book.save(path)
    book.close()
    row = parse_workbook(path, "CT").rows[0]
    assert valid_ct_email(row.fields["email_key"]) is valid
    assert any(issue.code == "EMAIL_INVALID" for issue in row.issues) is bool(email and not valid)


def test_privacy(sample: tuple[Settings, dict[str, int]]) -> None:
    """KPI-02/05: output has only requisition/run identifiers; zero percentages are n/a."""
    result = report(sample)
    output = json.dumps(result) + markdown(result)
    for private in PRIVATE:
        assert private.casefold() not in output.casefold()
    assert "0 (n/a)" in markdown(result)
    assert "first Send to Email Approval" in markdown(result)
    assert "manual and out of scope" in markdown(result)


def test_no_runs_or_demands(settings: Settings) -> None:
    result = report((settings, {}))
    assert result["kpi05"] == {"headline_ct": None, "ct_runs": [], "parse_errors": []}
    assert result["kpi02"]["demands"] == []
    assert result["kpi02"]["cycles"] == []
    assert "Latest committed CT run: n/a" in markdown(result)


@pytest.mark.parametrize("code", ["private.person@example.com", "Private Person"])
def test_invalid_requisitions_never_expose_pii(
    sample: tuple[Settings, dict[str, int]], code: str
) -> None:
    """KPI-02 privacy / REQ-IN-02: invalid source requisitions use a safe demand id."""
    with database(sample[0]) as db:
        demand_id = db.execute(
            "SELECT demand_id FROM demands WHERE code_requisition='T10001'"
        ).fetchone()[0]
        db.execute("UPDATE demands SET code_requisition=? WHERE demand_id=?", (code, demand_id))
    result = report(sample)
    assert code not in json.dumps(result)
    assert code not in markdown(result)
    assert any(row["requisition"] == f"demand:{demand_id}" for row in result["kpi02"]["demands"])


@pytest.mark.parametrize("damage", ["missing", "changed", "invalid", "counts", "rejected"])
def test_unavailable_history_fails_without_pii(
    sample: tuple[Settings, dict[str, int]],
    damage: str,
) -> None:
    """KPI-05 / REQ-ING-06: never substitute today's resources for absent historical rows."""
    settings, ids = sample
    path = upload_path(settings, ids["ct"])
    if damage == "missing":
        path.unlink()
    elif damage in {"changed", "invalid"}:
        path.write_bytes(b"Secret Name hidden@example.com")
    with database(settings) as db:
        if damage == "invalid":
            db.execute(
                "UPDATE ingestion_runs SET file_sha256=? WHERE ingestion_run_id=?",
                (file_sha256(path), ids["ct"]),
            )
        if damage == "counts":
            db.execute(
                "UPDATE ingestion_runs SET rows_read=99 WHERE ingestion_run_id=?", (ids["ct"],)
            )
        if damage == "rejected":
            db.execute(
                "UPDATE ingestion_runs SET rows_rejected=NULL WHERE ingestion_run_id=?",
                (ids["ct"],),
            )
    with pytest.raises(ValueError) as error:
        report(sample)
    for private in PRIVATE:
        assert private not in str(error.value)


@pytest.mark.parametrize("value", ["bad timestamp", "2026-11-06T00:00:00"])
def test_invalid_persisted_time_is_rejected(
    sample: tuple[Settings, dict[str, int]],
    value: str,
) -> None:
    with database(sample[0]) as db:
        db.execute(
            "UPDATE ingestion_runs SET committed_at=? WHERE ingestion_run_id=?",
            (value, sample[1]["dl"]),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        report(sample)


def test_naive_generated_at_rejected(settings: Settings) -> None:
    with database(settings) as db, pytest.raises(ValueError, match="now must be timezone-aware"):
        measure(db, settings, now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "case",
    [
        "normal",
        "same",
        "database",
        "missing",
        "hardlink",
        "unwritable_md",
        "unwritable_md_existing",
        "refresh_mode",
    ],
)
def test_cli_reports_are_read_only(
    sample: tuple[Settings, dict[str, int]],
    tmp_path: Path,
    case: str,
) -> None:
    """P1-EX-03: real CLI, read-only SQLite, collision protection, no PII in either format."""
    settings = sample[0]
    output = tmp_path.parent / f"{tmp_path.name}.json"
    md = tmp_path.parent / f"{tmp_path.name}.md"
    source = settings.data_dir / "db" / "pfc.db"
    before = source.read_bytes()
    if case == "same":
        md = output
    elif case == "database":
        output = source
    elif case == "missing":
        upload_path(settings, sample[1]["ct"]).unlink()
    elif case == "hardlink":
        try:
            os.link(source, output)
        except OSError:
            pytest.skip("Hardlinks unavailable")
    elif case.startswith("unwritable_md"):
        blocker = tmp_path.parent / f"{tmp_path.name}.blocker"
        blocker.write_text("Not a directory", encoding="utf-8")
        md = blocker / "report.md"
        if case == "unwritable_md_existing":
            output.write_text("Existing report", encoding="utf-8")
    elif case == "refresh_mode":
        if os.name != "posix":
            pytest.skip("POSIX permission bits")
        for path in (output, md):
            path.write_text("Existing report", encoding="utf-8")
            path.chmod(0o644)
    app = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "scripts/measure_kpi_baselines.py",
            "--data-dir",
            str(settings.data_dir),
            "--out-json",
            str(output),
            "--out-md",
            str(md),
        ],
        cwd=app,
        env={**os.environ, "PYTHONPATH": str(app)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert source.read_bytes() == before
    assert result.stdout == ""
    if case == "refresh_mode":
        assert result.returncode == 0, result.stderr
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o644 for path in (output, md))
        return
    if case != "normal":
        assert result.returncode == 1
        assert not md.exists()
        for private in PRIVATE:
            assert private not in result.stderr
        if case.startswith("unwritable_md"):
            if case == "unwritable_md_existing":
                assert output.read_text(encoding="utf-8") == "Existing report"
            else:
                assert not output.exists()
            assert not list(output.parent.glob(f".{output.name}.*.tmp"))
        return
    assert result.returncode == 0, result.stderr
    if os.name == "posix":
        umask = os.umask(0)
        os.umask(umask)
        assert stat.S_IMODE(output.stat().st_mode) == 0o666 & ~umask
    value = json.loads(output.read_text(encoding="utf-8"))
    assert value["kpi05"]["headline_ct"]["run_id"] == sample[1]["ct_zero"]
    assert markdown(value) == md.read_text(encoding="utf-8")
    assert "| 1 | upload_to_confirm | 3 | 5.00 | 10.00 |" in markdown(value)
    for private in PRIVATE:
        assert private.casefold() not in (output.read_text() + md.read_text()).casefold()
