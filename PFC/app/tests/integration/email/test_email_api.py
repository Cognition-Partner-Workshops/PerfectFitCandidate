from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email import message_from_bytes
from email.policy import default
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from pfc.adapters.email.transports import (
    DeliveryResult,
    EmailMessage,
    EmailTransportError,
    EmlHandoffTransport,
    FakeTransport,
)
from pfc.adapters.llm.providers import FakeProvider
from pfc.api.routers.auth import router as auth_router
from pfc.api.routers.email import router as email_router
from pfc.api.routers.occ import router as occ_router
from pfc.config import Settings
from pfc.db import audit, users
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.security.middleware import install_middleware
from pfc.security.passwords import hasher
from pfc.security.problems import Problem, install_handlers
from pfc.security.setup import code_path, generate_setup_code
from pfc.services.email import EmailService
from pfc.services.email.service import TEMPLATE_PATH
from pfc.services.occ.templates import PLACEHOLDER_SAMPLES, PLACEHOLDERS

NOW = "2026-01-01T00:00:00Z"
ANALYST_PASSWORD = "AnalystStrong123"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        data_dir=tmp_path,
        allowed_hosts=["testserver"],
        argon_memory_cost=1024,
        argon_time_cost=1,
        argon_parallelism=1,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        migrate(settings)
        generate_setup_code(settings)
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.now = datetime(2026, 1, 1, tzinfo=UTC)
    app.state.clock = lambda: app.state.now
    app.state.dummy_hash = hasher(settings).hash("unused-random-dummy-password")
    install_handlers(app)
    install_middleware(app)
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(email_router, prefix="/api/v1")
    app.include_router(occ_router, prefix="/api/v1")
    app.state.email_transport = FakeTransport()
    with TestClient(app, raise_server_exceptions=False) as result:
        setup = result.post(
            "/api/v1/setup/admin",
            json={
                "setup_code": code_path(settings).read_text(),
                "email": "admin@example.test",
                "display_name": "Admin",
                "password": "Strong!Random4567",
            },
        )
        assert setup.status_code == 201, setup.text
        with database(settings) as db:
            users.create_user(
                db,
                email="analyst@example.test",
                display_name="Analyst",
                role_id=2,
                password_hash=hasher(settings).hash(ANALYST_PASSWORD),
                now=NOW,
                must_change_password=False,
            )
        yield result


def login(client: TestClient) -> str:
    client.cookies.clear()
    response = client.post(
        "/api/v1/auth/login",
        json={
            "role": "Analyst",
            "email": "analyst@example.test",
            "password": ANALYST_PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


def seed_matches(
    client: TestClient,
    emails: list[str | None] | None = None,
    shortlisted: list[bool] | None = None,
    ranks: list[int] | None = None,
) -> tuple[int, list[int]]:
    app = cast(FastAPI, client.app)
    values = emails or [f"candidate-{index}@example.test" for index in range(1, 6)]
    shortlisted_values = shortlisted or [True] * len(values)
    rank_values = ranks or list(range(1, len(values) + 1))
    settings = app.state.settings
    with database(settings) as db:
        template_id = int(
            db.execute(
                "INSERT INTO scoring_templates "
                "(name,version,weights_json,rules_json,status,created_at) "
                "VALUES ('test',1,'{}','{}','active',?)",
                (NOW,),
            ).lastrowid
        )
        embedding_model_id = int(
            db.execute(
                "INSERT INTO embedding_models "
                "(name,runtime,file_sha256,dim,normalised,pipeline_version,is_active) "
                "VALUES ('test','onnx','hash',768,1,'v1',0)"
            ).lastrowid
        )
        run_id = int(
            db.execute(
                "INSERT INTO match_runs "
                "(scope,template_id,embedding_model_id,engine_version,synonyms_version,"
                "config_hash,status) VALUES ('demands',? ,?,'1','test','test','completed')",
                (template_id, embedding_model_id),
            ).lastrowid
        )
        demand_id = int(
            db.execute(
                "INSERT INTO demands "
                "(code_requisition,id_format_ok,demand_request_name,row_hash,"
                "created_at,updated_at) "
                "VALUES ('T10001',1,'Data Engineer','row',?,?)",
                (NOW, NOW),
            ).lastrowid
        )
        version_id = int(
            db.execute(
                "INSERT INTO demand_versions "
                "(demand_id,version_no,jd_text_raw,jd_text_norm,jd_sha256,created_at) "
                "VALUES (?,1,'Seeking Python and SQL.','Seeking Python and SQL.','jd-hash',?)",
                (demand_id, NOW),
            ).lastrowid
        )
        jd_req_id = int(
            db.execute(
                "INSERT INTO jd_requirements "
                "(demand_version_id,revision,status,created_at) VALUES (?,1,'confirmed',?)",
                (version_id, NOW),
            ).lastrowid
        )
        result_ids: list[int] = []
        for index, email in enumerate(values, start=1):
            resource_id = int(
                db.execute(
                    "INSERT INTO resources "
                    "(short_name,email_address,short_name_key,email_key,eligibility,row_hash,"
                    "created_at,updated_at) VALUES (?,?,?,?, 'eligible',?,?,?)",
                    (
                        f"Candidate {index}",
                        email,
                        f"candidate-{index}",
                        email.strip().lower() if email else "",
                        f"resource-{index}",
                        NOW,
                        NOW,
                    ),
                ).lastrowid
            )
            result_ids.append(
                int(
                    db.execute(
                        "INSERT INTO match_results "
                        "(run_id,demand_id,demand_version_id,jd_req_id,resource_id,score,band,"
                        "rank,shortlisted,email_preselected,grade_relation,location_assessment,"
                        "matching_evidence,gaps_checks,eligibility_evidence) "
                        "VALUES (?,?,?,?,?,?,'Strong',?,?,0,'exact','Aligned',?,?,?)",
                        (
                            run_id,
                            demand_id,
                            version_id,
                            jd_req_id,
                            resource_id,
                            95.0 - index,
                            rank_values[index - 1],
                            int(shortlisted_values[index - 1]),
                            "Python and SQL evidence",
                            "Missing dbt experience",
                            "Eligible",
                        ),
                    ).lastrowid
                )
            )
        db.execute(
            "INSERT INTO resource_team_recipients(email,display_name,is_active,added_at) "
            "VALUES ('team@example.test','Resource Team',1,?)",
            (NOW,),
        )
    return run_id, result_ids


def create_drafts(client: TestClient, csrf: str, run_id: int) -> Response:
    return client.post(
        "/api/v1/emails/drafts",
        headers={"X-CSRF-Token": csrf},
        json={"run_id": run_id},
    )


def create_approved_draft(client: TestClient) -> tuple[str, dict[str, Any]]:
    run_id, _ = seed_matches(client)
    csrf = login(client)
    response = create_drafts(client, csrf, run_id)
    assert response.status_code == 201, response.text
    draft = response.json()["drafts"][0]
    approved = client.post(
        f"/api/v1/emails/{draft['message_id']}/approve",
        headers={"X-CSRF-Token": csrf},
    )
    assert approved.status_code == 200, approved.text
    return csrf, draft


def test_create_defaults_to_four_of_top_five_and_lists_counts(client: TestClient) -> None:
    run_id, _ = seed_matches(client)
    csrf = login(client)

    response = create_drafts(client, csrf, run_id)

    assert response.status_code == 201, response.text
    drafts = response.json()["drafts"]
    assert len(drafts) == 5
    assert [draft["selected"] for draft in drafts] == [True, True, True, True, False]
    assert all(draft["status"] == "Draft" for draft in drafts)
    assert all(draft["cc"] == ["team@example.test"] for draft in drafts)
    assert all(draft["attachment_name"] == "demand-1-version-1.txt" for draft in drafts)
    listing = client.get("/api/v1/emails?status=Draft&demand_id=1")
    assert listing.status_code == 200
    assert len(listing.json()["drafts"]) == 5
    assert listing.json()["status_counts"] == {"Draft": 5}
    assert listing.json()["sent_count"] == 0
    assert listing.json()["transport"] == "other"


def test_demand_scope_and_result_filter_select_only_top_five(client: TestClient) -> None:
    _, result_ids = seed_matches(client)
    csrf = login(client)

    response = client.post(
        "/api/v1/emails/drafts",
        headers={"X-CSRF-Token": csrf},
        json={"demand_id": 1, "result_ids": [result_ids[4]]},
    )

    assert response.status_code == 201, response.text
    assert len(response.json()["drafts"]) == 1
    assert response.json()["drafts"][0]["result_id"] == result_ids[4]
    assert response.json()["drafts"][0]["rank"] == 5
    assert response.json()["drafts"][0]["selected"] is False


def test_only_shortlisted_results_get_drafts_and_unshortlisted_ids_fail(
    client: TestClient,
) -> None:
    run_id, result_ids = seed_matches(
        client,
        shortlisted=[True, True, True, False, False],
        ranks=[3, 4, 5, 1, 2],
    )
    csrf = login(client)

    response = create_drafts(client, csrf, run_id)

    assert response.status_code == 201
    assert len(response.json()["drafts"]) == 3
    assert all(draft["selected"] for draft in response.json()["drafts"])
    rejected = client.post(
        "/api/v1/emails/drafts",
        headers={"X-CSRF-Token": csrf},
        json={"run_id": run_id, "result_ids": [result_ids[3]]},
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "EMAIL_RESULT_NOT_TOP_FIVE"


@pytest.mark.parametrize("operation", ["edit", "approve"])
def test_concurrent_send_state_blocks_edit_and_approval(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    run_id, _ = seed_matches(client)
    csrf = login(client)
    draft = create_drafts(client, csrf, run_id).json()["drafts"][0]
    with database(client.app.state.settings) as db:
        user_id = int(
            db.execute("SELECT user_id FROM users WHERE email='analyst@example.test'").fetchone()[0]
        )
    service = EmailService(
        client.app.state.settings,
        cast(FakeTransport, client.app.state.email_transport),
        None,
        client.app.state.clock,
    )
    original_get = service._get_draft_row
    transitioned = False

    def transition_before_update(db: sqlite3.Connection, message_id: int) -> sqlite3.Row | None:
        nonlocal transitioned
        row = original_get(db, message_id)
        if not transitioned:
            db.execute(
                "UPDATE email_messages SET status='Sending' WHERE message_id=?",
                (message_id,),
            )
            db.commit()
            transitioned = True
        return row

    monkeypatch.setattr(service, "_get_draft_row", transition_before_update)
    with pytest.raises(Problem) as error:
        if operation == "edit":
            service.patch_draft(
                draft["message_id"], user_id=user_id, subject="Updated", body_html=None
            )
        else:
            service.approve(draft["message_id"], user_id=user_id)
    assert error.value.status == 409
    with database(client.app.state.settings) as db:
        status = db.execute(
            "SELECT status FROM email_messages WHERE message_id=?",
            (draft["message_id"],),
        ).fetchone()[0]
    assert status == "Sending"


@pytest.mark.parametrize("percentage", [False, True])
def test_occ_template_saved_before_first_draft_is_reused(
    client: TestClient,
    percentage: bool,
) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={
            "role": "Admin",
            "email": "admin@example.test",
            "password": "Strong!Random4567",
        },
    )
    assert response.status_code == 200
    saved = client.post(
        "/api/v1/occ/email-templates",
        headers={"X-CSRF-Token": response.json()["csrf_token"]},
        json={
            "name": "candidate_default",
            "subject": "Saved {candidate_name}" + (" {{{match_pct}}}" if percentage else ""),
            "body_html": (
                "<style>p {{ color: red; }}</style><p>Hello {candidate_name} {{ color: red; }}</p>"
                "<!-- {{internal_note}} -->"
            )
            + ("<p>{{{match_pct}}}</p>" if percentage else ""),
        },
    )
    assert saved.status_code == 201, saved.text
    run_id, _ = seed_matches(client)
    response = create_drafts(client, login(client), run_id)
    assert response.status_code == 201, response.text
    assert all(draft["subject"].startswith("Saved ") for draft in response.json()["drafts"])
    assert all("{ color: red; }" in draft["body_html"] for draft in response.json()["drafts"])
    assert all("internal_note" not in draft["body_html"] for draft in response.json()["drafts"])
    draft = response.json()["drafts"][0]
    if percentage:
        assert "{94.0%}" in draft["subject"]
        assert "{94.0%}" in draft["body_html"]
    csrf = login(client)
    approved = client.post(
        f"/api/v1/emails/{draft['message_id']}/approve",
        headers={"X-CSRF-Token": csrf},
    )
    assert approved.status_code == 200, approved.text
    with database(client.app.state.settings) as db:
        rows = db.execute("SELECT * FROM email_templates").fetchall()
        assert len(rows) == 1
        assert rows[0]["email_template_id"] == saved.json()["email_template_id"]
        assert rows[0]["is_default"] == 1


def test_template_percentage_sample_matches_renderer(client: TestClient) -> None:
    run_id, _ = seed_matches(client)
    service = EmailService(
        client.app.state.settings,
        cast(FakeTransport, client.app.state.email_transport),
        None,
        client.app.state.clock,
    )
    with database(client.app.state.settings) as db:
        result = dict(service._top_results(db, run_id=run_id, demand_id=None)[0])
    result["score"] = 90.0
    subject, body = service._render(
        {"subject": "{match_pct}", "body_html": "<p>{match_pct}</p>"},
        result,
        service._fallback_sections(result),
    )
    assert subject == PLACEHOLDER_SAMPLES["match_pct"] == "90.0%"
    assert body == f"<p>{PLACEHOLDER_SAMPLES['match_pct']}</p>"


def test_default_email_template_uses_occ_placeholders(client: TestClient) -> None:
    run_id, _ = seed_matches(client)
    csrf = login(client)

    created = create_drafts(client, csrf, run_id)

    assert created.status_code == 201, created.text
    with database(client.app.state.settings) as db:
        template = db.execute("SELECT * FROM email_templates WHERE is_default=1").fetchone()
        assert template["name"] == "candidate_default"
        assert template["subject"] == "Opportunity {requisition} – {role}"
        assert template["body_html"] == TEMPLATE_PATH.read_text(encoding="utf-8")
        service = EmailService(
            client.app.state.settings,
            cast(FakeTransport, client.app.state.email_transport),
            None,
            client.app.state.clock,
        )
        result = service._top_results(db, run_id=run_id, demand_id=None)[0]
        values = " ".join(f"{{{placeholder}}}" for placeholder in sorted(PLACEHOLDERS))
        rendered_subject, rendered_body = service._render(
            {"subject": values, "body_html": f"<p>{values}</p>"},
            result,
            service._fallback_sections(result),
        )

    output = f"{rendered_subject}\n{rendered_body}"
    for placeholder in PLACEHOLDERS:
        assert f"{{{placeholder}}}" not in output
    assert "{" not in output and "}" not in output


def test_verbatim_email_and_missing_email_issue(client: TestClient) -> None:
    exact_email = " Candidate <Exact@Example.Test> "
    run_id, _ = seed_matches(
        client,
        [
            exact_email,
            "second@example.test",
            "third@example.test",
            "fourth@example.test",
            None,
        ],
    )
    csrf = login(client)

    response = create_drafts(client, csrf, run_id)

    assert response.status_code == 201, response.text
    assert len(response.json()["drafts"]) == 4
    assert response.json()["drafts"][0]["to_address"] == exact_email
    assert [issue["code"] for issue in response.json()["issues"]] == ["missing_email"]
    transport = cast(FakeTransport, client.app.state.email_transport)
    assert list(transport.drafts.values())[0].to == exact_email


def test_edit_then_send_uses_stored_edited_content(client: TestClient) -> None:
    run_id, _ = seed_matches(client)
    csrf = login(client)
    response = create_drafts(client, csrf, run_id)
    draft = response.json()["drafts"][0]
    transport = cast(FakeTransport, client.app.state.email_transport)
    original = next(iter(transport.drafts.values()))

    edited = client.put(
        f"/api/v1/emails/{draft['message_id']}",
        headers={"X-CSRF-Token": csrf},
        json={"subject": "Analyst subject", "body_html": "<p>Edited body</p>"},
    )
    assert edited.status_code == 200, edited.text
    assert transport.drafts[original.idempotency_key] == original

    approved = client.post(
        f"/api/v1/emails/{draft['message_id']}/approve",
        headers={"X-CSRF-Token": csrf},
    )
    assert approved.status_code == 200
    sent = client.post(
        f"/api/v1/emails/{draft['message_id']}/send",
        headers={"X-CSRF-Token": csrf},
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["sent_count"] == 1
    assert transport.messages == [
        type(original)(
            idempotency_key=original.idempotency_key,
            to=original.to,
            cc=original.cc,
            subject="Analyst subject",
            html_body="<p>Edited body</p>",
            attachments=original.attachments,
            approved=True,
        )
    ]
    with database(client.app.state.settings) as db:
        row = db.execute(
            "SELECT status, subject, body_html, sent_at FROM email_messages WHERE message_id=?",
            (draft["message_id"],),
        ).fetchone()
        actions = {
            item["action"]
            for item in db.execute(
                "SELECT action FROM audit_log WHERE entity_id=?", (str(draft["message_id"]),)
            )
        }
        assert audit.verify_chain(db)
    assert (row["status"], row["subject"], row["body_html"]) == (
        "Sent",
        "Analyst subject",
        "<p>Edited body</p>",
    )
    assert row["sent_at"]
    assert {"EMAIL_EDIT", "EMAIL_APPROVE", "EMAIL_SEND", "EMAIL_SENT"} <= actions
    listing = client.get("/api/v1/emails?status=Sent&demand_id=1")
    assert listing.json()["sent_count"] == 1
    assert listing.json()["status_counts"] == {"Draft": 4, "Sent": 1}


def test_regenerate_replaces_body_and_refreshes_transport_draft(client: TestClient) -> None:
    run_id, _ = seed_matches(client)
    client.app.state.email_llm_provider = FakeProvider(
        json.dumps(
            {
                "analysis": "First analysis",
                "gaps": "First gaps",
                "pointers": "First pointers",
            }
        )
    )
    csrf = login(client)
    draft = create_drafts(client, csrf, run_id).json()["drafts"][0]
    assert "First analysis" in draft["body_html"]

    provider = cast(FakeProvider, client.app.state.email_llm_provider)
    provider.text = json.dumps(
        {"analysis": "New analysis", "gaps": "New gaps", "pointers": "New pointers"}
    )
    regenerated = client.post(
        f"/api/v1/emails/{draft['message_id']}/regenerate",
        headers={"X-CSRF-Token": csrf},
    )

    assert regenerated.status_code == 200, regenerated.text
    assert "New analysis" in regenerated.json()["body_html"]
    assert "First analysis" not in regenerated.json()["body_html"]
    transport = cast(FakeTransport, client.app.state.email_transport)
    key = next(
        key for key, message in transport.drafts.items() if message.to == draft["to_address"]
    )
    assert transport.drafts[key].html_body == regenerated.json()["body_html"]


def test_unapproved_is_never_sent_and_retry_is_idempotent(client: TestClient) -> None:
    run_id, _ = seed_matches(client)
    csrf = login(client)
    draft = create_drafts(client, csrf, run_id).json()["drafts"][0]
    transport = cast(FakeTransport, client.app.state.email_transport)

    unapproved = client.post(
        "/api/v1/emails/send-approved",
        headers={"X-CSRF-Token": csrf},
        json={"draft_ids": [draft["message_id"]]},
    )
    assert unapproved.status_code == 200
    assert unapproved.json()["skipped_count"] == 1
    assert transport.messages == []

    assert (
        client.post(
            f"/api/v1/emails/{draft['message_id']}/approve",
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 200
    )
    first = client.post(
        "/api/v1/emails/send-approved",
        headers={"X-CSRF-Token": csrf},
        json={"draft_ids": [draft["message_id"]]},
    )
    second = client.post(
        "/api/v1/emails/send-approved",
        headers={"X-CSRF-Token": csrf},
        json={"draft_ids": [draft["message_id"]]},
    )
    assert first.json()["sent_count"] == 1
    assert second.json()["skipped_count"] == 1
    assert len(transport.messages) == 1


def test_failed_send_is_recorded_and_retry_reuses_the_idempotency_key(
    client: TestClient,
) -> None:
    class FailOnceTransport(FakeTransport):
        failed = False

        def send(self, message: EmailMessage) -> DeliveryResult:
            if not self.failed:
                self.failed = True
                raise EmailTransportError("Outlook is unavailable")
            return super().send(message)

    transport = FailOnceTransport()
    client.app.state.email_transport = transport
    run_id, _ = seed_matches(client)
    csrf = login(client)
    draft = create_drafts(client, csrf, run_id).json()["drafts"][0]
    assert (
        client.post(
            f"/api/v1/emails/{draft['message_id']}/approve",
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 200
    )
    payload = {"draft_ids": [draft["message_id"]]}

    failed = client.post(
        "/api/v1/emails/send-approved", headers={"X-CSRF-Token": csrf}, json=payload
    )
    with database(client.app.state.settings) as db:
        row = db.execute(
            "SELECT status, attempts, idempotency_key, last_error FROM email_messages "
            "WHERE message_id=?",
            (draft["message_id"],),
        ).fetchone()
    assert failed.json()["failed_count"] == 1
    assert row["status"] == "Failed"
    assert row["attempts"] == 1
    assert row["last_error"] == "Outlook is unavailable"
    idempotency_key = row["idempotency_key"]

    retried = client.post(
        "/api/v1/emails/send-approved", headers={"X-CSRF-Token": csrf}, json=payload
    )

    assert retried.json()["sent_count"] == 1
    assert len(transport.messages) == 1
    assert transport.messages[0].idempotency_key == idempotency_key
    with database(client.app.state.settings) as db:
        assert (
            db.execute(
                "SELECT attempts FROM email_messages WHERE message_id=?",
                (draft["message_id"],),
            ).fetchone()[0]
            == 2
        )


def test_llm_failure_uses_deterministic_template(client: TestClient) -> None:
    class BrokenProvider(FakeProvider):
        async def generate(self, request, *, api_version=None):
            raise RuntimeError("model unavailable")

    run_id, _ = seed_matches(client)
    client.app.state.email_llm_provider = BrokenProvider()
    csrf = login(client)

    response = create_drafts(client, csrf, run_id)

    assert response.status_code == 201, response.text
    draft = response.json()["drafts"][0]
    assert draft["drafted_by_model"] == "template"
    assert "Missing dbt experience" in draft["body_html"]
    assert "Add accurate profile or CV evidence" in draft["body_html"]


def test_rbac_and_csrf_are_enforced(client: TestClient) -> None:
    run_id, _ = seed_matches(client)

    unauthenticated = client.post("/api/v1/emails/drafts", json={"run_id": run_id})
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["content-type"] == "application/problem+json"

    csrf = login(client)
    missing_csrf = create_drafts(client, csrf="", run_id=run_id)
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["code"] == "CSRF_INVALID"
    permitted = create_drafts(client, csrf, run_id)
    assert permitted.status_code == 201


def test_eml_handoff_requires_approval_and_serves_audited_draft(client: TestClient) -> None:
    opened: list[Path] = []
    client.app.state.email_transport = EmlHandoffTransport(opener=opened.append)
    run_id, _ = seed_matches(client)
    csrf = login(client)
    draft = create_drafts(client, csrf, run_id).json()["drafts"][0]
    message_id = draft["message_id"]
    download_path = f"/api/v1/emails/{message_id}/handoff.eml"

    rejected = client.post(
        f"/api/v1/emails/{message_id}/handoff",
        headers={"X-CSRF-Token": csrf},
    )
    assert rejected.status_code == 409
    assert client.get(download_path).status_code == 409

    approved = client.post(
        f"/api/v1/emails/{message_id}/approve",
        headers={"X-CSRF-Token": csrf},
    )
    assert approved.status_code == 200
    handed_off = client.post(
        f"/api/v1/emails/{message_id}/handoff",
        headers={"X-CSRF-Token": csrf},
    )

    assert handed_off.status_code == 200, handed_off.text
    assert handed_off.json()["opened"] is True
    assert handed_off.json()["draft"]["status"] == "Sending"
    assert handed_off.json()["draft"]["handoff_at"]
    outbox_path = client.app.state.settings.data_dir / "email" / "outbox" / f"{message_id}.eml"
    assert message_from_bytes(outbox_path.read_bytes(), policy=default)["X-Unsent"] == "1"

    outbox_path.write_bytes(b"stale draft")
    opened_again = client.post(
        f"/api/v1/emails/{message_id}/handoff",
        headers={"X-CSRF-Token": csrf},
    )
    assert opened_again.status_code == 200
    assert opened_again.json()["draft"]["handoff_at"] == handed_off.json()["draft"]["handoff_at"]
    assert opened == [outbox_path, outbox_path]
    assert message_from_bytes(outbox_path.read_bytes(), policy=default)["X-Unsent"] == "1"
    downloaded = client.get(download_path)
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "message/rfc822"
    assert downloaded.headers["content-disposition"] == (
        f'attachment; filename="PFC-email-{message_id}.eml"'
    )
    assert message_from_bytes(downloaded.content, policy=default)["X-Unsent"] == "1"
    with database(client.app.state.settings) as db:
        audit_rows = db.execute(
            "SELECT action, details_json FROM audit_log WHERE entity_id=? "
            "AND action='EMAIL_HANDOFF' ORDER BY audit_id",
            (str(message_id),),
        ).fetchall()
        persisted = db.execute(
            "SELECT status, attempts, handoff_at FROM email_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
        assert audit.verify_chain(db)
    assert [row["action"] for row in audit_rows] == ["EMAIL_HANDOFF", "EMAIL_HANDOFF"]
    assert [json.loads(row["details_json"])["repeat"] for row in audit_rows] == [False, True]
    assert tuple(persisted) == ("Sending", 1, handed_off.json()["draft"]["handoff_at"])


def test_eml_handoff_rejects_a_non_manual_transport(client: TestClient) -> None:
    csrf, draft = create_approved_draft(client)

    response = client.post(
        f"/api/v1/emails/{draft['message_id']}/handoff",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "EMAIL_TRANSPORT_MISMATCH"


def test_eml_handoff_requires_confirmation_after_failed_com_attempt(client: TestClient) -> None:
    client.app.state.email_transport = EmlHandoffTransport()
    csrf, draft = create_approved_draft(client)
    message_id = draft["message_id"]
    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE email_messages SET status='Failed', transport='outlook_com', attempts=1 "
            "WHERE message_id=?",
            (message_id,),
        )

    response = client.post(
        f"/api/v1/emails/{message_id}/handoff",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "EMAIL_PREVIOUS_SEND_UNCERTAIN"
    assert response.json()["detail"] == (
        "An earlier Outlook COM send attempt failed and may have reached the recipient. "
        "Check Outlook Sent Items before opening a new draft."
    )
    with database(client.app.state.settings) as db:
        row = db.execute(
            "SELECT status, transport, attempts, handoff_at FROM email_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
        handoffs = db.execute(
            "SELECT count(*) FROM audit_log WHERE entity_id=? AND action='EMAIL_HANDOFF'",
            (str(message_id),),
        ).fetchone()[0]
    assert tuple(row) == ("Failed", "outlook_com", 1, None)
    assert handoffs == 0


def test_eml_handoff_requires_confirmation_after_edit_and_reapproval(client: TestClient) -> None:
    client.app.state.email_transport = EmlHandoffTransport()
    csrf, draft = create_approved_draft(client)
    message_id = draft["message_id"]
    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE email_messages SET status='Failed', transport='outlook_com', attempts=1 "
            "WHERE message_id=?",
            (message_id,),
        )

    edited = client.put(
        f"/api/v1/emails/{message_id}",
        headers={"X-CSRF-Token": csrf},
        json={"subject": "Reviewed subject"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "Draft"
    with database(client.app.state.settings) as db:
        row = db.execute(
            "SELECT status, transport FROM email_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
    assert tuple(row) == ("Draft", "outlook_com")
    approved = client.post(
        f"/api/v1/emails/{message_id}/approve",
        headers={"X-CSRF-Token": csrf},
    )
    assert approved.status_code == 200, approved.text

    response = client.post(
        f"/api/v1/emails/{message_id}/handoff",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "EMAIL_PREVIOUS_SEND_UNCERTAIN"
    with database(client.app.state.settings) as db:
        row = db.execute(
            "SELECT status, transport, attempts, handoff_at FROM email_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
    assert tuple(row) == ("Approved", "outlook_com", 1, None)


def test_approved_com_row_requires_confirmation_before_handoff(client: TestClient) -> None:
    client.app.state.email_transport = EmlHandoffTransport()
    csrf, draft = create_approved_draft(client)
    message_id = draft["message_id"]
    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE email_messages SET transport='outlook_com' WHERE message_id=?",
            (message_id,),
        )

    response = client.post(
        f"/api/v1/emails/{message_id}/handoff",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "EMAIL_PREVIOUS_SEND_UNCERTAIN"
    with database(client.app.state.settings) as db:
        row = db.execute(
            "SELECT status, transport, attempts, handoff_at FROM email_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
    assert tuple(row) == ("Approved", "outlook_com", 0, None)


def test_eml_handoff_confirms_uncertain_com_attempt_and_audits_it(client: TestClient) -> None:
    client.app.state.email_transport = EmlHandoffTransport()
    csrf, draft = create_approved_draft(client)
    message_id = draft["message_id"]
    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE email_messages SET status='Failed', transport='outlook_com', attempts=1 "
            "WHERE message_id=?",
            (message_id,),
        )

    response = client.post(
        f"/api/v1/emails/{message_id}/handoff",
        headers={"X-CSRF-Token": csrf},
        json={"confirm_uncertain": True},
    )

    assert response.status_code == 200, response.text
    assert response.json()["draft"]["status"] == "Sending"
    assert response.json()["draft"]["handoff_at"]
    with database(client.app.state.settings) as db:
        row = db.execute(
            "SELECT status, transport, attempts, handoff_at FROM email_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
        event = db.execute(
            "SELECT details_json FROM audit_log WHERE entity_id=? AND action='EMAIL_HANDOFF'",
            (str(message_id),),
        ).fetchone()
    assert tuple(row) == (
        "Sending",
        None,
        2,
        response.json()["draft"]["handoff_at"],
    )
    assert json.loads(event["details_json"])["previous_com_attempt"] is True


def test_eml_handoff_failed_without_com_transport_needs_no_confirmation(client: TestClient) -> None:
    client.app.state.email_transport = EmlHandoffTransport()
    csrf, draft = create_approved_draft(client)
    message_id = draft["message_id"]
    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE email_messages SET status='Failed', transport=NULL, attempts=1 "
            "WHERE message_id=?",
            (message_id,),
        )

    response = client.post(
        f"/api/v1/emails/{message_id}/handoff",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200, response.text
    assert response.json()["draft"]["status"] == "Sending"
    assert response.json()["draft"]["handoff_at"]
    with database(client.app.state.settings) as db:
        event = db.execute(
            "SELECT details_json FROM audit_log WHERE entity_id=? AND action='EMAIL_HANDOFF'",
            (str(message_id),),
        ).fetchone()
    assert "previous_com_attempt" not in json.loads(event["details_json"])


def test_previous_com_send_can_be_confirmed_as_sent(client: TestClient) -> None:
    csrf, draft = create_approved_draft(client)
    message_id = draft["message_id"]
    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE email_messages SET status='Failed', transport='outlook_com', attempts=1, "
            "last_error='Outlook response was ambiguous' WHERE message_id=?",
            (message_id,),
        )

    response = client.post(
        f"/api/v1/emails/{message_id}/previous-send/confirm",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "Sent"
    assert response.json()["sent_at"]
    with database(client.app.state.settings) as db:
        row = db.execute(
            "SELECT status, sent_by, sent_at, last_error, transport FROM email_messages "
            "WHERE message_id=?",
            (message_id,),
        ).fetchone()
        event = db.execute(
            "SELECT action, details_json FROM audit_log WHERE entity_id=? AND action='EMAIL_SENT'",
            (str(message_id),),
        ).fetchone()
        assert audit.verify_chain(db)
    assert row["status"] == "Sent"
    assert row["sent_by"] is not None
    assert row["sent_at"] == response.json()["sent_at"]
    assert row["last_error"] is None
    assert row["transport"] == "outlook_com"
    assert event["action"] == "EMAIL_SENT"
    assert json.loads(event["details_json"]) == {
        "status": "Sent",
        "transport": "outlook_com",
        "confirmed_by_analyst": True,
        "previous_com_attempt": True,
    }

    second = client.post(
        f"/api/v1/emails/{message_id}/previous-send/confirm",
        headers={"X-CSRF-Token": csrf},
    )
    assert second.status_code == 409
    assert second.json()["code"] == "EMAIL_NO_UNCERTAIN_SEND"


def test_previous_com_send_confirmation_rejects_null_transport(client: TestClient) -> None:
    csrf, draft = create_approved_draft(client)
    message_id = draft["message_id"]
    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE email_messages SET status='Failed', transport=NULL WHERE message_id=?",
            (message_id,),
        )

    response = client.post(
        f"/api/v1/emails/{message_id}/previous-send/confirm",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "EMAIL_NO_UNCERTAIN_SEND"


def test_eml_handoff_attachment_failure_keeps_handoff_active(client: TestClient) -> None:
    client.app.state.email_transport = EmlHandoffTransport()
    csrf, draft = create_approved_draft(client)
    message_id = draft["message_id"]

    with database(client.app.state.settings) as db:
        attachment = db.execute(
            "SELECT jd_attachment_path FROM email_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()["jd_attachment_path"]
    (client.app.state.settings.data_dir / attachment).unlink()

    failed = client.post(
        f"/api/v1/emails/{message_id}/handoff",
        headers={"X-CSRF-Token": csrf},
    )
    assert failed.status_code == 503
    assert failed.json()["code"] == "EMAIL_HANDOFF_FAILED"
    unavailable = client.get(f"/api/v1/emails/{message_id}/handoff.eml")
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "EMAIL_HANDOFF_FAILED"
    with database(client.app.state.settings) as db:
        row = db.execute(
            "SELECT status, handoff_at FROM email_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
    assert row["status"] == "Sending"
    assert row["handoff_at"] is not None

    cancelled = client.post(
        f"/api/v1/emails/{message_id}/handoff/confirm",
        headers={"X-CSRF-Token": csrf},
        json={"sent": False},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "Approved"
    assert cancelled.json()["handoff_at"] is None


def test_eml_handoff_confirmation_succeeds_when_outbox_cleanup_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.app.state.email_transport = EmlHandoffTransport()
    csrf, draft = create_approved_draft(client)
    message_id = draft["message_id"]
    handed_off = client.post(
        f"/api/v1/emails/{message_id}/handoff",
        headers={"X-CSRF-Token": csrf},
    )
    assert handed_off.status_code == 200
    outbox_path = client.app.state.settings.data_dir / "email" / "outbox" / f"{message_id}.eml"
    original_unlink = Path.unlink

    def fail_outbox_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == outbox_path:
            raise PermissionError("draft file is locked")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_outbox_unlink)
    confirmed = client.post(
        f"/api/v1/emails/{message_id}/handoff/confirm",
        headers={"X-CSRF-Token": csrf},
        json={"sent": True},
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "Sent"
    with database(client.app.state.settings) as db:
        row = db.execute(
            "SELECT status FROM email_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
        event = db.execute(
            "SELECT action, details_json FROM audit_log WHERE entity_id=? AND action='EMAIL_SENT'",
            (str(message_id),),
        ).fetchone()
        assert audit.verify_chain(db)
    assert row["status"] == "Sent"
    assert event["action"] == "EMAIL_SENT"
    assert json.loads(event["details_json"])["confirmed_by_analyst"] is True


def test_eml_handoff_confirmation_survives_transport_change(client: TestClient) -> None:
    client.app.state.email_transport = EmlHandoffTransport()
    csrf, draft = create_approved_draft(client)
    message_id = draft["message_id"]
    handed_off = client.post(
        f"/api/v1/emails/{message_id}/handoff",
        headers={"X-CSRF-Token": csrf},
    )
    assert handed_off.status_code == 200

    client.app.state.email_transport = FakeTransport()
    confirmed = client.post(
        f"/api/v1/emails/{message_id}/handoff/confirm",
        headers={"X-CSRF-Token": csrf},
        json={"sent": False},
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "Approved"
    assert confirmed.json()["handoff_at"] is None


@pytest.mark.parametrize("sent", [True, False])
def test_eml_handoff_confirmation_cleans_outbox_and_updates_state(
    client: TestClient, sent: bool
) -> None:
    client.app.state.email_transport = EmlHandoffTransport()
    csrf, draft = create_approved_draft(client)
    message_id = draft["message_id"]
    outbox_path = client.app.state.settings.data_dir / "email" / "outbox" / f"{message_id}.eml"
    inactive_confirm = client.post(
        f"/api/v1/emails/{message_id}/handoff/confirm",
        headers={"X-CSRF-Token": csrf},
        json={"sent": True},
    )
    assert inactive_confirm.status_code == 409
    handed_off = client.post(
        f"/api/v1/emails/{message_id}/handoff",
        headers={"X-CSRF-Token": csrf},
    )
    assert handed_off.status_code == 200
    confirmed = client.post(
        f"/api/v1/emails/{message_id}/handoff/confirm",
        headers={"X-CSRF-Token": csrf},
        json={"sent": sent},
    )

    assert confirmed.status_code == 200, confirmed.text
    assert not outbox_path.exists()
    expected_status = "Sent" if sent else "Approved"
    assert confirmed.json()["status"] == expected_status
    assert confirmed.json()["handoff_at"] == (
        handed_off.json()["draft"]["handoff_at"] if sent else None
    )
    with database(client.app.state.settings) as db:
        row = db.execute(
            "SELECT status, sent_at, sent_by, handoff_at FROM email_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
        event = db.execute(
            "SELECT action, details_json FROM audit_log WHERE entity_id=? "
            "AND action IN ('EMAIL_SENT','EMAIL_HANDOFF_CANCELLED') ORDER BY audit_id DESC LIMIT 1",
            (str(message_id),),
        ).fetchone()
        user_id = db.execute(
            "SELECT user_id FROM users WHERE email='analyst@example.test'"
        ).fetchone()[0]
        assert audit.verify_chain(db)
    assert row["status"] == expected_status
    assert row["handoff_at"] == (handed_off.json()["draft"]["handoff_at"] if sent else None)
    if sent:
        assert row["sent_at"] and row["sent_by"] == user_id
        assert event["action"] == "EMAIL_SENT"
        assert json.loads(event["details_json"]) == {
            "confirmed_by_analyst": True,
            "status": "Sent",
            "transport": "eml_handoff",
        }
        assert (
            client.post(
                f"/api/v1/emails/{message_id}/handoff",
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 409
        )
        resent = client.post(
            f"/api/v1/emails/{message_id}/resend",
            headers={"X-CSRF-Token": csrf},
        )
        assert resent.status_code == 200, resent.text
        assert resent.json()["status"] == "Draft"
    else:
        assert event["action"] == "EMAIL_HANDOFF_CANCELLED"
        assert json.loads(event["details_json"]) == {"status": "Approved"}
        opened_again = client.post(
            f"/api/v1/emails/{message_id}/handoff",
            headers={"X-CSRF-Token": csrf},
        )
        assert opened_again.status_code == 200
        assert opened_again.json()["draft"]["status"] == "Sending"


def test_send_and_send_approved_skip_manual_handoff_without_state_changes(
    client: TestClient,
) -> None:
    client.app.state.email_transport = EmlHandoffTransport()
    csrf, draft = create_approved_draft(client)
    message_id = draft["message_id"]
    send_url = f"/api/v1/emails/{message_id}/send"

    single = client.post(send_url, headers={"X-CSRF-Token": csrf})
    assert single.status_code == 200
    assert single.json()["outcomes"][0]["detail"] == (
        "Use Open in Outlook for the draft file transport"
    )
    batch = client.post(
        "/api/v1/emails/send-approved",
        headers={"X-CSRF-Token": csrf},
        json={"draft_ids": [message_id]},
    )
    assert batch.status_code == 200
    assert batch.json()["outcomes"][0]["detail"] == (
        "Use Open in Outlook for the draft file transport"
    )
    with database(client.app.state.settings) as db:
        before = db.execute(
            "SELECT status, attempts, handoff_at FROM email_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
    assert tuple(before) == ("Approved", 0, None)

    handed_off = client.post(
        f"/api/v1/emails/{message_id}/handoff",
        headers={"X-CSRF-Token": csrf},
    )
    assert handed_off.status_code == 200
    pending = client.post(
        "/api/v1/emails/send-approved",
        headers={"X-CSRF-Token": csrf},
        json={"draft_ids": [message_id]},
    )
    assert pending.status_code == 200
    assert pending.json()["outcomes"][0]["detail"] == (
        "Waiting for confirmation that the Outlook draft was sent"
    )
    with database(client.app.state.settings) as db:
        after = db.execute(
            "SELECT status, attempts, handoff_at FROM email_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
    assert tuple(after) == ("Sending", 1, handed_off.json()["draft"]["handoff_at"])


@pytest.mark.parametrize("operation", ["edit", "approve"])
def test_edit_and_approve_are_blocked_during_eml_handoff(
    client: TestClient, operation: str
) -> None:
    client.app.state.email_transport = EmlHandoffTransport()
    csrf, draft = create_approved_draft(client)
    message_id = draft["message_id"]
    assert (
        client.post(
            f"/api/v1/emails/{message_id}/handoff",
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 200
    )

    if operation == "edit":
        response = client.put(
            f"/api/v1/emails/{message_id}",
            headers={"X-CSRF-Token": csrf},
            json={"subject": "Changed"},
        )
    else:
        response = client.post(
            f"/api/v1/emails/{message_id}/approve",
            headers={"X-CSRF-Token": csrf},
        )
    assert response.status_code == 409


def test_default_email_transport_and_invalid_setting(client: TestClient) -> None:
    login(client)
    client.app.state.email_transport = None

    default_response = client.get("/api/v1/emails")
    assert default_response.status_code == 200
    assert default_response.json()["transport"] == "eml_handoff"
    assert isinstance(client.app.state.email_handoff_transport, EmlHandoffTransport)

    with database(client.app.state.settings) as db:
        db.execute(
            "INSERT INTO settings(scope,key,value_json,admin_only,updated_at) "
            "VALUES ('system','email.transport',?,1,?)",
            (json.dumps("outlook_com"), NOW),
        )
    selected = client.get("/api/v1/emails")
    assert selected.status_code == 200
    assert selected.json()["transport"] == "outlook_com"
    assert client.app.state.email_com_transport._outlook is None

    client.cookies.clear()
    admin_login = client.post(
        "/api/v1/auth/login",
        json={
            "role": "Admin",
            "email": "admin@example.test",
            "password": "Strong!Random4567",
        },
    )
    assert admin_login.status_code == 200
    invalid = client.put(
        "/api/v1/occ/settings",
        headers={"X-CSRF-Token": admin_login.json()["csrf_token"]},
        json={"email.transport": "smtp"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "VALIDATION_ERROR"


def test_switch_to_eml_transport_rejects_in_flight_com_send(client: TestClient) -> None:
    _, draft = create_approved_draft(client)
    message_id = draft["message_id"]
    with database(client.app.state.settings) as db:
        db.execute(
            "UPDATE email_messages SET status='Sending', handoff_at=NULL WHERE message_id=?",
            (message_id,),
        )
        db.execute(
            "INSERT INTO settings(scope,key,value_json,admin_only,updated_at) "
            "VALUES ('system','email.transport',?,1,?)",
            (json.dumps("outlook_com"), NOW),
        )

    client.cookies.clear()
    admin_login = client.post(
        "/api/v1/auth/login",
        json={
            "role": "Admin",
            "email": "admin@example.test",
            "password": "Strong!Random4567",
        },
    )
    assert admin_login.status_code == 200
    response = client.put(
        "/api/v1/occ/settings",
        headers={"X-CSRF-Token": admin_login.json()["csrf_token"]},
        json={"email.transport": "eml_handoff"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "EMAIL_TRANSPORT_BUSY"
    assert response.json()["detail"] == (
        "Finish or retry the in-flight Outlook COM sends before switching to the Outlook draft file"
    )
    with database(client.app.state.settings) as db:
        transport = db.execute(
            "SELECT value_json FROM settings WHERE scope='system' AND key='email.transport'"
        ).fetchone()[0]
    assert json.loads(transport) == "outlook_com"
