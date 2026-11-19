"""Shared model interpretation and bounded SQL correction for chat and evaluation."""

from collections.abc import Callable
from dataclasses import dataclass, field

from starlette.concurrency import run_in_threadpool

from pfc.adapters.llm.providers import LLMProvider
from pfc.api.schemas.chat import ChatAnswer
from pfc.services.chat_intent import InvalidLookup, Lookup, ModelUnavailable, classify
from pfc.services.chat_sql import QueryRejected

MAX_CORRECTION_RESPONSE_CHARS = 1800


def _correction(response: str | None, error: str) -> list[dict[str, str]]:
    messages = []
    if response:
        messages.append({"role": "assistant", "content": response[:MAX_CORRECTION_RESPONSE_CHARS]})
    messages.append(
        {
            "role": "user",
            "content": error[:350] + ". Return corrected schema-valid JSON. Use intent=query for "
            "data questions outside the exact fixed examples; unsupported for forbidden requests. "
            "Use only the documented safe views and preserve every requested filter.",
        }
    )
    return messages


@dataclass
class ChatResult:
    answer: ChatAnswer
    lookup: Lookup | None = None
    attempted_sql: list[str] = field(default_factory=list)
    error: str | None = None


async def answer_question(
    provider: LLMProvider | None,
    question: str,
    execute: Callable[[Lookup], ChatAnswer],
) -> ChatResult:
    corrections: list[dict[str, str]] = []
    result = ChatResult(
        ChatAnswer(
            outcome="uninterpretable",
            text="I couldn't interpret that question. Try: "
            "Which resources are top ranked for T10004?",
        )
    )
    for attempt in range(3):
        try:
            lookup = await classify(provider, question, corrections)
        except ModelUnavailable:
            result.error = "The local model is unavailable"
            result.answer = ChatAnswer(
                outcome="unavailable",
                text=("The local model is unavailable. Check the active model and retry."),
            )
            break
        except InvalidLookup as exc:
            result.error = "Invalid structured lookup: " + str(exc)
            if attempt == 2:
                break
            corrections = _correction(exc.response, "Lookup rejected: " + str(exc))
            continue
        result.lookup = lookup
        if lookup.sql:
            result.attempted_sql.append(lookup.sql)
        try:
            result.answer = await run_in_threadpool(execute, lookup)
            result.error = None
            break
        except QueryRejected as exc:
            result.error = str(exc)
            if attempt == 2:
                break
            corrections = _correction(
                lookup.model_dump_json(exclude_none=True),
                "SQL rejected: " + str(exc),
            )
    return result
