"""Result types for automatic conversation-memory admission."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


MemoryProcessingStatus = Literal["store", "skip", "invalid"]


@dataclass(slots=True)
class MemoryProcessingResult:
    """Outcome of validating and admitting one source conversation window."""

    status: MemoryProcessingStatus
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    importance: float = 0.0
    stored_fact_count: int = 0
    skipped_fact_count: int = 0
    error: str | None = None


class MemoryAdmissionSkipped(RuntimeError):
    """Raised by the legacy tuple API when a valid window stores no facts."""


class InvalidMemoryOutputError(ValueError):
    """Raised when an LLM response remains invalid after one format repair."""


__all__ = [
    "InvalidMemoryOutputError",
    "MemoryAdmissionSkipped",
    "MemoryProcessingResult",
    "MemoryProcessingStatus",
]
