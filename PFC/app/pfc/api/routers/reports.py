from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from pfc.api.schemas.reports import PdfAccepted, PdfRequest
from pfc.db.audit import append_audit
from pfc.db.connection import database
from pfc.jobs.router import Runner
from pfc.security.dependencies import Principal, require_role
from pfc.security.problems import Problem
from pfc.services.reports.jobs import report_path, submit

router = APIRouter(prefix="/reports")
Analyst = Annotated[Principal, Depends(require_role("Analyst"))]


@router.post("/pdf", response_model=PdfAccepted, status_code=202)
def generate(body: PdfRequest, request: Request, runner: Runner, principal: Analyst) -> PdfAccepted:
    if body.report == "dashboard" and (run_id := body.dashboard_filters().run_id) is not None:
        with database(runner.settings) as db:
            if db.execute("SELECT 1 FROM match_runs WHERE run_id=?", (run_id,)).fetchone() is None:
                raise Problem(404, "RUN_NOT_FOUND", "Match run not found")
    return PdfAccepted(
        job_id=submit(
            runner,
            body,
            principal.user.user_id,
            principal.user.display_name,
            request.app.state.clock(),
        )
    )


@router.get("/{job_id}/file")
def download(job_id: int, request: Request, runner: Runner, principal: Analyst) -> FileResponse:
    job = runner.get(job_id)
    path = report_path(runner, job_id)
    if (
        job is None
        or job.kind != "pdf_report"
        or job.status != "completed"
        or (job.created_by != principal.user.user_id and principal.session.role_at_login != "Admin")
        or not path.is_file()
    ):
        raise Problem(404, "REPORT_NOT_FOUND", "Report not found")
    with database(runner.settings) as db:
        append_audit(
            db,
            ts=request.app.state.clock().isoformat(),
            action="PDF_DOWNLOAD",
            user_id=principal.user.user_id,
            entity_type="job",
            entity_id=str(job_id),
            details={"job_id": job_id},
        )
    return FileResponse(path, media_type="application/pdf", filename=f"PFC-report-{job_id}.pdf")
