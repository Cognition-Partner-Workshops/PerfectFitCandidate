"""Conservative, explicit duration evidence, never inferred from dates or totals."""

import pytest

from pfc.services.matching.evidence import shows_duration


@pytest.mark.parametrize(
    "text,expected",
    [
        ("10 years SQL experience", True),
        ("SQL experience: 12+ years", True),
        ("10 years of SQL", True),
        ("SQL for 10 yrs", True),
        ("SQL: 9.5 years", False),
        ("5-10 years SQL", False),
        ("5 to 10 years SQL", False),
        ("SQL 2020-2024", False),
        ("10 years Python, 2 years SQL", False),
        ("No 10 years SQL experience", False),
        ("Up to 15 years SQL", False),
        ("10 years PostgreSQL", False),
        ("", False),
    ],
)
def test_explicit_duration(text: str, expected: bool) -> None:
    assert shows_duration(text, text, "SQL", 10) is expected


def test_quote_negation_cannot_be_hidden_in_value() -> None:
    assert not shows_duration("10 years SQL", "No 10 years SQL", "SQL", 10)


@pytest.mark.parametrize(
    "quote,expected",
    [
        ("10 years SQL", True),
        ("SQL experience: 12+ years", True),
        ("No 10 years SQL", False),
        ("Up to 15 years SQL", False),
        ("5-10 years SQL", False),
        ("5 to 10 years SQL", False),
        ("2 years SQL", False),
        ("10 years Python, SQL", False),
        ("SQL", False),
    ],
)
def test_duration_can_be_recorded_only_in_quote(quote: str, expected: bool) -> None:
    assert shows_duration("SQL", quote, "SQL", 10) is expected
