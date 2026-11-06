import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from pfc.config import Settings
from pfc.db.connection import database
from pfc.security.sessions import stamp

logger = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled"}


class JobCancelled(Exception):
    pass


@dataclass(frozen=True)
class Job:
    job_id: int
    kind: str
    params_json: str | None
    status: str
    progress: float
    message: str | None
    created_by: int | None
    created_at: str
    started_at: str | None
    finished_at: str | None


class JobContext:
    def __init__(self, runner: "JobRunner", job_id: int, cancelled: threading.Event) -> None:
        self.runner, self.job_id, self.cancelled = runner, job_id, cancelled

    def is_cancelled(self) -> bool:
        return self.cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise JobCancelled()

    def report_progress(self, fraction: float, message: str = "") -> None:
        if not 0 <= fraction <= 1:
            raise ValueError("progress must be between 0 and 1")
        self.raise_if_cancelled()
        with database(self.runner.settings) as db:
            db.execute(
                "UPDATE jobs SET progress=?, message=? WHERE job_id=? AND status='running'",
                (fraction, message, self.job_id),
            )


class JobRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._closed = False
        self._tasks: dict[int, tuple[Future[None], threading.Event]] = {}
        with database(settings) as db:
            db.execute(
                "UPDATE jobs SET status='failed', message='interrupted by restart', finished_at=? "
                "WHERE status IN ('queued','running')",
                (stamp(datetime.now(UTC)),),
            )
        self._executor = ThreadPoolExecutor(
            max_workers=settings.job_workers, thread_name_prefix="pfc-job"
        )

    def submit(
        self,
        kind: str,
        fn: Callable[[JobContext], None],
        params: dict[str, Any] | None = None,
        created_by: int | None = None,
    ) -> int:
        """Persist before dispatch; params are metadata, callable receives JobContext."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Job runner is closed")
            with database(self.settings) as db:
                cursor = db.execute(
                    "INSERT INTO jobs(kind, params_json, status, created_by, created_at) "
                    "VALUES (?, ?, 'queued', ?, ?)",
                    (
                        kind,
                        json.dumps(params) if params is not None else None,
                        created_by,
                        stamp(datetime.now(UTC)),
                    ),
                )
                job_id = int(cursor.lastrowid or 0)
            cancelled = threading.Event()
            future = self._executor.submit(self._run, job_id, fn, cancelled)
            self._tasks[job_id] = (future, cancelled)
            future.add_done_callback(lambda _: self._forget(job_id))
            return job_id

    def _forget(self, job_id: int) -> None:
        with self._lock:
            self._tasks.pop(job_id, None)

    def _finish(self, job_id: int, status: str, message: str | None) -> None:
        with database(self.settings) as db:
            db.execute(
                "UPDATE jobs SET status=?, message=?, finished_at=?, "
                "progress=CASE WHEN ?='completed' THEN 1 ELSE progress END "
                "WHERE job_id=? AND status NOT IN ('completed','failed','cancelled')",
                (status, message, stamp(datetime.now(UTC)), status, job_id),
            )

    def _run(
        self, job_id: int, fn: Callable[[JobContext], None], cancelled: threading.Event
    ) -> None:
        try:
            with database(self.settings) as db:
                cursor = db.execute(
                    "UPDATE jobs SET status='running', started_at=? "
                    "WHERE job_id=? AND status='queued'",
                    (stamp(datetime.now(UTC)), job_id),
                )
                if not cursor.rowcount:
                    return
            context = JobContext(self, job_id, cancelled)
            context.raise_if_cancelled()
            fn(context)
            context.raise_if_cancelled()
            self._finish(job_id, "completed", None)
        except JobCancelled:
            self._finish(job_id, "cancelled", "cancelled")
        except Exception:
            logger.exception("Background job %s failed", job_id)
            self._finish(job_id, "failed", "Job failed")

    def get(self, job_id: int) -> Job | None:
        with database(self.settings) as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return Job(**dict(row)) if row else None

    def cancel(self, job_id: int) -> Job | None:
        with self._lock:
            task = self._tasks.get(job_id)
            if task:
                task[1].set()
            with database(self.settings) as db:
                db.execute(
                    "UPDATE jobs SET status='cancelled', message='cancelled', finished_at=? "
                    "WHERE job_id=? AND status='queued'",
                    (stamp(datetime.now(UTC)), job_id),
                )
        return self.get(job_id)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            ids = list(self._tasks)
        for job_id in ids:
            self.cancel(job_id)
        self._executor.shutdown(wait=True)

    async def events(
        self,
        job_id: int,
        poll_interval: float = 0.25,
        heartbeat: float = 15,
    ) -> AsyncIterator[str]:
        previous = None
        loop = asyncio.get_running_loop()
        last_emitted = loop.time()
        while True:
            job = await asyncio.to_thread(self.get, job_id)
            if job is None:
                return
            payload = {key: asdict(job)[key] for key in ("job_id", "status", "progress", "message")}
            if payload != previous:
                event = "done" if job.status in TERMINAL else "progress"
                yield f"event: {event}\ndata: {json.dumps(payload)}\n\n"
                previous, last_emitted = payload, loop.time()
            elif loop.time() - last_emitted >= heartbeat:
                yield ": heartbeat\n\n"
                last_emitted = loop.time()
            if job.status in TERMINAL:
                return
            await asyncio.sleep(poll_interval)
