"""REQ-CLS-02: exact-domain classification, overrides and transaction ownership."""

import json

import pytest
from fastapi.testclient import TestClient
from test_occ import client as client
from test_occ import login

from pfc.db.audit import verify_chain
from pfc.db.connection import database
from pfc.services.candidates.classification import reclassify_all, reclassify_candidate


def seed_candidate(
    client: TestClient,
    emails: list[tuple[str, str, int]],
    value: str = "External",
    override: str | None = None,
) -> int:
    with database(client.app.state.settings) as db:
        candidate_id = db.execute(
            "INSERT INTO candidates(internal_ref,identity_status,classification,"
            "classification_override,created_at,updated_at) "
            "VALUES ('PFC-C-TEST','complete',?,?, '2026-01-01','2026-01-01')",
            (value, override),
        ).lastrowid
        for email, source, confirmed in emails:
            db.execute(
                "INSERT INTO candidate_emails(candidate_id,email,email_key,source,confirmed) "
                "VALUES (?,?,?,?,?)",
                (candidate_id, email, email.strip().lower(), source, confirmed),
            )
    assert candidate_id is not None
    return candidate_id


def stored(client: TestClient, candidate_id: int) -> dict:
    with database(client.app.state.settings) as db:
        return dict(
            db.execute("SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        )


def test_domain_changes_reclassify_and_audit(client: TestClient) -> None:
    candidate_id = seed_candidate(client, [(" Person@Example.COM ", "CV", 1)])
    login(client)
    base = "/api/v1/occ/internal-domains"
    assert client.post(base, json={"domain": " @EXAMPLE.COM "}).status_code == 201
    assert stored(client, candidate_id)["classification"] == "Internal"
    assert client.delete(base + "/example.com").status_code == 204
    assert stored(client, candidate_id)["classification"] == "External"
    with database(client.app.state.settings) as db:
        events = db.execute(
            "SELECT * FROM audit_log WHERE action='CLASSIFICATION_REEVALUATED' ORDER BY audit_id"
        ).fetchall()
        assert len(events) == 2
        assert [json.loads(row["details_json"])["changed"] for row in events] == [1, 1]
        assert all(row["user_id"] == 1 for row in events)
        assert "Person@" not in str([dict(row) for row in events])
        assert "Example.COM" not in str([dict(row) for row in events])
        assert verify_chain(db)


@pytest.mark.parametrize(
    "emails,expected",
    [
        ([], "Unknown"),
        ([("   ", "CV", 1)], "Unknown"),
        ([("person@example.com", "CV", 0)], "External"),
        ([("person@example.com", "CV", 1)], "Internal"),
        ([("person@example.com", "CT", 0)], "Internal"),
        ([("person@sub.example.com", "CT", 1)], "External"),
        ([("invalid-email", "CV", 1)], "External"),
        ([("outside@other.test", "CV", 1), ("person@example.com", "manual", 1)], "Internal"),
    ],
)
def test_classification_rules(client: TestClient, emails: list, expected: str) -> None:
    login(client)
    assert (
        client.post("/api/v1/occ/internal-domains", json={"domain": "example.com"}).status_code
        == 201
    )
    candidate_id = seed_candidate(client, emails)
    with database(client.app.state.settings) as db:
        reclassify_candidate(db, candidate_id)
    assert stored(client, candidate_id)["classification"] == expected


def test_override_is_preserved_and_clear_recomputes(client: TestClient) -> None:
    candidate_id = seed_candidate(client, [("private@example.com", "CV", 1)])
    login(client)
    base = "/api/v1/occ/internal-domains"
    assert client.post(base, json={"domain": "example.com"}).status_code == 201
    path = f"/api/v1/candidates/{candidate_id}/classification"
    response = client.put(
        path,
        json={
            "classification": "Unknown",
            "reason": "Confirmed with private@example.com",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "candidateId": candidate_id,
        "effectiveClassification": "Unknown",
        "computedClassification": "Internal",
        "classificationOverride": "Unknown",
        "overrideReason": "Confirmed with private@example.com",
        "overrideBy": 1,
        "overrideAt": response.json()["overrideAt"],
    }
    assert response.json()["overrideAt"]
    before = stored(client, candidate_id)
    assert client.delete(base + "/example.com").status_code == 204
    with database(client.app.state.settings) as db:
        reclassify_candidate(db, candidate_id)
        assert reclassify_all(db) == 0
    assert stored(client, candidate_id) == before
    response = client.put(path, json={"classification": None, "reason": "Return to automatic"})
    assert response.status_code == 200
    assert response.json()["effectiveClassification"] == "External"
    assert response.json()["computedClassification"] == "External"
    assert all(
        response.json()[key] is None
        for key in (
            "classificationOverride",
            "overrideReason",
            "overrideBy",
            "overrideAt",
        )
    )
    with database(client.app.state.settings) as db:
        events = db.execute(
            "SELECT * FROM audit_log WHERE action='CLASSIFICATION_OVERRIDE'"
        ).fetchall()
        assert len(events) == 2
        assert all(row["entity_id"] == str(candidate_id) for row in events)
        details = [json.loads(row["details_json"]) for row in events]
        assert details[0]["before"] == {"effective": "Internal", "override": None}
        assert details[0]["after"] == {"effective": "Unknown", "override": "Unknown"}
        assert details[1]["after"] == {"effective": "External", "override": None}
        assert details[1]["reason"] == "Return to automatic"
        assert "private@example.com" not in json.dumps(details)
        assert verify_chain(db)


@pytest.mark.parametrize(
    "body",
    [
        {"classification": "Invalid", "reason": "Review"},
        {"classification": "Internal"},
        {"classification": "Internal", "reason": "  "},
        {"classification": None, "reason": ""},
        {"classification": None},
        {"reason": "Review"},
    ],
)
def test_invalid_override(client: TestClient, body: dict) -> None:
    candidate_id = seed_candidate(client, [])
    login(client)
    before = stored(client, candidate_id)
    response = client.put(f"/api/v1/candidates/{candidate_id}/classification", json=body)
    assert response.status_code == 422
    assert stored(client, candidate_id) == before


@pytest.mark.parametrize("value", ["Internal", "External", "Unknown"])
def test_override_values_and_empty_email(client: TestClient, value: str) -> None:
    candidate_id = seed_candidate(client, [])
    login(client)
    response = client.put(
        f"/api/v1/candidates/{candidate_id}/classification",
        json={"classification": value, "reason": "  Manual review  "},
    )
    assert response.status_code == 200
    assert response.json()["effectiveClassification"] == value
    assert response.json()["computedClassification"] == "Unknown"
    assert response.json()["overrideReason"] == "Manual review"


def test_override_permissions_csrf_and_missing(client: TestClient) -> None:
    path = "/api/v1/candidates/99999/classification"
    body = {"classification": None, "reason": "Recompute"}
    assert client.put(path, json=body).status_code == 401
    login(client, "worker@example.com", "Analyst")
    assert client.put(path, json=body).status_code == 403
    login(client)
    del client.headers["X-CSRF-Token"]
    assert client.put(path, json=body).status_code == 403
    login(client)
    assert client.put(path, json=body).status_code == 404


def test_classifier_leaves_commit_to_caller(client: TestClient) -> None:
    candidate_id = seed_candidate(client, [])
    with database(client.app.state.settings) as db:
        db.execute("BEGIN")
        assert reclassify_all(db) == 1
        assert db.in_transaction
        db.rollback()
        assert db.execute("SELECT classification FROM candidates").fetchone()[0] == "External"
        assert (
            db.execute(
                "SELECT count(*) FROM audit_log WHERE action='CLASSIFICATION_REEVALUATED'"
            ).fetchone()[0]
            == 0
        )
        reclassify_candidate(db, candidate_id)
        assert db.in_transaction
        db.rollback()
        assert db.execute("SELECT classification FROM candidates").fetchone()[0] == "External"
