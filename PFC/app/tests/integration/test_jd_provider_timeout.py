"""Timeout classification through the real local provider's exception wrapping."""

import httpx
import pytest
from test_matching_api import Api
from test_matching_api import api as api
from test_matching_service import seed_demand

from pfc.adapters.llm.providers import LlamaCppProvider
from pfc.db.connection import database


@pytest.mark.parametrize(
    "error_type",
    [
        httpx.ReadTimeout,
        httpx.ConnectTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        TimeoutError,
        httpx.ConnectError,
    ],
)
def test_real_provider_wrapped_timeout(api: Api, error_type: type[Exception]) -> None:
    client, headers = api
    requests: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        raise error_type("mock transport failure")

    provider = LlamaCppProvider(
        "http://127.0.0.1:8080",
        "mock-local",
        api_key="test-only",
        transport=httpx.MockTransport(transport),
    )
    client.app.state.llm_provider = provider
    try:
        with database(client.app.state.settings) as db:
            demand = seed_demand(db, confirmed=False)
        response = client.post(f"/api/v1/demands/{demand}/analyze", headers=headers)
        assert response.status_code == 200, response.text
        assert requests == ["/health", "/v1/chat/completions"]
        assert response.json()["extractionMethod"] == "rule_based_fallback"
        expected = "provider_error" if error_type is httpx.ConnectError else "provider_timeout"
        assert response.json()["fallbackReason"] == expected
        assert response.json()["warnings"] == [f"Rule-based fallback: {expected}"]
        assert response.json()["status"] == "draft"
    finally:
        assert client.portal is not None
        client.portal.call(provider.aclose)
