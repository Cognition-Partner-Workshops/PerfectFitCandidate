"""REQ-MAT-03/04, FR-MAT-10/60, REQ-IN-24 ordering and shortlist contracts."""

from dataclasses import replace
from datetime import date

import pytest

from pfc.domain.config_loader import load_domain_config
from pfc.domain.normalise import parse_date
from pfc.domain.scoring import (
    Demand,
    PersonResult,
    RequirementItem,
    Resource,
    SemanticInput,
    SkillBasis,
    SkillEvidence,
    build_combined_basis,
    build_ct_basis,
    ranking_key,
    run_match,
    score_basis,
)

AS_OF = date(2026, 11, 6)


def fixture():
    config = load_domain_config()
    resource = Resource(
        "r",
        "Person",
        "C1",
        "Manchester",
        "Available",
        "RG1",
        parse_date("2026-10-01"),
        parse_date(None),
        "SQL,Python",
    )
    demand = Demand(
        "D",
        parse_date("2026-11-16"),
        "C1",
        "Manchester",
        False,
        (RequirementItem("M1", "SQL", "mandatory"),),
    )
    return config, resource, demand


@pytest.mark.parametrize("key", ["score", "priority", "grade", "start", "missing", "name", "id"])
def test_fr_mat_10_one_tiebreak_at_a_time(key):
    config, resource, demand = fixture()
    best = score_basis(
        demand, resource, build_ct_basis(resource, config), SemanticInput(1), config, AS_OF
    )
    first = PersonResult(resource, best, (best,), 1, 0)
    # All later tie-breakers deliberately favour the second row.
    second = replace(first, resource=replace(resource, resource_id="a", short_name="AAA"))
    first = replace(first, resource=replace(resource, resource_id="z", short_name="ZZZ"))
    if key == "score":
        second = replace(second, best=replace(best, score=best.score - 0.1))
    elif key == "priority":
        second = replace(second, status_priority=2)
    elif key == "grade":
        second = replace(second, grade_distance=1)
    elif key == "start":
        second = replace(second, resource=replace(second.resource, bench_start=parse_date(None)))
    elif key == "missing":
        second = replace(second, best=replace(best, mandatory_missing=1))
    elif key == "name":
        first = replace(first, resource=replace(first.resource, short_name="aaa"))
        second = replace(second, resource=replace(second.resource, short_name="BBB"))
    else:
        first = replace(first, resource=replace(first.resource, resource_id="a", short_name="SAME"))
        second = replace(
            second, resource=replace(second.resource, resource_id="b", short_name="same")
        )
    assert sorted([second, first], key=ranking_key)[0] == first


def test_req_in_21_combined_basis_prefers_cv_canonical_and_normalised_evidence():
    config, resource, demand = fixture()
    person = replace(resource, ct_skills="SQL, Unlisted, Python")
    cv = SkillBasis(
        "CV:12",
        "CV v2",
        (
            SkillEvidence("SQL alias", "sql alias", "SQL", "SQL alias", 1),
            SkillEvidence("SQL", "sql", "SQL", "SQL evidence", 1),
            SkillEvidence("Unlisted", "unlisted", None, "CV unlisted evidence"),
        ),
        True,
    )
    combined = build_combined_basis(person, [cv], config)
    assert combined.basis_id == "CTCV:r:12" and combined.label == "CT + CV v2"
    assert [s.raw for s in combined.skills] == ["SQL", "Unlisted", "Python"]
    assert [s.evidence for s in combined.skills] == [
        "SQL evidence",
        "CV unlisted evidence",
        "Python",
    ]


def test_req_in_21_no_cv_is_exactly_ct_only():
    config, resource, demand = fixture()
    ct = build_ct_basis(resource, config)
    assert build_combined_basis(resource, [], config) == ct
    semantic = SemanticInput(0.5)
    expected = score_basis(demand, resource, ct, semantic, config, AS_OF)
    result = run_match([demand], [resource], {}, {("D", ct.basis_id): semantic}, config, AS_OF)
    assert result.demands[0].rows[0].best == expected
    assert result.demands[0].rows[0].per_basis == (expected,)


def test_req_in_21_empty_latest_cv_still_exists():
    config, resource, demand = fixture()
    cv = SkillBasis("CV:1", "CV v1", (), True)
    result = run_match([demand], [resource], {"r": [cv]}, {}, config, AS_OF)
    row = result.demands[0].rows[0]
    assert row.best.basis_id == "CTCV:r:1"
    assert "CV not found" not in row.gaps_checks


def test_req_mat_03_unique_top_five_and_email_four():
    config, resource, demand = fixture()
    people = [
        replace(resource, resource_id=str(i), candidate_id=str(i), short_name=f"Person{i}")
        for i in range(8)
    ]
    people.append(replace(people[0], resource_id="duplicate"))
    semantics = {("D", f"CT:{p.resource_id}"): SemanticInput(1) for p in people}
    result = run_match([demand], reversed(people), {}, semantics, config, AS_OF)
    output = result.demands[0]
    assert len(output.rows) == 8
    assert len(output.shortlist) == 5 and len(output.email_preselected) == 4
    assert len({r.resource.candidate_id for r in output.shortlist}) == 5
    assert output.shortlist[0].resource.resource_id == "0"
    assert result.pairs_scored == 9
    assert result == run_match([demand], people, {}, semantics, config, AS_OF)


@pytest.mark.parametrize(
    ("strong", "enabled", "requires_no_strong", "threshold", "expected_weak"),
    [
        (False, True, True, None, True),
        (True, True, True, None, False),
        (True, True, False, None, True),
        (False, False, True, None, False),
        (False, True, True, 0.7, False),
        (False, True, True, 0.2, True),
    ],
)
def test_req_mat_04_weak_inclusion(strong, enabled, requires_no_strong, threshold, expected_weak):
    config, resource, demand = fixture()
    d = replace(
        demand,
        requirements=(
            RequirementItem("M1", "SQL", "mandatory"),
            RequirementItem("M2", "AWS", "mandatory"),
            RequirementItem("M3", "Spark", "mandatory"),
            RequirementItem("M4", "Azure", "mandatory"),
        ),
    )
    weak = replace(resource, resource_id="weak", candidate_id="weak")
    people = [weak]
    if strong:
        people.append(
            replace(
                resource,
                resource_id="strong",
                candidate_id="strong",
                ct_skills="SQL,AWS,Spark,Azure",
            )
        )
    t = config.template.model_copy(
        update={
            "weak_inclusion": config.template.weak_inclusion.model_copy(
                update={
                    "enabled": enabled,
                    "requires_no_strong": requires_no_strong,
                    "min_transferable_semantic": threshold,
                }
            ),
        }
    )
    semantics = {("D", "CT:weak"): SemanticInput(0.2), ("D", "CT:strong"): SemanticInput(1)}
    result = run_match([d], people, {}, semantics, config, AS_OF, template=t)
    output = result.demands[0]
    assert any(r.resource.resource_id == "weak" for r in output.shortlist) == expected_weak
    assert output.no_suitable_candidate == (not strong)
    assert output.weak_only == (not strong and expected_weak)
    assert result.weak_only_roles == output.weak_only
    assert result.roles_without_suitable_candidate == (not strong)
    if expected_weak:
        assert output.shortlist[-1].display_band == "Weak Match"


def test_req_mat_04_no_transferable_not_shortlisted():
    config, resource, demand = fixture()
    d = replace(demand, requirements=(RequirementItem("M1", "AWS", "mandatory"),))
    result = run_match([d], [resource], {}, {}, config, AS_OF)
    assert result.demands[0].rows[0].best.band == "Weak"
    assert not result.demands[0].shortlist
    assert result.demands[0].no_suitable_candidate


def test_req_in_24_duplicate_basis_ids_rejected():
    config, resource, demand = fixture()
    ct = build_ct_basis(resource, config)
    cv = SkillBasis("CV:1", "CV v1", ct.skills, True)
    with pytest.raises(ValueError, match="one latest CV"):
        run_match([demand], [resource], {"r": [cv, cv]}, {}, config, AS_OF)


def test_req_in_21_combined_basis_uses_only_latest_cv():
    config, resource, demand = fixture()
    ct = build_ct_basis(resource, config)
    cv = SkillBasis("CV:2", "CV v2", ct.skills, True)
    old = SkillBasis("CV:1", "CV v1", (SkillEvidence("AWS", "aws"),), False)
    result = run_match([demand], [resource], {"r": [old, cv]}, {}, config, AS_OF)
    assert result.demands[0].rows[0].best.basis_id == "CTCV:r:2"
    assert len(result.demands[0].rows[0].per_basis) == 1


def test_req_in_21_cv_adds_missing_mandatory_skill():
    config, resource, demand = fixture()
    person = replace(resource, ct_skills="SQL, AWS")
    demand = replace(
        demand,
        requirements=(
            RequirementItem("M1", "SQL", "mandatory"),
            RequirementItem("M2", "Python", "mandatory"),
        ),
    )
    cv = SkillBasis("CV:1", "CV v1", (SkillEvidence("Python", "python", "Python", "Python"),), True)
    before = run_match([demand], [person], {}, {}, config, AS_OF).demands[0].rows[0].best
    after = run_match([demand], [person], {"r": [cv]}, {}, config, AS_OF).demands[0].rows[0].best
    assert before.mandatory_missing == 1 and after.mandatory_missing == 0
    assert after.score > before.score


def test_req_mat_03_uniqueness_keeps_highest_score():
    config, resource, demand = fixture()
    first = replace(resource, resource_id="a", candidate_id="same")
    better = replace(resource, resource_id="z", candidate_id="same")
    semantics = {("D", "CT:a"): SemanticInput(0), ("D", "CT:z"): SemanticInput(1)}
    result = run_match([demand], [first, better], {}, semantics, config, AS_OF)
    assert len(result.demands[0].rows) == 1
    assert result.demands[0].rows[0].resource.resource_id == "z"


def test_req_mat_04_weak_rows_cannot_exceed_top_n():
    config, resource, demand = fixture()
    d = replace(
        demand,
        requirements=(
            RequirementItem("M1", "SQL", "mandatory"),
            RequirementItem("M2", "AWS", "mandatory"),
            RequirementItem("M3", "Azure", "mandatory"),
        ),
    )
    people = [replace(resource, resource_id=str(i), candidate_id=str(i)) for i in range(9)]
    result = run_match([d], people, {}, {}, config, AS_OF)
    assert len(result.demands[0].shortlist) == 5
    assert result.demands[0].weak_only
    assert result.demands[0].no_suitable_candidate
