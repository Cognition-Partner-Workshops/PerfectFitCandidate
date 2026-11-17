from dataclasses import replace
from datetime import date

import pytest

from pfc.domain.config_loader import ScoringTemplate, load_domain_config
from pfc.domain.cv_profile import degree, degree_met, experience_years, skill_hits
from pfc.domain.normalise import SynonymRow, SynonymTable, parse_date
from pfc.domain.scoring import (
    Demand,
    RequirementItem,
    Resource,
    SemanticInput,
    SkillBasis,
    SkillEvidence,
    build_combined_basis,
    build_ct_basis,
    score_basis,
)
from pfc.domain.scoring.engine import _item

AS_OF = date(2026, 3, 1)


def test_skill_boundaries_and_dictionary_status():
    table = SynonymTable(
        [SynonymRow(term=term, canonical=term) for term in ("SQL", "C++", ".NET", "R", "Go")]
        + [SynonymRow("Python", "Python", status="suggested"), SynonymRow("SQL", "queries")]
    )
    assert not skill_hits("NoSQL cargo Rx C+++ ASP.NET Python", table)
    hits = skill_hits("Used SQL, C++, .NET, R and Go\nqueries", table)
    assert {canonical for _, canonical, _ in hits} == {"SQL", "C++", ".NET", "R", "Go"}
    assert next(quote for _, canonical, quote in hits if canonical == "SQL").startswith("Used SQL")
    assert skill_hits("", table) == ()
    assert len(skill_hits("x " * 200 + "SQL " + "y " * 200, table)[0][2]) <= 202


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Jan 2010 – Mar 2018\n2015-2020\n2020 – Present", 16 + 2 / 12),
        ("2018 – Current", 8 + 2 / 12),
        ("Jan 2020 to till date", 6 + 2 / 12),
        ("8 years at A\n9 yrs at B\n6 years at C", 23),
        ("8+ yrs at A\n8.5 years at B", 16.5),
        ("23 years of experience\n2010-2018\n8 years at A", 23),
        ("Overall experience of 23 years\n8 yrs at A", 23),
        ("2010-2018\n9 years at A", 8),
        ("No 5 years of experience", None),
        ("2030-2040", None),
        ("Experienced manager", None),
        ("", None),
    ],
)
def test_experience(text, expected):
    result = experience_years(text, AS_OF)
    assert result == pytest.approx(expected) if expected is not None else result is None


@pytest.mark.parametrize(
    "text",
    [
        "Bachler in Technology (Computer Science)",
        "Bachelors in Engineering",
        "Batchelor Engg",
        "Bacholar Computer Science",
        "B.Tech",
        "B.E.",
        "BSc Computer Science",
        "MCA",
        "M.Tech",
    ],
)
def test_technical_degrees(text):
    assert degree_met("Degree in a technical subject", text).technical


def test_degree_unclear_and_levels():
    assert degree("") is None
    assert degree("Worked on technology") is None
    assert degree("Pursuing B.Tech") is None
    assert degree_met("Master's degree", "BA History") is None
    assert degree_met("Degree in technical subject", "BA History\nEngineering work") is None
    assert degree_met("Bachelor degree", "Masters in History").level == 2
    assert degree_met("Doctorate qualification", "PhD Computer Science").level == 3


@pytest.mark.parametrize(
    ("text", "cv", "semantic", "credit", "method"),
    [
        ("At least 5 years of experience", "23 years of experience", 0.16, 1, "cv-experience"),
        ("5+ years", "3 years at A", 0.16, 0.6, "cv-experience"),
        ("minimum of 5 years experience", "3 years at A", 0.8, 0.8, "semantic"),
        ("5 years experience", "Experienced", 0.37, 0.37, "semantic"),
        ("Degree in a technical subject", "Bachler in Technology", 0.37, 1, "cv-degree"),
        ("Degree in a technical subject", "Unclear", 0.37, 0.37, "semantic"),
    ],
)
def test_engine_rules(text, cv, semantic, credit, method):
    config = load_domain_config()
    item = RequirementItem("1", text, "mandatory")
    basis = SkillBasis("CV:1", "CV", (), True, cv)
    inputs = SemanticInput(item_credits={"1": semantic})
    result = _item(item, basis, inputs, config, config.template, AS_OF)
    assert (result.credit, result.method) == (credit, method)
    if credit < 1:
        assert "requires validation" in result.evidence
    off = config.template.model_copy(update={"cv_experience_degree": False})
    assert _item(item, basis, inputs, config, off, AS_OF).credit == semantic
    unknown = replace(basis, cv_text="")
    assert _item(item, unknown, inputs, config, config.template, AS_OF).credit == semantic


def test_stored_template_default_and_explicit_minimum():
    config = load_domain_config()
    data = config.template.model_dump(mode="json")
    data.pop("cv_experience_degree")
    assert ScoringTemplate.from_dict(data).cv_experience_degree
    item = RequirementItem("1", "Professional experience", "mandatory", min_years_exp=5)
    result = _item(
        item,
        SkillBasis("CV:1", "CV", (), True, "3 yrs at A"),
        SemanticInput(),
        config,
        config.template,
        AS_OF,
    )
    assert result.credit == 0.6


def test_no_cv_ct_only_unchanged():
    config = load_domain_config()
    resource = Resource(
        "1",
        "CT only",
        "C1",
        "Manchester",
        "Available",
        "RG1",
        parse_date("2026-01-01"),
        parse_date(None),
        "SQL",
    )
    demand = Demand(
        "T1",
        parse_date("2026-03-01"),
        "C1",
        "Manchester",
        False,
        (
            RequirementItem("1", "SQL", "mandatory"),
            RequirementItem("2", "5 years experience", "mandatory"),
            RequirementItem("3", "Technical degree", "mandatory"),
        ),
    )
    semantic = SemanticInput(0.5, {"2": 0.16, "3": 0.37})
    basis = build_combined_basis(resource, [], config)
    assert basis == build_ct_basis(resource, config)
    after = score_basis(demand, resource, basis, semantic, config, AS_OF)
    legacy = score_basis(
        demand,
        resource,
        basis,
        semantic,
        config,
        AS_OF,
        template=config.template.model_copy(update={"cv_experience_degree": False}),
    )
    assert after == legacy
    assert after.score == 70.7
    assert after.items[0].evidence == "SQL — SQL (CT)"
    assert [i.credit for i in after.items] == [1, 0.16, 0.37]
    assert "CV not found" in after.gaps


def test_combined_ct_cv_equal_credit_and_sources():
    config = load_domain_config()
    resource = Resource(
        "1", "Combined", "C1", None, "Available", None, parse_date(None), parse_date(None), "SQL"
    )
    cv = SkillBasis(
        "CV:1",
        "CV v1",
        (
            SkillEvidence("Python", "python", "Python", "Python", source="CV Skills"),
            SkillEvidence("AWS", "aws", "AWS", "found in CV text: 'Used AWS'", source="CV text"),
        ),
        True,
        "Used AWS",
    )
    basis = build_combined_basis(resource, [cv], config)
    for term, source in (("SQL", "CT"), ("Python", "CV Skills"), ("AWS", "CV text")):
        result = _item(
            RequirementItem("1", term, "mandatory"),
            basis,
            SemanticInput(),
            config,
            config.template,
            AS_OF,
        )
        assert result.credit == 1
        assert f"[{source}]" in result.evidence
