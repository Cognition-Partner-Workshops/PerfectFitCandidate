"""Real packaged-runtime smoke, executed by offline_smoke.ps1 as a standard user."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import hashlib
import io
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from pfc.adapters.llm.providers import PromptRequest
from pfc.adapters.vector.stores import NumpyStore
from pfc.db.connection import database
from pfc.jobs.runner import JobContext
from pfc.launcher import launch
from pfc.services.embeddings.reembed import reembed
from pfc.services.embeddings.runtime import create_embedder

# isort: split
from pfc.config import Settings


def token_elevated() -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
    security.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    token, elevated, size = wintypes.HANDLE(), wintypes.DWORD(), wintypes.DWORD()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not security.GetTokenInformation(
            token, 20, ctypes.byref(elevated), ctypes.sizeof(elevated), ctypes.byref(size)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(elevated.value)
    finally:
        kernel.CloseHandle(token)


def working_set(pid: int) -> int:
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak",
                "rss",
                "paged_peak",
                "paged",
                "nonpaged_peak",
                "nonpaged",
                "pagefile",
                "pagefile_peak",
            )
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x410, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not kernel.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(counters.rss)
    finally:
        kernel.CloseHandle(handle)


def tiny_reembed(settings: Settings, app: Any, digest: str) -> int:
    with database(settings) as db:
        cursor = db.execute(
            "INSERT INTO embedding_models(name,runtime,file_sha256,dim,normalised,"
            "pipeline_version,is_active) VALUES ('offline-smoke','onnx',?,768,1,'',0)",
            (digest,),
        )
        model_id = int(cursor.lastrowid or 0)
        db.execute(
            "INSERT INTO demands(demand_id,code_requisition,id_format_ok,demand_request_name,"
            "row_hash,created_at,updated_at) VALUES (1,'T1SMOKE',1,'Engineer','smoke',"
            "'2026-01-01','2026-01-01')"
        )
        db.execute(
            "INSERT INTO demand_versions(demand_version_id,demand_id,version_no,jd_text_raw,"
            "jd_text_norm,jd_sha256,created_at) VALUES (1,1,1,'SQL','SQL','smoke','2026-01-01')"
        )
        db.execute("UPDATE demands SET current_version_id=1 WHERE demand_id=1")

    def job(ctx: JobContext) -> None:
        embedder = create_embedder(settings, embedding_model_id=model_id)
        with database(settings) as db:
            store = NumpyStore(db, dimensions=768)
            db.commit()
            counts = reembed(
                settings, ctx, embedding_model_id=model_id, embedder=embedder, store=store
            )
            assert counts.embedded == 1 and store.count() == 1, counts

    return int(app.state.job_runner.submit("reembed", job))


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    assert sys.platform == "win32", "This smoke requires Windows"
    assert not token_elevated(), "Launcher process token must not be elevated"
    assert not ctypes.windll.shell32.IsUserAnAdmin(), "Smoke account must not be an admin"
    settings = Settings(
        port=0,
        frontend_dir=root / "web",
        llm_ctx_size=512,
        llm_threads=2,
        llm_startup_timeout_s=300,
    )
    assert not (settings.data_dir / "db" / "pfc.db").exists(), "Smoke must be first-run"
    models = json.loads((root / "MODELS.lock").read_text())
    for model in models:
        with (root / "models" / model["file"]).open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == model["sha256"]
    gguf = next(model for model in models if model["file"].endswith(".gguf"))
    onnx = next(model for model in models if model["file"].endswith(".onnx"))
    metrics: dict[str, Any] = {"token_elevated": False, "administrator": False, "peak_rss_bytes": 0}
    start = time.perf_counter()
    child = None

    def serve(app: Any, **kwargs: Any) -> None:
        nonlocal child
        server = uvicorn.Server(uvicorn.Config(app, **kwargs))

        async def sample() -> None:
            while not server.should_exit:
                rss = working_set(os.getpid())
                supervisor = getattr(app.state, "llm_supervisor", None)
                proc = supervisor.proc if supervisor is not None else None
                if proc is not None and proc.poll() is None:
                    with contextlib.suppress(OSError):
                        rss += working_set(proc.pid)
                metrics["peak_rss_bytes"] = max(metrics["peak_rss_bytes"], rss)
                await asyncio.sleep(0.1)

        async def exercise() -> None:
            nonlocal child
            try:
                async with asyncio.timeout(600):
                    while not server.started:
                        await asyncio.sleep(0.05)
                    async with httpx.AsyncClient(trust_env=False) as client:
                        health = await client.get(
                            f"http://127.0.0.1:{kwargs['port']}/api/v1/health"
                        )
                        assert health.status_code == 200 and health.json() == {"status": "ok"}
                        metrics["startup_seconds"] = time.perf_counter() - start
                        page = await client.get(f"http://127.0.0.1:{kwargs['port']}/setup")
                        assert page.status_code == 200
                    assert (settings.data_dir / "run" / "setup-code.txt").is_file()
                    supervisor = app.state.llm_supervisor
                    # CI exercises the pinned candidate; production Admin approval stays mandatory.
                    await supervisor.start(gguf["name"], root / "models" / gguf["file"])
                    assert supervisor.state == "ready" and supervisor.provider is not None
                    child = supervisor.proc
                    result = await supervisor.provider.generate(
                        PromptRequest(
                            messages=[{"role": "user", "content": "Reply with OK."}],
                            max_tokens=8,
                            temperature=0,
                            timeout_s=180,
                        )
                    )
                    assert result.text.strip(), "llama-server returned no generation"
                    metrics["generation_chars"] = len(result.text)
                    job_id = tiny_reembed(settings, app, onnx["sha256"])
                    while True:
                        job = app.state.job_runner.get(job_id)
                        assert job is not None
                        if job.status in {"completed", "failed", "cancelled"}:
                            assert job.status == "completed", job.message
                            break
                        await asyncio.sleep(0.1)
                    metrics["reembed_job_status"] = "completed"
                    metrics["models_ready_seconds"] = time.perf_counter() - start
            finally:
                server.should_exit = True

        async def run() -> None:
            sampler = asyncio.create_task(sample())
            checks = asyncio.create_task(exercise())
            try:
                await server.serve()
                await checks
            finally:
                server.should_exit = True
                for task in (sampler, checks):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        asyncio.run(run())

    # The launcher prints a bootstrap credential; never send it to CI logs/artifacts.
    with contextlib.redirect_stdout(io.StringIO()):
        assert launch(settings, no_browser=True, run_server=serve) == 0
    assert child is not None and child.poll() is not None, "llama-server survived shutdown"
    assert not token_elevated()
    metrics["shutdown_clean"] = True
    (root / "smoke-metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics))


if __name__ == "__main__":
    main()
