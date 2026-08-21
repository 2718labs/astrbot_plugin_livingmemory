"""LivingMemory v3 identifiers and source-boundary helpers."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Iterable

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


def source_ids_for_indexes(
    messages: list[Message], indexes: Iterable[int]
) -> list[int | str]:
    refs: list[int | str] = []
    for index in indexes:
        if index < 1 or index > len(messages):
            raise ValueError(f"source index out of range: {index}")
        ref = message_reference(messages[index - 1], index)
        if ref not in refs:
            refs.append(ref)
    return refs


def source_messages_for_indexes(
    messages: list[Message], indexes: Iterable[int]
) -> list[Message]:
    return [messages[index - 1] for index in indexes]


_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _time_of_day(raw: str) -> tuple[int, int] | None:
    match = re.search(r"(?:(上午|中午|下午|晚上|凌晨))?(\d{1,2})[点时](?:(\d{1,2})分?)?", raw)
    if not match:
        return None
    period, hour_text, minute_text = match.groups()
    hour = int(hour_text)
    minute = int(minute_text or 0)
    if period in {"下午", "晚上"} and hour < 12:
        hour += 12
    if period == "中午" and hour < 11:
        hour += 12
    if period == "凌晨" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError(f"invalid time expression: {raw}")
    return hour, minute


def _resolved_relative_date(raw: str, base: datetime) -> datetime | None:
    for token, delta in (("前天", -2), ("昨天", -1), ("昨日", -1), ("今天", 0), ("今日", 0), ("明天", 1), ("明日", 1), ("后天", 2)):
        if token in raw:
            return base + timedelta(days=delta)

    match = re.search(r"(下周|本周|这周|周)([一二三四五六日天])", raw)
    if match:
        prefix, weekday_text = match.groups()
        target_weekday = _WEEKDAYS[weekday_text]
        if prefix == "下周":
            delta = 7 - base.weekday() + target_weekday
        elif prefix in {"本周", "这周"}:
            delta = target_weekday - base.weekday()
        else:
            delta = target_weekday - base.weekday()
            if delta <= 0:
                delta += 7
        return base + timedelta(days=delta)

    explicit = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?", raw)
    if explicit:
        year, month, day = map(int, explicit.groups())
        return base.replace(year=year, month=month, day=day)
    return None


def validate_and_normalize_time(
    value: dict[str, Any] | None,
    source_messages: list[Message],
) -> dict[str, Any] | None:
    """Resolve common relative expressions from their direct source time."""
    if value is None:
        return None
    raw = str(value.get("raw") or "").strip()
    normalized = str(value.get("normalized") or "").strip()
    precision = str(value.get("precision") or "").strip()
    if not raw or not normalized or precision not in {"day", "minute", "month", "year"}:
        raise ValueError("time requires raw, normalized and a supported precision")
    if not source_messages:
        raise ValueError("time requires at least one direct source message")
    source_text = "\n".join(Message.content_to_text(item.content) for item in source_messages)
    if raw not in source_text:
        raise ValueError(f"time.raw is not present in its source messages: {raw}")

    base = datetime.fromtimestamp(float(source_messages[0].timestamp)).astimezone()
    resolved = _resolved_relative_date(raw, base)
    time_value = _time_of_day(raw)
    if resolved is not None:
        if time_value is not None:
            resolved = resolved.replace(
                hour=time_value[0], minute=time_value[1], second=0, microsecond=0
            )
            expected = resolved.strftime("%Y-%m-%dT%H:%M")
            expected_precision = "minute"
        else:
            expected = resolved.strftime("%Y-%m-%d")
            expected_precision = "day"
        if normalized != expected:
            raise ValueError(
                f"time.normalized does not match source time: expected {expected}"
            )
        precision = expected_precision
    else:
        try:
            if precision == "day":
                datetime.strptime(normalized, "%Y-%m-%d")
            elif precision == "minute":
                datetime.strptime(normalized, "%Y-%m-%dT%H:%M")
            elif precision == "month":
                datetime.strptime(normalized, "%Y-%m")
            else:
                datetime.strptime(normalized, "%Y")
        except ValueError as exc:
            raise ValueError(f"invalid normalized time: {normalized}") from exc
    return {"raw": raw, "normalized": normalized, "precision": precision}


__all__ = [
    "MEMORY_GENERATION_VERSION",
    "MEMORY_SCHEMA_VERSION",
    "build_source_descriptor",
    "concept_key",
    "memory_idempotency_key",
    "normalize_concept_name",
    "parent_memory_id",
    "participant_id",
    "source_ids_for_indexes",
    "source_messages_for_indexes",
    "stable_fact_id",
    "topic_id",
    "validate_and_normalize_time",
]
