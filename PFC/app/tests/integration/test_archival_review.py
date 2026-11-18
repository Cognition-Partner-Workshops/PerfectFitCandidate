"""Regression coverage for archival interactions with retained history and active views."""

from datetime import UTC, datetime

import pytest
from test_ingestion_approvals import client as client
from test_ingestion_approvals import ingest, row

from pfc.db.connection import database
from pfc.services import archival
from pfc.services.lifecycle import record_transition

NOW = datetime(2026, 11, 18, tzinfo=UTC)


@pytest.mark.parametrize("kind,entity", [("dl", "demand"), ("ct", "resource")])
@pytest.mark.parametrize("highest_ledger", [None, "lifecycle_events", "archive_batch_items"])
def test_ingestion_does_not_reuse_purged_entity_ids(client, kind, entity, highest_ledger):
    ingest(client, kind, [{}])
    settings = client.app.state.settings
    old_id = row(client, entity)[f"{entity}_id"]
    with database(settings) as db:
        record_transition(
            db,
            entity,
            old_id,
            "open" if entity == "demand" else "active",
            "archived",
            "archive",
            now=NOW.isoformat(),
            metadata={"archived_at": "2024-01-01"},
        )
        batch = archival.preview(db, kind="purge", user_id=1, now=NOW)
        db.execute(
            "UPDATE archive_batches SET status='running' WHERE batch_id=?", (batch["batch_id"],)
        )
    archival.execute_batch(settings, batch["batch_id"], 1, NOW)
    highest_id = old_id
    with database(settings) as db:
        assert (
            db.execute(f"SELECT 1 FROM {entity}s WHERE {entity}_id=?", (old_id,)).fetchone() is None
        )
        if highest_ledger:
            highest_id += 10
            if highest_ledger == "lifecycle_events":
                db.execute(
                    "INSERT INTO lifecycle_events"
                    "(entity_type,entity_id,to_state,source,created_at) "
                    "VALUES (?,?,'purged','purge',?)",
                    (entity, highest_id, NOW.isoformat()),
                )
            else:
                db.execute(
                    "INSERT INTO archive_batch_items"
                    "(batch_id,entity_type,entity_id,label,reason,fingerprint) "
                    "VALUES (?,?,?,'Retained record','retention_expired','old')",
                    (batch["batch_id"], entity, highest_id),
                )
    replacement = (
        {"Code Requisition": "T29999"}
        if kind == "dl"
        else {"ShortName": "FreshPerson", "Email Address": "fresh@example.test"}
    )
    ingest(client, kind, [replacement])
    with database(settings) as db:
        new_id = db.execute(f"SELECT {entity}_id FROM {entity}s").fetchone()[0]
        assert new_id > highest_id
        assert [
            event[0]
            for event in db.execute(
                "SELECT to_state FROM lifecycle_events WHERE entity_type=? AND entity_id=? "
                "ORDER BY event_id",
                (entity, old_id),
            )
        ][-1] == "purged"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
