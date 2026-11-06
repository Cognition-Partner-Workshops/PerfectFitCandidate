"""REQ-LLM-01/04: one loopback llama-server, without crash auto-restart."""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from contextlib import suppress
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import httpx
from starlette.datastructures import State

from pfc.adapters.llm.providers import FallbackProvider, LlamaCppProvider
from pfc.api.schemas.llm import LlmHealth
from pfc.config import Settings

logger = logging.getLogger(__name__)


def _windows_job(pid: int) -> int | None:
    if sys.platform != "win32":
        return None

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong),
            ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD),
            ("minimum", ctypes.c_size_t),
            ("maximum", ctypes.c_size_t),
            ("active", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "read_ops",
                "write_ops",
                "other_ops",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits),
            ("io", IoCounters),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None, None)
    process = None
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not job or not kernel.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            raise OSError("Job limits unavailable")
        process = kernel.OpenProcess(0x0101, False, pid)
        if not process or not kernel.AssignProcessToJobObject(job, process):
            raise OSError("Job assignment unavailable")
        return int(job)
    except OSError:
        if job:
            kernel.CloseHandle(job)
        logger.warning("Unable to assign llama-server to a Windows Job Object")
        return None
    finally:
        if process:
            kernel.CloseHandle(process)


def _close_job(handle: int | None) -> None:
    if sys.platform == "win32" and handle is not None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.CloseHandle(handle)


class LlamaServerSupervisor:
    """TDD §8/§13: supervise a local child; failures stay unavailable until explicitly retried."""

    def __init__(
        self,
        settings: Settings,
        *,
        argv_prefix: Sequence[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval_s: float = 0.5,
        app_state: State | None = None,
    ) -> None:
        self.settings = settings
        self.binary = settings.llama_dir / (
            "llama-server.exe" if sys.platform == "win32" else "llama-server"
        )
        self.argv_prefix = list(argv_prefix) if argv_prefix is not None else None
        self.transport = transport
        self.poll_interval_s = poll_interval_s
        self.app_state = app_state
        self.proc: subprocess.Popen[bytes] | None = None
        self.provider: LlamaCppProvider | None = None
        self.model_name: str | None = None
        self.model_path: Path | None = None
        self.state = "unavailable"
        self.detail = (
            "disabled"
            if not settings.llm_enabled
            else "no approved GGUF model"
            if self.binary_available
            else "llama-server binary missing"
        )
        self._job: int | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._operation: asyncio.Task[None] | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._publish(None)

    def _publish(self, provider: FallbackProvider | None) -> None:
        if self.app_state is not None:
            self.app_state.llm_provider = provider

    @property
    def binary_available(self) -> bool:
        return self.argv_prefix is not None or self.binary.is_file()

    def resolve_model(self, db: sqlite3.Connection) -> tuple[str, Path] | None:
        rows = {
            row["model_id"]: row
            for row in db.execute("SELECT * FROM llm_models WHERE approved=1 ORDER BY model_id")
        }
        choices = []
        user_choices = db.execute(
            "SELECT value_json,updated_at,setting_id FROM settings "
            "WHERE scope='user' AND key='llm.active_model'"
        )
        for row in sorted(
            user_choices,
            key=lambda row: (datetime.fromisoformat(row["updated_at"]), row["setting_id"]),
            reverse=True,
        ):
            try:
                choice = json.loads(row["value_json"])
                if isinstance(choice, int) and choice in rows:
                    choices.append(rows[choice])
            except (ValueError, TypeError):
                continue
        choices += [row for row in rows.values() if row["is_active"]]
        choices += list(rows.values())
        root = self.settings.models_dir.resolve()
        for row in choices:
            filename = Path(row["file_name"])
            if ".." in filename.parts or filename.suffix.lower() != ".gguf":
                continue
            path = (root / filename).resolve()
            if path.is_relative_to(root) and path.is_file():
                return row["name"], path
        return None

    def schedule(self, model: tuple[str, Path] | None) -> None:
        """Last committed selection wins; the shared server restarts asynchronously."""
        self._publish(None)
        if self._operation is not None and not self._operation.done():
            self._operation.cancel()
        self._operation = asyncio.create_task(self.switch(*model) if model else self.stop())

    async def start(self, model_name: str, model_path: Path) -> None:
        self._start_task = asyncio.current_task()
        try:
            async with self._lock:
                await self._start(model_name, model_path)
        except asyncio.CancelledError:
            async with self._lock:
                await self._cleanup()
                self.state, self.detail = "unavailable", "startup cancelled"
            raise
        except Exception:
            logger.warning("llama-server startup failed")
            async with self._lock:
                await self._cleanup()
                self.state, self.detail = "failed", "llama-server startup failed"
        finally:
            if self._start_task is asyncio.current_task():
                self._start_task = None

    async def _start(self, model_name: str, model_path: Path) -> None:
        await self._cleanup()
        self._publish(None)
        if not self.settings.llm_enabled:
            self.state, self.detail = "unavailable", "disabled"
            return
        if not self.binary_available:
            self.state, self.detail = "unavailable", "llama-server binary missing"
            return
        root = self.settings.models_dir.resolve()
        path = model_path.resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() != ".gguf":
            self.state, self.detail = (
                "unavailable",
                "GGUF model missing or outside models directory",
            )
            return
        self.state, self.detail = "starting", "starting"
        self.model_name, self.model_path = model_name, path
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        api_key = secrets.token_urlsafe(32)
        base_url = f"http://127.0.0.1:{port}"
        threads = self.settings.llm_threads or max(1, (os.cpu_count() or 2) // 2 - 1)
        argv = [
            *(self.argv_prefix if self.argv_prefix is not None else [str(self.binary)]),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--api-key",
            api_key,
            "-m",
            str(path),
            "-c",
            str(self.settings.llm_ctx_size),
            "-t",
            str(threads),
            "--parallel",
            "1",
        ]
        try:
            logs = self.settings.data_dir / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            with (logs / "llm.log").open("ab") as output:
                if sys.platform == "win32":
                    self.proc = subprocess.Popen(
                        argv,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                        | subprocess.CREATE_NO_WINDOW,
                    )
                    self._job = _windows_job(self.proc.pid)
                else:
                    self.proc = subprocess.Popen(
                        argv, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
                    )
            async with asyncio.timeout(self.settings.llm_startup_timeout_s):
                async with httpx.AsyncClient(
                    base_url=base_url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    transport=self.transport,
                    trust_env=False,
                ) as client:
                    while True:
                        if self.proc.poll() is not None:
                            raise ChildProcessError
                        try:
                            response = await client.get("/health")
                            if response.status_code == 200 and self.proc.poll() is None:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(self.poll_interval_s)
            self.provider = LlamaCppProvider(
                base_url, model_name, api_key=api_key, transport=self.transport
            )
            self.state, self.detail = "ready", ""
            self._publish(FallbackProvider(self.provider, self.provider))
            self._monitor = asyncio.create_task(self._watch())
        except Exception as exc:
            detail = (
                "llama-server exited"
                if isinstance(exc, ChildProcessError)
                else (
                    "llama-server startup timed out"
                    if isinstance(exc, TimeoutError)
                    else "llama-server startup failed"
                )
            )
            logger.warning("%s", detail)
            await self._cleanup()
            self.state, self.detail = "failed", detail

    async def _watch(self) -> None:
        while self.proc is not None and self.proc.poll() is None:
            await asyncio.sleep(self.poll_interval_s)
        self._publish(None)
        self.state, self.detail = "failed", "llama-server exited"
        if self.provider is not None:
            await self.provider.aclose()
            self.provider = None

    async def _cleanup(self) -> None:
        self._publish(None)
        if self._monitor is not None:
            self._monitor.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor
            self._monitor = None
        if self.proc is not None and self.proc.poll() is None:
            with suppress(ProcessLookupError):
                self.proc.terminate()
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self.proc.wait), timeout=self.settings.llm_shutdown_timeout_s
                )
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self.proc.kill()
                await asyncio.to_thread(self.proc.wait)
        _close_job(self._job)
        self._job = None
        if self.provider is not None:
            await self.provider.aclose()
            self.provider = None

    async def stop(self) -> None:
        self._publish(None)
        current = asyncio.current_task()
        for task in (self._operation, self._start_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        async with self._lock:
            await self._cleanup()
            self.state, self.detail = "unavailable", "stopped"

    async def switch(self, model_name: str, model_path: Path) -> None:
        if (
            self.state == "ready"
            and self.model_name == model_name
            and self.model_path == model_path.resolve()
            and self.proc is not None
            and self.proc.poll() is None
        ):
            if self.provider is not None:
                self._publish(FallbackProvider(self.provider, self.provider))
            return
        await self.stop()
        await self.start(model_name, model_path)

    def check(self) -> LlmHealth:
        if self.state == "ready":
            if self.proc is not None and self.proc.poll() is None:
                return LlmHealth(status="ok", detail="", model=self.model_name)
            self._publish(None)
            return LlmHealth(
                status="unavailable", detail="llama-server exited", model=self.model_name
            )
        return LlmHealth(
            status="degraded" if self.state == "starting" else "unavailable",
            detail=self.detail,
            model=self.model_name,
        )
