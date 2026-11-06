"""Session-owned, expiring confirmation tokens for verified uploads (REQ-OCC-02/03)."""

import hashlib
import json
import os
import secrets
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Literal, TypeVar

from filelock import FileLock

from pfc.config import Settings
from pfc.security.package_signing import MODEL_LIMITS, PackageLimits
from pfc.services.packages.common import PackageServiceError, atomic_write
from pfc.services.packages.importer import SettingsImporter, SettingsPreview
from pfc.services.packages.models import ModelImporter, ModelPreview

Kind = Literal["settings", "model"]
Result = TypeVar("Result")
PREVIEW_LIFETIME_SECONDS = 3600


def upload_limit(kind: Kind) -> int:
    return (MODEL_LIMITS if kind == "model" else PackageLimits()).max_total_bytes


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class UploadStaging:
    def __init__(self, settings: Settings, *, clock: Callable[[], int] | None = None) -> None:
        self.settings = settings
        self.directory = settings.data_dir / "packages" / "staged"
        self.clock = clock or (lambda: int(time.time()))
        self.expired_tokens = self.purge_expired()

    def _purge_expired(self, now: int) -> set[str]:
        expired = set()
        for metadata in self.directory.glob("*.json"):
            stored = json.loads(metadata.read_bytes())
            if stored["expires_at"] <= now:
                (self.directory / f"{metadata.stem}.upload").unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
                expired.add(metadata.stem)
        return expired

    def purge_expired(self, *, now: int | None = None) -> set[str]:
        if not self.directory.exists():
            return set()
        with FileLock(self.directory / ".staging.lock", timeout=30):
            return self._purge_expired(self.clock() if now is None else now)

    def preview(
        self, stream: BinaryIO, *, kind: Kind, user_id: int, session_id: int, now: int
    ) -> tuple[str, SettingsPreview | ModelPreview]:
        self.purge_expired(now=now)
        self.directory.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        path = self.directory / f"{token}.upload"
        metadata = self.directory / f"{token}.json"
        fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as output:
                total = 0
                while block := stream.read(1024 * 1024):
                    total += len(block)
                    if total > upload_limit(kind):
                        raise PackageServiceError("size_limit", "Upload is too large")
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            staged = Path(temporary)
            preview = (
                SettingsImporter(self.settings).preview(staged)
                if kind == "settings"
                else ModelImporter(self.settings).preview(staged)
            )
            with FileLock(self.directory / ".staging.lock", timeout=30):
                staged.replace(path)
                atomic_write(
                    metadata,
                    json.dumps(
                        {
                            "kind": kind,
                            "user_id": user_id,
                            "session_id": session_id,
                            "expires_at": now + PREVIEW_LIFETIME_SECONDS,
                            "sha256": file_digest(path),
                        }
                    ).encode(),
                )
            return token, preview
        except BaseException:
            path.unlink(missing_ok=True)
            metadata.unlink(missing_ok=True)
            raise
        finally:
            Path(temporary).unlink(missing_ok=True)

    def apply(
        self,
        token: str,
        *,
        kind: Kind,
        user_id: int,
        session_id: int,
        now: int,
        operation: Callable[[Path], Result],
    ) -> Result:
        if not self.directory.exists():
            raise PackageServiceError("preview_missing", "Preview not found")
        with FileLock(self.directory / ".staging.lock", timeout=30):
            expired = self._purge_expired(now) | self.expired_tokens
            if token in expired:
                raise PackageServiceError("preview_expired", "Preview expired")
            path = self.directory / f"{token}.upload"
            metadata = self.directory / f"{token}.json"
            if not metadata.is_file() or not path.is_file():
                raise PackageServiceError("preview_missing", "Preview not found")
            stored = json.loads(metadata.read_bytes())
            if stored["user_id"] != user_id or stored["session_id"] != session_id:
                raise PackageServiceError("preview_owner", "Preview belongs to another session")
            if stored["expires_at"] <= now:
                metadata.unlink()
                path.unlink()
                raise PackageServiceError("preview_expired", "Preview expired")
            if stored["kind"] != kind:
                raise PackageServiceError("wrong_kind", "Wrong confirmation endpoint")
            if file_digest(path) != stored["sha256"]:
                raise PackageServiceError("preview_changed", "Staged upload changed")
            result = operation(path)
            metadata.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            return result


def purge_expired_previews(settings: Settings, *, clock: Callable[[], int] | None = None) -> None:
    """Startup cleanup for unused preview uploads (REQ-OCC-02/03)."""
    UploadStaging(settings, clock=clock)
