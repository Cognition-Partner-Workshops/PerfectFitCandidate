from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from pfc.adapters.email.transports import FakeTransport
from pfc.api.routers import email as email_router
from pfc.config import Settings
from pfc.main import create_app


def test_email_transport_is_closed_and_recreated_after_lifespan_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CloseTrackingTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    settings = Settings(
        data_dir=tmp_path,
        bundled_models_dir=tmp_path / "bundled-models",
        frontend_dir=tmp_path / "no-dist",
        allowed_hosts=["testserver"],
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )
    app = create_app(settings)
    first_transport = CloseTrackingTransport()
    app.state.email_transport = first_transport
    transports_created_by_dependency: list[CloseTrackingTransport] = []

    def create_transport() -> CloseTrackingTransport:
        transport = CloseTrackingTransport()
        transports_created_by_dependency.append(transport)
        return transport

    monkeypatch.setattr(email_router, "OutlookComTransport", create_transport)

    with TestClient(app):
        pass

    assert first_transport.closed
    assert app.state.email_transport is None

    with TestClient(app):
        email_router.get_email_service(Request({"type": "http", "app": app}))
        second_transport = app.state.email_transport
        assert second_transport is transports_created_by_dependency[0]
        assert second_transport is not first_transport
        assert not second_transport.closed

    assert second_transport.closed
