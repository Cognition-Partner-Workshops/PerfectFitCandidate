from pathlib import Path

from fastapi.testclient import TestClient

from pfc.adapters.email.transports import FakeTransport
from pfc.config import Settings
from pfc.main import create_app


def test_app_shutdown_closes_created_email_transport(tmp_path: Path) -> None:
    class CloseTrackingTransport(FakeTransport):
        closed = False

        def close(self) -> None:
            self.closed = True

    settings = Settings(
        data_dir=tmp_path,
        frontend_dir=tmp_path / "no-dist",
        allowed_hosts=["testserver"],
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )
    app = create_app(settings)
    transport = CloseTrackingTransport()
    app.state.email_transport = transport

    with TestClient(app):
        pass

    assert transport.closed
