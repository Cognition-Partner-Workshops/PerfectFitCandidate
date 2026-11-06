import json
import sqlite3
from datetime import UTC, datetime
from typing import Any, Protocol

from pfc.api.schemas.llm import LlmHealth
from pfc.security.problems import Problem
from pfc.services.occ.common import audit_write, get_row

# Metadata placeholders (empty hash/file name and zero sizes) are not verified artifacts.
DEFAULT_MODELS: tuple[dict[str, Any], ...] = (
    {
        "name": "Qwen3-4B-Instruct-2507",
        "file_name": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        "file_sha256": "",
        "size_mb": 0,
        "ram_estimate_mb": 0,
        "context_tokens": 4096,
        "licence": "Apache-2.0",
        "approved": 0,
        "is_active": 0,
    },
    {
        "name": "nomic-embed-text-v1.5",
        "file_name": "",
        "file_sha256": "",
        "size_mb": 0,
        "ram_estimate_mb": 0,
        "context_tokens": 8192,
        "licence": "Apache-2.0",
        "approved": 0,
        "is_active": 0,
    },
)


class LlmHealthProvider(Protocol):
    def check(self) -> LlmHealth: ...


class UnconfiguredHealthProvider:
    def check(self) -> LlmHealth:
        return LlmHealth(status="unavailable", detail="local LLM adapter not configured")


def seed_default_models(db: sqlite3.Connection) -> None:
    """REQ-LLM-01/06: documented candidates only, pending explicit Admin approval."""
    if db.execute("SELECT 1 FROM llm_models LIMIT 1").fetchone():
        return
    for model in DEFAULT_MODELS:
        db.execute(
            "INSERT INTO llm_models(name,file_name,file_sha256,size_mb,ram_estimate_mb,"
            "context_tokens,licence,approved,is_active) VALUES (:name,:file_name,:file_sha256,"
            ":size_mb,:ram_estimate_mb,:context_tokens,:licence,:approved,:is_active)",
            model,
        )


def list_models(
    db: sqlite3.Connection, user_id: int, approved_only: bool = False
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM llm_models"
    if approved_only:
        sql += " WHERE approved=1"
    return [dict(row) for row in db.execute(sql + " ORDER BY model_id")]


def approve(db: sqlite3.Connection, user_id: int, target: int, approved: bool) -> dict[str, Any]:
    before = get_row(db, "llm_models", "model_id", target)
    if before["is_active"] and not approved:
        raise Problem(409, "MODEL_ACTIVE", "The active model cannot be unapproved")
    db.execute("UPDATE llm_models SET approved=? WHERE model_id=?", (approved, target))
    after = get_row(db, "llm_models", "model_id", target)
    audit_write(db, user_id, "OCC_EDIT", "llm_models", target, before, after)
    return after


def require_approved(db: sqlite3.Connection, model_id: int) -> dict[str, Any]:
    row = db.execute(
        "SELECT * FROM llm_models WHERE model_id=? AND approved=1", (model_id,)
    ).fetchone()
    if row is None:
        raise Problem(422, "MODEL_NOT_APPROVED", "Model is not approved")
    return dict(row)


def active(db: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT value_json FROM settings WHERE scope='user' AND user_id=? "
        "AND key='llm.active_model'",
        (user_id,),
    ).fetchone()
    if row is not None:
        model = db.execute(
            "SELECT * FROM llm_models WHERE model_id=? AND approved=1", (json.loads(row[0]),)
        ).fetchone()
        if model:
            return dict(model)
    fallback = db.execute("SELECT * FROM llm_models WHERE is_active=1 AND approved=1").fetchone()
    return dict(fallback) if fallback else None


def select(db: sqlite3.Connection, user_id: int, model_id: int | None) -> dict[str, Any] | None:
    if model_id is not None:
        require_approved(db, model_id)
    old = db.execute(
        "SELECT value_json FROM settings WHERE scope='user' AND user_id=? "
        "AND key='llm.active_model'",
        (user_id,),
    ).fetchone()
    before = {"value": json.loads(old[0])} if old else None
    if model_id is None:
        db.execute(
            "DELETE FROM settings WHERE scope='user' AND user_id=? AND key='llm.active_model'",
            (user_id,),
        )
    else:
        db.execute(
            "INSERT INTO settings(scope,user_id,key,value_json,admin_only,updated_by,updated_at)"
            " VALUES ('user',?,'llm.active_model',?,0,?,?) ON CONFLICT DO UPDATE SET "
            "value_json=excluded.value_json,updated_by=excluded.updated_by,"
            "updated_at=excluded.updated_at",
            (
                user_id,
                json.dumps(model_id),
                user_id,
                datetime.now(UTC).isoformat(timespec="microseconds"),
            ),
        )
    after = {"value": model_id} if model_id is not None else None
    if before != after:
        audit_write(
            db,
            user_id,
            "SETTING_CHANGE",
            "settings",
            f"user:{user_id}:llm.active_model",
            before,
            after,
        )
    return active(db, user_id)
