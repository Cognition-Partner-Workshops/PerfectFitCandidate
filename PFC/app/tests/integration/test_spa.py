from pathlib import Path

from fastapi.testclient import TestClient

from pfc.config import Settings
from pfc.main import create_app


def test_spa_and_assets(tmp_path: Path) -> None:
    frontend = tmp_path / "dist"
    frontend.mkdir()
    (frontend / "assets").mkdir()
    (frontend / "index.html").write_text("<html>PerfectFitCandidate</html>")
    (frontend / "assets" / "app.js").write_text("console.log('app')")
    settings = Settings(
        data_dir=tmp_path / "data",
        bundled_models_dir=tmp_path / "bundled-models",
        frontend_dir=frontend,
        allowed_hosts=["testserver"],
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )
    with TestClient(create_app(settings)) as client:
        for path in ["/", "/setup", "/candidates/123"]:
            result = client.get(path)
            assert result.status_code == 200
            assert result.headers["content-type"].startswith("text/html")
            assert result.text == "<html>PerfectFitCandidate</html>"
        asset = client.get("/assets/app.js")
        assert asset.status_code == 200
        assert asset.text == "console.log('app')"
        for path in ["/api", "/api/v1/nope"]:
            result = client.get(path)
            assert result.status_code == 404
            assert result.headers["content-type"] == "application/problem+json"
        assert client.get("/assets/missing.js").status_code == 404


def test_no_dist(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        bundled_models_dir=tmp_path / "bundled-models",
        frontend_dir=tmp_path / "missing",
        allowed_hosts=["testserver"],
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 404
        assert client.get("/api/v1/health").status_code == 200
