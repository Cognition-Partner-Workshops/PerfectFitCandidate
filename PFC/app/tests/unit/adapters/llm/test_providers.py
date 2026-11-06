"""Offline provider coverage for REQ-LLM-01..05."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from pfc.adapters.llm.providers import (
    Capabilities,
    ExternalProviderPlaceholder,
    FakeProvider,
    FallbackProvider,
    GenerateResult,
    LlamaCppProvider,
    PromptRequest,
    ProviderError,
)


def test_http_schema_grammar_health_and_concurrency() -> None:
    async def scenario() -> None:
        active = peak = 0
        payloads = []

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, peak
            assert request.headers["authorization"] == "Bearer test"
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            assert request.url.path == "/v1/chat/completions"
            payloads.append(json.loads(request.content))
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.001)
            active -= 1
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"x": 1}'}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 4},
                },
            )

        client = LlamaCppProvider(
            "http://127.0.0.1:8000",
            "test-model",
            api_key="test",
            transport=httpx.MockTransport(handler),
        )
        try:
            assert (await client.health()).status == "ok"
            request = PromptRequest([], json_schema={"type": "object"})
            results = await asyncio.gather(*(client.generate(request) for _ in range(3)))
            assert peak == 1
            assert results[0].parsed_json == {"x": 1}
            assert results[0].tokens_in == 3
            assert results[0].served_by == "llamacpp"
            assert payloads[0]["response_format"]["json_schema"]["schema"] == {"type": "object"}
            await client.generate(PromptRequest([], grammar='root ::= "ok"'))
            assert payloads[-1]["grammar"] == 'root ::= "ok"'
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]}),
    ],
)
def test_http_errors(response: httpx.Response) -> None:
    async def scenario() -> None:
        client = LlamaCppProvider(
            "http://localhost:8000",
            "m",
            api_key="test",
            transport=httpx.MockTransport(lambda _: response),
        )
        try:
            with pytest.raises(ProviderError):
                await client.generate(PromptRequest([], json_schema={"type": "object"}))
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_timeout_and_health_failure() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                raise httpx.ConnectError("offline")
            await asyncio.sleep(1)
            return httpx.Response(200)

        client = LlamaCppProvider(
            "http://localhost", "m", api_key="test", transport=httpx.MockTransport(handler)
        )
        try:
            assert (await client.health()).status == "down"
            with pytest.raises(ProviderError):
                await client.generate(PromptRequest([], timeout_s=0.001))
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("conflict,succeed", [(True, True), (True, False), (False, False)])
def test_direct_local_fallback(conflict: bool, succeed: bool) -> None:
    class Primary(FakeProvider):
        name = "primary"
        calls: list[str | None] = []

        def capabilities(self) -> Capabilities:
            return Capabilities(api_versions=("old", "new"), data_residency="external")

        async def generate(
            self, request: PromptRequest, *, api_version: str | None = None
        ) -> GenerateResult:
            self.calls.append(api_version)
            if api_version == "new" and succeed:
                return await super().generate(request, api_version=api_version)
            raise ProviderError("failed", version_conflict=conflict)

    primary = Primary()
    result = asyncio.run(FallbackProvider(primary, FakeProvider()).generate(PromptRequest([])))
    assert primary.calls == (["old", "new"] if conflict else ["old"])
    assert result.served_by == ("primary" if succeed else "fake")
    assert result.fallback_chain


def test_disabled_and_fake() -> None:
    async def scenario() -> None:
        request = PromptRequest([])
        fake = FakeProvider()
        assert await fake.generate(request) == await fake.generate(request)
        assert (await fake.embed([])).shape == (0, 768)
        disabled = ExternalProviderPlaceholder()
        assert (await disabled.health()).status == "down"
        with pytest.raises(ProviderError, match="disabled"):
            await disabled.generate(request)
        with pytest.raises(ProviderError, match="prohibited"):
            await disabled.embed(["secret"])
        assert (await FallbackProvider(disabled, fake).generate(request)).served_by == "fake"
        with pytest.raises(ValueError):
            FallbackProvider(fake, disabled)
        with pytest.raises(ProviderError):
            await FakeProvider("[]").generate(replace(request, json_schema={"type": "object"}))

    asyncio.run(scenario())


def test_reject_remote_server() -> None:
    with pytest.raises(ValueError, match="loopback"):
        LlamaCppProvider("https://example.com", "m", api_key="test")
