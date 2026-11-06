import os
import secrets
import tempfile
from pathlib import Path

from pfc.config import Settings


def code_path(settings: Settings) -> Path:
    return settings.data_dir / "run" / "setup-code.txt"


def generate_setup_code(settings: Settings) -> str:
    path = code_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    code = secrets.token_urlsafe(32)
    # Windows per-user ACL protection relies on the profile data directory.
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".setup-code-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(code)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return code


def verify_setup_code(settings: Settings, code: str) -> bool:
    try:
        expected = code_path(settings).read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return secrets.compare_digest(expected.encode(), code.encode())


def consume_setup_code(settings: Settings) -> None:
    code_path(settings).unlink(missing_ok=True)
