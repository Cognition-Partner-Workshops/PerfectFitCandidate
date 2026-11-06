import json
from pathlib import Path

import pytest

from pfc.adapters.vector.embeddings import FakeEmbedder, OnnxEmbedder
from pfc.services.embeddings.chunking import (
    CHUNKER_VERSION,
    Chunk,
    Owner,
    TokenCounter,
    chunk_owner,
    pipeline_version,
)

from .conftest import tiny_bundle


def test_whitespace_windows_overlap_and_prefix_budget() -> None:
    words = [f"word{i}" for i in range(700)]
    chunks = chunk_owner(Owner("jd", 1, " ".join(words), title="Engineer"), FakeEmbedder(8))
    assert len(chunks) > 2
    assert all(chunk.text.startswith("Engineer\n") for chunk in chunks)
    assert all(
        chunk.token_count <= 256 and chunk.token_count == len(chunk.model_text.split())
        for chunk in chunks
    )
    for left, right in zip(chunks, chunks[1:], strict=False):
        assert left.text.split()[1:][-32:] == right.text.split()[1:][:32]
    assert chunks[-1].text.endswith(words[-1])
    assert pipeline_version("v1").endswith(CHUNKER_VERSION)
    assert pipeline_version(pipeline_version("v1")) == pipeline_version("v1")


def test_real_tokenizer_windows_include_task_and_title_tokens(tmp_path: Path) -> None:
    model = tiny_bundle(tmp_path / "tiny")
    embedder = OnnxEmbedder(model, model.parent / "tokenizer.json", dimensions=8)
    text = " ".join(f"word{i}!" for i in range(400))
    chunks = chunk_owner(Owner("jd", 1, text, title="Engineer"), embedder)
    assert len(chunks) >= 4
    counter = TokenCounter(embedder)
    assert all(chunk.token_count == counter.count(chunk.model_text) <= 256 for chunk in chunks)
    bodies = [chunk.text.split("\n", 1)[1] for chunk in chunks]
    for left, right in zip(bodies, bodies[1:], strict=False):
        assert (
            embedder.tokenizer.encode(left, add_special_tokens=False).ids[-32:]
            == embedder.tokenizer.encode(right, add_special_tokens=False).ids[:32]
        )


def test_sections_roles_skills_and_missing_fallback() -> None:
    embedder = FakeEmbedder(8)
    sections = {
        "experience": [
            {"role": "Engineer", "text": "Python"},
            {"role": "Architect", "text": "SQL"},
        ],
        "skills": ["Python", "SQL"],
        "education": "Degree",
    }
    chunks = chunk_owner(Owner("cv", 1, "fallback", json.dumps(sections)), embedder)
    assert [chunk.section for chunk in chunks] == [
        "experience/0",
        "experience/1",
        "skills",
        "education",
    ]
    assert "Engineer" in chunks[0].text and "Architect" in chunks[1].text
    assert "Python" in chunks[2].text and "SQL" in chunks[2].text
    assert chunk_owner(Owner("cv", 1, "fallback"), embedder)[0].text == "fallback"
    assert chunk_owner(Owner("cv", 1, "fallback", "{}"), embedder)[0].text == "fallback"
    assert (
        chunk_owner(
            Owner("cv", 1, "fallback", json.dumps([{"section": "skills", "text": "SQL"}])), embedder
        )[0].section
        == "skills"
    )
    assert chunk_owner(Owner("cv", 1, ""), embedder) == []
    with pytest.raises(ValueError):
        chunk_owner(Owner("cv", 1, "fallback", "invalid"), embedder)
    with pytest.raises(ValueError, match="256-token"):
        chunk_owner(Owner("jd", 1, "Python", title=" ".join(["title"] * 256)), embedder)


def test_jd_sections_and_query_task() -> None:
    chunks = chunk_owner(
        Owner("jd", 1, "Overview:\nBuild systems\nResponsibilities:\nWrite code", title="Engineer"),
        FakeEmbedder(8),
    )
    assert [chunk.section for chunk in chunks] == ["overview", "responsibilities"]
    assert all(chunk.model_text.startswith("search_document: Engineer\n") for chunk in chunks)
    query = chunk_owner(Owner("jd_item", 2, "Python"), FakeEmbedder(8))[0]
    assert query.model_text == "search_query: Python" and query.task == "query"
    assert isinstance(query, Chunk)
