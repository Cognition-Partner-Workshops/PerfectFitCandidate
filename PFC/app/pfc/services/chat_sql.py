"""Bounded, view-only SQLite queries for chat."""

import re
import sqlite3
import time

from pfc.api.schemas.chat import ChatAnswer, ChatQuery, ChatSource
from pfc.services.chat_intent import ChatMask, Lookup
from pfc.services.chat_skills import register_chat_skills

TIMEOUT_SECONDS = 5.0
MAX_CELL_BYTES = 100_000
FUNCTIONS = set(
    "count sum avg total min max round abs lower upper length substr trim coalesce "
    "ifnull nullif instr date strftime julianday group_concat".split()
)


class QueryRejected(ValueError):
    """The query could not be safely executed."""


def _validate(sql: str) -> None:
    if not sql or len(sql) > 2000:
        raise QueryRejected("SQL must contain at most 2000 characters")
    # SQLite's completeness parser understands quoted semicolons and comments.
    statements = []
    start = 0
    for index, char in enumerate(sql):
        if char == ";" and sqlite3.complete_statement(sql[start : index + 1]):
            statements.append(sql[start : index + 1])
            start = index + 1
    tail = re.sub(r"/\*.*?\*/|--[^\n]*", "", sql[start:], flags=re.S).strip()
    if tail:
        statements.append(sql[start:])
    if len(statements) != 1:
        raise QueryRejected("Exactly one SELECT statement is required")
    first = re.sub(r"/\*.*?\*/|--[^\n]*", "", statements[0], flags=re.S).lstrip()
    if not re.match(r"(SELECT|WITH)\b", first, re.I):
        raise QueryRejected("Only SELECT or WITH is allowed")
    tokens = re.findall(
        r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|"
        r"`(?:``|[^`])*`|\[[^]]*\]|[\w]+|[^\s]",
        sql,
        re.S,
    )
    tokens = [token for token in tokens if not token.startswith(("--", "/*"))]
    for index, (token, following) in enumerate(zip(tokens, tokens[1:], strict=False)):
        name = token.strip("'\"`[]").lower()
        cte = following == "(" or (
            following.lower() == "as"
            and index + 2 < len(tokens)
            and tokens[index + 2].lower() in {"(", "not", "materialized"}
        )
        if name.startswith("chat_v_") and cte:
            raise QueryRejected("Safe view names cannot be CTE names or column aliases")


def execute_query(db: sqlite3.Connection, lookup: Lookup) -> ChatAnswer:
    sql = lookup.sql or ""
    _validate(sql)
    mask = ChatMask(db)
    uses_skills = "chat_v_resource_skills" in sql.lower()
    if uses_skills:
        register_chat_skills(db, snapshot=True)
    db.execute("PRAGMA query_only=ON")
    db.execute("SELECT value FROM json_each('{}')").fetchall()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    view_json_read = False

    def authorize(
        action: int, arg1: str | None, arg2: str | None, schema: str | None, source: str | None
    ) -> int:
        nonlocal view_json_read
        internal = bool(source and source.startswith("chat_v_"))
        allowed = action == sqlite3.SQLITE_SELECT
        if action == sqlite3.SQLITE_READ:
            if internal and schema == "main" and arg1 == "json_each":
                view_json_read = True
            # COUNT(*) can add a source-less, zero-column read of the view's virtual table.
            count_json_read = (
                view_json_read and arg1 == "json_each" and arg2 == "" and source is None
            )
            allowed = schema == "main" and bool(
                (arg1 and arg1.startswith("chat_v_")) or internal or count_json_read
            )
        elif action == sqlite3.SQLITE_FUNCTION:
            allowed = (arg2 or "").lower() in FUNCTIONS or (
                internal
                and (arg2 or "").lower()
                in {
                    "json_valid",
                    "json_type",
                    "json_extract",
                    "glob",
                    "ltrim",
                    "chat_resource_skills",
                }
            )
        return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY

    previous_limit = db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_CELL_BYTES)
    db.set_authorizer(authorize)
    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    cursor: sqlite3.Cursor | None = None
    try:
        cursor = db.execute(sql)
        columns = [mask.mask(column[0]) for column in cursor.description or ()]
        raw = cursor.fetchmany(501)
        rows: list[list[str | int | float | None]] = []
        for row in raw[:500]:
            cells: list[str | int | float | None] = []
            for value in row:
                masked = mask.mask(str(value)) if value is not None else None
                cells.append(
                    value if masked == str(value) and isinstance(value, (int, float)) else masked
                )
            rows.append(cells)
    except sqlite3.Error as exc:
        raise QueryRejected(str(exc)) from exc
    finally:
        db.set_progress_handler(None, 0)
        db.set_authorizer(None)
        if cursor is not None:
            cursor.close()
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous_limit)
        if uses_skills:
            register_chat_skills(db)
    ranking = "rank" in [column.lower() for column in columns] or bool(
        re.search(r"\bchat_v_match_results\b", sql, re.I)
    )
    sources: dict[tuple[str, int], ChatSource] = {}
    for row in rows:
        for value in row:
            if not isinstance(value, str):
                continue
            resource = re.fullmatch(r"Resource #(\d+)", value)
            if resource:
                record_id = int(resource[1])
                found = db.execute(
                    "SELECT candidate_id FROM resources WHERE resource_id=? "
                    "AND lifecycle!='archived'",
                    (record_id,),
                ).fetchone()
                if found:
                    sources["resource", record_id] = ChatSource(
                        kind="resource",
                        record_id=record_id,
                        label=value,
                        link=f"/candidates/{found[0]}" if found[0] else "/bench",
                    )
            demand = db.execute(
                "SELECT demand_id FROM demands WHERE code_requisition=? AND lifecycle='open'",
                (value,),
            ).fetchone()
            if demand:
                sources["demand", demand[0]] = ChatSource(
                    kind="demand",
                    record_id=demand[0],
                    label=value,
                    link=f"/demands?demand={demand[0]}",
                )
    explanation = mask.mask(lookup.explanation or "")
    return ChatAnswer(
        outcome="answered" if rows else "no_data",
        text=f"{explanation} Returned {len(rows)} rows."
        + (" Stored PFC ranking — human review required; PFC never decides." if ranking else ""),
        sources=list(sources.values()),
        query=ChatQuery(
            sql=mask.mask(sql),
            explanation=explanation,
            columns=columns,
            rows=rows,
            truncated=len(raw) > 500,
            ranking=ranking,
        ),
    )
