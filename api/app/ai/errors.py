"""Shared provider error taxonomy."""


class AIProviderConfigError(Exception):
    """A failure no retry can fix: bad key, bad model id, rejected request.

    The worker treats this as a permanent review failure without burning
    retry attempts, mirroring how the Anthropic SDK's auth errors are handled.
    """


class UserAIKeyError(ValueError):
    """The user's stored key is unusable (e.g. undecryptable after a
    TOKEN_ENCRYPTION_KEY rotation). Only the user can fix it, by saving the
    key again, so the worker must point the failure message at them."""
