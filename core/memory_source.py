"""Structured source-conversation helpers for durable important memories."""

from __future__ import annotations

from typing import Any

from .memory_scope import resolve_persona_display_name
from .models.conversation_models import Message


def normalize_source_persona_names(
    messages: list[dict[str, Any]],
    *,
    persona_id: str | None = None,
    persona_display_aliases: Any = "",
) -> list[dict[str, Any]]:
    """Return source rows with a persona-facing Bot name and intact account IDs."""
    normalized: list[dict[str, Any]] = []
    for raw_message in messages:
        message = dict(raw_message)
        raw_metadata = message.get("metadata")
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        is_bot = bool(
            metadata.get("is_bot_message", False)
            or str(message.get("role") or "") == "assistant"
        )
        if is_bot:
            message["sender_name"] = resolve_persona_display_name(
                persona_id,
                persona_display_aliases,
                sender_name=message.get("sender_name"),
                sender_id=message.get("sender_id"),
            )
        normalized.append(message)
    return normalized


def serialize_source_messages(
    messages: list[Any],
    *,
    persona_id: str | None = None,
    persona_display_aliases: Any = "",
) -> list[dict[str, Any]]:
    """Store only fields required for review and deterministic re-summarization."""
    serialized: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, dict):
            data = message
        elif hasattr(message, "to_dict"):
            data = message.to_dict()
        else:
            data = {
                key: getattr(message, key, None)
                for key in (
                    "id",
                    "session_id",
                    "role",
                    "content",
                    "sender_id",
                    "sender_name",
                    "group_id",
                    "platform",
                    "timestamp",
                )
            }
        content = Message.content_to_text(data.get("content"))
        if not content:
            continue
        raw_metadata = data.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        try:
            message_id = int(data.get("id") or 0)
        except (TypeError, ValueError):
            message_id = 0
        try:
            timestamp = float(data.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            timestamp = 0.0
        serialized.append(
            {
                "id": message_id,
                "session_id": str(data.get("session_id") or ""),
                "role": str(data.get("role") or "user"),
                "content": content,
                "sender_id": str(data.get("sender_id") or "unknown"),
                "sender_name": data.get("sender_name"),
                "group_id": data.get("group_id"),
                "platform": data.get("platform"),
                "timestamp": timestamp,
                "metadata": {
                    "is_bot_message": bool(metadata.get("is_bot_message", False))
                },
            }
        )
    return normalize_source_persona_names(
        serialized,
        persona_id=persona_id,
        persona_display_aliases=persona_display_aliases,
    )


def restore_source_messages(source: list[dict[str, Any]]) -> list[Message]:
    """Restore source rows to the model expected by MemoryProcessor."""
    return [Message.from_dict(item) for item in source]


__all__ = [
    "normalize_source_persona_names",
    "restore_source_messages",
    "serialize_source_messages",
]
