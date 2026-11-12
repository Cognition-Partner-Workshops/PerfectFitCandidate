"""Admin-reviewed skill suggestions from CT, CV, and current JD analysis."""

import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from pfc.domain.config_loader import DomainConfig
from pfc.domain.normalise import SynonymTable, tokenise_skills
from pfc.security.problems import Problem
from pfc.services.ingestion.eligibility import refresh_resource_eligibility
from pfc.services.ingestion.masking import EMAIL, phone_matches
from pfc.services.matching.cv_bases import load_cv_bases, load_resources
from pfc.services.occ.common import audit_write, now
from pfc.services.occ.domain import occ_domain_config
from pfc.services.occ.seed import normalize_term

from . import synonyms

JD_FILLER_WORDS = frozenset(
    {
        "strong",
        "good",
        "solid",
        "excellent",
        "advanced",
        "basic",
        "working",
        "hands-on",
        "hands",
        "on",
        "proven",
        "experience",
        "experienced",
        "knowledge",
        "understanding",
        "proficiency",
        "proficient",
        "expertise",
        "skills",
        "skill",
        "of",
        "in",
        "with",
        "and",
    }
)
_JD_YEAR = re.compile(r"\b(\d+\+?|years?|yrs?)\b", re.IGNORECASE)
_JD_FILLER = re.compile(
    r"\b(?:"
    + "|".join(
        re.escape(word) for word in sorted(JD_FILLER_WORDS, key=lambda word: (-len(word), word))
    )
    + r")\b",
    re.IGNORECASE,
)


def is_skill_like_jd_term(text: str, synonym_table: SynonymTable) -> bool:
    term = text.strip()
    if not term or len(term) > 40 or len(term.split()) > 4:
        return False
    if ":" in term or ";" in term or term.endswith(".") or ". " in term:
        return False
    if _JD_YEAR.search(term):
        return False
    remainder = normalize_term(_JD_FILLER.sub(" ", term))
    return bool(remainder and synonym_table.resolve(remainder) is None)


def _contains_pii(term: str) -> bool:
    return bool(EMAIL.search(term) or next(phone_matches(term), None))


def _is_unrecognised(
    normalised: str, synonym_table: SynonymTable, canonical: str | None = None
) -> bool:
    return not (canonical or synonym_table.resolve(normalised))


def _collect_occurrences(
    db: sqlite3.Connection, config: DomainConfig
) -> tuple[dict[str, dict[str, set[int]]], dict[str, Counter[str]], set[str]]:
    people: dict[str, dict[str, set[int]]] = defaultdict(
        lambda: {"ct": set(), "cv": set(), "jd": set()}
    )
    spellings: dict[str, Counter[str]] = defaultdict(Counter)
    pii_norms: set[str] = set()

    def add(source: str, entity_id: int, raw: str) -> None:
        norm = normalize_term(raw)
        if not norm:
            return
        if _contains_pii(raw):
            pii_norms.add(norm)
            return
        people[norm][source].add(entity_id)
        spellings[norm][raw] += 1

    resources = load_resources(db)
    for resource in resources:
        for token in tokenise_skills(resource.ct_skills or "", config.synonyms).tokens:
            if _is_unrecognised(token.normalised, config.synonyms, token.canonical):
                add("ct", int(resource.resource_id), token.raw)

    active_candidates = {resource.candidate_id for resource in resources if resource.candidate_id}
    for candidate_key, bases in load_cv_bases(db, config).items():
        if candidate_key not in active_candidates:
            continue
        candidate_id = int(candidate_key.removeprefix("candidate:"))
        seen: set[tuple[str, str]] = set()
        for basis in bases:
            if not basis.is_latest:
                continue
            for skill in basis.skills:
                if not _is_unrecognised(skill.normalised, config.synonyms, skill.canonical):
                    continue
                norm = normalize_term(skill.raw)
                occurrence = (norm, skill.raw)
                if occurrence in seen:
                    continue
                seen.add(occurrence)
                add("cv", candidate_id, skill.raw)

    revisions = db.execute(
        "SELECT d.demand_id,q.jd_req_id FROM demands d "
        "JOIN jd_requirements q ON q.demand_version_id=d.current_version_id "
        "WHERE d.lifecycle='open' AND q.status IN ('draft','confirmed') "
        "AND q.revision=(SELECT MAX(latest.revision) FROM jd_requirements latest "
        "WHERE latest.demand_version_id=q.demand_version_id "
        "AND latest.status IN ('draft','confirmed')) "
        "ORDER BY d.demand_id,q.revision DESC"
    ).fetchall()
    latest_by_demand: dict[int, int] = {}
    for revision in revisions:
        latest_by_demand.setdefault(int(revision["demand_id"]), int(revision["jd_req_id"]))
    for demand_id, jd_req_id in latest_by_demand.items():
        items = db.execute(
            "SELECT text FROM jd_requirement_items WHERE jd_req_id=? "
            "AND kind IN ('mandatory_skill','preferred_skill') AND canonical_skill_id IS NULL "
            "ORDER BY position,item_id",
            (jd_req_id,),
        ).fetchall()
        seen_terms: set[str] = set()
        for item in items:
            raw = item["text"]
            norm = normalize_term(raw)
            if (
                not norm
                or norm in seen_terms
                or config.synonyms.resolve(raw) is not None
                or not is_skill_like_jd_term(raw, config.synonyms)
            ):
                continue
            seen_terms.add(norm)
            add("jd", demand_id, raw)
    return people, spellings, pii_norms


def list_suggestions(db: sqlite3.Connection) -> list[dict[str, Any]]:
    config = occ_domain_config(db)
    people, spellings, pii_norms = _collect_occurrences(db, config)
    rejected = {row[0] for row in db.execute("SELECT term_norm FROM skill_term_rejections")}
    existing = {row[0] for row in db.execute("SELECT term_norm FROM skill_synonyms")}
    suggestions = []
    for norm, sources in people.items():
        if norm in rejected or norm in existing or norm in pii_norms:
            continue
        raw_spellings = spellings[norm]
        term = min(raw_spellings, key=lambda raw: (-raw_spellings[raw], raw.casefold(), raw))
        closest = config.synonyms.suggest(term)
        suggestions.append(
            {
                "term": term,
                "term_norm": norm,
                "ct_people": len(sources["ct"]),
                "cv_people": len(sources["cv"]),
                "jd_demands": len(sources["jd"]),
                "closest": closest[0] if closest else None,
            }
        )
    return sorted(
        suggestions,
        key=lambda item: (
            -(item["ct_people"] + item["cv_people"] + item["jd_demands"]),
            item["term"].casefold(),
        ),
    )


def unrecognised_skills_by_resource(
    db: sqlite3.Connection, resource_ids: Iterable[int]
) -> dict[int, list[str]]:
    selected = set(resource_ids)
    result: dict[int, list[str]] = {resource_id: [] for resource_id in selected}
    if not selected:
        return result

    config = occ_domain_config(db)
    resources = [
        resource for resource in load_resources(db) if int(resource.resource_id) in selected
    ]
    spellings: dict[int, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    pii_norms: dict[int, set[str]] = defaultdict(set)

    def add(resource_id: int, raw: str) -> None:
        norm = normalize_term(raw)
        if not norm:
            return
        if _contains_pii(raw):
            pii_norms[resource_id].add(norm)
            return
        spellings[resource_id][norm][raw] += 1

    for resource in resources:
        resource_id = int(resource.resource_id)
        for token in tokenise_skills(resource.ct_skills or "", config.synonyms).tokens:
            if _is_unrecognised(token.normalised, config.synonyms, token.canonical):
                add(resource_id, token.raw)

    cv_bases = load_cv_bases(db, config)
    for resource in resources:
        resource_id = int(resource.resource_id)
        candidate_key = resource.candidate_id or ""
        for basis in cv_bases.get(candidate_key, []):
            if not basis.is_latest:
                continue
            for skill in basis.skills:
                if _is_unrecognised(skill.normalised, config.synonyms, skill.canonical):
                    add(resource_id, skill.raw)

    for resource_id, terms in spellings.items():
        result[resource_id] = sorted(
            (
                min(spelling_counts, key=lambda raw: (-spelling_counts[raw], raw.casefold(), raw))
                for norm, spelling_counts in terms.items()
                if norm not in pii_norms[resource_id]
            ),
            key=lambda term: (term.casefold(), term),
        )
    return result


def accept(
    db: sqlite3.Connection, user_id: int, items: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    db.execute("SAVEPOINT skill_suggestion_accept")
    accepted = []
    seen: set[str] = set()
    try:
        for item in items:
            term = str(item["term"]).strip()
            norm = normalize_term(term)
            if not norm or _contains_pii(term):
                raise Problem(422, "VALIDATION_ERROR", "Term must be a non-PII skill name")
            if (
                norm in seen
                or db.execute("SELECT 1 FROM skill_synonyms WHERE term_norm=?", (norm,)).fetchone()
            ):
                raise Problem(422, "VALIDATION_ERROR", f"{term}: term is already mapped")
            seen.add(norm)
            canonical = item.get("canonical")
            if canonical is None:
                data = {
                    "canonical": term,
                    "term": term,
                    "relation": "equivalent",
                    "credit": 1.0,
                    "status": "approved",
                }
            else:
                synonyms.skill_id(db, user_id, str(canonical), existing_only=True)
                data = {
                    "canonical": str(canonical),
                    "term": term,
                    "relation": item.get("relation", "equivalent"),
                    "credit": item.get("credit", 1.0),
                    "status": "approved",
                }
            try:
                accepted.append(synonyms.save(db, user_id, data, refresh_eligibility=False))
            except Problem as exc:
                if exc.status == 422:
                    raise Problem(422, exc.code, f"{term}: {exc.detail}") from exc
                raise
        refresh_resource_eligibility(db)
        db.execute("RELEASE SAVEPOINT skill_suggestion_accept")
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT skill_suggestion_accept")
        db.execute("RELEASE SAVEPOINT skill_suggestion_accept")
        raise
    return accepted


def list_rejected(db: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.execute(
            "SELECT term_norm,term,rejected_by,rejected_at FROM skill_term_rejections "
            "ORDER BY term COLLATE NOCASE,term_norm"
        )
    ]


def reject(db: sqlite3.Connection, user_id: int, terms: Iterable[str]) -> list[dict[str, Any]]:
    rejected_terms: dict[str, str] = {}
    for value in terms:
        term = value.strip()
        norm = normalize_term(term)
        if not norm or _contains_pii(term):
            raise Problem(422, "VALIDATION_ERROR", "Term must be a non-PII skill name")
        rejected_terms.setdefault(norm, term)
    for norm, term in rejected_terms.items():
        before_row = db.execute(
            "SELECT * FROM skill_term_rejections WHERE term_norm=?", (norm,)
        ).fetchone()
        before = dict(before_row) if before_row else None
        after = {
            "term_norm": norm,
            "term": term,
            "rejected_by": user_id,
            "rejected_at": now(),
        }
        db.execute(
            "INSERT INTO skill_term_rejections(term_norm,term,rejected_by,rejected_at) "
            "VALUES (:term_norm,:term,:rejected_by,:rejected_at) "
            "ON CONFLICT(term_norm) DO UPDATE SET term=excluded.term,"
            "rejected_by=excluded.rejected_by,rejected_at=excluded.rejected_at",
            after,
        )
        audit_write(db, user_id, "OCC_EDIT", "skill_term_rejections", norm, before, after)
    return list_rejected(db)


def restore(db: sqlite3.Connection, user_id: int, term_norms: Iterable[str]) -> int:
    restored = 0
    for norm in dict.fromkeys(term_norms):
        before_row = db.execute(
            "SELECT * FROM skill_term_rejections WHERE term_norm=?", (norm,)
        ).fetchone()
        if before_row is None:
            continue
        before = dict(before_row)
        db.execute("DELETE FROM skill_term_rejections WHERE term_norm=?", (norm,))
        audit_write(db, user_id, "OCC_EDIT", "skill_term_rejections", norm, before, None)
        restored += 1
    refresh_resource_eligibility(db)
    return restored
