"""Result types for automatic conversation-memory admission."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


MemoryProcessingStatus = Literal["store", "skip", "invalid"]


@dataclass(slots=True)
class MemoryWriteRecord:
    """One source-window parent ready for persistent storage."""

    content: str
    metadata: dict[str, Any]
    importance: float


@dataclass(slots=True)
class MemoryProcessingResult:
    """Outcome of validating and admitting one source conversation window.

    A source window produces at most one parent record.  The legacy
    ``content``/``metadata``/``importance`` fields remain as a transient view
    of that record; v3 facts are never persisted a second time as strings.
    """

    status: MemoryProcessingStatus
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    importance: float = 0.0
    stored_fact_count: int = 0
    skipped_fact_count: int = 0
    error: str | None = None
    records: list[MemoryWriteRecord] = field(default_factory=list)

    def iter_records(self) -> tuple[MemoryWriteRecord, ...]:
        """Return all admitted records, including one legacy transient record."""
        if self.records:
            return tuple(self.records)
        if self.status == "store" and self.content.strip():
            return (
                MemoryWriteRecord(
                    content=self.content,
                    metadata=self.metadata,
                    importance=self.importance,
                ),
            )
        return ()


class MemoryAdmissionSkipped(RuntimeError):
    """Raised by the legacy tuple API when a valid window stores no facts."""


class InvalidMemoryOutputError(ValueError):
    """Raised when an LLM response remains invalid after one format repair."""


__all__ = [
    "InvalidMemoryOutputError",
    "MemoryAdmissionSkipped",
    "MemoryProcessingResult",
    "MemoryProcessingStatus",
    "MemoryWriteRecord",
]
