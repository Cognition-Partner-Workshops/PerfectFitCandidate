import asyncio
import hashlib
from collections.abc import Callable
from concurrent.futures import wait
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from pfc.adapters.llm.providers import LLMProvider
from pfc.api.schemas.chat import ChatAnswer, ChatDetail, ChatEntry, ChatQuestion, ChatStatus
from pfc.db.audit import append_audit
from pfc.db.connection import Connection, database
from pfc.jobs.router import Runner
from pfc.jobs.runner import JobContext
from pfc.security.dependencies import Principal, require_role
from pfc.security.problems import Problem
from pfc.security.sessions import stamp
from pfc.services.chat_intent import (
    ChatMask,
    Lookup,
    model_ready,
)
from pfc.services.chat_pipeline import answer_question
from pfc.services.chat_queries import answer_lookup

router = APIRouter(prefix="/chat", tags=["chat"])
ChatUser = Annotated[Principal, Depends(require_role("Analyst"))]
_COLUMNS = "j.job_id,j.status,j.created_at,q.question"
_OWNED = " FROM chat_questions q JOIN jobs j USING(job_id) WHERE j.kind='chat' AND j.created_by=?"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _active_role(db: Connection, user_id: int) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM users u JOIN roles r USING(role_id) "
            "WHERE u.user_id=? AND u.is_active=1 AND r.name IN ('Analyst','Admin')",
            (user_id,),
        ).fetchone()
        is not None
    )


@router.get("/status")
async def chat_status(request: Request, _user: ChatUser) -> ChatStatus:
    return ChatStatus(available=await model_ready(getattr(request.app.state, "llm_provider", None)))


@router.get("")
def chat_history(runner: Runner, user: ChatUser) -> list[ChatEntry]:
    with database(runner.settings) as db:
        return [
            ChatEntry.model_validate(dict(row))
            for row in db.execute(
                f"SELECT {_COLUMNS}{_OWNED} ORDER BY j.job_id DESC LIMIT 50", (user.user.user_id,)
            )
        ]


@router.get("/{job_id}")
def chat_detail(job_id: int, runner: Runner, user: ChatUser) -> ChatDetail:
    with database(runner.settings) as db:
        row = db.execute(
            f"SELECT {_COLUMNS},q.answer_json{_OWNED} AND j.job_id=?",
            (user.user.user_id, job_id),
        ).fetchone()
    if row is None:
        raise Problem(404, "CHAT_NOT_FOUND", "Chat question not found")
    entry = dict(row)
    saved = entry.pop("answer_json")
    return ChatDetail.model_validate(
        {
            **entry,
            "answer": ChatAnswer.model_validate_json(saved)
            if saved and entry["status"] == "completed"
            else None,
        }
    )


@router.post("", status_code=202)
async def ask(body: ChatQuestion, request: Request, runner: Runner, user: ChatUser) -> ChatEntry:
    loop = asyncio.get_running_loop()
    user_id = user.user.user_id
    state = request.app.state
    clock: Callable[[], datetime] = state.clock

    def submit() -> ChatEntry:
        with database(runner.settings) as db:
            question = ChatMask(db).mask(body.question, identify=True)
        asked_at = stamp(clock())

        def record_question(db: Connection, job_id: int) -> None:
            db.execute(
                "INSERT INTO chat_questions(job_id,question) VALUES (?,?)", (job_id, question)
            )
            append_audit(
                db,
                ts=asked_at,
                action="CHAT_QUESTION",
                user_id=user_id,
                entity_type="chat",
                entity_id=str(job_id),
                details={"question_sha256": _digest(question)},
            )

        def work(context: JobContext) -> None:
            with database(runner.settings) as db:
                if not _active_role(db, user_id):
                    raise PermissionError("Chat role is unavailable")
            context.report_progress(0.25, "Interpreting question locally")

            def execute(lookup: Lookup) -> ChatAnswer:
                with database(runner.settings) as db:
                    db.execute("PRAGMA query_only=ON")
                    db.execute("BEGIN")
                    if not _active_role(db, user_id):
                        raise PermissionError("Chat role is unavailable")
                    return answer_lookup(db, lookup, clock().date())

            provider: LLMProvider | None = getattr(state, "llm_provider", None)
            future = asyncio.run_coroutine_threadsafe(
                answer_question(provider, question, execute),
                loop,
            )
            try:
                while not wait((future,), timeout=0.2).done:
                    context.raise_if_cancelled()
                result = future.result()
                answer = result.answer
                lookup = result.lookup
            finally:
                future.cancel()
            encoded = answer.model_dump_json()

            def record_answer(db: Connection) -> None:
                if not _active_role(db, user_id):
                    raise PermissionError("Chat role is unavailable")
                db.execute(
                    "UPDATE chat_questions SET answer_json=? WHERE job_id=?",
                    (encoded, context.job_id),
                )
                append_audit(
                    db,
                    ts=stamp(clock()),
                    action="CHAT_ANSWER",
                    user_id=user_id,
                    entity_type="chat",
                    entity_id=str(context.job_id),
                    details={
                        "outcome": answer.outcome,
                        "answer_sha256": _digest(encoded),
                        "sources": [[source.kind, source.record_id] for source in answer.sources],
                        **(
                            {
                                "sql_sha256": _digest(lookup.sql if lookup and lookup.sql else ""),
                                "row_count": len(answer.query.rows),
                            }
                            if answer.query
                            else {}
                        ),
                    },
                )

            context.complete(record_answer)

        job_id = runner.submit("chat", work, created_by=user_id, before_dispatch=record_question)
        return ChatEntry(job_id=job_id, question=question, status="queued", created_at=asked_at)

    return await run_in_threadpool(submit)
