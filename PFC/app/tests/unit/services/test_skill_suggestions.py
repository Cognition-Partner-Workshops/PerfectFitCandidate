import pytest

from pfc.domain.config_loader import DomainConfig, load_domain_config
from pfc.domain.normalise import SynonymRow, SynonymTable
from pfc.services.occ.skill_suggestions import is_skill_like_jd_term


@pytest.fixture
def config() -> DomainConfig:
    synonyms = SynonymTable([SynonymRow(term, term) for term in ("Python", "SQL")])
    return load_domain_config().model_copy(update={"synonyms": synonyms})


@pytest.mark.parametrize(
    "term",
    [
        "Kubernetes",
        "Azure Data Factory",
        "Stakeholder Management",
        "Node.js",
        "SQL Server",
        "AI Governance",
    ],
)
def test_short_skill_like_jd_terms_are_retained(config: DomainConfig, term: str) -> None:
    assert is_skill_like_jd_term(term, config.synonyms)


@pytest.mark.parametrize(
    "term",
    [
        "5+ years of hands-on experience designing cloud data pipelines",
        "Experience with: Kafka",
        "Strong Python experience",
        "Good knowledge of SQL",
        "Hands-on",
        "Designs scalable data pipelines for analytics teams.",
        "word " * 5,
        "x" * 41,
        "Python; SQL",
    ],
)
def test_non_skill_like_jd_terms_are_rejected(config: DomainConfig, term: str) -> None:
    assert not is_skill_like_jd_term(term, config.synonyms)
