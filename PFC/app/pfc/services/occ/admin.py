import csv
import io
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from pfc.config import Settings
from pfc.db.audit import verify_chain
from pfc.security.problems import Problem
from pfc.security.secrets_store import (
    PREFIX,
    SecretStore,
    SecretStoreError,
    redact_sensitive,
    sensitive_key,
    sensitive_setting,
)
from pfc.services.occ.common import audit_write, get_row, now

_MISSING = object()


def _restore_redacted(value: Any, previous: Any, key: str, settings: Settings) -> Any:
    def existing_at(path: tuple[Any, ...]) -> Any:
        nonlocal previous
        if isinstance(previous, str) and previous.startswith(PREFIX):
            try:
                previous = json.loads(SecretStore(settings.data_dir).decrypt(previous))
            except (SecretStoreError, ValueError, UnicodeError) as exc:
                raise Problem(
                    503, "SECRET_STORE_UNAVAILABLE", "Secret storage unavailable"
                ) from exc
        old = previous
        for part in path:
            if isinstance(old, dict) and isinstance(part, str):
                old = old.get(part, _MISSING)
            elif isinstance(old, list) and isinstance(part, tuple):
                identity, expected = part
                matches = [
                    item
                    for item in old
                    if isinstance(item, dict)
                    and identity in item
                    and type(item[identity]) is type(expected)
                    and item[identity] == expected
                ]
                if len(matches) != 1:
                    return _MISSING
                old = matches[0]
            else:
                return _MISSING
        return old

    def restore(node: Any, path: tuple[Any, ...], sensitive: bool) -> Any:
        if isinstance(node, dict):
            return {
                k: restore(v, (*path, k), sensitive or sensitive_key(str(k)))
                for k, v in node.items()
            }
        if isinstance(node, list):
            result = []
            for item in node:
                identity = next(
                    (
                        (name, item[name])
                        for name in ("id", "name", "key", "provider")
                        if isinstance(item, dict) and name in item
                    ),
                    None,
                )
                result.append(restore(item, (*path, identity), sensitive))
            return result
        if node == "[REDACTED]":
            old = existing_at(path)
            if None in path or (old is _MISSING and any(isinstance(p, tuple) for p in path)):
                raise Problem(
                    422, "VALIDATION_ERROR", "Re-enter the credential; no unique identity match"
                )
            if sensitive or (isinstance(old, str) and old.startswith(PREFIX)):
                if old is _MISSING:
                    raise Problem(
                        422, "VALIDATION_ERROR", "Redacted credential has no existing value"
                    )
                return old
        return node

    return restore(value, (), sensitive_key(key))


def recipients(db: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    """REQ-EML-05: only explicitly entered resource-team addresses."""
    return [
        dict(r) for r in db.execute("SELECT * FROM resource_team_recipients ORDER BY recipient_id")
    ]


def save_recipient(
    db: sqlite3.Connection, user_id: int, data: dict[str, Any], target: int | None = None
) -> dict[str, Any]:
    before = get_row(db, "resource_team_recipients", "recipient_id", target) if target else None
    if target is None:
        cursor = db.execute(
            "INSERT INTO resource_team_recipients(email,display_name,added_by,added_at) "
            "VALUES (?,?,?,?)",
            (data["email"], data.get("display_name"), user_id, now()),
        )
        target = int(cursor.lastrowid or 0)
    elif data:
        db.execute(
            "UPDATE resource_team_recipients SET "
            + ",".join(f"{key}=?" for key in data)
            + " WHERE recipient_id=?",
            (*data.values(), target),
        )
    after = get_row(db, "resource_team_recipients", "recipient_id", target)
    audit_write(db, user_id, "OCC_EDIT", "resource_team_recipients", target, before, after)
    return after


def delete_recipient(db: sqlite3.Connection, user_id: int, target: int) -> None:
    before = get_row(db, "resource_team_recipients", "recipient_id", target)
    db.execute("DELETE FROM resource_team_recipients WHERE recipient_id=?", (target,))
    audit_write(db, user_id, "OCC_EDIT", "resource_team_recipients", target, before, None)


def normalize_domain(domain: str) -> str:
    domain = domain.strip().lstrip("@").strip().lower()
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if len(domain) > 253 or not re.fullmatch(rf"{label}(?:\.{label})+", domain):
        raise Problem(422, "VALIDATION_ERROR", "Invalid domain")
    return domain


def domains(db: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    """REQ-CLS-01: explicit normalized internal-domain allowlist."""
    return [dict(r) for r in db.execute("SELECT * FROM internal_email_domains ORDER BY domain")]


def preview_domains(db: sqlite3.Connection, user_id: int, values: list[str]) -> dict[str, Any]:
    """REQ-CLS-01: preview CT email addresses using GUI_OCC §16.9 exact-match semantics."""
    counts = dict.fromkeys((normalize_domain(value) for value in values), 0)
    total = internal = unknown = 0
    for row in db.execute("SELECT email_address FROM resources"):
        total += 1
        email = (row["email_address"] or "").strip().lower()
        if not email:
            unknown += 1
        elif email.count("@") == 1 and email.rsplit("@", 1)[1] in counts:
            counts[email.rsplit("@", 1)[1]] += 1
            internal += 1
    return {
        "total": total,
        "internal": internal,
        "external": total - internal - unknown,
        "unknown": unknown,
        "per_domain": [
            {"domain": domain, "matches": matches} for domain, matches in counts.items()
        ],
    }


def add_domain(db: sqlite3.Connection, user_id: int, domain: str) -> dict[str, Any]:
    domain = normalize_domain(domain)
    db.execute(
        "INSERT INTO internal_email_domains(domain,added_by,added_at) VALUES (?,?,?)",
        (domain, user_id, now()),
    )
    after = get_row(db, "internal_email_domains", "domain", domain)
    audit_write(db, user_id, "OCC_EDIT", "internal_email_domains", domain, None, after)
    return after


def delete_domain(db: sqlite3.Connection, user_id: int, domain: str) -> None:
    domain = normalize_domain(domain)
    before = get_row(db, "internal_email_domains", "domain", domain)
    db.execute("DELETE FROM internal_email_domains WHERE domain=?", (domain,))
    audit_write(db, user_id, "OCC_EDIT", "internal_email_domains", domain, before, None)


def settings_get(db: sqlite3.Connection, user_id: int, settings: Settings) -> dict[str, Any]:
    """REQ-ROLE-04: system settings, excluding dedicated reference endpoints."""
    result = {}
    for row in db.execute(
        "SELECT key,value_json FROM settings WHERE scope='system' AND user_id IS NULL "
        "AND key!='location.aliases' ORDER BY key"
    ):
        value = json.loads(row["value_json"])
        if isinstance(value, str) and value.startswith(PREFIX):
            try:
                value = json.loads(SecretStore(settings.data_dir).decrypt(value))
                if not sensitive_setting(row["key"], value):
                    value = "[REDACTED]"
            except (SecretStoreError, ValueError, UnicodeError):
                result[row["key"]] = "[REDACTED]"
                continue
        result[row["key"]] = redact_sensitive(value, row["key"])
    return result


def settings_put(
    db: sqlite3.Connection, user_id: int, values: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    for key in values:
        if key == "location.aliases" or not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)+", key):
            raise Problem(422, "VALIDATION_ERROR", "Invalid or reserved settings key")
    before = settings_get(db, user_id, settings)
    try:
        encoded = {key: json.dumps(value, allow_nan=False) for key, value in values.items()}
    except (ValueError, TypeError) as exc:
        raise Problem(422, "VALIDATION_ERROR", "Settings values must be JSON") from exc
    for key, value in values.items():
        previous = db.execute(
            "SELECT value_json FROM settings WHERE scope='system' AND user_id IS NULL AND key=?",
            (key,),
        ).fetchone()
        previous_value = json.loads(previous[0]) if previous else _MISSING
        restored = _restore_redacted(value, previous_value, key, settings)
        if key in before and before[key] == value:
            continue
        value = restored
        encoded[key] = json.dumps(value, allow_nan=False)
        secret = sensitive_setting(key, value)
        encrypted_audit = secret or (
            isinstance(previous_value, str) and previous_value.startswith(PREFIX)
        )
        if secret:
            try:
                encoded[key] = json.dumps(
                    SecretStore(settings.data_dir).encrypt(encoded[key].encode("utf-8"))
                )
            except SecretStoreError as exc:
                raise Problem(
                    503, "SECRET_STORE_UNAVAILABLE", "Secret storage unavailable"
                ) from exc
        db.execute(
            "INSERT INTO settings(scope,key,value_json,admin_only,updated_by,updated_at)"
            " VALUES ('system',?,?,1,?,?) ON CONFLICT DO UPDATE SET value_json=excluded.value_json,"
            "admin_only=1,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (key, encoded[key], user_id, now()),
        )
        old = (
            {"value": "[encrypted]" if encrypted_audit else before[key]} if key in before else None
        )
        new = {"value": "[encrypted]" if encrypted_audit else redact_sensitive(value, key)}
        audit_write(db, user_id, "SETTING_CHANGE", "settings", key, old, new)
    return settings_get(db, user_id, settings)


def audit_timestamp(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(UTC).isoformat(timespec="microseconds")


def audit_query(
    db: sqlite3.Connection, user_id: int, filters: dict[str, Any], limit: int, offset: int
) -> dict[str, Any]:
    """REQ-AUTH-05: filtered audit history with complete-chain verification."""
    clauses, params = [], []
    for key in ("user_id", "entity_type", "entity_id"):
        if filters.get(key) is not None:
            clauses.append(f"{key}=?")
            params.append(filters[key])
    if filters.get("action"):
        clauses.append("action IN (" + ",".join("?" for _ in filters["action"]) + ")")
        params.extend(filters["action"])
    for key, operator in (("from", ">="), ("to", "<=")):
        if filters.get(key):
            db.create_function("audit_timestamp", 1, audit_timestamp, deterministic=True)
            clauses.append(f"audit_timestamp(ts){operator}?")
            params.append(audit_timestamp(filters[key]))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    total = db.execute("SELECT count(*) FROM audit_log" + where, params).fetchone()[0]
    rows = [
        dict(r)
        for r in db.execute(
            "SELECT * FROM audit_log" + where + " ORDER BY audit_id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
    ]
    return {"items": rows, "total": total, "chain_valid": verify_chain(db)}


def export_audit(
    db: sqlite3.Connection, user_id: int, filters: dict[str, Any], limit: int, offset: int
) -> str:
    data = audit_query(db, user_id, filters, limit, offset)
    columns = [
        "audit_id",
        "ts",
        "user_id",
        "action",
        "entity_type",
        "entity_id",
        "details_json",
        "row_hash",
    ]
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(columns)
    for row in data["items"]:
        cells = []
        for key in columns:
            value = str(row[key]) if row[key] is not None else ""
            cells.append("'" + value if value.startswith(("=", "+", "-", "@", "\t")) else value)
        writer.writerow(cells)
    audit_write(
        db, user_id, "AUDIT_EXPORT", "audit_log", "export", None, {"count": len(data["items"])}
    )
    return stream.getvalue()
