import json

import pytest

from pfc.domain.config_loader import load_domain_config
from pfc.domain.normalise import normalise_term
from pfc.domain.scoring import SkillEvidence
from pfc.services.matching.runs import cv_fact_skills


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "",
        "{",
        "null",
        "[]",
        "{}",
        '{"facts": []}',
        '{"facts": {}}',
        '{"facts": {"skills": {}}}',
        '{"facts": {"skills": [null]}}',
        '{"facts": {"skills": [{"value": "SQL"}]}}',
        '{"facts": {"skills": [{"value": 1, "evidence": "SQL"}]}}',
        '{"facts": {"skills": [{"value": "", "evidence": "SQL"}]}}',
    ],
)
def test_invalid_facts(payload: str | None) -> None:
    assert cv_fact_skills(payload, load_domain_config()) == ()


def test_current_synonyms_order_and_contact_filtering() -> None:
    config = load_domain_config()
    values = ["BA", "person@example.com", "07700 900123", "SQL", "unknown token"]
    payload = json.dumps(
        {
            "facts": {
                "skills": [
                    {"value": value, "canonical": "stale", "evidence": value} for value in values
                ]
            }
        }
    )
    assert cv_fact_skills(payload, config) == tuple(
        SkillEvidence(value, normalise_term(value), config.synonyms.resolve(value), value)
        for value in ["BA", "SQL", "unknown token"]
    )
    assert cv_fact_skills('{"facts": {"skills": []}}', config) == ()
