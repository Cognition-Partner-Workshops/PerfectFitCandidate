"""Resume real-path confinement, including platform-specific reparse points."""

import os
import subprocess
from pathlib import Path

import pytest

from pfc.adapters.sources.resume_folder import CandidateLookup, ResumeFolderSource
from pfc.adapters.sources.resume_paths import confined_path, resume_files


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
