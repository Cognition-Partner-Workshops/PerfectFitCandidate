"""Offline, time-bounded conversion of legacy Office documents."""

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock

conversion_lock = RLock()


class ConverterUnavailable(RuntimeError):
    pass


def find_soffice() -> Path | None:
    override = os.environ.get("PFC_SOFFICE")
    if override:
        found = shutil.which(override)
        return Path(found) if found else None
    roots = [Path(sys.executable).resolve().parent.parent]
    if home := os.environ.get("PFC_HOME"):
        roots.insert(0, Path(home))
    for root in roots:
        bundled = root / "libreoffice" / "program" / "soffice.exe"
        if bundled.is_file():
            return bundled
    found = shutil.which("soffice")
    return Path(found) if found else None


def converter_available() -> bool:
    return find_soffice() is not None


def kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def convert_legacy(path: Path, *, timeout: float, work_dir: Path) -> tuple[bytes, str]:
    executable = find_soffice()
    if executable is None:
        raise ConverterUnavailable("Legacy CV format requires LibreOffice")
    target = {".doc": "docx", ".ppt": "pptx"}[path.suffix.lower()]
    work_dir.mkdir(parents=True, exist_ok=True)
    profile = work_dir.parent / "lo-profile"
    with conversion_lock, TemporaryDirectory(prefix="convert-", dir=work_dir) as directory:
        process = subprocess.Popen(
            [
                str(executable),
                "--headless",
                "--norestore",
                "--nolockcheck",
                f"-env:UserInstallation={profile.resolve().as_uri()}",
                "--convert-to",
                target,
                "--outdir",
                directory,
                str(path.resolve()),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
        try:
            result = process.wait(timeout=timeout)
        except BaseException:
            kill_process_tree(process)
            raise
        if result:
            raise RuntimeError(f"LibreOffice conversion failed ({result})")
        return (Path(directory) / f"{path.stem}.{target}").read_bytes(), target
