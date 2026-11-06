"""SQLite service contracts: REQ-MAT-01..09, REQ-OUT-01/03, REQ-ELIG-08."""

import json
import math
import threading
import time
from datetime import UTC, datetime

import pytest

from pfc.config import Settings
from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.domain.config_loader import load_domain_config
from pfc.jobs.runner import JobCancelled, JobRunner
from pfc.services.matching import (
    MatchingPorts,
    add_override,
    analyze_jd,
    confirm_requirements,
    demand_matches,
    excluded_view,
    get_requirements,
    get_run,
    list_results,
    list_runs,
    result_detail,
    revoke_override,
    start_run,
)
from pfc.services.matching.runs import _inputs

NOW = datetime(2026, 11, 6, tzinfo=UTC)
STAMP = NOW.isoformat()


class FakeEmbedder:
    name = "deterministic-test"
    runtime = "onnx"
    file_sha256 = "a" * 64
    dim = 2
    pipeline_version = "test-v1"

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [[0.0, 1.0] if text.startswith("Skill:") else [1.0, 0.0] for text in texts]


class FakeLlm:
    model = "test-llm-v1"

    def __init__(self, *, fail=False, invalid=False):
        self.fail, self.invalid = fail, invalid

    def extract_jd(self, text):
        if self.fail:
            raise RuntimeError("offline")
        if self.invalid:
            return {"mandatory_skills": [{"text": " "}]}
        return {
            "mandatory_skills": [{"text": "BA", "evidence": "Business Analysis"}],
            "preferred_skills": [{"text": "SQL", "evidence": "SQL preferred"}],
            "experience": [{"text": "delivery experience", "years": 5}],
            "qualifications": ["degree"],
            "domain": ["insurance"],
            "location": "Manchester",
            "onsite_required": True,
        }


class FakeStore:
    def __init__(self):
        self.values = {}

    def get_vectors(self, owners):
        return [self.values.get(owner) for owner in owners]

    def upsert(self, owners, vectors):
        self.values.update(zip(owners, vectors, strict=True))


@pytest.fixture
def settings(tmp_path):
    result = Settings(data_dir=tmp_path, job_workers=1)
    migrate(result)
    with database(result) as db:
        db.executemany(
            "INSERT INTO grades(code,ordinal) VALUES (?,?)",
            [(code, i) for i, code in enumerate(load_domain_config().grades.codes, 1)],
        )
        db.execute(
            "INSERT INTO users"
            "(user_id,email,display_name,role_id,password_hash,created_at,updated_at)"
            " VALUES (1,'AlexBorder@capgemini.com','Analyst',2,'unused',?,?)",
            (STAMP, STAMP),
        )
    return result


@pytest.fixture
def runner(settings):
    result = JobRunner(settings)
    yield result
    result.shutdown()


def seed_demand(
    db,
    code="T1TEST",
    *,
    grade="C1",
    mandatory=("SQL",),
    preferred=(),
    jd="Required: SQL\nPreferred: Python",
    confirmed=True,
):
    cursor = db.execute(
        "INSERT INTO demands(code_requisition,id_format_ok,position_start_date,account_name,"
        "code_local_grade,demand_request_name,region,row_hash,created_at,updated_at) "
        "VALUES (?,1,'2026-11-16','Account','C1','Data role','Manchester','row',?,?)",
        (code, STAMP, STAMP),
    )
    demand_id = cursor.lastrowid
    db.execute("UPDATE demands SET code_local_grade=? WHERE demand_id=?", (grade, demand_id))
    cursor = db.execute(
        "INSERT INTO demand_versions"
        "(demand_id,version_no,jd_text_raw,jd_text_norm,jd_sha256,created_at)"
        " VALUES (?,1,?,?,?,?)",
        (demand_id, jd, jd, code, STAMP),
    )
    version_id = cursor.lastrowid
    db.execute("UPDATE demands SET current_version_id=? WHERE demand_id=?", (version_id, demand_id))
    if confirmed:
        confirm_requirements(
            db,
            demand_id,
            {
                "mandatory_skills": [{"text": term} for term in mandatory],
                "preferred_skills": [{"text": term} for term in preferred],
            },
            1,
            NOW,
        )
    return demand_id


def seed_resource(
    db,
    name="Person",
    *,
    skills="SQL, Python",
    grade="C1",
    status="Available",
    group="RG1",
    end=None,
    end_status="blank",
    location="Manchester",
    candidate=None,
    email=None,
):
    cursor = db.execute(
        "INSERT INTO resources(short_name,short_name_key,email_address,grade,location,bench_status,"
        "skills_raw,bench_start_date,bench_end_date,bench_end_date_parse_status,group_name,"
        "candidate_id,eligibility,row_hash,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,'2026-10-01',?,?,?,?,'excluded','row',?,?)",
        (
            name,
            name.casefold(),
            email,
            grade,
            location,
            status,
            skills,
            end,
            end_status,
            group,
            candidate,
            STAMP,
            STAMP,
        ),
    )
    return cursor.lastrowid


def seed_candidate(db):
    cursor = db.execute(
        "INSERT INTO candidates(internal_ref,identity_status,created_at,updated_at)"
        " VALUES ('PFC-C-TEST','incomplete',?,?)",
        (STAMP, STAMP),
    )
    return cursor.lastrowid


def seed_cv(db, resource_id, candidate_id, *, skills=("SQL", "Python", "AWS")):
    cursor = db.execute(
        "INSERT INTO resumes"
        "(candidate_id,source_path,file_name,file_sha256,mime_type,discovered_at)"
        " VALUES (?,'cv.pdf','cv.pdf','cv-file','application/pdf',?)",
        (candidate_id, STAMP),
    )
    cursor = db.execute(
        "INSERT INTO cv_versions"
        "(resume_id,candidate_id,version_no,text_sha256,text_norm,extracted_by,"
        "is_latest,extracted_at) VALUES (?,?,1,'cv-text','SQL Python AWS','test',1,?)",
        (cursor.lastrowid, candidate_id, STAMP),
    )
    cv_id = cursor.lastrowid
    for term in skills:
        db.execute(
            "INSERT INTO resource_skills(resource_id,cv_version_id,source,skill_raw,skill_norm,"
            "evidence) VALUES (?,?,'CV',?,?,?)",
            (resource_id, cv_id, term, term.casefold(), term),
        )
    return cv_id


def wait_done(runner, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = runner.get(job_id)
        if job.status in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.01)
    pytest.fail("Matching job did not finish")


def execute(settings, runner, ports=None, **kwargs):
    run_id, job_id = start_run(
        settings,
        runner,
        ports or MatchingPorts(FakeEmbedder()),
        user_id=1,
        clock=lambda: NOW,
        **kwargs,
    )
    job = wait_done(runner, job_id)
    assert job.status == "completed"
    return run_id


@pytest.mark.parametrize("llm", [FakeLlm(), FakeLlm(fail=True), FakeLlm(invalid=True), None])
def test_req_mat_02_analyze_revisions_confirmation(settings, llm):
    with database(settings) as db:
        demand_id = seed_demand(db, confirmed=False)
        first = analyze_jd(db, demand_id, MatchingPorts(None, llm), 1, NOW)
        assert first["status"] == "draft"
        if llm is not None and not llm.fail and not llm.invalid:
            assert first["extracted_by"] == "test-llm-v1"
            assert first["location_norm"] == "manchester" and first["onsite_required"] == 1
            assert len(first["items"]) == 5
            canonical = db.execute(
                "SELECT s.name FROM skills s JOIN jd_requirement_items i "
                "ON s.skill_id=i.canonical_skill_id WHERE i.jd_req_id=? AND i.position=0",
                (first["jd_req_id"],),
            ).fetchone()[0]
            assert canonical == "Business Analysis"
        else:
            assert first["extracted_by"] == "rule_based"
            assert [(i["kind"], i["text"]) for i in first["items"]] == [
                ("mandatory_skill", "SQL"),
                ("preferred_skill", "Python"),
            ]
        second = analyze_jd(db, demand_id, MatchingPorts(None, llm), 1, NOW)
        assert second["revision"] == 2
        assert (
            db.execute(
                "SELECT status FROM jd_requirements WHERE jd_req_id=?", (first["jd_req_id"],)
            ).fetchone()[0]
            == "superseded"
        )
        confirmed = confirm_requirements(db, demand_id, None, 1, NOW)
        assert confirmed["confirmed_by"] == 1 and confirmed["status"] == "confirmed"
        analyze_jd(db, demand_id, MatchingPorts(None), 1, NOW)
        assert (
            db.execute(
                "SELECT status FROM jd_requirements WHERE jd_req_id=?", (confirmed["jd_req_id"],)
            ).fetchone()[0]
            == "confirmed"
        )
        edited = confirm_requirements(
            db,
            demand_id,
            {"mandatory_skills": [{"text": "Python"}]},
            1,
            NOW,
        )
        assert edited["extracted_by"] == "manual" and edited["revision"] == 4
        assert (
            db.execute(
                "SELECT COUNT(*) FROM jd_requirements WHERE status='confirmed'",
            ).fetchone()[0]
            == 1
        )
        assert verify_chain(db)


def test_req_mat_02_empty_and_invalid_requirements(settings):
    with database(settings) as db:
        demand_id = seed_demand(db, confirmed=False, jd="")
        assert get_requirements(db, demand_id) is None
        with pytest.raises(ValueError, match="blank"):
            analyze_jd(db, demand_id, MatchingPorts(None), 1, NOW)
        with pytest.raises(ValueError, match="No draft"):
            confirm_requirements(db, demand_id, None, 1, NOW)
        with pytest.raises(ValueError):
            confirm_requirements(db, demand_id, {"mandatory_skills": [{"text": " "}]}, 1, NOW)
        with pytest.raises(ValueError, match="current JD"):
            analyze_jd(db, 999, MatchingPorts(None), 1, NOW)


def test_req_mat_01_item_kinds_follow_tdd_7_3(settings: Settings) -> None:
    with database(settings) as db:
        demand_id = seed_demand(db, confirmed=False)
        confirm_requirements(
            db,
            demand_id,
            {
                "mandatory_skills": [{"text": "SQL"}],
                "preferred_skills": [{"text": "Python"}],
                "experience": [{"text": "delivery experience"}],
                "qualifications": [{"text": "degree"}],
                "domain": [{"text": "insurance"}],
            },
            1,
            NOW,
        )
        demands, *_ = _inputs(db, "full", (), load_domain_config())
    assert {item.text: item.kind for item in demands[0].requirements} == {
        "SQL": "mandatory",
        "Python": "preferred",
        "delivery experience": "mandatory",
        "degree": "mandatory",
        "insurance": "preferred",
    }


def test_req_mat_03_run_persistence_uniqueness_cv_exclusions(settings, runner):
    with database(settings) as db:
        demand_id = seed_demand(db, mandatory=("SQL", "Python", "AWS"))
        weak_id = seed_demand(
            db, "T1WEAK", mandatory=("SQL", "Rust", "Go", "Kotlin", "Cobol", "Scala", "R")
        )
        missing_id = seed_demand(db, "T1DRAFT", confirmed=False)
        candidate = seed_candidate(db)
        resource = seed_resource(
            db, "CVPerson", skills="SQL", candidate=candidate, email="AlexBorder@capgemini.com"
        )
        cv_id = seed_cv(db, resource, candidate)
        seed_resource(db, "SameCandidate", candidate=candidate)
        for i in range(6):
            seed_resource(db, f"Person{i}", skills="SQL, Python, AWS")
        seed_resource(
            db,
            "Global",
            status="Selected",
            group="Not on Bench",
            end="2026-11-16",
            end_status="valid",
        )
        seed_resource(db, "GradeGap", grade="E1")
        seed_resource(db, "Unparseable", end_status="unparseable")
        inactive = seed_resource(db, "Inactive")
        db.execute("UPDATE resources SET lifecycle='archived' WHERE resource_id=?", (inactive,))
    run_id = execute(settings, runner)
    with database(settings) as db:
        run = get_run(db, run_id)
        assert run["status"] == "completed"
        assert run["template_id"] and run["embedding_model_id"]
        assert run["engine_version"] == "1.0.0"
        assert len(run["config_hash"]) == len(run["synonyms_version"]) == 64
        assert run["llm_model"] == "manual"
        assert run["demands_assessed"] == 2
        assert missing_id in json.loads(run["dq_summary_json"])["skipped_demand_ids"]
        matches = demand_matches(db, demand_id)["items"]
        assert len(matches) == 9
        shortlist = [r for r in matches if r["shortlisted"]]
        assert len(shortlist) == 5
        assert len({r["candidate_id"] or -r["resource_id"] for r in shortlist}) == 5
        cv_row = next(r for r in matches if r["resource_id"] == resource)
        assert cv_row["best_cv_version_id"] == cv_id
        detail = result_detail(db, cv_row["result_id"])
        assert {s["cv_version_id"] for s in detail["cv_scores"]} == {None, cv_id}
        assert len(detail["breakdown"]) == 6
        assert cv_row["person_email"] == "Al******er@capgemini.com"
        assert "AlexBorder@" not in json.dumps(detail)
        assert all(r["potential_person"] != "Inactive" for r in matches)
        weak = demand_matches(db, weak_id)
        assert weak["no_suitable_candidate"] and weak["weak_only"]
        assert weak["items"] and all(r["band"] == "Weak" for r in weak["items"])
        assert any(r["display_band"] == "Weak Match" for r in weak["items"])
        reasons = {(r["short_name"], r["reason_code"]) for r in excluded_view(db, run_id)}
        assert {
            ("Global", code) for code in ("STATUS_EXCLUDED", "NOT_ON_BENCH", "BENCH_END_SET")
        } <= reasons
        assert ("GradeGap", "GRADE_GAP_GT1") in reasons
        unparseable = next(r for r in matches if r["potential_person"] == "Unparseable")
        assert "unparseable" in unparseable["gaps_checks"]
        assert verify_chain(db)


def test_req_mat_08_scope_template_and_cache(settings, runner):
    embedder, store = FakeEmbedder(), FakeStore()
    ports = MatchingPorts(embedder, vector_store=store)
    with database(settings) as db:
        demand_id = seed_demand(db)
        skipped = seed_demand(db, "T1DRAFT", confirmed=False)
        seed_resource(db)
    run_id = execute(settings, runner, ports, scope="demands", demand_ids=[demand_id, skipped, 999])
    calls = embedder.calls
    again = execute(settings, runner, ports, scope="demands", demand_ids=[demand_id, skipped, 999])
    assert embedder.calls == calls and store.values
    with database(settings) as db:
        assert get_run(db, run_id)["config_hash"] == get_run(db, again)["config_hash"]
        assert get_run(db, run_id)["template_id"] == get_run(db, again)["template_id"]
        assert db.execute("SELECT COUNT(*) FROM embedding_models").fetchone()[0] == 1
        assert json.loads(get_run(db, run_id)["dq_summary_json"])["skipped_demand_ids"] == [
            skipped,
            999,
        ]
        template = db.execute("SELECT * FROM scoring_templates").fetchone()
        rules = json.loads(template["rules_json"])
        rules["status_score_mode"] = "tiered"
        db.execute("UPDATE scoring_templates SET rules_json=?", (json.dumps(rules),))
    tiered = execute(settings, runner, ports, scope="demands", demand_ids=[demand_id])
    with database(settings) as db:
        assert get_run(db, tiered)["config_hash"] != get_run(db, run_id)["config_hash"]


@pytest.mark.parametrize(
    "filters",
    [
        {"requisition": "T1TEST"},
        {"account": "Account"},
        {"demand_grade": "C1"},
        {"person_grade": "C1"},
        {"person_location": "Manchester"},
        {"band": "Potential"},
        {"location_assessment": "Aligned"},
        {"mandatory_skill": "SQL"},
        {"score_min": 80, "score_max": 90},
        {"status": "Available"},
        {"shortlisted_only": True},
    ],
)
def test_req_out_01_filters(settings, runner, filters):
    with database(settings) as db:
        seed_demand(db)
        seed_resource(db)
    run_id = execute(settings, runner)
    with database(settings) as db:
        result = list_results(db, None, filters)
        assert result["run_id"] == run_id and result["total"] == 1
        assert list_results(db, run_id, {"person_grade": "D2"})["total"] == 0
        assert list_results(db, run_id, limit=1, offset=1)["items"] == []
        assert list_runs(db)[0]["run_id"] == run_id
        with pytest.raises(ValueError):
            list_results(db, run_id, sort="score; DROP TABLE resources")
        with pytest.raises(ValueError):
            list_results(db, run_id, limit=0)


def test_req_mat_09_overrides_effective_values_and_audit(settings, runner, monkeypatch):
    with database(settings) as db:
        demand_id = seed_demand(db)
        seed_resource(db)
    run_id = execute(settings, runner)
    with database(settings) as db:
        row = list_results(db, run_id)["items"][0]
        result_id, score = row["result_id"], row["score"]
        for reason in ("", "  \n"):
            with pytest.raises(ValueError, match="reason"):
                add_override(db, result_id, "exclude", None, reason, 1, NOW)
        for action, band in (("invalid", None), ("adjust_band", None), ("adjust_band", "bad")):
            with pytest.raises(ValueError):
                add_override(db, result_id, action, band, "Review", 1, NOW)
        exclude = add_override(db, result_id, "exclude", None, "Not available", 1, NOW)
        assert not list_results(db, run_id, {"shortlisted_only": True})["items"]
        with pytest.raises(ValueError):
            revoke_override(db, exclude, " ", 1, NOW)
        revoke_override(db, exclude, "Available again", 1, NOW)
        unpin = add_override(db, result_id, "unpin", None, "Review", 1, NOW)
        assert not list_results(db, run_id)["items"][0]["effective_shortlisted"]
        add_override(db, result_id, "pin", None, "Prioritise", 1, NOW)
        band = add_override(db, result_id, "adjust_band", "Strong", "Validated", 1, NOW)
        adjusted = list_results(db, run_id, {"band": "Strong"})["items"][0]
        assert adjusted["score"] == score and adjusted["band"] == row["band"]
        assert adjusted["effective_band"] == "Strong" and adjusted["overridden"]
        assert adjusted["effective_shortlisted"]
        assert len(result_detail(db, result_id)["overrides"]) == 4
        assert verify_chain(db)
        details = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT details_json FROM audit_log WHERE action='match.override.revoke'",
            )
        ]
        assert details[0]["reason"] == "Available again"
        assert demand_matches(db, demand_id)["items"][0]["effective_band"] == "Strong"
        assert result_detail(db, 99999) is None and get_run(db, 99999) is None

        def fail(*args, **kwargs):
            raise RuntimeError("audit failed")

        monkeypatch.setattr("pfc.services.matching.common.append_audit", fail)
        with pytest.raises(RuntimeError):
            add_override(db, result_id, "exclude", None, "Review", 1, NOW)
        assert db.execute("SELECT COUNT(*) FROM overrides").fetchone()[0] == 4
        assert (
            db.execute("SELECT revoked_at FROM overrides WHERE override_id=?", (unpin,)).fetchone()[
                0
            ]
            is None
        )
        assert (
            db.execute("SELECT new_band FROM overrides WHERE override_id=?", (band,)).fetchone()[0]
            == "Strong"
        )


def test_req_mat_09_include_and_revoke_band(settings, runner):
    with database(settings) as db:
        seed_demand(db)
        seed_resource(db)
    run_id = execute(settings, runner)
    with database(settings) as db:
        result_id = list_results(db, run_id)["items"][0]["result_id"]
        add_override(db, result_id, "exclude", None, "Review", 1, NOW)
        add_override(db, result_id, "include", None, "Reviewed", 1, NOW)
        band = add_override(db, result_id, "adjust_band", "Weak", "Review", 1, NOW)
        row = list_results(db, run_id)["items"][0]
        assert row["effective_shortlisted"] and not row["effective_excluded"]
        assert row["display_band"] == "Weak Match"
        revoke_override(db, band, "Revert", 1, NOW)
        assert list_results(db, run_id)["items"][0]["effective_band"] == "Potential"
        assert verify_chain(db)


@pytest.mark.parametrize("kind", ["raises", "wrong_count", "wrong_dim", "nan", "zero", "cancelled"])
def test_req_mat_08_failed_or_cancelled_jobs(settings, runner, kind):
    class Broken(FakeEmbedder):
        def embed(self, texts):
            if kind == "raises":
                raise RuntimeError("Embedding failed")
            if kind == "cancelled":
                raise JobCancelled()
            vector = {"wrong_dim": [1.0], "nan": [math.nan, 1.0], "zero": [0.0, 0.0]}.get(kind)
            return [] if kind == "wrong_count" else [vector for _ in texts]

    with database(settings) as db:
        seed_demand(db)
        seed_resource(db)
    run_id, job_id = start_run(
        settings,
        runner,
        MatchingPorts(Broken()),
        user_id=1,
        clock=lambda: NOW,
    )
    status = "cancelled" if kind == "cancelled" else "failed"
    assert wait_done(runner, job_id).status == status
    with database(settings) as db:
        assert get_run(db, run_id)["status"] == status
        assert list_results(db, run_id)["items"] == []


def test_req_mat_08_empty_run_and_missing_embedder(settings, runner):
    with pytest.raises(ValueError, match="embedder"):
        start_run(settings, runner, MatchingPorts(None))
    with pytest.raises(ValueError, match="Scope"):
        start_run(settings, runner, MatchingPorts(FakeEmbedder()), scope="demands")
    with database(settings) as db:
        assert list_results(db)["items"] == [] and list_runs(db) == []
    run_id = execute(settings, runner)
    with database(settings) as db:
        assert get_run(db, run_id)["demands_assessed"] == 0
        assert demand_matches(db, 999)["items"] == []


def test_req_mat_08_real_job_cancellation(settings, runner):
    entered, release = threading.Event(), threading.Event()

    class Blocking(FakeEmbedder):
        def embed(self, texts):
            entered.set()
            assert release.wait(5)
            return super().embed(texts)

    with database(settings) as db:
        seed_demand(db)
        seed_resource(db)
    run_id, job_id = start_run(
        settings,
        runner,
        MatchingPorts(Blocking()),
        user_id=1,
        clock=lambda: NOW,
    )
    try:
        assert entered.wait(5)
        runner.cancel(job_id)
        release.set()
        assert wait_done(runner, job_id).status == "cancelled"
        with database(settings) as db:
            assert get_run(db, run_id)["status"] == "cancelled"
    finally:
        release.set()


def test_req_mat_08_submission_failure(settings, runner):
    runner.shutdown()
    with pytest.raises(RuntimeError, match="closed"):
        start_run(settings, runner, MatchingPorts(FakeEmbedder()), clock=lambda: NOW)
    with database(settings) as db:
        assert list_runs(db)[0]["status"] == "failed"


def test_req_mat_08_queued_cancellation_reconciles(settings, runner):
    entered, release = threading.Event(), threading.Event()

    def blocker(context):
        entered.set()
        assert release.wait(5)

    blocker_id = runner.submit("blocker", blocker)
    assert entered.wait(5)
    try:
        run_id, job_id = start_run(
            settings,
            runner,
            MatchingPorts(FakeEmbedder()),
            clock=lambda: NOW,
        )
        runner.cancel(job_id)
        with database(settings) as db:
            assert (
                db.execute(
                    "SELECT status FROM match_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
                == "queued"
            )
            row = get_run(db, run_id)
            assert row["status"] == "cancelled" and row["finished_at"]
            assert json.loads(row["scope_json"])["job_id"] == job_id
        with database(settings) as db:
            assert list_runs(db)[0]["status"] == "cancelled"
            assert list_results(db)["run_id"] is None
    finally:
        release.set()
        wait_done(runner, blocker_id)


def test_req_mat_08_restart_interruption_reconciles(settings):
    # Persist the state left by a process exit, without a live worker racing recovery.
    with database(settings) as db:
        template = load_domain_config().template.model_dump(mode="json")
        db.execute(
            "INSERT INTO scoring_templates(template_id,name,version,weights_json,rules_json,"
            "status,created_at) VALUES (1,'default',1,?,?,'active',?)",
            (json.dumps(template["weights"]), json.dumps(template), STAMP),
        )
        db.execute(
            "INSERT INTO embedding_models VALUES (1,'test','onnx','hash',2,1,'v1',1)",
        )
        db.execute(
            "INSERT INTO jobs(job_id,kind,status,created_at) VALUES (1,'match_run','running',?)",
            (STAMP,),
        )
        db.execute(
            "INSERT INTO match_runs(scope,scope_json,template_id,embedding_model_id,engine_version,"
            "synonyms_version,config_hash,status) "
            "VALUES ('full','{\"job_id\":1}',1,1,'test','syn','cfg','running')",
        )
    restarted = JobRunner(settings)
    try:
        with database(settings) as db:
            assert db.execute("SELECT message FROM jobs WHERE job_id=1").fetchone()[0] == (
                "interrupted by restart"
            )
            row = list_runs(db)[0]
            assert row["status"] == "failed" and row["finished_at"]
            assert get_run(db, row["run_id"])["status"] == "failed"
    finally:
        restarted.shutdown()


def test_req_mat_08_job_freezes_inputs_before_dispatch(settings, runner):
    release, started = threading.Event(), threading.Event()

    def blocker(context):
        started.set()
        assert release.wait(5)

    blocker_id = runner.submit("blocker", blocker)
    assert started.wait(5)
    try:
        with database(settings) as db:
            demand_id = seed_demand(db)
            resource_id = seed_resource(db)
        run_id, job_id = start_run(
            settings,
            runner,
            MatchingPorts(FakeEmbedder()),
            user_id=1,
            clock=lambda: NOW,
        )
        with database(settings) as db:
            db.execute("UPDATE resources SET skills_raw='' WHERE resource_id=?", (resource_id,))
        release.set()
        assert wait_done(runner, blocker_id).status == "completed"
        assert wait_done(runner, job_id).status == "completed"
        with database(settings) as db:
            assert len(demand_matches(db, demand_id)["items"]) == 1
            assert get_run(db, run_id)["pairs_scored"] == 1
    finally:
        release.set()
