import json
import re
import sqlite3
from typing import Any

from pfc.security.problems import Problem
from pfc.services.occ.common import audit_write, get_row, now

WEIGHTS = {"skills", "grade", "location", "bench_start", "bench_end", "status"}
PLACEHOLDERS = {
    "candidate_name",
    "requisition",
    "role",
    "match_pct",
    "analysis",
    "gaps",
    "pointers",
    "spoc_1",
    "spoc_2",
}


def decode(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["weights"] = json.loads(result.pop("weights_json"))
    result["rules"] = json.loads(result.pop("rules_json"))
    return result


def scoring_list(db: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    """REQ-SCR-01/02: immutable published scoring versions."""
    return [
        decode(dict(r))
        for r in db.execute("SELECT * FROM scoring_templates ORDER BY name,version DESC")
    ]


def scoring_get(db: sqlite3.Connection, user_id: int, target: int) -> dict[str, Any]:
    return decode(get_row(db, "scoring_templates", "template_id", target))


def draft(db: sqlite3.Connection, target: int) -> dict[str, Any]:
    row = get_row(db, "scoring_templates", "template_id", target)
    if row["status"] != "draft":
        raise Problem(409, "TEMPLATE_IMMUTABLE", "Only drafts may be changed")
    return row


def create_scoring(db: sqlite3.Connection, user_id: int, data: dict[str, Any]) -> dict[str, Any]:
    source_id = data.get("clone_from_id")
    if source_id is None:
        row = db.execute(
            "SELECT * FROM scoring_templates WHERE status='active' ORDER BY template_id LIMIT 1"
        ).fetchone()
        source = dict(row) if row else None
        if source is None:
            raise Problem(404, "NOT_FOUND", "Active scoring template not found")
    else:
        source = get_row(db, "scoring_templates", "template_id", source_id)
    name = data.get("name") or source["name"]
    version = db.execute(
        "SELECT coalesce(max(version),0)+1 FROM scoring_templates WHERE name=?", (name,)
    ).fetchone()[0]
    weights = (
        data.get("weights")
        if data.get("weights") is not None
        else json.loads(source["weights_json"])
    )
    rules: dict[str, Any] = (
        data["rules"] if data.get("rules") is not None else json.loads(source["rules_json"])
    )
    rules = {**rules, "_meta": {"change_note": data["change_note"]}}
    cursor = db.execute(
        "INSERT INTO scoring_templates(name,version,role_family,weights_json,rules_json,status,"
        "created_by,created_at) VALUES (?,?,?,?,?,'draft',?,?)",
        (
            name,
            version,
            source["role_family"],
            json.dumps(weights),
            json.dumps(rules),
            user_id,
            now(),
        ),
    )
    target = int(cursor.lastrowid or 0)
    after = scoring_get(db, user_id, target)
    audit_write(db, user_id, "OCC_EDIT", "scoring_templates", target, None, after)
    return after


def update_scoring(
    db: sqlite3.Connection, user_id: int, target: int, data: dict[str, Any]
) -> dict[str, Any]:
    before = decode(draft(db, target))
    weights = data.get("weights") if data.get("weights") is not None else before["weights"]
    rules: dict[str, Any] = data["rules"] if data.get("rules") is not None else before["rules"]
    rules = {**rules, "_meta": {"change_note": data["change_note"]}}
    db.execute(
        "UPDATE scoring_templates SET weights_json=?,rules_json=? WHERE template_id=?",
        (json.dumps(weights), json.dumps(rules), target),
    )
    after = scoring_get(db, user_id, target)
    audit_write(db, user_id, "OCC_EDIT", "scoring_templates", target, before, after)
    return after


def delete_scoring(db: sqlite3.Connection, user_id: int, target: int) -> None:
    before = decode(draft(db, target))
    db.execute("DELETE FROM scoring_templates WHERE template_id=?", (target,))
    audit_write(db, user_id, "OCC_EDIT", "scoring_templates", target, before, None)


def activate(db: sqlite3.Connection, user_id: int, target: int) -> dict[str, Any]:
    """REQ-SCR-01/02, REQ-ELIG-06, REQ-GRD-01: only six scoring components."""
    before = decode(draft(db, target))
    weights = before["weights"]
    if (
        set(weights) != WEIGHTS
        or any(type(v) is not int or v < 0 for v in weights.values())
        or sum(weights.values()) != 100
    ):
        raise Problem(
            422, "VALIDATION_ERROR", "Weights must be six non-negative integers totaling 100"
        )
    active = db.execute(
        "SELECT * FROM scoring_templates WHERE name=? AND "
        "coalesce(role_family,'')=coalesce(?,'') AND status='active'",
        (before["name"], before["role_family"]),
    ).fetchall()
    for row in active:
        old = decode(dict(row))
        db.execute(
            "UPDATE scoring_templates SET status='retired' WHERE template_id=?",
            (row["template_id"],),
        )
        audit_write(
            db,
            user_id,
            "OCC_EDIT",
            "scoring_templates",
            row["template_id"],
            old,
            scoring_get(db, user_id, row["template_id"]),
        )
    db.execute(
        "UPDATE scoring_templates SET status='active',activated_at=? WHERE template_id=?",
        (now(), target),
    )
    after = scoring_get(db, user_id, target)
    audit_write(db, user_id, "OCC_EDIT", "scoring_templates", target, before, after)
    return after


def validate_placeholders(subject: str, body: str) -> None:
    tokens = re.findall(r"(?<!\{)\{([^{}\s:;]+)\}(?!\})", subject + "\n" + body)
    if set(tokens) - PLACEHOLDERS:
        raise Problem(422, "UNKNOWN_PLACEHOLDER", "Unknown email placeholder")


def email_list(db: sqlite3.Connection, user_id: int, all_versions: bool) -> list[dict[str, Any]]:
    clause = (
        ""
        if all_versions
        else (
            " WHERE version=(SELECT max(version) FROM email_templates e "
            "WHERE e.name=email_templates.name)"
        )
    )
    return [
        dict(r)
        for r in db.execute(
            "SELECT * FROM email_templates" + clause + " ORDER BY name,version DESC"
        )
    ]


def create_email(db: sqlite3.Connection, user_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """REQ-EML-03: templates are append-only versions with allowlisted placeholders."""
    validate_placeholders(data["subject"], data["body_html"])
    version = db.execute(
        "SELECT coalesce(max(version),0)+1 FROM email_templates WHERE name=?", (data["name"],)
    ).fetchone()[0]
    cursor = db.execute(
        "INSERT INTO email_templates(name,version,subject,body_html,created_by,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (data["name"], version, data["subject"], data["body_html"], user_id, now()),
    )
    target = int(cursor.lastrowid or 0)
    after = get_row(db, "email_templates", "email_template_id", target)
    audit_write(db, user_id, "OCC_EDIT", "email_templates", target, None, after)
    return after


def default_email(db: sqlite3.Connection, user_id: int, target: int) -> dict[str, Any]:
    before = get_row(db, "email_templates", "email_template_id", target)
    for row in db.execute("SELECT * FROM email_templates WHERE is_default=1").fetchall():
        if row["email_template_id"] != target:
            old = dict(row)
            db.execute(
                "UPDATE email_templates SET is_default=0 WHERE email_template_id=?",
                (row["email_template_id"],),
            )
            audit_write(
                db,
                user_id,
                "OCC_EDIT",
                "email_templates",
                row["email_template_id"],
                old,
                {**old, "is_default": 0},
            )
    db.execute("UPDATE email_templates SET is_default=1 WHERE email_template_id=?", (target,))
    after = get_row(db, "email_templates", "email_template_id", target)
    audit_write(db, user_id, "OCC_EDIT", "email_templates", target, before, after)
    return after
