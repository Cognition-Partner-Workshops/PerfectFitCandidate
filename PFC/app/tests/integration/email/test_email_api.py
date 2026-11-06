from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pfc.adapters.email.transports import (
    DeliveryResult,
    EmailMessage,
    EmailTransportError,
    FakeTransport,
)
from pfc.adapters.llm.providers import FakeProvider
from pfc.api.routers.auth import router as auth_router
from pfc.api.routers.email import router as email_router
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
from pfc.services.occ.templates import PLACEHOLDERS

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


def create_drafts(client: TestClient, csrf: str, run_id: int):
    return client.post(
        "/api/v1/emails/drafts",
        headers={"X-CSRF-Token": csrf},
        json={"run_id": run_id},
    )


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

    def transition_before_update(db, message_id):
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
