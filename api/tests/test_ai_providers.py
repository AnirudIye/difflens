"""Provider adapters: the mock (offline, deterministic) and the Anthropic client.

The Anthropic provider is tested against a fake transport injected into the
SDK's HTTP client, so these tests assert the exact request shape without a
key or a network.
"""

import json

import httpx
import pytest

from app.ai.anthropic_provider import AnthropicProvider
from app.ai.errors import AIProviderConfigError
from app.ai.factory import build_provider, provider_from_settings
from app.ai.gemini_provider import GeminiProvider
from app.ai.mock import MockProvider
from app.ai.openai_provider import OpenAIProvider
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


# --- gemini ---


def _gemini_payload(**overrides) -> dict:
    payload = {
        "candidates": [
            {
                "content": {"parts": [{"text": json.dumps({"findings": []})}]},
                "finishReason": "STOP",
            }
        ]
    }
    return {**payload, **overrides}


def _gemini_with_fake(handler) -> GeminiProvider:
    return GeminiProvider(
        api_key="g-test-key",
        model="gemini-3.6-flash",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_gemini_request_shape():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_gemini_payload())

    response = _gemini_with_fake(handler).review(REQUEST)

    assert response.refused is False
    assert response.model == "gemini-3.6-flash"
    assert "/v1beta/models/gemini-3.6-flash:generateContent" in seen["url"]
    # The key travels in a header, never in the URL where it could hit logs
    assert seen["headers"]["x-goog-api-key"] == "g-test-key"
    assert "key=" not in seen["url"]
    body = seen["body"]
    assert body["system_instruction"]["parts"] == [{"text": REQUEST.system}]
    assert body["contents"] == [{"role": "user", "parts": [{"text": REQUEST.user}]}]
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == FINDINGS_SCHEMA
    assert config["maxOutputTokens"] > 0


def test_gemini_joins_text_parts():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_gemini_payload(
                candidates=[
                    {
                        "content": {"parts": [{"text": '{"find'}, {"text": 'ings": []}'}]},
                        "finishReason": "STOP",
                    }
                ]
            ),
        )

    response = _gemini_with_fake(handler).review(REQUEST)
    assert response.raw_text == '{"findings": []}'


def test_gemini_maps_blocked_prompt_to_refusal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}
        )

    response = _gemini_with_fake(handler).review(REQUEST)
    assert response.refused is True
    assert response.raw_text == ""


def test_gemini_maps_safety_stop_to_refusal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_gemini_payload(candidates=[{"content": {"parts": []}, "finishReason": "SAFETY"}]),
        )

    response = _gemini_with_fake(handler).review(REQUEST)
    assert response.refused is True


def test_gemini_marks_truncated_output():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_gemini_payload(
                candidates=[
                    {"content": {"parts": [{"text": '{"findi'}]}, "finishReason": "MAX_TOKENS"}
                ]
            ),
        )

    response = _gemini_with_fake(handler).review(REQUEST)
    assert response.truncated is True
    assert response.raw_text == '{"findi'


def test_gemini_empty_candidates_degrade_to_unparseable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": []})

    response = _gemini_with_fake(handler).review(REQUEST)
    assert response.refused is False
    assert response.raw_text == ""  # parse_candidates(None) upstream counts it


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_gemini_config_errors_are_permanent(status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "bad key or model"}})

    with pytest.raises(AIProviderConfigError):
        _gemini_with_fake(handler).review(REQUEST)


@pytest.mark.parametrize("status", [429, 500])
def test_gemini_rate_limit_and_server_errors_stay_transient(status):
    # 429 is the dominant failure mode on the free tier: it must reach the
    # retry path, never the permanent blame-the-key path
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "boom"}})

    with pytest.raises(httpx.HTTPStatusError):
        _gemini_with_fake(handler).review(REQUEST)


# --- openai ---


def _openai_payload(**overrides) -> dict:
    payload = {
        "model": "gpt-5.6-terra-2026-07-01",
        "choices": [
            {
                "message": {"role": "assistant", "content": json.dumps({"findings": []})},
                "finish_reason": "stop",
            }
        ],
    }
    return {**payload, **overrides}


def _openai_with_fake(handler) -> OpenAIProvider:
    return OpenAIProvider(
        api_key="sk-test-key",
        model="gpt-5.6-terra",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_openai_request_shape():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_openai_payload())

    response = _openai_with_fake(handler).review(REQUEST)

    assert response.refused is False
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    # The key travels in a header, never in the URL where it could hit logs
    assert seen["headers"]["authorization"] == "Bearer sk-test-key"
    assert "sk-test-key" not in seen["url"]
    body = seen["body"]
    assert body["model"] == "gpt-5.6-terra"
    assert body["messages"] == [
        {"role": "system", "content": REQUEST.system},
        {"role": "user", "content": REQUEST.user},
    ]
    schema = body["response_format"]["json_schema"]
    assert body["response_format"]["type"] == "json_schema"
    assert schema["strict"] is True
    assert schema["schema"] == FINDINGS_SCHEMA
    # Reasoning-capable models reject max_tokens
    assert body["max_completion_tokens"] > 0
    assert "max_tokens" not in body


def test_openai_reports_the_resolved_model_not_the_alias():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_openai_payload())

    response = _openai_with_fake(handler).review(REQUEST)
    assert response.model == "gpt-5.6-terra-2026-07-01"


def test_openai_maps_refusal_field_to_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_openai_payload(
                choices=[
                    {
                        "message": {"role": "assistant", "content": None, "refusal": "I cannot"},
                        "finish_reason": "stop",
                    }
                ]
            ),
        )

    response = _openai_with_fake(handler).review(REQUEST)
    assert response.refused is True
    assert response.raw_text == ""


def test_openai_marks_truncated_output():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_openai_payload(
                choices=[
                    {
                        "message": {"role": "assistant", "content": '{"findi'},
                        "finish_reason": "length",
                    }
                ]
            ),
        )

    response = _openai_with_fake(handler).review(REQUEST)
    assert response.truncated is True
    assert response.raw_text == '{"findi'


def test_openai_null_content_does_not_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_openai_payload(
                choices=[
                    {"message": {"role": "assistant", "content": None}, "finish_reason": "stop"}
                ]
            ),
        )

    response = _openai_with_fake(handler).review(REQUEST)
    assert response.raw_text == ""
    assert response.refused is False


def test_openai_empty_choices_degrade_to_unparseable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "gpt-5.6-terra", "choices": []})

    response = _openai_with_fake(handler).review(REQUEST)
    assert response.refused is False
    assert response.raw_text == ""


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_openai_config_errors_are_permanent(status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "bad key or model"}})

    with pytest.raises(AIProviderConfigError):
        _openai_with_fake(handler).review(REQUEST)


@pytest.mark.parametrize("status", [429, 500])
def test_openai_rate_limit_and_server_errors_stay_transient(status):
    # 429 must reach the job retry path, never the permanent blame-the-key path
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "boom"}})

    with pytest.raises(httpx.HTTPStatusError):
        _openai_with_fake(handler).review(REQUEST)


def test_openai_blocked_prompt_is_a_refusal_not_a_permanent_failure():
    """OpenAI signals a prompt-safety block as a 400, where the other two
    providers return a 200 refusal. It has to land on the same path: the
    prompt carries pull request content, so a third party picks the moment,
    and a permanent error would discard the deterministic findings too."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "Invalid prompt", "code": "invalid_prompt", "type": None}},
        )

    response = _openai_with_fake(handler).review(REQUEST)

    assert response.refused is True
    assert response.raw_text == ""


def test_openai_content_filter_is_a_refusal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_openai_payload(
                choices=[
                    {
                        "message": {"role": "assistant", "content": None},
                        "finish_reason": "content_filter",
                    }
                ]
            ),
        )

    response = _openai_with_fake(handler).review(REQUEST)
    assert response.refused is True


@pytest.mark.parametrize("field", ["code", "type"])
def test_openai_exhausted_quota_is_permanent(field):
    """A funded-out account 429s on every request, so retrying it three
    times and blaming the server would send the user chasing the wrong fix."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {field: "insufficient_quota"}})

    with pytest.raises(AIProviderConfigError, match="quota"):
        _openai_with_fake(handler).review(REQUEST)


def test_openai_plain_rate_limit_stays_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded"}})

    with pytest.raises(httpx.HTTPStatusError):
        _openai_with_fake(handler).review(REQUEST)


@pytest.mark.parametrize("status", [400, 429])
def test_openai_non_json_error_bodies_do_not_crash(status):
    """Gateways answer with HTML; the body probe must never assume JSON."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="<html>502 Bad Gateway</html>")

    expected = AIProviderConfigError if status == 400 else httpx.HTTPStatusError
    with pytest.raises(expected):
        _openai_with_fake(handler).review(REQUEST)


def test_openai_default_client_allows_a_slow_reasoning_completion():
    """The Gemini adapter's 120s is sized for a fast model. Copying it here
    would time out a large diff three times over and then invite a rerun."""
    from app.ai.openai_provider import READ_TIMEOUT_S

    provider = OpenAIProvider(api_key="sk-x", model="gpt-5.6-terra")
    assert READ_TIMEOUT_S >= 600
    assert provider._client.timeout.read == READ_TIMEOUT_S


# --- factory ---


def test_factory_gemini_requires_key(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ai_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "")
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        provider_from_settings()

    monkeypatch.setattr(settings, "gemini_api_key", "g-key")
    mode, provider = provider_from_settings()
    assert mode == "cheap"
    assert isinstance(provider, GeminiProvider)


def test_factory_openai_requires_key(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        provider_from_settings()

    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    mode, provider = provider_from_settings()
    assert mode == "cheap"
    assert isinstance(provider, OpenAIProvider)


def test_build_provider_applies_per_provider_default_models():
    anthropic_provider = build_provider("anthropic", "sk-ant-x", None)
    gemini_provider = build_provider("gemini", "g-x", None)
    openai_provider = build_provider("openai", "sk-x", None)
    assert isinstance(openai_provider, OpenAIProvider)
    assert openai_provider.model == "gpt-5.6-terra"
    assert isinstance(anthropic_provider, AnthropicProvider)
    assert anthropic_provider.model == "claude-opus-5"
    assert isinstance(gemini_provider, GeminiProvider)
    assert gemini_provider.model == "gemini-3.6-flash"
    custom = build_provider("gemini", "g-x", "gemini-2.5-pro")
    assert isinstance(custom, GeminiProvider)
    assert custom.model == "gemini-2.5-pro"


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
