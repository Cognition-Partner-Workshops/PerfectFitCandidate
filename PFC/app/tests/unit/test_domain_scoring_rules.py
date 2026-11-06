"""Table-driven Phase 1 scoring contracts (REQ-ELIG, REQ-GRD, REQ-SCR, REQ-MAT)."""

from dataclasses import fields, replace
from datetime import date, timedelta
from itertools import product

import pytest

from pfc.domain.config_loader import load_domain_config
from pfc.domain.normalise import SynonymRow, SynonymTable, parse_date
from pfc.domain.scoring import (
    Demand,
    RequirementItem,
    Resource,
    SemanticInput,
    SkillBasis,
    SkillEvidence,
    assess_location,
    assign_band,
    build_ct_basis,
    eligibility,
    normalise_cosine,
    round_score,
    run_match,
    score_basis,
)

AS_OF = date(2026, 11, 6)


@pytest.fixture
def config():
    return load_domain_config()


@pytest.fixture
def resource():
    return Resource(
        "r1",
        "Person",
        "C1",
        "Manchester",
        "Available",
        "RG1",
        parse_date("2026-10-01"),
        parse_date(None),
        "SQL, Python",
    )


@pytest.fixture
def demand():
    return Demand(
        "D1",
        parse_date("2026-11-16"),
        "C1",
        "Manchester",
        False,
        (RequirementItem("M1", "SQL", "mandatory"), RequirementItem("M2", "Python", "mandatory")),
    )


def pair(config, resource, demand, semantic=None, **kwargs):
    return score_basis(
        demand,
        resource,
        build_ct_basis(resource, config),
        semantic or SemanticInput(1),
        config,
        AS_OF,
        **kwargs,
    )


def run(config, resource, demand, semantic=1, **kwargs):
    return run_match(
        [demand],
        [resource],
        {},
        {(demand.code_requisition, f"CT:{resource.resource_id}"): SemanticInput(semantic)},
        config,
        AS_OF,
        **kwargs,
    )


def test_req_elig_06_input_contract(resource, demand):
    for model in (Resource, Demand, SkillBasis, SkillEvidence, RequirementItem, SemanticInput):
        assert not any("capacity" in f.name.casefold() for f in fields(model))
    assert resource.candidate_id == resource.resource_id
    assert isinstance(demand.requirements, tuple)


@pytest.mark.parametrize(
    ("status", "priority", "binary", "tiered", "excluded"),
    [
        ("Available", 1, 5, 5, False),
        ("Overall upskilling (New bench)", 2, 0, 4, False),
        ("CV with client - Demand Proposed", 3, 0, 3, False),
        ("CV with client - Proactive Proposed", 3, 0, 3, False),
        ("Expected on Bench Soon", 4, 0, 2, False),
        ("Status - x", 5, 0, 1, False),
        ("Status - y", 6, 0, 0, False),
        ("Selected", None, 0, 0, True),
        ("Unknown", None, 0, 0, True),
        ("CV with Client - Sold - Pending Allocation", None, 0, 0, True),
        ("new source status", 2, 0, 0, False),
        (None, 2, 0, 0, False),
    ],
)
def test_req_elig_02_07_every_status(
    config, resource, demand, status, priority, binary, tiered, excluded
):
    raw = "  " + "  ".join(status.swapcase().split()) + " " if status else None
    person = replace(resource, bench_status=raw)
    result = run(config, person, demand)
    if excluded:
        assert [r.code for r in result.global_exclusions[0].reasons] == ["STATUS_EXCLUDED"]
        return
    row = result.demands[0].rows[0]
    assert row.status_priority == priority
    assert row.best.components["status"].points == binary
    tier = replace(config.template, status_score_mode="tiered")
    assert (
        run(config, person, demand, template=tier)
        .demands[0]
        .rows[0]
        .best.components["status"]
        .points
        == tiered
    )
    if status not in {s.label for s in config.bench_statuses.statuses.values()}:
        assert "Unmapped status" in row.best.gaps
    if status and "Proposed" in status:
        assert "CV with client caveat" in row.best.eligibility_evidence
        assert "CV with client caveat" in row.gaps_checks
    if status == "Expected on Bench Soon":
        assert "availability from Bench Start Date" in row.gaps_checks
    assert "subject to review" in row.best.eligibility_evidence


@pytest.mark.parametrize("switches", list(product([False, True], repeat=5)))
def test_req_elig_08_all_reason_combinations(config, resource, switches):
    status, group, end, grade, skills = switches
    person = replace(
        resource,
        bench_status="Selected" if status else "Available",
        group=" not  ON bench " if group else "RG1",
        bench_end=parse_date("2026-11-16" if end else None),
        grade=None if grade else "C1",
        ct_skills="" if skills else "SQL,Python",
    )
    reasons = eligibility(person, [build_ct_basis(person, config)], config)
    codes = ["STATUS_EXCLUDED", "NOT_ON_BENCH", "BENCH_END_SET", "NO_GRADE", "NO_SKILLS"]
    assert [r.code for r in reasons] == [
        code for code, enabled in zip(codes, switches, strict=True) if enabled
    ]
    assert all(r.detail for r in reasons)


@pytest.mark.parametrize("grade", [None, "", "not-a-grade"])
def test_req_elig_01_unknown_person_grade(config, resource, demand, grade):
    assert (
        run(config, replace(resource, grade=grade), demand).global_exclusions[0].reasons[0].code
        == "NO_GRADE"
    )


def test_req_elig_01_unparseable_end(config, resource, demand):
    person = replace(resource, bench_end=parse_date("bad"))
    result = run(config, person, demand)
    assert not result.global_exclusions
    best = result.demands[0].rows[0].best
    assert best.components["bench_end"].points == 5
    assert "Bench End Date unparseable — pending review" in best.gaps


@pytest.mark.parametrize(
    ("grade", "allowed"),
    [
        ("D1", {"C2", "D1", "D2"}),
        ("C2", {"C1", "C2", "D1"}),
        ("C1", {"B2", "C1", "C2"}),
        ("A3", {"A3", "A4"}),
        ("E1", {"D2", "E1"}),
    ],
)
def test_req_grd_01_ladder_examples(config, resource, demand, grade, allowed):
    for candidate in config.grades.codes:
        result = run(config, replace(resource, grade=candidate), replace(demand, grade=grade))
        if candidate not in allowed:
            assert result.pair_exclusions[0].reasons[0].code == "GRADE_GAP_GT1"
            assert result.pair_exclusions[0].reasons[0].detail == "Grade outside ±1"
        else:
            best = result.demands[0].rows[0].best
            assert best.components["grade"].points == (10 if candidate == grade else 7)
            assert any("Adjacent grade" in gap for gap in best.gaps) == (candidate != grade)


@pytest.mark.parametrize("grade", [None, "", "unknown"])
def test_fr_mat_22_unknown_demand(config, resource, demand, grade):
    result = run(config, resource, replace(demand, grade=grade))
    assert result.demands[0].flags == ("Unknown demand grade — demand not scorable",)
    assert result.pairs_scored == 0


@pytest.mark.parametrize(
    ("person", "role", "expected"),
    [
        ("London", None, "Remote/unspecified"),
        ("London", "UK Remote", "Remote/unspecified"),
        ("Manchster", "Manchester", "Aligned"),
        ("Liverpool", "Manchester", "Potential travel"),
        ("Northampton", "Manchester", "Location check"),
        ("London", "Manchester", "Location check"),
        ("Edinburgh/Glasgow", "Glasgow", "Aligned"),
        ("London, Manchester", "Manchester", "Aligned"),
        ("London & Manchester", "Manchester", "Aligned"),
        ("London;Manchester", "Manchester", "Aligned"),
        ("London or Manchester", "Manchester", "Aligned"),
        ("Edinburgh", "Glasgow", "Potential travel"),
        ("Reading", "London", "Potential travel"),
        ("Coventry", "Birmingham", "Potential travel"),
        (None, "Manchester", "Location check"),
    ],
)
@pytest.mark.parametrize("onsite", [False, True])
def test_req_loc_01_classes(config, resource, demand, person, role, expected, onsite):
    d = replace(demand, region=role, onsite_required=onsite)
    r = replace(resource, location=person)
    classification, gaps = assess_location(d, r, config)
    assert classification == expected
    assert any("Onsite requirement at" in gap for gap in gaps) == (onsite and expected != "Aligned")
    assert ("Person location not recorded" in gaps) == (person is None)
    best = pair(config, r, d)
    assert best.components["location"].points == config.template.location_points[expected]
    assert dict(best.components["location"].inputs)["role_location"] == (role or "Unspecified")


@pytest.mark.parametrize(
    ("days", "points"),
    [
        (-1, 5),
        (0, 5),
        (1, 4),
        (30, 4),
        (31, 3),
        (45, 3),
        (46, 2),
        (60, 2),
        (61, 1),
        (90, 1),
        (91, 0),
    ],
)
def test_req_scr_01_bench_tiers(config, resource, demand, days, points):
    start = demand.position_start.value + timedelta(days=days)
    best = pair(config, replace(resource, bench_start=parse_date(start)), demand)
    assert best.components["bench_start"].points == points
    assert any("Bench Start Date after" in gap for gap in best.gaps) == (days > 0)


@pytest.mark.parametrize("raw", [None, "bad"])
def test_req_scr_01_missing_bench_start(config, resource, demand, raw):
    best = pair(config, replace(resource, bench_start=parse_date(raw)), demand)
    assert best.components["bench_start"].points == 0
    assert any("Bench Start Date not recorded or unparseable" in gap for gap in best.gaps)


def test_req_scr_01_explicit_reference_date(config, resource, demand):
    person = replace(resource, bench_start=parse_date(AS_OF + timedelta(days=31)))
    d = replace(demand, position_start=parse_date(None))
    best = pair(config, person, d)
    assert best.components["bench_start"].points == 3
    assert dict(best.components["bench_start"].inputs)["reference"] == AS_OF.isoformat()


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_req_in_12_past_position_start_flag_without_score_change(config, resource, demand, offset):
    start = AS_OF + timedelta(days=offset)
    d = replace(demand, position_start=parse_date(start))
    best = pair(config, resource, d)
    expected = f"Position Start Date {start.isoformat()} is in the past — confirm"
    assert (expected in best.gaps) == (offset < 0)
    assert best.score == pair(config, resource, demand).score
    assert dict(best.components["bench_start"].inputs)["reference"] == start.isoformat()


@pytest.mark.parametrize(
    ("score", "rounded", "band"),
    [
        (89.95, 90.0, "Strong"),
        (89.94, 89.9, "Potential"),
        (60.9, 60.9, "Weak"),
        (61, 61, "Avg"),
        (69.9, 69.9, "Avg"),
        (70, 70, "Potential"),
    ],
)
def test_req_scr_02_band_rounding(config, score, rounded, band):
    assert round_score(score) == rounded
    assert assign_band(score, config.template) == band


def test_req_scr_02_total_half_up_boundary(config, resource, demand):
    best = pair(config, resource, demand, SemanticInput((89.95 - 82.5) / 17.5))
    assert best.score == 90.0
    assert best.band == "Strong"


@pytest.mark.parametrize(("cos", "expected"), [(0, 0), (0.45, 0), (0.625, 0.5), (0.8, 1), (1, 1)])
def test_req_mat_01_cosine(config, cos, expected):
    assert normalise_cosine(cos, config.template) == pytest.approx(expected)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1])
def test_req_mat_01_semantic_validation(value):
    with pytest.raises(ValueError):
        SemanticInput(value)
    with pytest.raises(ValueError):
        SemanticInput(0, {"M1": value})


def test_req_mat_01_cosine_errors(config):
    with pytest.raises(ValueError):
        normalise_cosine(float("nan"), config.template)
    template = replace(config.template, skills=replace(config.template.skills, semantic_high=0.45))
    with pytest.raises(ValueError):
        normalise_cosine(0.5, template)


@pytest.mark.parametrize(
    ("term", "skills", "semantic_credit", "expected", "method"),
    [
        ("SQL", "sql,Python", 0, 1, "exact"),
        ("Spark", "Pyspark,SQL", 0, 1, "synonym"),
        ("Business Analysis", "BA,SQL", 0, 1, "synonym"),
        ("Python", "SQL,AWS", 1, 0.8, "semantic"),
        ("Python", "SQL,AWS", 0.5, 0.5, "semantic"),
        ("Python", "SQL,AWS", 0, 0, "not recorded"),
        ("Data Engineer", "Data Engineer,SQL,Python", 0, 1, "exact"),
        ("Delivery Lead", "Project Manager,SQL", 0.3, 0.6, "related"),
    ],
)
def test_fr_mat_40_45_item_credit(
    config, resource, demand, term, skills, semantic_credit, expected, method
):
    d = replace(demand, requirements=(RequirementItem("M1", term, "mandatory"),))
    best = pair(
        config, replace(resource, ct_skills=skills), d, SemanticInput(0.5, {"M1": semantic_credit})
    )
    item = best.items[0]
    assert item.credit == expected
    assert item.method == method
    assert "lacks" not in item.evidence
    assert ("partial — requires validation" in item.evidence) == (0 < expected < 1)
    assert ("not recorded — requires validation" in item.evidence) == (expected == 0)
    assert best.mandatory_missing == (expected < 0.5)
    assert any("Mandatory gap" in gap for gap in best.gaps) == (expected < 0.5)


def test_req_scr_01_subweights_and_breakdown(config, resource, demand):
    d = replace(
        demand,
        requirements=(
            RequirementItem("M1", "SQL", "mandatory"),
            RequirementItem("M2", "AWS", "mandatory"),
            RequirementItem("P1", "Python", "preferred"),
        ),
    )
    best = pair(config, resource, d, SemanticInput(0.4))
    assert best.components["skills"].points == pytest.approx(70 * (0.6 * 0.5 + 0.15 + 0.25 * 0.4))
    assert tuple(best.components) == (
        "skills",
        "grade",
        "location",
        "bench_start",
        "bench_end",
        "status",
    )
    assert [c.maximum for c in best.components.values()] == [70, 10, 5, 5, 5, 5]
    assert all(c.inputs for c in best.components.values())
    assert best.score == round_score(sum(c.points for c in best.components.values()))


@pytest.mark.parametrize(
    ("skills", "requirements", "insufficient"),
    [
        ("SQL", True, True),
        ("SQL, sql", True, True),
        ("SQL,Python", False, True),
        ("SQL,Python", True, False),
    ],
)
def test_req_scr_02_insufficient(config, resource, demand, skills, requirements, insufficient):
    d = demand if requirements else replace(demand, requirements=())
    result = run(config, replace(resource, ct_skills=skills), d)
    rows = result.demands[0].insufficient_data if insufficient else result.demands[0].rows
    assert len(rows) == 1
    assert (rows[0].best.band == "Insufficient data") == insufficient
    assert "CV not found" in rows[0].gaps_checks
    if insufficient:
        assert not result.demands[0].shortlist


def test_req_scr_03_cap_band(config, resource, demand):
    d = replace(
        demand,
        requirements=tuple(
            RequirementItem(str(i), "SQL" if i else "AWS", "mandatory") for i in range(10)
        ),
    )
    assert pair(config, resource, d).band == "Strong"
    template = replace(config.template, mandatory_gap_policy="cap_band")
    best = pair(config, resource, d, template=template)
    assert best.band == "Potential" and best.mandatory_missing == 1


def test_req_mat_05_empty_unknown_related(config, resource, demand):
    assert not run(config, replace(resource, ct_skills=None), demand).demands[0].rows
    table = SynonymTable(
        [SynonymRow("SQL", "query", "related", 0.4), SynonymRow("Python", "Python")]
    )
    cfg = replace(config, synonyms=table)
    d = replace(demand, requirements=(RequirementItem("M1", "query", "mandatory"),))
    assert pair(cfg, resource, d).items[0].credit == 0.4


def test_req_mat_05_related_aliases(config, resource, demand):
    table = SynonymTable(
        [
            SynonymRow("Query Language", "QL", "abbreviation"),
            SynonymRow("Python", "Query Language", "related", 0.6),
            SynonymRow("SQL", "SQL"),
        ]
    )
    cfg = replace(config, synonyms=table)
    d = replace(demand, requirements=(RequirementItem("M1", "QL", "mandatory"),))
    assert pair(cfg, resource, d).items[0].credit == 0.6


def test_req_in_24_cv_independence_and_evidence(config, resource, demand):
    person = replace(resource, ct_skills=None)
    cv = SkillBasis(
        "cv2",
        "CV v2",
        (
            SkillEvidence("SQL", "sql", "SQL", "Experience – project"),
            SkillEvidence("Python", "python", "Python", "Skills section"),
        ),
        True,
    )
    result = run_match([demand], [person], {person.candidate_id: [cv]}, {}, config, AS_OF)
    row = result.demands[0].rows[0]
    assert row.best.basis_id == "cv2"
    assert len(row.per_basis) == 2
    assert row.per_basis[0].recognised_skills == 0
    assert "CV not found" not in row.gaps_checks
    assert "Experience – project" in row.best.matching_evidence
    assert "(CV v2)" in row.best.matching_evidence


def test_req_in_24_empty_run(config):
    result = run_match([], [], {}, {}, config, AS_OF)
    assert result.demands == result.global_exclusions == result.pair_exclusions == ()
    assert result.pairs_scored == result.requisitions_assessed == 0
    assert result.template_name == "default" and result.template_version == 1
    assert result.engine_version and result.as_of_date == AS_OF


def test_req_elig_01_unrecognised_free_text(config, resource, demand):
    person = replace(resource, ct_skills="unapproved term, !!!")
    assert run(config, person, demand).global_exclusions[0].reasons[0].code == "NO_SKILLS"


def test_req_scr_01_empty_kind_coverage(config, resource, demand):
    best = pair(config, resource, demand)
    assert dict(best.components["skills"].inputs)["preferred_cov"] == "1.0"
    best = pair(config, resource, replace(demand, requirements=()))
    inputs = dict(best.components["skills"].inputs)
    assert inputs["mandatory_cov"] == inputs["preferred_cov"] == "1.0"
    assert best.band == "Insufficient data"


def test_req_in_24_canonical_cv_skills_without_seed(config, resource, demand):
    person = replace(resource, ct_skills=None)
    cv = SkillBasis(
        "cv",
        "CV v1",
        (
            SkillEvidence("ETL", "etl", "ETL", "Work history"),
            SkillEvidence("Data Engineer", "data engineer", "Data Engineer", "Role"),
        ),
    )
    d = replace(demand, requirements=(RequirementItem("M1", "ETL", "mandatory"),))
    result = run_match([d], [person], {"r1": [cv]}, {}, config, AS_OF)
    row = result.demands[0].rows[0]
    assert row.best.recognised_skills == 2
    assert row.best.items[0].credit == 1


@pytest.mark.parametrize("person", [None, "", "  "])
@pytest.mark.parametrize(
    ("role", "expected", "points"),
    [
        (None, "Remote/unspecified", 5),
        ("", "Remote/unspecified", 5),
        ("  ", "Remote/unspecified", 5),
        ("Remote", "Remote/unspecified", 5),
        ("UK Remote", "Remote/unspecified", 5),
        ("WFH", "Remote/unspecified", 5),
        ("Anywhere", "Remote/unspecified", 5),
        ("Any", "Remote/unspecified", 5),
        ("Manchester", "Location check", 1),
    ],
)
def test_req_loc_01_demand_location_precedence(
    config, resource, demand, person, role, expected, points
):
    d = replace(demand, region=role)
    r = replace(resource, location=person)
    classification, gaps = assess_location(d, r, config)
    assert classification == expected
    assert gaps == ("Person location not recorded",)
    assert pair(config, r, d).components["location"].points == points


def test_req_mat_06_bad_input_contracts():
    with pytest.raises(ValueError):
        RequirementItem("M1", "SQL", "invalid")
    with pytest.raises(ValueError):
        Demand(
            "D",
            parse_date(None),
            "C1",
            None,
            False,
            (RequirementItem("M1", "SQL", "mandatory"), RequirementItem("M1", "SQL", "mandatory")),
        )
