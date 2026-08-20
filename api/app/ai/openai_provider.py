"""OpenAI via the REST chat completions endpoint.

Talks HTTP directly rather than through the openai SDK: the request is one
POST, and the Gemini adapter already established that shape, so a second
vendor SDK would be dependency weight for nothing.

Same trust posture as every provider: the schema is pinned server-side with
strict structured outputs, and the pure layer still re-parses and
re-validates, because the model reads attacker-authored diff content.

Where this deliberately differs from the Gemini adapter it copies:
OpenAI overloads its status codes, so the status alone does not say whether
a retry could help. A 400 can mean a blocked prompt and a 429 can mean an
empty account, and both need the body to tell them apart.

The key travels in the Authorization header, never in the URL, so it cannot
leak into request logs.
"""

import httpx

from app.ai.errors import AIProviderConfigError
from app.analysis.ai_review import AIRequest, AIResponse

OPENAI_BASE_URL = "https://api.openai.com"

# Findings JSON is bounded output; generous room without streaming
MAX_OUTPUT_TOKENS = 16000

# The default model reasons before it answers, and this request is not
# streamed, so nothing arrives until the whole completion is done. Gemini
# Flash finishes inside 120s; matching the Anthropic SDK's 600s here stops a
# large diff from timing out three times and then advising a pointless rerun.
READ_TIMEOUT_S = 600

# 4xx here means the key, model id, or request is wrong, and an identical
# retry cannot help. 429 is deliberately absent: it is usually the
# rate-limit path, which belongs to the job retry logic.
_CONFIG_STATUSES = {400, 401, 403, 404}

# A safety block on the prompt. The other providers report this as a refusal
# on a 200, and it has to land in the same place here: the prompt embeds
# pull request content, so a third party chooses when it fires, and a
# permanent error would throw away the deterministic findings too.
_PROMPT_BLOCKED = "invalid_prompt"

# A 429 that no amount of waiting fixes: the account is out of credit or
# past its billing cap. Common enough for bring-your-own-key users that
# retrying it three times and blaming the server would be actively unhelpful.
_NO_QUOTA = "insufficient_quota"


def _error_fields(response: httpx.Response) -> dict:
    """The error object from a JSON error body, empty when there is not one.

    Gateways and proxies answer with HTML, so this can never assume JSON.
    """
    try:
        payload = response.json()
    except ValueError:
        return {}
    error = payload.get("error") if isinstance(payload, dict) else None
    return error if isinstance(error, dict) else {}


class OpenAIProvider:
    def __init__(self, api_key: str, model: str, http_client: httpx.Client | None = None) -> None:
        self.model = model
        self._key = api_key
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=10)
        )

    def review(self, request: AIRequest) -> AIResponse:
        response = self._client.post(
            f"{OPENAI_BASE_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self._key}"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.user},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "findings",
                        "strict": True,
                        "schema": request.output_schema,
                    },
                },
                # max_tokens is rejected by the current reasoning-capable
                # models; max_completion_tokens is the parameter they take
                "max_completion_tokens": MAX_OUTPUT_TOKENS,
            },
        )

        if response.status_code in (400, 429):
            fields = _error_fields(response)
            code = fields.get("code")
            kind = fields.get("type")
            if response.status_code == 400 and code == _PROMPT_BLOCKED:
                return AIResponse(raw_text="", refused=True, model=self.model)
            if response.status_code == 429 and _NO_QUOTA in (code, kind):
                raise AIProviderConfigError(
                    "OpenAI rejected the request (429): the account has no remaining quota"
                )

        if response.status_code in _CONFIG_STATUSES:
            raise AIProviderConfigError(
                f"OpenAI rejected the request ({response.status_code}): check the API key and model"
            )
        response.raise_for_status()  # 429/5xx: transient, the job retry path owns it
        data = response.json()

        # The echoed model is the resolved id, which can be more specific
        # than the alias that was asked for
        model = data.get("model") or self.model
        choices = data.get("choices") or []
        if not choices:
            # No refusal and no output: surfaces upstream as unusable output
            return AIResponse(raw_text="", refused=False, model=model)
        choice = choices[0]
        message = choice.get("message") or {}

        # Structured outputs put a declined request in its own field rather
        # than returning prose that would fail schema validation downstream
        if message.get("refusal"):
            return AIResponse(raw_text="", refused=True, model=model)
        # A filtered response carries no refusal field, so without this it
        # would look like malformed output rather than a decline
        if choice.get("finish_reason") == "content_filter":
            return AIResponse(raw_text="", refused=True, model=model)

        return AIResponse(
            raw_text=message.get("content") or "",
            refused=False,
            model=model,
            truncated=choice.get("finish_reason") == "length",
        )
