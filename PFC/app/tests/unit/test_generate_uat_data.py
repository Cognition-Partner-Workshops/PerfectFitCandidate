import re
from pathlib import Path

import pytest

from pfc.services.ingestion.workbooks import parse_workbook
from scripts.generate_uat_data import generate


def codes(path: Path, kind: str) -> set[str]:
    preview = parse_workbook(path, kind)  # type: ignore[arg-type]
    assert preview.errors == 0
    return {issue.code for issue in preview.issues}


def test_default_500_by_500_parses_with_zero_errors(tmp_path: Path) -> None:
    paths = generate(tmp_path)
    dl, ct = parse_workbook(paths["dl"], "DL"), parse_workbook(paths["ct"], "CT")
    assert (dl.errors, ct.errors) == (0, 0)
    assert len(dl.rows) == len(ct.rows) == 500
    assert {issue.code for issue in dl.issues} == set()
    assert {issue.code for issue in ct.issues} == {"UNMAPPED_STATUS", "DATE_UNPARSEABLE"}
    assert len(list(paths["resumes"].glob("*.txt"))) == 500


def test_rows_exercise_eligibility_rules_with_synthetic_contacts_only(tmp_path: Path) -> None:
    paths = generate(tmp_path, demands=30, resources=120)
    rows = parse_workbook(paths["ct"], "CT").rows
    fields = [row.fields for row in rows]
    assert all(str(f["email_address"]).endswith("@example.com") for f in fields)
    assert {f["bench_status"] for f in fields} >= {"Selected", "On secondment", "Available"}
    assert {f["bench_end_date_parse_status"] for f in fields} == {"blank", "valid", "unparseable"}
    assert {f["available_capacity"] for f in fields} >= {"Y", "N"}
    demand_grades = {
        row.fields["code_local_grade"] for row in parse_workbook(paths["dl"], "DL").rows
    }
    assert len({f["grade"] for f in fields}) > 1 and len(demand_grades) > 1
    texts = [path.read_text("utf-8") for path in sorted(paths["resumes"].glob("*.txt"))]
    phones = [re.search(r"Phone: (.+)", text) for text in texts]
    assert all(m and re.fullmatch(r"07700 900\d{3}", m.group(1)) for m in phones)
    assert sum("Qualifications" not in text for text in texts) > 0


def test_same_seed_gives_same_data(tmp_path: Path) -> None:
    first, second = (
        generate(tmp_path / "a", demands=20, resources=20),
        generate(tmp_path / "b", demands=20, resources=20),
    )
    for kind, key in (("DL", "dl"), ("CT", "ct")):
        hashes = [
            [row.row_hash for row in parse_workbook(p[key], kind).rows] for p in (first, second)
        ]  # type: ignore[arg-type]
        assert hashes[0] == hashes[1]
    assert [p.read_text("utf-8") for p in sorted(first["resumes"].iterdir())] == [
        p.read_text("utf-8") for p in sorted(second["resumes"].iterdir())
    ]


def test_dirty_percent_changes_that_share_of_ct_rows(tmp_path: Path) -> None:
    paths = generate(tmp_path, demands=10, resources=200, dirty_percent=5)
    assert codes(paths["ct_dirty"], "CT") <= {"UNMAPPED_STATUS", "DATE_UNPARSEABLE"}
    base = {row.key: row.row_hash for row in parse_workbook(paths["ct"], "CT").rows}
    dirty = {row.key: row.row_hash for row in parse_workbook(paths["ct_dirty"], "CT").rows}
    assert base.keys() == dirty.keys()
    assert sum(base[key] != dirty[key] for key in base) == 10


@pytest.mark.parametrize(
    "kwargs", [{"demands": 0}, {"resources": 10000}, {"dirty_percent": 0}, {"dirty_percent": 101}]
)
def test_rejects_out_of_range_options(tmp_path: Path, kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        generate(tmp_path, **kwargs)  # type: ignore[arg-type]


def test_small_dirty_percent_still_changes_a_row(tmp_path: Path) -> None:
    paths = generate(tmp_path, demands=5, resources=5, dirty_percent=1)
    base = [row.row_hash for row in parse_workbook(paths["ct"], "CT").rows]
    dirty = [row.row_hash for row in parse_workbook(paths["ct_dirty"], "CT").rows]
    assert sum(a != b for a, b in zip(base, dirty, strict=True)) == 1


def test_smaller_rerun_leaves_only_its_own_cvs(tmp_path: Path) -> None:
    generate(tmp_path, demands=5, resources=20)
    (tmp_path / "resumes" / "personcustom@example.com.txt").write_text("tester's own CV", "utf-8")
    paths = generate(tmp_path, demands=5, resources=5)
    names = [f"person{i:04d}@example.com.txt" for i in range(1, 6)] + [
        "personcustom@example.com.txt"
    ]
    assert sorted(p.name for p in paths["resumes"].iterdir()) == sorted(names)
