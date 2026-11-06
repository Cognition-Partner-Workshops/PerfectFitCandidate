"""REQ-IN-21/24: local CV synchronisation contracts."""

from pydantic import BaseModel


class CVSyncRequest(BaseModel):
    resource_ids: list[int] | None = None


class CVSyncJob(BaseModel):
    job_id: int


class CVVersionItem(BaseModel):
    cv_version_id: int
    version_no: int
    file_name: str
    source_path: str
    file_mtime: str | None
    identity_match: str | None
    is_latest: bool
    extracted_at: str


class CVSyncCounts(BaseModel):
    scanned: int = 0
    new_versions: int = 0
    unchanged: int = 0
    missing_cv: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    extraction_failed: int = 0
    embedded: int = 0
    warnings: int = 0
