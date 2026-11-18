import io
import re
from datetime import UTC, datetime
from typing import Any

import pytest
from pypdf import PdfReader

from pfc.api.schemas.reports import PdfRequest
from pfc.services.reports import builder
from pfc.services.reports.builder import (
    DISCLAIMER,
    Chart,
    build_pdf,
    date_text,
    display,
    drawing,
    match_pct,
    register_fonts,
)
from pfc.services.reports.data import ReportData


@pytest.mark.parametrize(
    ("score", "expected"), [(63.1, "63 %"), (89.5, "90 %"), (0, "0 %"), (100, "100 %")]
)
def test_frontend_score_rounding(score: float, expected: str) -> None:
    assert match_pct(score) == expected


def test_formatting_and_masking() -> None:
    assert date_text("2026-11-06") == "06 Nov 2026"
    assert date_text(None) == "Not recorded"
    assert date_text("bad") == "Unparseable: bad"
    assert display("   ") == "Not recorded"
    assert (
        display(["person@example.com", 2.0, True, None])
        == "pe****@example.com; 2; Yes; Not recorded"
    )


@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
def test_empty_pdf_fonts_footer_and_orientation(orientation: str) -> None:
    request = PdfRequest.model_validate({"report": "match_results", "orientation": orientation})
    data = ReportData(
        request, "reporter@example.com", datetime(2026, 11, 6, tzinfo=UTC), {}, [], 0, [], []
    )
    reader = PdfReader(io.BytesIO(build_pdf(data)))
    assert len(reader.pages) == 2
    for number, page in enumerate(reader.pages, 1):
        text = page.extract_text()
        assert DISCLAIMER in text
        assert f"Page {number}/2" in text
        assert (page.mediabox.width > page.mediabox.height) == (orientation == "landscape")
    text = " ".join(page.extract_text() for page in reader.pages)
    assert "No results for this selection." in text
    assert "reporter@example.com" not in text
    assert "re******@example.com" in text
    fonts = [
        font.get_object()
        for page in reader.pages
        for font in page["/Resources"]["/Font"].get_object().values()
    ]
    embedded = [font for font in fonts if "/FontDescriptor" in font]
    assert embedded and all("Vera" in font["/BaseFont"] for font in embedded)
    assert all("/FontFile2" in font["/FontDescriptor"].get_object() for font in embedded)


def test_report_orientation_defaults_and_override() -> None:
    assert PdfRequest(report="dashboard").orientation == "portrait"
    assert PdfRequest(report="match_results").orientation == "landscape"
    assert PdfRequest(report="match_results", orientation="portrait").orientation == "portrait"


def test_chart_units_and_single_series_labels() -> None:
    register_fonts()
    assert (
        drawing(
            Chart("DB-01 KPIs", ["Metric", "Value"], [["People", "2"], ["Coverage", "50 %"]]), 500
        )
        is None
    )
    assert drawing(Chart("DB-11 Coverage", ["Coverage %"], [["50 %"]]), 500) is None
    assert (
        drawing(
            Chart("DB-03 Counts", ["Label", "Count"], [["SQL", "2"], ["Python", "Not recorded"]]),
            500,
        )
        is None
    )
    chart = drawing(Chart("DB-03 Counts", ["Label", "Value"], [["SQL", "2"], ["Python", "3"]]), 500)
    assert chart is not None
    labels = [getattr(shape, "text", "") for shape in chart.contents]
    assert "SQL" in labels and "Python" in labels
    assert not any(" · " in label for label in labels)
    chart = drawing(
        Chart(
            "DB-30 Override rate per run",
            ["Run", "Pairs", "Overridden", "Rate %"],
            [["1", "10000", "2500", "25"], ["2", "100", "50", "50"]],
        ),
        500,
    )
    assert chart is not None
    labels = [getattr(shape, "text", "") for shape in chart.contents]
    assert "25" in labels and "50" in labels and "10000" not in labels and "2500" not in labels


def test_summary_and_single_values_use_tiles(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        Chart("DB-01 KPIs", ["Metric", "Value"], [["People", "2"], ["Coverage", "50 %"]]),
        Chart("DB-11 Match coverage", ["Coverage %"], [["50 %"]]),
    ]
    monkeypatch.setattr(builder, "catalogue", lambda _: {"summary": items})

    def no_chart(*_: Any) -> None:
        pytest.fail("KPIs and single values must not use bar charts")

    monkeypatch.setattr(builder, "drawing", no_chart)
    data = ReportData(
        PdfRequest(report="dashboard", sections=["summary"]),
        "Reviewer",
        datetime(2026, 11, 6, tzinfo=UTC),
        {},
        [],
        0,
        [],
        [],
    )
    text = re.sub(
        r"\s+",
        " ",
        " ".join(p.extract_text() for p in PdfReader(io.BytesIO(build_pdf(data))).pages),
    )
    assert "People 2" in text and "Coverage 50 %" in text and "Coverage % 50 %" in text


def results_data(
    count: int, *, orientation: str = "landscape", evidence: str = "Skills matched"
) -> ReportData:
    request = PdfRequest.model_validate({"report": "match_results", "orientation": orientation})
    rows = [
        {
            "requisition": "T10000",
            "rank": i + 1,
            "potential_person": f"Person {i + 1:03d}",
            "person_email": "person@example.com",
            "account": "Account 1",
            "role": "Analyst role",
            "start_date": "2026-11-06",
            "role_location": "Manchester",
            "demand_grade": "C1",
            "person_grade": "C2",
            "person_location": "Leeds",
            "score": 91,
            "effective_band": "Strong",
            "location_assessment": "Aligned",
            "matching_evidence": evidence,
            "gaps_checks": f"Check candidate {i + 1:03d}",
            "eligibility_evidence": "Grade eligible",
        }
        for i in range(count)
    ]
    return ReportData(
        request, "Reviewer", datetime(2026, 11, 6, tzinfo=UTC), {}, rows, count, [], []
    )


@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
def test_compact_table_repeats_headers_and_keeps_evidence(orientation: str) -> None:
    reader = PdfReader(io.BytesIO(build_pdf(results_data(30, orientation=orientation))))
    assert len(reader.pages) >= 3
    texts = [re.sub(r"\s+", " ", p.extract_text()) for p in reader.pages]
    for page in texts[1:]:
        for header in (
            "Requisition",
            "Rank",
            "Name",
            "Email (masked)",
            "Demand grade",
            "Person grade",
            "Location",
            "Match %",
            "Band",
            "Location assessment",
        ):
            assert header in page
        for number in range(1, 31):
            if f"Person {number:03d}" in page:
                assert f"Check candidate {number:03d}" in page
    text = " ".join(texts)
    assert text.count("Account Account 1") == 1
    assert text.count("Role Analyst role") == 1
    assert text.count("Start date 06 Nov 2026") == 1
    assert "person@example.com" not in text
    assert "pe****@example.com" in text
    for number in range(1, 31):
        assert f"Person {number:03d}" in text and f"Check candidate {number:03d}" in text


def test_oversized_evidence_wraps_across_pages() -> None:
    data = results_data(1, evidence="Detailed evidence " * 4000 + "END_OF_EVIDENCE")
    reader = PdfReader(io.BytesIO(build_pdf(data)))
    assert len(reader.pages) > 3
    assert "END_OF_EVIDENCE" in " ".join(p.extract_text() for p in reader.pages)


def test_result_cells_escape_unicode_and_wrap_long_values() -> None:
    data = results_data(2)
    data.results[0]["potential_person"] = "Renée <Reviewer>"
    data.results[1]["potential_person"] = "Long candidate name " * 8
    data.results[1]["person_location"] = "Manchester / Leeds / London / Birmingham"
    reader = PdfReader(io.BytesIO(build_pdf(data)))
    text = re.sub(r"\s+", " ", " ".join(p.extract_text() for p in reader.pages))
    assert "Renée <Reviewer>" in text
    assert "Long candidate name " * 7 + "Long candidate name" in text
    assert "Manchester / Leeds / London / Birmingham" in text


def test_paragraph_cache_preserves_reflow(monkeypatch: pytest.MonkeyPatch) -> None:
    register_fonts()
    original = builder.Paragraph.wrap
    calls = []

    def tracked(self: Any, width: float, height: float) -> Any:
        calls.append(width)
        return original(self, width, height)

    monkeypatch.setattr(builder.Paragraph, "wrap", tracked)
    style = builder.ParagraphStyle("cache-test", fontName="PFC", fontSize=7, leading=10)
    text = "Wrapped words " * 30
    paragraph = builder.ReportParagraph(text, style)
    first = paragraph.wrap(100, 500)
    assert paragraph.wrap(100, 30) == first
    assert calls == [100]
    wider = paragraph.wrap(200, 500)
    assert wider[1] < first[1] and calls == [100, 200]
    assert paragraph.wrap(100, 500) == first
    parts = paragraph.split(100, 40)
    assert len(parts) == 2
    for part in parts:
        assert part.wrap(100, 500)[1] > 0
