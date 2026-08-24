"""供 Agent 主动调用的长期记忆写入工具。"""

import asyncio
import json
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.platform import MessageType
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..memory_scope import (
    is_event_memory_allowed,
    resolve_memory_scope,
    resolve_sender_alias,
)
from ..models.memory_contract import (
    build_participant_identity,
    concept_key,
    normalize_concept_name,
)
from ..utils import get_persona_id


TOPIC_CANDIDATE_LIMIT = 5


def _json_result(data: dict[str, Any]) -> str:
    """将工具结果稳定序列化为 JSON 文本。"""
    return json.dumps(data, ensure_ascii=False, default=str)


def _normalize_list(value: Any, limit: int = 5) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()][:limit]
    if isinstance(value, str) and value.strip():
        return [value.strip()][:limit]
    return []


def _event_value(event: Any, method_name: str, attribute_name: str = "") -> str:
    getter = getattr(event, method_name, None)
    if callable(getter):
        try:
            value = getter()
            return str(value).strip() if value is not None else ""
        except Exception:
            return ""
    value = getattr(event, attribute_name or method_name, "")
    return str(value).strip() if value is not None else ""


def _raw_value(source: Any, key: str) -> str:
    if source is None:
        return ""
    value = source.get(key) if isinstance(source, dict) else getattr(source, key, "")
    if not isinstance(value, (str, int, float)):
        return ""
    return str(value).strip() if value is not None else ""


@dataclass
class MemoryMemorizeTool(FunctionTool[AstrAgentContext]):
    """长期记忆主动写入工具。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Any = None
    config_manager: Any = None
    memory_engine: Any = None
    memory_processor: Any = None

    name: str = "memorize_long_term_memory"
    description: str = (
        "Memorize durable long-term memory when the user explicitly asks to remember something, "
        "or when stable preferences, identity details, agreements, or project context appear. "
        "Write concise factual memory, not the full conversation. This is a two-step tool: "
        "first call with memory and no topic selection to receive relevant existing topics; "
        "then call again with topic_id, new_topic, or commit_without_topic=true to save."
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "memory": {
                    "type": "string",
                    "description": "Concise factual long-term memory to save. Do not copy the full conversation.",
                },
                "topic_id": {
                    "type": "string",
                    "description": "An existing topic_id returned by the first call. Leave empty on the first call.",
                    "default": "",
                },
                "new_topic": {
                    "type": "string",
                    "description": "A new human-readable topic name. Use only on the second call when no returned topic fits.",
                    "default": "",
                },
                "commit_without_topic": {
                    "type": "boolean",
                    "description": "On the second call, explicitly save without a topic when no topic is appropriate.",
                    "default": False,
                },
                "key_facts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional key facts supporting the memory, up to 5.",
                    "default": [],
                },
                "participants": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional people directly involved in these facts, up to 8.",
                    "default": [],
                },
                "sentiment": {
                    "type": "string",
                    "description": "Sentiment of the memory: positive, neutral, or negative.",
                    "default": "neutral",
                },
                "importance": {
                    "type": "number",
                    "description": "Importance from 0.0 to 1.0. Use higher values for durable preferences, commitments, or identity facts.",
                    "default": 0.7,
                },
                "reason": {
                    "type": "string",
                    "description": "Optional short reason why this information should be remembered.",
                    "default": "",
                },
            },
            "required": ["memory"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        memory: str,
        topic_id: str = "",
        new_topic: str = "",
        commit_without_topic: bool = False,
        key_facts: list[str] | None = None,
        participants: list[str] | None = None,
        sentiment: str = "neutral",
        importance: float = 0.7,
        reason: str = "",
    ) -> ToolExecResult:
        """执行长期记忆写入。"""
        cleaned_memory = (memory or "").strip()
        if not cleaned_memory:
            return _json_result({"memorized": False, "error": "memory is empty"})

        normalized_sentiment = str(sentiment or "neutral").strip().lower()
        if normalized_sentiment not in {"positive", "neutral", "negative"}:
            normalized_sentiment = "neutral"

        if (
            self.context is None
            or self.memory_engine is None
            or self.memory_processor is None
        ):
            return _json_result(
                {
                    "memorized": False,
                    "error": "memory memorize tool is not initialized",
                }
            )

        try:
            event = context.context.event
            if not is_event_memory_allowed(self.config_manager, event):
                return _json_result(
                    {"memorized": False, "error": "memory access is not allowed"}
                )
            session_id = event.unified_msg_origin
            memory_scope = (
                resolve_memory_scope(self.config_manager, event) or session_id
            )
            persona_id = await get_persona_id(self.context, event)
            is_group_chat = event.get_message_type() == MessageType.GROUP_MESSAGE

            search_topic_candidates = getattr(
                self.memory_engine, "search_topic_candidates", None
            )
            topic_candidates = (
                await search_topic_candidates(
                    cleaned_memory,
                    scope=memory_scope,
                    persona_id=persona_id,
                    limit=TOPIC_CANDIDATE_LIMIT,
                )
                if callable(search_topic_candidates)
                else []
            )
            if not isinstance(topic_candidates, list):
                topic_candidates = []
            topic_candidates = [
                {
                    "topic_id": str(item.get("topic_id") or "").strip(),
                    "name": normalize_concept_name(str(item.get("name") or "")),
                }
                for item in topic_candidates
                if isinstance(item, dict)
                and str(item.get("topic_id") or "").strip()
                and normalize_concept_name(str(item.get("name") or ""))
            ][:TOPIC_CANDIDATE_LIMIT]

            selected_topic_id = str(topic_id or "").strip()
            selected_new_topic = normalize_concept_name(str(new_topic or ""))
            selection_count = sum(
                (
                    bool(selected_topic_id),
                    bool(selected_new_topic),
                    bool(commit_without_topic),
                )
            )
            if selection_count > 1:
                return _json_result(
                    {
                        "memorized": False,
                        "error": "choose exactly one of topic_id, new_topic, or commit_without_topic",
                    }
                )
            if selection_count == 0:
                return _json_result(
                    {
                        "memorized": False,
                        "requires_topic_selection": True,
                        "topic_candidates": topic_candidates,
                        "next_step": (
                            "Call memorize_long_term_memory again with the same memory and "
                            "exactly one of topic_id, new_topic, or commit_without_topic=true."
                        ),
                    }
                )

            selected_topics: list[str] = []
            selected_topic: dict[str, str] | None = None
            if selected_topic_id:
                selected_topic = next(
                    (
                        item
                        for item in topic_candidates
                        if item["topic_id"] == selected_topic_id
                    ),
                    None,
                )
                if selected_topic is None:
                    return _json_result(
                        {
                            "memorized": False,
                            "error": "topic_id is not a current candidate for this memory",
                            "topic_candidates": topic_candidates,
                        }
                    )
                selected_topics = [selected_topic["name"]]
            elif selected_new_topic:
                existing = next(
                    (
                        item
                        for item in topic_candidates
                        if concept_key(item["name"])
                        == concept_key(selected_new_topic)
                    ),
                    None,
                )
                if existing is not None:
                    return _json_result(
                        {
                            "memorized": False,
                            "error": "new_topic already exists; use its topic_id",
                            "existing_topic": existing,
                        }
                    )
                selected_topics = [selected_new_topic]

            platform = _event_value(event, "get_platform_name", "platform")
            sender_id = _event_value(event, "get_sender_id", "sender_id")
            sender_name = _event_value(event, "get_sender_name", "sender_name")
            aliases_value = ""
            config_get = getattr(self.config_manager, "get", None)
            if callable(config_get):
                aliases_value = config_get("access_control.identity_aliases", "")
            resolved_sender_name = resolve_sender_alias(
                aliases_value,
                platform,
                sender_id,
                sender_name,
            )
            participant_identities: list[dict[str, Any]] = []
            user_identity = build_participant_identity(
                platform=platform,
                sender_id=sender_id,
                display_name=resolved_sender_name or sender_name or sender_id,
                aliases=[sender_name] if sender_name else [],
                is_bot=False,
            )
            if user_identity:
                participant_identities.append(user_identity)

            self_id = _event_value(event, "get_self_id", "self_id")
            message_obj = getattr(event, "message_obj", None)
            raw_message = getattr(message_obj, "raw_message", None)
            bot_name = ""
            for source in (event, message_obj, raw_message):
                for key in ("bot_name", "bot_nickname", "self_name"):
                    bot_name = _raw_value(source, key)
                    if bot_name:
                        break
                if bot_name:
                    break
            bot_identity = build_participant_identity(
                platform=platform,
                sender_id=self_id,
                display_name=bot_name or self_id,
                aliases=[bot_name] if bot_name else [],
                is_bot=True,
            )
            if bot_identity and bot_identity["identity_key"] != user_identity.get(
                "identity_key"
            ):
                participant_identities.append(bot_identity)

            message_obj = getattr(event, "message_obj", None)
            source_reference = getattr(message_obj, "message_id", None)
            record = self.memory_processor.build_explicit_memory_record(
                memory=cleaned_memory,
                source_scope=memory_scope,
                topics=selected_topics,
                key_facts=_normalize_list(key_facts),
                participants=_normalize_list(participants, limit=8),
                sentiment=normalized_sentiment,
                importance=importance,
                topic_candidates=topic_candidates,
                participant_identities=participant_identities,
                source_reference=source_reference,
                origin="agent_memorize_tool",
                is_group_chat=is_group_chat,
            )
            cleaned_reason = (reason or "").strip()
            if cleaned_reason:
                record.metadata["memorize_reason"] = cleaned_reason

            memory_id = await self.memory_engine.add_canonical_memory(
                metadata=record.metadata,
                session_id=memory_scope,
                persona_id=persona_id,
                importance=record.importance,
            )

            return _json_result(
                {
                    "memorized": True,
                    "id": memory_id,
                    "content": record.content,
                    "importance": record.importance,
                    "session_id": memory_scope,
                    "persona_id": persona_id,
                    "topic": selected_topic or (
                        {"topic_id": None, "name": selected_new_topic}
                        if selected_new_topic
                        else None
                    ),
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"记忆工具写入失败: {e}", exc_info=True)
            return _json_result({"memorized": False, "error": "internal_error"})
