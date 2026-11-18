"""Per-user local watch pickup using the manual ingestion pipeline."""

import asyncio
import hashlib
import json
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from pfc.adapters.sources.watch_folder import WatchFolderSource
from pfc.api.schemas.settings import WatchSettings, WatchSource
from pfc.config import Settings
from pfc.db.connection import database
from pfc.jobs.runner import JobContext, JobRunner
from pfc.security.sessions import stamp
from pfc.services.ingestion import repository, service


class WatchPoller:
    def __init__(
        self, settings: Settings, job_runner: JobRunner, clock: Callable[[], datetime]
    ) -> None:
        self.settings, self.job_runner, self.clock = settings, job_runner, clock
        self._lock = threading.RLock()
        self._inflight: dict[tuple[int, str, str], int] = {}
        self._sources: dict[tuple[int, str], tuple[WatchSource, WatchFolderSource]] = {}
        self._status: dict[tuple[int, str], dict[str, Any]] = {}
        self._stop = asyncio.Event()

    async def run(self) -> None:
        while not self._stop.is_set():
            await asyncio.to_thread(self.tick, self.clock())
            try:
                await asyncio.wait_for(self._stop.wait(), self.settings.watch_tick_seconds)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    def tick(self, now: datetime) -> None:
        with self._lock:
            for inflight_key, job_id in list(self._inflight.items()):
                job = self.job_runner.get(job_id)
                if job is None or job.status in {"completed", "failed", "cancelled"}:
                    self._inflight.pop(inflight_key, None)
            with database(self.settings) as db:
                rows = db.execute(
                    "SELECT s.user_id,s.value_json FROM settings s "
                    "JOIN users u ON u.user_id=s.user_id "
                    "JOIN roles r ON r.role_id=u.role_id WHERE s.key='watch.sources' "
                    "AND s.scope='user' AND u.is_active=1 AND r.name IN ('Admin','Analyst')"
                ).fetchall()
            active = set()
            for row in rows:
                sources = WatchSettings.model_validate_json(row["value_json"])
                for kind in ("dl", "ct"):
                    source = getattr(sources, kind)
                    if not source.enabled:
                        continue
                    key = (row["user_id"], kind)
                    active.add(key)
                    prior = self._sources.get(key)
                    if prior is None or prior[0] != source:
                        self._sources[key] = (
                            source,
                            WatchFolderSource(
                                Path(source.path or ""),
                                source.pattern,
                                self.settings.watch_settle_seconds,
                            ),
                        )
                        self._status.pop(key, None)
                    scanner = self._sources[key][1]
                    status = self._status.get(key, {})
                    last = status.get("last_scan_at")
                    if (
                        last
                        and not scanner.pending
                        and (now - datetime.fromisoformat(last)).total_seconds()
                        < source.poll_minutes * 60
                    ):
                        continue
                    try:
                        scanner.max_bytes = service.upload_limit(self.settings)
                        ready = scanner.scan(now.timestamp())
                        self._status[key] = {
                            "reachable": True,
                            "last_scan_at": stamp(now),
                            "pending": [
                                {"file_name": name, "reason": reason}
                                for name, reason in scanner.pending.items()
                            ],
                            "last_error": None,
                        }
                    except OSError:
                        scanner.pending.clear()
                        self._status[key] = {
                            "reachable": False,
                            "last_scan_at": stamp(now),
                            "pending": [],
                            "last_error": "Watch folder is not accessible",
                        }
                        continue
                    for name, data in ready:
                        self._submit(
                            row["user_id"], kind.upper(), name, data, source.require_preview
                        )
            for key in set(self._sources) - active:
                self._sources.pop(key, None)
                self._status.pop(key, None)

    def _submit(
        self, user_id: int, kind: str, name: str, data: bytes, require_preview: bool
    ) -> None:
        digest = hashlib.sha256(data).hexdigest()
        key = (user_id, kind, digest)
        with database(self.settings) as db:
            exists = db.execute(
                "SELECT 1 FROM ingestion_runs WHERE source='watch_folder' "
                "AND uploaded_by=? AND kind=? AND file_sha256=?",
                key,
            ).fetchone()
        if exists or key in self._inflight:
            return

        def pickup(ctx: JobContext) -> None:
            try:
                ctx.raise_if_cancelled()
                run = service.upload(
                    self.settings,
                    cast(Literal["DL", "CT"], kind),
                    name,
                    data,
                    user_id,
                    stamp(self.clock()),
                    source="watch_folder",
                )
                outcome = "no_op" if run.no_op else "awaiting_review"
                with database(self.settings) as db:
                    errors = db.execute(
                        "SELECT 1 FROM ingestion_row_issues WHERE ingestion_run_id=? "
                        "AND severity='error' LIMIT 1",
                        (run.ingestion_run_id,),
                    ).fetchone()
                if not run.no_op and not require_preview and not errors:
                    service.commit_with_downstream(
                        self.settings,
                        self.job_runner,
                        run.ingestion_run_id,
                        user_id,
                        stamp(self.clock()),
                    )
                    outcome = "committed"
                with database(self.settings) as db:
                    db.execute(
                        "UPDATE jobs SET status='completed',progress=1,message=?,finished_at=? "
                        "WHERE job_id=? AND status='running'",
                        (
                            json.dumps({"run_id": run.ingestion_run_id, "outcome": outcome}),
                            stamp(self.clock()),
                            ctx.job_id,
                        ),
                    )
            finally:
                with self._lock:
                    self._inflight.pop(key, None)

        try:
            self._inflight[key] = self.job_runner.submit(
                "watch_pickup", pickup, {"kind": kind, "file_name": name}, created_by=user_id
            )
        except Exception:
            self._inflight.pop(key, None)
            raise

    def status(self, user_id: int) -> dict[str, Any]:
        with self._lock, database(self.settings) as db:
            row = db.execute(
                "SELECT value_json FROM settings WHERE scope='user' AND user_id=? "
                "AND key='watch.sources'",
                (user_id,),
            ).fetchone()
            sources = WatchSettings.model_validate_json(row[0]) if row else WatchSettings()
            result = {}
            for kind in ("dl", "ct"):
                source = getattr(sources, kind)
                state = self._status.get((user_id, kind), {}) if source.enabled else {}
                runs = db.execute(
                    "SELECT ingestion_run_id FROM ingestion_runs WHERE source='watch_folder' "
                    "AND uploaded_by=? AND kind=? ORDER BY ingestion_run_id DESC",
                    (user_id, kind.upper()),
                ).fetchall()

                def summary(run_id: int) -> dict[str, Any]:
                    run = repository.run_response(db, self.settings, run_id)
                    return {
                        "run_id": run_id,
                        "file_name": run.file_name,
                        "status": run.status,
                        "started_at": run.started_at,
                        "committed_at": run.committed_at,
                        "counts": run.counts.model_dump(),
                    }

                previews = db.execute(
                    "SELECT ingestion_run_id FROM ingestion_runs WHERE source='watch_folder' "
                    "AND uploaded_by=? AND kind=? AND status='preview' "
                    "ORDER BY ingestion_run_id DESC",
                    (user_id, kind.upper()),
                ).fetchall()
                result[kind] = {
                    "enabled": source.enabled,
                    "reachable": None,
                    "last_scan_at": None,
                    "pending": [],
                    "last_error": None,
                    **state,
                    "last_pickup": summary(runs[0][0]) if runs else None,
                    "awaiting_review": [summary(r[0]) for r in previews],
                }
            return result
