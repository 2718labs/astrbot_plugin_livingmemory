"""Bounded three-generation continuity state for automatic fact recall."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True, slots=True)
class RecallContinuityEntry:
    """Two older generations retained for one physical conversation."""

    previous_fact_ids: tuple[str, ...]
    older_fact_ids: tuple[str, ...]
    memory_scope: str | None
    persona_id: str | None
    updated_at: float


@dataclass(frozen=True, slots=True)
class RecallContinuityWindow:
    """Previous-turn and two-turn-old slots consumed by one request."""

    previous_fact_ids: tuple[str, ...] = ()
    older_fact_ids: tuple[str, ...] = ()


class RecallContinuityCache:
    """A bounded, consume-on-read cache keyed by physical conversation id."""

    def __init__(
        self,
        *,
        max_sessions: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_sessions = max(1, int(max_sessions))
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._clock = clock
        self._entries: OrderedDict[str, RecallContinuityEntry] = OrderedDict()
        self._lock = asyncio.Lock()

    def _purge_expired(self, now: float) -> None:
        if self.ttl_seconds <= 0:
            return
        expired = [
            session_id
            for session_id, entry in self._entries.items()
            if now - entry.updated_at > self.ttl_seconds
        ]
        for session_id in expired:
            self._entries.pop(session_id, None)

    async def take(
        self,
        session_id: str,
        *,
        memory_scope: str | None,
        persona_id: str | None,
    ) -> RecallContinuityWindow:
        """Consume both older generations when their scope signature still matches."""
        if not session_id:
            return RecallContinuityWindow()
        async with self._lock:
            self._purge_expired(self._clock())
            entry = self._entries.pop(session_id, None)
        if entry is None:
            return RecallContinuityWindow()
        if entry.memory_scope != memory_scope or entry.persona_id != persona_id:
            return RecallContinuityWindow()
        return RecallContinuityWindow(
            previous_fact_ids=entry.previous_fact_ids,
            older_fact_ids=entry.older_fact_ids,
        )

    async def put(
        self,
        session_id: str,
        previous_fact_ids: list[str] | tuple[str, ...],
        older_fact_ids: list[str] | tuple[str, ...] = (),
        *,
        memory_scope: str | None,
        persona_id: str | None,
    ) -> None:
        """Store the next two generations, preserving order and uniqueness."""
        if not session_id:
            return
        normalized_previous = tuple(
            dict.fromkeys(
                str(fact_id).strip()
                for fact_id in previous_fact_ids
                if fact_id
            )
        )
        previous_set = set(normalized_previous)
        normalized_older = tuple(
            fact_id
            for fact_id in dict.fromkeys(
                str(fact_id).strip() for fact_id in older_fact_ids if fact_id
            )
            if fact_id not in previous_set
        )
        async with self._lock:
            now = self._clock()
            self._purge_expired(now)
            self._entries.pop(session_id, None)
            if not normalized_previous and not normalized_older:
                return
            self._entries[session_id] = RecallContinuityEntry(
                previous_fact_ids=normalized_previous,
                older_fact_ids=normalized_older,
                memory_scope=memory_scope,
                persona_id=persona_id,
                updated_at=now,
            )
            while len(self._entries) > self.max_sessions:
                self._entries.popitem(last=False)

    async def clear(self, session_id: str | None = None) -> None:
        """Clear one conversation or the entire ephemeral cache."""
        async with self._lock:
            if session_id:
                self._entries.pop(session_id, None)
            else:
                self._entries.clear()


__all__ = [
    "RecallContinuityCache",
    "RecallContinuityEntry",
    "RecallContinuityWindow",
]
