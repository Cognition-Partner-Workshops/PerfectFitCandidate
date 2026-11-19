"""Classification errors and SQL rejections share one bounded correction budget."""

import asyncio
import json

import pytest

from pfc.adapters.llm.providers import FakeProvider, GenerateResult, PromptRequest, ProviderError
from pfc.api.schemas.chat import ChatAnswer
from pfc.services.chat_intent import SYSTEM_PROMPT, Lookup
from pfc.services.chat_pipeline import MAX_CORRECTION_RESPONSE_CHARS, _correction, answer_question
from pfc.services.chat_sql import QueryRejected


class SequenceProvider(FakeProvider):
    def __init__(self, responses: list[str | Exception]) -> None:
        super().__init__()
        self.responses = iter(responses)
        self.requests: list[PromptRequest] = []

    async def generate(
        self,
        request: PromptRequest,
        *,
        api_version: str | None = None,
    ) -> GenerateResult:
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return GenerateResult(response, "fake", "correction-stub")


VALID = json.dumps({"intent": "query", "sql": "SELECT 1", "explanation": "Compute a count."})


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        "not JSON",
        json.dumps({"intent": "demand_count", "scope_type": "region", "scope_term": "London"}),
        json.dumps({"intent": "bench_skill", "skill": "Unmentioned skill"}),
        ProviderError("Invalid structured output"),
    ],
)
def test_invalid_lookup_corrected_before_execution(invalid: str | Exception) -> None:
    provider = SequenceProvider([invalid, VALID])
    executed: list[Lookup] = []

    def execute(lookup: Lookup) -> ChatAnswer:
        executed.append(lookup)
        return ChatAnswer(outcome="answered", text="A count.")

    result = asyncio.run(answer_question(provider, "Count roles by region.", execute))
    assert result.answer.outcome == "answered" and result.error is None
    assert len(provider.requests) == 2 and len(executed) == 1
    assert result.attempted_sql == ["SELECT 1"]
    messages = provider.requests[1].messages
    assert messages[1]["content"] == "Count roles by region."
    assert "Lookup rejected" in messages[-1]["content"]
    assert "query" in messages[-1]["content"]


def test_provider_validation_detail_sent_back_without_changing_adapter() -> None:
    error = ProviderError("Invalid structured output")
    error.__cause__ = ValueError("scope_type: 'region' is not one of ['bu', 'account']")
    provider = SequenceProvider([error, VALID])
    asyncio.run(
        answer_question(
            provider,
            "Count roles by region.",
            lambda lookup: ChatAnswer(outcome="answered", text="A count."),
        )
    )
    assert "scope_type" in provider.requests[1].messages[-1]["content"]


def test_invalid_lookups_exhaust_shared_budget_without_execution() -> None:
    provider = SequenceProvider(["not JSON"] * 3)
    executed: list[Lookup] = []

    def execute(lookup: Lookup) -> ChatAnswer:
        executed.append(lookup)
        return ChatAnswer(outcome="answered", text="Unexpected execution.")

    result = asyncio.run(answer_question(provider, "Count roles.", execute))
    assert result.answer.outcome == "uninterpretable" and result.error
    assert len(provider.requests) == 3
    assert not executed and not result.attempted_sql
    assert len(provider.requests[-1].messages) <= 4


def test_sql_and_lookup_errors_share_budget_and_keep_latest_correction() -> None:
    bad_sql = json.dumps(
        {
            "intent": "query",
            "sql": "SELECT * FROM resources",
            "explanation": "List resources.",
        }
    )
    provider = SequenceProvider([bad_sql, "not JSON", VALID])

    def execute(lookup: Lookup) -> ChatAnswer:
        if lookup.sql != "SELECT 1":
            raise QueryRejected("Direct table access denied")
        return ChatAnswer(outcome="answered", text="A count.")

    result = asyncio.run(answer_question(provider, "Count roles.", execute))
    assert result.answer.outcome == "answered" and result.error is None
    assert len(provider.requests) == 3
    assert result.attempted_sql == ["SELECT * FROM resources", "SELECT 1"]
    assert "SQL rejected" in provider.requests[1].messages[-1]["content"]
    assert "Lookup rejected" in provider.requests[2].messages[-1]["content"]
    assert len(provider.requests[-1].messages) == 4


def test_unavailable_provider_not_retried() -> None:
    provider = SequenceProvider([ProviderError("Connection failed")])
    result = asyncio.run(
        answer_question(
            provider,
            "Count roles.",
            lambda lookup: ChatAnswer(outcome="answered", text="Unexpected execution."),
        )
    )
    assert result.answer.outcome == "unavailable"
    assert len(provider.requests) == 1


def test_correction_context_budget() -> None:
    messages = _correction("x" * 5000, "x" * 1000)
    assert len(messages[0]["content"]) == MAX_CORRECTION_RESPONSE_CHARS
    chars = len(SYSTEM_PROMPT) + len(json.dumps(Lookup.model_json_schema())) + 600
    chars += sum(len(message["content"]) for message in messages)
    assert chars / 3 + 512 + 100 <= 4096
