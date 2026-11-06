"""REQ-PKG-03/04: local single-instance launcher.

llama-server supervision belongs to the LLM adapter.
"""

import argparse
import json
import os
import socket
import sys
import threading
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn

from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.db.users import count_admins
from pfc.main import create_app
from pfc.security.setup import generate_setup_code


@dataclass
class InstanceLock:
    handle: TextIO
    acquired: bool
    port: int

    def write(self, port: int) -> None:
        self.port = port
        self.handle.seek(0)
        self.handle.write(" " + json.dumps({"pid": os.getpid(), "port": port}))
        self.handle.truncate()
        self.handle.flush()

    def close(self) -> None:
        self.handle.close()


def acquire_lock(path: Path, port: int) -> InstanceLock:
    """OS lock ownership, not a potentially stale PID file, determines liveness."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    handle = path.open("r+", encoding="utf-8")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            handle.seek(1)
            stored_port = int(json.load(handle)["port"])
        except (ValueError, KeyError, TypeError):
            stored_port = port
        return InstanceLock(handle, False, stored_port)
    lock = InstanceLock(handle, True, port)
    lock.write(port)
    return lock


def is_port_free(port: int) -> bool:
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def choose_port(configured: int) -> int:
    if configured and is_port_free(configured):
        return configured
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def build_url(port: int, first_run: bool = False) -> str:
    return f"http://127.0.0.1:{port}/" + ("setup" if first_run else "")


def first_run(settings: Settings) -> bool:
    with database(settings) as db:
        return count_admins(db) == 0


def wait_for_health(port: int, stop: threading.Event, timeout: float = 30) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=0.5) as result:
                if result.status == 200:
                    return True
        except (URLError, OSError):
            pass
        stop.wait(0.1)
    return False


def launch(
    settings: Settings,
    no_browser: bool = False,
    *,
    run_server: Callable[..., Any] = uvicorn.run,
    open_browser: Callable[[str], Any] = webbrowser.open,
    ready: Callable[[int, threading.Event], bool] = wait_for_health,
) -> int:
    """REQ-AUTH-03/07: bootstrap locally and never bind an external interface."""
    if settings.host not in {"127.0.0.1", "localhost"}:
        raise ValueError("host must be loopback")
    for directory in ("db", "run", "logs", "backups"):
        (settings.data_dir / directory).mkdir(parents=True, exist_ok=True)
    lock = acquire_lock(settings.data_dir / "run" / "pfc.lock", settings.port)
    stop = threading.Event()
    browser_thread = None
    try:
        if not lock.acquired:
            if not no_browser:
                open_browser(build_url(lock.port))
            return 0
        migrate(settings)
        port = choose_port(settings.port)
        settings = settings.model_copy(update={"port": port})
        lock.write(port)
        (settings.data_dir / "run" / "port").write_text(str(port), encoding="utf-8")
        setup = first_run(settings)
        if setup:
            code = generate_setup_code(settings)
            print(
                f"\n=== FIRST-RUN ADMIN SETUP CODE ===\n{code}\n"
                f"Saved to {settings.data_dir / 'run' / 'setup-code.txt'}\n"
            )
        if not no_browser:

            def browse() -> None:
                if ready(port, stop) and not stop.is_set():
                    open_browser(build_url(port, setup))

            browser_thread = threading.Thread(target=browse, daemon=True, name="pfc-browser")
            browser_thread.start()
        run_server(create_app(settings), host="127.0.0.1", port=port, workers=1)
        return 0
    finally:
        stop.set()
        if browser_thread is not None:
            browser_thread.join(timeout=2)
        lock.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PerfectFitCandidate local launcher")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    return launch(Settings(), no_browser=args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
