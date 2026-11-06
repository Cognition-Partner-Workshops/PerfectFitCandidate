"""REQ-IN-23 / REQ-MAT-02 / REQ-LLM-02: offline scoring and real bridge contexts."""

import argparse
import asyncio
import json
import re
import threading
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from pfc.adapters.llm.providers import (
    FakeProvider,
    GenerateResult,
    LlamaCppProvider,
    PromptRequest,
    ProviderError,
)
from pfc.api.schemas.cv import CVFacts
from pfc.jobs.runner import JobContext
from pfc.services.ingestion.cv_facts import KEYS, SCHEMA, LLMBridge, extract_facts
from scripts import validate_llm_extraction as validation
from scripts.validate_llm_extraction import (
    FIXTURES,
    RecordingProvider,
    cv_drops,
    final_fact_failures,
    jd_schema_invalid,
    render_markdown,
    run_jd,
    score_cv,
    score_jd,
    score_values,
    summarize,
)


def test_req_llm_02_dotted_output_prefixes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def default_encoding(encoding: str | None, stacklevel: int = 2) -> str:
        return "cp1252" if encoding is None else encoding

    async def fake_live(args: argparse.Namespace) -> dict[str, Any]:
        return summarize([], [], [], load_s=0, peak_bytes=0, hardware={"prefix": str(args.out)})

    monkeypatch.setattr("io.text_encoding", default_encoding)
    monkeypatch.setattr(validation, "live", fake_live)
    for name in ("validation.run1", "validation.run2"):
        prefix = tmp_path / name
        monkeypatch.setattr(
            "sys.argv",
            ["validate", "--gguf", "unused.gguf", "--llama-server", "unused", "--out", str(prefix)],
        )
        assert validation.main() == 0
    for name in ("validation.run1", "validation.run2"):
        prefix = tmp_path / name
        report = json.loads(Path(f"{prefix}.json").read_text(encoding="utf-8"))
        assert report["hardware"]["prefix"] == str(prefix)
        markdown = Path(f"{prefix}.md").read_text(encoding="utf-8")
        assert markdown == render_markdown(report)
        assert "→" in markdown
    assert not (tmp_path / "validation.json").exists()
    assert not (tmp_path / "validation.md").exists()


def test_req_llm_02_fixture_sanity() -> None:
    groups = json.loads(FIXTURES.read_text())
    ids = []
    for kind in ("jds", "cvs"):
        assert len(groups[kind]) == 10
        for fixture in groups[kind]:
            ids.append(fixture["id"])
            text = fixture["text"]
            emails = re.findall(r"[\w.+-]+@[^\s]+", text)
            assert all(email.endswith("@example.com") for email in emails)
            for digit_run in re.findall(r"\b(?:\d[ -]?){7,15}\b", text):
                digits = re.sub(r"\D", "", digit_run)
                if len(digits) == 11:
                    assert re.fullmatch(r"07700900\d{3}|02079460\d{3}", digits)
            for expected in fixture["expected"].values():
                assert all(value.casefold() in text.casefold() for value in expected)
    assert len(ids) == len(set(ids))


def test_req_mat_02_exact_normalized_precision_recall() -> None:
    score = score_values(["SQL", "Python"], [" sql ", "SQL", "Java"])
    assert score == {"expected": 2, "predicted": 2, "recovered": 1, "recall": 0.5, "precision": 0.5}
    assert score_values(["ＡＷＳ"], ["aws"])["recall"] == 1
    assert score_values(["SQL"], ["SQL developer"])["recovered"] == 0
    assert score_values([], [])["recall"] is None
    assert score_values([], [])["precision"] is None


class RawFake(FakeProvider):
    async def generate(
        self, request: PromptRequest, *, api_version: str | None = None
    ) -> GenerateResult:
        return GenerateResult(self.text, "fake", "fake-v1")


@pytest.mark.parametrize(
    "raw,reason",
    [
        ('{"mandatory_skills":[{"text":"SQL"}]}', None),
        ('{"mandatory_skills":[{"text":" "}]}', "schema_invalid"),
        ('{"invented":true}', "schema_invalid"),
        ("not json", "invalid_json"),
    ],
)
def test_req_mat_02_real_jd_bridge_in_anyio_worker(raw: str, reason: str | None) -> None:
    async def probe() -> dict[str, Any]:
        provider = RecordingProvider(RawFake(raw))
        result = await anyio.to_thread.run_sync(run_jd, provider, "Required: SQL")
        assert result["fallback_reason"] == reason
        assert provider.calls[0]["raw"] == raw
        assert provider.calls[0]["latency_s"] >= 0
        return result

    result = anyio.run(probe)
    assert result["requirements"]["mandatory_skills"][0]["text"] == "SQL"


def test_req_in_23_grounding_drops_are_not_rule_precedence() -> None:
    text = "Name: Avery Slate\nExperience\nEngineer at Acme\nProjects\nAtlas delivery"
    output = {
        "name": {"value": "Avery Slate", "evidence": "Name: Avery Slate"},
        "phone": None,
        "experience": [
            {"value": "Engineer at Acme", "evidence": "Engineer at Acme"},
            {"value": "Invented", "evidence": "Engineer at Acme"},
            {"value": "Fiction", "evidence": "Absent quote"},
        ],
        "qualifications": [],
        "projects": [],
    }
    raw = json.dumps(output)
    drops = cv_drops(text, raw)
    assert drops["categories"]["experience"] == {
        "raw": 3,
        "quote_located": 2,
        "validated": 1,
        "ungrounded": 2,
        "rule_precedence": 0,
    }
    assert drops["categories"]["name"]["ungrounded"] == 0
    assert drops["categories"]["name"]["rule_precedence"] == 1

    async def probe() -> CVFacts:
        provider = RecordingProvider(FakeProvider(raw))
        bridge = LLMBridge(provider, asyncio.get_running_loop(), asyncio.Semaphore(1))
        context = JobContext(None, 0, threading.Event())  # type: ignore[arg-type]
        result = await anyio.to_thread.run_sync(extract_facts, text, bridge, 2, context)
        assert result.facts.name is not None and result.facts.name.source == "rule"
        assert result.facts.experience[0].source == "llm"
        assert result.facts.projects[0].source == "rule"
        assert final_fact_failures(text, result.facts) == 0
        return result.facts

    facts = anyio.run(probe)
    fixture: dict[str, Any] = {
        "id": "synthetic",
        "text": text,
        "expected": {key: [] for key in KEYS},
    }
    fixture["expected"]["experience"] = ["Engineer at Acme"]
    assert score_cv(fixture, facts, raw)["scores"]["experience"]["recovered"] == 1
    broken = facts.model_copy(deep=True)
    broken.experience[0].span = [0, 1]
    assert final_fact_failures(text, broken) == 1


def test_req_in_23_invalid_and_missing_cv_responses() -> None:
    assert cv_drops("", "not json")["schema_invalid"]
    assert cv_drops("", "{}")["schema_invalid"]
    assert cv_drops("", None)["response_missing"]
    assert final_fact_failures("", CVFacts()) == 0
    assert not jd_schema_invalid("not json")
    assert jd_schema_invalid('{"invented":true}')
    assert jd_schema_invalid('{"mandatory_skills":[{"text":" "}]}')


def test_req_llm_02_proxy_captures_adapter_rejected_raw_output() -> None:
    async def probe() -> None:
        raw = '{"invented":true}'

        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": raw}}]})

        transport = httpx.MockTransport(respond)
        actual = LlamaCppProvider(
            "http://127.0.0.1:1234", "fake-model", api_key="synthetic-key", transport=transport
        )
        proxy = RecordingProvider(actual)
        actual._client.event_hooks["response"].append(proxy.observe_response)
        try:
            with pytest.raises(ProviderError):
                await proxy.generate(PromptRequest(messages=[], json_schema=SCHEMA))
            assert proxy.calls[0]["raw"] == raw
            assert proxy.calls[0]["error"] == "ProviderError"
            assert proxy.calls[0]["wire_responses"] == [raw]
        finally:
            await actual._client.aclose()

    anyio.run(probe)


def test_req_llm_02_reports_hand_computed_totals_and_empty_cases() -> None:
    fixture = {
        "id": "jd",
        "expected": {"mandatory_skills": ["SQL", "Python"], "preferred_skills": ["Docker"]},
    }
    result = {
        "fallback_reason": None,
        "requirements": {
            "mandatory_skills": [{"text": "SQL"}, {"text": "Java"}],
            "preferred_skills": [{"text": "Docker"}],
        },
    }
    scored = score_jd(fixture, result)
    fallback = {**scored, "fallback_reason": "schema_invalid", "schema_invalid": True}
    report = summarize(
        [scored, fallback],
        [],
        [{"latency_s": 2}, {"latency_s": 4}],
        load_s=1,
        peak_bytes=2_000_000_000,
        hardware={"cpus": 8},
    )
    assert report["jd"]["llm_share"] == 0.5
    assert report["jd"]["fallback_reasons"] == {"schema_invalid": 1}
    assert report["jd"]["schema_invalid"] == 1
    assert report["jd"]["scores"]["mandatory_skills"]["recall"] == 0.5
    assert report["latency"] == {"calls": 2, "median_s": 3, "max_s": 4}
    markdown = render_markdown(report)
    assert "1/2 (50.0%)" in markdown
    assert "| mandatory_skills | 2/4 | 50.0% | 50.0% |" in markdown
    assert "2.000 GB / 8 GB — PASS" in markdown
    assert "final fact/span validation failures: 0" in markdown
    assert json.loads(json.dumps(report)) == report
    empty = summarize([], [], [], load_s=0, peak_bytes=None, hardware={})
    assert "n/a" in render_markdown(empty)
    assert not empty["memory_pass"]
    over = summarize([], [], [], load_s=0, peak_bytes=8_000_000_001, hardware={})
    assert not over["memory_pass"]
