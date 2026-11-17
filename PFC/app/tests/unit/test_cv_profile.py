from dataclasses import replace
from datetime import date

import pytest

from pfc.domain import cv_profile
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
from pfc.domain.scoring.engine import _cv_requirement, _item

AS_OF = date(2026, 3, 1)


def test_dictionary_patterns_are_reused_beyond_regex_cache_size():
    table = SynonymTable([SynonymRow(f"Skill{i}", f"Skill{i}") for i in range(600)])
    cv_profile._skill_patterns.cache_clear()
    first = skill_hits("Skill599", table)
    assert skill_hits("Skill599", table) == first
    assert cv_profile._skill_patterns.cache_info().misses == 1
    assert cv_profile._skill_patterns.cache_info().hits == 1
    assert skill_hits("Skill599", SynonymTable([])) == ()


def test_cv_and_requirement_caches_keep_inputs_isolated():
    text = "2020 – Present\nBA in History\nMSc Computer Science"
    experience_years.cache_clear()
    assert experience_years(text, AS_OF) == experience_years(text, AS_OF)
    assert experience_years.cache_info().misses == 1
    assert experience_years(text, date(2025, 3, 1)) == pytest.approx(5 + 2 / 12)
    cv_profile._cv_degrees.cache_clear()
    assert degree_met("Bachelor degree", text).label == "Bachelor"
    assert degree_met("Master degree", text).label == "Master"
    assert cv_profile._cv_degrees.cache_info().misses == 1
    assert cv_profile._cv_degrees.cache_info().hits == 1
    assert degree_met("Master degree", "") is None
    assert experience_years("", AS_OF) is None
    assert experience_years("", AS_OF) is None
    _cv_requirement.cache_clear()
    table = SynonymTable([])
    item = RequirementItem("1", "5 years of Java experience", "mandatory", source_kind="experience")
    assert _cv_requirement(item, table)[0] == 5
    assert _cv_requirement(item, table)[0] == 5
    assert _cv_requirement.cache_info().misses == 1
    assert _cv_requirement.cache_info().hits == 1
    java = SynonymTable([SynonymRow("Java", "Java")])
    assert _cv_requirement(item, java)[0] is None
    assert _cv_requirement(RequirementItem("2", "AWS certification", "mandatory"), table) == (
        None,
        None,
    )


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
        ("5 years of experience in SQL\n2010-2020", 10),
        ("5 years of experience with SQL\n8 yrs at A\n9 years at B", 17),
        ("5 years of experience of SQL", None),
        ("5 years of professional experience in SQL\n2010-2020", 5),
        ("Overall 5 years of experience in SQL\n2010-2020", 5),
        ("2010-2018\n9 years at A", 8),
        ("B.Tech, XYZ University, 2001–2005\n2006–2009 Software Engineer", 3),
        ("Education\n2001–2005\nWork Experience\n2006–2009", 3),
        ("Qualifications:\nB.Tech\n2001–2005\nPROFESSIONAL EXPERIENCE\n2006–2009", 3),
        ("## Education\n2001–2005\n## Projects:\n2006–2009", 3),
        ("School 2001–2005\n2006–2009", 7),
        ("Scrum Master 2015–2020\nDeveloper 2020–2022", 7),
        ("Lecturer, XYZ University 2010–2015\nDeveloper 2015–2020", 10),
        ("Education\n2001–2005", None),
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
    "text,expected",
    [
        ("Scrum Master 2015–2020\nDeveloper 2020–2022", 7),
        ("Lecturer, XYZ University 2010–2015", 5),
    ],
)
def test_employment_titles_and_institutions_are_not_degrees(text, expected):
    assert degree(text) is None
    assert experience_years(text, AS_OF) == expected


@pytest.mark.parametrize(
    "heading",
    [
        "Professional Background",
        "CAREER PROFILE",
        "Technical Employment",
        "Work History:",
        "professional",
        "career",
        "projects",
        "employment",
    ],
)
def test_work_resumes_after_education_heading(heading):
    text = f"Education\nB.Tech, XYZ University\n2001–2005\n{heading}\nDeveloper 2006–2009"
    assert experience_years(text, AS_OF) == 3


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
    "text,level,technical",
    [
        ("Master of Science", 2, False),
        ("Masters in Engineering", 2, True),
        ("Master’s in Engineering", 2, True),
        ("Bachelor of Engineering", 1, True),
        ("Bachelor's in History", 1, False),
        ("Bachelors in History", 1, False),
        ("Graduate", 1, False),
        ("Graduate qualification", 1, False),
        ("Degree-level qualification", 1, False),
        ("Doctorate", 3, False),
        ("PhD", 3, False),
    ],
)
def test_full_word_degree_requirements(text, level, technical):
    assert cv_profile.degree_requirement(text) == (level, technical)
    assert degree_met(text, "PhD Engineering").level == 3


@pytest.mark.parametrize("text", ["BA", "MSc", "Scrum Master", "5 years of BA experience"])
def test_ambiguous_degree_requirements_are_rejected(text):
    assert cv_profile.degree_requirement(text) is None
    assert degree_met(text, "PhD Engineering") is None


@pytest.mark.parametrize(
    "text",
    [
        "Postgraduate in Computer Science",
        "Post-graduate in Computer Science",
        "Post graduate degree",
        "PG degree",
        "Master's-level qualification",
    ],
)
def test_postgraduate_requirements_need_a_master(text):
    required = cv_profile.degree_requirement(text)
    assert required is not None and required[0] == 2
    assert degree_met(text, "BSc Computer Science") is None
    assert degree_met(text, "MSc Computer Science").level == 2
    config = load_domain_config()
    result = _item(
        RequirementItem("1", text, "mandatory", source_kind="qualification"),
        SkillBasis("CV:1", "CV", (), True, "BSc Computer Science"),
        SemanticInput(item_credits={"1": 0.37}),
        config,
        config.template,
        AS_OF,
    )
    assert (result.credit, result.method) == (0.37, "semantic")


@pytest.mark.parametrize(
    "text", ["Postgraduate Diploma", "Post-graduate Diploma in Computer Science"]
)
def test_postgraduate_diploma_is_not_a_master(text):
    assert degree(text) is None
    assert degree_met("Postgraduate in Computer Science", text) is None
    assert degree_met("Postgraduate in Computer Science", text + "\nBSc Computer Science") is None
    assert (
        degree_met(
            "Postgraduate in Computer Science", text + "\nMaster's in Computer Science"
        ).level
        == 2
    )


@pytest.mark.parametrize(
    "text,level,technical",
    [
        ("I will be responsible for it", None, False),
        ("ba bs mba mca it", None, False),
        ("BE (Mechanical)", 1, True),
        ("B.E.", 1, True),
        ("BA in History", 1, False),
        ("Bachelor in History: it was interesting", 1, False),
        ("Bachelor IT", 1, True),
        ("Bachler in Technology (Computer Science)", 1, True),
        ("B.Sc", 1, False),
        ("BS", 1, False),
        ("MBA", 2, False),
        ("MSc", 2, False),
        ("Master of Science", 2, False),
        ("Masters in Physics", 2, False),
        ("Master’s in Engineering", 2, True),
        ("Scrum Master", None, False),
    ],
)
def test_degree_abbreviation_case(text, level, technical):
    result = degree(text)
    if level is None:
        assert result is None
    else:
        assert (result.level, result.technical) == (level, technical)


@pytest.mark.parametrize(
    "source_kind,skill_term,text",
    [
        ("mandatory_skill", None, "5 years of Java experience"),
        ("experience", None, "5 years of Java experience"),
        ("experience", "Java", "At least 5 years experience"),
        (None, None, "5 years of Java experience"),
        (None, "Java", "At least 5 years experience"),
        ("domain", None, "At least 5 years experience"),
    ],
)
def test_experience_rule_excludes_skill_items(source_kind, skill_term, text):
    config = load_domain_config()
    config = config.model_copy(update={"synonyms": SynonymTable([SynonymRow("Java", "Java")])})
    item = RequirementItem("1", text, "mandatory", skill_term, source_kind=source_kind)
    result = _item(
        item,
        SkillBasis("CV:1", "CV", (), True, "23 years of experience"),
        SemanticInput(item_credits={"1": 0.37}),
        config,
        config.template,
        AS_OF,
    )
    assert (result.credit, result.method) == (0.37, "semantic")


def test_explicit_qualification_kind():
    config = load_domain_config()
    item = RequirementItem("1", "MSc degree", "mandatory", source_kind="qualification")
    result = _item(
        item,
        SkillBasis("CV:1", "CV", (), True, "MSc"),
        SemanticInput(),
        config,
        config.template,
        AS_OF,
    )
    assert (result.credit, result.method) == (1, "cv-degree")


@pytest.mark.parametrize(
    "text",
    [
        "AWS certification",
        "MSc",
        "PMP qualification",
        "AWS qualification",
        "Academic qualification",
        "Professional qualification",
    ],
)
@pytest.mark.parametrize("source_kind", ["qualification", None])
def test_certification_is_not_a_degree(text, source_kind):
    config = load_domain_config()
    assert degree_met(text, "BA in History") is None
    result = _item(
        RequirementItem("1", text, "mandatory", source_kind=source_kind),
        SkillBasis("CV:1", "CV", (), True, "BA in History"),
        SemanticInput(item_credits={"1": 0.37}),
        config,
        config.template,
        AS_OF,
    )
    assert (result.credit, result.method) == (0.37, "semantic")


@pytest.mark.parametrize("source_kind", ["experience", "mandatory_skill", None])
@pytest.mark.parametrize(
    "text", ["5 years of BA experience", "5 years of bachelor degree experience"]
)
def test_experience_requirements_do_not_receive_degree_credit(source_kind, text):
    config = load_domain_config()
    result = _item(
        RequirementItem("1", text, "mandatory", source_kind=source_kind),
        SkillBasis("CV:1", "CV", (), True, "BSc Physics"),
        SemanticInput(item_credits={"1": 0.37}),
        config,
        config.template,
        AS_OF,
    )
    assert (result.credit, result.method) == (0.37, "semantic")


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
