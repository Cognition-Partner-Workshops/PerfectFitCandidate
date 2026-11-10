"""REQ-MAT-05: portable related edges, legacy upgrades and first-run parity."""

import csv
import logging
import shutil
from datetime import date
from pathlib import Path

import pytest
from test_matching_api import Api
from test_matching_api import api as api
from test_occ_domain_config import edit

from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.domain.config_loader import load_domain_config
from pfc.domain.normalise import parse_date
from pfc.domain.scoring import (
    Demand,
    RequirementItem,
    Resource,
    SemanticInput,
    build_ct_basis,
    score_basis,
)
from pfc.services.occ import synonyms
from pfc.services.occ.domain import occ_domain_config
from pfc.services.occ.seed import RELATED_SEED_MARKER, normalize_term, seed_reference_data

STAMP = "2026-11-06T00:00:00Z"
CONFIG = Path(__file__).resolve().parents[3] / "config"


def old_database(tmp_path: Path) -> Settings:
    directory = tmp_path / "old_migrations"
    directory.mkdir()
    for name in ("0001_initial.sql", "0002_occ_approvals.sql"):
        shutil.copyfile(Path("pfc/db/migrations") / name, directory / name)
    settings = Settings(data_dir=tmp_path / "data")
    assert migrate(settings, directory) == ["0001", "0002"]
    with database(settings) as db:
        db.execute(
            "INSERT INTO users(user_id,email,display_name,role_id,password_hash,"
            "created_at,updated_at) "
            "VALUES (1,'admin@example.com','Admin',1,'unused',?,?)",
            (STAMP, STAMP),
        )
        with (CONFIG / "skill_synonyms.seed.csv").open() as stream:
            for row in csv.DictReader(stream):
                db.execute(
                    "INSERT OR IGNORE INTO skills(name,created_by) VALUES (?,'seed')",
                    (row["canonical"],),
                )
                db.execute(
                    "INSERT OR IGNORE INTO skill_synonyms(skill_id,term,term_norm,relation,credit,"
                    "status,updated_at) SELECT skill_id,?,?,?,?,?,? FROM skills WHERE name=?",
                    (
                        row["term"],
                        normalize_term(row["term"]),
                        row["relation"],
                        float(row["credit"]),
                        row["status"],
                        STAMP,
                        row["canonical"],
                    ),
                )
        assert db.execute("SELECT count(*) FROM skill_synonyms").fetchone()[0] == 27
    return settings


def test_seeded_table_exactly_matches_file_and_related_score(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    migrate(settings)
    original = load_domain_config().synonyms
    with database(settings) as db:
        seed_reference_data(db)
        config = occ_domain_config(db)
        assert db.execute("SELECT count(*) FROM skill_synonyms").fetchone()[0] == 31
        current = config.synonyms
        for row in original.rows:
            for term in (row.term, row.canonical):
                assert current.resolve(term) == original.resolve(term)
                assert current.expand(term) == original.expand(term)
                assert sorted(current.related(term)) == sorted(original.related(term))
        assert {
            (r.canonical, normalize_term(r.term), r.relation, r.credit, r.status)
            for r in current.rows
        } == {
            (r.canonical, normalize_term(r.term), r.relation, r.credit, r.status)
            for r in original.rows
        }
        assert sorted(current.related("Delivery Lead")) == [
            ("Program Manager", 0.6),
            ("Project Management", 0.6),
        ]
        resource = Resource(
            "r1",
            "Person",
            "C1",
            "Manchester",
            "Available",
            "RG1",
            parse_date("2026-10-01"),
            parse_date(None),
            "Project Manager,SQL",
        )
        demand = Demand(
            "D1",
            parse_date("2026-11-16"),
            "C1",
            "Manchester",
            False,
            (RequirementItem("M1", "Delivery Lead", "mandatory"),),
        )
        result = score_basis(
            demand,
            resource,
            build_ct_basis(resource, config),
            SemanticInput(0),
            config,
            date(2026, 11, 6),
        )
        assert (result.score, result.band, result.items[0].credit) == (65.7, "Avg", 0.6)


@pytest.mark.parametrize("touch", ["none", "owner", "delete", "move", "package"])
def test_old_unique_schema_upgrade_preserves_data_and_admin_changes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, touch: str
) -> None:
    settings = old_database(tmp_path)
    with database(settings) as db:
        target = db.execute(
            "SELECT synonym_id FROM skill_synonyms JOIN skills USING(skill_id) "
            "WHERE name='Delivery Lead'"
        ).fetchone()[0]
        if touch == "owner":
            db.execute("UPDATE skill_synonyms SET created_by=1 WHERE synonym_id=?", (target,))
        elif touch == "delete":
            synonyms.delete(db, 1, target)
        elif touch == "move":
            synonyms.save(
                db,
                1,
                {
                    "canonical": "Python",
                    "term": "delivery-role",
                    "relation": "equivalent",
                    "credit": 1,
                    "status": "approved",
                },
                target,
            )
        elif touch == "package":
            db.execute(
                "INSERT INTO settings_packages(package_version,schema_version,app_version,"
                "package_sha256,signing_key_id,imported_by,imported_at) "
                "VALUES (1,'1','0.1.0','fixture','fixture',1,?)",
                (STAMP,),
            )
        before = [
            tuple(row) for row in db.execute("SELECT * FROM skill_synonyms ORDER BY synonym_id")
        ]
    assert migrate(settings) == ["0003", "0004", "0005", "0006", "0007", "0008", "0009"]
    assert migrate(settings) == []
    with database(settings) as db:
        assert [
            tuple(row) for row in db.execute("SELECT * FROM skill_synonyms ORDER BY synonym_id")
        ] == before
        with caplog.at_level(logging.WARNING):
            seed_reference_data(db)
        expected = 4 if touch == "none" else (0 if touch == "package" else 2)
        assert (
            db.execute("SELECT count(*) FROM skill_synonyms WHERE relation='related'").fetchone()[0]
            == expected
        )
        assert db.execute("SELECT 1 FROM settings WHERE key=?", (RELATED_SEED_MARKER,)).fetchone()
        if touch != "none":
            assert "Skipping related synonym upgrade for touched skill" in caplog.text
            assert not db.execute(
                "SELECT 1 FROM skill_synonyms JOIN skills USING(skill_id) "
                "WHERE name='Delivery Lead' AND relation='related'"
            ).fetchone()
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        db.execute("DELETE FROM skill_synonyms WHERE relation='related'")
        changes = db.total_changes
        seed_reference_data(db)
        assert db.total_changes == changes


def test_deleted_entire_store_is_not_first_run(tmp_path: Path, caplog) -> None:
    settings = old_database(tmp_path)
    with database(settings) as db:
        for row in db.execute("SELECT synonym_id FROM skill_synonyms").fetchall():
            synonyms.delete(db, 1, row[0])
    migrate(settings)
    with database(settings) as db:
        seed_reference_data(db)
        assert db.execute("SELECT count(*) FROM skill_synonyms").fetchone()[0] == 0
        assert "touched skill" in caplog.text


def test_empty_legacy_import_is_not_first_run(tmp_path: Path, caplog) -> None:
    settings = old_database(tmp_path)
    with database(settings) as db:
        db.execute("DELETE FROM skill_synonyms")
        db.execute("DELETE FROM skills")
        db.execute(
            "INSERT INTO settings_packages(package_version,schema_version,app_version,"
            "package_sha256,signing_key_id,imported_by,imported_at) "
            "VALUES (1,'1','0.1.0','fixture','fixture',1,?)",
            (STAMP,),
        )
    migrate(settings)
    with database(settings) as db:
        seed_reference_data(db)
        assert db.execute("SELECT count(*) FROM skills").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM skill_synonyms").fetchone()[0] == 0
        assert "missing canonical skill" in caplog.text


def test_upgrade_error_does_not_mark_completion(tmp_path: Path, caplog) -> None:
    settings = old_database(tmp_path)
    migrate(settings)
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "skill_synonyms.seed.csv").write_text(
        "canonical,term,relation,credit,status\nDelivery Lead,Project Manager,related,2,approved\n"
    )
    with database(settings) as db:
        seed_reference_data(db, directory)
        assert not db.execute(
            "SELECT 1 FROM settings WHERE key=?", (RELATED_SEED_MARKER,)
        ).fetchone()
        assert db.execute("SELECT count(*) FROM skill_synonyms").fetchone()[0] == 27
        assert "malformed related synonym upgrade" in caplog.text
        seed_reference_data(db)
        assert db.execute("SELECT count(*) FROM skill_synonyms").fetchone()[0] == 31


@pytest.mark.parametrize("api", ["Admin"], indirect=True)
def test_occ_multiple_related_targets_and_alias_exclusivity(api: Api) -> None:
    assert RELATED_SEED_MARKER not in api[0].get("/api/v1/occ/settings").json()
    assert (
        api[0]
        .put("/api/v1/occ/settings", headers=api[1], json={RELATED_SEED_MARKER: False})
        .status_code
        == 422
    )
    for canonical in ("Delivery Lead", "Program Manager"):
        edit(
            api,
            "synonyms",
            {
                "canonical": canonical,
                "term": "cross-role",
                "relation": "related",
                "credit": 0.6,
                "status": "approved",
            },
            "POST",
        )
    edit(
        api,
        "synonyms",
        {
            "canonical": "Delivery Lead",
            "term": "one-alias",
            "relation": "equivalent",
            "credit": 1,
            "status": "approved",
        },
        "POST",
    )
    for canonical, term in (("Program Manager", "one-alias"), ("Python", "SQL")):
        response = api[0].post(
            "/api/v1/occ/synonyms",
            headers=api[1],
            json={
                "canonical": canonical,
                "term": term,
                "relation": "equivalent",
                "credit": 1,
                "status": "approved",
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"
