"""Hand-computed KPI-01 contracts using real migrated SQLite tables."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_matching_service import NOW, seed_candidate, seed_demand, seed_resource
from test_matching_service import settings as settings

from pfc.config import Settings
from pfc.db.connection import database
from pfc.services.kpi.kpi01 import markdown, measure, parse_labels

A = """code_requisition,email,split
PERF,alice@example.com,
PART,alice@example.com,holdout
PART,carol@example.com,holdout
MISS,carol@example.com,holdout
ZERO,alice@example.com,holdout
UNRES,unknown@example.com,holdout
WEAK,,holdout
STRONG,,holdout
TUNE,carol@example.com,tune
"""
B = """code_requisition,email,split
PERF,ALICE@example.com,holdout
PART,alice@example.com,holdout
PART,carol@example.com,holdout
MISS,alice@example.com,holdout
ZERO,bob@example.com,holdout
UNRES,,holdout
WEAK,,holdout
STRONG,,holdout
TUNE,carol@example.com,tune
"""


@pytest.fixture
def sample(settings: Settings) -> tuple[Settings, int]:
    with database(settings) as db:
        demands = {
            code: seed_demand(db, code)
            for code in ("PERF", "PART", "MISS", "ZERO", "UNRES", "WEAK", "STRONG", "TUNE")
        }
        resources = {
            name: seed_resource(db, name, email=f"{name}@example.com")
            for name in ("alice", "bob", "carol")
        }
        template = db.execute(
            "INSERT INTO scoring_templates(name,version,weights_json,rules_json,status,created_at) "
            "VALUES ('kpi-fixture',3,'{}','{}','active',?)",
            (NOW.isoformat(),),
        ).lastrowid
        model = db.execute(
            "INSERT INTO embedding_models(name,runtime,file_sha256,dim,pipeline_version) "
            "VALUES ('fake-embed','onnx','sha256-fixture',2,'v2')"
        ).lastrowid
        run = int(
            db.execute(
                "INSERT INTO match_runs(scope,template_id,embedding_model_id,"
                "llm_model,engine_version,"
                "synonyms_version,config_hash,status,dq_summary_json) "
                "VALUES ('full',?,?,'fixture-llm','engine-1','syn','config','completed',?)",
                (
                    template,
                    model,
                    json.dumps({"demands": {str(value): {} for value in demands.values()}}),
                ),
            ).lastrowid
            or 0
        )
        for code, person, rank, band, shortlisted in (
            ("PERF", "alice", 1, "Strong", 1),
            ("PART", "alice", 1, "Strong", 1),
            ("PART", "bob", 2, "Potential", 1),
            ("MISS", "alice", 1, "Strong", 1),
            ("WEAK", "bob", 1, "Weak", 1),
            ("STRONG", "alice", 1, "Strong", 1),
            ("TUNE", "alice", 1, "Strong", 1),
            ("TUNE", "carol", 6, "Avg", 0),
        ):
            version = db.execute(
                "SELECT current_version_id FROM demands WHERE demand_id=?", (demands[code],)
            ).fetchone()[0]
            req = db.execute(
                "SELECT jd_req_id FROM jd_requirements WHERE demand_version_id=?", (version,)
            ).fetchone()[0]
            result = db.execute(
                "INSERT INTO match_results(run_id,demand_id,demand_version_id,"
                "jd_req_id,resource_id,"
                "score,band,rank,shortlisted,grade_relation,location_assessment,matching_evidence,"
                "gaps_checks,eligibility_evidence) VALUES (?,?,?,?,?,80,?,?,?,'exact','Aligned',"
                "'Evidence','Review','Eligible')",
                (run, demands[code], version, req, resources[person], band, rank, shortlisted),
            ).lastrowid
            if code == "TUNE":
                db.execute(
                    "INSERT INTO overrides(demand_id,resource_id,result_id,action,"
                    "reason,created_by,created_at) "
                    "VALUES (?,?,?,?,'fixture',1,?)",
                    (
                        demands[code],
                        resources[person],
                        result,
                        "exclude" if person == "alice" else "pin",
                        NOW.isoformat(),
                    ),
                )
    return settings, run


def evaluate(sample: tuple[Settings, int], a: str = A, b: str | None = B) -> dict[str, Any]:
    settings, run = sample
    with database(settings) as db:
        changes = db.total_changes
        report = measure(
            db,
            run,
            parse_labels(io.StringIO(a)),
            parse_labels(io.StringIO(b)) if b is not None else None,
            now=NOW,
        )
        assert db.total_changes == changes
    return report


def test_hand_computed_metrics(sample: tuple[Settings, int]) -> None:
    report = evaluate(sample)
    persisted = report["splits"]["holdout"]["persisted"]
    assert persisted == {
        "demands_labelled": 7,
        "scored": 4,
        "excluded": 1,
        "no_suitable": 2,
        "no_suitable_agree": 1,
        "no_suitable_disagree": 1,
        "hit_rate_at_5": 0.5,
        "precision_at_5": 0.375,
    }
    assert report["splits"]["holdout"]["after_overrides"] == persisted
    ceiling = report["splits"]["holdout"]["analyst_b"]
    assert ceiling == {
        **persisted,
        "precision_at_5": 0.5,
        "no_suitable_agree": 2,
        "no_suitable_disagree": 0,
    }
    rows = {row["requisition"]: row for row in report["demands"]}
    for code, size, gold, hits, precision in (
        ("PERF", 1, 1, 1, 1),
        ("PART", 2, 2, 1, 0.5),
        ("MISS", 1, 1, 0, 0),
        ("ZERO", 0, 1, 0, 0),
    ):
        value = rows[code]["variants"]["persisted"]
        assert (value["s_size"], rows[code]["g_size"], value["hits"], value["precision"]) == (
            size,
            gold,
            hits,
            precision,
        )
    assert rows["PERF"]["split"] == "holdout"
    assert rows["WEAK"]["variants"]["persisted"]["no_suitable_agreement"] is True
    assert rows["STRONG"]["variants"]["persisted"]["no_suitable_agreement"] is False
    for variant, expected in (("persisted", 0), ("after_overrides", 1), ("analyst_b", 1)):
        value = report["splits"]["tune"][variant]
        assert value["scored"] == 1
        assert value["hit_rate_at_5"] == value["precision_at_5"] == expected
    assert report["scoring_template"] == {"name": "kpi-fixture", "version": 3}
    assert report["embedding_model"] == {
        "name": "fake-embed",
        "version": "v2",
        "sha": "sha256-fixture",
    }
    assert report["engine_version"] == "engine-1"
    assert report["llm_model"] == "fixture-llm"
    assert report["generated_at"] == NOW.isoformat()
    for output in (json.dumps(report), markdown(report)):
        for email in (
            "alice@example.com",
            "ALICE@example.com",
            "bob@example.com",
            "carol@example.com",
            "unknown@example.com",
        ):
            assert email not in output
    assert any(issue.get("email") == "un*****@example.com" for issue in report["data_issues"])


def test_issues_and_scope(sample: tuple[Settings, int]) -> None:
    settings, run = sample
    with database(settings) as db:
        seed_demand(db, "OUTSIDE")
    labels = A + "PART,,tune\nOUTSIDE,alice@example.com,holdout\nABSENT,,holdout\n"
    report = evaluate(sample, labels, B.replace("PERF,ALICE@example.com,holdout\n", ""))
    issues = {(issue["requisition"], issue["code"]) for issue in report["data_issues"]}
    assert {
        ("PART", "conflicting_splits"),
        ("PART", "empty_label_ignored"),
        ("OUTSIDE", "not_assessed_by_run"),
        ("ABSENT", "not_assessed_by_run"),
        ("PERF", "missing_labels"),
    } <= issues
    assert report["splits"]["holdout"]["persisted"]["excluded"] == 3
    assert report["splits"]["holdout"]["analyst_b"]["excluded"] == 4
    assert report["splits"]["holdout"]["analyst_b"]["hit_rate_at_5"] == pytest.approx(1 / 3)
    assert (
        next(row for row in report["demands"] if row["requisition"] == "PART")["split"] == "holdout"
    )
    with database(settings) as db:
        db.execute("UPDATE match_runs SET dq_summary_json='{}' WHERE run_id=?", (run,))
    report = evaluate(
        sample,
        "code_requisition,email,split\nPERF,alice@example.com,\nZERO,alice@example.com,\n",
        None,
    )
    assert report["splits"]["holdout"]["persisted"]["scored"] == 1
    assert report["splits"]["holdout"]["persisted"]["excluded"] == 1


def test_unique_profiles_and_ambiguous_emails(sample: tuple[Settings, int]) -> None:
    settings, run = sample
    with database(settings) as db:
        candidate = seed_candidate(db)
        db.execute("UPDATE resources SET candidate_id=? WHERE short_name='alice'", (candidate,))
        alias = seed_resource(db, "Alias", candidate=candidate, email="ALICE@example.com")
        source = dict(
            db.execute(
                "SELECT * FROM match_results WHERE run_id=? AND demand_id="
                "(SELECT demand_id FROM demands WHERE code_requisition='PERF')",
                (run,),
            ).fetchone()
        )
        db.execute(
            "UPDATE match_results SET candidate_id=? WHERE resource_id=?",
            (candidate, source["resource_id"]),
        )
        db.execute(
            "INSERT INTO match_results(run_id,demand_id,demand_version_id,jd_req_id,"
            "resource_id,candidate_id,score,band,rank,shortlisted,grade_relation,"
            "location_assessment,matching_evidence,gaps_checks,eligibility_evidence) "
            "VALUES (?,?,?,?,?,?,80,'Avg',2,1,'exact','Aligned','E','G','E')",
            (
                run,
                source["demand_id"],
                source["demand_version_id"],
                source["jd_req_id"],
                alias,
                candidate,
            ),
        )
    labels = "code_requisition,email,split\nPERF,alice@example.com,\n"
    report = evaluate(sample, labels, None)
    assert report["demands"][0]["variants"]["persisted"]["s_size"] == 1
    assert report["splits"]["holdout"]["persisted"]["precision_at_5"] == 1
    with database(settings) as db:
        seed_resource(db, "Other profile", email="alice@example.com")
    report = evaluate(sample, labels, None)
    assert report["splits"]["holdout"]["persisted"]["excluded"] == 1
    assert {issue["code"] for issue in report["data_issues"]} == {
        "ambiguous_email",
        "no_resolved_labels",
    }


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled", "missing"])
def test_completed_run_required(sample: tuple[Settings, int], status: str) -> None:
    settings, run = sample
    with database(settings) as db:
        if status != "missing":
            db.execute("UPDATE match_runs SET status=? WHERE run_id=?", (status, run))
        with pytest.raises(ValueError, match="completed run"):
            measure(db, run if status != "missing" else run + 100, [])


@pytest.mark.parametrize(
    "text,match",
    [
        ("code_requisition,email,split\nX,private@example.com,invalid\n", "line 2"),
        ("code_requisition,email,split\n,private@example.com,\n", "line 2"),
        ("code_requisition,email,split\nX\n", "line 2"),
        ("code_requisition,email,split\nX,,holdout,extra\n", "line 2"),
        ('code_requisition,email,split\nX,"unterminated', "line 2"),
        ("code_requisition,email\nX,private@example.com\n", "header"),
    ],
)
def test_invalid_csv(text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match) as error:
        parse_labels(io.StringIO(text))
    assert "private@example.com" not in str(error.value)


def test_empty_sample(sample: tuple[Settings, int]) -> None:
    report = evaluate(sample, "code_requisition,email,split\n", None)
    assert report["demands"] == report["data_issues"] == []
    assert report["splits"]["holdout"]["persisted"]["hit_rate_at_5"] is None
    assert "analyst_b" not in report["splits"]["holdout"]
    assert "## Per demand" in markdown(report)


def test_email_match_is_exact_except_case(sample: tuple[Settings, int]) -> None:
    report = evaluate(sample, "code_requisition,email,split\nPERF, alice@example.com ,\n", None)
    assert report["splits"]["holdout"]["persisted"]["excluded"] == 1
    assert report["splits"]["holdout"]["persisted"]["scored"] == 0
    assert {issue["code"] for issue in report["data_issues"]} == {
        "unresolved_email",
        "no_resolved_labels",
    }


def test_no_suitable_overrides_and_unresolved_second(sample: tuple[Settings, int]) -> None:
    labels = "code_requisition,email,split\nTUNE,,\nPERF,alice@example.com,\n"
    other = "code_requisition,email,split\nTUNE,unknown@example.com,\nPERF,unknown@example.com,\n"
    report = evaluate(sample, labels, other)
    assert report["splits"]["holdout"]["persisted"]["no_suitable_disagree"] == 1
    assert report["splits"]["holdout"]["after_overrides"]["no_suitable_agree"] == 1
    assert report["splits"]["holdout"]["analyst_b"]["excluded"] == 2
    assert report["splits"]["holdout"]["analyst_b"]["hit_rate_at_5"] is None


def test_five_profile_cap(sample: tuple[Settings, int]) -> None:
    settings, run = sample
    with database(settings) as db:
        source = db.execute(
            "SELECT * FROM match_results WHERE run_id=? AND demand_id="
            "(SELECT demand_id FROM demands WHERE code_requisition='TUNE') LIMIT 1",
            (run,),
        ).fetchone()
        for rank in range(2, 8):
            resource = seed_resource(db, f"Extra {rank}")
            db.execute(
                "INSERT INTO match_results(run_id,demand_id,demand_version_id,jd_req_id,"
                "resource_id,score,band,rank,shortlisted,grade_relation,location_assessment,"
                "matching_evidence,gaps_checks,eligibility_evidence) "
                "VALUES (?,?,?,?,?,60,'Avg',?,1,'exact','Aligned','E','G','E')",
                (
                    run,
                    source["demand_id"],
                    source["demand_version_id"],
                    source["jd_req_id"],
                    resource,
                    rank,
                ),
            )
    report = evaluate(sample, "code_requisition,email,split\nTUNE,carol@example.com,\n", None)
    row = report["demands"][0]
    assert row["variants"]["persisted"]["s_size"] == 5
    assert row["variants"]["persisted"]["hits"] == 0
    assert row["variants"]["after_overrides"]["s_size"] == 5
    assert row["variants"]["after_overrides"]["precision"] == 0.2


@pytest.mark.parametrize("invalid", [False, True])
def test_cli_writes_reports(sample: tuple[Settings, int], tmp_path: Path, invalid: bool) -> None:
    settings, run = sample
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    a.write_text(
        A if not invalid else A.replace("alice@example.com,", "alice@example.com,bad", 1),
        encoding="utf-8",
    )
    b.write_text(B, encoding="utf-8")
    output, md = tmp_path / "report.json", tmp_path / "report.md"
    app = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "scripts/measure_kpi01.py",
            "--run-id",
            str(run),
            "--labels",
            str(a),
            "--labels-b",
            str(b),
            "--out-json",
            str(output),
            "--out-md",
            str(md),
        ],
        cwd=app,
        env={**os.environ, "PFC_DATA_DIR": str(settings.data_dir), "PYTHONPATH": str(app)},
        capture_output=True,
        text=True,
        check=False,
    )
    if invalid:
        assert result.returncode == 1
        assert "line 2" in result.stderr
        assert "alice@example.com" not in result.stderr
        assert not output.exists() and not md.exists()
        return
    assert result.returncode == 0, result.stderr
    assert (
        json.loads(output.read_text(encoding="utf-8"))["splits"]["holdout"]["persisted"][
            "precision_at_5"
        ]
        == 0.375
    )
    assert "Primary split: **holdout**" in md.read_text(encoding="utf-8")
    for path in (output, md):
        assert "unknown@example.com" not in path.read_text(encoding="utf-8")
