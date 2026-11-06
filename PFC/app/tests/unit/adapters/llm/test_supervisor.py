"""Offline child supervision for REQ-LLM-01/04/06 and TDD §13."""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock

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
import json, os, pathlib, signal, sys, time
record = pathlib.Path(sys.argv[1])
record.with_suffix(".key").write_text(os.environ.get("LLAMA_API_KEY", ""))
record.with_suffix(".env.json").write_text(json.dumps({
    name: os.environ.get(name) for name in ("LLAMA_API_KEY", "LLAMA_ARG_API_KEY")
}))
lines = record.with_suffix(".lines.json")
if lines.exists():
    for index, line in enumerate(json.loads(lines.read_text())):
        print(line.replace("__CHILD_KEY__", os.environ["LLAMA_API_KEY"]),
              file=sys.stdout if index % 2 else sys.stderr, flush=True)
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


def supervisor(
    settings: Settings,
    tmp_path: Path,
    mode: str = "ready",
    *,
    restart_backoff_s: tuple[float, float, float] = (10, 60, 180),
    restart_reset_window_s: float = 600,
) -> LlamaServerSupervisor:
    script = tmp_path / "fake_server.py"
    script.write_text(SCRIPT)
    record = tmp_path / (mode + ".json")

    async def health(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        assert request.url.path == "/health"
        if not record.exists() or mode in {"timeout", "exit"}:
            return httpx.Response(503)
        key = record.with_suffix(".key").read_text()
        if request.headers["authorization"] != "Bearer " + key:
            return httpx.Response(503)
        return httpx.Response(200)

    return LlamaServerSupervisor(
        settings,
        argv_prefix=[sys.executable, str(script), str(record)],
        transport=httpx.MockTransport(health),
        poll_interval_s=0.01,
        app_state=State(),
        restart_backoff_s=restart_backoff_s,
        restart_reset_window_s=restart_reset_window_s,
    )


def test_start_switch_stop(settings: Settings, tmp_path: Path, caplog) -> None:
    async def scenario() -> None:
        service = supervisor(settings, tmp_path)
        try:
            await service.start("one", settings.models_dir / "one.gguf")
            assert service.check().status == "ok"
            assert service.running_model == ("one", (settings.models_dir / "one.gguf").resolve())
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
            assert args["--parallel"] == "1" and "--api-key" not in args
            key = (tmp_path / "ready.key").read_text()
            assert len(key) >= 32 and key not in caplog.text
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
        assert service.running_model is None
        assert service.app_state.llm_provider is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "state,exit_code,expected",
    [
        ("ready", None, True),
        ("starting", None, True),
        ("ready", 1, False),
        ("starting", 1, False),
        ("unavailable", None, False),
        ("restarting", None, False),
    ],
)
def test_running_model_requires_live_process(
    settings: Settings,
    state: str,
    exit_code: int | None,
    expected: bool,
) -> None:
    service = LlamaServerSupervisor(settings)
    service.model_name = "one"
    service.model_path = (settings.models_dir / "one.gguf").resolve()
    service.state = state
    assert service.running_model is None
    service.proc = Mock()
    service.proc.poll.return_value = exit_code
    assert service.running_model == (("one", service.model_path) if expected else None)


def test_api_key_passed_only_in_environment(settings: Settings, tmp_path: Path) -> None:
    async def scenario() -> None:
        service = supervisor(settings, tmp_path)
        try:
            await service.start("one", settings.models_dir / "one.gguf")
            argv = json.loads((tmp_path / "ready.json").read_text())
            assert "--api-key" not in argv
            key = (tmp_path / "ready.key").read_text()
            assert len(key) >= 32
            assert key not in argv
            child_env = json.loads((tmp_path / "ready.env.json").read_text())
            assert child_env == {"LLAMA_API_KEY": key, "LLAMA_ARG_API_KEY": key}
            assert service.check().status == "ok"
        finally:
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["logging", "exit"])
def test_native_lines_are_filtered_before_child_log_write(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    fixture = Path(__file__).parents[3] / "fixtures" / "llama_b8936_windows.json"
    cases = json.loads(fixture.read_text(encoding="utf-8"))["cases"]
    lines = [line for group in cases.values() for line in group]
    lines += [
        "main: model loading error: __CHILD_KEY__",
        "main: model loading error: PARENT_KEY_ONE PARENT_KEY_TWO Bearer UNKNOWN_BEARER",
        "main: prompt: PRIVATE_PROMPT",
        "main: completion: PRIVATE_COMPLETION",
        "main: request body: PRIVATE_BODY",
        r"main: loading model 'D:\CustomProfile\private-user\models\one.gguf'",
    ]
    (tmp_path / f"{mode}.lines.json").write_text(json.dumps(lines))
    monkeypatch.setenv("LLAMA_API_KEY", "PARENT_KEY_ONE")
    monkeypatch.setenv("LLAMA_ARG_API_KEY", "PARENT_KEY_TWO")
    monkeypatch.setenv("USERPROFILE", r"D:\CustomProfile\private-user")

    async def scenario() -> None:
        service = supervisor(settings, tmp_path, mode)
        await service.start("one", settings.models_dir / "one.gguf")
        capture = service._log_capture
        await service.stop()
        assert service._log_capture is None
        if capture is not None:
            assert capture.thread is not None and not capture.thread.is_alive()
        output = (settings.data_dir / "logs" / "llm.log").read_text(encoding="utf-8")
        assert "main: model loaded" in output and "couldn't bind HTTP server socket" in output
        assert "failed to open GGUF file" in output and "<profile>" in output
        key = (tmp_path / f"{mode}.key").read_text()
        for sensitive in (
            key,
            "PARENT_KEY_ONE",
            "PARENT_KEY_TWO",
            "UNKNOWN_BEARER",
            "PRIVATE_PROMPT",
            "PRIVATE_COMPLETION",
            "PRIVATE_BODY",
            "fixture-user",
            "private-user",
            "capture-key-suffix",
        ):
            assert sensitive not in output

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
        assert service.check().status == "degraded"
        assert service.check().detail == "restarting (attempt 1/3)"
        await service.stop()

    asyncio.run(scenario())


def test_crash_restarts_and_republishes(settings: Settings, tmp_path: Path) -> None:
    async def scenario() -> None:
        service = supervisor(settings, tmp_path, restart_backoff_s=(0.05, 0.01, 0.01))
        try:
            await service.start("one", settings.models_dir / "one.gguf")
            first = service.proc
            assert first is not None
            first.kill()
            await asyncio.to_thread(first.wait)
            async with asyncio.timeout(5):
                while service.state != "restarting":
                    await asyncio.sleep(0.001)
                assert service.app_state.llm_provider is None
                assert service.check().status == "degraded"
                assert service.check().detail == "restarting (attempt 1/3)"
                while service.check().status != "ok":
                    await asyncio.sleep(0.001)
            assert service.proc is not first
            assert isinstance(service.app_state.llm_provider, FallbackProvider)
            assert service._restart_attempts == 1
        finally:
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("startup_fails", [False, True])
def test_crash_restart_budget_exhausted(
    settings: Settings, tmp_path: Path, startup_fails: bool
) -> None:
    async def scenario() -> None:
        service = supervisor(
            settings,
            tmp_path,
            restart_backoff_s=(0.01, 0.01, 0.01),
            restart_reset_window_s=0.3,
        )
        try:
            await service.start("one", settings.models_dir / "one.gguf")
            if startup_fails:
                settings.llm_startup_timeout_s = 0.05
                service.transport = httpx.MockTransport(lambda _: httpx.Response(503))
            for attempt in range(1 if startup_fails else 4):
                proc = service.proc
                assert proc is not None
                proc.kill()
                await asyncio.to_thread(proc.wait)
                async with asyncio.timeout(5):
                    while service.proc is proc or service.state not in {"ready", "unavailable"}:
                        if service.check().detail.endswith("restart attempts exhausted"):
                            break
                        await asyncio.sleep(0.001)
                if attempt < 3 and not startup_fails:
                    assert service.check().status == "ok"
                    assert service._restart_attempts == attempt + 1
            assert service.check().status == "unavailable"
            assert service.check().detail == "llama-server exited; restart attempts exhausted"
            assert service.app_state.llm_provider is None
            assert service._restart_attempts == 3
            assert service.proc is not None and service.proc.poll() is not None
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_healthy_window_refills_restart_budget(settings: Settings, tmp_path: Path) -> None:
    async def crash_and_recover(service: LlamaServerSupervisor) -> None:
        proc = service.proc
        assert proc is not None
        proc.kill()
        await asyncio.to_thread(proc.wait)
        async with asyncio.timeout(5):
            while service.proc is proc or service.check().status != "ok":
                await asyncio.sleep(0.001)

    async def scenario() -> None:
        service = supervisor(
            settings,
            tmp_path,
            restart_backoff_s=(0.01, 0.01, 0.01),
            restart_reset_window_s=0.2,
        )
        try:
            await service.start("one", settings.models_dir / "one.gguf")
            await crash_and_recover(service)
            assert service._restart_attempts == 1
            async with asyncio.timeout(5):
                while service._restart_attempts != 0:
                    assert service.check().status == "ok"
                    await asyncio.sleep(0.01)
            for attempt in range(1, 4):
                await crash_and_recover(service)
                assert service._restart_attempts == attempt
            assert service.proc is not None
            service.proc.kill()
            await asyncio.to_thread(service.proc.wait)
            async with asyncio.timeout(5):
                while service.state != "unavailable":
                    await asyncio.sleep(0.001)
            assert service.check().detail == "llama-server exited; restart attempts exhausted"
            assert service.app_state.llm_provider is None
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_healthy_exit_between_polls_refills_restart_budget(
    settings: Settings, tmp_path: Path
) -> None:
    async def crash_and_recover(service: LlamaServerSupervisor) -> None:
        proc = service.proc
        assert proc is not None
        proc.kill()
        await asyncio.to_thread(proc.wait)
        async with asyncio.timeout(5):
            while service.proc is proc or service.check().status != "ok":
                assert service.check().detail != "llama-server exited; restart attempts exhausted"
                await asyncio.sleep(0.001)

    async def scenario() -> None:
        service = supervisor(
            settings,
            tmp_path,
            restart_backoff_s=(0.01, 0.01, 0.01),
            restart_reset_window_s=0.1,
        )
        try:
            await service.start("one", settings.models_dir / "one.gguf")
            for attempt in range(1, 4):
                await crash_and_recover(service)
                assert service._restart_attempts == attempt
            service.poll_interval_s = 0.5
            await asyncio.sleep(0.15)
            assert service._restart_attempts == 3
            service.poll_interval_s = 0.01
            for attempt in range(1, 4):
                await crash_and_recover(service)
                assert service._restart_attempts == attempt
        finally:
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("switch", [False, True])
def test_stop_or_switch_cancels_restart_backoff(
    settings: Settings, tmp_path: Path, switch: bool
) -> None:
    async def scenario() -> None:
        service = supervisor(settings, tmp_path, restart_backoff_s=(0.1, 0.01, 0.01))
        try:
            await service.start("one", settings.models_dir / "one.gguf")
            proc = service.proc
            assert proc is not None
            proc.kill()
            await asyncio.to_thread(proc.wait)
            async with asyncio.timeout(5):
                while service.state != "restarting":
                    await asyncio.sleep(0.001)
            if switch:
                await service.switch("two", settings.models_dir / "two.gguf")
                assert service.check().status == "ok" and service.check().model == "two"
                assert service._restart_attempts == 0
            else:
                await service.stop()
                assert service.check().status == "unavailable"
                assert service.app_state.llm_provider is None
            stopped_or_switched = service.proc
            await asyncio.sleep(0.15)
            assert service.proc is stopped_or_switched
        finally:
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
