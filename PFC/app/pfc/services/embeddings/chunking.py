"""Token-bounded, section-aware retrieval chunks (TDD §6.3)."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

from tokenizers import Tokenizer

from pfc.adapters.vector.embeddings import Embedder, Task, task_prefix

CHUNKER_VERSION = "section-tokens-256-overlap-32-v1"
MAX_TOKENS = 256
OVERLAP = 32
OwnerType = Literal["cv", "jd", "jd_item"]


def pipeline_version(base: str) -> str:
    return base if base.endswith(f"+{CHUNKER_VERSION}") else f"{base}+{CHUNKER_VERSION}"


@dataclass(frozen=True)
class Owner:
    owner_type: OwnerType
    owner_id: int
    text: str
    sections_json: str | None = None
    title: str = ""

    @property
    def task(self) -> Task:
        return "query" if self.owner_type == "jd_item" else "document"


@dataclass(frozen=True)
class Chunk:
    owner_type: OwnerType
    owner_id: int
    section: str
    chunk_no: int
    text: str
    token_count: int

    @property
    def task(self) -> Task:
        return "query" if self.owner_type == "jd_item" else "document"

    @property
    def model_text(self) -> str:
        return task_prefix(self.task) + self.text


_SOURCES: dict[OwnerType, str] = {
    "cv": "SELECT v.cv_version_id, v.text_norm, v.sections_json, '' FROM cv_versions v "
    "WHERE v.is_latest=1",
    "jd": "SELECT v.demand_version_id, v.jd_text_norm, NULL, coalesce(d.demand_request_name,'') "
    "FROM demands d JOIN demand_versions v ON v.demand_version_id=d.current_version_id "
    "AND v.demand_id=d.demand_id WHERE 1=1",
    "jd_item": "SELECT i.item_id, i.text, NULL, '' FROM demands d "
    "JOIN jd_requirements r ON r.demand_version_id=d.current_version_id "
    "JOIN jd_requirement_items i ON i.jd_req_id=r.jd_req_id "
    "WHERE r.status='confirmed'",
}
_IDS = {"cv": "v.cv_version_id", "jd": "v.demand_version_id", "jd_item": "i.item_id"}


def load_owners(db: sqlite3.Connection) -> list[Owner]:
    return [
        Owner(kind, row[0], row[1], row[2], row[3])
        for kind, sql in sorted(_SOURCES.items())
        for row in db.execute(sql + f" ORDER BY {_IDS[kind]}")
    ]


def load_owner(db: sqlite3.Connection, kind: OwnerType, id: int) -> Owner | None:
    row = db.execute(_SOURCES[kind] + f" AND {_IDS[kind]}=?", (id,)).fetchone()
    return Owner(kind, row[0], row[1], row[2], row[3]) if row else None


class TokenCounter:
    def __init__(self, embedder: Embedder) -> None:
        tokenizer = getattr(embedder, "tokenizer", None)
        self.tokenizer = Tokenizer.from_str(tokenizer.to_str()) if tokenizer is not None else None
        if self.tokenizer is not None:
            self.tokenizer.no_padding()
            self.tokenizer.no_truncation()

    def count(self, text: str) -> int:
        return len(self.tokenizer.encode(text).ids) if self.tokenizer else len(text.split())

    def offsets(self, text: str) -> list[tuple[int, int]]:
        if self.tokenizer:
            return [
                (start, end)
                for start, end in self.tokenizer.encode(text, add_special_tokens=False).offsets
                if end > start
            ]
        return [(match.start(), match.end()) for match in re.finditer(r"\S+", text)]


def _windows(text: str, prefix: str, task: Task, counter: TokenCounter) -> list[tuple[str, int]]:
    offsets = counter.offsets(text)
    result: list[tuple[str, int]] = []
    start = 0
    while start < len(offsets):
        end = min(len(offsets), start + MAX_TOKENS)
        while end > start:
            value = prefix + text[offsets[start][0] : offsets[end - 1][1]]
            count = counter.count(task_prefix(task) + value)
            if count <= MAX_TOKENS:
                break
            end -= 1
        if end == start:
            raise ValueError("JD title/task prefix leaves no room within the 256-token limit")
        result.append((value, count))
        if end == len(offsets):
            break
        if end - start <= OVERLAP:
            raise ValueError("JD title/task prefix leaves no room for 32-token overlap")
        start = end - OVERLAP
    return result


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(filter(None, (_text(item) for item in value)))
    if isinstance(value, dict):
        return "\n".join(filter(None, (_text(item) for item in value.values())))
    return ""


def _cv_sections(owner: Owner) -> list[tuple[str, str]]:
    data = json.loads(owner.sections_json) if owner.sections_json else None
    if isinstance(data, list):
        sections = [
            (
                str(item.get("section") or item.get("name") or item.get("title") or "other"),
                item.get("text", item.get("content", item.get("items", ""))),
            )
            for item in data
            if isinstance(item, dict)
        ]
    elif isinstance(data, dict):
        sections = list(data.items())
    else:
        sections = []
    result: list[tuple[str, str]] = []
    for name, value in sections:
        section = re.sub(r"\s+", "_", name.strip().casefold()) or "other"
        if "experience" in section and isinstance(value, list):
            result.extend((f"{section}/{number}", _text(role)) for number, role in enumerate(value))
        else:
            result.append((section, _text(value)))
    return [(name, text) for name, text in result if text.strip()] or [("full", owner.text)]


def _jd_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    name = "full"
    lines: list[str] = []
    headings = {
        "role purpose",
        "overview",
        "responsibilities",
        "requirements",
        "essential",
        "requirements / essential",
        "desirable",
        "other",
    }
    for line in text.splitlines():
        heading = line.strip().strip("# :").casefold()
        if heading in headings:
            if lines:
                sections.append((name, "\n".join(lines)))
            name, lines = heading.replace(" ", "_"), [line.strip()]
        else:
            lines.append(line)
    if lines:
        sections.append((name, "\n".join(lines)))
    return sections


def chunk_owner(owner: Owner, embedder: Embedder) -> list[Chunk]:
    counter = TokenCounter(embedder)
    if owner.owner_type == "cv":
        sections = _cv_sections(owner)
    elif owner.owner_type == "jd":
        sections = _jd_sections(owner.text)
    else:
        sections = [("requirement", owner.text)]
    prefix = f"{owner.title.strip()}\n" if owner.owner_type == "jd" and owner.title.strip() else ""
    counts: dict[str, int] = defaultdict(int)
    chunks = []
    for section, text in sections:
        for value, count in _windows(text, prefix, owner.task, counter):
            chunks.append(
                Chunk(owner.owner_type, owner.owner_id, section, counts[section], value, count)
            )
            counts[section] += 1
    return chunks
