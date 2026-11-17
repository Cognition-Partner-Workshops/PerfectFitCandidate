import json
from dataclasses import replace

import pytest
from test_matching_service import (
    NOW,
    execute,
    seed_candidate,
    seed_cv,
    seed_demand,
    seed_resource,
)
from test_matching_service import runner as runner
from test_matching_service import settings as settings

from pfc.db.connection import database
from pfc.domain.normalise import SynonymRow, SynonymTable, parse_date
from pfc.domain.scoring import Demand, RequirementItem, SemanticInput, SkillBasis, score_basis
from pfc.services.matching import confirm_requirements
from pfc.services.matching.cv_bases import load_cv_bases, load_resources
from pfc.services.matching.runs import _inputs
from pfc.services.occ.domain import occ_domain_config


@pytest.mark.parametrize(
    "experience,expected",
    [
        ([{"text": "Delivery experience", "years": 5}], [5]),
        ([{"text": "5 years delivery experience", "years": 5}], [None]),
        (
            [
                {"text": "5 years delivery experience", "years": 5},
                {"text": "Leadership experience", "years": 2},
            ],
            [None, None],
        ),
        ([], []),
    ],
)
def test_minimum_experience_fallback_is_unambiguous(settings, experience, expected):
    with database(settings) as db:
        demand_id = seed_demand(db)
        confirm_requirements(db, demand_id, {"experience": experience}, 1, NOW)
        demands, *_ = _inputs(db, "demands", [demand_id], occ_domain_config(db))
    items = [item for item in demands[0].requirements if item.source_kind == "experience"]
    assert [item.min_years_exp for item in items] == expected


@pytest.mark.parametrize(
    "text",
    [
        "SQL engineer alice@example.com +44 7700 900123",
        "SQL engineer",
        "",
    ],
)
def test_cv_text_evidence_redacts_contacts(settings, text):
    with database(settings) as db:
        candidate = seed_candidate(db)
        resource = seed_resource(db, candidate=candidate)
        cv = seed_cv(db, resource, candidate, skills=())
        db.execute("UPDATE cv_versions SET text_norm=? WHERE cv_version_id=?", (text, cv))
        config = occ_domain_config(db)
        basis = load_cv_bases(db, config)[f"candidate:{candidate}"][0]
    quotes = " ".join(skill.evidence for skill in basis.skills)
    assert "alice@example.com" not in quotes
    assert "7700" not in quotes
    if "@" in text:
        assert "[email removed]" in quotes
        assert "[phone removed]" in quotes
    assert bool(basis.skills) == bool(text)


def test_dummy_candidate_whole_cv_score(settings):
    text = (
        "Project Manager\nSkills: Project Management\n"
        "8 years at A using SQL, Hadoop and UAT for requirements gathering\n"
        "9 years at B\n6 years at C\nBachler in Technology (Computer Science)"
    )
    with database(settings) as db:
        candidate = seed_candidate(db)
        resource_id = seed_resource(db, "Dummy Candidate", candidate=candidate)
        cv_id = seed_cv(db, resource_id, candidate, skills=("Project Management",))
        db.execute("UPDATE cv_versions SET text_norm=? WHERE cv_version_id=?", (text, cv_id))
        config = occ_domain_config(db)
        terms = ("Project Management", "SQL", "Hadoop", "UAT", "requirements gathering")
        config = config.model_copy(
            update={"synonyms": SynonymTable([SynonymRow(t, t) for t in terms])}
        )
        basis = load_cv_bases(db, config)[f"candidate:{candidate}"][0]
        resource = load_resources(db)[0]
    assert len(basis.skills) == 5
    assert basis.skills[0].evidence == "Project Management"
    assert all("found in CV text:" in s.evidence for s in basis.skills[1:])
    items = tuple(
        RequirementItem(str(i), t, "mandatory")
        for i, t in enumerate(
            (*terms, "At least 5 years of experience", "Degree in a technical subject")
        )
    )
    demand = Demand(
        "T10004", parse_date("2026-11-16"), resource.grade, resource.location, False, items
    )
    semantic = SemanticInput(0.2, {str(i): 0.65 for i in range(7)} | {"5": 0.16, "6": 0.37})
    old = SkillBasis(basis.basis_id, basis.label, basis.skills[:1], True)
    before = score_basis(demand, resource, old, semantic, config, NOW.date(), has_cv=True)
    after = score_basis(demand, resource, basis, semantic, config, NOW.date(), has_cv=True)
    assert before.score < 75
    assert 82 <= after.score <= 90
    assert after.score - before.score > 15
    assert "cv-text" in {i.method for i in after.items}
    assert "Experience: 23 yrs ≥ 5 (CV)" in [i.evidence for i in after.items]
    assert "Degree: Bachelor, technical (CV)" in [i.evidence for i in after.items]
    disabled = config.template.model_copy(update={"cv_experience_degree": False})
    off = score_basis(
        demand, resource, basis, semantic, config, NOW.date(), template=disabled, has_cv=True
    )
    assert after.score > off.score
    assert not any(i.method in {"cv-experience", "cv-degree"} for i in off.items)
    assert replace(basis, cv_text="").skills == basis.skills


@pytest.mark.parametrize("change", ["text", "rule"])
def test_whole_cv_incremental(settings, runner, change):
    with database(settings) as db:
        seed_demand(db)
        candidate = seed_candidate(db)
        resource = seed_resource(db, candidate=candidate)
        cv = seed_cv(db, resource, candidate)
    base = execute(settings, runner)
    with database(settings) as db:
        if change == "text":
            db.execute(
                "UPDATE cv_versions SET text_norm=text_norm || '\n8 years at A' "
                "WHERE cv_version_id=?",
                (cv,),
            )
        else:
            row = db.execute(
                "SELECT template_id,rules_json FROM scoring_templates WHERE status='active'"
            ).fetchone()
            rules = json.loads(row["rules_json"])
            rules["cv_experience_degree"] = False
            db.execute(
                "UPDATE scoring_templates SET rules_json=? WHERE template_id=?",
                (json.dumps(rules), row["template_id"]),
            )
    updated = execute(settings, runner, incremental=True)
    with database(settings) as db:
        old = db.execute("SELECT * FROM match_runs WHERE run_id=?", (base,)).fetchone()
        new = db.execute("SELECT * FROM match_runs WHERE run_id=?", (updated,)).fetchone()
        plan = json.loads(new["scope_json"])
        if change == "rule":
            assert new["config_key"] != old["config_key"]
            assert (plan["mode"], plan["reason"]) == ("full", "config_changed")
        else:
            assert new["config_key"] == old["config_key"]
            assert plan["mode"] == "incremental"
            assert plan["dirty_resources"] == 1


def test_persisted_experience_and_degree_evidence(settings, runner):
    with database(settings) as db:
        demand = seed_demand(db, confirmed=False)
        confirm_requirements(
            db,
            demand,
            {
                "mandatory_skills": [{"text": "SQL"}],
                "experience": [{"text": "At least 5 years of experience", "years": 5}],
                "qualifications": [{"text": "Degree in a technical subject"}],
            },
            1,
            NOW,
        )
        candidate = seed_candidate(db)
        resource = seed_resource(db, candidate=candidate)
        cv = seed_cv(db, resource, candidate)
        db.execute(
            "UPDATE cv_versions SET text_norm=? WHERE cv_version_id=?",
            (
                "SQL\n23 years of experience\nBachler in Technology",
                cv,
            ),
        )
    run = execute(settings, runner)
    with database(settings) as db:
        row = db.execute("SELECT * FROM match_results WHERE run_id=?", (run,)).fetchone()
        assert "Experience: 23 yrs ≥ 5 (CV)" in row["matching_evidence"]
        assert "Degree: Bachelor, technical (CV)" in row["matching_evidence"]
        assert "Experience duration not recorded" not in row["gaps_checks"]
        inputs = json.loads(
            db.execute(
                "SELECT inputs_json FROM score_breakdowns WHERE result_id=? AND component='skills'",
                (row["result_id"],),
            ).fetchone()[0]
        )
        assert {item["method"] for item in inputs["items"]} >= {"cv-experience", "cv-degree"}
