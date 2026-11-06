import asyncio
import json
import socket
import threading
from pathlib import Path

import pytest

from pfc.config import Settings
from pfc.db import sessions, users
from pfc.db.connection import database
from pfc.jobs.cleanup import session_cleanup
from pfc.launcher import acquire_lock, choose_port, is_port_free, launch, wait_for_health
from pfc.security.setup import code_path


def test_lock(tmp_path: Path) -> None:
    path = tmp_path / "pfc.lock"
    first = acquire_lock(path, 12345)
    try:
        assert first.acquired
        second = acquire_lock(path, 54321)
        try:
            assert not second.acquired
            assert second.port == 12345
        finally:
            second.close()
    finally:
        first.close()
    path.write_text(" " + json.dumps({"pid": 99999999, "port": 12345}))
    recovered = acquire_lock(path, 54321)
    try:
        assert recovered.acquired
        assert recovered.port == 54321
    finally:
        recovered.close()


def test_port() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        occupied = listener.getsockname()[1]
        assert not is_port_free(occupied)
        replacement = choose_port(occupied)
        assert replacement != occupied
        assert is_port_free(replacement)
    assert choose_port(replacement) == replacement


def test_launch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(
        data_dir=tmp_path, argon_memory_cost=1024, argon_time_cost=1, argon_parallelism=1
    )
    opened: list[str] = []
    browser_opened = threading.Event()

    def open_browser(url: str) -> None:
        opened.append(url)
        browser_opened.set()

    def server(app, *, host: str, port: int, workers: int) -> None:
        assert host == "127.0.0.1" and workers == 1
        assert browser_opened.wait(5)
        from fastapi.testclient import TestClient

        with TestClient(app, base_url=f"http://127.0.0.1:{port}") as client:
            assert client.get("/api/v1/health").status_code == 200
            assert code_path(settings).read_text() == printed_code[0]

    printed_code: list[str] = []

    def ready(port: int, stop: threading.Event) -> bool:
        printed_code.append(code_path(settings).read_text())
        return True

    assert launch(settings, run_server=server, open_browser=open_browser, ready=ready) == 0
    assert opened[0].endswith("/setup")
    assert printed_code[0] in capsys.readouterr().out
    assert (tmp_path / "run" / "port").exists()
    with database(settings) as db:
        users.create_user(
            db,
            email="admin@example.com",
            display_name="Admin",
            role_id=1,
            password_hash="unused",
            now="2026-01-01T00:00:00Z",
        )
    browser_opened.clear()
    assert (
        launch(
            settings,
            run_server=lambda *a, **k: browser_opened.wait(5),
            open_browser=open_browser,
            ready=lambda *a: True,
        )
        == 0
    )
    assert opened[-1].endswith("/") and not opened[-1].endswith("/setup")
    with pytest.raises(ValueError, match="loopback"):
        launch(settings.model_copy(update={"host": "0.0.0.0"}))
    existing = acquire_lock(tmp_path / "run" / "pfc.lock", 12345)
    try:
        assert launch(settings, open_browser=open_browser) == 0
        assert opened[-1] == "http://127.0.0.1:12345/"
    finally:
        existing.close()
    assert (
        launch(
            settings,
            no_browser=True,
            run_server=lambda *a, **k: None,
            open_browser=lambda _: pytest.fail("browser opened"),
        )
        == 0
    )


def test_health_wait_cancelled() -> None:
    stop = threading.Event()
    stop.set()
    assert not wait_for_health(8765, stop)


def test_cleanup(tmp_path: Path) -> None:
    from pfc.db.migrate import migrate

    settings = Settings(data_dir=tmp_path)
    migrate(settings)
    with database(settings) as db:
        user = users.create_user(
            db,
            email="a@example.com",
            display_name="A",
            role_id=2,
            password_hash="unused",
            now="2000-01-01T00:00:00Z",
        )
        sessions.create_session(
            db,
            token_hash="expired",
            csrf_hash="csrf",
            user_id=user.user_id,
            role_at_login="Analyst",
            now="2000-01-01T00:00:00Z",
            expires_at="2000-01-01T01:00:00Z",
        )

    async def run() -> None:
        task = asyncio.create_task(session_cleanup(settings, interval=0.001))
        try:
            for _ in range(100):
                await asyncio.sleep(0.01)
                with database(settings) as db:
                    if sessions.get_session_by_token_hash(db, "expired") is None:
                        return
            pytest.fail("cleanup did not purge session")
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
