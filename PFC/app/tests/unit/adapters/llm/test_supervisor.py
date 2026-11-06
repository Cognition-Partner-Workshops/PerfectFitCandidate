"""Offline child supervision for REQ-LLM-01/04/06 and TDD §13."""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import State

from pfc.adapters.llm.providers import FallbackProvider
from pfc.adapters.llm.supervisor import LlamaServerSupervisor
from pfc.api.schemas.llm import LlmHealth
from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.main import create_app

SCRIPT = """
import json, pathlib, signal, sys, time
record = pathlib.Path(sys.argv[1])
if "ignore" in record.name:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
temporary = record.with_suffix(".tmp")
temporary.write_text(json.dumps(sys.argv[2:]))
temporary.replace(record)
if "exit" not in record.name:
    time.sleep(60)
"""


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    models = tmp_path / "models"
    models.mkdir()
    (models / "one.gguf").touch()
    (models / "two.gguf").touch()
    return Settings(
        data_dir=tmp_path / "data",
        models_dir=models,
        llama_dir=tmp_path / "llama",
        llm_ctx_size=2048,
        llm_threads=2,
        llm_startup_timeout_s=2,
        llm_shutdown_timeout_s=0.05,
        allowed_hosts=["testserver"],
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )


def supervisor(settings: Settings, tmp_path: Path, mode: str = "ready") -> LlamaServerSupervisor:
    script = tmp_path / "fake_server.py"
    script.write_text(SCRIPT)
    record = tmp_path / (mode + ".json")

    async def health(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        assert request.url.path == "/health"
        if not record.exists() or mode in {"timeout", "exit"}:
            return httpx.Response(503)
        argv = json.loads(record.read_text())
        if request.headers["authorization"] != "Bearer " + argv[argv.index("--api-key") + 1]:
            return httpx.Response(503)
        return httpx.Response(200)

    return LlamaServerSupervisor(
        settings,
        argv_prefix=[sys.executable, str(script), str(record)],
        transport=httpx.MockTransport(health),
        poll_interval_s=0.01,
        app_state=State(),
    )


def test_start_switch_stop(settings: Settings, tmp_path: Path, caplog) -> None:
    async def scenario() -> None:
        service = supervisor(settings, tmp_path)
        try:
            await service.start("one", settings.models_dir / "one.gguf")
            assert service.check().status == "ok"
            assert service.check().model == "one"
            assert isinstance(service.app_state.llm_provider, FallbackProvider)
            proc = service.proc
            assert proc is not None and proc.poll() is None
            argv = json.loads((tmp_path / "ready.json").read_text())
            args = dict(zip(argv[::2], argv[1::2], strict=True))
            assert args["--host"] == "127.0.0.1"
            assert 0 < int(args["--port"]) < 65536
            assert args["-m"] == str((settings.models_dir / "one.gguf").resolve())
            assert args["-c"] == "2048" and args["-t"] == "2"
            assert args["--parallel"] == "1" and len(args["--api-key"]) >= 32
            assert args["--api-key"] not in caplog.text
            await service.switch("one", settings.models_dir / "one.gguf")
            assert service.proc is proc
            await service.switch("two", settings.models_dir / "two.gguf")
            assert proc.poll() is not None
            assert service.proc is not proc and service.check().model == "two"
            assert service.check().status == "ok"
        finally:
            await service.stop()
            await service.stop()
        assert service.proc is not None and service.proc.poll() is not None
        assert service.app_state.llm_provider is None

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["timeout", "exit"])
def test_failed_start(settings: Settings, tmp_path: Path, mode: str) -> None:
    async def scenario() -> None:
        settings.llm_startup_timeout_s = 0.3 if mode == "timeout" else 2
        service = supervisor(settings, tmp_path, mode)
        await service.start("one", settings.models_dir / "one.gguf")
        try:
            assert service.state == "failed"
            assert service.check().status == "unavailable"
            assert ("timed out" if mode == "timeout" else "exited") in service.check().detail
            assert service.proc is not None and service.proc.poll() is not None
            assert service.app_state.llm_provider is None
        finally:
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGTERM semantics")
def test_shutdown_kills_uncooperative_child(settings: Settings, tmp_path: Path) -> None:
    async def scenario() -> None:
        service = supervisor(settings, tmp_path, "ignore")
        await service.start("one", settings.models_dir / "one.gguf")
        assert service.check().status == "ok"
        proc = service.proc
        await service.stop()
        assert proc is not None and proc.poll() is not None

    asyncio.run(scenario())


def test_stop_during_startup(settings: Settings, tmp_path: Path) -> None:
    async def scenario() -> None:
        service = supervisor(settings, tmp_path, "timeout")
        service.schedule(("one", settings.models_dir / "one.gguf"))
        while service.proc is None:
            await asyncio.sleep(0.01)
        proc = service.proc
        assert service.check().status == "degraded"
        await service.stop()
        assert proc.poll() is not None and service.app_state.llm_provider is None
        assert service.check().status == "unavailable"

    asyncio.run(scenario())


def test_unexpected_exit_clears_provider(settings: Settings, tmp_path: Path) -> None:
    async def scenario() -> None:
        service = supervisor(settings, tmp_path)
        await service.start("one", settings.models_dir / "one.gguf")
        assert service.proc is not None
        service.proc.kill()
        await asyncio.to_thread(service.proc.wait)
        for _ in range(100):
            if service.app_state.llm_provider is None:
                break
            await asyncio.sleep(0.01)
        assert service.app_state.llm_provider is None
        assert service.check().detail == "llama-server exited"
        await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["binary", "model", "disabled"])
def test_app_unavailable_and_override(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    monkeypatch.setenv("PFC_CONFIG_DIR", str(tmp_path / "absent"))
    if reason == "disabled":
        settings.llm_enabled = False
    elif reason == "model":
        settings.llama_dir.mkdir()
        binary = settings.llama_dir / (
            "llama-server.exe" if sys.platform == "win32" else "llama-server"
        )
        binary.touch()
    app = create_app(settings)

    class Override:
        def check(self) -> LlmHealth:
            return LlmHealth(status="ok", detail="test override")

    override = Override()
    app.state.llm_health_provider = override
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert app.state.llm_provider is None
        assert app.state.llm_supervisor.check().status == "unavailable"
        assert {"binary": "binary missing", "model": "no approved", "disabled": "disabled"}[
            reason
        ] in app.state.llm_supervisor.check().detail
        assert app.state.llm_health_provider is override


def test_resolve_model(settings: Settings, tmp_path: Path) -> None:
    migrate(settings)
    service = LlamaServerSupervisor(settings)
    with database(settings) as db:
        for email in ("one@example.com", "two@example.com"):
            db.execute(
                "INSERT INTO users(email,display_name,role_id,password_hash,created_at,updated_at)"
                " VALUES (?,'Test',2,'unused','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
                (email,),
            )
        for name, filename, approved, active in [
            ("one", "one.gguf", 1, 0),
            ("two", "two.gguf", 1, 1),
            ("escape", "../outside.gguf", 1, 0),
            ("not-approved", "one.gguf", 0, 0),
            ("onnx", "embedding.onnx", 1, 0),
        ]:
            db.execute(
                "INSERT INTO llm_models(name,file_name,file_sha256,size_mb,ram_estimate_mb,"
                "context_tokens,licence,approved,is_active) VALUES (?,?, '',0,0,1,'test',?,?)",
                (name, filename, approved, active),
            )
        (tmp_path / "outside.gguf").touch()
        assert service.resolve_model(db) == ("two", (settings.models_dir / "two.gguf").resolve())
        db.execute(
            "INSERT INTO settings(scope,user_id,key,value_json,updated_at) "
            "VALUES ('user',1,'llm.active_model','1','2026-01-01T00:00:00Z')"
        )
        assert service.resolve_model(db)[0] == "one"
        db.execute(
            "INSERT INTO settings(scope,user_id,key,value_json,updated_at) "
            "VALUES ('user',2,'llm.active_model','2','2026-01-02T00:00:00Z')"
        )
        assert service.resolve_model(db)[0] == "two"
        db.execute("UPDATE settings SET updated_at='2026-01-02T00:00:00.000001Z' WHERE user_id=1")
        assert service.resolve_model(db)[0] == "one"
        db.execute("UPDATE settings SET value_json='3'")
        assert service.resolve_model(db)[0] == "two"
        db.execute("UPDATE llm_models SET is_active=0")
        assert service.resolve_model(db)[0] == "one"
        db.execute("UPDATE llm_models SET approved=0 WHERE model_id IN (1,2)")
        assert service.resolve_model(db) is None
        db.execute(
            "UPDATE llm_models SET file_name=? WHERE model_id=3", (str(tmp_path / "outside.gguf"),)
        )
        assert service.resolve_model(db) is None


def test_thread_default(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    settings.llm_threads = None

    async def scenario() -> None:
        service = supervisor(settings, tmp_path)
        try:
            await service.start("one", settings.models_dir / "one.gguf")
            args = json.loads((tmp_path / "ready.json").read_text())
            assert args[args.index("-t") + 1] == "3"
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_rapid_selection_last_wins(settings: Settings, tmp_path: Path) -> None:
    async def scenario() -> None:
        service = supervisor(settings, tmp_path)
        try:
            service.schedule(("one", settings.models_dir / "one.gguf"))
            async with asyncio.timeout(5):
                while service.proc is None:
                    await asyncio.sleep(0.001)
                first = service.proc
                service.schedule(("two", settings.models_dir / "two.gguf"))
                while service.check().status != "ok":
                    await asyncio.sleep(0.01)
            assert service.model_name == "two" and first.poll() is not None
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_lifespan_starts_in_background(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrate(settings)
    with database(settings) as db:
        db.execute(
            "INSERT INTO llm_models(name,file_name,file_sha256,size_mb,ram_estimate_mb,"
            "context_tokens,licence,approved,is_active) "
            "VALUES ('one','one.gguf','',0,0,1,'test',1,1)"
        )
    service = supervisor(settings, tmp_path)

    def factory(settings: Settings, *, app_state: State | None) -> LlamaServerSupervisor:
        service.app_state = app_state
        if app_state is not None:
            app_state.llm_provider = None
        return service

    monkeypatch.setattr("pfc.main.LlamaServerSupervisor", factory)
    app = create_app(settings)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            assert service.state != "ready"
            assert app.state.llm_provider is None
            async with asyncio.timeout(5):
                while service.state != "ready":
                    await asyncio.sleep(0.01)
            assert app.state.llm_health_provider is service
            assert isinstance(app.state.llm_provider, FallbackProvider)
        assert service.proc is not None and service.proc.poll() is not None
        assert app.state.llm_provider is None

    asyncio.run(scenario())
