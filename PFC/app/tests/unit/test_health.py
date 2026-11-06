from fastapi.testclient import TestClient

from pfc.config import Settings
from pfc.main import create_app


def test_health() -> None:
    response = TestClient(create_app(Settings(allowed_hosts=["testserver"]))).get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "details": {"dataVolumeEncryption": "Unknown"}}
