from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from pfc.api.schemas.match import WireModel


class ChatQuestion(WireModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]


class ChatSource(WireModel):
    kind: Literal["demand", "resource", "match_result"]
    record_id: int
    label: str
    link: str
    values: dict[str, str] = Field(default_factory=dict)


class ChatAnswer(WireModel):
    outcome: Literal["answered", "no_data", "unsupported", "unavailable", "uninterpretable"]
    text: str
    sources: list[ChatSource] = Field(default_factory=list)


class ChatEntry(WireModel):
    job_id: int
    question: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    created_at: str


class ChatDetail(ChatEntry):
    answer: ChatAnswer | None


class ChatStatus(WireModel):
    available: bool
