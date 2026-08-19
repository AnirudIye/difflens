"""The real reviewer: Claude via the Anthropic SDK.

One structured-output request per review. The response is pinned to the
findings schema server-side, but the pure layer still re-parses and
re-validates everything: the model reads attacker-authored diff content, so
its output gets the same distrust as its input.

Server-side refusal fallbacks are enabled by default: if the model declines
a request on safety grounds, the API retries it on a fallback model inside
the same call. A refusal that survives the whole chain comes back as
refused=True and the review completes with deterministic findings only.
"""

import httpx
from anthropic import Anthropic

from app.analysis.ai_review import AIRequest, AIResponse

# Findings JSON is bounded output; 16K leaves generous room without streaming
MAX_OUTPUT_TOKENS = 16000


class AnthropicProvider:
    def __init__(self, api_key: str, model: str, http_client: httpx.Client | None = None) -> None:
        self.model = model
        self._client = Anthropic(api_key=api_key, http_client=http_client)

    def review(self, request: AIRequest) -> AIResponse:
        response = self._client.beta.messages.create(
            model=self.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=request.system,
            messages=[{"role": "user", "content": request.user}],
            output_config={"format": {"type": "json_schema", "schema": request.output_schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            return AIResponse(raw_text="", refused=True, model=str(response.model))
        text = next((block.text for block in response.content if block.type == "text"), "")
        return AIResponse(
            raw_text=text,
            refused=False,
            model=str(response.model),
            truncated=response.stop_reason == "max_tokens",
        )
