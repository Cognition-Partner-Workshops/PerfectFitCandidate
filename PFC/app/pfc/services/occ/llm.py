import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pfc.api.schemas.llm import LlmHealth
from pfc.config import Settings
from pfc.db.connection import database
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
        "thinking": 0,
        "kv_bytes_per_token": 147456,
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
        "thinking": 0,
        "kv_bytes_per_token": None,
        "licence": "Apache-2.0",
        "approved": 0,
        "is_active": 0,
    },
)


THINKING_MODEL_SIZE = 2_497_280_736
THINKING_MODEL_RAM_MB = 4800
THINKING_MODEL = {
    "name": "Qwen3-4B-Thinking-2507",
    "file_name": "Qwen3-4B-Thinking-2507-Q4_K_M.gguf",
    "file_sha256": "64ce5838a76b4d63071289c17ed04525265a47abf51546cd747a30e41b7687fa",
    "size_mb": 2382,
    "ram_estimate_mb": THINKING_MODEL_RAM_MB,
    "context_tokens": 4096,
    "licence": "Apache-2.0",
    "thinking": 1,
    "kv_bytes_per_token": 147456,
    "approved": 0,
    "is_active": 0,
}


def register_optional_thinking_model(settings: Settings) -> None:
    with database(settings) as db:
        if db.execute(
            "SELECT 1 FROM llm_models WHERE name=?", (THINKING_MODEL["name"],)
        ).fetchone():
            return
        path = settings.models_dir / str(THINKING_MODEL["file_name"])
        if not path.is_file():
            return
        if path.stat().st_size != THINKING_MODEL_SIZE:
            logging.getLogger(__name__).warning("Optional thinking model size mismatch")
            return
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != THINKING_MODEL["file_sha256"]:
            logging.getLogger(__name__).warning("Optional thinking model hash mismatch")
            return
        columns = ",".join(THINKING_MODEL)
        placeholders = ",".join("?" for _ in THINKING_MODEL)
        db.execute(
            f"INSERT INTO llm_models({columns}) SELECT {placeholders} "
            "WHERE NOT EXISTS (SELECT 1 FROM llm_models WHERE name=?)",
            (*THINKING_MODEL.values(), THINKING_MODEL["name"]),
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
    allowed = {row[1] for row in db.execute("PRAGMA table_info(llm_models)")}
    for model in DEFAULT_MODELS:
        columns = [name for name in model if name in allowed]
        db.execute(
            f"INSERT INTO llm_models({','.join(columns)}) "
            f"VALUES ({','.join(':' + name for name in columns)})",
            model,
        )


def list_models(
    db: sqlite3.Connection, user_id: int, approved_only: bool = False
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM llm_models"
    if approved_only:
        sql += " WHERE approved=1"
    return [
        {
            **dict(row),
            "thinking": bool(row["thinking"]),
            "kvBytesPerToken": row["kv_bytes_per_token"],
        }
        for row in db.execute(sql + " ORDER BY model_id")
    ]


def model_purpose(model: dict[str, Any]) -> str:
    if "embed" in model["name"].casefold() or Path(model["file_name"]).suffix.lower() == ".onnx":
        return "embedding"
    return "generation"


def occ_models(db: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    return [
        {
            **model,
            "id": str(model["model_id"]),
            "purpose": model_purpose(model),
            "ramGb": model["ram_estimate_mb"] / 1024,
            "diskGb": model["size_mb"] / 1024,
            "approved": bool(model["approved"]),
            "thinking": bool(model["thinking"]),
            "kvBytesPerToken": model["kv_bytes_per_token"],
            "isDefault": bool(model["is_active"]),
        }
        for model in list_models(db, user_id)
    ]


def approve(
    db: sqlite3.Connection,
    user_id: int,
    target: int,
    approved: bool | None,
    *,
    is_default: bool = False,
) -> dict[str, Any]:
    before = get_row(db, "llm_models", "model_id", target)
    if is_default:
        if not (before["approved"] if approved is None else approved):
            raise Problem(422, "MODEL_NOT_APPROVED", "Model is not approved")
        filename = Path(before["file_name"])
        if (
            filename.suffix.lower() != ".gguf"
            or filename.is_absolute()
            or ".." in filename.parts
            or model_purpose(before) != "generation"
        ):
            raise Problem(422, "MODEL_NOT_GENERATIVE", "Default model must be a generation GGUF")
    if before["is_active"] and approved is False:
        raise Problem(409, "MODEL_ACTIVE", "The active model cannot be unapproved")
    if approved is not None:
        db.execute("UPDATE llm_models SET approved=? WHERE model_id=?", (approved, target))
    if is_default:
        previous = db.execute("SELECT model_id FROM llm_models WHERE is_active=1").fetchone()
        db.execute("UPDATE llm_models SET is_active=0 WHERE is_active=1")
        db.execute("UPDATE llm_models SET is_active=1 WHERE model_id=?", (target,))
        audit_write(
            db,
            user_id,
            "OCC_EDIT",
            "llm_models",
            "default",
            {"default_model_id": previous[0] if previous else None},
            {"default_model_id": target},
        )
    after = get_row(db, "llm_models", "model_id", target)
    if approved is not None:
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
