"""Reviewed JD revisions and deterministic fallback (REQ-MAT-02/05, TDD §8.7)."""

import json
import re
import sqlite3
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pfc.domain.config_loader import DomainConfig
from pfc.domain.normalise import normalise_term
from pfc.services.occ.domain import occ_domain_config

from .common import atomic, audit, dumps, timestamp
from .ports import MatchingPorts


class ExtractionFailure(RuntimeError):
    pass


class ExtractedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    evidence: str | None = None
    years: float | None = Field(default=None, ge=0)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Requirement text must not be blank")
        return value


class Requirements(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mandatory_skills: list[ExtractedItem] = Field(default_factory=list)
    preferred_skills: list[ExtractedItem] = Field(default_factory=list)
    experience: list[ExtractedItem] = Field(default_factory=list)
    qualifications: list[ExtractedItem] = Field(default_factory=list)
    domain: list[ExtractedItem] = Field(default_factory=list)
    location: str | None = None
    onsite_required: bool | None = None

    @field_validator("qualifications", "domain", mode="before")
    @classmethod
    def text_items(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [{"text": item} if isinstance(item, str) else item for item in value]
        return value


KINDS = {
    "mandatory_skills": "mandatory_skill",
    "preferred_skills": "preferred_skill",
    "experience": "experience",
    "qualifications": "qualification",
    "domain": "domain",
}
PREFERRED = re.compile(r"\b(desirable|preferred|nice to have|bonus)\b", re.I)
MANDATORY = re.compile(r"\b(mandatory|required|essential|must have)\b", re.I)


def _fallback(text: str, config: DomainConfig) -> Requirements:
    terms = sorted(
        {
            term
            for row in config.synonyms.rows
            if row.status == "approved"
            for term in (row.canonical, row.term)
        },
        key=lambda s: (-len(s), s.casefold()),
    )
    output: dict[str, list[dict[str, str]]] = {"mandatory_skills": [], "preferred_skills": []}
    preferred = False
    seen: dict[str, tuple[str, dict[str, str]]] = {}
    for line in text.splitlines():
        norm = normalise_term(line)
        if PREFERRED.search(norm):
            preferred = True
        elif MANDATORY.search(norm):
            preferred = False
        for term in terms:
            canonical = config.synonyms.resolve(term)
            if not canonical:
                continue
            if re.search(r"(?<!\w)" + re.escape(normalise_term(term)) + r"(?!\w)", norm):
                key = "preferred_skills" if preferred else "mandatory_skills"
                if canonical not in seen or (
                    seen[canonical][0] == "preferred_skills" and not preferred
                ):
                    seen[canonical] = (key, {"text": canonical, "evidence": line})
    for key, item in seen.values():
        output[key].append(item)
    return Requirements.model_validate(output)


def _current(db: sqlite3.Connection, demand_id: int) -> sqlite3.Row:
    row = db.execute(
        "SELECT v.* FROM demands d JOIN demand_versions v "
        "ON v.demand_version_id=d.current_version_id AND v.demand_id=d.demand_id "
        "WHERE d.demand_id=?",
        (demand_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Demand has no current JD version")
    return row


def get_requirements(
    db: sqlite3.Connection, demand_id: int, *, jd_req_id: int | None = None
) -> dict[str, Any] | None:
    version = _current(db, demand_id)
    row = db.execute(
        "SELECT * FROM jd_requirements WHERE demand_version_id=? "
        "AND ((? IS NULL AND status IN ('draft','confirmed')) OR jd_req_id=?) "
        "ORDER BY CASE status WHEN 'draft' THEN 0 ELSE 1 END,revision DESC LIMIT 1",
        (version["demand_version_id"], jd_req_id, jd_req_id),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    event = db.execute(
        "WITH RECURSIVE ancestors(item_id,jd_req_id,source_jd_req_id) AS ("
        "SELECT item_id,jd_req_id,source_jd_req_id FROM jd_requirement_items WHERE jd_req_id=? "
        "UNION SELECT i.item_id,i.jd_req_id,i.source_jd_req_id "
        "FROM jd_requirement_items i JOIN ancestors p ON i.item_id=p.source_jd_req_id "
        "JOIN jd_requirements r ON r.jd_req_id=i.jd_req_id WHERE r.demand_version_id=?"
        ") SELECT a.details_json,r.extracted_by FROM jd_requirements r JOIN audit_log a "
        "ON a.entity_id=CAST(r.jd_req_id AS TEXT) "
        "AND a.action='jd.analyze' AND a.entity_type='jd_requirements' "
        "WHERE r.jd_req_id=? OR r.jd_req_id IN (SELECT jd_req_id FROM ancestors) "
        "ORDER BY r.revision DESC,a.audit_id DESC LIMIT 1",
        (row["jd_req_id"], row["demand_version_id"], row["jd_req_id"]),
    ).fetchone()
    extracted_by = event["extracted_by"] if event else None
    method = (
        "rule_based_fallback"
        if extracted_by == "rule_based"
        else None
        if extracted_by in {"manual", None}
        else "llm"
    )
    metadata = json.loads(event[0] or "{}") if event else {}
    result["extraction_method"] = metadata.get("extraction_method", method)
    result["fallback_reason"] = (
        metadata.get("fallback_reason") or "reason_not_recorded"
        if result["extraction_method"] == "rule_based_fallback"
        else None
    )
    result["extraction"] = json.loads(result.pop("extraction_json") or "{}")
    result["items"] = [
        dict(item)
        for item in db.execute(
            "SELECT i.*,s.name AS canonical FROM jd_requirement_items i "
            "LEFT JOIN skills s ON s.skill_id=i.canonical_skill_id "
            "WHERE jd_req_id=? ORDER BY position,item_id",
            (row["jd_req_id"],),
        )
    ]
    return result


def _write(
    db: sqlite3.Connection,
    version_id: int,
    data: Requirements,
    extracted_by: str,
    now: datetime,
    config: DomainConfig,
    *,
    source_ids: dict[tuple[str, int], int] | None = None,
) -> int:
    revision = db.execute(
        "SELECT COALESCE(MAX(revision),0)+1 FROM jd_requirements WHERE demand_version_id=?",
        (version_id,),
    ).fetchone()[0]
    db.execute(
        "UPDATE jd_requirements SET status='superseded' "
        "WHERE demand_version_id=? AND status='draft'",
        (version_id,),
    )
    years = [item.years for item in data.experience if item.years is not None]
    cursor = db.execute(
        "INSERT INTO jd_requirements(demand_version_id,revision,status,extracted_by,"
        "prompt_version,extraction_json,location_norm,onsite_required,min_years_exp,created_at)"
        " VALUES (?,?,'draft',?,'jd_extract.v1',?,?,?,?,?)",
        (
            version_id,
            revision,
            extracted_by,
            dumps(data.model_dump()),
            normalise_term(data.location) if data.location else None,
            data.onsite_required,
            max(years) if years else None,
            timestamp(now),
        ),
    )
    req_id = int(cursor.lastrowid or 0)
    position = 0
    for field, kind in KINDS.items():
        for index, item in enumerate(getattr(data, field)):
            canonical = config.synonyms.resolve(item.text)
            if canonical:
                db.execute(
                    "INSERT OR IGNORE INTO skills(name,created_by) VALUES (?,'system')",
                    (canonical,),
                )
            skill = db.execute(
                "SELECT skill_id FROM skills WHERE name=? COLLATE NOCASE AND is_active=1",
                (canonical or item.text,),
            ).fetchone()
            source_id = (source_ids or {}).get((kind, index))
            source = (
                db.execute(
                    "SELECT i.item_id FROM jd_requirement_items i JOIN jd_requirements r "
                    "ON r.jd_req_id=i.jd_req_id WHERE i.item_id=? "
                    "AND r.demand_version_id=? AND r.jd_req_id<>?",
                    (source_id, version_id, req_id),
                ).fetchone()
                if source_id is not None
                else None
            )
            db.execute(
                "INSERT INTO jd_requirement_items(jd_req_id,kind,text,canonical_skill_id,"
                "source_span,position,source_jd_req_id) VALUES (?,?,?,?,?,?,?)",
                (
                    req_id,
                    kind,
                    item.text,
                    skill[0] if skill else None,
                    item.evidence,
                    position,
                    source[0] if source else None,
                ),
            )
            position += 1
    return req_id


def analyze_jd(
    db: sqlite3.Connection,
    demand_id: int,
    ports: MatchingPorts,
    user_id: int | None,
    now: datetime,
) -> dict[str, Any]:
    """REQ-MAT-02: extraction failure falls back; confirmed revisions are untouched."""
    version = _current(db, demand_id)
    text = version["jd_text_norm"]
    if not text.strip():
        raise ValueError("JD text is blank")
    config = occ_domain_config(db)
    extracted_by = "rule_based"
    fallback_reason = None
    try:
        if ports.llm is None:
            raise ExtractionFailure("provider_unavailable")
        data = Requirements.model_validate(ports.llm.extract_jd(text))
        extracted_by = ports.llm.model
    except Exception as exc:
        fallback_reason = (
            str(exc)
            if isinstance(exc, ExtractionFailure)
            else "schema_invalid"
            if isinstance(exc, ValidationError)
            else "provider_error"
        )
        data = _fallback(text, config)
    with atomic(db, immediate=True):
        if _current(db, demand_id)["demand_version_id"] != version["demand_version_id"]:
            raise ValueError("JD version changed during analysis; retry")
        req_id = _write(db, version["demand_version_id"], data, extracted_by, now, config)
        audit(
            db,
            "jd.analyze",
            "jd_requirements",
            req_id,
            user_id,
            now,
            demand_id=demand_id,
            extracted_by=extracted_by,
            extraction_method="rule_based_fallback" if fallback_reason is not None else "llm",
            fallback_reason=fallback_reason,
        )
    result = get_requirements(db, demand_id)
    assert result is not None
    return result


def save_draft(
    db: sqlite3.Connection,
    demand_id: int,
    edited_requirements: dict[str, Any],
    user_id: int,
    now: datetime,
) -> dict[str, Any]:
    cleaned = dict(edited_requirements)
    sources: dict[tuple[str, int], int] = {}
    for field, kind in KINDS.items():
        if not isinstance(cleaned.get(field), list):
            continue
        items = []
        for index, item in enumerate(cleaned[field]):
            if isinstance(item, dict):
                item = dict(item)
                source_id = item.pop("jdReqId", None)
                if type(source_id) is int:
                    sources[(kind, index)] = source_id
            items.append(item)
        cleaned[field] = items
    data = Requirements.model_validate(cleaned)
    config = occ_domain_config(db)
    with atomic(db):
        version = _current(db, demand_id)
        req_id = _write(
            db, version["demand_version_id"], data, "manual", now, config, source_ids=sources
        )
        audit(db, "jd.edit", "jd_requirements", req_id, user_id, now, demand_id=demand_id)
    result = get_requirements(db, demand_id)
    assert result is not None
    return result


def confirm_requirements(
    db: sqlite3.Connection,
    demand_id: int,
    edited_requirements: dict[str, Any] | None,
    user_id: int,
    now: datetime,
) -> dict[str, Any]:
    """REQ-MAT-02: replace confirmation atomically, preserving the revision history."""
    data = (
        Requirements.model_validate(edited_requirements)
        if edited_requirements is not None
        else None
    )
    with atomic(db):
        version = _current(db, demand_id)
        if data is not None:
            req_id = _write(
                db, version["demand_version_id"], data, "manual", now, occ_domain_config(db)
            )
        else:
            draft = db.execute(
                "SELECT jd_req_id FROM jd_requirements WHERE demand_version_id=? "
                "AND status='draft' ORDER BY revision DESC LIMIT 1",
                (version["demand_version_id"],),
            ).fetchone()
            if draft is None:
                raise ValueError("No draft requirements to confirm")
            req_id = draft[0]
        db.execute(
            "UPDATE jd_requirements SET status='superseded' "
            "WHERE demand_version_id=? AND status='confirmed'",
            (version["demand_version_id"],),
        )
        db.execute(
            "UPDATE jd_requirements SET status='confirmed',confirmed_by=?,confirmed_at=? "
            "WHERE jd_req_id=?",
            (user_id, timestamp(now), req_id),
        )
        audit(db, "jd.confirm", "jd_requirements", req_id, user_id, now, demand_id=demand_id)
    result = get_requirements(db, demand_id)
    assert result is not None
    return result
