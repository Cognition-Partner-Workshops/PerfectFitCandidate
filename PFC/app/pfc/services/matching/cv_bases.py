"""Latest, non-removed CV skill bases shared by matching and eligibility."""

import sqlite3
from collections.abc import Iterable

from pfc.domain.config_loader import DomainConfig
from pfc.domain.normalise import ParsedDate, normalise_term, parse_date
from pfc.domain.scoring import Resource, SkillBasis, SkillEvidence


def load_resources(
    db: sqlite3.Connection, resource_ids: Iterable[int] | None = None
) -> list[Resource]:
    selected = sorted(set(resource_ids)) if resource_ids is not None else None
    if selected is None:
        rows = db.execute(
            "SELECT * FROM resources WHERE lifecycle='active' ORDER BY resource_id"
        ).fetchall()
    elif not selected:
        return []
    else:
        placeholders = ",".join("?" for _ in selected)
        rows = db.execute(
            f"SELECT * FROM resources WHERE resource_id IN ({placeholders}) ORDER BY resource_id",
            selected,
        ).fetchall()

    resources = []
    for row in rows:
        end = parse_date(row["bench_end_date"])
        if row["bench_end_date_parse_status"] == "unparseable":
            end = ParsedDate(
                None, "unparseable", row["bench_end_date"], "Bench End Date unparseable"
            )
        resources.append(
            Resource(
                str(row["resource_id"]),
                row["short_name"],
                row["grade"],
                row["location"],
                row["bench_status"],
                row["group_name"],
                parse_date(row["bench_start_date"]),
                end,
                row["skills_raw"],
                f"candidate:{row['candidate_id']}"
                if row["candidate_id"]
                else f"resource:{row['resource_id']}",
            )
        )
    return resources


def load_cv_bases(
    db: sqlite3.Connection,
    config: DomainConfig,
    resource_ids: Iterable[int] | None = None,
) -> dict[str, list[SkillBasis]]:
    selected = sorted(set(resource_ids)) if resource_ids is not None else None
    cv_bases: dict[str, list[SkillBasis]] = {}
    versions = db.execute(
        "SELECT * FROM cv_versions WHERE candidate_id IS NOT NULL "
        "AND is_latest=1 AND extraction_removed_at IS NULL "
        "ORDER BY candidate_id,version_no,cv_version_id",
    ).fetchall()
    included_candidates = {
        resource.candidate_id for resource in load_resources(db) if resource.candidate_id
    }
    if selected:
        included_candidates.update(
            resource.candidate_id
            for resource in load_resources(db, selected)
            if resource.candidate_id
        )
    canonical_ids = {
        normalise_term(row["name"]): row["skill_id"]
        for row in db.execute("SELECT skill_id,name FROM skills WHERE is_active=1")
    }
    for version in versions:
        candidate = f"candidate:{version['candidate_id']}"
        if candidate not in included_candidates:
            continue
        resource_lifecycle = "lifecycle='active'"
        resource_params: tuple[int, ...] = ()
        if selected:
            placeholders = ",".join("?" for _ in selected)
            resource_lifecycle = f"(lifecycle='active' OR resource_id IN ({placeholders}))"
            resource_params = tuple(selected)
        skills = db.execute(
            "SELECT rs.*,s.name AS canonical FROM resource_skills rs "
            "LEFT JOIN skills s ON s.skill_id=rs.canonical_skill_id "
            "WHERE rs.cv_version_id=? AND rs.source='CV' AND rs.resource_id IN "
            f"(SELECT resource_id FROM resources WHERE candidate_id=? AND {resource_lifecycle}) "
            "ORDER BY rs.resource_skill_id",
            (version["cv_version_id"], version["candidate_id"], *resource_params),
        ).fetchall()
        evidence = tuple(
            dict.fromkeys(
                SkillEvidence(
                    item["skill_raw"],
                    normalise_term(item["skill_norm"]),
                    config.synonyms.resolve(item["skill_raw"]),
                    item["evidence"] or item["skill_raw"],
                    canonical_ids.get(
                        normalise_term(config.synonyms.resolve(item["skill_raw"]) or "")
                    ),
                )
                for item in skills
            )
        )
        cv_bases.setdefault(candidate, []).append(
            SkillBasis(
                f"CV:{version['cv_version_id']}",
                f"CV v{version['version_no']}",
                evidence,
                bool(version["is_latest"]),
            )
        )
    return cv_bases
