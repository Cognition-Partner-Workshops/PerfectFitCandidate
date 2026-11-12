"""REQ-OUT-01/03, REQ-ELIG-08: parameterised read queries and masked projections."""

import sqlite3
from typing import Any

from pfc.api.schemas.demands import DemandDetail, DemandItem, DemandPage, DemandVersion
from pfc.api.schemas.resources import CVSummary, ResourceDetail, ResourceItem, ResourcePage
from pfc.config import Settings
from pfc.db.connection import database
from pfc.security.problems import Problem
from pfc.services.pii import mask_email

RESOURCE_SELECT = (
    "SELECT resources.*, (SELECT count(*) FROM cv_versions "
    "WHERE cv_versions.candidate_id=resources.candidate_id) AS cv_versions_count FROM resources"
)
# One grouped join, not a query per row. Draft wins over Confirmed because the review drawer
# opens the draft; "JD changed" means requirements exist only for an older demand version.
DEMAND_SELECT = (
    "SELECT demands.*, CASE WHEN jd.has_draft THEN 'Draft' WHEN jd.has_confirmed THEN 'Confirmed' "
    "WHEN jd.has_older THEN 'JD changed' ELSE 'Not analysed' END AS jd_status FROM demands "
    "LEFT JOIN (SELECT v.demand_id, "
    "max(r.demand_version_id=d.current_version_id AND r.status='draft') AS has_draft, "
    "max(r.demand_version_id=d.current_version_id AND r.status='confirmed') AS has_confirmed, "
    "max(r.demand_version_id IS NOT d.current_version_id) AS has_older "
    "FROM jd_requirements r JOIN demand_versions v ON v.demand_version_id=r.demand_version_id "
    "JOIN demands d ON d.demand_id=v.demand_id GROUP BY v.demand_id"
    ") jd ON jd.demand_id=demands.demand_id"
)


def _where(
    filters: dict[str, str | None], substrings: dict[str, str | None]
) -> tuple[str, list[str]]:
    clauses = []
    values = []
    for field, value in filters.items():
        if value is not None:
            clauses.append(f"{field}=?")
            values.append(value)
    for field, value in substrings.items():
        if value is not None:
            clauses.append(f"instr(lower(coalesce({field},'')),lower(?))>0")
            values.append(value)
    return " AND ".join(clauses) or "1=1", values


def _demand(row: sqlite3.Row) -> DemandItem:
    return DemandItem(**dict(row), occ_pending_review=row["occ_reviewed_at"] is None)


def demands(
    settings: Settings, page: int, page_size: int, filters: dict[str, str | None], q: str | None
) -> DemandPage:
    where, params = _where(filters, {})
    if q is not None:
        where += (
            " AND (instr(lower(code_requisition),lower(?))>0 "
            "OR instr(lower(coalesce(demand_request_name,'')),lower(?))>0)"
        )
        params.extend([q, q])
    with database(settings) as db:
        total = db.execute(f"SELECT count(*) FROM demands WHERE {where}", params).fetchone()[0]
        rows = db.execute(
            f"{DEMAND_SELECT} WHERE {where} ORDER BY position_start_date IS NULL, "
            "position_start_date DESC, demands.demand_id LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        return DemandPage(
            items=[_demand(r) for r in rows], page=page, page_size=page_size, total=total
        )


def demand_detail(settings: Settings, demand_id: int) -> DemandDetail:
    with database(settings) as db:
        row = db.execute(f"{DEMAND_SELECT} WHERE demands.demand_id=?", (demand_id,)).fetchone()
        if row is None:
            raise Problem(404, "DEMAND_NOT_FOUND", "Demand not found")
        versions = db.execute(
            "SELECT version_no, jd_sha256, created_at, jd_text_norm FROM demand_versions "
            "WHERE demand_id=? ORDER BY version_no DESC",
            (demand_id,),
        ).fetchall()
        return DemandDetail(
            **_demand(row).model_dump(),
            current_version_id=row["current_version_id"],
            versions=[DemandVersion(**dict(v)) for v in versions],
        )


def _mask(email: str | None) -> str | None:
    if not email or not email.strip():
        return None
    local, separator, domain = email.strip().partition("@")
    if not separator or not local or not domain or "@" in domain:
        return "***"
    return mask_email(email)


def _resource(row: sqlite3.Row, unrecognised_skills: list[str]) -> ResourceItem:
    fields: dict[str, Any] = {
        name: row[name] for name in ResourceItem.model_fields if name in row.keys()
    }
    fields.update(
        unrecognised_skills=unrecognised_skills,
        email_address=_mask(row["email_address"]),
        exclusion_reasons=[
            s.strip() for s in (row["exclusion_reason"] or "").split(",") if s.strip()
        ],
        pending_review=row["bench_end_date_parse_status"] == "unparseable",
        unmapped_status=bool((row["bench_status"] or "").strip())
        and row["bench_status_code"] is None,
        occ_pending_review=row["occ_reviewed_at"] is None,
    )
    return ResourceItem(**fields)


def resources(
    settings: Settings,
    page: int,
    page_size: int,
    filters: dict[str, str | None],
    location: str | None,
    q: str | None,
) -> ResourcePage:
    where, params = _where(filters, {"location": location, "short_name": q})
    with database(settings) as db:
        total = db.execute(f"SELECT count(*) FROM resources WHERE {where}", params).fetchone()[0]
        rows = db.execute(
            f"{RESOURCE_SELECT} WHERE {where} ORDER BY bench_start_date IS NULL, "
            "bench_start_date DESC, resource_id LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        from pfc.services.occ.skill_suggestions import unrecognised_skills_by_resource

        unrecognised = unrecognised_skills_by_resource(db, [row["resource_id"] for row in rows])
        return ResourcePage(
            items=[_resource(r, unrecognised.get(r["resource_id"], [])) for r in rows],
            page=page,
            page_size=page_size,
            total=total,
        )


def resource_detail(settings: Settings, resource_id: int) -> ResourceDetail:
    with database(settings) as db:
        row = db.execute(f"{RESOURCE_SELECT} WHERE resource_id=?", (resource_id,)).fetchone()
        if row is None:
            raise Problem(404, "RESOURCE_NOT_FOUND", "Resource not found")
        from pfc.services.occ.skill_suggestions import unrecognised_skills_by_resource

        unrecognised = unrecognised_skills_by_resource(db, [resource_id])
        latest = db.execute(
            "SELECT cv_version_id, version_no, extracted_at FROM cv_versions "
            "WHERE candidate_id=? ORDER BY is_latest DESC, version_no DESC, "
            "cv_version_id DESC LIMIT 1",
            (row["candidate_id"],),
        ).fetchone()
        return ResourceDetail(
            **_resource(row, unrecognised.get(resource_id, [])).model_dump(),
            latest_cv=CVSummary(**dict(latest)) if latest else None,
        )
