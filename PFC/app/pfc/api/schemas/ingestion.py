"""REQ-ING-01..03/06: persisted upload review API contracts."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from pfc.services.ingestion.models import Diff, RowIssue


class RunCounts(BaseModel):
    rows_read: int = 0
    rows_new: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    rows_rejected: int = 0
    rows_incomplete: int = 0


class IngestionRun(BaseModel):
    ingestion_run_id: int
    kind: Literal["DL", "CT"]
    status: Literal["preview", "committed", "discarded", "no_op", "failed"]
    file_name: str
    file_sha256: str
    file_size: int
    sheet_name: str | None
    header_row: int | None
    started_at: str
    committed_at: str | None
    error_summary: str | None
    no_op: bool = False
    counts: RunCounts
    diff: Diff = Field(default_factory=Diff)
    issues: list[RowIssue] = Field(default_factory=list)
    issues_total: int = 0


class RunPage(BaseModel):
    items: list[IngestionRun]
    total: int
    limit: int
    offset: int


class IssuePage(BaseModel):
    items: list[RowIssue]
    total: int
    limit: int
    offset: int


class ReviewSnapshot(BaseModel):
    diff: Diff
    counts: RunCounts
    metadata: dict[str, Any] = Field(default_factory=dict)
