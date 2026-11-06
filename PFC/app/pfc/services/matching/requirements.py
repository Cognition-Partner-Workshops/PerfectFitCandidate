"""Reviewed JD revisions and deterministic fallback (REQ-MAT-02/05, TDD §8.7)."""

import json
import re
import sqlite3
import unicodedata
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic.json_schema import SkipJsonSchema

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
    classification_reason: Literal["classification_adjusted"] | None = None
    canonicalized_from: SkipJsonSchema[str | None] = None
    evidence_unverified: bool = False

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


def _classification_reason(
    extraction: dict[str, Any], position: int
) -> Literal["classification_adjusted"] | None:
    items = [item for field in KINDS for item in extraction.get(field, [])]
    if (
        0 <= position < len(items)
        and isinstance(items[position], dict)
        and items[position].get("classification_reason") == "classification_adjusted"
    ):
        return "classification_adjusted"
    return None


def _extraction_json(data: Requirements) -> str:
    value = data.model_dump()
    for field in KINDS:
        for item in value[field]:
            if item.get("classification_reason") is None:
                item.pop("classification_reason", None)
            if item.get("canonicalized_from") is None:
                item.pop("canonicalized_from", None)
            if not item["evidence_unverified"]:
                item.pop("evidence_unverified")
    return dumps(value)


def _canonicalized_from(extraction: dict[str, Any], position: int) -> str | None:
    items = [item for field in KINDS for item in extraction.get(field, [])]
    if 0 <= position < len(items) and isinstance(items[position], dict):
        value = items[position].get("canonicalized_from")
        return value if isinstance(value, str) else None
    return None


def _canonicalize_skills(data: Requirements, config: DomainConfig) -> Requirements:
    data = data.model_copy(deep=True)
    terms = sorted(
        {
            normalise_term(term)
            for row in config.synonyms.rows
            if row.status == "approved"
            for term in (row.canonical, row.term)
            if config.synonyms.resolve(term) is not None
        },
        key=lambda term: (-len(term), term),
    )
    skills = [*data.mandatory_skills, *data.preferred_skills]
    present = {normalise_term(item.text) for item in skills}
    present.update(
        normalise_term(canonical)
        for item in skills
        if (canonical := config.synonyms.resolve(item.text)) is not None
    )
    for item in skills:
        if config.synonyms.resolve(item.text) is not None:
            continue
        masked = normalise_term(item.text)
        canonicals: set[str] = set()
        for term in terms:
            canonical = config.synonyms.resolve(term)
            assert canonical is not None
            matches = list(re.finditer(r"(?<!\w)" + re.escape(term) + r"(?!\w)", masked))
            for match in matches:
                canonicals.add(canonical)
                masked = (
                    masked[: match.start()]
                    + "\0" * (match.end() - match.start())
                    + masked[match.end() :]
                )
        if len(canonicals) == 1:
            canonical = next(iter(canonicals))
            if normalise_term(canonical) not in present:
                item.canonicalized_from = item.text
                item.text = canonical
                present.add(normalise_term(canonical))
    return data


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
        {
            **dict(item),
            "classification_reason": _classification_reason(result["extraction"], item["position"]),
        }
        for item in db.execute(
            "SELECT i.*,s.name AS canonical FROM jd_requirement_items i "
            "LEFT JOIN skills s ON s.skill_id=i.canonical_skill_id "
            "WHERE jd_req_id=? ORDER BY position,item_id",
            (row["jd_req_id"],),
        )
    ]
    offsets: dict[str, int] = {}
    fields = {kind: field for field, kind in KINDS.items()}
    for item in result["items"]:
        field = fields[item["kind"]]
        index = offsets.get(field, 0)
        extracted = result["extraction"].get(field, [])
        detail = extracted[index] if index < len(extracted) else {}
        item["evidence_unverified"] = bool(detail.get("evidence_unverified", False))
        offsets[field] = index + 1
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
    if extracted_by == "manual":
        for field in KINDS:
            for item in getattr(data, field):
                item.classification_reason = None
                item.canonicalized_from = None
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
            _extraction_json(data),
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
                    "SELECT i.item_id,i.position,i.kind,i.text,r.extraction_json "
                    "FROM jd_requirement_items i JOIN jd_requirements r "
                    "ON r.jd_req_id=i.jd_req_id WHERE i.item_id=? "
                    "AND r.demand_version_id=? AND r.jd_req_id<>?",
                    (source_id, version_id, req_id),
                ).fetchone()
                if source_id is not None
                else None
            )
            if (
                source is not None
                and source["kind"] == kind
                and unicodedata.normalize("NFKC", source["text"]).casefold().split()
                == unicodedata.normalize("NFKC", item.text).casefold().split()
            ):
                item.classification_reason = _classification_reason(
                    json.loads(source["extraction_json"]), source["position"]
                )
                item.canonicalized_from = _canonicalized_from(
                    json.loads(source["extraction_json"]), source["position"]
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
    if extracted_by == "manual":
        db.execute(
            "UPDATE jd_requirements SET extraction_json=? WHERE jd_req_id=?",
            (_extraction_json(data), req_id),
        )
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
        data = _canonicalize_skills(data, config)
        for field in KINDS:
            for item in getattr(data, field):
                item.evidence_unverified = (
                    not item.evidence or not item.evidence.strip() or item.evidence not in text
                )
                if item.evidence_unverified:
                    item.evidence = None
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


def _edited_data(
    db: sqlite3.Connection, demand_id: int, edited: dict[str, Any]
) -> tuple[Requirements, dict[tuple[str, int], int]]:
    cleaned = dict(edited)
    previous = get_requirements(db, demand_id)
    prior = previous["items"] if previous else []
    version_id = _current(db, demand_id)["demand_version_id"]
    sources: dict[tuple[str, int], int] = {}
    for field, kind in KINDS.items():
        if not isinstance(cleaned.get(field, []), list):
            continue
        items = []
        available = [item for item in prior if item["kind"] == kind]
        for index, raw in enumerate(cleaned.get(field, [])):
            if isinstance(raw, str) and field in {"qualifications", "domain"}:
                raw = {"text": raw}
            if not isinstance(raw, dict):
                items.append(raw)
                continue
            item = dict(raw)
            source_id = item.pop("jdReqId", None)
            source = None
            if type(source_id) is int:
                parent = db.execute(
                    "SELECT i.jd_req_id FROM jd_requirement_items i JOIN jd_requirements r "
                    "ON r.jd_req_id=i.jd_req_id WHERE i.item_id=? AND i.kind=? "
                    "AND r.demand_version_id=?",
                    (source_id, kind, version_id),
                ).fetchone()
                if parent:
                    ancestor = get_requirements(db, demand_id, jd_req_id=parent[0])
                    assert ancestor is not None
                    source = next(i for i in ancestor["items"] if i["item_id"] == source_id)
                    sources[(kind, index)] = source_id
            else:
                source = next((i for i in available if i["text"] == item.get("text")), None)
                if source:
                    available.remove(source)
            if source:
                item.setdefault("evidence", source["source_span"])
                if source["evidence_unverified"]:
                    item["evidence_unverified"] = True
            if item.get("evidence_unverified"):
                item["evidence"] = None
            items.append(item)
        if field in cleaned:
            cleaned[field] = items
    return Requirements.model_validate(cleaned), sources


def save_draft(
    db: sqlite3.Connection,
    demand_id: int,
    edited_requirements: dict[str, Any],
    user_id: int,
    now: datetime,
) -> dict[str, Any]:
    data, sources = _edited_data(db, demand_id, edited_requirements)
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
    data, sources = (
        _edited_data(db, demand_id, edited_requirements)
        if edited_requirements is not None
        else (None, {})
    )
    with atomic(db):
        version = _current(db, demand_id)
        if data is not None:
            req_id = _write(
                db,
                version["demand_version_id"],
                data,
                "manual",
                now,
                occ_domain_config(db),
                source_ids=sources,
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
