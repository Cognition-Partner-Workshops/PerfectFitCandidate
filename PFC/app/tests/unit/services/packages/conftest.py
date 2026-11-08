"""Isolated 0001 databases for signed package services (REQ-OCC-01..03)."""

from pathlib import Path

import pytest

from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.db.users import create_user
from pfc.security.package_signing import serialise_public_key
from pfc.services.packages.keys import SigningKeys

NOW = "2026-11-06T00:00:00Z"


def initialize(path: Path) -> Settings:
    settings = Settings(
        data_dir=path,
        bundled_models_dir=path / "bundled-models",
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )
    migrate(settings)
    with database(settings) as db:
        create_user(
            db,
            email="admin@example.com",
            display_name="Admin",
            role_id=1,
            password_hash="local-admin-hash",
            now=NOW,
            must_change_password=False,
        )
        create_user(
            db,
            email="analyst@example.com",
            display_name="Analyst",
            role_id=2,
            password_hash="local-analyst-hash",
            now=NOW,
            must_change_password=False,
        )
        db.execute(
            "INSERT INTO demands(code_requisition, id_format_ok, row_hash, created_at, "
            "updated_at) VALUES ('T10001', 1, 'demand-hash', ?, ?)",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO resources(short_name, short_name_key, email_key, eligibility, "
            "row_hash, created_at, updated_at) VALUES ('Person', 'person', 'person@example.com', "
            "'eligible', 'resource-hash', ?, ?)",
            (NOW, NOW),
        )
    return settings


def seed(settings: Settings) -> None:
    with database(settings) as db:
        db.execute("INSERT INTO skills VALUES (1, 'Python', 'Engineering', 'admin', 1)")
        db.execute(
            "INSERT INTO skill_synonyms VALUES (1, 1, 'py', 'py', 'abbreviation', 1, "
            "'approved', 1, ?)",
            (NOW,),
        )
        db.execute(
            "INSERT INTO scoring_templates VALUES (1, 'default', 1, NULL, '{}', '{}', "
            "'active', 1, ?, ?)",
            (NOW, NOW),
        )
        db.execute("INSERT INTO grades VALUES ('A3', 1, 1)")
        db.execute("INSERT INTO bench_statuses VALUES ('AVAILABLE', 'Available', 0, 1, 0, 0)")
        db.execute("INSERT INTO status_map VALUES ('available', 'AVAILABLE', 1, 0, 1, ?)", (NOW,))
        db.execute(
            "INSERT INTO resource_team_recipients VALUES (1, 'team@example.com', 'Team', 1, 1, ?)",
            (NOW,),
        )
        db.execute("INSERT INTO internal_email_domains VALUES ('example.com', 1, ?)", (NOW,))
        db.execute(
            "INSERT INTO email_templates VALUES (1, 'default', 1, 'Hello', '<p>Hi</p>', 1, 1, ?)",
            (NOW,),
        )
        db.execute(
            "INSERT INTO llm_models VALUES (1, 'approved', 'model.gguf', 'sha', 1, 2, "
            "8192, 'Apache-2.0', 1, NULL, 1)"
        )
        db.execute(
            "INSERT INTO llm_models VALUES (2, 'not-approved', 'other.gguf', 'sha2', 1, "
            "2, 8192, 'Apache-2.0', 0, NULL, 0)"
        )
        db.execute(
            "INSERT INTO llm_providers(provider_id, name, kind, fallback_order, base_url, "
            "token_blob, owner_user_id, updated_at) VALUES (1, 'local', 'llamacpp', 0, "
            "'http://127.0.0.1:8080', ?, 1, ?)",
            (b"provider-secret", NOW),
        )
        for key, value, admin in [
            ("dl.approvals", '["T10001"]', 0),
            ("ct.approvals", '["person"]', 0),
            ("admin.token", '"admin-secret"', 1),
            ("unrelated.secret", '"other-secret"', 0),
        ]:
            db.execute(
                "INSERT INTO settings(scope, key, value_json, admin_only, updated_at) "
                "VALUES ('system', ?, ?, ?, ?)",
                (key, value, admin, NOW),
            )
        db.execute("UPDATE demands SET occ_reviewed_at=?, occ_reviewed_by=1", (NOW,))
        db.execute("UPDATE resources SET occ_reviewed_at=?, occ_reviewed_by=1", (NOW,))


def trust(source: Settings, target: Settings) -> None:
    keys = SigningKeys(source, use_dpapi=False)
    keys.get_or_create("test signing passphrase")
    directory = target.data_dir / "keys" / "trusted"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "admin.pub").write_bytes(serialise_public_key(keys.get_public_key()))


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Settings, Settings]:
    source = initialize(tmp_path / "admin")
    target = initialize(tmp_path / "analyst")
    seed(source)
    trust(source, target)
    return source, target
