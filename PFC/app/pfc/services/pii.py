"""REQ-OUT-03, REQ-AUTH-05: single-record unmasking without PII in audit details."""

import json
import sqlite3

from pfc.db.audit import append_audit
from pfc.security.problems import Problem
from pfc.services.occ.common import now

ANALYST_UNMASK_KEY = "privacy.analysts_may_unmask"


def mask_email(value: str | None) -> str | None:
    """REQ-OUT-03: keep 2 local-part characters and mask the rest; no `@` means all local part."""
    if not value:
        return value
    local, separator, domain = value.partition("@")
    shown = local[:2] if len(local) > 2 else ""
    return shown + "*" * (len(local) - len(shown)) + separator + domain


QUERIES = {
    ("resource", "email"): "SELECT email_address FROM resources WHERE resource_id=?",
    ("resource", "phone"): (
        "SELECT c.phone FROM resources r JOIN candidates c ON c.candidate_id=r.candidate_id "
        "WHERE r.resource_id=?"
    ),
    ("candidate", "email"): (
        "SELECT email FROM candidate_emails WHERE candidate_id=? AND trim(email)!='' "
        "ORDER BY confirmed DESC,email_key LIMIT 1"
    ),
    ("candidate", "phone"): "SELECT phone FROM candidates WHERE candidate_id=?",
    ("occ_user", "email"): "SELECT email FROM users WHERE user_id=?",
}


def require_unmask_permission(db: sqlite3.Connection, role: str) -> None:
    if role == "Admin":
        return
    policy = db.execute(
        "SELECT value_json FROM settings WHERE scope='system' AND user_id IS NULL AND key=?",
        (ANALYST_UNMASK_KEY,),
    ).fetchone()
    if policy is not None and json.loads(policy["value_json"]) is not True:
        raise Problem(403, "UNMASK_DISABLED", "Unmasking is disabled for Analysts")


def unmask(
    db: sqlite3.Connection, actor_id: int, role: str, entity: str, target: int, field: str
) -> str:
    """Unsupported fields return 422; denied/missing lookups are not audited.

    Candidate email prefers a confirmed address, then email_key order.
    The system privacy setting defaults to true; only JSON true enables it when present.
    """
    if role != "Admin" and entity == "occ_user":
        raise Problem(403, "FORBIDDEN", "Insufficient permissions")
    require_unmask_permission(db, role)
    query = QUERIES.get((entity, field))
    if query is None:
        raise Problem(422, "FIELD_NOT_SUPPORTED", "Field is not recorded for this entity")
    row = db.execute(query, (target,)).fetchone()
    if row is None or row[0] is None or not row[0].strip():
        raise Problem(404, "NOT_FOUND", "Not recorded")
    append_audit(
        db,
        ts=now(),
        user_id=actor_id,
        action="UNMASK",
        entity_type=entity,
        entity_id=str(target),
        details={"field": field},
    )
    return str(row[0])
