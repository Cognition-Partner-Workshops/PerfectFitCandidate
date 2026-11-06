"""FDD §14 golden fixture: REQ-GRD-02, REQ-SCR-01..04, REQ-ELIG-08."""

from dataclasses import replace
from datetime import date

import pytest

from pfc.domain.config_loader import load_domain_config
from pfc.domain.normalise import parse_date
from pfc.domain.scoring import Demand, RequirementItem, Resource, SemanticInput, run_match


@pytest.mark.parametrize(
    ("mode", "totals"),
    [("binary", [63.1, 55.7, 51.3]), ("tiered", [67.1, 55.7, 54.3])],
)
def test_req_scr_01_fdd_idfne323(mode, totals):
    config = load_domain_config()
    requirements = tuple(
        RequirementItem(item_id, text, kind, term)
        for item_id, text, kind, term in [
            ("M1", "Data engineering / data pipeline development", "mandatory", "Data Engineer"),
            ("M2", "Python", "mandatory", None),
            ("M3", "SQL", "mandatory", None),
            ("M4", "Spark (or similar processing technology)", "mandatory", "Spark"),
            ("M5", "ETL/ELT pipelines and enterprise integration", "mandatory", "ETL/ELT"),
            ("P1", "Cloud data services, preferably AWS", "preferred", "AWS"),
            ("P2", "Agile delivery", "preferred", "Agile"),
            ("P3", "Structured and unstructured data at scale", "preferred", "Data at scale"),
            ("P4", "Data quality, reliability, security", "preferred", "Data quality"),
        ]
    )
    demand = Demand("IDFNE323", parse_date("16 Nov 2026"), "C1", "Manchester", False, requirements)
    resources = [
        Resource(
            name, name, grade, location, status, group, parse_date(start), parse_date(end), skills
        )
        for name, grade, location, status, group, start, end, skills in [
            (
                "AlexB",
                "D1",
                "London",
                "Overall upskilling (New bench)",
                "RG1",
                None,
                None,
                "SQL,Python",
            ),
            (
                "AmitS",
                "C2",
                "London ",
                "Overall upskilling (New bench)",
                "RG1",
                "24 Aug 2026",
                None,
                "Data Engineer, SQL, Pyspark, Azure",
            ),
            (
                "RutujaK",
                "C1",
                "Liverpool",
                "Available",
                "RG4",
                "Wed 4/11/2026",
                None,
                "Python, SQL, JIRA",
            ),
            (
                "ShanthiM",
                "D2",
                "London",
                "CV with client - Demand Proposed",
                "RG3",
                None,
                None,
                "SQL,Python",
            ),
            (
                "JagmohanD",
                "C1",
                "London",
                "Selected",
                "Not on Bench",
                None,
                "16 Nov 2026",
                "SQL,Python",
            ),
            (
                "YesG",
                "C2",
                "Northampton",
                "CV with client - Proactive Proposed",
                "RG2",
                "19 Oct 2026",
                None,
                "Data Architect, SQL, DataBricks, Data Warehousing",
            ),
        ]
    ]
    semantics = {
        ("IDFNE323", "CT:AmitS"): SemanticInput(0.82, {"M2": 0.5, "P1": 0.5}),
        ("IDFNE323", "CT:RutujaK"): SemanticInput(0.55, {"P2": 0.5}),
        ("IDFNE323", "CT:YesG"): SemanticInput(0.70, {"M1": 0.5, "M4": 0.5, "M5": 0.5}),
    }
    result = run_match(
        [demand],
        resources,
        {},
        semantics,
        config,
        date(2026, 11, 6),
        template=replace(config.template, status_score_mode=mode),
    )
    excluded = {
        e.resource_id: {reason.code for reason in e.reasons}
        for e in (*result.global_exclusions, *result.pair_exclusions)
    }
    assert excluded == {
        "AlexB": {"GRADE_GAP_GT1"},
        "ShanthiM": {"GRADE_GAP_GT1"},
        "JagmohanD": {"STATUS_EXCLUDED", "NOT_ON_BENCH", "BENCH_END_SET"},
    }
    rows = result.demands[0]
    assert [r.resource.short_name for r in rows.shortlist] == ["AmitS", "RutujaK", "YesG"]
    assert [r.display_band for r in rows.shortlist] == ["Avg", "Weak Match", "Weak Match"]
    assert [r.best.score for r in rows.shortlist] == totals
    # FDD rounds coverage contributions for display; retain the exact formula until 4 dp.
    assert [r.best.components["skills"].points for r in rows.shortlist] == pytest.approx(
        [45.0625, 27.7375, 33.25]
    )
    assert not rows.no_suitable_candidate and not rows.weak_only
    assert rows.email_preselected == rows.shortlist
    assert result.pairs_scored == 3 and result.requisitions_assessed == 1
    assert result.roles_without_suitable_candidate == result.weak_only_roles == 0
    amit, rutuja, yes = rows.shortlist
    assert "Spark via synonym Pyspark (CT)" in amit.best.matching_evidence
    assert "Mandatory gap:" in amit.gaps_checks
    assert "Adjacent grade (C2 vs C1) — confirm" in amit.gaps_checks
    assert "Location check: London " in amit.gaps_checks
    assert "Potential travel: Liverpool vs Manchester — confirm" in rutuja.gaps_checks
    assert "CV with client caveat" in yes.gaps_checks
    assert all("Bench End Date blank" in r.best.eligibility_evidence for r in rows.shortlist)
    assert all("subject to review" in r.best.eligibility_evidence for r in rows.shortlist)
    assert all("CV not found" in r.gaps_checks for r in rows.shortlist)
