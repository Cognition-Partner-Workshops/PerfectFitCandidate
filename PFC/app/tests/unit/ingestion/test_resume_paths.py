"""Resume real-path confinement, including platform-specific reparse points."""

import hashlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from pfc.adapters.sources import resume_folder
from pfc.adapters.sources.resume_folder import CandidateLookup, ResumeFolderSource
from pfc.adapters.sources.resume_paths import confined_path, read_resume, resume_files


def test_skip_symlink_files_and_directories_even_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "Alex.txt").write_text("inside", encoding="utf-8")
    (outside / "Alex.txt").write_text("outside", encoding="utf-8")
    try:
        (root / "Alex outside.txt").symlink_to(outside / "Alex.txt")
        (root / "Alex inside.txt").symlink_to(root / "Alex.txt")
        (root / "escape").symlink_to(outside, target_is_directory=True)
        (root / "loop").symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable")
    assert list(resume_files(root)) == [root / "Alex.txt"]
    (result,) = ResumeFolderSource(root).scan(
        [CandidateLookup(candidate_id="1", short_name="Alex")]
    )
    assert [v.source_path for v in result.versions] == ["Alex.txt"]
    assert result.versions[0].text_norm == "inside"
    assert confined_path(root, "escape/Alex.txt") is None


@pytest.mark.parametrize(
    "relative",
    [
        "../Alex.txt",
        "nested/../../Alex.txt",
        "/Alex.txt",
        "C:/Alex.txt",
        "C:Alex.txt",
        r"..\Alex.txt",
        r"\\server\share\Alex.txt",
        r"\Alex.txt",
        "missing.txt",
    ],
)
def test_reject_unsafe_cached_paths(tmp_path: Path, relative: str) -> None:
    assert confined_path(tmp_path, relative) is None
    with pytest.raises(OSError):
        read_resume(tmp_path, relative, 1024)


def test_unsafe_cached_path_does_not_supply_identity(tmp_path: Path) -> None:
    (tmp_path / "profile.txt").write_text("SQL", encoding="utf-8")
    (result,) = ResumeFolderSource(tmp_path).scan(
        [
            CandidateLookup(
                candidate_id="1", source_paths=["../profile.txt", str(tmp_path / "profile.txt")]
            )
        ]
    )
    assert result.versions == []
    assert result.checks == ["CV not found"]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_skip_windows_junction(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Alex.txt").write_text("outside", encoding="utf-8")
    junction = root / "linked"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True
    )
    if created.returncode:
        pytest.skip("Junction creation is unavailable")
    assert junction.is_junction()
    assert list(resume_files(root)) == []
    assert confined_path(root, "linked/Alex.txt") is None


@pytest.mark.parametrize("stage", ["before_open", "after_open", "parent_before_open"])
@pytest.mark.parametrize("nofollow", [True, False])
def test_descriptor_rejects_symlink_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, nofollow: bool
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    nested = root / "nested"
    nested.mkdir()
    path = nested / "Alex.txt"
    path.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "Alex.txt"
    external.write_text("outside", encoding="utf-8")
    probe = root / "probe"
    try:
        probe.symlink_to(outside, target_is_directory=True)
        probe.unlink()
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable")
    original_open = os.open
    if not nofollow:
        monkeypatch.setattr(os, "O_NOFOLLOW", 0, raising=False)

    def swapped_open(
        name: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if Path(name) != path:
            return original_open(name, flags, mode, dir_fd=dir_fd)
        if stage == "after_open":
            fd = original_open(name, flags, mode, dir_fd=dir_fd)
            try:
                path.unlink()
                path.symlink_to(external)
            except BaseException:
                os.close(fd)
                raise
            return fd
        if stage == "parent_before_open":
            nested.rename(root / "original")
            nested.symlink_to(outside, target_is_directory=True)
        else:
            path.unlink()
            path.symlink_to(external)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapped_open)
    (result,) = ResumeFolderSource(root).scan(
        [CandidateLookup(candidate_id="1", short_name="Alex")]
    )
    assert result.versions == []
    assert any("nested/Alex.txt: unreadable" in check for check in result.checks)


def test_descriptor_rejects_regular_file_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "Alex.txt"
    path.write_bytes(b"original")
    original_open = os.open

    def replaced_open(
        name: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if Path(name) == path:
            path.rename(tmp_path / "original.txt")
            path.write_bytes(b"replacement")
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replaced_open)
    with pytest.raises(OSError, match="changed before opening"):
        read_resume(tmp_path, "Alex.txt", 1024)


def test_hash_and_extraction_use_the_same_opened_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "Alex.txt"
    content = b"inside alex@example.com"
    path.write_bytes(content)
    extract = resume_folder.bounded_extract

    def replace_before_extract(
        source: Path,
        extractor: Callable[[Path], str],
        timeout: float,
        *,
        content: bytes,
        work_dir: Path,
    ) -> tuple[str, list[str]]:
        source.write_text("outside other@example.com", encoding="utf-8")
        return extract(source, extractor, timeout, content=content, work_dir=work_dir)

    monkeypatch.setattr(resume_folder, "bounded_extract", replace_before_extract)
    (result,) = ResumeFolderSource(tmp_path).scan(
        [CandidateLookup(candidate_id="1", email_address="alex@example.com")]
    )
    version = result.versions[0]
    assert version.text_norm == content.decode()
    assert version.file_sha256 == hashlib.sha256(content).hexdigest()
    assert version.file_size == len(content)
    assert version.identity_match == "email_exact"
    assert path.read_text() == "outside other@example.com"
