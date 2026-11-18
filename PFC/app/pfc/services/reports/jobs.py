import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pfc.api.schemas.reports import PdfRequest
from pfc.db.audit import append_audit
from pfc.db.connection import database
from pfc.jobs.runner import JobContext, JobRunner
from pfc.services.pii import mask_emails_in_json

from .builder import build_pdf
from .data import collect


def audit_details(request: PdfRequest) -> dict[str, Any]:
    filters = (
        request.dashboard_filters() if request.report == "dashboard" else request.results_query()
    )
    return {
        "report": request.report,
        "scope": request.scope,
        "sections": request.sections,
        "orientation": request.orientation,
        "filters": mask_emails_in_json(filters.model_dump(by_alias=True, exclude_none=True)),
    }


def report_path(runner: JobRunner, job_id: int) -> Path:
    return runner.settings.data_dir / "reports" / f"{job_id}.pdf"


def submit(
    runner: JobRunner, request: PdfRequest, user_id: int, username: str, now: datetime
) -> int:
    def generate(context: JobContext) -> None:
        path = report_path(runner, context.job_id)
        temporary = path.with_suffix(".pdf.part")
        try:
            context.report_progress(0.1, "Collecting report values")
            with database(runner.settings) as db:
                data = collect(db, request, username, now)
            context.report_progress(0.35, "Rendering masked PDF")
            content = build_pdf(data)
            context.report_progress(0.85, "Saving report")
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            context.raise_if_cancelled()
            os.replace(temporary, path)
            context.report_progress(0.95, "Recording report generation")
            with database(runner.settings) as db:
                append_audit(
                    db,
                    ts=now.isoformat(),
                    action="PDF_GENERATE",
                    user_id=user_id,
                    entity_type="job",
                    entity_id=str(context.job_id),
                    details={
                        **audit_details(request),
                        "job_id": context.job_id,
                        "row_count": len(data.results)
                        if request.report == "match_results"
                        else data.total,
                        "total_rows": data.total,
                    },
                )
        except BaseException:
            temporary.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            raise

    return runner.submit("pdf_report", generate, {"report": request.report}, created_by=user_id)
