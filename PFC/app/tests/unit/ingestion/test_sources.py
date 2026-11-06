"""Focused REQ-IN-20..25 / REQ-ING-04/05 source tests; synthetic identities only."""

import hashlib
import os
from pathlib import Path

import pytest
from docx import Document
from pydantic import ValidationError
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from pfc.adapters.sources.resume_folder import CandidateLookup, ResumeFolderSource, extract_text
from pfc.adapters.sources.watch_folder import WatchFolderSource


def candidate(**changes: object) -> CandidateLookup:
    return CandidateLookup(candidate_id="1", **changes)


@pytest.mark.parametrize(
    "filename,text,identity,method",
    [
        ("alex@example.com.txt", "SQL", {"email_address": "alex@example.com"}, "email_exact"),
        (
            "profile.txt",
            "SQL alex@example.com",
            {"email_address": "alex@example.com"},
            "email_exact",
        ),
        ("Alex_Taylor_profile.txt", "SQL", {"short_name": "Alex Taylor"}, "name_fuzzy"),
        ("Taylor_Alex_profile.txt", "SQL", {"display_name": "Alex Taylor"}, "name_fuzzy"),
        ("profile.txt", "SQL", {"source_paths": ["nested/profile.txt"]}, "manual"),
    ],
)
def test_req_in_25_identity_signals(
    tmp_path: Path, filename: str, text: str, identity: dict[str, object], method: str
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / filename).write_text(text, encoding="utf-8")
    (result,) = ResumeFolderSource(tmp_path).scan([candidate(**identity)])
    assert len(result.versions) == 1
    assert result.versions[0].identity_match == method
    assert result.versions[0].source_path == f"nested/{filename}"


def test_req_in_25_ambiguous_candidates(tmp_path: Path) -> None:
    (tmp_path / "Alex.txt").write_text("SQL alex@example.com", encoding="utf-8")
    results = ResumeFolderSource(tmp_path).scan(
        [
            candidate(email_address="alex@example.com"),
            CandidateLookup(candidate_id="2", short_name="Alex"),
        ]
    )
    assert len(results) == 2
    assert all(result.versions[0].identity_match == "ambiguous" for result in results)


def test_req_in_25_email_boundary(tmp_path: Path) -> None:
    (tmp_path / "notalex@example.com.txt").write_text("notalex@example.com", encoding="utf-8")
    (result,) = ResumeFolderSource(tmp_path).scan([candidate(email_address="alex@example.com")])
    assert not result.versions
    assert result.checks == ["CV not found"]


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("alex@example.com.au.pdf", False),
        ("alex@example.com1.pdf", False),
        ("AlEx@Example.com.pdf", True),
        ("alex@example.com_CV.pdf", True),
        ("CV - alex@example.com.pdf", True),
    ],
)
def test_req_in_25_filename_email_exact_domain(
    tmp_path: Path, filename: str, expected: bool
) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(tmp_path / filename)
    (result,) = ResumeFolderSource(tmp_path).scan([candidate(email_address="alex@example.com")])
    assert bool(result.versions) is expected
    if expected:
        assert result.versions[0].identity_match == "email_exact"


def test_req_in_22_missing_cv_not_error(tmp_path: Path) -> None:
    (result,) = ResumeFolderSource(tmp_path).scan([candidate(short_name="Nobody")])
    assert result.checks == ["CV not found"]
    assert result.versions == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("alex@example.com_CV", True),
        ("CV - alex@example.com", True),
        ("alex@example.com.au", False),
        ("alex@example.com1", False),
    ],
)
def test_req_in_25_text_candidate_email_boundaries(
    tmp_path: Path, text: str, expected: bool
) -> None:
    (tmp_path / "profile.txt").write_text(text, encoding="utf-8")
    (result,) = ResumeFolderSource(tmp_path).scan([candidate(email_address="alex@example.com")])
    assert bool(result.versions) is expected


@pytest.mark.parametrize("filename", ["~$Alex.docx", "Alex.bin", "Alex.xlsx"])
def test_req_in_20_skip_lock_and_unsupported(tmp_path: Path, filename: str) -> None:
    (tmp_path / filename).write_bytes(b"not a resume")
    (result,) = ResumeFolderSource(tmp_path).scan([candidate(short_name="Alex")])
    assert result.versions == []


def test_req_in_23_docx_paragraph_table_header(tmp_path: Path) -> None:
    path = tmp_path / "Alex.docx"
    doc = Document()
    doc.add_paragraph("Python")
    doc.add_table(rows=1, cols=1).cell(0, 0).text = "Projects"
    doc.sections[0].header.paragraphs[0].text = "alex@example.com"
    doc.save(path)
    text = extract_text(path)
    assert all(word in text for word in ["Python", "Projects", "alex@example.com"])


def test_req_in_23_txt_extraction(tmp_path: Path) -> None:
    path = tmp_path / "Alex.txt"
    path.write_text("\ufeff SQL\nPython ", encoding="utf-8")
    assert extract_text(path) == "SQL\nPython"


def test_req_in_23_pdf_text_extraction(tmp_path: Path) -> None:
    path = tmp_path / "Alex.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 100 Td (SQL alex@example.com) Tj ET")
    page[NameObject("/Contents")] = stream
    writer.write(path)
    assert "SQL alex@example.com" in extract_text(path)


def test_req_in_23_empty_pdf_flag(tmp_path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(tmp_path / "Alex.pdf")
    (result,) = ResumeFolderSource(tmp_path).scan([candidate(short_name="Alex")])
    (version,) = result.versions
    assert version.text_norm == ""
    assert version.flags == ["no extractable text"]


@pytest.mark.parametrize("extension", ["pdf", "docx"])
def test_req_in_23_extraction_failure_filename_match_flag(tmp_path: Path, extension: str) -> None:
    path = tmp_path / f"Alex.{extension}"
    path.write_bytes(b"broken")
    (result,) = ResumeFolderSource(tmp_path).scan([candidate(short_name="Alex")])
    (version,) = result.versions
    assert version.text_norm == ""
    assert version.flags[0].startswith("extraction failed:")


@pytest.mark.parametrize("extension", ["pdf", "docx"])
def test_req_in_23_failed_extraction_cached_path_keeps_version(
    tmp_path: Path, extension: str
) -> None:
    name = f"profile.{extension}"
    (tmp_path / name).write_bytes(b"broken")
    (result,) = ResumeFolderSource(tmp_path).scan([candidate(source_paths=[name])])
    (version,) = result.versions
    assert version.source_path == name
    assert version.identity_match == "manual"
    assert version.text_norm == ""
    assert version.flags[0].startswith("extraction failed:")
    assert result.checks == []


def test_req_in_24_versions_sorted_metadata(tmp_path: Path) -> None:
    for name, timestamp, text in [("Alex_new.txt", 2000, "Python"), ("Alex_old.txt", 1000, "SQL")]:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        os.utime(path, (timestamp, timestamp))
    (result,) = ResumeFolderSource(tmp_path).scan([candidate(short_name="Alex")])
    assert [v.source_path for v in result.versions] == ["Alex_old.txt", "Alex_new.txt"]
    assert [v.file_mtime for v in result.versions] == [1000, 2000]
    for version in result.versions:
        data = (tmp_path / version.source_path).read_bytes()
        assert version.file_size == len(data)
        assert version.file_sha256 == hashlib.sha256(data).hexdigest()
        assert version.text_sha256 == hashlib.sha256(version.text_norm.encode()).hexdigest()


def test_req_in_21_empty_selection(tmp_path: Path) -> None:
    assert ResumeFolderSource(tmp_path).scan([]) == []


def test_req_ing_05_missing_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="existing directory"):
        ResumeFolderSource(tmp_path / "missing").scan([])


def test_req_in_23_unsupported_extraction(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        extract_text(tmp_path / "file.bin")


def test_req_ing_04_disabled_watch(tmp_path: Path) -> None:
    watch = WatchFolderSource(path=tmp_path)
    assert watch.enabled is False
    assert watch.poll() == []


def test_req_ing_04_enabled_watch_not_implemented(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError, match="Phase 2"):
        WatchFolderSource(path=tmp_path, enabled=True).poll()


@pytest.mark.parametrize("minutes", [0, -1])
def test_req_ing_04_poll_minutes_validation(tmp_path: Path, minutes: int) -> None:
    with pytest.raises(ValidationError):
        WatchFolderSource(path=tmp_path, poll_minutes=minutes)
