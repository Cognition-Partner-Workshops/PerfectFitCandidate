"""Verified preview, atomic settings apply and retained rollback (REQ-OCC-02)."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pfc.config import Settings
from pfc.db.audit import append_audit
from pfc.security import package_signing as signing
from pfc.services.packages.common import (
    PackageServiceError,
    active_package,
    app_version,
    atomic_write,
    package_lock,
    readonly_database,
    source_bytes,
    timestamp,
    transaction,
)
from pfc.services.packages.keys import load_trusted_keys
from pfc.services.packages.payloads import (
    PayloadDiff,
    decode_snapshot,
    diff,
    replace_snapshot,
    snapshot,
)


@dataclass(frozen=True)
class SettingsPreview:
    manifest: signing.Manifest
    changes: dict[str, PayloadDiff]
    imported_accounts: int


def _latest_version(db: sqlite3.Connection) -> int:
    return int(
        db.execute("SELECT COALESCE(MAX(package_version), 0) FROM settings_packages").fetchone()[0]
    )


class SettingsImporter:
    def __init__(
        self, settings: Settings, *, failure_hook: Callable[[], None] | None = None
    ) -> None:
        self.settings = settings
        self.failure_hook = failure_hook

    def preview(self, source: Path | bytes) -> SettingsPreview:
        """No writes, staging, audit or apply side effects (REQ-OCC-02)."""
        package = source_bytes(source)
        with readonly_database(self.settings) as db:
            verified = signing.verify_package(
                package,
                trusted_keys=load_trusted_keys(self.settings),
                app_version=app_version(),
                current_version=_latest_version(db),
            )
            data = decode_snapshot(verified.settings_payloads)
            return SettingsPreview(
                verified.manifest, diff(snapshot(db), data), len(data["analysts"])
            )

    def apply(self, source: Path | bytes, *, actor_id: int) -> SettingsPreview:
        package = source_bytes(source)
        preview = self.preview(package)
        path = self.settings.data_dir / "packages" / f"v{preview.manifest.package_version}.pfcpkg"
        installed = False
        try:
            with package_lock(self.settings), transaction(self.settings) as db:
                active = active_package(db)
                verified = signing.verify_package(
                    package,
                    trusted_keys=load_trusted_keys(self.settings),
                    app_version=app_version(),
                    current_version=_latest_version(db),
                )
                data = decode_snapshot(verified.settings_payloads)
                preview = SettingsPreview(
                    verified.manifest, diff(snapshot(db), data), len(data["analysts"])
                )
                if path.exists() and path.read_bytes() != package:
                    raise PackageServiceError(
                        "version_collision", "Retained package version already exists"
                    )
                now = timestamp()
                replace_snapshot(db, data, actor_id=actor_id, now=now)
                if self.failure_hook:
                    self.failure_hook()
                db.execute("UPDATE settings_packages SET is_active=0 WHERE is_active=1")
                db.execute(
                    "INSERT INTO settings_packages(package_version, schema_version, app_version, "
                    "package_sha256, signing_key_id, imported_by, imported_at, "
                    "previous_package_id, "
                    "is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        verified.manifest.package_version,
                        str(verified.manifest.schema_version),
                        app_version(),
                        hashlib.sha256(package).hexdigest(),
                        verified.trusted_key_id,
                        actor_id,
                        now,
                        active["package_id"] if active else None,
                    ),
                )
                append_audit(
                    db,
                    ts=now,
                    action="settings_package.import",
                    user_id=actor_id,
                    entity_type="settings_package",
                    entity_id=str(verified.manifest.package_version),
                )
                if not path.exists():
                    atomic_write(path, package)
                    installed = True
            return preview
        except BaseException:
            if installed:
                path.unlink(missing_ok=True)
            raise

    def rollback(self, *, actor_id: int) -> SettingsPreview:
        """Restore only the retained previous, trusted, hash-pinned package (REQ-OCC-02)."""
        with package_lock(self.settings), transaction(self.settings) as db:
            active = active_package(db)
            previous = (
                db.execute(
                    "SELECT * FROM settings_packages WHERE package_id=?",
                    (active["previous_package_id"],),
                ).fetchone()
                if active
                else None
            )
            if previous is None:
                raise PackageServiceError(
                    "no_previous_version", "No previous settings package is retained"
                )
            assert active is not None
            path = self.settings.data_dir / "packages" / f"v{previous['package_version']}.pfcpkg"
            package = path.read_bytes()
            if hashlib.sha256(package).hexdigest() != previous["package_sha256"]:
                raise PackageServiceError(
                    "retained_hash", "Retained package does not match import history"
                )
            verified = signing.verify_package(
                package,
                trusted_keys=load_trusted_keys(self.settings),
                app_version=app_version(),
                current_version=0,
            )
            data = decode_snapshot(verified.settings_payloads)
            preview = SettingsPreview(
                verified.manifest, diff(snapshot(db), data), len(data["analysts"])
            )
            now = timestamp()
            replace_snapshot(db, data, actor_id=actor_id, now=now, preserve_credentials=True)
            if self.failure_hook:
                self.failure_hook()
            db.execute("UPDATE settings_packages SET is_active=0 WHERE is_active=1")
            db.execute(
                "UPDATE settings_packages SET is_active=1 WHERE package_id=?",
                (previous["package_id"],),
            )
            append_audit(
                db,
                ts=now,
                action="settings_package.rollback",
                user_id=actor_id,
                entity_type="settings_package",
                entity_id=str(active["package_version"]),
                details={"restored_version": previous["package_version"]},
            )
            return preview
