"""Seeded equivalence of incremental and exhaustive persisted scoring."""

import json
import random
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from docx import Document
from test_matching_api import Api, finish
from test_matching_api import api as api
from test_matching_service import (
    NOW,
    FakeEmbedder,
    execute,
    seed_candidate,
    seed_cv,
    seed_demand,
    seed_resource,
    wait_done,
)
from test_matching_service import runner as runner
from test_matching_service import settings as settings

from pfc.config import Settings
from pfc.db.connection import database
from pfc.domain.scoring import engine
from pfc.jobs.runner import JobCancelled, JobContext, JobRunner
from pfc.services.ingestion import cv_sync
from pfc.services.matching import (
    MatchingPorts,
    add_override,
    confirm_requirements,
    list_results,
    runs,
    start_run,
)
from pfc.services.occ import synonyms


def persisted(db: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    rows = []
    for row in db.execute(
        "SELECT * FROM match_results WHERE run_id=? ORDER BY demand_id,resource_id", (run_id,)
    ):
        rows.append({k: row[k] for k in row.keys() if k not in {"result_id", "run_id"}})
    output: dict[str, Any] = {"results": rows}
    for table, fields in (
        ("match_cv_scores", "cv_version_id,score,band"),
        ("score_breakdowns", "component,points,max_points,inputs_json"),
    ):
        records = [
            dict(r)
            for r in db.execute(
                f"SELECT r.demand_id,r.resource_id,{','.join('s.' + f for f in fields.split(','))} "
                f"FROM {table} s JOIN match_results r USING(result_id) WHERE r.run_id=?",
                (run_id,),
            )
        ]
        output[table] = sorted(records, key=lambda r: json.dumps(r, sort_keys=True))
    output["exclusions"] = sorted(
        [
            dict(r)
            for r in db.execute(
                "SELECT resource_id,demand_id,reason_code,detail FROM eligibility_exclusions "
                "WHERE run_id=?",
                (run_id,),
            )
        ],
        key=lambda r: json.dumps(r, sort_keys=True),
    )
    row = db.execute("SELECT * FROM match_runs WHERE run_id=?", (run_id,)).fetchone()
    output["summary"] = {
        key: row[key]
        for key in (
            "demands_assessed",
            "pairs_scored",
            "roles_without_candidate",
            "dq_summary_json",
        )
    }
    return output


@pytest.mark.parametrize("seed", [19401, 63217, 87121])
@pytest.mark.parametrize(
    "scenario",
    [
        "clean",
        "skills",
        "grade",
        "status",
        "cv_sync",
        "jd",
        "new_resource",
        "displace",
        "duplicate_candidate",
        "synonym",
        "resource_removed",
        "demand_removed",
        "new_demand",
        "mixed",
        "timestamps",
    ],
)
def test_incremental_equals_full(
    settings: Settings,
    runner: JobRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed: int,
    scenario: str,
) -> None:
    rng = random.Random(seed)
    with database(settings) as db:
        candidate = seed_candidate(db)
        demands = [
            seed_demand(
                db,
                f"T{i:03}",
                grade="C1" if i == 0 else rng.choice(["C1", "C2", "D1", None]),
                mandatory=("SQL",) if i == 0 else (rng.choice(["SQL", "Python", "AWS"]),),
            )
            for i in range(7)
        ]
        resources = [
            seed_resource(
                db,
                f"Person {i:03}",
                grade="C1" if i < 10 else rng.choice(["C1", "C2", "D1", None]),
                skills="SQL, Python" if i < 10 else rng.choice(["SQL, AWS", "Python, Java", "SQL"]),
                status="Available" if i < 10 else rng.choice(["Available", "Selected", "Unknown"]),
                candidate=candidate if i < 2 else None,
                email="candidate@example.com" if i < 2 else f"person{i}@example.com",
            )
            for i in range(20)
        ]
        challenger = seed_resource(db, "ZZ Challenger", skills="Java, AWS")
        seed_cv(db, resources[0], candidate)
        db.execute(
            "INSERT INTO candidate_emails(candidate_id,email,email_key,source) "
            "VALUES (?,'candidate@example.com','candidate@example.com','manual')",
            (candidate,),
        )
    base = execute(settings, runner)
    with database(settings) as db:
        before = persisted(db, base)
        initial_shortlist = {
            r[0]
            for r in db.execute(
                "SELECT resource_id FROM match_results WHERE run_id=? AND demand_id=? "
                "AND shortlisted=1",
                (base, demands[0]),
            )
        }
        if scenario in {"skills", "mixed"}:
            db.execute(
                "UPDATE resources SET skills_raw='Python, Java' WHERE resource_id=?",
                (resources[2],),
            )
        if scenario == "timestamps":
            db.execute("UPDATE resources SET updated_at='2030-01-01'")
            db.execute("UPDATE demands SET updated_at='2030-01-01'")
        if scenario == "grade":
            db.execute("UPDATE resources SET grade='A3' WHERE resource_id=?", (resources[3],))
        if scenario == "status":
            db.execute(
                "UPDATE resources SET bench_status='Selected' WHERE resource_id=?", (resources[4],)
            )
        if scenario in {"jd", "mixed"}:
            confirm_requirements(
                db,
                demands[1],
                {
                    "mandatory_skills": [{"text": "Python"}],
                    "preferred_skills": [{"text": "SQL"}],
                },
                1,
                NOW,
            )
        if scenario == "new_resource":
            seed_resource(db, "New Person", skills="SQL, Python, AWS")
        if scenario == "new_demand":
            seed_demand(db, "TNEW", mandatory=("Java",))
        if scenario == "displace":
            db.execute(
                "UPDATE resources SET skills_raw='SQL, Python',short_name='AAA Challenger' "
                "WHERE resource_id=?",
                (challenger,),
            )
        if scenario == "duplicate_candidate":
            db.execute(
                "UPDATE resources SET short_name='AAA Duplicate' WHERE resource_id=?",
                (resources[1],),
            )
        if scenario == "synonym":
            synonyms.save(
                db,
                1,
                {
                    "canonical": "SQL",
                    "term": "query alias",
                    "credit": 1,
                    "relation": "abbreviation",
                    "status": "approved",
                },
            )
        if scenario == "resource_removed":
            db.execute(
                "UPDATE resources SET lifecycle='archived' WHERE resource_id=?", (resources[-1],)
            )
        if scenario == "demand_removed":
            db.execute("UPDATE demands SET lifecycle='closed' WHERE demand_id=?", (demands[1],))
        result_id = db.execute(
            "SELECT result_id FROM match_results WHERE run_id=? AND demand_id=? "
            "ORDER BY resource_id LIMIT 1",
            (base, demands[0]),
        ).fetchone()[0]
        add_override(db, result_id, "exclude", None, "Keep existing human decision", 1, NOW)
    if scenario == "cv_sync":
        root = tmp_path / "resumes"
        root.mkdir()
        doc = Document()
        doc.add_paragraph("candidate@example.com SQL Python Java AWS Azure")
        doc.save(root / "candidate@example.com.docx")
        cv_sync.sync(JobContext(runner, 0, threading.Event()), settings, root, resources[:2], 1)
        with database(settings) as db:
            assert db.execute("SELECT count(*) FROM cv_versions").fetchone()[0] > 1
    calls = []
    original = engine.score_basis

    def counted(demand, resource, *args, **kwargs):
        calls.append((demand.code_requisition, resource.resource_id))
        return original(demand, resource, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(engine, "score_basis", counted)
        incremental = execute(settings, runner, incremental=True)
    full = execute(settings, runner)
    with database(settings) as db:
        assert persisted(db, incremental) == persisted(db, full)
        assert persisted(db, base) == before
        run = db.execute("SELECT * FROM match_runs WHERE run_id=?", (incremental,)).fetchone()
        reference = db.execute("SELECT * FROM match_runs WHERE run_id=?", (full,)).fetchone()
        assert run["config_key"] == reference["config_key"]
        assert run["config_hash"] == reference["config_hash"]
        provenance = json.loads(run["scope_json"])
        expected_reason = {
            "synonym": "config_changed",
            "resource_removed": "resource_removed",
            "demand_removed": "demand_removed",
        }.get(scenario, "inputs_compared")
        assert provenance["reason"] == expected_reason
        if expected_reason == "inputs_compared":
            assert provenance["mode"] == "incremental"
            assert provenance["base_run_id"] == base
            old = json.loads(
                db.execute(
                    "SELECT input_fingerprints_json FROM match_runs WHERE run_id=?", (base,)
                ).fetchone()[0]
            )
            new = json.loads(run["input_fingerprints_json"])
            dirty = {
                kind: {k for k, v in new[kind].items() if old[kind].get(k) != v}
                for kind in ("demands", "resources")
            }
            codes = {
                str(r[0]): r[1]
                for r in db.execute("SELECT demand_id,code_requisition FROM demands")
            }
            dirty_codes = {codes[k] for k in dirty["demands"]}
            assert all(d in dirty_codes or r in dirty["resources"] for d, r in calls)
            assert provenance["dirty_demands"] == len(dirty["demands"])
            assert provenance["dirty_resources"] == len(dirty["resources"])
            if scenario in {"clean", "timestamps"}:
                assert not calls
            if scenario == "cv_sync":
                assert dirty["resources"] == {str(r) for r in resources[:2]}
        else:
            assert provenance["mode"] == "full"
        if scenario == "displace":
            new_shortlist = {
                r[0]
                for r in db.execute(
                    "SELECT resource_id FROM match_results WHERE run_id=? AND demand_id=? "
                    "AND shortlisted=1",
                    (incremental, demands[0]),
                )
            }
            assert challenger not in initial_shortlist and challenger in new_shortlist
            assert initial_shortlist - new_shortlist
        left = list_results(db, run_id=incremental)
        right = list_results(db, run_id=full)
        fields = (
            "resource_id",
            "effective_band",
            "effective_excluded",
            "overridden",
            "effective_shortlisted",
            "effective_email_preselected",
        )
        assert [tuple(r[k] for k in fields) for r in left["items"]] == [
            tuple(r[k] for k in fields) for r in right["items"]
        ]
        assert not any(r["overridden"] for r in left["items"])


@pytest.mark.parametrize("error,status", [(ValueError, "failed"), (JobCancelled, "cancelled")])
def test_incremental_persistence_rolls_back(
    settings: Settings,
    runner: JobRunner,
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
    status: str,
) -> None:
    with database(settings) as db:
        seed_demand(db)
        seed_resource(db)
    base = execute(settings, runner)
    with database(settings) as db:
        before = persisted(db, base)

    def interrupt(*args, **kwargs):
        raise error("stop after carry-forward")

    monkeypatch.setattr(runs, "rerank", interrupt)
    run_id, job_id = start_run(
        settings,
        runner,
        MatchingPorts(FakeEmbedder()),
        incremental=True,
        user_id=1,
        clock=lambda: NOW,
    )
    assert wait_done(runner, job_id).status == status
    with database(settings) as db:
        assert persisted(db, base) == before
        assert (
            db.execute("SELECT count(*) FROM match_results WHERE run_id=?", (run_id,)).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT count(*) FROM eligibility_exclusions WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == 0
        )


def test_incremental_api_default_explicit_and_empty_runs(api: Api) -> None:
    client, headers = api
    ids = []
    for body, mode in [
        ({}, "full"),
        ({"incremental": True}, "incremental"),
        ({"incremental": False}, "full"),
    ]:
        response = client.post("/api/v1/match-runs", headers=headers, json=body)
        finish(client, response)
        run_id = response.json()["runId"]
        ids.append(run_id)
        with database(client.app.state.settings) as db:
            row = db.execute("SELECT * FROM match_runs WHERE run_id=?", (run_id,)).fetchone()
            assert json.loads(row["scope_json"])["mode"] == mode
            assert len(row["config_key"]) == 64
            assert json.loads(row["input_fingerprints_json"]) == {
                "version": 1,
                "demands": {},
                "resources": {},
            }
            assert row["pairs_scored"] == 0
    invalid = client.post("/api/v1/match-runs", headers=headers, json={"incremental": {}})
    assert invalid.status_code == 422
    with database(client.app.state.settings) as db:
        assert db.execute("SELECT count(*) FROM match_runs").fetchone()[0] == len(ids)
        assert len({r[0] for r in db.execute("SELECT config_key FROM match_runs")}) == 1
