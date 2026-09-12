from __future__ import annotations

from typing import Protocol

from humanfallback.models import ClassificationResult


class Classifier(Protocol):
    name: str

    def classify(self, text: str) -> ClassificationResult: ...
