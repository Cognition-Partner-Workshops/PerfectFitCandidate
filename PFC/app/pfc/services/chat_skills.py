"""Query-time canonical skill projection; raw CT/CV text never leaves trusted code."""

import json
import sqlite3

from pfc.domain.cv_profile import skill_hits
from pfc.domain.normalise import normalise_term, tokenise_skills
from pfc.services.ingestion.masking import mask_evidence
from pfc.services.occ.synonyms import valid_synonym_table


def resource_skills(
    db: sqlite3.Connection,
    resource_id: int | None = None,
) -> dict[int, str]:
    synonyms = valid_synonym_table(db)
    active = {
        normalise_term(row["name"]): row["name"]
        for row in db.execute("SELECT name FROM skills WHERE is_active=1")
    }
    rows: dict[int, dict[tuple[str, str], float | None]] = {}

    def add(id: int, term: str, source: str, years: float | None = None) -> None:
        name = active.get(normalise_term(synonyms.resolve(term) or term))
        if name:
            key = (name, source)
            previous = rows.setdefault(id, {}).get(key)
            rows[id][key] = (
                max(previous, years)
                if previous is not None and years is not None
                else (previous if previous is not None else years)
            )

    for row in db.execute(
        "SELECT resource_id,skills_raw FROM resources WHERE lifecycle!='archived' "
        "AND (? IS NULL OR resource_id=?)",
        (resource_id, resource_id),
    ):
        for token in tokenise_skills(row["skills_raw"] or "", synonyms).tokens:
            add(row["resource_id"], token.canonical or token.normalised, "CT")
    for row in db.execute(
        "SELECT r.resource_id,rs.skill_raw,s.name,rs.years FROM resources r "
        "JOIN cv_versions v USING(candidate_id) "
        "JOIN resource_skills rs ON rs.cv_version_id=v.cv_version_id AND rs.source='CV' "
        "AND rs.resource_id=r.resource_id "
        "LEFT JOIN skills s ON s.skill_id=rs.canonical_skill_id AND s.is_active=1 "
        "WHERE r.lifecycle!='archived' AND v.is_latest=1 AND v.extraction_removed_at IS NULL "
        "AND (? IS NULL OR r.resource_id=?)",
        (resource_id, resource_id),
    ):
        add(
            row["resource_id"],
            synonyms.resolve(row["skill_raw"]) or row["name"] or row["skill_raw"],
            "CV",
            row["years"],
        )
    for row in db.execute(
        "SELECT r.resource_id,v.text_norm FROM resources r JOIN cv_versions v USING(candidate_id) "
        "WHERE r.lifecycle!='archived' AND v.is_latest=1 AND v.extraction_removed_at IS NULL "
        "AND (? IS NULL OR r.resource_id=?)",
        (resource_id, resource_id),
    ):
        for _, canonical, _ in skill_hits(mask_evidence(row["text_norm"] or ""), synonyms):
            add(row["resource_id"], canonical, "CV")
    return {
        id: json.dumps(
            [
                {"skill": skill, "source": source, "years": years}
                for (skill, source), years in sorted(values.items())
            ]
        )
        for id, values in rows.items()
    }


def register_chat_skills(db: sqlite3.Connection, *, snapshot: bool = False) -> None:
    projection = resource_skills(db) if snapshot else None

    def lookup(resource_id: int) -> str:
        current = resource_skills(db, resource_id) if projection is None else projection
        return current.get(resource_id, "[]")

    db.create_function("chat_resource_skills", 1, lookup)
