"""Spawn-safe extraction limits and child-process lifecycle."""

import multiprocessing
import time
from pathlib import Path

import pytest

from pfc.adapters.sources.resume_folder import CandidateLookup, ResumeFolderSource


def slow_extract(path: Path) -> str:
    time.sleep(30)
    return "should never finish"


def injected_extract(path: Path) -> str:
    return "injected SQL alex@example.com"


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
