"""Incremental carry-forward runs with the writer reserved, so no SQLITE_BUSY_SNAPSHOT (517)."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "integration"))

from test_matching_service import execute, seed_demand, seed_resource  # noqa: E402
from test_matching_service import runner as runner  # noqa: E402
from test_matching_service import settings as settings  # noqa: E402

from pfc.config import Settings  # noqa: E402
from pfc.db.connection import database  # noqa: E402
from pfc.jobs.runner import JobRunner  # noqa: E402
from pfc.services.matching import runs  # noqa: E402
from pfc.services.matching.incremental import Plan  # noqa: E402


def test_carry_forward_starts_with_the_write_lock_held(
    settings: Settings, runner: JobRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    with database(settings) as db:
        seed_demand(db)
        seed_resource(db)
    execute(settings, runner)
    probes: list[str] = []
    original = runs.carry_forward

    def probe(db: sqlite3.Connection, run_id: int, plan: Plan) -> None:
        other = sqlite3.connect(settings.data_dir / "db" / "pfc.db", timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
            probes.append("writer free")
        except sqlite3.OperationalError as exc:
            probes.append(str(exc))
        finally:
            other.close()
        original(db, run_id, plan)

    monkeypatch.setattr(runs, "carry_forward", probe)
    run_id = execute(settings, runner, incremental=True)
    assert probes == ["database is locked"]
    with database(settings) as db:
        mode = db.execute(
            "SELECT json_extract(scope_json,'$.mode') FROM match_runs WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    assert mode == "incremental"
