"""Spawn-safe extraction limits and child-process lifecycle."""

import hashlib
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from pfc.adapters.sources import resume_folder
from pfc.adapters.sources.resume_folder import CandidateLookup, ResumeFolderSource


def slow_extract(path: Path) -> str:
    time.sleep(30)
    return "should never finish"


def injected_extract(path: Path) -> str:
    return "injected SQL alex@example.com"


def read_or_fail(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text == "broken":
        raise ValueError("Synthetic extraction error")
    return text


def test_timeout_terminates_worker_and_flags_result(tmp_path: Path) -> None:
    (tmp_path / "Alex.pdf").write_bytes(b"synthetic")
    before = {p.pid for p in multiprocessing.active_children()}
    started = time.monotonic()
    (result,) = ResumeFolderSource(tmp_path, extractor=slow_extract, extract_timeout_s=0.2).scan(
        [CandidateLookup(candidate_id="1", short_name="Alex")]
    )
    assert time.monotonic() - started < 4
    assert {p.pid for p in multiprocessing.active_children()} == before
    assert result.versions[0].flags == ["extraction_timeout"]
    assert result.versions[0].text_norm == ""


def test_injected_extractor_is_picklable_under_spawn(tmp_path: Path) -> None:
    (tmp_path / "profile.pdf").write_bytes(b"synthetic")
    (result,) = ResumeFolderSource(tmp_path, extractor=injected_extract).scan(
        [CandidateLookup(candidate_id="1", email_address="alex@example.com")]
    )
    assert result.versions[0].text_norm == "injected SQL alex@example.com"
    assert result.versions[0].flags == []


@pytest.mark.parametrize("extension", ["pdf", "docx"])
def test_oversized_cv_is_flagged_without_starting_extractor(
    tmp_path: Path, extension: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / f"Alex.{extension}").write_bytes(b"x" * 11)

    def must_not_start(*args, **kwargs):
        pytest.fail("Oversized files must not start an extractor process")

    monkeypatch.setattr(multiprocessing, "get_context", must_not_start)
    (result,) = ResumeFolderSource(tmp_path, max_file_bytes=10).scan(
        [CandidateLookup(candidate_id="1", short_name="Alex")]
    )
    assert result.versions[0].flags == ["too_large"]
    assert result.versions[0].file_sha256 == ""


def test_empty_folder_does_not_start_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_start(*args, **kwargs):
        pytest.fail("An empty folder needs no workers")

    monkeypatch.setattr(multiprocessing, "get_context", must_not_start)
    (result,) = ResumeFolderSource(tmp_path).scan([CandidateLookup(candidate_id="1")])
    assert result.versions == []
    assert result.checks == ["CV not found"]


def test_changed_bytes_refresh_identity_even_when_mtime_and_size_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "profile.txt"
    first = b"alex@example.com SQL"
    changed = b"jane@example.com SQL"
    path.write_bytes(first)
    original_stat = path.stat()
    candidates = [
        CandidateLookup(candidate_id="1", email_address="alex@example.com"),
        CandidateLookup(candidate_id="2", email_address="jane@example.com"),
    ]
    source = ResumeFolderSource(tmp_path)
    extracted: list[bytes] = []
    extract = resume_folder.bounded_extract

    def tracked_extract(path, extractor, timeout, *, content):
        extracted.append(content)
        return extract(path, extractor, timeout, content=content)

    monkeypatch.setattr(resume_folder, "bounded_extract", tracked_extract)
    alex, jane = source.scan(candidates)
    assert alex.versions[0].text_norm == first.decode()
    assert jane.versions == []
    path.write_bytes(changed)
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert path.stat().st_size == original_stat.st_size
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
    alex, jane = source.scan(candidates)
    assert alex.versions == []
    assert alex.checks == ["CV not found"]
    assert jane.versions[0].text_norm == changed.decode()
    assert jane.versions[0].file_sha256 == hashlib.sha256(changed).hexdigest()
    assert jane.versions[0].identity_match == "email_exact"
    assert jane.versions[0].flags == []
    assert source.scan(candidates) == [alex, jane]
    assert extracted == [first, changed]


@pytest.mark.parametrize(
    "initial,limit,flag",
    [
        (b"broken", 100, "extraction failed: ValueError"),
        (b"oversized", 6, "too_large"),
    ],
)
def test_changed_file_clears_cached_failure_flags(
    tmp_path: Path, initial: bytes, limit: int, flag: str
) -> None:
    path = tmp_path / "Alex.pdf"
    path.write_bytes(initial)
    source = ResumeFolderSource(tmp_path, extractor=read_or_fail, max_file_bytes=limit)
    identity = [CandidateLookup(candidate_id="1", short_name="Alex")]
    (before,) = source.scan(identity)
    assert before.versions[0].flags == [flag]
    path.write_bytes(b"usable")
    (after,) = source.scan(identity)
    assert after.versions[0].flags == []
    assert after.versions[0].text_norm == "usable"
    assert after.versions[0].file_sha256 == hashlib.sha256(b"usable").hexdigest()
