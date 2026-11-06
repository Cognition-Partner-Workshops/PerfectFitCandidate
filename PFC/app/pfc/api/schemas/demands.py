"""REQ-OUT-01: demand browsing contracts."""

from typing import Literal

from pydantic import BaseModel, Field

# Derived from the requirements of the current demand version; see browse.DEMAND_SELECT.
JdStatus = Literal["Not analysed", "Draft", "Confirmed", "JD changed"]


class DemandQuery(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)
    bu: str | None = None
    region: str | None = None
    account_name: str | None = None
    grade: str | None = None
    lifecycle: str = "open"
    q: str | None = None


class DemandItem(BaseModel):
    demand_id: int
    code_requisition: str
    id_format_ok: bool
    position_start_date: str | None
    account_name: str | None
    code_local_grade: str | None
    demand_request_name: str | None
    region: str | None
    bu: str | None
    owner: str | None
    lifecycle: str
    occ_pending_review: bool
    jd_status: JdStatus


class DemandVersion(BaseModel):
    version_no: int
    jd_sha256: str
    created_at: str
    jd_text_norm: str


class DemandDetail(DemandItem):
    current_version_id: int | None
    versions: list[DemandVersion]


class DemandPage(BaseModel):
    items: list[DemandItem]
    page: int
    page_size: int
    total: int
