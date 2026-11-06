"""Real-path confinement for resume discovery and stored provenance."""

import os
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath


def confined_path(root: Path, relative: str) -> Path | None:
    windows = PureWindowsPath(relative)
    parts = relative.replace("\\", "/").split("/")
    if (
        not relative
        or windows.drive
        or windows.root
        or ".." in parts
        or Path(relative).is_absolute()
    ):
        return None
    try:
        base = root.resolve(strict=True)
        path = base
        for part in parts:
            path /= part
            if path.is_symlink() or path.is_junction():
                return None
        real = path.resolve(strict=True)
        return real if real.is_relative_to(base) else None
    except (OSError, ValueError, RuntimeError):
        return None


def resume_files(root: Path) -> Iterator[Path]:
    base = root.resolve(strict=True)
    for current, directories, files in os.walk(base, followlinks=False):
        directory = Path(current)
        directories[:] = sorted(
            name
            for name in directories
            if confined_path(base, (directory / name).relative_to(base).as_posix()) is not None
        )
        for name in sorted(files):
            path = confined_path(base, (directory / name).relative_to(base).as_posix())
            if (
                path is not None
                and path.is_file()
                and not name.startswith("~$")
                and path.suffix.lower() in {".txt", ".docx", ".pdf"}
            ):
                yield path
