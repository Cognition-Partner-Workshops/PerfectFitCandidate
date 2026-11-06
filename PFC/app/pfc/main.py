from fastapi import FastAPI

from pfc.api.routers.health import router as health_router


def create_app() -> FastAPI:
    app = FastAPI(title="PerfectFitCandidate")
    app.include_router(health_router, prefix="/api/v1")
    return app
