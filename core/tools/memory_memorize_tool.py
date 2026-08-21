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

from ..memory_scope import is_event_memory_allowed, resolve_memory_scope
from ..utils import get_persona_id


def _json_result(data: dict[str, Any]) -> str:
    """将工具结果稳定序列化为 JSON 文本。"""
    return json.dumps(data, ensure_ascii=False, default=str)


def _normalize_list(value: Any, limit: int = 5) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()][:limit]
    if isinstance(value, str) and value.strip():
        return [value.strip()][:limit]
    return []


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
        "Write concise factual memory, not the full conversation."
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "memory": {
                    "type": "string",
                    "description": "Concise factual long-term memory to save. Do not copy the full conversation.",
                },
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional short topic tags for this memory, up to 5.",
                    "default": [],
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
        topics: list[str] | None = None,
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

            get_topic_candidates = getattr(
                self.memory_engine, "get_topic_candidates", None
            )
            topic_candidates = (
                await get_topic_candidates(memory_scope)
                if callable(get_topic_candidates)
                else []
            )
            message_obj = getattr(event, "message_obj", None)
            source_reference = getattr(message_obj, "message_id", None)
            record = self.memory_processor.build_explicit_memory_record(
                memory=cleaned_memory,
                source_scope=memory_scope,
                topics=_normalize_list(topics),
                key_facts=_normalize_list(key_facts),
                participants=_normalize_list(participants, limit=8),
                sentiment=normalized_sentiment,
                importance=importance,
                topic_candidates=(
                    topic_candidates if isinstance(topic_candidates, list) else []
                ),
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
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"记忆工具写入失败: {e}", exc_info=True)
            return _json_result({"memorized": False, "error": "internal_error"})
