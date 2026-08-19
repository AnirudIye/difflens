"""Gemini via the REST generateContent endpoint.

Google AI Studio keys have a free tier, which makes this the zero-cost way
to run real AI reviews. Same trust posture as every provider: output is
schema-pinned server-side and still re-validated by the hallucination chain.

The API key travels in the x-goog-api-key header, never in the URL, so it
cannot leak into request logs.
"""

import httpx

from app.ai.errors import AIProviderConfigError
from app.analysis.ai_review import AIRequest, AIResponse

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"

# Findings JSON is bounded output; generous room without streaming
MAX_OUTPUT_TOKENS = 16000

# 4xx from generateContent means the key, model id, or request is wrong;
# retrying the identical request cannot help (429 is handled separately)
_CONFIG_STATUSES = {400, 401, 403, 404}


class GeminiProvider:
    def __init__(self, api_key: str, model: str, http_client: httpx.Client | None = None) -> None:
        self.model = model
        self._key = api_key
        self._client = http_client or httpx.Client(timeout=httpx.Timeout(120, connect=10))

    def review(self, request: AIRequest) -> AIResponse:
        response = self._client.post(
            f"{GEMINI_BASE_URL}/v1beta/models/{self.model}:generateContent",
            headers={"x-goog-api-key": self._key},
            json={
                "system_instruction": {"parts": [{"text": request.system}]},
                "contents": [{"role": "user", "parts": [{"text": request.user}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseJsonSchema": request.output_schema,
                    "maxOutputTokens": MAX_OUTPUT_TOKENS,
                },
            },
        )
        if response.status_code in _CONFIG_STATUSES:
            raise AIProviderConfigError(
                f"Gemini rejected the request ({response.status_code}): check the API key and model"
            )
        response.raise_for_status()  # 429/5xx: transient, the job retry path owns it
        data = response.json()

        feedback = data.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            return AIResponse(raw_text="", refused=True, model=self.model)
        candidates = data.get("candidates") or []
        if not candidates:
            # No block reason and no output: surfaces upstream as unusable output
            return AIResponse(raw_text="", refused=False, model=self.model)
        first = candidates[0]
        if first.get("finishReason") == "SAFETY":
            return AIResponse(raw_text="", refused=True, model=self.model)
        parts = (first.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        return AIResponse(
            raw_text=text,
            refused=False,
            model=self.model,
            truncated=first.get("finishReason") == "MAX_TOKENS",
        )
