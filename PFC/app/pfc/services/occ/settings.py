import json
import sqlite3
from typing import Any

from pfc.api.schemas.settings import PersonalSettings
from pfc.security.problems import Problem
from pfc.services.occ import llm
from pfc.services.occ.common import audit_write, now

KEYS = {
    "resume_root_path": "resume.root_path",
    "watch": "watch.sources",
    "density": "display.density",
    "page_size": "display.page_size",
    "default_landing": "display.default_landing",
}


def get_personal(db: sqlite3.Connection, user_id: int) -> PersonalSettings:
    """REQ-ING-04/05, REQ-IN-20: user paths and display preferences."""
    stored = {
        row["key"]: json.loads(row["value_json"])
        for row in db.execute(
            "SELECT key,value_json FROM settings WHERE scope='user' AND user_id=?", (user_id,)
        )
    }
    values = {field: stored[key] for field, key in KEYS.items() if key in stored}
    model = llm.active(db, user_id)
    values["active_model_id"] = model["model_id"] if model else None
    return PersonalSettings.model_validate(values)


def put_personal(db: sqlite3.Connection, user_id: int, changes: dict[str, Any]) -> PersonalSettings:
    before = get_personal(db, user_id).model_dump()
    merged = {**before, **changes}
    if "watch" in changes:
        merged["watch"] = {
            kind: {**before["watch"][kind], **changes["watch"].get(kind, {})}
            for kind in ("dl", "ct")
        }
    after = PersonalSettings.model_validate(merged)
    if after.watch.dl.enabled or after.watch.ct.enabled:
        raise Problem(422, "PHASE_2_FEATURE", "Watch folders are available in Phase 2")
    if "active_model_id" in changes:
        llm.select(db, user_id, after.active_model_id)
    data = after.model_dump()
    for field, key in KEYS.items():
        if field not in changes:
            continue
        row = db.execute(
            "SELECT value_json FROM settings WHERE scope='user' AND user_id=? AND key=?",
            (user_id, key),
        ).fetchone()
        old = {"value": json.loads(row[0])} if row else None
        new = {"value": data[field]}
        if old == new:
            continue
        db.execute(
            "INSERT INTO settings(scope,user_id,key,value_json,admin_only,updated_by,updated_at)"
            " VALUES ('user',?,?,?,0,?,?) ON CONFLICT DO UPDATE SET value_json=excluded.value_json,"
            "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (user_id, key, json.dumps(data[field]), user_id, now()),
        )
        audit_write(db, user_id, "SETTING_CHANGE", "settings", f"user:{user_id}:{key}", old, new)
    return get_personal(db, user_id)
