"""OCC-backed immutable matching and ingestion reference data."""

import json
import sqlite3

from pfc.domain.config_loader import (
    BenchStatus,
    BenchStatuses,
    DomainConfig,
    GradeLadder,
    LocationConfig,
    ScoringTemplate,
)
from pfc.domain.normalise import normalise_term
from pfc.services.occ.synonyms import valid_synonym_table


def active_template(db: sqlite3.Connection) -> tuple[int, ScoringTemplate]:
    row = db.execute(
        "SELECT * FROM scoring_templates WHERE name='default' AND status='active' "
        "ORDER BY version DESC,template_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValueError("Default template is not active; activate it first")
    rules = json.loads(row["rules_json"])
    rules.pop("_meta", None)
    rules["weights"] = json.loads(row["weights_json"])
    rules.update(name=row["name"], version=row["version"])
    return row["template_id"], ScoringTemplate.from_dict(rules)


def occ_domain_config(db: sqlite3.Connection) -> DomainConfig:
    location_query = (
        "SELECT value_json FROM settings WHERE scope='system' AND user_id IS NULL "
        "AND key='location.aliases'"
    )
    statuses = {
        row["code"]: BenchStatus(**dict(row))
        for row in db.execute("SELECT * FROM bench_statuses ORDER BY code")
    }
    mapping = {}
    for row in db.execute("SELECT * FROM status_map ORDER BY source_value"):
        canonical = statuses.get(row["canonical_code"])
        if canonical:
            source = normalise_term(row["source_value"])
            key = f"source:{source}"
            statuses[key] = canonical.model_copy(
                update={"is_excluded": bool(row["is_excluded"]), "priority": row["priority"]}
            )
            mapping[source] = key
    location = db.execute(location_query).fetchone()
    locations = (
        LocationConfig.model_validate(json.loads(location[0]))
        if location is not None
        else LocationConfig(aliases={}, travel_clusters={})
    )
    _, template = active_template(db)
    return DomainConfig(
        grades=GradeLadder(
            codes=tuple(row[0] for row in db.execute("SELECT code FROM grades ORDER BY ordinal"))
        ),
        bench_statuses=BenchStatuses(statuses=statuses, status_map=mapping),
        synonyms=valid_synonym_table(db),
        locations=LocationConfig(
            aliases={normalise_term(k): normalise_term(v) for k, v in locations.aliases.items()},
            travel_clusters={
                k: tuple(normalise_term(v) for v in values)
                for k, values in locations.travel_clusters.items()
            },
        ),
        template=template,
    )
