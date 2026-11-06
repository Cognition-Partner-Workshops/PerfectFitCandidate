"""REQ-IN-20..25 and REQ-ING-04/05: synthetic local resume sources."""

import hashlib
from pathlib import Path

import pytest
from docx import Document
from pypdf import PdfWriter

from pfc.adapters.sources.resume_folder import CandidateLookup, ResumeFolderSource, extract_text
from pfc.adapters.sources.watch_folder import WatchFolderSource


def test_req_in_20_25_resume_versions_and_ambiguity(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "alex@example.com.txt").write_text("SQL\nalex@example.com", encoding="utf-8")
    doc = Document()
    doc.add_paragraph("Python")
    doc.add_table(rows=1, cols=1).cell(0, 0).text = "Projects"
    doc.sections[0].header.paragraphs[0].text = "alex@example.com"
    doc.save(nested / "Alex_Profile.docx")
    pdf = PdfWriter()
    pdf.add_blank_page(width=100, height=100)
    pdf.write(nested / "Alex_scan.pdf")
    (nested / "~$Alex.docx").write_bytes(b"lock")
    (nested / "Alex_broken.docx").write_bytes(b"broken")
    (nested / "unrelated.txt").write_text("unrelated", encoding="utf-8")
    candidates = [
        CandidateLookup(candidate_id="1", short_name="Alex", email_address="alex@example.com"),
        CandidateLookup(candidate_id="2", short_name="Alex"),
        CandidateLookup(candidate_id="3", short_name="Nobody"),
    ]
    results = ResumeFolderSource(tmp_path).scan(candidates)
    assert len(results[0].versions) == 4
    assert results[2].checks == ["CV not found"]
    assert all(v.identity_match == "ambiguous" for v in results[0].versions)
    doc_version = next(v for v in results[0].versions if v.source_path.endswith("Profile.docx"))
    assert all(word in doc_version.text_norm for word in ["Python", "Projects", "alex@example.com"])
    assert doc_version.text_sha256 == hashlib.sha256(doc_version.text_norm.encode()).hexdigest()
    assert len(doc_version.file_sha256) == 64
    assert doc_version.file_size > 0
    assert doc_version.file_mtime > 0
    assert next(v for v in results[0].versions if v.source_path.endswith(".pdf")).flags == [
        "no extractable text"
    ]
    assert (
        "extraction failed"
        in next(v for v in results[0].versions if "broken" in v.source_path).flags[0]
    )


def test_req_in_25_email_boundary_and_cached_path(tmp_path: Path) -> None:
    (tmp_path / "cv.txt").write_text("notalex@example.com", encoding="utf-8")
    source = ResumeFolderSource(tmp_path)
    candidate = CandidateLookup(candidate_id="1", email_address="alex@example.com")
    assert source.scan([candidate])[0].checks == ["CV not found"]
    candidate.source_paths = ["cv.txt"]
    assert source.scan([candidate])[0].versions[0].identity_match == "manual"
    (tmp_path / "cv.txt").write_text("alex@example.com", encoding="utf-8")
    assert source.scan([candidate])[0].versions[0].identity_match == "email_exact"


def test_req_ing_04_05_empty_errors_and_disabled_watch(tmp_path: Path) -> None:
    assert ResumeFolderSource(tmp_path).scan([]) == []
    assert ResumeFolderSource(tmp_path).scan([CandidateLookup(candidate_id="1")])[0].checks == [
        "CV not found"
    ]
    with pytest.raises(ValueError, match="existing directory"):
        ResumeFolderSource(tmp_path / "missing").scan([])
    with pytest.raises(ValueError, match="Unsupported"):
        extract_text(tmp_path / "file.bin")
    watch = WatchFolderSource(path=tmp_path)
    assert watch.enabled is False
    assert watch.poll() == []
    watch.enabled = True
    with pytest.raises(NotImplementedError, match="Phase 2"):
        watch.poll()
