import json

import pytest

from pfc.domain.config_loader import DomainConfig, load_domain_config
from pfc.domain.normalise import SynonymRow, SynonymTable, normalise_term
from pfc.services.matching.llm_bridge import jd_generation_schema
from pfc.services.matching.requirements import (
    Requirements,
    _canonicalize_skills,
    _extraction_json,
)


@pytest.fixture
def config() -> DomainConfig:
    rows = [SynonymRow(term, term) for term in ("Kafka", "C", "C++", "C#", "R", "Python", "SQL")]
    rows += [
        SynonymRow("Python", "Py"),
        SynonymRow("Kafka", "stream processing", relation="related", credit=0.6),
        SynonymRow("Unapproved", "unapproved tech", status="suggested"),
        SynonymRow("Rejected", "rejected tech", status="rejected"),
    ]
    return load_domain_config().model_copy(update={"synonyms": SynonymTable(rows)})


@pytest.mark.parametrize("field", ["mandatory_skills", "preferred_skills"])
@pytest.mark.parametrize(
    ("phrase", "canonical"),
    [
        ("Kafka experience for asynchronous order updates", "Kafka"),
        ("C++ is essential for the control loop", "C++"),
        ("C# tooling experience", "C#"),
        ("R for statistical reporting workflow", "R"),
        ("Python and Py experience", "Python"),
        ("Use Ｐｙｔｈｏｎ for ingestion", "Python"),
    ],
)
def test_canonicalize_one_skill(
    config: DomainConfig, field: str, phrase: str, canonical: str
) -> None:
    data = Requirements.model_validate({field: [{"text": phrase, "evidence": phrase, "years": 2}]})
    result = _canonicalize_skills(data, config)
    item = getattr(result, field)[0]
    assert item.text == canonical and item.canonicalized_from == phrase
    assert item.evidence == phrase and item.years == 2
    assert json.loads(_extraction_json(result))[field][0]["canonicalized_from"] == phrase
    assert getattr(data, field)[0].text == phrase
    assert _canonicalize_skills(result, config) == result


@pytest.mark.parametrize(
    "phrase",
    [
        "Use Python and SQL",
        "C++ and C experience",
        "Develop control software for remote environmental instruments",
        "Python",
        "Py",
        "Stream processing experience",
        "Unapproved tech experience",
        "Rejected tech experience",
        "MySQL operations",
    ],
)
def test_ambiguous_unknown_and_exact_skills_unchanged(config: DomainConfig, phrase: str) -> None:
    data = Requirements(mandatory_skills=[{"text": phrase}])
    assert _canonicalize_skills(data, config) == data
    assert "canonicalized_from" not in json.loads(_extraction_json(data))["mandatory_skills"][0]


@pytest.mark.parametrize("existing_field", ["mandatory_skills", "preferred_skills"])
def test_existing_canonical_in_either_list_blocks_rewrite(
    config: DomainConfig, existing_field: str
) -> None:
    data = Requirements(mandatory_skills=[{"text": "Use Python"}])
    getattr(data, existing_field).append(
        Requirements(mandatory_skills=[{"text": "ＰＹＴＨＯＮ"}]).mandatory_skills[0]
    )
    assert _canonicalize_skills(data, config) == data


def test_rewrites_do_not_create_duplicates(config: DomainConfig) -> None:
    data = Requirements.model_validate(
        {
            "mandatory_skills": [{"text": "Use Python"}],
            "preferred_skills": [{"text": "Python experience"}],
        }
    )
    result = _canonicalize_skills(data, config)
    assert result.mandatory_skills[0].text == "Python"
    assert result.preferred_skills[0].text == "Python experience"
    assert result.preferred_skills[0].canonicalized_from is None


@pytest.mark.parametrize("existing_field", ["mandatory_skills", "preferred_skills"])
def test_existing_alias_in_either_list_blocks_rewrite(existing_field: str) -> None:
    config = load_domain_config().model_copy(
        update={"synonyms": SynonymTable([SynonymRow("Business analysis", "Business Analyst")])}
    )
    data = Requirements(mandatory_skills=[{"text": "Business Analysis experience"}])
    getattr(data, existing_field).append(
        Requirements(mandatory_skills=[{"text": "Business Analyst"}]).mandatory_skills[0]
    )
    result = _canonicalize_skills(data, config)
    assert result == data
    assert all(
        item.canonicalized_from is None
        for item in [*result.mandatory_skills, *result.preferred_skills]
    )


def test_empty_and_non_skill_fields_unchanged(config: DomainConfig) -> None:
    assert _canonicalize_skills(Requirements(), config) == Requirements()
    data = Requirements(experience=[{"text": "Use Python"}], qualifications=[{"text": "Use SQL"}])
    assert _canonicalize_skills(data, config) == data


def test_internal_marker_not_in_generation_schema() -> None:
    assert "canonicalized_from" not in json.dumps(jd_generation_schema())
    assert normalise_term("C++") == "c++"
    assert normalise_term("C#") == "c#"
