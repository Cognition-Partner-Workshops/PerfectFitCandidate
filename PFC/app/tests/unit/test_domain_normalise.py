"""REQ-IN-03, REQ-IN-12, REQ-MAT-05 normalisation contract."""

from datetime import date, datetime

import pytest

from pfc.domain.normalise import (
    SynonymRow,
    SynonymTable,
    normalise_term,
    normalise_text,
    parse_date,
    tokenise_skills,
)


@pytest.mark.parametrize("bullet", ["?", "•", "▪", "·", "\uf0b7", "*"])
def test_req_in_03_bullets(bullet):
    assert normalise_text(f"# **Skills**\r\n{bullet} SQL\xa0  and\u200b Python") == (
        "Skills\n- SQL and Python"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("  ＳＱＬ  ", "SQL"),
        ("\x60\x60\x60text\n__SQL__\n\x60\x60\x60", "SQL"),
        ("a  b\n\n c", "a b\nc"),
        ("question? text", "question? text"),
    ],
)
def test_req_in_03_text(raw, expected):
    assert normalise_text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" C++ ", "c++"),
        ("C#", "c#"),
        (".NET", ".net"),
        ("CI/CD", "ci/cd"),
        ("  Business,   Analysis! ", "business analysis"),
        ("Straße", "strasse"),
        ("", ""),
    ],
)
def test_req_mat_05_terms(raw, expected):
    assert normalise_term(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "Wed 4/11/2026",
        "04/11/2026",
        "4-11-2026",
        "2026-11-04",
        date(2026, 11, 4),
        datetime(2026, 11, 4, 12),
    ],
)
def test_req_in_12_valid_dates(raw):
    parsed = parse_date(raw)
    assert parsed.value == date(2026, 11, 4)
    assert parsed.status == "valid"
    assert parsed.raw == raw
    assert parsed.warning is None


@pytest.mark.parametrize("raw", ["16 Nov 2026", "16-Nov-2026"])
def test_req_in_12_month_names(raw):
    assert parse_date(raw).value == date(2026, 11, 16)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1, date(1900, 1, 1)),
        (59, date(1900, 2, 28)),
        (60, date(1900, 2, 28)),
        (61, date(1900, 3, 1)),
        ("61.75", date(1900, 3, 1)),
        (2958465, date(9999, 12, 31)),
    ],
)
def test_req_in_12_excel(raw, expected):
    assert parse_date(raw).value == expected
    assert bool(parse_date(raw).warning) == (raw == 60)


@pytest.mark.parametrize("raw", [None, "", "  \n"])
def test_req_in_12_blank(raw):
    assert parse_date(raw).status == "blank"


@pytest.mark.parametrize(
    "raw",
    [
        "bad",
        "31/02/2026",
        "11/16/2026",
        0,
        -1,
        2958466,
        10**1000,
        float("nan"),
        float("inf"),
        True,
        [],
        {},
    ],
)
def test_req_in_12_unparseable(raw):
    assert parse_date(raw).status == "unparseable"
    assert parse_date(raw).value is None


def test_req_in_12_weekday_warning():
    parsed = parse_date("Mon 4/11/2026")
    assert parsed.value == date(2026, 11, 4)
    assert parsed.warning


@pytest.fixture
def synonyms():
    return SynonymTable(
        [
            SynonymRow("Business Analysis", "BA", "abbreviation"),
            SynonymRow("Business Analysis", "Bussiness Analysis", "misspelling"),
            SynonymRow("SQL", "SQL"),
            SynonymRow("Python", "Python"),
            SynonymRow("CI/CD", "CI/CD"),
            SynonymRow("SQL", "query", "related", 0.6),
            SynonymRow("SQL", "pending", status="suggested"),
            SynonymRow("SQL", "rejected", status="rejected"),
        ]
    )


@pytest.mark.parametrize(
    ("term", "canonical"),
    [
        ("  ba ", "Business Analysis"),
        ("BUSINESS   ANALYSIS", "Business Analysis"),
        ("Bussiness Analysis", "Business Analysis"),
        ("query", None),
        ("pending", None),
        ("rejected", None),
        ("unknown", None),
        ("", None),
    ],
)
def test_req_mat_05_resolve(synonyms, term, canonical):
    assert synonyms.resolve(term) == canonical


def test_req_mat_05_related_expand_suggest(synonyms):
    assert synonyms.related("QUERY") == [("SQL", 0.6)]
    assert synonyms.related("unknown") == []
    assert synonyms.expand("BA") == {"business analysis", "ba", "bussiness analysis"}
    assert synonyms.expand("unknown") == set()
    assert synonyms.suggest("Business Analysi") == ["Business Analysis"]
    assert synonyms.resolve("Business Analysi") is None
    assert synonyms.suggest("unrecognisable") == []
    expanded = synonyms.expand("BA")
    expanded.clear()
    assert synonyms.expand("BA")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"relation": "bad"},
        {"status": "bad"},
        {"credit": -0.1},
        {"credit": 1.1},
        {"credit": float("nan")},
        {"credit": 0.5},
        {"canonical": ""},
        {"term": ""},
    ],
)
def test_req_mat_05_invalid_rows(kwargs):
    data = {"canonical": "SQL", "term": "SQL", **kwargs}
    with pytest.raises(ValueError):
        SynonymRow(**data)


def test_req_mat_05_ambiguous_rows():
    with pytest.raises(ValueError, match="Ambiguous"):
        SynonymTable([SynonymRow("SQL", "X"), SynonymRow("Python", "X")])
    table = SynonymTable([SynonymRow("SQL", "X", status="suggested")])
    assert table.resolve("SQL") is None


@pytest.mark.parametrize(
    ("text", "raws", "empties"),
    [
        ("SQL, ,Python;", ["SQL", "Python"], 2),
        ("BA; Business Analysis", ["BA"], 0),
        ("SQL/Python", ["SQL", "Python"], 0),
        ("SQL and Python", ["SQL", "Python"], 0),
        ("SQL/Python/BA", ["SQL", "Python", "BA"], 0),
        ("CI/CD", ["CI/CD"], 0),
        ("SQL/unknown", ["SQL/unknown"], 0),
        ("SQL and unknown", ["SQL and unknown"], 0),
        (
            "Python, MLOps\nSoftware Architecture\r\nDocker",
            ["Python", "MLOps", "Software Architecture", "Docker"],
            0,
        ),
        ("Python\n\nDocker", ["Python", "Docker"], 1),
        ("", [], 1),
        (" ,;", [], 3),
        ("SQL,sql", ["SQL"], 0),
    ],
)
def test_fr_ing_11_tokenise(synonyms, text, raws, empties):
    result = tokenise_skills(text, synonyms)
    assert [token.raw for token in result.tokens] == raws
    assert result.empty_count == empties
    for token in result.tokens:
        assert token.normalised == normalise_term(token.raw)
        assert token.canonical == synonyms.resolve(token.raw)
