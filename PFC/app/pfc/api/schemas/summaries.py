"""REQ-LLM-02: reading-only, grounded document summaries."""

from pydantic import Field

from .match import WireModel


class SummarySentence(WireModel):
    text: str = Field(min_length=1, max_length=300)
    evidence: str = Field(min_length=1, max_length=300)


class SummaryDocument(WireModel):
    sentences: list[SummarySentence] = Field(min_length=1, max_length=5)


class SummaryView(SummaryDocument):
    model: str
    created_at: str
    stale: bool
    dropped_ungrounded: int
    source_version_id: int
