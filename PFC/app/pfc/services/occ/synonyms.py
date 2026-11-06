import logging
import sqlite3
from typing import Any

from pfc.domain.normalise import SynonymRow, SynonymTable, normalise_term
from pfc.security.problems import Problem
from pfc.services.occ.common import audit_write, get_row, now
from pfc.services.occ.seed import normalize_term

logger = logging.getLogger(__name__)


def valid_synonym_table(db: sqlite3.Connection, exclude: int | None = None) -> SynonymTable:
    rows = [
        dict(row)
        for row in db.execute(
            "SELECT y.*,s.name AS canonical FROM skill_synonyms y JOIN skills s USING(skill_id) "
            "WHERE (? IS NULL OR y.synonym_id!=?) ORDER BY y.synonym_id",
            (exclude, exclude),
        )
    ]
    try:
        return SynonymTable(rows)
    except ValueError:
        canonical_names = {
            normalise_term(row["canonical"]): row["canonical"]
            for row in rows
            if row["status"] == "approved" and normalise_term(row["canonical"])
        }
        exact = {key: key for key in canonical_names}
        accepted = []
        for row in rows:
            try:
                parsed = SynonymTable([row]).rows[0]
                if parsed.status == "approved" and parsed.relation != "related":
                    term = normalise_term(parsed.term)
                    canonical = normalise_term(parsed.canonical)
                    if term in exact and exact[term] != canonical:
                        raise ValueError("Ambiguous equivalent synonym")
                    exact[term] = canonical
                accepted.append(parsed)
            except ValueError:
                logger.warning("Skipping invalid stored synonym row %s", row["synonym_id"])
        represented = {
            normalise_term(row.canonical) for row in accepted if row.status == "approved"
        }
        accepted.extend(
            SynonymRow(name, name)
            for key, name in sorted(canonical_names.items())
            if key not in represented
        )
        return SynonymTable(accepted)


def _validate(db: sqlite3.Connection, data: dict[str, Any], target: int | None) -> None:
    try:
        candidate = SynonymTable([data]).rows[0]
        SynonymTable([*valid_synonym_table(db, exclude=target).rows, candidate])
    except ValueError as exc:
        raise Problem(422, "VALIDATION_ERROR", str(exc)) from exc


def list_synonyms(
    db: sqlite3.Connection,
    user_id: int,
    status: str | None = None,
    skill: str | None = None,
    q: str | None = None,
) -> list[dict[str, Any]]:
    """REQ-MAT-05: canonical skill names accompany filterable synonym rows."""
    sql = (
        "SELECT s.*,k.name AS canonical FROM skill_synonyms s JOIN skills k USING(skill_id) WHERE 1"
    )
    params = []
    if status is not None:
        sql += " AND s.status=?"
        params.append(status)
    if skill is not None:
        sql += " AND k.name=? COLLATE NOCASE"
        params.append(skill)
    if q is not None:
        sql += " AND (s.term LIKE ? OR k.name LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])
    return [dict(r) for r in db.execute(sql + " ORDER BY s.synonym_id", params)]


def skill_id(
    db: sqlite3.Connection, user_id: int, canonical: str, existing_only: bool = False
) -> int:
    canonical = canonical.strip()
    if not canonical:
        raise Problem(422, "VALIDATION_ERROR", "Canonical skill must not be empty")
    row = db.execute("SELECT skill_id FROM skills WHERE name=?", (canonical,)).fetchone()
    if row:
        return int(row[0])
    if existing_only:
        raise Problem(404, "NOT_FOUND", "Canonical skill not found")
    cursor = db.execute("INSERT INTO skills(name,created_by) VALUES (?,'admin')", (canonical,))
    target = int(cursor.lastrowid or 0)
    audit_write(
        db, user_id, "OCC_EDIT", "skills", target, None, get_row(db, "skills", "skill_id", target)
    )
    return target


def save(
    db: sqlite3.Connection, user_id: int, data: dict[str, Any], target: int | None = None
) -> dict[str, Any]:
    before = get_row(db, "skill_synonyms", "synonym_id", target) if target is not None else None
    _validate(db, data, target)
    norm = normalize_term(data["term"])
    if not norm:
        raise Problem(422, "VALIDATION_ERROR", "Term must not be empty")
    canonical_id = skill_id(db, user_id, data["canonical"])
    if target is None:
        cursor = db.execute(
            "INSERT INTO skill_synonyms(skill_id,term,term_norm,relation,credit,status,created_by,"
            "updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                canonical_id,
                data["term"],
                norm,
                data["relation"],
                data["credit"],
                data["status"],
                user_id,
                now(),
            ),
        )
        target = int(cursor.lastrowid or 0)
    else:
        db.execute(
            "UPDATE skill_synonyms SET skill_id=?,term=?,term_norm=?,relation=?,credit=?,"
            "status=?,updated_at=? WHERE synonym_id=?",
            (
                canonical_id,
                data["term"],
                norm,
                data["relation"],
                data["credit"],
                data["status"],
                now(),
                target,
            ),
        )
    after = get_row(db, "skill_synonyms", "synonym_id", target)
    audit_write(db, user_id, "OCC_EDIT", "skill_synonyms", target, before, after)
    return {**after, "canonical": data["canonical"].strip()}


def transition(
    db: sqlite3.Connection, user_id: int, target: int, status: str, canonical: str | None = None
) -> dict[str, Any]:
    before = get_row(db, "skill_synonyms", "synonym_id", target)
    if before["status"] != "suggested":
        raise Problem(409, "INVALID_STATE", "Only suggested terms can be approved or rejected")
    mapped = (
        skill_id(db, user_id, canonical, existing_only=True) if canonical else before["skill_id"]
    )
    name = db.execute("SELECT name FROM skills WHERE skill_id=?", (mapped,)).fetchone()[0]
    _validate(db, {**before, "canonical": name, "status": status}, target)
    db.execute(
        "UPDATE skill_synonyms SET status=?,skill_id=?,updated_at=? WHERE synonym_id=?",
        (status, mapped, now(), target),
    )
    after = get_row(db, "skill_synonyms", "synonym_id", target)
    audit_write(db, user_id, "OCC_EDIT", "skill_synonyms", target, before, after)
    return after


def delete(db: sqlite3.Connection, user_id: int, target: int) -> None:
    before = get_row(db, "skill_synonyms", "synonym_id", target)
    db.execute("DELETE FROM skill_synonyms WHERE synonym_id=?", (target,))
    audit_write(db, user_id, "OCC_EDIT", "skill_synonyms", target, before, None)
