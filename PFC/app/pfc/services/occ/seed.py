import csv
import json
import logging
import os
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter

from pfc.api.schemas.occ import BenchStatus, Locations
from pfc.security.problems import Problem
from pfc.services.occ.common import now
from pfc.services.occ.llm import seed_default_models
from pfc.services.occ.templates import validate_placeholders

logger = logging.getLogger(__name__)
RELATED_SEED_MARKER = "occ.related_synonyms.v1"


def normalize_term(term: str) -> str:
    return " ".join(
        "".join(c for c in term.casefold() if not unicodedata.category(c).startswith("P")).split()
    )


def _insert(db: sqlite3.Connection, table: str, data: dict[str, Any]) -> None:
    db.execute(
        f"INSERT INTO {table} ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",
        tuple(data.values()),
    )


def _seed(db: sqlite3.Connection, filename: str, data: Any) -> None:
    if filename == "grades.yaml":
        codes = TypeAdapter(list[str]).validate_python(data)
        if any(not code.strip() for code in codes) or len(set(codes)) != len(codes):
            raise ValueError("Invalid grade codes")
        for ordinal, code in enumerate(codes, 1):
            _insert(db, "grades", {"code": code, "ordinal": ordinal, "active": 1})
    elif filename == "bench_statuses.yaml":
        for status in TypeAdapter(list[BenchStatus]).validate_python(data):
            _insert(db, "bench_statuses", status.model_dump())
    elif filename == "status_map.yaml":
        mapping = TypeAdapter(dict[str, str]).validate_python(data)
        for label, code in mapping.items():
            row = db.execute("SELECT * FROM bench_statuses WHERE code=?", (code,)).fetchone()
            if row is None:
                logger.warning("Skipping unknown canonical bench status in status_map seed")
                continue
            _insert(
                db,
                "status_map",
                {
                    "source_value": label,
                    "canonical_code": code,
                    "priority": row["priority"] or 2,
                    "is_excluded": row["is_excluded"],
                    "updated_at": now(),
                },
            )
    elif filename == "skill_synonyms.seed.csv":
        for item in data:
            canonical, term = item["canonical"].strip(), item["term"].strip()
            norm = normalize_term(term)
            if not canonical or not norm:
                raise ValueError("Empty skill or term")
            db.execute(
                "INSERT INTO skills(name,created_by) VALUES (?,'seed') "
                "ON CONFLICT(name) DO NOTHING",
                (canonical,),
            )
            skill_id = db.execute(
                "SELECT skill_id FROM skills WHERE name=?", (canonical,)
            ).fetchone()[0]
            db.execute(
                "INSERT INTO skill_synonyms(skill_id,term,term_norm,relation,credit,status,"
                "updated_at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(skill_id,term_norm) DO NOTHING",
                (
                    skill_id,
                    term,
                    norm,
                    item["relation"],
                    float(item["credit"]),
                    item["status"],
                    now(),
                ),
            )
    elif filename == "scoring_template.default.json":
        if not isinstance(data, dict) or not isinstance(data["weights"], dict):
            raise ValueError("Invalid scoring template")
        _insert(
            db,
            "scoring_templates",
            {
                "name": data["name"],
                "version": data["version"],
                "weights_json": json.dumps(data["weights"]),
                "rules_json": json.dumps(
                    {k: v for k, v in data.items() if k not in {"name", "version", "weights"}}
                ),
                "status": "active",
                "created_at": now(),
            },
        )
    elif filename == "location_aliases.yaml":
        locations = Locations.model_validate(data)
        _insert(
            db,
            "settings",
            {
                "scope": "system",
                "key": "location.aliases",
                "admin_only": 1,
                "value_json": locations.model_dump_json(),
                "updated_at": now(),
            },
        )
    elif filename == "llm_models.approved.json":
        allowed = {row[1] for row in db.execute("PRAGMA table_info(llm_models)")}
        if not isinstance(data, list):
            raise ValueError("Models must be a list")
        for model in data:
            if not isinstance(model, dict) or not set(model) <= allowed:
                raise ValueError("Invalid model columns")
            _insert(db, "llm_models", model)


def _upgrade_related_synonyms(db: sqlite3.Connection, directory: Path) -> None:
    if db.execute(
        "SELECT 1 FROM settings WHERE scope='system' AND user_id IS NULL AND key=?",
        (RELATED_SEED_MARKER,),
    ).fetchone():
        return
    path = directory / "skill_synonyms.seed.csv"
    if not path.is_file():
        return
    db.execute("SAVEPOINT related_seed")
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = [row for row in csv.DictReader(stream) if row["relation"] == "related"]
        imported = db.execute("SELECT 1 FROM settings_packages LIMIT 1").fetchone()
        for item in rows:
            skill = db.execute(
                "SELECT * FROM skills WHERE name=?", (item["canonical"].strip(),)
            ).fetchone()
            if skill is None:
                logger.warning("Skipping related synonym upgrade: missing canonical skill")
                continue
            skill_id = skill["skill_id"]
            if db.execute(
                "SELECT 1 FROM skill_synonyms WHERE skill_id=? AND term_norm=?",
                (skill_id, normalize_term(item["term"])),
            ).fetchone():
                continue
            touched = db.execute(
                "SELECT 1 FROM audit_log WHERE "
                "(entity_type='skills' AND entity_id=?) OR "
                "(entity_type='skill_synonyms' AND ("
                "json_extract(details_json,'$.before.skill_id')=? OR "
                "json_extract(details_json,'$.after.skill_id')=? OR "
                "entity_id IN (SELECT CAST(synonym_id AS TEXT) FROM skill_synonyms "
                "WHERE skill_id=?))) LIMIT 1",
                (str(skill_id), skill_id, skill_id, skill_id),
            ).fetchone()
            owned = db.execute(
                "SELECT 1 FROM skill_synonyms WHERE skill_id=? AND created_by IS NOT NULL LIMIT 1",
                (skill_id,),
            ).fetchone()
            if imported or touched or owned or skill["created_by"] != "seed":
                logger.warning("Skipping related synonym upgrade for touched skill %s", skill_id)
                continue
            _seed(db, "skill_synonyms.seed.csv", [item])
        _insert(
            db,
            "settings",
            {
                "scope": "system",
                "key": RELATED_SEED_MARKER,
                "admin_only": 1,
                "value_json": "true",
                "updated_at": now(),
            },
        )
    except (ValueError, TypeError, KeyError, sqlite3.Error, csv.Error, OSError):
        db.execute("ROLLBACK TO related_seed")
        logger.warning("Skipping malformed related synonym upgrade")
    finally:
        db.execute("RELEASE related_seed")


def seed_reference_data(
    db: sqlite3.Connection,
    config_dir: Path | None = None,
    templates_dir: Path | None = None,
) -> None:
    """REQ-ELIG-02/03/07, REQ-GRD-01: seed empty reference stores without auditing."""
    directory = config_dir or Path(
        os.environ.get("PFC_CONFIG_DIR", str(Path(__file__).resolve().parents[4] / "config"))
    )
    stores = {
        "grades.yaml": "grades",
        "bench_statuses.yaml": "bench_statuses",
        "status_map.yaml": "status_map",
        "skill_synonyms.seed.csv": "skill_synonyms",
        "scoring_template.default.json": "scoring_templates",
        "location_aliases.yaml": "settings",
        "llm_models.approved.json": "llm_models",
    }
    for filename, table in stores.items():
        condition = (
            " WHERE scope='system' AND user_id IS NULL AND key='location.aliases'"
            if table == "settings"
            else ""
        )
        if db.execute(f"SELECT 1 FROM {table}{condition} LIMIT 1").fetchone():
            continue
        if table == "skill_synonyms" and (
            db.execute("SELECT 1 FROM skills LIMIT 1").fetchone()
            or db.execute("SELECT 1 FROM settings_packages LIMIT 1").fetchone()
            or db.execute(
                "SELECT 1 FROM audit_log WHERE entity_type IN ('skills','skill_synonyms') LIMIT 1"
            ).fetchone()
        ):
            continue
        path = directory / filename
        if not path.is_file():
            logger.info("Skipping missing seed file %s", filename)
            continue
        db.execute("SAVEPOINT seed_file")
        try:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                data = (
                    list(csv.DictReader(stream))
                    if filename.endswith(".csv")
                    else yaml.safe_load(stream)
                )
            _seed(db, filename, data)
        except (ValueError, TypeError, KeyError, sqlite3.Error, yaml.YAMLError, csv.Error, OSError):
            db.execute("ROLLBACK TO seed_file")
            logger.warning("Skipping malformed seed file %s", filename)
        finally:
            db.execute("RELEASE seed_file")
    _upgrade_related_synonyms(db, directory)
    if not (directory / "llm_models.approved.json").is_file():
        seed_default_models(db)
    email_path = (
        (templates_dir or directory.parent / "templates") / "email" / "candidate_default.html"
    )
    if db.execute("SELECT 1 FROM email_templates LIMIT 1").fetchone():
        return
    if not email_path.is_file():
        logger.info("Skipping missing email template candidate_default.html")
        return
    db.execute("SAVEPOINT seed_email")
    try:
        body = email_path.read_text(encoding="utf-8")
        subject = "Opportunity {requisition} – {role}"
        validate_placeholders(subject, body)
        _insert(
            db,
            "email_templates",
            {
                "name": "candidate_default",
                "version": 1,
                "subject": subject,
                "body_html": body,
                "is_default": 1,
                "created_at": now(),
            },
        )
    except (OSError, ValueError, sqlite3.Error, Problem):
        db.execute("ROLLBACK TO seed_email")
        logger.warning("Skipping malformed email template candidate_default.html")
    finally:
        db.execute("RELEASE seed_email")
