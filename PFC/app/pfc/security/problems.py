import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

logger = logging.getLogger(__name__)


class Problem(Exception):
    def __init__(self, status: int, code: str, detail: str) -> None:
        self.status, self.code, self.detail = status, code, detail


def response(request: Request, status: int, code: str, detail: str) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "")
    return JSONResponse(
        {
            "type": "about:blank",
            "title": detail,
            "status": status,
            "detail": detail,
            "code": code,
            "request_id": request_id,
        },
        status_code=status,
        media_type="application/problem+json",
        headers={"X-Request-ID": request_id},
    )


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def problem_handler(request: Request, exc: Problem) -> JSONResponse:
        return response(request, exc.status, exc.code, exc.detail)

    @app.exception_handler(HTTPException)
    async def http_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return response(request, exc.status_code, f"HTTP_{exc.status_code}", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return response(request, 422, "VALIDATION_ERROR", "Invalid request")

    @app.exception_handler(Exception)
    async def unexpected_handler(request: Request, exc: Any) -> JSONResponse:
        logger.error("Unexpected request error", exc_info=exc)
        return response(request, 500, "INTERNAL_ERROR", "Internal server error")
