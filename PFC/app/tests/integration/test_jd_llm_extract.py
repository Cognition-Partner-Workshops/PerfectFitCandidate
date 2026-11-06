"""Shared local provider extraction, safe fallback and deterministic scoring."""

import asyncio
from dataclasses import replace

import pytest
from test_matching_api import Api, finish
from test_matching_api import api as api
from test_matching_service import FakeLlm, seed_demand, seed_resource

from pfc.adapters.llm.providers import (
    Capabilities,
    FakeProvider,
    GenerateResult,
    Health,
    PromptRequest,
)
from pfc.api.routers import match
from pfc.db.connection import database
from pfc.services.matching import MatchingPorts
from pfc.services.matching.llm_bridge import JdLlmBridge, jd_generation_schema


class Provider(FakeProvider):
    def __init__(self, mode: str = "ready") -> None:
        super().__init__()
        self.mode = mode
        self.requests: list[PromptRequest] = []
        self.health_calls = 0

    def capabilities(self) -> Capabilities:
        capabilities = super().capabilities()
        if self.mode == "external":
            return replace(capabilities, data_residency="external")
        if self.mode == "unconstrained":
            return replace(capabilities, json_schema=False)
        return capabilities

    async def health(self) -> Health:
        self.health_calls += 1
        if self.mode == "health_timeout":
            await asyncio.sleep(10)
        return Health("down" if self.mode == "down" else "ok", "shared-local")

    async def generate(
        self, request: PromptRequest, *, api_version: str | None = None
    ) -> GenerateResult:
        self.requests.append(request)
        if self.mode == "error":
            raise RuntimeError("Do not expose provider error details")
        if self.mode == "timeout":
            await asyncio.sleep(10)
        text = {
            "invalid_json": "not json",
            "empty": "",
            "nonfinite": '{"experience":[{"text":"Experience","years":Infinity}]}',
            "wrong_type": '{"experience":[{"text":"Experience","years":true}]}',
            "schema_invalid": '{"mandatory_skills":[{"text":" "}]}',
            "extra_field": '{"invented":true}',
            "array": "[]",
            "empty_requirements": "{}",
        }.get(self.mode, '{"mandatory_skills":[{"text":"SQL","evidence":"Required: SQL"}]}')
        return GenerateResult(
            text, "llamacpp", "rule_based" if self.mode == "reserved_model" else "shared-local"
        )


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("ready", None),
        ("reserved_model", None),
        ("empty_requirements", None),
        ("missing", "provider_unavailable"),
        ("down", "provider_not_ready"),
        ("invalid_json", "invalid_json"),
        ("empty", "invalid_json"),
        ("nonfinite", "invalid_json"),
        ("wrong_type", "schema_invalid"),
        ("schema_invalid", "schema_invalid"),
        ("extra_field", "schema_invalid"),
        ("array", "schema_invalid"),
        ("error", "provider_error"),
        ("timeout", "provider_timeout"),
        ("health_timeout", "provider_timeout"),
        ("external", "local_provider_required"),
        ("unconstrained", "structured_output_unavailable"),
    ],
)
def test_shared_provider_extraction(
    api: Api, monkeypatch: pytest.MonkeyPatch, mode: str, reason: str | None
) -> None:
    client, headers = api
    provider = Provider(mode)
    client.app.state.llm_provider = None if mode == "missing" else provider
    client.app.state.matching_ports = MatchingPorts()
    monkeypatch.setattr(match, "JdLlmBridge", lambda shared: JdLlmBridge(shared, timeout_s=0.1))
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
    url = f"/api/v1/demands/{demand}"
    response = client.post(url + "/analyze", headers=headers)
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["status"] == "draft" and draft["confirmedAt"] is None
    assert draft["extractionMethod"] == ("llm" if reason is None else "rule_based_fallback")
    assert draft["fallbackReason"] == reason
    assert draft["warnings"] == ([] if reason is None else [f"Rule-based fallback: {reason}"])
    model = "rule_based" if mode == "reserved_model" else "shared-local"
    assert draft["extractedBy"] == (model if reason is None else "rule_based")
    assert client.get(url + "/requirements").json() == draft
    if mode in {"down", "missing", "external", "unconstrained", "health_timeout"}:
        assert provider.requests == []
    else:
        request = provider.requests[0]
        assert request.json_schema == jd_generation_schema()
        assert request.purpose == "jd_extract" and request.temperature == 0
        assert request.messages[-1]["content"] == "Required: SQL\nPreferred: Python"
    if reason is not None:
        assert draft["mandatorySkills"] == ["SQL"] and draft["preferredSkills"] == ["Python"]
    with database(client.app.state.settings) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM jd_requirements WHERE status='confirmed'").fetchone()[
                0
            ]
            == 0
        )


def test_provider_replacement_and_injected_port_precedence(api: Api) -> None:
    client, headers = api
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
    url = f"/api/v1/demands/{demand}/analyze"
    first, second = Provider("down"), Provider()
    client.app.state.llm_provider = first
    assert client.post(url, headers=headers).json()["extractionMethod"] == "rule_based_fallback"
    client.app.state.llm_provider = second
    assert client.post(url, headers=headers).json()["extractionMethod"] == "llm"
    assert first.requests == [] and len(second.requests) == 1
    ports = client.app.state.matching_ports
    client.app.state.matching_ports = MatchingPorts(ports.embedder, FakeLlm(), ports.vector_store)
    assert client.post(url, headers=headers).json()["extractedBy"] == "test-llm-v1"
    assert len(second.requests) == 1


def test_confirmation_required_and_matching_makes_zero_llm_calls(api: Api) -> None:
    client, headers = api
    provider = Provider()
    client.app.state.llm_provider = provider
    with database(client.app.state.settings) as db:
        demand = seed_demand(db, confirmed=False)
        seed_demand(db, "T1CONFIRMED")
        for index in range(3):
            seed_resource(db, f"Person{index}")
    response = client.post(f"/api/v1/demands/{demand}/analyze", headers=headers)
    assert response.status_code == 200 and response.json()["extractionMethod"] == "llm"
    assert len(provider.requests) == 1
    provider.requests.clear()
    provider.health_calls = 0
    run = finish(client, client.post("/api/v1/match-runs", headers=headers, json={}))
    with database(client.app.state.settings) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM match_results WHERE run_id=?", (run,)).fetchone()[0]
            > 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM match_results WHERE run_id=? AND demand_id=?", (run, demand)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT status FROM jd_requirements WHERE jd_req_id=?",
                (response.json()["jdReqId"],),
            ).fetchone()[0]
            == "draft"
        )
    finish(client, client.post(f"/api/v1/demands/{demand}/requirements/confirm", headers=headers))
    with database(client.app.state.settings) as db:
        assert (
            db.execute(
                "SELECT status FROM jd_requirements WHERE jd_req_id=?",
                (response.json()["jdReqId"],),
            ).fetchone()[0]
            == "confirmed"
        )
    assert provider.requests == [] and provider.health_calls == 0
