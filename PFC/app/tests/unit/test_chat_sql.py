import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.services import chat_sql
from pfc.services.chat_intent import SYSTEM_PROMPT, Lookup
from pfc.services.chat_sql import QueryRejected, execute_query


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    settings = Settings(data_dir=tmp_path)
    assert "0019" in migrate(settings)
    with database(settings) as conn:
        yield conn


def query(sql: str) -> Lookup:
    return Lookup(intent="query", sql=sql, explanation="This lists stored records.")


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO resources DEFAULT VALUES",
        "UPDATE resources SET grade=NULL",
        "DELETE FROM resources",
        "PRAGMA database_list",
        "ATTACH ':memory:' AS x",
        "SELECT * FROM resources",
        "SELECT * FROM candidates",
        "SELECT * FROM sqlite_master",
        "SELECT 1; SELECT 2",
        "SELECT load_extension('x')",
        "CREATE TEMP TABLE x(a)",
        "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x) SELECT * FROM x",
        "WITH x AS (SELECT 1) DELETE FROM resources",
        "SELECT randomblob(1000000000)",
        "SELECT * FROM pragma_table_info('resources')",
        "SELECT chat_resource_skills(1)",
        "SELECT count(*) FROM json_each('[1]')",
        "SELECT value FROM json_each('[1]'), chat_v_resource_skills",
        "SELECT count(*) FROM resources, chat_v_resource_skills",
        "SELECT skills_raw FROM resources, chat_v_resource_skills",
    ],
)
def test_denied(db: sqlite3.Connection, sql: str) -> None:
    with pytest.raises(QueryRejected):
        execute_query(db, query(sql))
    assert db.execute("SELECT count(*) FROM resources").fetchone()[0] == 0


def test_views_empty_and_statement_quoting(db: sqlite3.Connection) -> None:
    for name in ("demands", "demand_skills", "resources", "resource_skills", "match_results"):
        answer = execute_query(db, query("SELECT * FROM chat_v_" + name))
        assert answer.outcome == "no_data"
        assert answer.query and answer.query.rows == []
    answer = execute_query(db, query("/* hello */ SELECT ';' AS value; -- tail"))
    assert answer.query and answer.query.rows == [[";"]]


@pytest.mark.parametrize("populated", [False, True])
def test_unfiltered_resource_skill_count(db: sqlite3.Connection, populated: bool) -> None:
    if populated:
        db.execute("INSERT INTO skills(name,created_by) VALUES ('SQL','system')")
        db.execute(
            "INSERT INTO resources(short_name,short_name_key,skills_raw,eligibility,row_hash,"
            "created_at,updated_at) "
            "VALUES ('SYNTHETIC','synthetic','SQL','eligible','x','now','now')"
        )
    answer = execute_query(db, query("SELECT count(*) FROM chat_v_resource_skills"))
    assert answer.query and answer.query.rows == [[int(populated)]]


@pytest.mark.parametrize("virtual_schema", [None, "temp"])
def test_zero_column_json_read_schema(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, virtual_schema: str | None
) -> None:
    install = db.set_authorizer

    def set_authorizer(callback: Callable[..., int] | None) -> None:
        if callback is None:
            install(None)
            return

        def authorize(
            action: int,
            table: str | None,
            column: str | None,
            schema: str | None,
            source: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_READ and table == "json_each" and column == "":
                schema = virtual_schema
            return callback(action, table, column, schema, source)

        install(authorize)

    monkeypatch.setattr(db, "set_authorizer", set_authorizer)
    if virtual_schema == "temp":
        with pytest.raises(QueryRejected):
            execute_query(db, query("SELECT count(*) FROM chat_v_resource_skills"))
    else:
        answer = execute_query(db, query("SELECT count(*) FROM chat_v_resource_skills"))
        assert answer.query and answer.query.rows == [[0]]


@pytest.mark.parametrize("preparation", ["python", "sqlite"])
def test_skill_projection_uses_query_deadline(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, preparation: str
) -> None:
    register = chat_sql.register_chat_skills

    def slow_projection(db: sqlite3.Connection, *, snapshot: bool = False) -> None:
        if snapshot:
            if preparation == "python":
                time.sleep(0.03)
            else:
                values = json.dumps([0] * 1000)
                db.execute(
                    "SELECT count(*) FROM json_each(?) a CROSS JOIN json_each(?) b "
                    "CROSS JOIN json_each(?) c",
                    (values, values, values),
                ).fetchone()
        register(db, snapshot=snapshot)

    monkeypatch.setattr(chat_sql, "TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(chat_sql, "register_chat_skills", slow_projection)
    previous = db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    with pytest.raises(QueryRejected, match="interrupted"):
        execute_query(db, query("SELECT count(*) FROM chat_v_resource_skills"))
    assert db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == previous
    assert db.execute("SELECT count(*) FROM resources").fetchone()[0] == 0


def test_cap_mask_and_timeout(db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    db.executemany(
        "INSERT INTO demands(code_requisition,id_format_ok,row_hash,created_at,updated_at) "
        "VALUES (?,1,'x','now','now')",
        [(f"T{i:05}",) for i in range(600)],
    )
    answer = execute_query(db, query("SELECT * FROM chat_v_demands"))
    assert answer.query and len(answer.query.rows) == 500 and answer.query.truncated
    answer = execute_query(db, query("SELECT 'person@example.test',123456789"))
    assert answer.query and answer.query.rows == [["[email masked]", "[number masked]"]]
    monkeypatch.setattr(chat_sql, "TIMEOUT_SECONDS", 0.01)
    with pytest.raises(QueryRejected, match="interrupted"):
        execute_query(
            db,
            query(
                "SELECT sum(a.grade+b.grade+c.grade+d.grade) FROM chat_v_demands a "
                "CROSS JOIN chat_v_demands b CROSS JOIN chat_v_demands c "
                "CROSS JOIN chat_v_demands d"
            ),
        )


def test_prompt_budget() -> None:
    # Include constrained-output schema, maximum question and message overhead.
    chars = len(SYSTEM_PROMPT) + len(json.dumps(Lookup.model_json_schema())) + 600
    assert chars / 3 + 512 + 100 <= 4096


@pytest.mark.parametrize("function", ["printf", "format"])
def test_large_format_width_rejected_quickly(db: sqlite3.Connection, function: str) -> None:
    previous = db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    started = time.monotonic()
    with pytest.raises(QueryRejected, match="not authorized"):
        execute_query(db, query(f"SELECT {function}('%500000000s','x')"))
    assert time.monotonic() - started < 2
    assert db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == previous


def test_cell_length_limit_and_restoration(db: sqlite3.Connection) -> None:
    db.executemany(
        "INSERT INTO demands(code_requisition,id_format_ok,row_hash,created_at,updated_at) "
        "VALUES (?,1,'x','now','now')",
        [(f"T{i:05}",) for i in range(150)],
    )
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 200_000)
    for sql in ("SELECT 'hello'", "SELECT * FROM chat_v_resources"):
        execute_query(db, query(sql))
        assert db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == 200_000
    with pytest.raises(QueryRejected, match="too big"):
        execute_query(
            db,
            query(
                "SELECT group_concat(a.requisition || b.requisition,'') "
                "FROM chat_v_demands a CROSS JOIN chat_v_demands b"
            ),
        )
    assert db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) == 200_000
    assert db.execute("SELECT length(?)", ("x" * 100_001,)).fetchone()[0] == 100_001


@pytest.mark.parametrize(
    "name", ["chat_v_evil", '"chat_v_resources"', "[chat_v_x]", "`chat_v_x`", "'chat_v_x'"]
)
@pytest.mark.parametrize("columns", ["", "(name)"])
def test_cte_cannot_impersonate_view(db: sqlite3.Connection, name: str, columns: str) -> None:
    with pytest.raises(QueryRejected):
        execute_query(
            db,
            query(
                f"WITH {name}{columns} /* hidden */ AS (SELECT short_name FROM resources) "
                f"SELECT * FROM {name}"
            ),
        )
