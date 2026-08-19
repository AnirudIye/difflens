"""Pick the review mode and provider: the user's own key first, then config."""

import uuid

from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.anthropic_provider import AnthropicProvider
from app.ai.errors import UserAIKeyError
from app.ai.gemini_provider import GeminiProvider
from app.ai.mock import MockProvider
from app.analysis.ai_review import AIProvider
from app.config import settings
from app.models import UserAIKey
from app.security import decrypt_token

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "gemini": "gemini-3.6-flash",
}


def build_provider(kind: str, api_key: str, model: str | None) -> AIProvider:
    if kind == "anthropic":
        return AnthropicProvider(api_key, model or DEFAULT_MODELS["anthropic"])
    if kind == "gemini":
        return GeminiProvider(api_key, model or DEFAULT_MODELS["gemini"])
    raise ValueError(f"unknown AI provider {kind!r}")


def provider_from_settings() -> tuple[str, AIProvider | None]:
    """The server-configured default. Raises ValueError on misconfiguration."""
    kind = settings.ai_provider
    if kind == "off":
        return "deterministic_only", None
    if kind == "mock":
        return "cheap", MockProvider()
    if kind == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError("AI_PROVIDER=anthropic requires ANTHROPIC_API_KEY to be set")
        return "cheap", build_provider(
            "anthropic", settings.anthropic_api_key, settings.ai_model or None
        )
    if kind == "gemini":
        if not settings.gemini_api_key:
            raise ValueError("AI_PROVIDER=gemini requires GEMINI_API_KEY to be set")
        return "cheap", build_provider("gemini", settings.gemini_api_key, settings.ai_model or None)
    raise ValueError(f"unknown AI_PROVIDER {kind!r}; expected off, mock, anthropic, or gemini")


def resolve_provider(db: Session, user_id: uuid.UUID) -> tuple[str, AIProvider | None, str]:
    """(mode, provider, source) for one user's review; their own key wins.

    source is "user" (their stored key), "server" (config), or "none" —
    the worker uses it to blame the right party when the provider fails.
    """
    row = db.execute(select(UserAIKey).where(UserAIKey.user_id == user_id)).scalar_one_or_none()
    if row is not None:
        try:
            api_key = decrypt_token(row.key_enc)
        except InvalidToken as exc:
            raise UserAIKeyError(
                "the stored API key could not be decrypted; save it again in Settings"
            ) from exc
        return "cheap", build_provider(row.provider, api_key, row.model), "user"
    mode, provider = provider_from_settings()
    return mode, provider, "server" if provider is not None else "none"
