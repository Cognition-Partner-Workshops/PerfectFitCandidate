import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pfc.config import Settings
from pfc.db import audit, sessions, users
from pfc.db.connection import connect, database
from pfc.db.migrate import migrate

NOW = "2026-01-01T00:00:00Z"
LATER = "2026-01-01T01:00:00Z"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path)


def test_schema_and_pragmas(settings: Settings) -> None:
    assert migrate(settings) == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008"]
    assert migrate(settings) == []
    assert len(list((settings.data_dir / "backups").glob("*-pre-0002.db"))) == 1
    sql = Path("pfc/db/migrations/0001_initial.sql").read_text()
    expected = set(re.findall(r"CREATE (?:VIRTUAL )?TABLE (\w+)", sql))
    with database(settings) as db:
        actual = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert expected <= actual
        assert "schema_migrations" in actual
        assert "summaries" in actual
        assert [row[1] for row in db.execute("PRAGMA table_info(summaries)")] == [
            "summary_id",
            "owner_kind",
            "owner_id",
            "source_version_id",
            "source_sha256",
            "model",
            "template_id",
            "template_version",
            "sentences_json",
            "dropped_ungrounded",
            "created_by",
            "created_at",
        ]
        assert "ix_summaries_latest" in {
            row[1] for row in db.execute("PRAGMA index_list(summaries)")
        }
        assert "vec_chunks" not in actual
        assert [row[0] for row in db.execute("SELECT name FROM roles ORDER BY role_id")] == [
            "Admin",
            "Analyst",
        ]
        for pragma, value in [
            ("journal_mode", "wal"),
            ("synchronous", 1),
            ("foreign_keys", 1),
            ("busy_timeout", 5000),
        ]:
            assert db.execute(f"PRAGMA {pragma}").fetchone()[0] == value
        db.execute(
            "INSERT INTO embedding_models VALUES (1, 'test', 'onnx', 'hash', 384, 1, 'v1', 1)"
        )
        db.execute(
            "INSERT INTO embedding_chunks VALUES (1, 'cv', 1, 'skills', 0, "
            "'python', 'hash', 1, 1, ?)",
            (NOW,),
        )
        assert (
            db.execute("SELECT rowid FROM fts_chunks WHERE fts_chunks MATCH 'python'").fetchone()[0]
            == 1
        )
        db.execute("UPDATE embedding_chunks SET text='rust' WHERE chunk_id=1")
        assert (
            db.execute("SELECT rowid FROM fts_chunks WHERE fts_chunks MATCH 'python'").fetchall()
            == []
        )
        assert (
            db.execute("SELECT rowid FROM fts_chunks WHERE fts_chunks MATCH 'rust'").fetchone()[0]
            == 1
        )
        db.execute("DELETE FROM embedding_chunks WHERE chunk_id=1")
        assert (
            db.execute("SELECT rowid FROM fts_chunks WHERE fts_chunks MATCH 'rust'").fetchall()
            == []
        )


def test_repositories(settings: Settings) -> None:
    migrate(settings)
    with database(settings) as db:
        assert users.count_admins(db) == 0
        assert users.get_user_by_email(db, "missing") is None
        assert users.get_user_by_id(db, 99) is None
        assert sessions.get_session_by_token_hash(db, "missing") is None
        assert sessions.purge_expired_sessions(db, NOW) == 0
        user = users.create_user(
            db,
            email="Admin@example.com",
            display_name="Admin",
            role_id=1,
            password_hash="hash",
            now=NOW,
        )
        assert users.get_user_by_email(db, "admin@EXAMPLE.com") == user
        assert users.count_admins(db) == 1
        users.update_login_state(db, user.user_id, failed_attempts=5, locked_until=LATER, now=NOW)
        users.update_password(
            db, user.user_id, password_hash="new", must_change_password=False, now=NOW
        )
        updated = users.get_user_by_id(db, user.user_id)
        assert updated is not None
        assert (
            updated.failed_attempts,
            updated.locked_until,
            updated.password_hash,
            updated.must_change_password,
        ) == (5, LATER, "new", 0)
        with pytest.raises(sqlite3.IntegrityError):
            users.create_user(
                db,
                email=user.email,
                display_name="Duplicate",
                role_id=1,
                password_hash="hash",
                now=NOW,
            )
        session = sessions.create_session(
            db,
            token_hash="token",
            csrf_hash="csrf",
            user_id=user.user_id,
            role_at_login="Admin",
            now=NOW,
            expires_at=LATER,
        )
        sessions.touch_session(db, session.session_id, LATER, LATER)
        assert sessions.get_session_by_token_hash(db, "token").last_seen_at == LATER
        sessions.revoke_session(db, session.session_id, NOW)
        assert sessions.get_session_by_token_hash(db, "token").revoked_at == NOW
        assert sessions.purge_expired_sessions(db, LATER) == 1


def test_audit(settings: Settings) -> None:
    migrate(settings)
    with database(settings) as db:
        assert audit.verify_chain(db)
        audit.append_audit(db, ts=NOW, action="LOGIN_FAIL", details={"reason": "invalid"})
        audit.append_audit(db, ts=NOW, action="LOGIN_OK")
        assert audit.verify_chain(db)
        for statement in ["UPDATE audit_log SET action='bad'", "DELETE FROM audit_log"]:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(statement)
        db.execute("DROP TRIGGER audit_log_no_update")
        db.execute("UPDATE audit_log SET action='bad' WHERE audit_id=1")
        assert not audit.verify_chain(db)


def test_vec_fallback(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(_: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("no extension")

    monkeypatch.setattr("pfc.db.connection.sqlite_vec.load", fail)
    with database(settings) as db:
        assert not db.vec_available
        assert db.execute("SELECT 1").fetchone()[0] == 1
    assert "sqlite-vec unavailable" in caplog.text


def test_extension_api_unavailable(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_: object) -> None:
        raise AttributeError("unavailable")

    monkeypatch.setattr("pfc.db.connection.Connection.enable_load_extension", fail)
    connection = connect(settings)
    assert not connection.vec_available
    connection.close()


def test_classification_migration_preserves_existing_override(
    settings: Settings,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "old_migrations"
    directory.mkdir()
    for name in ("0001_initial.sql", "0002_occ_approvals.sql", "0003_jd_item_ancestry.sql"):
        (directory / name).write_text((Path("pfc/db/migrations") / name).read_text())
    assert migrate(settings, directory) == ["0001", "0002", "0003"]
    with database(settings) as db:
        db.execute(
            "INSERT INTO candidates(internal_ref,identity_status,classification,"
            "classification_override,created_at,updated_at) VALUES "
            "('PFC-C-1','complete','External','Internal',?,?)",
            (NOW, NOW),
        )
    assert migrate(settings) == ["0004", "0005", "0006", "0007", "0008"]
    with database(settings) as db:
        row = db.execute("SELECT * FROM candidates").fetchone()
        assert row["classification"] == "External"
        assert row["classification_override"] == "Internal"
        assert row["override_reason"] is None
        assert row["override_at"] is None


def test_backup_restore(settings: Settings, tmp_path: Path) -> None:
    migrate(settings)
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0009_bad.sql").write_text(
        "CREATE TABLE should_rollback(id INTEGER);\nINSERT INTO missing VALUES(1);\n"
    )
    with pytest.raises(sqlite3.OperationalError):
        migrate(settings, directory)
    backups = list((settings.data_dir / "backups").glob("pfc-*-pre-0009.db"))
    assert len(backups) == 1
    with database(settings) as db:
        assert db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 8
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='should_rollback'").fetchone()
            is None
        )
    (directory / "0009_bad.sql").write_text("CREATE TABLE success(id INTEGER);\n")
    assert migrate(settings, directory) == ["0009"]
    assert migrate(settings, directory) == []


def test_item_ancestry_upgrade(settings: Settings, tmp_path: Path) -> None:
    directory = tmp_path / "old_migrations"
    directory.mkdir()
    for name in ("0001_initial.sql", "0002_occ_approvals.sql"):
        (directory / name).write_text((Path("pfc/db/migrations") / name).read_text())
    assert migrate(settings, directory) == ["0001", "0002"]
    with database(settings) as db:
        db.execute(
            "INSERT INTO demands(demand_id,code_requisition,id_format_ok,row_hash,created_at,"
            "updated_at) VALUES (1,'T1TEST',1,'hash',?,?)",
            (NOW, NOW),
        )
        db.execute(
            "INSERT INTO demand_versions(demand_version_id,demand_id,version_no,jd_text_raw,"
            "jd_text_norm,jd_sha256,created_at) VALUES (1,1,1,'SQL','SQL','hash',?)",
            (NOW,),
        )
        db.execute(
            "INSERT INTO jd_requirements(jd_req_id,demand_version_id,revision,status,created_at) "
            "VALUES (1,1,1,'draft',?)",
            (NOW,),
        )
        db.execute(
            "INSERT INTO jd_requirement_items(item_id,jd_req_id,kind,text,position) "
            "VALUES (1,1,'mandatory_skill','SQL',0)"
        )
    assert migrate(settings) == ["0003", "0004", "0005", "0006", "0007", "0008"]
    assert migrate(settings) == []
    with database(settings) as db:
        row = db.execute("SELECT text,source_jd_req_id FROM jd_requirement_items").fetchone()
        assert tuple(row) == ("SQL", None)
        db.execute(
            "INSERT INTO jd_requirement_items(jd_req_id,kind,text,position,source_jd_req_id) "
            "VALUES (1,'mandatory_skill','Edited SQL',1,1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE jd_requirement_items SET source_jd_req_id=999 WHERE item_id=2")
        db.execute("DELETE FROM jd_requirement_items WHERE item_id=1")
        assert (
            db.execute(
                "SELECT source_jd_req_id FROM jd_requirement_items WHERE item_id=2"
            ).fetchone()[0]
            is None
        )


def test_synonym_upgrade_after_published_jd_migration(settings: Settings, tmp_path: Path) -> None:
    directory = tmp_path / "main_migrations"
    directory.mkdir()
    for name in ("0001_initial.sql", "0002_occ_approvals.sql", "0003_jd_item_ancestry.sql"):
        (directory / name).write_text((Path("pfc/db/migrations") / name).read_text())
    assert migrate(settings, directory) == ["0001", "0002", "0003"]
    with database(settings) as db:
        db.execute("INSERT INTO skills(skill_id,name,created_by) VALUES (7,'Python','seed')")
        db.execute("INSERT INTO skills(skill_id,name,created_by) VALUES (8,'SQL','seed')")
        db.execute(
            "INSERT INTO skill_synonyms VALUES "
            "(42,7,'Data','data','related',0.6,'approved',NULL,?)",
            (NOW,),
        )
    assert migrate(settings) == ["0004", "0005", "0006", "0007", "0008"]
    assert migrate(settings) == []
    with database(settings) as db:
        assert tuple(db.execute("SELECT * FROM skill_synonyms").fetchone()) == (
            42,
            7,
            "Data",
            "data",
            "related",
            0.6,
            "approved",
            None,
            NOW,
        )
        db.execute(
            "INSERT INTO skill_synonyms VALUES "
            "(43,8,'Data','data','related',0.6,'approved',NULL,?)",
            (NOW,),
        )
        assert "source_jd_req_id" in {
            row["name"] for row in db.execute("PRAGMA table_info(jd_requirement_items)")
        }


def test_jobs_use_separate_connections(settings: Settings) -> None:
    migrate(settings)

    def write(index: int) -> None:
        with database(settings) as db:
            audit.append_audit(db, ts=NOW, action=str(index))

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(write, range(12)))
    with database(settings) as db:
        assert db.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 12
        assert audit.verify_chain(db)
