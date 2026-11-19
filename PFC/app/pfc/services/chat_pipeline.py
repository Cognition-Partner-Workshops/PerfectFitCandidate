"""Shared model interpretation and bounded SQL correction for chat and evaluation."""

from collections.abc import Callable
from dataclasses import dataclass, field

from starlette.concurrency import run_in_threadpool

from pfc.adapters.llm.providers import LLMProvider
from pfc.api.schemas.chat import ChatAnswer
from pfc.services.chat_intent import InvalidLookup, Lookup, ModelUnavailable, classify
from pfc.services.chat_sql import QueryRejected


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
        except InvalidLookup:
            result.error = "Invalid structured lookup"
            break
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
            corrections.extend(
                [
                    {"role": "assistant", "content": lookup.model_dump_json(exclude_none=True)},
                    {
                        "role": "user",
                        "content": "SQL rejected: "
                        + str(exc)[:300]
                        + ". Correct the query using only safe views.",
                    },
                ]
            )
    return result
