import json
from pathlib import Path

from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.services.occ.seed import seed_reference_data


def fixture_files(directory: Path) -> None:
    directory.mkdir()
    files = {
        "grades.yaml": ["A1", "A2"],
        "bench_statuses.yaml": [
            {"code": "AVAILABLE", "label": "Available", "is_excluded": False, "priority": 1},
            {"code": "SOLD", "label": "Sold", "is_excluded": True, "priority": None},
        ],
        "status_map.yaml": {"Available": "AVAILABLE", "Sold": "SOLD", "Missing": "NOPE"},
        "scoring_template.default.json": {
            "name": "default",
            "version": 1,
            "weights": {"skills": 70},
            "bands": {"strong": 80},
        },
        "location_aliases.yaml": {"aliases": {" LDN ": " London "}, "travel_clusters": {}},
        "llm_models.approved.json": [
            {
                "name": "local",
                "file_name": "local.gguf",
                "file_sha256": "abc",
                "size_mb": 100,
                "ram_estimate_mb": 200,
                "context_tokens": 1024,
                "licence": "MIT",
                "approved": 1,
            }
        ],
    }
    for filename, data in files.items():
        (directory / filename).write_text(json.dumps(data))
    (directory / "skill_synonyms.seed.csv").write_text(
        "canonical,term,relation,credit,status\nPython,PY.,abbreviation,1,approved\n"
        "Python,py,abbreviation,1,approved\n"
    )


def test_seed_full_and_admin_edits(tmp_path: Path) -> None:
    """REQ-ELIG-02/03/07, REQ-GRD-01: seed once and preserve administrator edits."""
    settings = Settings(data_dir=tmp_path / "data")
    migrate(settings)
    config = tmp_path / "config"
    fixture_files(config)
    with database(settings) as db:
        seed_reference_data(db, config)
        for table, count in [
            ("grades", 2),
            ("bench_statuses", 2),
            ("status_map", 2),
            ("skills", 1),
            ("skill_synonyms", 1),
            ("scoring_templates", 1),
            ("settings", 1),
            ("llm_models", 1),
            ("audit_log", 0),
        ]:
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == count
        assert (
            db.execute("SELECT priority FROM status_map WHERE source_value='Sold'").fetchone()[0]
            == 2
        )
        assert db.execute("SELECT term_norm FROM skill_synonyms").fetchone()[0] == "py"
        db.execute("UPDATE grades SET active=0 WHERE code='A1'")
        db.execute("UPDATE bench_statuses SET label='Edited' WHERE code='AVAILABLE'")
        before = db.total_changes
        seed_reference_data(db, config)
        assert db.total_changes == before
        assert db.execute("SELECT active FROM grades WHERE code='A1'").fetchone()[0] == 0
        assert (
            db.execute("SELECT label FROM bench_statuses WHERE code='AVAILABLE'").fetchone()[0]
            == "Edited"
        )


def test_missing_and_malformed(tmp_path: Path, caplog) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    migrate(settings)
    config = tmp_path / "config"
    with database(settings) as db:
        seed_reference_data(db, config)
        assert db.execute("SELECT count(*) FROM grades").fetchone()[0] == 0
        fixture_files(config)
        (config / "grades.yaml").write_text("[broken")
        seed_reference_data(db, config)
        assert db.execute("SELECT count(*) FROM grades").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM bench_statuses").fetchone()[0] == 2
        assert "malformed seed" in caplog.text


def test_email_seed(tmp_path: Path) -> None:
    """REQ-EML-03: optional packaged email template is seeded once."""
    settings = Settings(data_dir=tmp_path / "data")
    migrate(settings)
    templates = tmp_path / "custom_templates"
    (templates / "email").mkdir(parents=True)
    path = templates / "email" / "candidate_default.html"
    path.write_text("<p>Hello {candidate_name}</p>")
    with database(settings) as db:
        seed_reference_data(db, tmp_path / "missing_config", templates)
        row = db.execute("SELECT * FROM email_templates").fetchone()
        assert row["is_default"] == 1 and row["version"] == 1
        assert row["body_html"] == "<p>Hello {candidate_name}</p>"
        path.write_text("<p>Changed</p>")
        seed_reference_data(db, tmp_path / "missing_config", templates)
        assert db.execute("SELECT count(*) FROM email_templates").fetchone()[0] == 1
        assert db.execute("SELECT body_html FROM email_templates").fetchone()[0] != "<p>Changed</p>"
