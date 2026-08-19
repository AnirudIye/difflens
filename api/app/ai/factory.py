"""Pick the review mode and provider from configuration."""

from app.ai.anthropic_provider import AnthropicProvider
from app.ai.mock import MockProvider
from app.analysis.ai_review import AIProvider
from app.config import settings


def provider_from_settings() -> tuple[str, AIProvider | None]:
    """Returns (pipeline mode, provider). Raises ValueError on misconfiguration."""
    kind = settings.ai_provider
    if kind == "off":
        return "deterministic_only", None
    if kind == "mock":
        return "cheap", MockProvider()
    if kind == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError("AI_PROVIDER=anthropic requires ANTHROPIC_API_KEY to be set")
        return "cheap", AnthropicProvider(settings.anthropic_api_key, settings.ai_model)
    raise ValueError(f"unknown AI_PROVIDER {kind!r}; expected off, mock, or anthropic")
