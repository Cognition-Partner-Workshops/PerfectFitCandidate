"""REQ-CLS-02: transactional candidate classification and audited overrides."""

import re
import sqlite3
from datetime import UTC, datetime
from typing import cast

from pfc.api.schemas.candidates import (
    CandidateClassification,
    Classification,
    ClassificationOverride,
)
from pfc.db.audit import append_audit
from pfc.security.problems import Problem
from pfc.security.sessions import stamp


def normalize_domain(domain: str) -> str:
    domain = domain.strip().lstrip("@").strip().lower()
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if len(domain) > 253 or not re.fullmatch(rf"{label}(?:\.{label})+", domain):
        raise Problem(422, "VALIDATION_ERROR", "Invalid domain")
    return domain


def domain_set(db: sqlite3.Connection) -> set[str]:
    return {
        normalize_domain(row[0]) for row in db.execute("SELECT domain FROM internal_email_domains")
    }


def _candidate(db: sqlite3.Connection, candidate_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
    if row is None:
        raise Problem(404, "CANDIDATE_NOT_FOUND", "Candidate not found")
    return row


def _computed(db: sqlite3.Connection, candidate_id: int, domains: set[str]) -> Classification:
    emails = [
        row
        for row in db.execute(
            "SELECT email,confirmed,source FROM candidate_emails WHERE candidate_id=?",
            (candidate_id,),
        )
        if row["email"].strip()
    ]
    for row in emails:
        email = row["email"].strip().lower()
        if (
            (row["confirmed"] or row["source"] == "CT")
            and email.count("@") == 1
            and email.rsplit("@", 1)[1] in domains
        ):
            return "Internal"
    return "External" if emails else "Unknown"


def reclassify_candidate(db: sqlite3.Connection, candidate_id: int) -> None:
    row = _candidate(db, candidate_id)
    if row["classification_override"] is not None:
        return
    value = _computed(db, candidate_id, domain_set(db))
    if row["classification"] != value:
        db.execute(
            "UPDATE candidates SET classification=? WHERE candidate_id=?", (value, candidate_id)
        )


def reclassify_all(db: sqlite3.Connection, *, user_id: int | None = None) -> int:
    domains = domain_set(db)
    counts = dict.fromkeys(("Internal", "External", "Unknown"), 0)
    rows = db.execute(
        "SELECT candidate_id,classification FROM candidates WHERE classification_override IS NULL "
        "ORDER BY candidate_id"
    ).fetchall()
    for row in rows:
        value = _computed(db, row["candidate_id"], domains)
        if row["classification"] != value:
            db.execute(
                "UPDATE candidates SET classification=? WHERE candidate_id=?",
                (value, row["candidate_id"]),
            )
            counts[value] += 1
    changed = sum(counts.values())
    if changed:
        append_audit(
            db,
            ts=stamp(datetime.now(UTC)),
            user_id=user_id,
            action="CLASSIFICATION_REEVALUATED",
            entity_type="candidates",
            details={"changed": changed, "classifications": counts},
        )
    return changed


def _effective(row: sqlite3.Row) -> Classification:
    return cast(
        Classification, row["classification_override"] or row["classification"] or "Unknown"
    )


def set_override(
    db: sqlite3.Connection,
    candidate_id: int,
    body: ClassificationOverride,
    user_id: int,
    now: str,
) -> CandidateClassification:
    before = _candidate(db, candidate_id)
    if body.classification is None:
        db.execute(
            "UPDATE candidates SET classification_override=NULL,override_by=NULL,"
            "override_reason=NULL,override_at=NULL WHERE candidate_id=?",
            (candidate_id,),
        )
        reclassify_candidate(db, candidate_id)
    else:
        reclassify_candidate(db, candidate_id)
        db.execute(
            "UPDATE candidates SET classification_override=?,override_by=?,override_reason=?,"
            "override_at=? WHERE candidate_id=?",
            (body.classification, user_id, body.reason, now, candidate_id),
        )
    after = _candidate(db, candidate_id)
    append_audit(
        db,
        ts=now,
        user_id=user_id,
        action="CLASSIFICATION_OVERRIDE",
        entity_type="candidates",
        entity_id=str(candidate_id),
        details={
            "before": {
                "effective": _effective(before),
                "override": before["classification_override"],
            },
            "after": {"effective": _effective(after), "override": after["classification_override"]},
            "reason": re.sub(r"[\w.+-]+@[\w.-]+", "[REDACTED]", body.reason),
        },
    )
    return CandidateClassification(
        candidate_id=candidate_id,
        effective_classification=_effective(after),
        computed_classification=after["classification"] or "Unknown",
        classification_override=after["classification_override"],
        override_reason=after["override_reason"],
        override_by=after["override_by"],
        override_at=after["override_at"],
    )
