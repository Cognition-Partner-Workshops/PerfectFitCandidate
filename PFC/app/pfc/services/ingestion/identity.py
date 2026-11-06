"""REQ-IN-23..25 / REQ-OUT-03: explicit human identity decisions, never inferred email."""

import json
import re
import sqlite3
from pathlib import PurePosixPath
from typing import Any

from pfc.api.schemas.candidates import (
    CandidateActivity,
    CandidateCV,
    CandidateDetail,
    CandidateResource,
    CTEmailOption,
    IdentityResolution,
)
from pfc.db.audit import append_audit
from pfc.security.problems import Problem
from pfc.services.candidates.classification import reclassify_candidate

from .browse import _mask
from .cv_facts import public_facts
from .masking import mask_phone


def _candidate(db: sqlite3.Connection, candidate_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
    if row is None:
        raise Problem(404, "CANDIDATE_NOT_FOUND", "Candidate not found")
    return row


def _name(value: str | None) -> str:
    return "".join(re.findall(r"\w+", (value or "").casefold()))


def _name_keys(value: str | None) -> set[str]:
    tokens = re.findall(r"\w+", (value or "").casefold())
    keys = {_name(value)} - {""}
    if len(tokens) > 1:
        keys.add(tokens[0][0] + tokens[-1])
    return keys


def _masked_text(value: str) -> str:
    return re.sub(r"[\w.+-]+@[\w.-]+", lambda m: _mask(m[0]) or "***", value)


def _options(db: sqlite3.Connection, candidate: sqlite3.Row) -> list[sqlite3.Row]:
    names = _name_keys(candidate["display_name"])
    names.update(
        _name(r[0])
        for r in db.execute(
            "SELECT short_name FROM resources WHERE candidate_id=?", (candidate["candidate_id"],)
        )
    )
    names.discard("")
    return [
        r
        for r in db.execute(
            "SELECT * FROM resources WHERE email_key<>'' AND lifecycle='active' "
            "ORDER BY resource_id"
        )
        if r["candidate_id"] is None or bool(_name_keys(r["short_name"]) & names)
    ]


def _resource(db: sqlite3.Connection, candidate: sqlite3.Row, cv_count: int) -> CandidateResource:
    row = db.execute(
        "SELECT r.*,s.priority FROM resources r LEFT JOIN bench_statuses s "
        "ON s.code=r.bench_status_code WHERE r.candidate_id=? "
        "ORDER BY (r.lifecycle='active') DESC,(r.email_key<>'') DESC,r.resource_id LIMIT 1",
        (candidate["candidate_id"],),
    ).fetchone()
    values = dict(row) if row else {}
    capacity = values.get("available_capacity")
    return CandidateResource(
        resource_id=values.get("resource_id", 0),
        candidate_id=candidate["candidate_id"],
        short_name=values.get("short_name")
        or candidate["display_name"]
        or candidate["internal_ref"],
        email_address_masked=_mask(values.get("email_address")),
        name=candidate["display_name"],
        grade=values.get("grade"),
        location=values.get("location"),
        bench_status=values.get("bench_status"),
        bench_status_priority=values.get("priority"),
        unmapped_status=bool(values.get("bench_status"))
        and values.get("bench_status_code") is None,
        skills=[
            s.strip() for s in re.split(r"[,;\n]", values.get("skills_raw") or "") if s.strip()
        ],
        available_capacity=capacity if capacity in {"Y", "N"} else None,
        bench_start_date=values.get("bench_start_date"),
        bench_end_date=values.get("bench_end_date"),
        group_name=values.get("group_name"),
        group_owner=values.get("group_owner"),
        eligibility=values.get("eligibility", "insufficient"),
        exclusion_reasons=(values.get("exclusion_reason") or "").split(",")
        if values.get("exclusion_reason")
        else [],
        cv_status="Synced" if cv_count else "Not synced",
        cv_count=cv_count,
        candidate_type=candidate["classification_override"]
        or candidate["classification"]
        or "Unknown",
        identity_status=candidate["identity_status"],
    )


def _matches(db: sqlite3.Connection, candidate: sqlite3.Row) -> list[dict[str, Any]]:
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='match_results'"
    ).fetchone():
        return []
    rows = db.execute(
        "SELECT m.*,d.code_requisition,d.position_start_date,d.account_name,d.bu,"
        "d.demand_request_name,d.code_local_grade,d.region,r.short_name,r.email_address,"
        "r.grade,r.location,r.bench_status FROM match_results m "
        "JOIN demands d ON d.demand_id=m.demand_id JOIN resources r ON r.resource_id=m.resource_id "
        "WHERE m.candidate_id=? ORDER BY m.run_id DESC,m.rank,m.result_id",
        (candidate["candidate_id"],),
    ).fetchall()
    result = []
    for r in rows:
        breakdown = [
            {"component": b["component"], "points": b["points"], "maxPoints": b["max_points"]}
            for b in db.execute(
                "SELECT * FROM score_breakdowns WHERE result_id=? ORDER BY component",
                (r["result_id"],),
            )
        ]
        result.append(
            {
                "resultId": r["result_id"],
                "runId": r["run_id"],
                "demandId": r["demand_id"],
                "resourceId": r["resource_id"],
                "candidateId": candidate["candidate_id"],
                "rank": r["rank"],
                "codeRequisition": r["code_requisition"],
                "positionStartDate": r["position_start_date"],
                "accountName": r["account_name"],
                "bu": r["bu"],
                "demandRequestName": r["demand_request_name"],
                "demandGrade": r["code_local_grade"],
                "region": r["region"],
                "personName": candidate["display_name"] or r["short_name"],
                "personEmailMasked": _mask(r["email_address"]),
                "personGrade": r["grade"],
                "personLocation": r["location"],
                "benchStatus": r["bench_status"],
                "score": r["score"],
                "band": r["band"],
                "gradeRelation": r["grade_relation"],
                "locationAssessment": r["location_assessment"],
                "matchingEvidence": [_masked_text(v) for v in json.loads(r["matching_evidence"])],
                "gapsChecks": [_masked_text(v) for v in json.loads(r["gaps_checks"])],
                "eligibilityEvidence": [
                    _masked_text(v) for v in json.loads(r["eligibility_evidence"])
                ],
                "mandatoryMissing": r["mandatory_missing"],
                "candidateType": candidate["classification_override"]
                or candidate["classification"]
                or "Unknown",
                "incompleteIdentity": candidate["identity_status"] != "complete",
                "emailPreselected": bool(r["email_preselected"]),
                "breakdown": breakdown,
            }
        )
    return result


def detail(db: sqlite3.Connection, candidate_id: int) -> CandidateDetail:
    candidate = _candidate(db, candidate_id)
    best = (
        db.execute(
            "SELECT best_cv_version_id FROM match_results WHERE candidate_id=? "
            "ORDER BY score DESC,run_id DESC,result_id DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='match_results'"
        ).fetchone()
        else None
    )
    versions = [
        CandidateCV(
            cv_version_id=r["cv_version_id"],
            file_name=_masked_text(r["file_name"]),
            folder_path=_masked_text(str(PurePosixPath(r["source_path"]).parent)),
            modified_at=r["file_mtime"] or r["extracted_at"],
            extracted_at=r["extracted_at"],
            is_best=(r["cv_version_id"] == best[0]) if best else bool(r["is_latest"]),
            facts=public_facts(r["facts_json"]),
        )
        for r in db.execute(
            "SELECT v.*,r.file_name,r.source_path,r.file_mtime FROM cv_versions v "
            "JOIN resumes r ON r.resume_id=v.resume_id WHERE v.candidate_id=? "
            "ORDER BY v.version_no DESC,v.cv_version_id DESC",
            (candidate_id,),
        )
    ]
    phone = candidate["phone"]
    return CandidateDetail(
        candidate_id=candidate_id,
        internal_ref=candidate["internal_ref"],
        resource=_resource(db, candidate, len(versions)),
        phone_masked=mask_phone(phone),
        emails=[
            masked
            for r in db.execute(
                "SELECT email FROM candidate_emails WHERE candidate_id=? ORDER BY email_key",
                (candidate_id,),
            )
            if (masked := _mask(r[0])) is not None
        ],
        identity_status=candidate["identity_status"],
        ct_email_options=[
            CTEmailOption(
                resource_id=r["resource_id"],
                label=r["short_name"],
                email_masked=_mask(r["email_address"]) or "***",
            )
            for r in _options(db, candidate)
        ],
        cv_versions=versions,
        matches=_matches(db, candidate),
        activity=[
            CandidateActivity(at=r["ts"], summary=r["action"])
            for r in db.execute(
                "SELECT ts,action FROM audit_log WHERE entity_type='candidate' AND entity_id=? "
                "ORDER BY audit_id DESC",
                (str(candidate_id),),
            )
        ],
    )


def _requires_merge(
    db: sqlite3.Connection, candidate_id: int, resource_id: int, email_key: str
) -> bool:
    if db.execute(
        "SELECT 1 FROM candidate_emails WHERE candidate_id=? "
        "AND NOT (email_key=? AND source='CT' AND confirmed=0)",
        (candidate_id, email_key),
    ).fetchone():
        return True
    if db.execute(
        "SELECT 1 FROM resources WHERE candidate_id=? AND resource_id<>?",
        (candidate_id, resource_id),
    ).fetchone():
        return True
    for table in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        name = '"' + table[0].replace('"', '""') + '"'
        if table[0] in {"candidates", "candidate_emails", "resources"}:
            continue
        columns = {c[1] for c in db.execute(f"PRAGMA table_info({name})")}
        if (
            "candidate_id" in columns
            and db.execute(
                f"SELECT 1 FROM {name} WHERE candidate_id=? LIMIT 1", (candidate_id,)
            ).fetchone()
        ):
            return True
        if (
            table[0] in {"match_results", "email_messages"}
            and db.execute(
                f"SELECT 1 FROM {name} WHERE resource_id=? LIMIT 1", (resource_id,)
            ).fetchone()
        ):
            return True
    return False


def resolve(
    db: sqlite3.Connection, candidate_id: int, body: IdentityResolution, user_id: int, now: str
) -> CandidateDetail:
    candidate = _candidate(db, candidate_id)
    if body.action == "manual_entry":
        if db.execute(
            "SELECT 1 FROM candidate_emails WHERE candidate_id=? AND source='CT'",
            (candidate_id,),
        ).fetchone():
            raise Problem(409, "IDENTITY_CT_AUTHORITATIVE", "CT email remains authoritative")
        if body.email is not None:
            email = body.email.strip()
            email_key = email.lower()
            if db.execute(
                "SELECT 1 FROM candidate_emails WHERE email_key=? AND candidate_id<>? "
                "AND confirmed=1",
                (email_key, candidate_id),
            ).fetchone():
                raise Problem(409, "IDENTITY_CONFLICT", "Email is confirmed for another candidate")
            db.execute(
                "DELETE FROM candidate_emails WHERE candidate_id=? AND source='manual' "
                "AND email_key<>?",
                (candidate_id, email_key),
            )
            db.execute(
                "INSERT INTO candidate_emails(candidate_id,email,email_key,source,confirmed) "
                "VALUES (?,?,?,'manual',1) ON CONFLICT(candidate_id,email_key) "
                "DO UPDATE SET email=excluded.email,source='manual',confirmed=1",
                (candidate_id, email, email_key),
            )
        db.execute(
            "UPDATE candidates SET display_name=? WHERE candidate_id=?", (body.name, candidate_id)
        )
        status = "complete"
    elif body.action == "select_ct_record":
        row = db.execute(
            "SELECT * FROM resources WHERE resource_id=?", (body.resource_id,)
        ).fetchone()
        if row is None:
            raise Problem(404, "RESOURCE_NOT_FOUND", "CT resource not found")
        if body.resource_id not in {r["resource_id"] for r in _options(db, candidate)}:
            raise Problem(
                422, "INVALID_CT_SELECTION", "Select a matching or unlinked CT record with an email"
            )
        other = db.execute(
            "SELECT 1 FROM candidate_emails WHERE email_key=? AND candidate_id<>? AND confirmed=1",
            (row["email_key"], candidate_id),
        ).fetchone()
        if other:
            raise Problem(
                409, "IDENTITY_CONFLICT", "CT email is already confirmed for another candidate"
            )
        old_id = row["candidate_id"]
        if old_id is not None and old_id != candidate_id:
            if _requires_merge(db, old_id, row["resource_id"], row["email_key"]):
                raise Problem(
                    409,
                    "IDENTITY_MERGE_REQUIRED",
                    "Existing candidate data requires an explicit merge",
                )
            db.execute(
                "DELETE FROM candidate_emails WHERE candidate_id=? AND email_key=? "
                "AND source='CT' AND confirmed=0",
                (old_id, row["email_key"]),
            )
        db.execute(
            "UPDATE resources SET candidate_id=?,updated_at=? WHERE resource_id=?",
            (candidate_id, now, row["resource_id"]),
        )
        db.execute(
            "INSERT INTO candidate_emails(candidate_id,email,email_key,source,confirmed) "
            "VALUES (?,?,?,'CT',1) ON CONFLICT(candidate_id,email_key) "
            "DO UPDATE SET email=excluded.email,source='CT',confirmed=1",
            (candidate_id, row["email_address"], row["email_key"]),
        )
        reclassify_candidate(db, candidate_id)
        if old_id is not None and old_id != candidate_id:
            reclassify_candidate(db, old_id)
        status = "complete"
    else:
        status = "review_later" if body.action == "review_later" else "complete"
    db.execute(
        "UPDATE candidates SET identity_status=?,updated_at=? WHERE candidate_id=?",
        (status, now, candidate_id),
    )
    append_audit(
        db,
        ts=now,
        action="IDENTITY_RESOLVE",
        user_id=user_id,
        entity_type="candidate",
        entity_id=str(candidate_id),
        details={
            "action": body.action,
            "resource_id": body.resource_id,
            "before_status": candidate["identity_status"],
            "after_status": status,
            **(
                {"name_masked": "***", "email_masked": _mask(body.email)}
                if body.action == "manual_entry"
                else {}
            ),
        },
    )
    return detail(db, candidate_id)
