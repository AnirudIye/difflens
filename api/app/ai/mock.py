"""The offline provider: deterministic, free, and the default everywhere.

CI, local dev, and production-without-a-key all run the full cheap-mode
pipeline through this. It answers with canned candidates (none by default),
so the AI plumbing is exercised end to end at zero cost.
"""

import json

from app.analysis.ai_review import AIRequest, AIResponse


class MockProvider:
    def __init__(self, candidates: list[dict] | None = None, refused: bool = False) -> None:
        self._candidates = candidates or []
        self._refused = refused

    def review(self, request: AIRequest) -> AIResponse:
        if self._refused:
            return AIResponse(raw_text="", refused=True, model="mock")
        return AIResponse(
            raw_text=json.dumps({"findings": self._candidates}), refused=False, model="mock"
        )
