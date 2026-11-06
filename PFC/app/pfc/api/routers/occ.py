import sqlite3
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response

from pfc.adapters.llm.supervisor import restart_selected_model
from pfc.api.schemas.llm import ModelApproval
from pfc.api.schemas.occ import (
    ApproveSynonym,
    AuditFilters,
    BenchStatus,
    Domain,
    DomainPreview,
    EmailTemplate,
    Grade,
    Locations,
    Recipient,
    RecipientUpdate,
    StatusMap,
    Synonym,
    TemplateCreate,
    TemplateUpdate,
    UserCreate,
    UserUpdate,
)
from pfc.db.connection import database
from pfc.security.dependencies import Principal, require_role
from pfc.security.problems import Problem
from pfc.security.sessions import stamp
from pfc.services.occ import admin as administration
from pfc.services.occ import llm, reference, synonyms, templates, users
from pfc.services.occ.common import get_row
from pfc.services.occ.cv_extractions import ExtractionItem, RemovalCounts, list_extractions, remove

Admin = Annotated[Principal, Depends(require_role("Admin"))]
router = APIRouter(prefix="/occ", dependencies=[Depends(require_role("Admin"))])


def connection(request: Request) -> Iterator[sqlite3.Connection]:
    with database(request.app.state.settings) as db:
        try:
            if request.method != "GET":
                db.execute("BEGIN IMMEDIATE")
            yield db
        except sqlite3.IntegrityError as exc:
            raise Problem(409, "CONFLICT", "Record conflicts with existing data") from exc


DB = Annotated[sqlite3.Connection, Depends(connection)]


@router.get("/candidates/{candidate_id}/cv-extractions")
def cv_extractions_list(candidate_id: int, db: DB, admin: Admin) -> list[ExtractionItem]:
    return list_extractions(db, candidate_id)


@router.delete("/candidates/{candidate_id}/cv-extractions")
def cv_extractions_remove(
    candidate_id: int,
    request: Request,
    db: DB,
    admin: Admin,
) -> RemovalCounts:
    return remove(
        db,
        request.app.state.settings,
        admin.user.user_id,
        stamp(request.app.state.clock()),
        candidate_id,
    )


@router.delete("/cv-extractions")
def cv_extractions_remove_all(request: Request, db: DB, admin: Admin) -> RemovalCounts:
    return remove(
        db,
        request.app.state.settings,
        admin.user.user_id,
        stamp(request.app.state.clock()),
    )


@router.get("/users")
def list_users(db: DB, admin: Admin) -> list[dict[str, Any]]:
    """REQ-ROLE-03: Admin-only user management."""
    return users.list_users(db, admin.user.user_id)


@router.post("/users", status_code=201)
def create_user(body: UserCreate, request: Request, db: DB, admin: Admin) -> dict[str, Any]:
    return users.create_user(db, admin.user.user_id, body.model_dump(), request.app.state.settings)


@router.put("/users/{target}")
def update_user(target: int, body: UserUpdate, db: DB, admin: Admin) -> dict[str, Any]:
    return users.update_user(db, admin.user.user_id, target, body.model_dump(exclude_unset=True))


@router.post("/users/{target}/unlock")
def unlock(target: int, db: DB, admin: Admin) -> dict[str, Any]:
    return users.unlock(db, admin.user.user_id, target)


@router.post("/users/{target}/reset-password")
def reset(target: int, request: Request, db: DB, admin: Admin) -> dict[str, Any]:
    return users.reset_password(db, admin.user.user_id, target, request.app.state.settings)


@router.get("/bench-statuses")
def bench_list(db: DB, admin: Admin) -> list[dict[str, Any]]:
    """REQ-ELIG-02/03: configured bench eligibility."""
    return reference.list_rows(db, admin.user.user_id, "bench_statuses")


@router.post("/bench-statuses", status_code=201)
def bench_create(body: BenchStatus, db: DB, admin: Admin) -> dict[str, Any]:
    return reference.save_bench(db, admin.user.user_id, body.model_dump())


@router.put("/bench-statuses/{code}")
def bench_update(code: str, body: BenchStatus, db: DB, admin: Admin) -> dict[str, Any]:
    return reference.save_bench(db, admin.user.user_id, body.model_dump(), code)


@router.delete("/bench-statuses/{code}", status_code=204)
def bench_delete(code: str, db: DB, admin: Admin) -> None:
    reference.delete_bench(db, admin.user.user_id, code)


@router.get("/status-map")
def mapping_list(db: DB, admin: Admin) -> list[dict[str, Any]]:
    """REQ-ELIG-07: explicit mappings for source status labels."""
    return reference.list_rows(db, admin.user.user_id, "status_map")


@router.put("/status-map/{source:path}")
def mapping_save(source: str, body: StatusMap, db: DB, admin: Admin) -> dict[str, Any]:
    return reference.save_mapping(db, admin.user.user_id, source, body.model_dump())


@router.delete("/status-map/{source:path}", status_code=204)
def mapping_delete(source: str, db: DB, admin: Admin) -> None:
    reference.delete_mapping(db, admin.user.user_id, source)


@router.get("/grades")
def grades_list(db: DB, admin: Admin) -> list[dict[str, Any]]:
    """REQ-GRD-01: ordered grade reference data."""
    return reference.list_rows(db, admin.user.user_id, "grades")


@router.put("/grades")
def grades_save(body: list[Grade], db: DB, admin: Admin) -> list[dict[str, Any]]:
    return reference.replace_grades(db, admin.user.user_id, [g.model_dump() for g in body])


@router.get("/location-aliases")
def locations_get(db: DB, admin: Admin) -> dict[str, Any]:
    return reference.get_locations(db, admin.user.user_id)


@router.put("/location-aliases")
def locations_save(body: Locations, db: DB, admin: Admin) -> dict[str, Any]:
    return reference.save_locations(db, admin.user.user_id, body.model_dump())


@router.get("/synonyms")
def synonyms_list(
    db: DB, admin: Admin, status: str | None = None, skill: str | None = None, q: str | None = None
) -> list[dict[str, Any]]:
    """REQ-MAT-05: canonical synonyms and suggestions."""
    return synonyms.list_synonyms(db, admin.user.user_id, status, skill, q)


@router.post("/synonyms", status_code=201)
def synonym_create(body: Synonym, db: DB, admin: Admin) -> dict[str, Any]:
    return synonyms.save(db, admin.user.user_id, body.model_dump())


@router.put("/synonyms/{target}")
def synonym_update(target: int, body: Synonym, db: DB, admin: Admin) -> dict[str, Any]:
    return synonyms.save(db, admin.user.user_id, body.model_dump(), target)


@router.delete("/synonyms/{target}", status_code=204)
def synonym_delete(target: int, db: DB, admin: Admin) -> None:
    synonyms.delete(db, admin.user.user_id, target)


@router.post("/synonyms/{target}/approve")
def synonym_approve(
    target: int, db: DB, admin: Admin, body: ApproveSynonym | None = None
) -> dict[str, Any]:
    return synonyms.transition(
        db, admin.user.user_id, target, "approved", body.canonical if body else None
    )


@router.post("/synonyms/{target}/reject")
def synonym_reject(target: int, db: DB, admin: Admin) -> dict[str, Any]:
    return synonyms.transition(db, admin.user.user_id, target, "rejected")


@router.get("/scoring-templates")
def scoring_list(db: DB, admin: Admin) -> list[dict[str, Any]]:
    """REQ-SCR-01/02: versioned scoring templates."""
    return templates.scoring_list(db, admin.user.user_id)


@router.get("/scoring-templates/{target}")
def scoring_get(target: int, db: DB, admin: Admin) -> dict[str, Any]:
    return templates.scoring_get(db, admin.user.user_id, target)


@router.post("/scoring-templates", status_code=201)
def scoring_create(body: TemplateCreate, db: DB, admin: Admin) -> dict[str, Any]:
    return templates.create_scoring(db, admin.user.user_id, body.model_dump())


@router.put("/scoring-templates/{target}")
def scoring_update(target: int, body: TemplateUpdate, db: DB, admin: Admin) -> dict[str, Any]:
    return templates.update_scoring(db, admin.user.user_id, target, body.model_dump())


@router.delete("/scoring-templates/{target}", status_code=204)
def scoring_delete(target: int, db: DB, admin: Admin) -> None:
    templates.delete_scoring(db, admin.user.user_id, target)


@router.post("/scoring-templates/{target}/activate")
def scoring_activate(target: int, db: DB, admin: Admin) -> dict[str, Any]:
    return templates.activate(db, admin.user.user_id, target)


@router.get("/email-templates")
def email_list(db: DB, admin: Admin, all: bool = False) -> list[dict[str, Any]]:
    """REQ-EML-03: latest versions by default, optional complete history."""
    return templates.email_list(db, admin.user.user_id, all)


@router.get("/email-templates/{target}")
def email_get(target: int, db: DB, admin: Admin) -> dict[str, Any]:
    return get_row(db, "email_templates", "email_template_id", target)


@router.post("/email-templates", status_code=201)
def email_create(body: EmailTemplate, db: DB, admin: Admin) -> dict[str, Any]:
    return templates.create_email(db, admin.user.user_id, body.model_dump())


@router.post("/email-templates/{target}/set-default")
def email_default(target: int, db: DB, admin: Admin) -> dict[str, Any]:
    return templates.default_email(db, admin.user.user_id, target)


@router.get("/resource-team")
def recipients_list(db: DB, admin: Admin) -> list[dict[str, Any]]:
    """REQ-EML-05: explicit resource-team recipient addresses."""
    return administration.recipients(db, admin.user.user_id)


@router.post("/resource-team", status_code=201)
def recipient_create(body: Recipient, db: DB, admin: Admin) -> dict[str, Any]:
    return administration.save_recipient(db, admin.user.user_id, body.model_dump())


@router.put("/resource-team/{target}")
def recipient_update(target: int, body: RecipientUpdate, db: DB, admin: Admin) -> dict[str, Any]:
    return administration.save_recipient(
        db, admin.user.user_id, body.model_dump(exclude_unset=True), target
    )


@router.delete("/resource-team/{target}", status_code=204)
def recipient_delete(target: int, db: DB, admin: Admin) -> None:
    administration.delete_recipient(db, admin.user.user_id, target)


@router.get("/internal-domains")
def domains_list(db: DB, admin: Admin) -> list[dict[str, Any]]:
    """REQ-CLS-01: normalized domain allowlist."""
    return administration.domains(db, admin.user.user_id)


@router.post("/internal-domains", status_code=201)
def domain_create(body: Domain, db: DB, admin: Admin) -> dict[str, Any]:
    return administration.add_domain(db, admin.user.user_id, body.domain)


@router.delete("/internal-domains/{domain}", status_code=204)
def domain_delete(domain: str, db: DB, admin: Admin) -> None:
    administration.delete_domain(db, admin.user.user_id, domain)


@router.post("/internal-domains/preview")
def domain_preview(body: DomainPreview, db: DB, admin: Admin) -> dict[str, Any]:
    return administration.preview_domains(db, admin.user.user_id, body.domains)


@router.get("/settings")
def settings_get(db: DB, admin: Admin, request: Request) -> dict[str, Any]:
    """REQ-ROLE-04: system-wide Admin settings."""
    return administration.settings_get(db, admin.user.user_id, request.app.state.settings)


@router.put("/settings")
def settings_put(body: dict[str, Any], db: DB, admin: Admin, request: Request) -> dict[str, Any]:
    return administration.settings_put(db, admin.user.user_id, body, request.app.state.settings)


Filters = Annotated[AuditFilters, Query()]


def filter_values(filters: AuditFilters) -> dict[str, Any]:
    return filters.model_dump(mode="json", by_alias=True)


@router.get("/audit-log")
def audit_list(db: DB, admin: Admin, filters: Filters) -> dict[str, Any]:
    """REQ-AUTH-05: audit values, filtering and chain validation."""
    return administration.audit_query(
        db, admin.user.user_id, filter_values(filters), filters.limit, filters.offset
    )


@router.get("/audit-log/export")
def audit_export(db: DB, admin: Admin, filters: Filters) -> Response:
    result = administration.export_audit(
        db, admin.user.user_id, filter_values(filters), filters.limit, filters.offset
    )
    return Response(
        result,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="pfc-audit.csv"'},
    )


@router.get("/llm-models")
def llm_models(db: DB, admin: Admin) -> list[dict[str, Any]]:
    """REQ-LLM-01/06: static, Admin-managed model approval list."""
    return llm.occ_models(db, admin.user.user_id)


@router.put("/llm-models/{target}")
def llm_approve(
    target: int,
    body: ModelApproval,
    db: DB,
    admin: Admin,
    request: Request,
    background_tasks: BackgroundTasks,
) -> list[dict[str, Any]]:
    llm.approve(db, admin.user.user_id, target, body.approved, is_default=body.is_default is True)
    db.commit()
    supervisor = getattr(request.app.state, "llm_supervisor", None)
    if supervisor is not None:
        resolved = supervisor.resolve_model(db)
        if resolved != supervisor.running_model:
            background_tasks.add_task(
                restart_selected_model, request.app.state.settings, request.app.state
            )
    return llm.occ_models(db, admin.user.user_id)
