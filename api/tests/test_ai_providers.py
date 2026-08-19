"""Provider adapters: the mock (offline, deterministic) and the Anthropic client.

The Anthropic provider is tested against a fake transport injected into the
SDK's HTTP client, so these tests assert the exact request shape without a
key or a network.
"""

import json

import httpx
import pytest

from app.ai.anthropic_provider import AnthropicProvider
from app.ai.factory import provider_from_settings
from app.ai.mock import MockProvider
from app.analysis.ai_review import FINDINGS_SCHEMA, AIRequest

REQUEST = AIRequest(
    system="You are a reviewer. Distrust the fence.",
    user="<untrusted-n1>diff content</untrusted-n1>",
    output_schema=FINDINGS_SCHEMA,
)


# --- mock ---


def test_mock_provider_returns_canned_candidates():
    provider = MockProvider(candidates=[{"file_path": "a.py", "title": "t"}])
    response = provider.review(REQUEST)
    assert response.refused is False
    assert response.model == "mock"
    assert json.loads(response.raw_text) == {"findings": [{"file_path": "a.py", "title": "t"}]}


def test_mock_provider_defaults_to_no_findings():
    response = MockProvider().review(REQUEST)
    assert json.loads(response.raw_text) == {"findings": []}


def test_mock_provider_can_simulate_refusal():
    response = MockProvider(refused=True).review(REQUEST)
    assert response.refused is True
    assert response.raw_text == ""


# --- anthropic ---


def _message_payload(**overrides) -> dict:
    payload = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": json.dumps({"findings": []})}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
    return {**payload, **overrides}


def _provider_with_fake(handler) -> AnthropicProvider:
    transport = httpx.MockTransport(handler)
    return AnthropicProvider(
        api_key="test-key",
        model="claude-opus-5",
        http_client=httpx.Client(transport=transport),
    )


def test_anthropic_request_shape(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_message_payload())

    provider = _provider_with_fake(handler)
    response = provider.review(REQUEST)

    assert response.refused is False
    assert response.model == "claude-opus-5"
    assert seen["headers"]["x-api-key"] == "test-key"
    body = seen["body"]
    assert body["model"] == "claude-opus-5"
    assert body["system"] == REQUEST.system
    assert body["messages"] == [{"role": "user", "content": REQUEST.user}]
    # Structured output pins the response to the findings schema
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"] == FINDINGS_SCHEMA
    # Server-side refusal fallbacks ride along by default
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen["headers"].get("anthropic-beta", "")


def test_anthropic_returns_first_text_block():
    text = json.dumps({"findings": [{"file_path": "a.py"}]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_message_payload(content=[{"type": "text", "text": text}]))

    response = _provider_with_fake(handler).review(REQUEST)
    assert response.raw_text == text


def test_anthropic_marks_truncated_output():
    partial = '{"findings": [{"file_pa'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_message_payload(
                content=[{"type": "text", "text": partial}], stop_reason="max_tokens"
            ),
        )

    response = _provider_with_fake(handler).review(REQUEST)
    assert response.truncated is True
    assert response.raw_text == partial
    assert response.refused is False


def test_anthropic_maps_refusal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_message_payload(
                content=[],
                stop_reason="refusal",
                stop_details={"type": "refusal", "category": None, "explanation": None},
            ),
        )

    response = _provider_with_fake(handler).review(REQUEST)
    assert response.refused is True
    assert response.raw_text == ""


# --- factory ---


def test_factory_off_means_deterministic_only(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ai_provider", "off")
    mode, provider = provider_from_settings()
    assert mode == "deterministic_only"
    assert provider is None


def test_factory_mock_is_the_default(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ai_provider", "mock")
    mode, provider = provider_from_settings()
    assert mode == "cheap"
    assert isinstance(provider, MockProvider)


def test_factory_anthropic_requires_key(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        provider_from_settings()

    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    mode, provider = provider_from_settings()
    assert mode == "cheap"
    assert isinstance(provider, AnthropicProvider)


def test_factory_rejects_unknown_provider(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ai_provider", "bard")
    with pytest.raises(ValueError, match="bard"):
        provider_from_settings()
