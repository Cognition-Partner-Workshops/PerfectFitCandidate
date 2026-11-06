import secrets
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import uuid4

from fastapi import FastAPI, Request, Response

from pfc.db import sessions, users
from pfc.db.connection import database
from pfc.security.dependencies import Principal
from pfc.security.problems import Problem, response
from pfc.security.sessions import digest, parse, stamp


def install_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def security(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = request.headers.get("X-Request-ID") or str(uuid4())
        try:
            settings = request.app.state.settings
            host = request.headers.get("host", "")
            allowed = {
                f"127.0.0.1:{settings.port}",
                f"localhost:{settings.port}",
                *settings.allowed_hosts,
            }
            if host not in allowed:
                raise Problem(400, "INVALID_HOST", "Invalid Host header")
            origin = request.headers.get("origin")
            if origin is not None and origin != f"{request.url.scheme}://{host}":
                raise Problem(403, "INVALID_ORIGIN", "Invalid Origin")
            token = request.cookies.get("pfc_session")
            public = request.url.path == "/api/v1/health"
            if token and not public:
                with database(settings) as db:
                    session = sessions.get_session_by_token_hash(db, digest(token))
                    now = request.app.state.clock()
                    if session and not session.revoked_at and parse(session.expires_at) > now:
                        absolute = parse(session.created_at) + timedelta(
                            hours=settings.absolute_timeout_h
                        )
                        user = users.get_user_by_id(db, session.user_id)
                        if user and user.is_active and now < absolute:
                            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                                supplied = request.headers.get("X-CSRF-Token", "")
                                if not secrets.compare_digest(digest(supplied), session.csrf_hash):
                                    raise Problem(403, "CSRF_INVALID", "Invalid CSRF token")
                            if user.must_change_password and request.url.path not in {
                                "/api/v1/auth/me",
                                "/api/v1/auth/password",
                                "/api/v1/auth/logout",
                            }:
                                raise Problem(
                                    403, "PASSWORD_CHANGE_REQUIRED", "Password change required"
                                )
                            request.state.principal = Principal(user, session)
                            sessions.touch_session(
                                db,
                                session.session_id,
                                stamp(now),
                                stamp(
                                    min(
                                        now + timedelta(minutes=settings.idle_timeout_min), absolute
                                    )
                                ),
                            )
            result = await call_next(request)
        except Problem as exc:
            result = response(request, exc.status, exc.code, exc.detail)
        except Exception:
            result = response(request, 500, "INTERNAL_ERROR", "Internal server error")
        result.headers.update(
            {
                "X-Request-ID": request.state.request_id,
                "Content-Security-Policy": "default-src 'self'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
            }
        )
        return result
