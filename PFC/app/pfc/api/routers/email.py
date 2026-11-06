from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request

from pfc.adapters.email.transports import EmailTransport, OutlookComTransport
from pfc.adapters.llm.providers import LLMProvider
from pfc.api.schemas.email import (
    EmailDraftCreate,
    EmailDraftCreateResponse,
    EmailDraftEdit,
    EmailDraftIssueView,
    EmailDraftListResponse,
    EmailDraftView,
    EmailSendOutcomeView,
    EmailSendRequest,
    EmailSendResponse,
)
from pfc.security.dependencies import Principal, current_user, require_role
from pfc.services.email import EmailService
from pfc.services.email.service import EmailDraft

router = APIRouter(prefix="/emails", dependencies=[Depends(require_role("Analyst"))])
PrincipalDependency = Annotated[Principal, Depends(current_user)]


def get_email_service(request: Request) -> EmailService:
    state = request.app.state
    transport = getattr(state, "email_transport", None)
    if transport is None:
        transport = OutlookComTransport()
        state.email_transport = transport
    provider = getattr(state, "email_llm_provider", getattr(state, "llm_provider", None))
    clock = getattr(state, "clock", None)
    if clock is None:
        raise RuntimeError("Application clock is not configured")
    return EmailService(
        settings=state.settings,
        transport=cast(EmailTransport, transport),
        llm_provider=cast(LLMProvider | None, provider),
        clock=cast(Callable[[], datetime], clock),
    )


EmailServiceDependency = Annotated[EmailService, Depends(get_email_service)]


def _view(draft: EmailDraft) -> EmailDraftView:
    return EmailDraftView(**asdict(draft))


@router.post("/drafts", response_model=EmailDraftCreateResponse, status_code=201)
async def create_drafts(
    body: EmailDraftCreate, principal: PrincipalDependency, service: EmailServiceDependency
) -> EmailDraftCreateResponse:
    drafts, issues = await service.create_drafts(
        run_id=body.run_id,
        demand_id=body.demand_id,
        result_ids=body.result_ids,
        user_id=principal.user.user_id,
    )
    return EmailDraftCreateResponse(
        drafts=[_view(draft) for draft in drafts],
        issues=[EmailDraftIssueView(**asdict(issue)) for issue in issues],
    )


@router.get("", response_model=EmailDraftListResponse)
def list_drafts(
    service: EmailServiceDependency,
    status: str | None = Query(
        default=None, pattern="^(Draft|Approved|Sending|Sent|Failed|Discarded)$"
    ),
    demand_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
) -> EmailDraftListResponse:
    drafts, status_counts, sent_count = service.list_drafts(
        status=status, demand_id=demand_id, limit=limit
    )
    return EmailDraftListResponse(
        drafts=[_view(draft) for draft in drafts],
        status_counts=status_counts,
        sent_count=sent_count,
    )


@router.get("/{message_id}", response_model=EmailDraftView)
def get_draft(message_id: int, service: EmailServiceDependency) -> EmailDraftView:
    return _view(service.get_draft(message_id))


@router.put("/{message_id}", response_model=EmailDraftView)
def edit_draft(
    message_id: int,
    body: EmailDraftEdit,
    principal: PrincipalDependency,
    service: EmailServiceDependency,
) -> EmailDraftView:
    return _view(
        service.patch_draft(
            message_id,
            user_id=principal.user.user_id,
            subject=body.subject,
            body_html=body.body_html,
        )
    )


@router.post("/{message_id}/regenerate", response_model=EmailDraftView)
async def regenerate_draft(
    message_id: int, principal: PrincipalDependency, service: EmailServiceDependency
) -> EmailDraftView:
    return _view(await service.regenerate(message_id, user_id=principal.user.user_id))


@router.post("/{message_id}/approve", response_model=EmailDraftView)
def approve_draft(
    message_id: int, principal: PrincipalDependency, service: EmailServiceDependency
) -> EmailDraftView:
    return _view(service.approve(message_id, user_id=principal.user.user_id))


@router.post("/{message_id}/resend", response_model=EmailDraftView)
def resend_draft(
    message_id: int, principal: PrincipalDependency, service: EmailServiceDependency
) -> EmailDraftView:
    return _view(service.resend(message_id, user_id=principal.user.user_id))


@router.post("/{message_id}/send", response_model=EmailSendResponse)
def send_one(
    principal: PrincipalDependency,
    service: EmailServiceDependency,
    message_id: int,
) -> EmailSendResponse:
    outcomes = service.send_approved(
        user_id=principal.user.user_id,
        message_ids=[message_id],
    )
    return EmailSendResponse(
        outcomes=[EmailSendOutcomeView(**asdict(outcome)) for outcome in outcomes],
        sent_count=sum(outcome.status == "sent" for outcome in outcomes),
        failed_count=sum(outcome.status == "failed" for outcome in outcomes),
        skipped_count=sum(outcome.status == "skipped" for outcome in outcomes),
    )


@router.post("/send-approved", response_model=EmailSendResponse)
def send_approved(
    principal: PrincipalDependency,
    service: EmailServiceDependency,
    body: EmailSendRequest | None = None,
) -> EmailSendResponse:
    outcomes = service.send_approved(
        user_id=principal.user.user_id,
        message_ids=body.draft_ids if body is not None else None,
    )
    return EmailSendResponse(
        outcomes=[EmailSendOutcomeView(**asdict(outcome)) for outcome in outcomes],
        sent_count=sum(outcome.status == "sent" for outcome in outcomes),
        failed_count=sum(outcome.status == "failed" for outcome in outcomes),
        skipped_count=sum(outcome.status == "skipped" for outcome in outcomes),
    )
