"""Versioned, secret-free Admin settings export (REQ-OCC-01)."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from pfc.config import Settings
from pfc.db.audit import append_audit
from pfc.security import package_signing as signing
from pfc.security.passwords import hasher, validate_password
from pfc.services.packages.common import (
    active_package,
    app_version,
    atomic_write,
    package_lock,
    timestamp,
    transaction,
)
from pfc.services.packages.keys import SigningKeys
from pfc.services.packages.payloads import SETTINGS_SCHEMA, encode_snapshot, snapshot


@dataclass(frozen=True)
class ExportResult:
    path: Path
    version: int
    one_time_passwords: dict[str, str] = field(repr=False)


def export_settings(
    settings: Settings,
    *,
    passphrase: str,
    actor_id: int,
    keys: SigningKeys | None = None,
    min_app_version: str | None = None,
    max_app_version: str | None = None,
) -> ExportResult:
    """Reserve a monotonic version; OTP plaintext is returned once, never stored (REQ-OCC-01)."""
    keys = keys or SigningKeys(settings)
    with package_lock(settings), transaction(settings) as db:
        private = keys.get_or_create(passphrase)
        state_path = settings.data_dir / "keys" / "export-version.json"
        last = (
            int(json.loads(state_path.read_bytes())["last_exported"]) if state_path.exists() else 0
        )
        active = active_package(db)
        number = max(last, active["package_version"] if active else 0) + 1
        atomic_write(state_path, signing.canonical_json({"last_exported": number}))
        data = snapshot(db)
        otps: dict[str, str] = {}
        for account in data["analysts"]:
            otp = secrets.token_urlsafe(24)
            validate_password(otp, account["email"], "Analyst")
            account["password_hash"] = hasher(settings).hash(otp)
            account["must_change_password"] = True
            otps[account["email"]] = otp
        now = timestamp()
        package = signing.build_settings_package(
            encode_snapshot(data),
            private=private,
            package_version=number,
            schema_version=SETTINGS_SCHEMA,
            min_app_version=min_app_version or app_version(),
            max_app_version=max_app_version or app_version(),
            created_at=now,
        )
        path = settings.data_dir / "exports" / f"PFC-settings-v{number}.pfcpkg"
        atomic_write(path, package)
        append_audit(
            db,
            ts=now,
            action="settings_package.export",
            user_id=actor_id,
            entity_type="settings_package",
            entity_id=str(number),
            details={
                "package_version": number,
                "signing_key_id": signing.key_id(private.public_key()),
            },
        )
    return ExportResult(path, number, otps)
