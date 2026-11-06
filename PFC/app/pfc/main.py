import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pfc.adapters.llm.supervisor import LlamaServerSupervisor
from pfc.api.routers.auth import router as auth_router
from pfc.api.routers.cv import router as cv_router
from pfc.api.routers.demands import router as demands_router
from pfc.api.routers.email import router as email_router
from pfc.api.routers.health import router as health_router
from pfc.api.routers.ingestion import router as ingestion_router
from pfc.api.routers.llm import router as llm_router
from pfc.api.routers.occ import router as occ_router
from pfc.api.routers.packages import router as packages_router
from pfc.api.routers.pii import router as pii_router
from pfc.api.routers.resources import router as resources_router
from pfc.api.routers.settings import router as settings_router
from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.db.users import count_admins
from pfc.jobs.cleanup import session_cleanup
from pfc.jobs.router import router as jobs_router
from pfc.jobs.runner import JobRunner
from pfc.security.middleware import install_middleware
from pfc.security.passwords import hasher
from pfc.security.problems import Problem, install_handlers
from pfc.security.setup import code_path, generate_setup_code
from pfc.services.occ.seed import seed_reference_data
from pfc.services.packages.inbox import scan_inbox

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        migrate(settings)
        with database(settings) as db:
            seed_reference_data(db)
            if not count_admins(db) and not code_path(settings).exists():
                generate_setup_code(settings)
            elif count_admins(db):
                code_path(settings).unlink(missing_ok=True)
        try:
            scan_inbox(settings)
        except Exception:
            logger.exception("Package inbox scan failed; startup continues")
        app.state.job_runner = JobRunner(settings)
        supervisor = LlamaServerSupervisor(
            settings, app_state=app.state if not hasattr(app.state, "llm_provider") else None
        )
        app.state.llm_supervisor = supervisor
        if not hasattr(app.state, "llm_health_provider"):
            app.state.llm_health_provider = supervisor
        with database(settings) as db:
            model = supervisor.resolve_model(db)
        if settings.llm_enabled and supervisor.binary_available and model:
            supervisor.schedule(model)
        else:
            logging.getLogger(__name__).info("Local LLM unavailable: %s", supervisor.detail)
        cleanup = asyncio.create_task(session_cleanup(settings))
        try:
            yield
        finally:
            await supervisor.stop()
            cleanup.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup
            try:
                email_transport = getattr(app.state, "email_transport", None)
                close_transport = getattr(email_transport, "close", None)
                if callable(close_transport):
                    close_transport()
            finally:
                app.state.email_transport = None
                app.state.job_runner.shutdown()

    app = FastAPI(title="PerfectFitCandidate", lifespan=lifespan)
    app.state.settings = settings
    app.state.clock = lambda: datetime.now(UTC)
    app.state.dummy_hash = hasher(settings).hash("unused-random-dummy-password")
    install_handlers(app)
    install_middleware(app)
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(email_router, prefix="/api/v1")
    app.include_router(jobs_router, prefix="/api/v1")
    app.include_router(ingestion_router, prefix="/api/v1")
    app.include_router(demands_router, prefix="/api/v1")
    app.include_router(resources_router, prefix="/api/v1")
    app.include_router(cv_router, prefix="/api/v1")
    app.include_router(occ_router, prefix="/api/v1")
    app.include_router(settings_router, prefix="/api/v1")
    app.include_router(packages_router, prefix="/api/v1")
    app.include_router(pii_router, prefix="/api/v1")
    app.include_router(llm_router, prefix="/api/v1")
    frontend = settings.frontend_dir.resolve()
    if (frontend / "index.html").is_file():
        if (frontend / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path == "api" or path.startswith("api/"):
                raise Problem(404, "HTTP_404", "Not Found")
            candidate = (frontend / path).resolve()
            if candidate.is_relative_to(frontend) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(frontend / "index.html")

    return app
