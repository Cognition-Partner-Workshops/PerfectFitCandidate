"""REQ-IN-24 / FR-ING-52: read-only, masked CV version comparison."""

import json
import sqlite3

import pytest
from test_matching_api import Api
from test_matching_api import api as api
from test_matching_service import (
    NOW,
    STAMP,
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
from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.jobs.runner import JobRunner
from pfc.security.problems import Problem
from pfc.services.candidates import cv_compare
from pfc.services.matching import MatchingPorts
from pfc.services.matching.cv_bases import load_cv_bases, load_resources
from pfc.services.occ.domain import occ_domain_config

OLD_TEXT = (
    "Data Analyst\nSkills: SQL, Python\nWorked at Bank A\nContact alice@example.com +44 7700 900123"
)
NEW_TEXT = (
    "Data Analyst\nSkills: SQL, Python, AWS\nWorked at Bank A\nWorked at Bank B\n"
    "Master of Computer Science\nContact alice@example.com +44 7700 900123"
)
RAW_PII = ("alice@example.com", "7700 900123", "900123")


def seed_versions(db: sqlite3.Connection) -> tuple[int, int, int, int]:
    candidate = seed_candidate(db)
    resource = seed_resource(db, candidate=candidate)
    old = seed_cv(db, resource, candidate, skills=("Python",))
    db.execute(
        "UPDATE cv_versions SET text_norm=?,is_latest=0 WHERE cv_version_id=?", (OLD_TEXT, old)
    )
    resume = db.execute(
        "INSERT INTO resumes(candidate_id,source_path,file_name,file_sha256,mime_type,"
        "discovered_at) VALUES (?,'cv2.pdf','cv2.pdf','cv-file-2','application/pdf',?)",
        (candidate, STAMP),
    ).lastrowid
    new = db.execute(
        "INSERT INTO cv_versions(resume_id,candidate_id,version_no,text_sha256,text_norm,"
        "extracted_by,is_latest,extracted_at) VALUES (?,?,2,'cv-text-2',?,'test',1,?)",
        (resume, candidate, NEW_TEXT, STAMP),
    ).lastrowid
    assert new is not None
    for term in ("SQL", "Python", "AWS"):
        db.execute(
            "INSERT INTO resource_skills(resource_id,cv_version_id,source,skill_raw,skill_norm,"
            "evidence) VALUES (?,?,'CV',?,?,?)",
            (resource, new, term, term.casefold(), f"Skills: {term}"),
        )
    return candidate, resource, old, new


def seeded(settings: Settings, runner: JobRunner) -> tuple[int, int, int]:
    with database(settings) as db:
        seed_demand(db, mandatory=("SQL", "AWS"))
        candidate, _, old, new = seed_versions(db)
    execute(settings, runner)
    return candidate, old, new


def test_compare_facts_text_and_stored_scores_are_masked(
    settings: Settings, runner: JobRunner
) -> None:
    candidate, old, new = seeded(settings, runner)
    with database(settings) as db:
        before = db.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    result = cv_compare.compare(settings, candidate, old, new)
    assert (result["left"]["version_no"], result["right"]["version_no"]) == (1, 2)
    assert result["right"]["is_latest"] and not result["left"]["is_latest"]
    skills = {row["key"]: row["status"] for row in result["facts"]["skills"]}
    assert skills["aws"] == "added"
    assert skills["python"] == "unchanged"
    aws = next(row for row in result["facts"]["skills"] if row["key"] == "aws")
    assert aws["left"] is None and aws["right"]["evidence"]
    summary = {row["key"]: row for row in result["facts"]["summary"]}
    assert summary["degree"]["status"] == "added"
    assert summary["degree"]["right"]["value"].startswith("Master")
    diff = result["text_diff"]
    assert diff["added"] >= 2 and diff["removed"] >= 1
    assert {row["op"] for row in diff["rows"]} >= {"equal", "added"}
    stored = result["stored_scores"]
    assert len(stored) == 1 and stored[0]["code_requisition"] == "T1TEST"
    assert stored[0]["best_cv_version_id"] == new
    assert stored[0]["right"] is not None and stored[0]["left"] is None
    dumped = json.dumps(result)
    assert not any(value in dumped for value in RAW_PII)
    with database(settings) as db:
        assert db.execute("SELECT count(*) FROM audit_log").fetchone()[0] == before


@pytest.mark.parametrize("shared_with_active", [False, True])
def test_archived_cv_comparison_keeps_stored_skills_outside_active_matching(
    settings: Settings, shared_with_active: bool
) -> None:
    with database(settings) as db:
        candidate, resource, old, new = seed_versions(db)
        db.execute(
            "UPDATE cv_versions SET text_norm='Plain biography.' WHERE candidate_id=?", (candidate,)
        )
        for version, terms in (
            (old, ("StoredOnly", "LegacyOnly")),
            (new, ("StoredOnly", "CurrentOnly")),
        ):
            for term in terms:
                db.execute(
                    "INSERT INTO resource_skills(resource_id,cv_version_id,source,skill_raw,"
                    "skill_norm,evidence) VALUES (?,?,'CV',?,?,?)",
                    (resource, version, term, term.casefold(), f"Recorded: {term}"),
                )
        db.execute("UPDATE resources SET lifecycle='archived' WHERE resource_id=?", (resource,))
        selected = [resource]
        if shared_with_active:
            selected.append(seed_resource(db, "Active counterpart", candidate=candidate))
        assert load_resources(db, [resource]) == []
        bases = load_cv_bases(db, occ_domain_config(db), selected)
        assert not any(
            skill.raw in {"StoredOnly", "LegacyOnly", "CurrentOnly"}
            for candidate_bases in bases.values()
            for basis in candidate_bases
            for skill in basis.skills
        )
        if not shared_with_active:
            assert bases == {}
    comparison = cv_compare.compare(settings, candidate, old, new)
    skills = {item["key"]: item for item in comparison["facts"]["skills"]}
    assert skills["storedonly"]["status"] == "unchanged"
    assert skills["legacyonly"]["status"] == "removed"
    assert skills["currentonly"]["status"] == "added"
    assert skills["storedonly"]["left"]["evidence"] == "Recorded: StoredOnly"
    assert skills["storedonly"]["right"]["evidence"] == "Recorded: StoredOnly"


def test_compare_rejects_foreign_or_same_versions(settings: Settings) -> None:
    with database(settings) as db:
        candidate, _, old, new = seed_versions(db)
        other = db.execute(
            "INSERT INTO candidates(internal_ref,identity_status,created_at,updated_at)"
            " VALUES ('PFC-C-OTHER','incomplete',?,?)",
            (STAMP, STAMP),
        ).lastrowid
        assert other is not None
    with pytest.raises(Problem) as same:
        cv_compare.compare(settings, candidate, old, old)
    assert same.value.code == "CV_COMPARE_SAME_VERSION"
    with pytest.raises(Problem) as foreign:
        cv_compare.compare(settings, other, old, new)
    assert foreign.value.code == "CV_VERSION_NOT_FOUND"
    with pytest.raises(Problem) as missing:
        cv_compare.compare(settings, 9999, old, new)
    assert missing.value.code == "CANDIDATE_NOT_FOUND"


def test_what_if_scoring_is_audited_and_never_changes_results(
    settings: Settings, runner: JobRunner
) -> None:
    candidate, old, new = seeded(settings, runner)
    with database(settings) as db:
        results = [tuple(row) for row in db.execute("SELECT * FROM match_results")]
        cv_scores = [tuple(row) for row in db.execute("SELECT * FROM match_cv_scores")]
    comparison_id, job_id = cv_compare.start_scoring(
        settings,
        runner,
        MatchingPorts(FakeEmbedder()),
        candidate,
        old,
        new,
        user_id=1,
        clock=lambda: NOW,
    )
    assert wait_done(runner, job_id).status == "completed"
    latest = cv_compare.latest_scoring(settings, candidate, old, new)
    assert latest is not None and latest["comparison_id"] == comparison_id
    assert latest["status"] == "completed" and latest["job_id"] == job_id
    rows = latest["result"]["demands"]
    assert len(rows) == 1
    row = rows[0]
    assert row["as_of_date"] == NOW.date().isoformat()
    assert row["right"]["score"] > row["left"]["score"]
    assert row["delta"] == round(row["right"]["score"] - row["left"]["score"], 2)
    assert row["right"]["score"] == pytest.approx(row["stored_score"])
    assert {c["component"] for c in row["left"]["components"]} >= {"skills", "grade"}
    assert row["left"]["mandatory_missing"] > row["right"]["mandatory_missing"]
    assert not any(value in json.dumps(latest) for value in RAW_PII)
    assert cv_compare.latest_scoring(settings, candidate, new, old) is None
    with database(settings) as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM match_results")] == results
        assert [tuple(r) for r in db.execute("SELECT * FROM match_cv_scores")] == cv_scores
        entry = db.execute(
            "SELECT * FROM audit_log WHERE action='cv.compare_score' ORDER BY audit_id DESC"
        ).fetchone()
        assert entry["entity_id"] == str(comparison_id)
        assert json.loads(entry["details_json"])["left_cv_version_id"] == old
        assert verify_chain(db)
        job = db.execute("SELECT kind,params_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    assert job["kind"] == "cv_compare"
    assert json.loads(job["params_json"])["comparison_id"] == comparison_id


def test_unscorable_demand_is_reported_not_fatal(settings: Settings, runner: JobRunner) -> None:
    candidate, old, new = seeded(settings, runner)
    with database(settings) as db:
        db.execute("UPDATE demands SET code_local_grade=NULL")
    _, job_id = cv_compare.start_scoring(
        settings, runner, MatchingPorts(FakeEmbedder()), candidate, old, new, clock=lambda: NOW
    )
    assert wait_done(runner, job_id).status == "completed"
    latest = cv_compare.latest_scoring(settings, candidate, old, new)
    assert latest is not None and latest["status"] == "completed"
    row = latest["result"]["demands"][0]
    assert row["delta"] is None
    for side in ("left", "right"):
        assert row[side]["excluded"] is True
        assert row[side]["reasons"][0]["code"] == "DEMAND_NOT_SCORABLE"


@pytest.mark.parametrize(
    ("job_status", "expected"), [("failed", "failed"), ("cancelled", "cancelled")]
)
def test_interrupted_job_ends_a_pending_comparison(
    settings: Settings, runner: JobRunner, job_status: str, expected: str
) -> None:
    candidate, old, new = seeded(settings, runner)
    _, job_id = cv_compare.start_scoring(
        settings, runner, MatchingPorts(FakeEmbedder()), candidate, old, new, clock=lambda: NOW
    )
    wait_done(runner, job_id)
    with database(settings) as db:
        db.execute("UPDATE cv_comparisons SET status='queued', result_json=NULL")
        db.execute("UPDATE jobs SET status=? WHERE job_id=?", (job_status, job_id))
    latest = cv_compare.latest_scoring(settings, candidate, old, new)
    assert latest is not None and latest["status"] == expected


def test_removed_extraction_is_compared_but_never_scored(
    settings: Settings, runner: JobRunner
) -> None:
    with database(settings) as db:
        candidate, _, old, new = seed_versions(db)
        db.execute(
            "UPDATE cv_versions SET extraction_removed_at=? WHERE cv_version_id=?", (STAMP, old)
        )
    result = cv_compare.compare(settings, candidate, old, new)
    assert result["left"]["extraction_removed"]
    assert all(row["left"] is None for row in result["facts"]["skills"])
    assert all(row["left"] is None for row in result["text_diff"]["rows"] if row["op"] != "skip")
    with pytest.raises(Problem) as removed:
        cv_compare.start_scoring(
            settings, runner, MatchingPorts(FakeEmbedder()), candidate, old, new, user_id=1
        )
    assert removed.value.code == "CV_EXTRACTION_REMOVED"


def test_text_diff_collapses_unchanged_runs_and_masks() -> None:
    same = [f"line {n}" for n in range(20)]
    left = "\n".join([*same, "call +44 7700 900123"])
    right = "\n".join([*same, "mail bob@example.com"])
    diff = cv_compare.text_diff(left, right)
    assert diff["unchanged"] == 20 and diff["added"] == 1 and diff["removed"] == 1
    assert diff["rows"][0]["op"] == "skip" and diff["rows"][0]["count"] == 18
    changed = diff["rows"][-1]
    assert changed["op"] == "changed"
    assert "900123" not in changed["left"]["text"] and "bob@" not in changed["right"]["text"]
    assert cv_compare.text_diff("", "")["rows"] == []


def test_api_compare_and_score(api: Api) -> None:
    client, headers = api
    app_settings = client.app.state.settings  # type: ignore[attr-defined]
    with database(app_settings) as db:
        seed_demand(db, mandatory=("SQL", "AWS"))
        candidate, _, old, new = seed_versions(db)
    base = f"/api/v1/candidates/{candidate}"
    response = client.get(f"{base}/cv-versions/compare", params={"left": old, "right": new})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"candidateId", "left", "right", "facts", "textDiff", "storedScores"}
    assert body["left"]["cvVersionId"] == old and body["textDiff"]["rows"]
    assert not any(value in response.text for value in RAW_PII)
    assert (
        client.get(f"{base}/cv-comparisons/latest", params={"left": old, "right": new}).json()
        is None
    )
    assert (
        client.get(f"{base}/cv-versions/compare", params={"left": old, "right": old}).status_code
        == 422
    )
    assert client.get(f"/api/v1/candidates/{candidate}/cv-versions/compare").status_code == 422
    start = client.post(
        f"{base}/cv-comparisons",
        json={"leftCvVersionId": old, "rightCvVersionId": new},
        headers=headers,
    )
    assert start.status_code == 202, start.text
    job_id = start.json()["jobId"]
    assert wait_done(client.app.state.job_runner, job_id).status == "completed"  # type: ignore[attr-defined]
    latest = client.get(f"{base}/cv-comparisons/latest", params={"left": old, "right": new})
    assert latest.status_code == 200
    assert latest.json()["status"] == "completed"
    assert latest.json()["result"]["demands"] == []
    jobs = client.get("/api/v1/jobs").json()
    card = next(job for job in jobs if job["jobId"] == job_id)
    assert card["label"] == f"CV comparison scoring CV-{old} vs CV-{new}"
    assert card["link"] == f"/#/candidates/{candidate}?compare={old},{new}"
