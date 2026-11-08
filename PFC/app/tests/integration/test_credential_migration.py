"""REQ-AUTH-06 (S4): legacy plaintext credential settings are encrypted once at startup."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.main import create_app
from pfc.security.credentials import CREDENTIAL_SETTING_KEYS, encrypt_legacy_credentials
from pfc.security.secrets_store import SecretStore, sensitive_key

PLAINTEXT = "LegacyPlaintextCredential-7f3a"


def file_bytes(settings: Settings) -> bytes:
    db = settings.data_dir / "db"
    return b"".join(p.read_bytes() for p in sorted(db.glob("pfc.db*")) if p.is_file())


def seeded(tmp_path: Path) -> Settings:
    settings = Settings(
        data_dir=tmp_path,
        bundled_models_dir=tmp_path / "bundled-models",
        frontend_dir=tmp_path,
        allowed_hosts=["testserver"],
    )
    migrate(settings)
    with database(settings) as db:
        db.execute(
            "INSERT INTO settings(scope,key,value_json,updated_at) "
            "VALUES ('system','smtp.password',?,'2026-11-06T00:00:00Z'),"
            "('system','ui.theme','\"dark\"','2026-11-06T00:00:00Z')",
            (json.dumps(PLAINTEXT),),
        )
    with database(settings) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert PLAINTEXT.encode() in file_bytes(settings)
    return settings


def audits(settings: Settings) -> list[str]:
    with database(settings) as db:
        return [
            row[0]
            for row in db.execute(
                "SELECT details_json FROM audit_log WHERE entity_id='smtp.password'"
            )
        ]


def test_registry_keys_are_the_ones_the_store_treats_as_secrets() -> None:
    assert CREDENTIAL_SETTING_KEYS and all(sensitive_key(key) for key in CREDENTIAL_SETTING_KEYS)


def test_plaintext_is_encrypted_audited_and_gone_from_the_file(tmp_path: Path) -> None:
    settings = seeded(tmp_path)
    assert encrypt_legacy_credentials(settings) == 1
    assert PLAINTEXT.encode() not in file_bytes(settings)
    with database(settings) as db:
        stored = json.loads(
            db.execute("SELECT value_json FROM settings WHERE key='smtp.password'").fetchone()[0]
        )
        theme = db.execute("SELECT value_json FROM settings WHERE key='ui.theme'").fetchone()[0]
    assert json.loads(SecretStore(settings.data_dir).decrypt(stored)) == PLAINTEXT
    assert theme == '"dark"'
    (detail,) = audits(settings)
    assert PLAINTEXT not in detail and "[encrypted]" in detail
    assert encrypt_legacy_credentials(settings) == 0
    assert len(audits(settings)) == 1


def test_startup_runs_the_migration_and_reads_stay_redacted(tmp_path: Path) -> None:
    settings = seeded(tmp_path)
    with TestClient(create_app(settings)):
        pass
    assert PLAINTEXT.encode() not in file_bytes(settings)
    assert len(audits(settings)) == 1
