"""REQ-IN-21/23: only evidence-grounded facts survive rules, local generation and merge."""

import asyncio
import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from pfc.adapters.llm.providers import FakeProvider, GenerateResult, Health, PromptRequest
from pfc.api.schemas.cv import CVFacts
from pfc.domain.normalise.text import normalise_text
from pfc.services.ingestion.cv_facts import (
    KEYS,
    LLMBridge,
    extract_facts,
    public_facts,
    rule_facts,
    validate_facts,
)
from pfc.services.ingestion.masking import mask_phone

TEXT = normalise_text(
    "Name: Alex Border\nPhone: +44 (0)20 7946 0958\nAlEx@example.com\n"
    "Work Experience\n• Engineer at Example 2020-2024\nEducation\nBSc Computing\n"
    "Projects\n- Atlas delivery\nSkills\nSQL"
)


class Context:
    def raise_if_cancelled(self) -> None:
        pass


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    selected = asyncio.new_event_loop()
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(selected)
        selected.call_soon(ready.set)
        selected.run_forever()

    thread = threading.Thread(target=run)
    thread.start()
    ready.wait()
    yield selected
    selected.call_soon_threadsafe(selected.stop)
    thread.join()
    selected.close()


def all_facts(facts: CVFacts) -> list[dict[str, Any]]:
    data = facts.model_dump()
    return [
        item
        for key in KEYS
        for item in (([data[key]] if data[key] else []) if key in ("name", "phone") else data[key])
    ]


def generated(**updates: Any) -> str:
    return json.dumps(
        {
            "name": None,
            "phone": None,
            "experience": [],
            "qualifications": [],
            "projects": [],
            **updates,
        }
    )


def test_req_in_23_rule_spans() -> None:
    facts = rule_facts(TEXT)
    assert facts.name is not None and facts.name.value == "Alex Border"
    assert facts.phone is not None and facts.phone.value == "+44 (0)20 7946 0958"
    assert [fact.value for fact in facts.experience] == ["Engineer at Example 2020-2024"]
    assert [fact.value for fact in facts.qualifications] == ["BSc Computing"]
    assert [fact.value for fact in facts.projects] == ["Atlas delivery"]
    for fact in all_facts(facts):
        start, end = fact["span"]
        assert TEXT[start:end] == fact["evidence"]
        assert fact["value"] in fact["evidence"]
        assert fact["source"] == "rule"


@pytest.mark.parametrize(
    "phone", ["1234567", "020 7946 0958", "+123456789012345", "(212) 555-0199"]
)
def test_req_in_23_phone_formats(phone: str) -> None:
    found = rule_facts(f"Contact\n{phone}").phone
    assert found is not None and found.value == phone


@pytest.mark.parametrize(
    "text", ["", "alice@example.com", "123456", "+1234567890123456", "2020-2024"]
)
def test_req_in_23_missing_identity_is_null(text: str) -> None:
    facts = rule_facts(text)
    assert facts.name is None and facts.phone is None


def test_req_in_23_leading_name_and_headings() -> None:
    facts = rule_facts(
        "Alex Border\nEmployment\nEngineer\nCertifications:\nCertificate\nProjects\nAtlas"
    )
    assert facts.name is not None and facts.name.value == "Alex Border"
    assert [item.value for item in facts.experience] == ["Engineer"]
    assert [item.value for item in facts.qualifications] == ["Certificate"]
    assert [item.value for item in facts.projects] == ["Atlas"]
    assert rule_facts("Work Experience\nEngineer").name is None


@pytest.mark.parametrize(
    "update",
    [
        {"span": [-1, 4]},
        {"span": [0, 9999]},
        {"span": [True, 5]},
        {"evidence": "Invented"},
        {"value": "Invented"},
        {"value": " "},
        {"source": "invented"},
    ],
)
def test_req_in_23_invalid_facts_are_dropped(update: dict[str, Any]) -> None:
    item = {"value": "Alex", "evidence": "Alex", "span": [0, 4], "source": "llm", **update}
    assert validate_facts("Alex", {"name": item}).name is None


def test_req_in_23_phone_value_must_match_evidence_digits() -> None:
    text = "+44 123456789"
    fact = {"value": "123456789", "evidence": text, "span": [0, len(text)], "source": "llm"}
    assert validate_facts(text, {"phone": fact}).phone is None


def test_req_in_23_llm_quotes_and_precedence(loop: asyncio.AbstractEventLoop) -> None:
    fake = FakeProvider(
        generated(
            name={"value": "Engineer", "evidence": "Engineer at Example 2020-2024"},
            phone={"value": "020 0000 0000", "evidence": "Never recorded"},
            experience=[
                {"value": "Engineer", "evidence": "Engineer at Example 2020-2024"},
                {"value": "Invented executive", "evidence": "Engineer at Example 2020-2024"},
                {"value": "CEO", "evidence": "CEO at Fabricated"},
            ],
        )
    )
    result = extract_facts(TEXT, LLMBridge(fake, loop, asyncio.Semaphore(1)), 2, Context())  # type: ignore[arg-type]
    assert result.facts.name == rule_facts(TEXT).name
    assert result.facts.phone == rule_facts(TEXT).phone
    assert [item.value for item in result.facts.experience] == ["Engineer"]
    assert result.facts.experience[0].source == "llm"
    assert result.facts.qualifications == []
    assert result.extracted_by == "llm:fake:fake-v1" and result.prompt_version == "cv_extract_v1"
    for fact in all_facts(result.facts):
        assert TEXT[slice(*fact["span"])] == fact["evidence"]


class HangingProvider(FakeProvider):
    async def generate(
        self, request: PromptRequest, *, api_version: str | None = None
    ) -> GenerateResult:
        await asyncio.sleep(10)
        return await super().generate(request, api_version=api_version)


class UnhealthyProvider(FakeProvider):
    async def health(self) -> Health:
        return Health("down", "fake")


@pytest.mark.parametrize("mode", ["absent", "timeout", "invalid", "unhealthy", "closed"])
def test_req_in_23_llm_failure_falls_back(loop: asyncio.AbstractEventLoop, mode: str) -> None:
    provider = HangingProvider() if mode == "timeout" else FakeProvider("not JSON")
    if mode == "unhealthy":
        provider = UnhealthyProvider()
    bridge = None if mode == "absent" else LLMBridge(provider, loop, asyncio.Semaphore(1))
    closed = asyncio.new_event_loop()
    closed.close()
    if mode == "closed":
        bridge = LLMBridge(provider, closed, asyncio.Semaphore(1))
    result = extract_facts(TEXT, bridge, 0.05, Context())  # type: ignore[arg-type]
    assert result.facts == rule_facts(TEXT)
    assert result.prompt_version is None and result.extracted_by == "rule_based"
    assert result.failure == (
        None if mode == "absent" else "timeout" if mode == "timeout" else "error"
    )


def test_req_in_23_llm_identity_only_when_rules_missing(loop: asyncio.AbstractEventLoop) -> None:
    text = "contact person: alex border"
    provider = FakeProvider(generated(name={"value": "alex border", "evidence": text}))
    found = extract_facts(text, LLMBridge(provider, loop, asyncio.Semaphore(1)), 2, Context())  # type: ignore[arg-type]
    assert found.facts.name is not None and found.facts.name.source == "llm"


def test_req_in_23_public_fact_masking() -> None:
    text = "Name: Alex Border\n+44 123456789\nProjects\nContact alice@example.com on +44 123456789"
    facts = rule_facts(text)
    public = public_facts(json.dumps({"facts_schema": 1, "facts": facts.model_dump()}))
    assert public is not None and public.phone is not None and public.phone.value == "***89"
    assert "alice@example.com" not in public.model_dump_json()
    assert "+44 123456789" not in public.model_dump_json()
    assert "a***@example.com" in public.projects[0].evidence
    assert public_facts('{"emails": []}') is None
    assert mask_phone(None) is None and mask_phone("12") == "***"


def test_req_in_21_generation_is_serial_and_on_provider_loop(
    loop: asyncio.AbstractEventLoop,
) -> None:
    class RecordingProvider(FakeProvider):
        active = 0
        maximum = 0

        async def generate(
            self, request: PromptRequest, *, api_version: str | None = None
        ) -> GenerateResult:
            assert asyncio.get_running_loop() is loop
            assert request.temperature == 0 and request.seed == 0
            assert request.template_id == "cv_extract_v1" and request.json_schema is not None
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            await asyncio.sleep(0.02)
            self.active -= 1
            return await super().generate(request, api_version=api_version)

    provider = RecordingProvider(generated())
    bridge = LLMBridge(provider, loop, asyncio.Semaphore(1))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: extract_facts(TEXT, bridge, 2, Context()), range(2)))  # type: ignore[arg-type]
    assert all(result.failure is None for result in results)
    assert provider.maximum == 1
