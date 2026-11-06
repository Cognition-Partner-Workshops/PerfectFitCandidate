"""REQ-IN-20..25 / REQ-ING-05: local resume discovery, not identity confirmation."""

import hashlib
import re
from pathlib import Path
from typing import Literal

from docx import Document
from pydantic import BaseModel, Field
from pypdf import PdfReader

from pfc.services.ingestion.models import file_sha256


class CandidateLookup(BaseModel):
    candidate_id: str
    short_name: str = ""
    display_name: str = ""
    email_address: str | None = None
    source_paths: list[str] = Field(default_factory=list)


class CVVersion(BaseModel):
    source_path: str
    file_mtime: float
    file_size: int
    file_sha256: str
    text_sha256: str
    text_norm: str
    identity_match: Literal["email_exact", "name_fuzzy", "manual", "ambiguous", "none"] = "none"
    flags: list[str] = Field(default_factory=list)


class ResumeResult(BaseModel):
    candidate_id: str
    versions: list[CVVersion] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)


def extract_text(path: Path) -> str:
    """REQ-IN-23: deterministic text only; no OCR or invented identity facts."""
    if path.suffix.lower() == ".txt":
        return path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if path.suffix.lower() == ".pdf":
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages).strip()
    if path.suffix.lower() == ".docx":
        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            parts.extend("\t".join(cell.text for cell in row.cells) for row in table.rows)
        for section in doc.sections:
            for header in (section.header, section.first_page_header, section.even_page_header):
                parts.extend(p.text for p in header.paragraphs)
                for table in header.tables:
                    parts.extend("\t".join(c.text for c in r.cells) for r in table.rows)
        return "\n".join(parts).strip()
    raise ValueError(f"Unsupported resume extension: {path.suffix}")


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", text.lower()))


class ResumeFolderSource:
    """REQ-IN-21/22/24/25: scan only selected candidates; retain all matching versions."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def scan(self, candidates: list[CandidateLookup]) -> list[ResumeResult]:
        if not self.root.is_dir():
            raise ValueError("Resume root must be an existing directory")
        results = [ResumeResult(candidate_id=c.candidate_id) for c in candidates]
        if not candidates:
            return results
        for path in sorted(self.root.rglob("*")):
            if (
                not path.is_file()
                or path.name.startswith("~$")
                or path.suffix.lower() not in {".txt", ".docx", ".pdf"}
            ):
                continue
            relative = path.relative_to(self.root).as_posix()
            flags: list[str] = []
            try:
                text = extract_text(path)
                if not text:
                    flags.append("no extractable text")
            except Exception as exc:
                text = ""
                flags.append(f"extraction failed: {type(exc).__name__}")
            text_lower = text.lower()
            filename_lower = path.stem.lower()
            matches: list[tuple[int, Literal["email_exact", "manual", "name_fuzzy"]]] = []
            for index, candidate in enumerate(candidates):
                email = (candidate.email_address or "").strip().lower()
                email_pattern = (
                    re.compile(r"(?<![a-z0-9.+-])" + re.escape(email) + r"(?![a-z0-9-]|\.[a-z0-9])")
                    if email
                    else None
                )
                names = [_tokens(candidate.short_name), _tokens(candidate.display_name)]
                if email_pattern and (
                    email_pattern.search(filename_lower) or email_pattern.search(text_lower)
                ):
                    matches.append((index, "email_exact"))
                elif relative in candidate.source_paths:
                    matches.append((index, "manual"))
                elif any(tokens and tokens <= _tokens(path.stem) for tokens in names):
                    matches.append((index, "name_fuzzy"))
            if not matches:
                continue
            try:
                stat = path.stat()
                digest = file_sha256(path)
            except OSError as exc:
                for index, _ in matches:
                    results[index].checks.append(f"{relative}: unreadable ({type(exc).__name__})")
                continue
            for index, method in matches:
                version = CVVersion(
                    source_path=relative,
                    file_mtime=stat.st_mtime,
                    file_size=stat.st_size,
                    file_sha256=digest,
                    text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    text_norm=text,
                    flags=flags,
                    identity_match="ambiguous" if len(matches) > 1 else method,
                )
                results[index].versions.append(version)
        for result in results:
            result.versions.sort(key=lambda v: (v.file_mtime, v.source_path))
            if not result.versions:
                result.checks.append("CV not found")
        return results
