import asyncio
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from pfc.adapters.llm.providers import FakeProvider, GenerateResult, PromptRequest
from pfc.api.schemas.chat import ChatAnswer, ChatQuery
from pfc.config import Settings
from pfc.db.connection import database
from pfc.services.chat_intent import Lookup
from pfc.services.chat_pipeline import ChatResult
from pfc.services.chat_sql import execute_query

from .fixture import build_fixture
from .harness import equal_results, evaluate, load_questions, score
from .run_eval import print_report, run


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    return build_fixture(tmp_path_factory.mktemp("chat-eval"))


class ReferenceProvider(FakeProvider):
    def __init__(self, mode: str = "reference") -> None:
        super().__init__()
        self.mode = mode
        self.items = {item["question"]: item for item in load_questions()}
        self.requests: list[PromptRequest] = []

    async def generate(
        self,
        request: PromptRequest,
        *,
        api_version: str | None = None,
    ) -> GenerateResult:
        self.requests.append(request)
        item = self.items[request.messages[1]["content"]]
        value: dict[str, Any] = {"intent": "unsupported"}
        if "reference_sql" in item:
            sql = item["reference_sql"]
            if self.mode == "wrong":
                sql = "SELECT -999 AS wrong"
            if self.mode == "alias":
                sql = "SELECT count(*) AS different_column_name FROM chat_v_demands"
            if self.mode == "retry" and len(request.messages) == 2:
                sql = "SELECT * FROM resources"
            if self.mode == "fixed":
                value = {"intent": "demand_count"}
            else:
                value = {
                    "intent": "query",
                    "sql": sql,
                    "explanation": "This computes the requested data.",
                }
        elif self.mode == "execute_then_refuse" and len(request.messages) == 2:
            value = {"intent": "query", "sql": "SELECT 1", "explanation": "This computes a value."}
        return GenerateResult(json.dumps(value), "fake", "eval-stub")


def test_dataset_and_references(settings: Settings) -> None:
    items = load_questions()
    assert len(items) == 100 and len({item["id"] for item in items}) == 100
    assert Counter(item["category"] for item in items) == {
        "single_view": 30,
        "aggregate": 25,
        "top_n": 20,
        "join": 10,
        "must_refuse": 15,
    }
    assert "Which candidate is best fit for demand T10004?" in [i["question"] for i in items]
    assert "Top 5 candidates for T10001" in [i["question"] for i in items]
    with database(settings) as db:
        assert db.execute("SELECT count(*) FROM demands").fetchone()[0] == 50
        assert db.execute("SELECT count(*) FROM resources").fetchone()[0] == 80
        archived_refs = {
            f"Resource #{r[0]}"
            for r in db.execute("SELECT resource_id FROM resources WHERE lifecycle='archived'")
        }
        archived_reqs = {
            r[0]
            for r in db.execute("SELECT code_requisition FROM demands WHERE lifecycle='archived'")
        }
        for item in items:
            assert isinstance(item["ordered"], bool)
            expected_keys = {"id", "question", "category", "ordered"} | (
                {"expect"} if item["category"] == "must_refuse" else {"reference_sql"}
            )
            assert set(item) == expected_keys
            if "expect" in item:
                assert item["expect"] == "unsupported"
                continue
            answer = execute_query(
                db,
                Lookup(
                    intent="query",
                    sql=item["reference_sql"],
                    explanation="Reference computation.",
                ),
            )
            assert answer.query and answer.query.rows and not answer.query.truncated, item["id"]
            cells = {str(cell) for row in answer.query.rows for cell in row}
            assert not cells & (archived_refs | archived_reqs)
        # Full view membership, not just selected reference columns, excludes archived rows.
        for view in ("resources", "resource_skills", "match_results"):
            refs = {r[0] for r in db.execute(f"SELECT resource_ref FROM chat_v_{view}")}
            assert not refs & archived_refs
        for view in ("demands", "demand_skills", "match_results"):
            reqs = {r[0] for r in db.execute(f"SELECT requisition FROM chat_v_{view}")}
            assert not reqs & archived_reqs
        scores = [r[0] for r in db.execute("SELECT score FROM chat_v_match_results")]
        assert len(scores) == 47 * 76
        assert all(round(score * 100) % 10 == 1 for score in scores)


def test_fixture_deterministic(settings: Settings, tmp_path: Path) -> None:
    second = build_fixture(tmp_path / "again")
    with database(settings) as left, database(second) as right:
        for table in (
            "demands",
            "resources",
            "resource_skills",
            "jd_requirements",
            "match_results",
        ):
            assert [tuple(r) for r in left.execute(f"SELECT * FROM {table} ORDER BY rowid")] == [
                tuple(r) for r in right.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]


def test_reference_model_scores_one_hundred_percent(settings: Settings) -> None:
    report = asyncio.run(evaluate(settings, ReferenceProvider()))
    assert report["overall"] == {"correct": 100, "total": 100, "accuracy": 1.0}
    assert not report["failures"]
    assert report["p95_seconds"] >= report["median_seconds"] >= 0
    assert all(not item["executed_sql"] for item in report["results"][-15:])


def test_wrong_model_and_fixed_lookup_are_incorrect(settings: Settings) -> None:
    items = load_questions()
    report = asyncio.run(evaluate(settings, ReferenceProvider("wrong"), [items[0], items[-1]]))
    assert report["overall"]["correct"] == 1
    assert report["failures"][0]["generated_sql"] == "SELECT -999 AS wrong"
    fixed = asyncio.run(evaluate(settings, ReferenceProvider("fixed"), [items[0]]))
    assert fixed["overall"]["correct"] == 0
    assert fixed["failures"][0]["error"] == "Non-query intent is not scored as SQL"


def test_shared_correction_pipeline(settings: Settings) -> None:
    provider = ReferenceProvider("retry")
    report = asyncio.run(evaluate(settings, provider, load_questions()[:1]))
    assert report["overall"]["accuracy"] == 1.0
    assert len(provider.requests) == 2
    assert len(report["results"][0]["attempted_sql"]) == 2
    assert report["results"][0]["executed_sql"] == [load_questions()[0]["reference_sql"]]


def test_column_names_ignored(settings: Settings) -> None:
    report = asyncio.run(evaluate(settings, ReferenceProvider("alias"), load_questions()[:1]))
    assert report["overall"]["accuracy"] == 1.0


@pytest.mark.parametrize("outcome", ["unsupported", "uninterpretable"])
def test_refusal_requires_no_executed_sql(outcome: str) -> None:
    answer = ChatAnswer.model_validate({"outcome": outcome, "text": "Refused."})
    result = ChatResult(answer)
    item = {"expect": "unsupported"}
    assert score(item, result, None, []) == (True, None)
    assert score(item, result, None, ["SELECT 1"])[0] is False


def test_empty_query_results_score() -> None:
    answer = ChatAnswer(
        outcome="no_data",
        text="No records.",
        query=ChatQuery(
            sql="SELECT 1 WHERE 0",
            explanation="No records.",
            columns=["unused"],
            rows=[],
            truncated=False,
            ranking=False,
        ),
    )
    assert score({"ordered": False}, ChatResult(answer), answer, []) == (True, None)


def test_refusal_with_sql_is_wrong(settings: Settings) -> None:
    report = asyncio.run(
        evaluate(settings, ReferenceProvider("execute_then_refuse"), load_questions()[-1:])
    )
    assert report["overall"]["correct"] == 0
    assert report["results"][0]["executed_sql"] == ["SELECT 1"]


@pytest.mark.parametrize(
    "left,right,ordered,expected",
    [
        ([[1, 1.234], [2, None]], [[2, None], [1, 1.233]], False, True),
        ([[1], [2]], [[2], [1]], True, False),
        ([[1], [1]], [[1]], False, False),
        ([[1, 2]], [[2, 1]], False, False),
        ([], [], True, True),
        ([[None]], [["None"]], False, False),
    ],
)
def test_scorer(
    left: list[list[Any]], right: list[list[Any]], ordered: bool, expected: bool
) -> None:
    assert equal_results(left, right, ordered=ordered) is expected


def test_report_empty_and_failure_output(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = asyncio.run(evaluate(settings, ReferenceProvider(), []))
    assert empty["median_seconds"] == empty["p95_seconds"] == 0
    report = asyncio.run(evaluate(settings, ReferenceProvider("wrong"), load_questions()[:1]))
    print_report(report)
    printed = capsys.readouterr().out
    assert "overall: 0/1 (0.0%)" in printed
    assert "FAIL q001:" in printed and "SQL: SELECT -999 AS wrong" in printed


@pytest.mark.skipif(
    not os.environ.get("PFC_CHAT_EVAL_URL"), reason="Opt-in local llama-server evaluation"
)
def test_real_model(tmp_path: Path) -> None:
    report = asyncio.run(
        run(
            os.environ["PFC_CHAT_EVAL_URL"],
            tmp_path / "results.json",
            os.environ.get("LLAMA_API_KEY", ""),
        )
    )
    assert report["overall"]["total"] == 100
    assert (tmp_path / "results.json").is_file()
