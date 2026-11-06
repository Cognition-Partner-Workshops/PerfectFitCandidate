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

from pfc.config import Settings
from pfc.db.connection import database
from pfc.domain.config_loader import load_domain_config
from pfc.domain.scoring import build_combined_basis
from pfc.jobs.runner import JobRunner
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
