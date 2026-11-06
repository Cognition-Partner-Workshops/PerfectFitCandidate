"""Startup-callable preview-only package inbox (REQ-OCC-02/03)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from pfc.config import Settings
from pfc.services.packages.common import PackageServiceError, atomic_write, package_lock
from pfc.services.packages.importer import SettingsImporter, SettingsPreview
from pfc.services.packages.models import JobSubmitter, ModelApplyResult, ModelImporter
from pfc.services.packages.staging import purge_expired_previews


@dataclass(frozen=True)
class PendingPackage:
    path: str
    sha256: str
    kind: str
    version: int | None
    valid: bool
    error: str | None


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


class PackageInbox:
    def __init__(self, settings: Settings, *, jobs: JobSubmitter | None = None) -> None:
        self.settings, self.jobs = settings, jobs
        self._pending: list[PendingPackage] | None = None

    @property
    def _state(self) -> Path:
        return self.settings.data_dir / "pending-packages.json"

    def scan(self) -> list[PendingPackage]:
        """Scan and persist validation results only; never import (REQ-OCC-02)."""
        pending: list[PendingPackage] = []
        inbox = self.settings.data_dir / "inbox"
        with package_lock(self.settings):
            for path in sorted(inbox.glob("*")):
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or path.suffix not in {".pfcpkg", ".pfcmodel"}
                ):
                    continue
                kind = "settings" if path.suffix == ".pfcpkg" else "model"
                digest = ""
                try:
                    digest = _digest(path)
                    preview = (
                        SettingsImporter(self.settings).preview(path)
                        if kind == "settings"
                        else ModelImporter(self.settings).preview(path)
                    )
                    if digest != _digest(path):
                        raise PackageServiceError(
                            "inbox_changed", "Inbox file changed during preview"
                        )
                    item = PendingPackage(
                        str(path.resolve()),
                        digest,
                        kind,
                        preview.manifest.package_version,
                        True,
                        None,
                    )
                except (ValueError, OSError) as exc:
                    reason = str(getattr(exc, "reason", "unreadable_package"))
                    item = PendingPackage(str(path.resolve()), digest, kind, None, False, reason)
                pending.append(item)
            atomic_write(self._state, json.dumps([asdict(item) for item in pending]).encode())
            self._pending = pending
        return list(pending)

    def list_pending(self) -> list[PendingPackage]:
        if self._pending is None:
            self._pending = (
                [PendingPackage(**item) for item in json.loads(self._state.read_bytes())]
                if self._state.exists()
                else []
            )
        return list(self._pending)

    def apply_pending(self, path: Path, *, actor_id: int) -> SettingsPreview | ModelApplyResult:
        matching = next(
            (item for item in self.list_pending() if item.path == str(path.resolve())), None
        )
        if matching is None or not matching.valid:
            raise PackageServiceError("not_pending", "Select a valid previewed inbox package")
        if (
            path.is_symlink()
            or path.resolve().parent != (self.settings.data_dir / "inbox").resolve()
        ):
            raise PackageServiceError("inbox_path", "Pending packages must remain in the inbox")
        if _digest(path) != matching.sha256:
            raise PackageServiceError("inbox_changed", "Pending file changed; preview it again")
        if matching.kind == "settings":
            package = path.read_bytes()
            if hashlib.sha256(package).hexdigest() != matching.sha256:
                raise PackageServiceError("inbox_changed", "Pending settings package changed")
            return SettingsImporter(self.settings).apply(package, actor_id=actor_id)
        return ModelImporter(self.settings, jobs=self.jobs).apply(
            path, actor_id=actor_id, expected_sha256=matching.sha256
        )


def scan_inbox(settings: Settings) -> list[PendingPackage]:
    """Startup integration point, without HTTP/lifespan changes (REQ-OCC-02)."""
    purge_expired_previews(settings)
    return PackageInbox(settings).scan()


def list_pending(settings: Settings) -> list[PendingPackage]:
    return PackageInbox(settings).list_pending()
