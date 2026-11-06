"""REQ-ING-02/06: serialisable previews and deterministic business-field diffs."""

import hashlib
import json
from collections.abc import Iterable, Mapping, Set
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

type RowKey = str | tuple[str, str]


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def is_no_op(digest: str, committed_hashes: Set[str]) -> bool:
    """REQ-ING-06: only committed hashes should be supplied (scoped by kind)."""
    return digest in committed_hashes


def row_hash(fields: Mapping[str, Any]) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class RowIssue(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    row_number: int
    column: str | None = None
    raw_value: Any = None
    message: str


class ParsedRow(BaseModel):
    row_number: int
    key: RowKey | None
    fields: dict[str, Any]
    row_hash: str
    issues: list[RowIssue] = Field(default_factory=list)
    rejected: bool = False
    superseded: bool = False


class Diff(BaseModel):
    new: list[RowKey] = Field(default_factory=list)
    changed: list[RowKey] = Field(default_factory=list)
    unchanged: list[RowKey] = Field(default_factory=list)
    missing: list[RowKey] = Field(default_factory=list)
    incomplete: list[int] = Field(default_factory=list)
    rejected: int = 0


def diff_rows(rows: Iterable[ParsedRow], existing: Mapping[RowKey, str]) -> Diff:
    """REQ-ING-02/03: describe changes without mutating records or committing."""
    result = Diff()
    seen: set[RowKey] = set()
    for row in rows:
        if row.superseded:
            continue
        if row.rejected or row.key is None:
            result.rejected += 1
            continue
        if isinstance(row.key, tuple) and not row.key[1]:
            result.incomplete.append(row.row_number)
            continue
        if row.key in seen:
            continue
        seen.add(row.key)
        if row.key not in existing:
            result.new.append(row.key)
        elif existing[row.key] == row.row_hash:
            result.unchanged.append(row.key)
        else:
            result.changed.append(row.key)
    result.missing = [
        key for key in existing if key not in seen and not (isinstance(key, tuple) and not key[1])
    ]
    return result


class Preview(BaseModel):
    kind: Literal["DL", "CT"]
    file_name: str
    file_sha256: str
    file_size: int
    sheet_name: str
    header_row: int
    rows: list[ParsedRow]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rows_read(self) -> int:
        return len(self.rows)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rows_rejected(self) -> int:
        return sum(row.rejected for row in self.rows)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rows_superseded(self) -> int:
        return sum(row.superseded for row in self.rows)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rows_incomplete(self) -> int:
        return len(diff_rows(self.rows, {}).incomplete)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def warnings(self) -> int:
        return sum(issue.severity == "warning" for issue in self.issues)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def errors(self) -> int:
        return sum(issue.severity == "error" for issue in self.issues)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def issues(self) -> list[RowIssue]:
        return [issue for row in self.rows for issue in row.issues]

    def diff(self, existing: Mapping[RowKey, str]) -> Diff:
        return diff_rows(self.rows, existing)
