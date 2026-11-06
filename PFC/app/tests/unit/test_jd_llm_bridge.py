"""LLM-only grounded skill classification; synthetic inputs, no model calls."""

import json
from typing import Any

import anyio
import pytest

from pfc.adapters.llm.providers import FakeProvider, GenerateResult, PromptRequest
from pfc.services.matching import llm_bridge
from pfc.services.matching.llm_bridge import JdLlmBridge, _ground_skills
from pfc.services.matching.requirements import Requirements
from scripts.validate_llm_extraction import RecordingProvider, run_jd


@pytest.mark.parametrize(
    "text",
    [
        "Redis is preferred.",
        "Redis is nice to have.",
        "Redis is desirable.",
        "Redis would be a plus.",
        "Ideally, you know Redis.",
        "Redis experience is a bonus.",
    ],
)
def test_preferred_cues_move_skills(text: str) -> None:
    item = {"text": "Redis", "years": 2}
    result = _ground_skills(text, {"mandatory_skills": [item]})
    assert result == {"mandatory_skills": [], "preferred_skills": [item]}


@pytest.mark.parametrize("cue", ["mandatory", "required", "must", "essential"])
def test_mandatory_cues_keep_or_move_skills(cue: str) -> None:
    item = {"text": "Redis"}
    text = f"{cue.upper()}: Redis"
    assert _ground_skills(text, {"mandatory_skills": [item]}) == {"mandatory_skills": [item]}
    assert _ground_skills(text, {"preferred_skills": [item]}) == {
        "mandatory_skills": [item],
        "preferred_skills": [],
    }


@pytest.mark.parametrize("separator", [";", ".", "!", "?"])
def test_mixed_sentences_split_into_clauses(separator: str) -> None:
    sql, redis = {"text": "SQL"}, {"text": "Redis"}
    result = _ground_skills(
        f"SQL is required{separator} Redis would be a plus.",
        {"mandatory_skills": [sql, redis]},
    )
    assert result == {"mandatory_skills": [sql], "preferred_skills": [redis]}


@pytest.mark.parametrize(
    "text",
    [
        "PREFERRED SKILLS:\nRedis",
        "Preferred technical skills\nRedis",
        "Mandatory skills:\nSQL\nDesirable skills:\nRedis",
        "Bonus: Redis",
    ],
)
@pytest.mark.parametrize("evidence", [None, "Redis"])
def test_nearest_heading_classifies_skills(text: str, evidence: str | None) -> None:
    item = {"text": "Redis", "evidence": evidence}
    assert _ground_skills(text, {"mandatory_skills": [item]}) == {
        "mandatory_skills": [],
        "preferred_skills": [item],
    }


@pytest.mark.parametrize(
    ("text", "item"),
    [
        ("Redis is required and desirable.", {"text": "Redis"}),
        ("Preferred skills:\nRedis is required and desirable.", {"text": "Redis"}),
        ("Redis supports the service.", {"text": "Redis"}),
        ("Required: SQL", {"text": "Redis"}),
        ("Required: SQL", {"text": " "}),
        ("Required and preferred skills:\nRedis", {"text": "Redis"}),
        ("Preferred skills:\nSQL\nOther requirements:\nRedis", {"text": "Redis"}),
        ("Redis is preferentially used.", {"text": "Redis"}),
        ("Redis is a plusone.", {"text": "Redis"}),
        ("", {"text": "Redis"}),
        ("Required: NoSQL", {"text": "SQL"}),
        (
            "Redis is required; Redis is a bonus.",
            {"text": "Redis", "evidence": "Redis is required; Redis is a bonus."},
        ),
    ],
)
def test_ambiguous_or_unlocatable_items_are_unchanged(text: str, item: dict[str, Any]) -> None:
    value = {"preferred_skills": [item]}
    assert _ground_skills(text, value) == value


def test_grounded_evidence_takes_precedence_and_is_normalised() -> None:
    item = {"text": "Redis", "evidence": "  REDIS   is nice to HAVE "}
    result = _ground_skills(
        "Redis is required. Later, Ｒｅｄｉｓ is nice to have.", {"mandatory_skills": [item]}
    )
    assert result == {"mandatory_skills": [], "preferred_skills": [item]}


def test_fabricated_evidence_is_ignored() -> None:
    item = {"text": "Redis", "evidence": "Redis is preferred"}
    result = _ground_skills("Redis is required.", {"preferred_skills": [item]})
    assert result == {"mandatory_skills": [item], "preferred_skills": []}


def test_dedupe_preserves_the_existing_correct_item_and_other_fields() -> None:
    wrong = {"text": "Ｒｅｄｉｓ", "years": None}
    correct = {"text": "redis", "evidence": "Redis is preferred", "years": 2}
    value = {
        "mandatory_skills": [wrong],
        "preferred_skills": [correct],
        "experience": [{"text": "Support", "years": 3}],
        "location": "Remote",
    }
    snapshot = json.dumps(value)
    result = _ground_skills("Redis is preferred.", value)
    assert result == {**value, "mandatory_skills": []}
    assert result["preferred_skills"][0] is correct
    assert json.dumps(value) == snapshot


def test_conflicting_grounded_contexts_are_unchanged() -> None:
    value = {
        "mandatory_skills": [{"text": "Redis", "evidence": "Redis is preferred"}],
        "preferred_skills": [{"text": "Redis", "evidence": "Redis is required"}],
    }
    assert _ground_skills("Redis is required. Redis is preferred.", value) == value


@pytest.mark.parametrize("value", [{}, {"mandatory_skills": [], "preferred_skills": []}])
def test_empty_requirements_are_unchanged(value: dict[str, Any]) -> None:
    assert _ground_skills("Required: SQL", value) == value


class CapturingProvider(FakeProvider):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.requests: list[PromptRequest] = []

    async def generate(
        self, request: PromptRequest, *, api_version: str | None = None
    ) -> GenerateResult:
        self.requests.append(request)
        return GenerateResult(self.text, "fake", "fake-v1")


def test_bridge_corrects_captured_jd05_raw_output() -> None:
    raw = """{
  "mandatory_skills": [
    {"text": "SQL", "years": null},
    {"text": "PostgreSQL", "years": null}
  ],
  "preferred_skills": [
    {"text": "PostgreSQL", "years": null}
  ]
}"""
    text = "Database specialist\nREQUIRED SKILLS: SQL\nPREFERRED SKILLS: PostgreSQL"
    provider = CapturingProvider(raw)
    result = anyio.run(JdLlmBridge(provider)._extract, text)
    assert result == {
        "mandatory_skills": [{"text": "SQL", "years": None}],
        "preferred_skills": [{"text": "PostgreSQL", "years": None}],
    }
    assert provider.requests[0].json_schema == Requirements.model_json_schema()
    assert provider.requests[0].messages[-1] == {"role": "user", "content": text}
    prompt = provider.requests[0].messages[0]["content"]
    assert "exactly one" in prompt and "short verbatim quote" in prompt


def test_fallback_does_not_call_post_check(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(text: str, value: dict[str, Any]) -> dict[str, Any]:
        pytest.fail("LLM post-check must not run on fallback output")

    monkeypatch.setattr(llm_bridge, "_ground_skills", unexpected)

    async def probe() -> dict[str, Any]:
        return await anyio.to_thread.run_sync(
            run_jd,
            RecordingProvider(CapturingProvider("not JSON")),
            "Required: SQL\nPreferred: Python",
        )

    result = anyio.run(probe)
    assert result["fallback_reason"] == "invalid_json"
    assert [item["text"] for item in result["requirements"]["mandatory_skills"]] == ["SQL"]
    assert [item["text"] for item in result["requirements"]["preferred_skills"]] == ["Python"]
