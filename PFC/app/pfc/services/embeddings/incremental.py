"""Submit local incremental embedding independently of HTTP request state."""

from collections.abc import Iterable
from datetime import UTC, datetime

from pfc.adapters.vector.stores import create_vector_store
from pfc.config import Settings
from pfc.db.connection import connect, database
from pfc.jobs.runner import JobContext, JobRunner
from pfc.services.embeddings.chunking import OwnerType
from pfc.services.embeddings.reembed import EmbeddingPipelineMismatch, embed_owners
from pfc.services.embeddings.runtime import EmbeddingModelUnavailable, create_embedder


def _finish(settings: Settings, ctx: JobContext, status: str, message: str) -> None:
    with database(settings) as db:
        # TODO(foundation): JobContext.complete(message)/fail(message) in pfc/jobs/runner.py.
        db.execute(
            "UPDATE jobs SET status=?,message=?,finished_at=?,"
            "progress=CASE WHEN ?='completed' THEN 1 ELSE progress END WHERE job_id=?",
            (status, message, datetime.now(UTC).isoformat(), status, ctx.job_id),
        )


def submit_incremental_embed(
    runner: JobRunner,
    settings: Settings,
    owners: Iterable[tuple[OwnerType, int]],
    *,
    created_by: int | None = None,
) -> int | None:
    selected = sorted(set(owners))
    if not selected:
        return None
    with database(settings) as db:
        row = db.execute(
            "SELECT embedding_model_id FROM embedding_models WHERE is_active=1"
        ).fetchone()
    if row is None:
        return None
    model_id = int(row[0])

    def work(ctx: JobContext) -> None:
        connection = connect(settings)
        try:
            try:
                ctx.raise_if_cancelled()
                embedder = create_embedder(settings, embedding_model_id=model_id)
                store = create_vector_store(connection, dimensions=embedder.dimensions)
                counts = embed_owners(
                    settings,
                    ctx,
                    owners=selected,
                    embedder=embedder,
                    store=store,
                    embedding_model_id=model_id,
                )
            except (EmbeddingModelUnavailable, EmbeddingPipelineMismatch) as exc:
                ctx.raise_if_cancelled()
                _finish(settings, ctx, "failed", f"Incremental embedding unavailable: {exc}")
                return
            message = (
                f"Incremental embedding complete: embedded={counts.embedded}, "
                f"reused={counts.reused}, skipped={counts.skipped}, deleted={counts.deleted}"
            )
            ctx.report_progress(1, message)
            _finish(settings, ctx, "completed", message)
        finally:
            connection.close()

    return runner.submit(
        "embed", work, {"embedding_model_id": model_id, "owners": selected}, created_by=created_by
    )
