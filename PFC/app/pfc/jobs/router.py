from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from pfc.jobs.runner import Job, JobRunner
from pfc.security.dependencies import require_role
from pfc.security.problems import Problem

router = APIRouter(prefix="/jobs", dependencies=[Depends(require_role("Analyst"))])


def get_runner(request: Request) -> JobRunner:
    return request.app.state.job_runner


Runner = Annotated[JobRunner, Depends(get_runner)]


def find(runner: JobRunner, job_id: int) -> Job:
    job = runner.get(job_id)
    if job is None:
        raise Problem(404, "JOB_NOT_FOUND", "Job not found")
    return job


@router.get("/{job_id}")
def get_job(job_id: int, runner: Runner) -> Job:
    return find(runner, job_id)


@router.get("/{job_id}/events")
def events(job_id: int, runner: Runner) -> StreamingResponse:
    find(runner, job_id)
    return StreamingResponse(
        runner.events(job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{job_id}/cancel")
def cancel(job_id: int, runner: Runner) -> Job:
    find(runner, job_id)
    runner.cancel(job_id)
    return find(runner, job_id)
