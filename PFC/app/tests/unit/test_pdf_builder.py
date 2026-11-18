import io
from datetime import UTC, datetime

import pytest
from pypdf import PdfReader

from pfc.api.schemas.reports import PdfRequest
from pfc.services.reports.builder import DISCLAIMER, build_pdf, date_text, display, match_pct
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
