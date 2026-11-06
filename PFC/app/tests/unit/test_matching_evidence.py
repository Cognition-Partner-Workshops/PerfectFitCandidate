import pytest

from pfc.services.matching.export import evidence_parts


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", []),
        (" \t ", []),
        ("; ; ", []),
        (" SQL ; Python; ; Rust ; ", ["SQL", "Python", "Rust"]),
        ("first;second", ["first;second"]),
    ],
)
def test_evidence_parts(value: str, expected: list[str]) -> None:
    assert evidence_parts(value) == expected
