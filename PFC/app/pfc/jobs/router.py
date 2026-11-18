import json
import re
from datetime import timedelta
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from pfc.api.schemas.match import WireModel
from pfc.db.audit import append_audit
from pfc.db.connection import database
from pfc.jobs.runner import Job, JobRunner
from pfc.security.dependencies import Principal, current_user, require_role
from pfc.security.problems import Problem
from pfc.security.sessions import stamp

router = APIRouter(prefix="/jobs", dependencies=[Depends(require_role("Analyst"))])


def get_runner(request: Request) -> JobRunner:
    return request.app.state.job_runner


Runner = Annotated[JobRunner, Depends(get_runner)]
JobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
JobUser = Annotated[Principal, Depends(current_user)]


class SkippedFile(BaseModel):
    file_name: str
    reason: str


class JobItem(WireModel):
    job_id: int
    kind: str
    status: JobStatus
    progress: float
    message: str | None
    label: str
    link: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    skipped_files: list[SkippedFile] | None = None
    skipped_count: int | None = None


def skipped_summary(job: Job) -> tuple[list[SkippedFile] | None, int | None]:
    if job.kind != "cv_sync" or job.status != "completed":
        return None, None
    try:
        summary = json.loads(job.message or "null")
    except (ValueError, TypeError):
        return None, None
    if not isinstance(summary, dict):
        return None, None
    entries = summary.get("skipped_files")
    count = summary.get("skipped_count")
    if not isinstance(entries, list) and type(count) is not int:
        return None, None
    files = [
        SkippedFile(file_name=entry["file_name"], reason=entry["reason"])
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict)
        and isinstance(entry.get("file_name"), str)
        and isinstance(entry.get("reason"), str)
    ]
    total = count if type(count) is int and count >= 0 else len(files)
    return files[:50], total


def presentation(job: Job) -> tuple[str, str | None]:
    try:
        params = json.loads(job.params_json or "{}")
    except (ValueError, TypeError):
        params = {}
    if not isinstance(params, dict):
        params = {}
    if job.kind == "watch_pickup":
        target = "Demand List" if params.get("kind") == "DL" else "Candidate Tracker"
        name = params.get("file_name")
        name = (
            "".join(c for c in name if c.isprintable())[:80]
            if isinstance(name, str)
            else "workbook"
        )
        suffix = ""
        if job.status == "completed":
            try:
                outcome = json.loads(job.message or "null")
            except (ValueError, TypeError):
                outcome = None
            if isinstance(outcome, dict):
                suffix = {
                    "committed": " — committed",
                    "awaiting_review": " — awaiting review",
                    "no_op": " — no changes",
                }.get(str(outcome.get("outcome")), "")
        return f"Auto-pickup {target}: {name}{suffix}", "/#/settings/data"
    if job.kind == "cv_sync":
        ids = params.get("resource_ids")
        count = len(ids) if isinstance(ids, list) else 0
        target = f"{count} profile{'s' if count != 1 else ''}" if count else "all profiles"
        return f"CV Sync for {target}", "/#/bench"
    if job.kind == "email_follow_up":
        ids = params.get("message_ids")
        count = len(ids) if isinstance(ids, list) else 0
        return (
            f"Outlook follow-up reminders ({count})" if count > 1 else "Outlook follow-up reminder"
        ), "/#/email"
    if job.kind == "match_run":
        run_id = params.get("run_id")
        if type(run_id) is int and run_id > 0:
            return f"Matching run #{run_id}", "/#/matches"
        return "Matching run", "/#/matches"
    if job.kind == "chat":
        return "Chat question", f"/#/chat?job={job.job_id}"
    if job.kind == "embed":
        return ("Model check" if params.get("model_type") == "llm" else "Re-indexing CVs"), None
    # Kinds are internal identifiers, never arbitrary display text.
    label = re.sub(r"[_-]+", " ", job.kind).capitalize()
    if not re.fullmatch(r"[A-Za-z0-9 ]{1,100}", label):
        label = "Background task"
    return label, None


@router.get("")
def list_jobs(
    request: Request,
    runner: Runner,
    principal: Annotated[Principal, Depends(current_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[JobItem]:
    cutoff = stamp(request.app.state.clock() - timedelta(minutes=15))
    with database(runner.settings) as db:
        rows = db.execute(
            "SELECT * FROM jobs WHERE (created_by=? OR created_by IS NULL) "
            "AND (status IN ('queued','running') OR finished_at>=?) "
            "ORDER BY created_at DESC, job_id DESC LIMIT ?",
            (principal.user.user_id, cutoff, limit),
        ).fetchall()
    result = []
    for row in rows:
        job = Job(**dict(row))
        label, link = presentation(job)
        skipped_files, skipped_count = skipped_summary(job)
        # Like runner failures, expose fixed messages rather than exception or progress payloads.
        message = {
            "queued": "Job queued",
            "running": "Job running",
            "completed": "Job completed",
            "failed": "Job failed",
            "cancelled": "Job cancelled",
        }[job.status]
        result.append(
            JobItem(
                job_id=job.job_id,
                kind=job.kind,
                status=cast(JobStatus, job.status),
                progress=job.progress,
                message=message,
                label=label,
                link=link,
                created_at=job.created_at,
                started_at=job.started_at,
                finished_at=job.finished_at,
                skipped_files=skipped_files,
                skipped_count=skipped_count,
            )
        )
    return result


def find(runner: JobRunner, job_id: int, principal: Principal) -> Job:
    job = runner.get(job_id)
    if job is None or (job.kind == "chat" and job.created_by != principal.user.user_id):
        raise Problem(404, "JOB_NOT_FOUND", "Job not found")
    return job


@router.get("/{job_id}")
def get_job(job_id: int, runner: Runner, principal: JobUser) -> Job:
    return find(runner, job_id, principal)


@router.get("/{job_id}/events")
def events(job_id: int, runner: Runner, principal: JobUser) -> StreamingResponse:
    find(runner, job_id, principal)
    return StreamingResponse(
        runner.events(job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{job_id}/cancel")
def cancel(job_id: int, request: Request, runner: Runner, principal: JobUser) -> Job:
    job = find(runner, job_id, principal)
    if job.kind == "chat" and job.status in {"queued", "running"}:
        with database(runner.settings) as db:
            append_audit(
                db,
                ts=stamp(request.app.state.clock()),
                action="CHAT_CANCEL_REQUESTED",
                user_id=principal.user.user_id,
                entity_type="chat",
                entity_id=str(job_id),
            )
    return runner.cancel(job_id) or job
