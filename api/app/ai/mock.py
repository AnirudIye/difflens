"""The offline provider: deterministic, free, and the default everywhere.

CI, local dev, and production-without-a-key all run the full cheap-mode
pipeline through this. It answers with canned candidates (none by default),
so the AI plumbing is exercised end to end at zero cost.
"""

import json

from app.analysis.ai_review import AIRequest, AIResponse


class MockProvider:
    """Canned provider output, reported under a caller-chosen model name.

    `model` exists because the public demo replays a recorded response
    through this class and must not report itself as the mock: `_ai_note`
    answers a mock with "No AI reviewer is configured", which is true of the
    default empty mock and false of a replay that does return findings.
    The default keeps every existing caller, and its tests, unchanged.
    """

    def __init__(
        self,
        candidates: list[dict] | None = None,
        refused: bool = False,
        model: str = "mock",
    ) -> None:
        self._candidates = candidates or []
        self._refused = refused
        self._model = model

    def review(self, request: AIRequest) -> AIResponse:
        if self._refused:
            return AIResponse(raw_text="", refused=True, model=self._model)
        return AIResponse(
            raw_text=json.dumps({"findings": self._candidates}), refused=False, model=self._model
        )
