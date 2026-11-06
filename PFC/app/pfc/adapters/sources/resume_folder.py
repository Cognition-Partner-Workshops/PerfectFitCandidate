"""REQ-IN-20..25 / REQ-ING-05: local resume discovery, not identity confirmation."""

import hashlib
import multiprocessing
import re
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from docx import Document
from pydantic import BaseModel, Field
from pypdf import PdfReader

from pfc.config import Settings

from .resume_paths import confined_path, read_resume, resume_files


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


def _extract_worker(path: Path, extractor: Callable[[Path], str], sender: Connection) -> None:
    try:
        sender.send((True, extractor(path)))
    except BaseException as exc:
        sender.send((False, type(exc).__name__))
    finally:
        sender.close()


def bounded_extract(
    path: Path,
    extractor: Callable[[Path], str],
    timeout: float,
    *,
    content: bytes,
    work_dir: Path,
) -> tuple[str, list[str]]:
    work_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="pfc-cv-", dir=work_dir) as directory:
        snapshot = Path(directory) / path.name
        snapshot.write_bytes(content)
        return _run_extractor(snapshot, extractor, timeout)


def _run_extractor(
    path: Path, extractor: Callable[[Path], str], timeout: float
) -> tuple[str, list[str]]:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_extract_worker, args=(path, extractor, sender), daemon=True)
    try:
        worker.start()
        sender.close()
        if not receiver.poll(timeout):
            return "", ["extraction_timeout"]
        success, value = receiver.recv()
        if not success:
            return "", [f"extraction failed: {value}"]
        return value, [] if value else ["no extractable text"]
    except (EOFError, OSError) as exc:
        return "", [f"extraction failed: {type(exc).__name__}"]
    finally:
        receiver.close()
        sender.close()
        if worker.pid is not None:
            worker.join(timeout=0.1)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=1)
            if worker.is_alive():
                worker.kill()
                worker.join()
            worker.close()


class ResumeFolderSource:
    """REQ-IN-21/22/24/25: scan only selected candidates; retain all matching versions."""

    def __init__(
        self,
        root: Path,
        *,
        max_file_bytes: int = 20 * 1024 * 1024,
        extract_timeout_s: float = 30,
        extractor: Callable[[Path], str] | None = None,
        work_dir: Path | None = None,
    ) -> None:
        self.root = root.resolve()
        self.max_file_bytes = max_file_bytes
        self.extract_timeout_s = extract_timeout_s
        self.extractor = extractor or extract_text
        self.work_dir = (
            work_dir if work_dir is not None else Settings().data_dir / "work" / "cv-extraction"
        )
        self._extracted: dict[str, tuple[str, list[str]]] = {}

    def scan(self, candidates: list[CandidateLookup]) -> list[ResumeResult]:
        if not self.root.is_dir():
            raise ValueError("Resume root must be an existing directory")
        results = [ResumeResult(candidate_id=c.candidate_id) for c in candidates]
        if not candidates:
            return results
        cached = [
            {
                safe.relative_to(self.root).as_posix()
                for stored in candidate.source_paths
                if (safe := confined_path(self.root, stored)) is not None
            }
            for candidate in candidates
        ]
        for path in sorted(resume_files(self.root)):
            relative = path.relative_to(self.root).as_posix()
            try:
                content, stat = read_resume(self.root, relative, self.max_file_bytes)
                digest = hashlib.sha256(content).hexdigest() if content is not None else ""
                if content is None:
                    text, flags = "", ["too_large"]
                else:
                    if digest not in self._extracted:
                        self._extracted[digest] = bounded_extract(
                            path,
                            self.extractor,
                            self.extract_timeout_s,
                            content=content,
                            work_dir=self.work_dir,
                        )
                    text, flags = self._extracted[digest]
            except OSError as exc:
                for result in results:
                    result.checks.append(f"{relative}: unreadable ({type(exc).__name__})")
                continue
            text_lower = text.lower()
            filename_lower = path.stem.lower()
            matches: list[tuple[int, Literal["email_exact", "manual", "name_fuzzy"]]] = []
            for index, candidate in enumerate(candidates):
                email = (candidate.email_address or "").strip().lower()
                email_pattern = (
                    re.compile(
                        r"(?<![a-z0-9._+-])" + re.escape(email) + r"(?![a-z0-9-]|\.[a-z0-9])"
                    )
                    if email
                    else None
                )
                names = [_tokens(candidate.short_name), _tokens(candidate.display_name)]
                if email_pattern and (
                    email_pattern.search(filename_lower) or email_pattern.search(text_lower)
                ):
                    matches.append((index, "email_exact"))
                elif relative in cached[index]:
                    matches.append((index, "manual"))
                elif any(tokens and tokens <= _tokens(path.stem) for tokens in names):
                    matches.append((index, "name_fuzzy"))
            if not matches:
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
