"""Non-recursive local workbook stability detection."""

import fnmatch
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path

NOT_SYNCED = 0x1000 | 0x40000 | 0x400000
REPARSE_POINT = 0x400


class WatchFolderSource:
    def __init__(
        self,
        path: Path,
        pattern: str,
        settle_seconds: float = 10,
        clock: Callable[[], float] = time.monotonic,
        stat_file: Callable[[Path], os.stat_result] = lambda p: p.lstat(),
        read_file: Callable[[Path], bytes] = lambda p: p.read_bytes(),
    ) -> None:
        self.path, self.pattern = path, pattern
        self.settle_seconds, self.clock = settle_seconds, clock
        self.stat_file, self.read_file = stat_file, read_file
        self.observed: dict[str, tuple[int, int, float]] = {}
        self.pending: dict[str, str] = {}

    def scan(self, now: float | None = None) -> list[tuple[str, bytes]]:
        now = self.clock() if now is None else now
        files = list(self.path.iterdir())
        names = {p.name.casefold() for p in files}
        pending: dict[str, str] = {}
        seen: set[str] = set()
        ready = []
        for path in files:
            name = path.name
            if name.startswith("~$") or not fnmatch.fnmatchcase(
                name.casefold(), self.pattern.casefold()
            ):
                continue
            seen.add(name)
            try:
                before = self.stat_file(path)
                attributes = getattr(before, "st_file_attributes", 0)
                if not stat.S_ISREG(before.st_mode) or attributes & REPARSE_POINT:
                    self.observed.pop(name, None)
                    continue
                if attributes & NOT_SYNCED:
                    pending[name] = "not synced"
                    self.observed.pop(name, None)
                    continue
                if ("~$" + name).casefold() in names:
                    pending[name] = "locked"
                    self.observed.pop(name, None)
                    continue
                signature = (before.st_size, before.st_mtime_ns)
                previous = self.observed.get(name)
                if previous is None or previous[:2] != signature:
                    self.observed[name] = (*signature, now)
                    pending[name] = "settling"
                    continue
                if now - previous[2] < self.settle_seconds:
                    pending[name] = "settling"
                    continue
                data = self.read_file(path)
                after = self.stat_file(path)
                if (after.st_size, after.st_mtime_ns) != signature or len(data) != after.st_size:
                    self.observed[name] = (after.st_size, after.st_mtime_ns, now)
                    pending[name] = "settling"
                    continue
                if not stat.S_ISREG(after.st_mode) or getattr(after, "st_file_attributes", 0) & (
                    NOT_SYNCED | REPARSE_POINT
                ):
                    self.observed.pop(name, None)
                    pending[name] = "not synced"
                    continue
                if (self.path / ("~$" + name)).exists():
                    pending[name] = "locked"
                    continue
                ready.append((name, data))
            except OSError:
                pending[name] = "unreadable"
                self.observed.pop(name, None)
        self.observed = {name: value for name, value in self.observed.items() if name in seen}
        self.pending = pending
        return ready
