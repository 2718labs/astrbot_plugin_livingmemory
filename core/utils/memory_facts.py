"""Read fact text from legacy and v3 metadata without creating a second truth."""

from __future__ import annotations

from typing import Any, Iterable


def fact_objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def fact_texts(value: Any, limit: int | None = None) -> list[str]:
    if not isinstance(value, list):
        return []
    texts: list[str] = []
    for item in value:
        raw = item.get("fact") if isinstance(item, dict) else item
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = raw.strip()
        if text not in texts:
            texts.append(text)
        if limit is not None and len(texts) >= limit:
            break
    return texts


def fact_texts_from_metadata(
    metadata: dict[str, Any] | None, limit: int | None = None
) -> list[str]:
    return fact_texts((metadata or {}).get("key_facts"), limit=limit)


def unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value.strip() not in result:
            result.append(value.strip())
    return result


__all__ = ["fact_objects", "fact_texts", "fact_texts_from_metadata", "unique_strings"]
