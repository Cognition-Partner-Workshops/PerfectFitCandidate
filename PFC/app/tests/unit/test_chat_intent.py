import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.services.chat_intent import Lookup


@pytest.mark.parametrize(
    "value",
    [
        {"intent": "free_sql"},
        {"intent": "bench_skill"},
        {"intent": "bench_skill", "skill": "SQL", "grade_to": "C1"},
        {"intent": "demand_count", "scope_term": "INS"},
        {"intent": "demand_count", "requisition": "T10001"},
        {"intent": "match_rank", "requisition": "T10001", "rank": "2"},
        {"intent": "match_rank", "requisition": "T10001", "rank": 0},
    ],
)
def test_lookup_schema_rejects_unregistered_or_invalid_fields(value: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Lookup.model_validate(value)


def test_match_lookup_cannot_drop_masked_resource_or_add_another_requisition() -> None:
    question = "Why is Resource #42 ranked #2 for T10001?"
    base = {"intent": "match_rank", "requisition": "T10001", "rank": 2}
    assert Lookup.model_validate({**base, "resource_id": 42}).grounded_in(question)
    assert not Lookup.model_validate(base).grounded_in(question)
    assert not Lookup.model_validate(
        {**base, "resource_id": 42, "requisition": "T10002"}
    ).grounded_in(question)


def test_query_requisition_literals_must_come_from_question() -> None:
    lookup = Lookup(
        intent="query",
        sql="SELECT rank FROM chat_v_match_results WHERE requisition='T12345'",
        explanation="Read the stored rank.",
    )
    assert lookup.grounded_in("What are the stored ranks for t12345?")
    assert not lookup.grounded_in("What are the stored ranks for T54321?")
    assert not lookup.grounded_in("What are the stored ranks?")


def test_0017_upgrades_existing_database_and_leaves_old_jobs_intact(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    phase1 = tmp_path / "phase1"
    phase1.mkdir()
    for path in Path("pfc/db/migrations").glob("*.sql"):
        if path.name < "0013":
            shutil.copyfile(path, phase1 / path.name)
    assert migrate(settings, phase1)[-1] == "0012"
    with database(settings) as db:
        db.execute(
            "INSERT INTO jobs(job_id,kind,status,created_at) VALUES (120,'embed','completed','now')"
        )
    assert "0017" in migrate(settings)
    assert migrate(settings) == []
    with database(settings) as db:
        assert db.execute("SELECT count(*) FROM chat_questions").fetchone()[0] == 0
        assert db.execute("SELECT kind FROM jobs WHERE job_id=120").fetchone()[0] == "embed"
