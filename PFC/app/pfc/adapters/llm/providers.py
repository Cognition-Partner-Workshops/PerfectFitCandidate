"""Local-first provider contracts and adapters (REQ-LLM-01..04)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import httpx
import jsonschema
import numpy as np
from numpy.typing import NDArray

from pfc.adapters.vector.embeddings import FakeEmbedder


@dataclass(frozen=True)
class Capabilities:
    generate: bool = True
    embed: bool = False
    json_schema: bool = True
    grammar: bool = True
    max_context: int = 4096
    streaming: bool = False
    api_versions: tuple[str, ...] = ("v1",)
    data_residency: Literal["local", "external"] = "local"


@dataclass(frozen=True)
class Health:
    status: Literal["ok", "degraded", "down"]
    model: str
    version: str = "v1"
    latency_ms: float = 0
    detail: str = ""


@dataclass(frozen=True)
class PromptRequest:
    messages: list[dict[str, str]]
    template_id: str = ""
    template_version: str = "1"
    json_schema: dict[str, Any] | None = None
    grammar: str | None = None
    temperature: float = 0
    seed: int = 0
    max_tokens: int = 1024
    timeout_s: float = 120
    purpose: str = "extraction"


@dataclass(frozen=True)
class GenerateResult:
    text: str
    served_by: str
    model: str
    api_version: str = "v1"
    parsed_json: Any = None
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0
    fallback_chain: tuple[str, ...] = field(default_factory=tuple)


class LLMProvider(Protocol):
    """TDD §8.1 contract; embedding stays local (REQ-LLM-07)."""

    name: str
    kind: str

    def capabilities(self) -> Capabilities: ...
    async def health(self) -> Health: ...
    async def generate(
        self, request: PromptRequest, *, api_version: str | None = None
    ) -> GenerateResult: ...
    async def embed(self, texts: list[str]) -> NDArray[np.float32]: ...


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, version_conflict: bool = False) -> None:
        super().__init__(message)
        self.version_conflict = version_conflict


def parse_output(text: str, schema: dict[str, Any] | None) -> Any:
    if schema is None:
        return None
    try:
        parsed = json.loads(text)
        jsonschema.validate(parsed, schema)
        return parsed
    except (ValueError, jsonschema.ValidationError) as exc:
        raise ProviderError("Invalid structured output") from exc


class LlamaCppProvider:
    """Loopback-only llama-server client (REQ-LLM-01, REQ-LLM-02)."""

    name = "llamacpp"
    kind = "llamacpp"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str,
        timeout: float = 120,
        connect_timeout: float = 10,
        max_concurrency: int = 1,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        url = urlparse(base_url)
        if url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("llama.cpp must use a loopback address")
        if max_concurrency < 1 or timeout <= 0 or connect_timeout <= 0:
            raise ValueError("Concurrency and timeouts must be positive")
        self.model = model
        self._slots = asyncio.Semaphore(max_concurrency)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            transport=transport,
            trust_env=False,
        )

    def capabilities(self) -> Capabilities:
        return Capabilities()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> Health:
        start = perf_counter()
        try:
            response = await self._client.get("/health")
            response.raise_for_status()
            status: Literal["ok", "degraded", "down"] = "ok"
            detail = ""
        except httpx.HTTPError:
            status, detail = "down", "llama.cpp health check failed"
        return Health(status, self.model, latency_ms=(perf_counter() - start) * 1000, detail=detail)

    async def generate(
        self, request: PromptRequest, *, api_version: str | None = None
    ) -> GenerateResult:
        if api_version not in (None, "v1"):
            raise ProviderError("Unsupported local API version", version_conflict=True)
        if request.json_schema is not None and request.grammar is not None:
            raise ValueError("Choose JSON schema or GBNF grammar, not both")
        if request.timeout_s <= 0:
            raise ValueError("Request timeout must be positive")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": request.messages,
            "temperature": request.temperature,
            "seed": request.seed,
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        if request.json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "pfc_output",
                    "strict": True,
                    "schema": request.json_schema,
                },
            }
        if request.grammar is not None:
            payload["grammar"] = request.grammar
        start = perf_counter()
        try:
            async with asyncio.timeout(request.timeout_s):
                async with self._slots:
                    response = await self._client.post("/v1/chat/completions", json=payload)
                    response.raise_for_status()
            data = response.json()
            text = data["choices"][0]["message"]["content"]
            if not isinstance(text, str):
                raise ValueError("Missing text")
            usage = data.get("usage", {})
            return GenerateResult(
                text=text,
                parsed_json=parse_output(text, request.json_schema),
                served_by=self.name,
                model=self.model,
                tokens_in=int(usage.get("prompt_tokens", 0)),
                tokens_out=int(usage.get("completion_tokens", 0)),
                latency_ms=(perf_counter() - start) * 1000,
            )
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("llama.cpp generation failed") from exc

    async def embed(self, texts: list[str]) -> NDArray[np.float32]:
        raise ProviderError("Use the local ONNX Embedder for embeddings (REQ-LLM-07)")


class FakeProvider:
    """Deterministic, offline fixture provider (REQ-LLM-02)."""

    name = "fake"
    kind = "fake"

    def __init__(self, text: str = "{}") -> None:
        self.text = text

    def capabilities(self) -> Capabilities:
        return Capabilities(grammar=False)

    async def health(self) -> Health:
        return Health("ok", "fake-v1")

    async def generate(
        self, request: PromptRequest, *, api_version: str | None = None
    ) -> GenerateResult:
        return GenerateResult(
            self.text,
            self.name,
            "fake-v1",
            api_version or "v1",
            parse_output(self.text, request.json_schema),
        )

    async def embed(self, texts: list[str]) -> NDArray[np.float32]:
        return FakeEmbedder().embed(texts)


class ExternalProviderPlaceholder(FakeProvider):
    """Never sends data externally in Phase 1 (REQ-LLM-03, REQ-LLM-05)."""

    name = "external-disabled"
    kind = "external"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            generate=False,
            json_schema=False,
            grammar=False,
            api_versions=(),
            data_residency="external",
        )

    async def health(self) -> Health:
        return Health("down", "", detail="External providers are disabled in Phase 1")

    async def generate(
        self, request: PromptRequest, *, api_version: str | None = None
    ) -> GenerateResult:
        raise ProviderError("External providers are disabled in Phase 1 (REQ-LLM-03)")

    async def embed(self, texts: list[str]) -> NDArray[np.float32]:
        raise ProviderError("External embeddings are prohibited (REQ-LLM-07)")


class FallbackProvider:
    """Retry compatible versions, then only local; expose served_by (REQ-LLM-04)."""

    name = "fallback"
    kind = "fallback"

    def __init__(self, primary: LLMProvider, local: LLMProvider) -> None:
        if local.capabilities().data_residency != "local":
            raise ValueError("Fallback must be local")
        self.primary, self.local = primary, local

    def capabilities(self) -> Capabilities:
        return self.primary.capabilities()

    async def health(self) -> Health:
        return await self.primary.health()

    async def embed(self, texts: list[str]) -> NDArray[np.float32]:
        return await self.local.embed(texts)

    async def generate(
        self, request: PromptRequest, *, api_version: str | None = None
    ) -> GenerateResult:
        versions = (
            (api_version,) if api_version is not None else self.primary.capabilities().api_versions
        )
        attempts: list[str] = []
        for version in versions or (None,):
            attempts.append(f"{self.primary.name}:{version or 'default'}")
            try:
                result = await self.primary.generate(request, api_version=version)
                return replace(result, fallback_chain=tuple(attempts[:-1]))
            except (ProviderError, httpx.HTTPError, TimeoutError) as exc:
                if not isinstance(exc, ProviderError) or not exc.version_conflict:
                    if self.local is self.primary:
                        raise
                    break
        result = await self.local.generate(request)
        return replace(result, fallback_chain=tuple(attempts))
