"""LivingMemory v3 identifiers and source-boundary helpers."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from .conversation_models import Message


MEMORY_SCHEMA_VERSION = "v3"
MEMORY_GENERATION_VERSION = "s1-v1"


def _stable_hash(value: Any, length: int = 24) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def normalize_concept_name(value: str) -> str:
    """Normalize a topic for exact, explainable reuse without semantic merging."""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", normalized).strip()


def concept_key(value: str) -> str:
    return normalize_concept_name(value).casefold()


def topic_id(scope: str, name: str) -> str:
    return f"topic_{_stable_hash({'scope': scope, 'name': concept_key(name)})}"


def participant_id(scope: str, name: str) -> str:
    return f"person_{_stable_hash({'scope': scope, 'name': concept_key(name)})}"


def message_reference(message: Message, index: int) -> int | str:
    """Use the database ID when available, otherwise a deterministic local ref."""
    if isinstance(message.id, int) and message.id > 0:
        return message.id
    return "msg_" + _stable_hash(
        {
            "index": index,
            "session_id": message.session_id,
            "role": message.role,
            "sender_id": message.sender_id,
            "timestamp": round(float(message.timestamp), 6),
            "content": Message.content_to_text(message.content),
        },
        length=20,
    )


def build_source_descriptor(
    messages: list[Message],
    scope: str | None = None,
    generation_version: str = MEMORY_GENERATION_VERSION,
) -> dict[str, Any]:
    """Build a stable source boundary from message identities, not row offsets."""
    if not messages:
        raise ValueError("source messages cannot be empty")
    resolved_scope = str(scope or messages[0].session_id or "").strip()
    refs = [message_reference(message, i) for i, message in enumerate(messages, 1)]
    source_rows = [
        {
            "ref": refs[i - 1],
            "role": message.role,
            "sender_id": str(message.sender_id or ""),
            "timestamp": round(float(message.timestamp), 6),
            "content_hash": _stable_hash(
                Message.content_to_text(message.content), length=20
            ),
        }
        for i, message in enumerate(messages, 1)
    ]
    fingerprint = "src_" + _stable_hash(
        {"scope": resolved_scope, "messages": source_rows}
    )
    return {
        "scope": resolved_scope,
        "first_message_id": refs[0],
        "last_message_id": refs[-1],
        "message_ids": refs,
        "message_count": len(refs),
        "generation_version": generation_version,
        "fingerprint": fingerprint,
    }


def build_explicit_source_descriptor(
    scope: str,
    content: Any,
    *,
    origin: str = "agent_memorize_tool",
    source_reference: str | int | None = None,
) -> dict[str, Any]:
    """Build a stable source identity for an explicit remember request."""
    resolved_scope = str(scope or "").strip()
    payload = {
        "scope": resolved_scope,
        "origin": str(origin or "explicit"),
        "content": content,
    }
    reference: str | int = source_reference or (
        "intent_" + _stable_hash(payload, length=20)
    )
    return {
        "scope": resolved_scope,
        "first_message_id": reference,
        "last_message_id": reference,
        "message_ids": [reference],
        "message_count": 1,
        "generation_version": MEMORY_GENERATION_VERSION,
        "fingerprint": "src_" + _stable_hash(payload),
        "triggered_by": "explicit",
        "origin": str(origin or "explicit"),
    }


def parent_memory_id(source_fingerprint: str, unit_key: Any) -> str:
    return "memory_" + _stable_hash(
        {"source_fingerprint": source_fingerprint, "unit_key": unit_key}
    )


def memory_idempotency_key(source_fingerprint: str, unit_key: Any) -> str:
    return "idem_" + _stable_hash(
        {"source_fingerprint": source_fingerprint, "unit_key": unit_key}
    )


def stable_fact_id(parent_id: str, fact_key: Any) -> str:
    return "fact_" + _stable_hash(
        {"parent_id": parent_id, "fact_key": fact_key}
    )


__all__ = [
    "MEMORY_GENERATION_VERSION",
    "MEMORY_SCHEMA_VERSION",
    "build_source_descriptor",
    "build_explicit_source_descriptor",
    "concept_key",
    "memory_idempotency_key",
    "normalize_concept_name",
    "parent_memory_id",
    "participant_id",
    "stable_fact_id",
    "topic_id",
]
