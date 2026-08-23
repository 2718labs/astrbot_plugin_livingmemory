"""Pack complete fact hits into a bounded production injection payload."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable


def estimate_token_count(text: str) -> int:
    """Estimate text tokens without treating every UTF-8 byte as one token.

    AstrBot cannot expose one exact tokenizer for every OpenAI-compatible
    provider.  Count CJK/non-ASCII characters conservatively as one token and
    ASCII text as roughly four characters per token.  This keeps the configured
    budgets in token-like units while avoiding the old 3x penalty on Chinese.
    """
    value = str(text or "")
    non_ascii = sum(ord(char) > 127 for char in value)
    ascii_chars = len(value) - non_ascii
    return int(math.ceil(non_ascii + ascii_chars / 4))


def token_upper_bound(text: str) -> int:
    """Backward-compatible name for the provider-independent token estimate."""
    return estimate_token_count(text)


def _reaction_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    parts: list[str] = []
    for key in ("emotion", "thought"):
        text = str(value.get(key) or "").strip()
        if text and text not in parts:
            parts.append(text)
    return "；".join(parts)


def fact_entry_text(hit: Any, *, include_reaction: bool = True) -> str:
    """Render one complete fact without parent summary or sibling metadata."""
    content = str(getattr(hit, "content", "") or "").strip()
    metadata = getattr(hit, "metadata", {}) or {}
    if metadata.get("recent_summary"):
        lines = [f"- 最近对话摘要：{content}"]
    else:
        lines = [f"- {content}"]
    if include_reaction:
        reaction = _reaction_text(metadata.get("persona_reaction"))
        if reaction:
            lines.append(f"  当时反应：{reaction}")
    return "\n".join(lines)


def _format_timestamp(value: Any) -> str:
    """Render a unix/ISO timestamp as 'YYYY-MM-DD HH:MM', or empty string."""
    if isinstance(value, (int, float)):
        timestamp = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ""
        try:
            timestamp = float(stripped)
        except ValueError:
            try:
                return datetime.fromisoformat(
                    stripped.replace("Z", "+00:00")
                ).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                return ""
    else:
        return ""
    if timestamp > 100_000_000_000:
        timestamp /= 1000.0
    if timestamp <= 0:
        return ""
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def fact_header_text(hit: Any, index: int) -> str:
    """Render the per-entry prefix: 记忆 #N (重要性: X, 写入时间: ...)."""
    metadata = getattr(hit, "metadata", {}) or {}
    if metadata.get("recent_summary"):
        return f"最近对话 #{index}"
    parts = [f"记忆 #{index}"]
    meta_parts: list[str] = []
    importance = metadata.get("importance")
    if isinstance(importance, (int, float)):
        meta_parts.append(f"重要性: {importance:.2f}")
    time_text = _format_timestamp(metadata.get("create_time"))
    if time_text:
        meta_parts.append(f"写入时间: {time_text}")
    if meta_parts:
        parts.append("(" + ", ".join(meta_parts) + ")")
    return " ".join(parts)


def format_fact_hits_for_injection(
    hits: list[Any], *, include_reaction: bool = True
) -> str:
    """Format only the selected canonical facts for the current answer."""
    if not hits:
        return ""
    from ..base.constants import MEMORY_INJECTION_FOOTER, MEMORY_INJECTION_HEADER
    from ..prompts.prompt_manager import get_prompt_manager

    try:
        manager = get_prompt_manager()
        header_body = manager.get_prompt("memory_injection_header") if manager else ""
        footer_body = manager.get_prompt("memory_injection_footer") if manager else ""
    except Exception:
        header_body = ""
        footer_body = ""
    if not header_body:
        header_body = (
            "以下是与当前问题直接相关的历史事实，仅作背景。"
            "若与用户当前说法冲突，以当前消息为准。"
        )
    if not footer_body:
        footer_body = "自然使用相关事实，不要主动宣布或逐条复述记忆。"

    body_lines: list[str] = []
    for index, hit in enumerate(hits, start=1):
        body_lines.append(fact_header_text(hit, index))
        body_lines.append(fact_entry_text(hit, include_reaction=include_reaction))
    body = "\n".join(body_lines)
    return (
        f"{MEMORY_INJECTION_HEADER}\n{header_body}\n\n"
        f"{body}\n\n{footer_body}\n{MEMORY_INJECTION_FOOTER}"
    )


@dataclass(slots=True)
class PackedFacts:
    hits: list[Any] = field(default_factory=list)
    token_count: int = 0
    token_budget: int = 0
    dropped: list[dict[str, str]] = field(default_factory=list)


def pack_fact_hits(
    hits: list[Any],
    *,
    token_budget: int,
    single_fact_budget: int,
    include_reaction: bool = True,
    renderer: Callable[[list[Any]], str] | None = None,
) -> PackedFacts:
    """Greedily pack ranked complete facts without truncating any fact."""
    budget = max(0, int(token_budget))
    single_budget = max(1, int(single_fact_budget))
    render = renderer or (
        lambda selected: format_fact_hits_for_injection(
            selected, include_reaction=include_reaction
        )
    )
    selected: list[Any] = []
    dropped: list[dict[str, str]] = []
    seen_fact_ids: set[str] = set()
    seen_contents: set[str] = set()

    for hit in hits:
        metadata = getattr(hit, "metadata", {}) or {}
        fact_id = str(metadata.get("fact_id") or "").strip()
        content = str(getattr(hit, "content", "") or "").strip()
        label = fact_id or content[:80]
        if not content or (fact_id and fact_id in seen_fact_ids) or content in seen_contents:
            dropped.append({"fact_id": label, "reason": "duplicate_or_empty"})
            continue
        entry_tokens = token_upper_bound(
            fact_entry_text(hit, include_reaction=include_reaction)
        )
        if entry_tokens > single_budget:
            dropped.append({"fact_id": label, "reason": "single_fact_budget"})
            continue
        candidate = [*selected, hit]
        payload_tokens = token_upper_bound(render(candidate))
        if payload_tokens > budget:
            dropped.append({"fact_id": label, "reason": "total_budget"})
            break
        selected = candidate
        if fact_id:
            seen_fact_ids.add(fact_id)
        seen_contents.add(content)

    payload = render(selected) if selected else ""
    return PackedFacts(
        hits=selected,
        token_count=token_upper_bound(payload),
        token_budget=budget,
        dropped=dropped,
    )


__all__ = [
    "PackedFacts",
    "estimate_token_count",
    "fact_entry_text",
    "format_fact_hits_for_injection",
    "pack_fact_hits",
    "token_upper_bound",
]
