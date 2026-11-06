"""Portable configuration snapshots and diffs (REQ-OCC-01/02).

Business records and local secret ownership never cross the settings boundary.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from argon2 import Type, extract_parameters
from argon2.exceptions import InvalidHashError

from pfc.services.packages.common import PackageServiceError

TABLES: dict[str, tuple[str, ...]] = {
    "skills": ("skill_id", "name", "category", "created_by", "is_active"),
    "skill_synonyms": (
        "synonym_id",
        "skill_id",
        "term",
        "term_norm",
        "relation",
        "credit",
        "status",
        "updated_at",
    ),
    "scoring_templates": (
        "template_id",
        "name",
        "version",
        "role_family",
        "weights_json",
        "rules_json",
        "status",
        "created_at",
        "activated_at",
    ),
    "grades": ("code", "ordinal", "active"),
    "bench_statuses": (
        "code",
        "label",
        "is_excluded",
        "priority",
        "flag_in_gaps",
        "available_from_bench_start",
    ),
    "status_map": ("source_value", "canonical_code", "priority", "is_excluded", "updated_at"),
    "resource_team_recipients": ("recipient_id", "email", "display_name", "is_active", "added_at"),
    "internal_email_domains": ("domain", "added_at"),
    "email_templates": (
        "email_template_id",
        "name",
        "version",
        "subject",
        "body_html",
        "is_default",
        "created_at",
    ),
    "llm_models": (
        "model_id",
        "name",
        "file_name",
        "file_sha256",
        "size_mb",
        "ram_estimate_mb",
        "context_tokens",
        "licence",
        "approved",
        "benchmark_json",
        "is_active",
    ),
    "llm_providers": (
        "provider_id",
        "name",
        "kind",
        "enabled",
        "fallback_order",
        "base_url",
        "api_version",
        "compatible_versions_json",
        "model",
        "capabilities_json",
        "timeout_s",
        "max_concurrency",
        "updated_at",
    ),
}
SYSTEM_KEYS = frozenset(
    {"dl.approvals", "ct.approvals", "ingestion.dl.approvals", "ingestion.ct.approvals"}
)
EXTRA_PAYLOADS = {"settings", "analysts", "dl_approvals", "ct_approvals"}
Row = dict[str, Any]
Snapshot = dict[str, list[Row]]


@dataclass(frozen=True)
class PayloadDiff:
    added: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]


def _safe_provider(row: Row) -> None:
    if row.get("base_url"):
        url = urlsplit(row["base_url"])
        if url.username or url.password or url.query or url.fragment:
            raise PackageServiceError(
                "secret_provider_url", "Provider URL contains credentials/query"
            )
    if row.get("capabilities_json") is not None:
        try:
            capabilities = json.loads(row["capabilities_json"])
        except (ValueError, TypeError) as exc:
            raise PackageServiceError(
                "provider_capabilities", "Invalid provider capabilities JSON"
            ) from exc
        row["capabilities_json"] = json.dumps(_without_secrets(capabilities), sort_keys=True)


def _without_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_secrets(item)
            for key, item in value.items()
            if not any(
                word in key.casefold()
                for word in (
                    "secret",
                    "password",
                    "credential",
                    "token",
                    "api_key",
                    "authorization",
                )
            )
        }
    if isinstance(value, list):
        return [_without_secrets(item) for item in value]
    return value


def snapshot(db: sqlite3.Connection) -> Snapshot:
    result: Snapshot = {}
    for table, columns in TABLES.items():
        where = " WHERE approved=1" if table == "llm_models" else ""
        result[table] = [
            dict(row)
            for row in db.execute(
                f"SELECT {','.join(columns)} FROM {table}{where} ORDER BY {columns[0]}"
            )
        ]
    for row in result["llm_providers"]:
        _safe_provider(row)
    result["settings"] = [
        dict(row)
        for row in db.execute(
            "SELECT key, value_json FROM settings WHERE scope='system' AND user_id IS NULL "
            "AND admin_only=0 ORDER BY key"
        )
        if row["key"] in SYSTEM_KEYS
    ]
    result["analysts"] = [
        dict(row)
        for row in db.execute(
            "SELECT email, display_name, persona, password_hash, must_change_password, "
            "roles.name AS role "
            "FROM users JOIN roles USING(role_id) WHERE roles.name='Analyst' AND is_active=1 "
            "ORDER BY email COLLATE NOCASE"
        )
    ]
    result["dl_approvals"] = [
        dict(row)
        for row in db.execute(
            "SELECT code_requisition, occ_reviewed_at FROM demands "
            "WHERE occ_reviewed_at IS NOT NULL "
            "ORDER BY code_requisition"
        )
    ]
    result["ct_approvals"] = [
        dict(row)
        for row in db.execute(
            "SELECT short_name_key, email_key, occ_reviewed_at FROM resources "
            "WHERE occ_reviewed_at IS NOT NULL ORDER BY short_name_key, email_key"
        )
    ]
    return result


def encode_snapshot(data: Snapshot) -> dict[str, Any]:
    return {f"{name}.v1.json": rows for name, rows in data.items()}


def decode_snapshot(payloads: dict[str, Any]) -> Snapshot:
    names = {*TABLES, *EXTRA_PAYLOADS}
    if set(payloads) != {f"{name}.v1.json" for name in names}:
        raise PackageServiceError(
            "payload_schema", "Settings payload set is incomplete or unexpected"
        )
    result: Snapshot = {}
    extra_columns = {
        "settings": ("key", "value_json"),
        "analysts": (
            "email",
            "display_name",
            "persona",
            "password_hash",
            "must_change_password",
            "role",
        ),
        "dl_approvals": ("code_requisition", "occ_reviewed_at"),
        "ct_approvals": ("short_name_key", "email_key", "occ_reviewed_at"),
    }
    for name in names:
        rows = payloads[f"{name}.v1.json"]
        columns = TABLES.get(name, extra_columns.get(name, ()))
        if not isinstance(rows, list) or any(
            not isinstance(row, dict)
            or set(row) - ({"is_active"} if name == "analysts" else set()) != set(columns)
            for row in rows
        ):
            raise PackageServiceError("payload_schema", f"Invalid rows in {name}")
        if len({_row_key(name, row) for row in rows}) != len(rows):
            raise PackageServiceError("payload_schema", f"Duplicate keys in {name}")
        result[name] = rows
    for row in result["analysts"]:
        if (
            not isinstance(row["password_hash"], str)
            or not row["password_hash"].startswith("$argon2id$")
            or row["must_change_password"] != 1
            or row["role"] != "Analyst"
            or not isinstance(row["email"], str)
            or row.get("is_active", 1) not in (0, 1)
        ):
            raise PackageServiceError("payload_schema", "Invalid imported Analyst account")
        try:
            if extract_parameters(row["password_hash"]).type != Type.ID:
                raise ValueError("Expected Argon2id")
        except (InvalidHashError, ValueError) as exc:
            raise PackageServiceError("payload_schema", "Invalid imported password hash") from exc
    for row in result["settings"]:
        if row["key"] not in SYSTEM_KEYS:
            raise PackageServiceError("payload_schema", "Nonportable settings key")
    for row in result["llm_providers"]:
        _safe_provider(row)
    return result


def _row_key(name: str, row: Row) -> str:
    if name == "ct_approvals":
        return json.dumps([row["short_name_key"], row["email_key"]])
    field = {"analysts": "email", "settings": "key", "dl_approvals": "code_requisition"}.get(name)
    value = row[field or TABLES[name][0]]
    return str(value).casefold() if name == "analysts" else str(value)


def diff(before: Snapshot, after: Snapshot) -> dict[str, PayloadDiff]:
    result: dict[str, PayloadDiff] = {}
    for name, rows in after.items():
        old = {_row_key(name, row): row for row in before[name]}
        new = {_row_key(name, row): row for row in rows}
        result[name] = PayloadDiff(
            tuple(sorted(new.keys() - old.keys())),
            tuple(sorted(key for key in old.keys() & new.keys() if old[key] != new[key])),
            tuple(sorted(old.keys() - new.keys())),
        )
    return result


def replace_snapshot(
    db: sqlite3.Connection,
    data: Snapshot,
    *,
    actor_id: int,
    now: str,
    preserve_credentials: bool = False,
) -> None:
    tokens = {
        row["name"]: (row["token_blob"], row["owner_user_id"])
        for row in db.execute("SELECT name, token_blob, owner_user_id FROM llm_providers")
    }
    for table in reversed(TABLES):
        db.execute(f"DELETE FROM {table}")
    for table, columns in TABLES.items():
        for row in data[table]:
            names = list(columns)
            values = [row[name] for name in names]
            if table == "llm_providers":
                names += ["token_blob", "owner_user_id"]
                values += list(tokens.get(row["name"], (None, None)))
            db.execute(
                f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                values,
            )
    for key in SYSTEM_KEYS:
        db.execute(
            "DELETE FROM settings WHERE scope='system' AND user_id IS NULL AND key=? "
            "AND admin_only=0",
            (key,),
        )
    for row in data["settings"]:
        db.execute(
            "INSERT INTO settings(scope, key, value_json, admin_only, updated_by, updated_at) "
            "VALUES ('system', ?, ?, 0, ?, ?)",
            (row["key"], row["value_json"], actor_id, now),
        )
    role_id = db.execute("SELECT role_id FROM roles WHERE name='Analyst'").fetchone()[0]
    for row in data["analysts"]:
        existing = db.execute(
            "SELECT users.user_id, roles.name FROM users JOIN roles USING(role_id) "
            "WHERE email=? COLLATE NOCASE",
            (row["email"],),
        ).fetchone()
        if existing and existing["name"] == "Admin":
            continue
        if existing and preserve_credentials:
            if "is_active" in row:
                db.execute(
                    "UPDATE users SET is_active=? WHERE user_id=?",
                    (row["is_active"], existing["user_id"]),
                )
            continue
        db.execute(
            "INSERT INTO users(email, display_name, persona, role_id, password_hash, "
            "must_change_password, is_active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, "
            "1, 1, ?, ?) ON CONFLICT(email) DO UPDATE SET display_name=excluded.display_name, "
            "persona=excluded.persona, role_id=excluded.role_id, "
            "password_hash=excluded.password_hash, must_change_password=1, is_active=1, "
            "failed_attempts=0, locked_until=NULL, updated_at=excluded.updated_at",
            (
                row["email"],
                row["display_name"],
                row["persona"],
                role_id,
                row["password_hash"],
                now,
                now,
            ),
        )
        if existing:
            db.execute(
                "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (now, existing["user_id"]),
            )
    db.execute("UPDATE demands SET occ_reviewed_at=NULL, occ_reviewed_by=NULL")
    db.execute("UPDATE resources SET occ_reviewed_at=NULL, occ_reviewed_by=NULL")
    for row in data["dl_approvals"]:
        changed = db.execute(
            "UPDATE demands SET occ_reviewed_at=?, occ_reviewed_by=? WHERE code_requisition=?",
            (row["occ_reviewed_at"], actor_id, row["code_requisition"]),
        )
        if changed.rowcount != 1:
            raise PackageServiceError(
                "approval_target_missing", "DL approval has no matching local demand"
            )
    for row in data["ct_approvals"]:
        changed = db.execute(
            "UPDATE resources SET occ_reviewed_at=?, occ_reviewed_by=? "
            "WHERE short_name_key=? AND email_key=?",
            (row["occ_reviewed_at"], actor_id, row["short_name_key"], row["email_key"]),
        )
        if changed.rowcount != 1:
            raise PackageServiceError(
                "approval_target_missing", "CT approval has no matching local resource"
            )
