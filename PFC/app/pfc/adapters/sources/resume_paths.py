"""Real-path confinement for resume discovery and stored provenance."""

import os
import stat
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


def read_resume(root: Path, relative: str, max_bytes: int) -> tuple[bytes | None, os.stat_result]:
    path = confined_path(root, relative)
    if path is None:
        raise OSError("Resume path is not confined")

    def checked_stat() -> os.stat_result:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & (
            stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise OSError("Resume is not a regular non-reparse file")
        return metadata

    checked = checked_stat()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("Opened resume is not a regular file")
        identity = (opened.st_dev, opened.st_ino)
        if identity != (checked.st_dev, checked.st_ino):
            raise OSError("Resume changed before opening")
        if confined_path(root, relative) != path:
            raise OSError("Resume escaped its root")
        current = checked_stat()
        real_stat = os.stat(path, follow_symlinks=False)
        if identity != (current.st_dev, current.st_ino) or identity != (
            real_stat.st_dev,
            real_stat.st_ino,
        ):
            raise OSError("Resume path no longer matches its descriptor")
        if opened.st_size > max_bytes:
            return None, opened
        content = source.read(max_bytes + 1)
        return (content if len(content) <= max_bytes else None), opened
