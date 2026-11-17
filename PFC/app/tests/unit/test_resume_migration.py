import shutil
import sqlite3
from pathlib import Path

import pytest

from pfc.config import Settings
from pfc.cv_formats import CV_MIME_TYPES
from pfc.db.connection import database
from pfc.db.migrate import migrate


def test_resume_mime_upgrade_preserves_children_and_schema(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    old = tmp_path / "migrations"
    old.mkdir()
    for path in Path("pfc/db/migrations").glob("*.sql"):
        if path.name < "0012":
            shutil.copyfile(path, old / path.name)
    assert migrate(settings, old)[-1] == "0011"
    with database(settings) as db:
        db.execute("CREATE INDEX ix_resume_name ON resumes(file_name)")
        db.execute("CREATE TABLE resume_events(id INTEGER)")
        db.execute(
            "CREATE TRIGGER resume_insert AFTER INSERT ON resumes BEGIN "
            "INSERT INTO resume_events VALUES(new.resume_id); END"
        )
        db.execute(
            "INSERT INTO candidates(candidate_id,internal_ref,identity_status,"
            "created_at,updated_at) "
            "VALUES (41,'PFC-C-41','complete','now','now')"
        )
        db.execute(
            "INSERT INTO resumes VALUES "
            "(51,41,'old.pdf','old.pdf','hash','now','application/pdf','now')"
        )
        db.execute(
            "INSERT INTO cv_versions(cv_version_id,resume_id,candidate_id,version_no,"
            "text_sha256,text_norm,extracted_by,is_latest,extracted_at) "
            "VALUES (61,51,41,1,'text-hash','CV','rule_based',1,'now')"
        )
        before = tuple(db.execute("SELECT * FROM resumes").fetchone())
        children = [tuple(r) for r in db.execute("SELECT * FROM cv_versions")]
        schema = [
            tuple(r)
            for r in db.execute(
                "SELECT type,name,sql FROM sqlite_schema WHERE tbl_name='resumes' "
                "AND type IN ('index','trigger') ORDER BY name"
            )
        ]
    assert migrate(settings) == ["0012"]
    with database(settings) as db:
        assert tuple(db.execute("SELECT * FROM resumes").fetchone()) == before
        assert [tuple(r) for r in db.execute("SELECT * FROM cv_versions")] == children
        assert [
            tuple(r)
            for r in db.execute(
                "SELECT type,name,sql FROM sqlite_schema WHERE tbl_name='resumes' "
                "AND type IN ('index','trigger') ORDER BY name"
            )
        ] == schema
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        for extension, mime in CV_MIME_TYPES.items():
            db.execute(
                "INSERT INTO resumes(candidate_id,source_path,file_name,file_sha256,"
                "mime_type,discovered_at) VALUES (41,?,?,?,?, 'now')",
                (extension, extension, extension, mime),
            )
        assert db.execute("SELECT count(*) FROM resume_events").fetchone()[0] == 9
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO resumes(source_path,file_name,file_sha256,mime_type,"
                "discovered_at) VALUES ('bad','bad','bad','invalid','now')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM resumes WHERE resume_id=51")
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_rebuild_failure_rolls_back_and_restores_foreign_keys(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    migrate(settings)
    directory = tmp_path / "bad"
    directory.mkdir()
    (directory / "9999_bad.sql").write_text(
        "-- rebuild-table: resumes\nDROP TABLE resumes;\nINSERT INTO absent VALUES(1);\n"
    )
    with pytest.raises(sqlite3.OperationalError):
        migrate(settings, directory)
    with database(settings) as db:
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM resumes").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
