import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from pfc.config import Settings
from pfc.db.connection import database


def migrate(settings: Settings, migrations_dir: Path | None = None) -> list[str]:
    """Apply forward-only migrations; restore an online backup on failure."""
    directory = migrations_dir if migrations_dir is not None else files("pfc.db.migrations")
    migrations = sorted(
        (entry for entry in directory.iterdir() if entry.name.endswith(".sql")),
        key=lambda entry: entry.name,
    )
    applied: list[str] = []
    with database(settings) as connection:
        nonempty = bool(
            connection.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone()
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.commit()
        known = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        for migration in migrations:
            version = migration.name.split("_", 1)[0]
            if version in known:
                continue
            backup_path = None
            if nonempty:
                backup_dir = settings.data_dir / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                backup_path = backup_dir / f"pfc-{stamp}-pre-{version}.db"
                with closing(sqlite3.connect(backup_path)) as backup:
                    connection.backup(backup)
            try:
                connection.execute("BEGIN IMMEDIATE")
                statement = ""
                for line in migration.read_text(encoding="utf-8").splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ""
                if statement.strip() and not all(
                    line.strip().startswith("--") or not line.strip()
                    for line in statement.splitlines()
                ):
                    raise ValueError(f"Incomplete SQL in {migration.name}")
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)",
                    (version, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                if backup_path is not None:
                    with closing(sqlite3.connect(backup_path)) as backup:
                        backup.backup(connection)
                raise
            applied.append(version)
            nonempty = True
    return applied
