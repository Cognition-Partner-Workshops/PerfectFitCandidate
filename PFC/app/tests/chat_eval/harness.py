"""Execution-based scoring; fixed lookup answers deliberately score as incorrect."""

import math
import statistics
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from pfc.adapters.llm.providers import LLMProvider
from pfc.api.schemas.chat import ChatAnswer
from pfc.config import Settings
from pfc.db.connection import database
from pfc.services.chat_intent import ChatMask, Lookup
from pfc.services.chat_pipeline import ChatResult, answer_question
from pfc.services.chat_queries import answer_lookup
from pfc.services.chat_sql import execute_query

from .fixture import TODAY

Question = dict[str, Any]


def load_questions() -> list[Question]:
    with Path(__file__).with_name("questions.yaml").open() as stream:
        questions: list[Question] = yaml.safe_load(stream)
    return questions


def equal_results(
    expected: list[list[Any]],
    actual: list[list[Any]],
    *,
    ordered: bool,
) -> bool:
    def normalize(rows: list[list[Any]]) -> list[tuple[Any, ...]]:
        return [
            tuple(round(cell, 2) if isinstance(cell, float) else cell for cell in row)
            for row in rows
        ]

    left, right = normalize(expected), normalize(actual)
    return left == right if ordered else Counter(left) == Counter(right)


def score(
    item: Question,
    result: ChatResult,
    reference: ChatAnswer | None,
    executed_sql: list[str],
) -> tuple[bool, str | None]:
    if item.get("expect") == "unsupported":
        correct = result.answer.outcome in {"unsupported", "uninterpretable"} and not executed_sql
        return correct, None if correct else "Expected refusal without executing SQL"
    if result.answer.query is None:
        return False, result.error or "Non-query intent is not scored as SQL"
    assert reference and reference.query
    correct = equal_results(reference.query.rows, result.answer.query.rows, ordered=item["ordered"])
    return correct, None if correct else "Result rows differ from reference"


async def evaluate(
    settings: Settings,
    provider: LLMProvider,
    questions: list[Question] | None = None,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for item in load_questions() if questions is None else questions:
        executed_sql: list[str] = []
        with database(settings) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            question = ChatMask(db).mask(item["question"], identify=True)
            reference = (
                execute_query(
                    db,
                    Lookup(
                        intent="query",
                        sql=item["reference_sql"],
                        explanation="Reference computation.",
                    ),
                )
                if "reference_sql" in item
                else None
            )

            def execute(lookup: Lookup, executed: list[str] = executed_sql) -> ChatAnswer:
                def trace(statement: str) -> None:
                    if lookup.sql and statement.strip() == lookup.sql.strip():
                        executed.append(statement)

                db.set_trace_callback(trace)
                try:
                    return answer_lookup(db, lookup, date.fromisoformat(TODAY))
                finally:
                    db.set_trace_callback(None)

            started = time.perf_counter()
            result = await answer_question(provider, question, execute)
            elapsed = time.perf_counter() - started
            correct, error = score(item, result, reference, executed_sql)
            records.append(
                {
                    "id": item["id"],
                    "question": item["question"],
                    "category": item["category"],
                    "correct": correct,
                    "seconds": elapsed,
                    "outcome": result.answer.outcome,
                    "intent": result.lookup.intent if result.lookup else None,
                    "generated_sql": result.lookup.sql if result.lookup else None,
                    "attempted_sql": result.attempted_sql,
                    "executed_sql": executed_sql,
                    "error": error,
                    "pipeline_error": result.error,
                }
            )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["category"]].append(record)

    def accuracy(items: list[dict[str, Any]]) -> dict[str, Any]:
        correct = sum(item["correct"] for item in items)
        return {
            "correct": correct,
            "total": len(items),
            "accuracy": correct / len(items) if items else 0.0,
        }

    durations = sorted(record["seconds"] for record in records)
    return {
        "overall": accuracy(records),
        "categories": {name: accuracy(items) for name, items in sorted(grouped.items())},
        "median_seconds": statistics.median(durations) if durations else 0.0,
        "p95_seconds": durations[math.ceil(len(durations) * 0.95) - 1] if durations else 0.0,
        "failures": [record for record in records if not record["correct"]],
        "results": records,
        "non_query_policy": "Fixed lookup intents are incorrect for non-refusal items.",
    }
