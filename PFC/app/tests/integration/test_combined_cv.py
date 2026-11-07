"""Combined per-resource skill inputs, persisted shape and shared-CV cache isolation."""

import json
from pathlib import Path

import pytest
from cv_matching_gate import FIXTURES, capture
from test_matching_service import (
    FakeEmbedder,
    execute,
    seed_candidate,
    seed_cv,
    seed_demand,
    seed_resource,
)
from test_matching_service import runner as runner
from test_matching_service import settings as settings

from pfc.api.schemas.candidates import IdentityResolution
from pfc.config import Settings
from pfc.db.connection import database
from pfc.domain.config_loader import load_domain_config
from pfc.domain.normalise import SynonymRow, SynonymTable
from pfc.domain.scoring import build_combined_basis
from pfc.jobs.runner import JobRunner
from pfc.services.ingestion.cv_sync import _replace_skills
from pfc.services.ingestion.identity import resolve
from pfc.services.matching import MatchingPorts, result_detail
from pfc.services.matching.runs import _cv_id, _inputs
from pfc.services.matching.semantic import VectorCache, semantic_inputs


def test_combined_cv_gate_scores(tmp_path: Path) -> None:
    actual = [
        [row[key] for key in ("code_requisition", "short_name", "score", "band", "rank")]
        for row in capture(tmp_path)
    ]
    expected = json.loads((FIXTURES / "cv_matching_gate_expected.json").read_text())
    assert actual == expected


def test_combined_shared_cv_is_unique_per_resource(settings: Settings, runner: JobRunner) -> None:
    with database(settings) as db:
        seed_demand(db, mandatory=("SQL", "Python", "AWS"))
        candidate = seed_candidate(db)
        first = seed_resource(db, "First", skills="SQL", candidate=candidate)
        second = seed_resource(db, "Second", skills="AWS", candidate=candidate)
        version = seed_cv(db, first, candidate, skills=("Python",))
        config = load_domain_config()
        demands, resources, cvs, metadata, _ = _inputs(db, "full", [], config)
        bases = [build_combined_basis(r, cvs[r.candidate_id], config) for r in resources]
    assert [b.basis_id for b in bases] == [f"CTCV:{first}:{version}", f"CTCV:{second}:{version}"]
    assert [[s.raw for s in b.skills] for b in bases] == [["SQL", "Python"], ["AWS", "Python"]]
    cache = VectorCache(MatchingPorts(FakeEmbedder()))
    semantics = semantic_inputs(demands, metadata, bases, cache, config.template, lambda *_: None)
    assert {key[1] for key in semantics} == {b.basis_id for b in bases}
    assert {key.owner_id for key in cache.local if key.owner_type == "ct_cv_skills"} == {
        first,
        second,
    }
    run_id = execute(settings, runner)
    with database(settings) as db:
        rows = db.execute("SELECT * FROM match_results WHERE run_id=?", (run_id,)).fetchall()
        assert len(rows) == 2
        for row in rows:
            assert row["best_cv_version_id"] == version
            detail = result_detail(db, row["result_id"])
            assert detail is not None
            assert [s["cv_version_id"] for s in detail["cv_scores"]] == [version]
            assert detail["cv_scores"][0]["is_latest"] == 1
        snapshot = json.loads(
            db.execute(
                "SELECT input_fingerprints_json FROM match_runs WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
        assert set(snapshot["resources"]) == {str(first), str(second)}
        assert snapshot["resources"][str(first)] != snapshot["resources"][str(second)]


@pytest.mark.parametrize("basis,expected", [("CT:4", None), ("CV:9", 9), ("CTCV:4:9", 9)])
def test_combined_cv_id_mapping(basis: str, expected: int | None) -> None:
    assert _cv_id(basis) == expected


@pytest.mark.parametrize("basis", ["CTCV:4", "CTCV:4:x", "OTHER:9"])
def test_invalid_combined_id_rejected(basis: str) -> None:
    with pytest.raises(ValueError):
        _cv_id(basis)


@pytest.mark.parametrize("removed", [False, True])
def test_link_backfills_existing_latest_cv_rows(settings: Settings, removed: bool) -> None:
    with database(settings) as db:
        candidate = seed_candidate(db)
        resource = seed_resource(db, skills="SQL", email="candidate@example.com")
        db.execute(
            "UPDATE resources SET email_key='candidate@example.com' WHERE resource_id=?",
            (resource,),
        )
        version = seed_cv(db, resource, candidate, skills=())
        db.execute(
            "UPDATE cv_versions SET facts_json=?,extraction_removed_at=? WHERE cv_version_id=?",
            (
                json.dumps(
                    {
                        "facts": {
                            "skills": [
                                {
                                    "value": "Python",
                                    "canonical": "Python",
                                    "evidence": "Python",
                                    "span": [4, 10],
                                    "source": "rule",
                                }
                            ]
                        }
                    }
                ),
                "2026-11-06" if removed else None,
                version,
            ),
        )
        _replace_skills(db, version)
        assert db.execute("SELECT count(*) FROM resource_skills").fetchone()[0] == 0
        resolve(
            db,
            candidate,
            IdentityResolution(action="select_ct_record", resource_id=resource),
            1,
            "2026-11-06T00:00:00+00:00",
        )
        _, resources, cvs, _, _ = _inputs(db, "full", [], load_domain_config())
        basis = build_combined_basis(
            resources[0], cvs.get(resources[0].candidate_id, []), load_domain_config()
        )
        assert ("Python" in [s.raw for s in basis.skills]) is not removed


@pytest.mark.parametrize("older_removed", [False, True])
def test_confirm_ct_link_preserves_cv_version_rows(settings: Settings, older_removed: bool) -> None:
    with database(settings) as db:
        candidate = seed_candidate(db)
        resource = seed_resource(db, candidate=candidate, email="candidate@example.com")
        db.execute(
            "UPDATE resources SET email_key='candidate@example.com' WHERE resource_id=?",
            (resource,),
        )
        older = seed_cv(db, resource, candidate, skills=())
        latest = db.execute(
            "INSERT INTO cv_versions(resume_id,candidate_id,version_no,text_sha256,text_norm,"
            "extracted_by,is_latest,extracted_at) "
            "SELECT resume_id,candidate_id,2,'cv-text-v2',text_norm,extracted_by,1,extracted_at "
            "FROM cv_versions WHERE cv_version_id=?",
            (older,),
        ).lastrowid
        for version, skill, span in [(older, "SQL", [0, 3]), (latest, "Python", [4, 10])]:
            db.execute(
                "UPDATE cv_versions SET facts_json=?,is_latest=?,extraction_removed_at=? "
                "WHERE cv_version_id=?",
                (
                    json.dumps(
                        {
                            "facts": {
                                "skills": [
                                    {
                                        "value": skill,
                                        "canonical": skill,
                                        "evidence": skill,
                                        "span": span,
                                        "source": "rule",
                                    }
                                ]
                            }
                        }
                    ),
                    version == latest,
                    "2026-11-06" if version == older and older_removed else None,
                    version,
                ),
            )
            _replace_skills(db, version)
        expected = [(latest, "Python")] if older_removed else [(older, "SQL"), (latest, "Python")]
        for _ in range(2):
            resolve(
                db,
                candidate,
                IdentityResolution(action="select_ct_record", resource_id=resource),
                1,
                "2026-11-06T00:00:00+00:00",
            )
            assert [
                tuple(row)
                for row in db.execute(
                    "SELECT cv_version_id,skill_raw FROM resource_skills "
                    "WHERE resource_id=? AND source='CV' ORDER BY cv_version_id",
                    (resource,),
                )
            ] == expected
        _, _, cvs, _, _ = _inputs(db, "full", [], load_domain_config())
        assert [basis.basis_id for basis in cvs[f"candidate:{candidate}"]] == [f"CV:{latest}"]


@pytest.mark.parametrize("canonical", ["Python", None])
def test_matching_uses_current_synonyms_not_stored_ids(
    settings: Settings, canonical: str | None
) -> None:
    with database(settings) as db:
        candidate = seed_candidate(db)
        resource = seed_resource(db, candidate=candidate)
        version = seed_cv(db, resource, candidate, skills=("PFC alias",))
        db.execute(
            "UPDATE resource_skills SET canonical_skill_id=(SELECT skill_id FROM skills "
            "WHERE name='SQL') WHERE cv_version_id=?",
            (version,),
        )
        config = load_domain_config().model_copy(
            update={
                "synonyms": SynonymTable([SynonymRow(canonical, "PFC alias")] if canonical else [])
            }
        )
        basis = _inputs(db, "full", [], config)[2][f"candidate:{candidate}"][0]
        assert basis.skills[0].canonical == canonical
        expected = db.execute("SELECT skill_id FROM skills WHERE name=?", (canonical,)).fetchone()
        assert basis.skills[0].canonical_skill_id == (expected[0] if expected else None)
