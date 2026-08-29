"""
记忆召回模块
负责长期记忆的检索和注入
"""

import asyncio
import json
import inspect
import time
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart

from ..memory_scope import is_event_memory_allowed, resolve_memory_scope
from ..retrieval.hybrid_retriever import HybridResult
from ..utils import (
    OperationContext,
    format_memories_for_fake_tool_call,
    get_persona_id,
)
from ..utils.fact_packing import (
    format_fact_hits_for_injection,
    pack_fact_hits,
    token_upper_bound,
)
from ..utils.recall_continuity import RecallContinuityCache
from ..utils.recall_logging import (
    RecallAssemblyStage,
    RecallInjectionStage,
    RecallQueryStage,
    RecallSearchStage,
    RecentBlockStats,
    format_assembly_stage,
    format_injection_stage,
    format_query_stage,
    format_recall_stage,
)

if TYPE_CHECKING:
    from ..base.config_manager import ConfigManager
    from ..managers.conversation_manager import ConversationManager
    from ..managers.memory_engine import MemoryEngine
    from ..utils.injection_adapter import InjectionAdapter
    from .message_utils import MessageUtils


class MemoryRecall:
    """记忆召回类"""

    def __init__(
        self,
        context,
        config_manager: "ConfigManager",
        memory_engine: "MemoryEngine",
        conversation_manager: "ConversationManager",
        message_utils: "MessageUtils",
        injection_adapter: "InjectionAdapter",
        user_baseline_manager=None,
    ):
        """
        初始化记忆召回模块

        Args:
            context: AstrBot上下文
            config_manager: 配置管理器
            memory_engine: 记忆引擎
            conversation_manager: 会话管理器
            message_utils: 消息处理工具
            injection_adapter: 注入适配器
        """
        self.context = context
        self.config_manager = config_manager
        self.memory_engine = memory_engine
        self.conversation_manager = conversation_manager
        self.message_utils = message_utils
        self.injection_adapter = injection_adapter
        self.user_baseline_manager = user_baseline_manager
        self._continuity_cache = RecallContinuityCache(
            max_sessions=int(
                self.config_manager.get("session_manager.max_sessions", 100)
            ),
            ttl_seconds=float(
                self.config_manager.get("session_manager.session_ttl", 3600)
            ),
        )

    async def clear_recall_continuity(self, session_id: str | None = None) -> None:
        """Clear ephemeral previous-turn fact ids for one or every conversation."""
        await self._continuity_cache.clear(session_id)

    @staticmethod
    def _canonical_fact_ids(hits: list[HybridResult]) -> list[str]:
        fact_ids = [
            str(hit.metadata.get("fact_id") or "").strip()
            for hit in hits
            if isinstance(getattr(hit, "metadata", None), dict)
            and hit.metadata.get("fact_id")
            and not hit.metadata.get("recent_summary")
        ]
        return list(dict.fromkeys(fact_id for fact_id in fact_ids if fact_id))

    @staticmethod
    def _is_importance_grace_admitted(hit: HybridResult) -> bool:
        breakdown = getattr(hit, "score_breakdown", None)
        if not isinstance(breakdown, dict):
            return False
        value = breakdown.get("importance_grace_admitted", 0.0)
        return isinstance(value, (int, float)) and value > 0

    async def _load_continuity_hits(
        self,
        fact_ids: tuple[str, ...],
        *,
        generation: int,
        memory_scope: str | None,
        persona_id: str | None,
    ) -> list[HybridResult]:
        """Rehydrate active canonical facts instead of carrying stale hit snapshots."""
        if not fact_ids:
            return []

        canonical_store = getattr(self.memory_engine, "canonical_store", None)
        get_fact_records = getattr(canonical_store, "get_fact_records", None)
        if not callable(get_fact_records):
            return []
        try:
            records = get_fact_records(list(fact_ids))
            if inspect.isawaitable(records):
                records = await records
            if not isinstance(records, dict):
                return []

            hits: list[HybridResult] = []
            for fact_id in fact_ids:
                record = records.get(fact_id)
                if not isinstance(record, dict):
                    continue
                if str(record.get("status") or "") != "active":
                    continue
                if memory_scope is not None and str(record.get("scope") or "") != str(
                    memory_scope
                ):
                    continue
                if persona_id is not None and record.get("persona_id") != persona_id:
                    continue
                fact = record.get("fact")
                if not isinstance(fact, dict):
                    continue
                content = str(fact.get("fact") or "").strip()
                if not content:
                    continue
                document_metadata = record.get("document_metadata") or {}
                create_time = float(
                    document_metadata.get("create_time")
                    or record.get("created_at")
                    or time.time()
                )
                hits.append(
                    HybridResult(
                        doc_id=int(record.get("document_id") or 0),
                        final_score=0.0,
                        rrf_score=0.0,
                        bm25_score=None,
                        vector_score=None,
                        content=content,
                        metadata={
                            "memory_schema_version": "v3",
                            "fact_id": fact_id,
                            "parent_id": str(record.get("parent_id") or ""),
                            "session_id": str(record.get("scope") or ""),
                            "persona_id": record.get("persona_id"),
                            "importance": float(record.get("importance") or 0.5),
                            "status": str(record.get("status") or "active"),
                            "create_time": create_time,
                            "persona_reaction": fact.get("persona_reaction"),
                            "has_source": bool(record.get("source_window")),
                            "retrieval_route": "continuity",
                            "selection_reason": (
                                "previous_turn_continuity"
                                if generation == 1
                                else "two_turn_continuity"
                            ),
                            "continuity_generation": generation,
                        },
                        score_breakdown=None,
                    )
                )
            return hits
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"续带 fact 重新读取失败，按空结果处理: {exc}")
            return []

    @staticmethod
    def _baseline_content_key(value: str) -> str:
        return " ".join(str(value or "").split()).strip().casefold()

    async def _inject_user_baseline(
        self,
        *,
        event: AstrMessageEvent,
        req: ProviderRequest,
        persona_id: str | None,
        session_id: str,
    ):
        """Inject the independent fixed block before relevance recall."""
        manager = self.user_baseline_manager
        if manager is None:
            return None
        try:
            payload = await manager.build_injection(event=event, persona_id=persona_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                f"[{session_id}] 用户画像读取失败；正常记忆召回继续执行。",
                exc_info=True,
            )
            return None
        if not getattr(payload, "text", ""):
            return payload

        configured_method = self.config_manager.get(
            "recall_engine.injection_method", "extra_user_content"
        )
        provider = None
        if configured_method in ("fake_tool_call", "fake_tool_call_deepseek_v4"):
            try:
                provider = self.context.get_using_provider(session_id)
            except Exception:
                provider = None
        resolved_method, _ = self.injection_adapter.resolve(provider, configured_method)
        # The baseline is not a retrieval result, so fake-tool modes use the
        # same provider-compatible temporary user-content path instead of
        # fabricating a second search call.
        actual_method = (
            resolved_method
            if resolved_method in {"user_message_before", "user_message_after"}
            else "extra_user_content"
        )
        if actual_method == "user_message_before":
            req.prompt = payload.text + "\n\n" + (req.prompt or "")
        elif actual_method == "user_message_after":
            req.prompt = (req.prompt or "") + "\n\n" + payload.text
        else:
            req.extra_user_content_parts.append(
                TextPart(text=payload.text).mark_as_temp()
            )
        logger.info(
            f"[{session_id}] [用户画像] 注入 {len(payload.entries)} 条；"
            f"预算 {payload.token_count}/{int(getattr(manager, 'token_budget', 800))} "
            f"token；方式={actual_method}。"
        )
        return payload

    @staticmethod
    def _message_timestamp_seconds(value) -> float | None:
        if isinstance(value, (int, float)):
            timestamp = float(value)
        elif isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                timestamp = float(stripped)
            except ValueError:
                try:
                    timestamp = datetime.fromisoformat(
                        stripped.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    return None
        else:
            return None

        if timestamp > 100_000_000_000:
            timestamp /= 1000.0
        return timestamp if timestamp > 0 else None

    async def handle_memory_recall(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """Query and inject long-term memory before LLM request"""
        recall_started = time.perf_counter()
        try:
            session_id = event.unified_msg_origin
            if not is_event_memory_allowed(self.config_manager, event):
                await self.clear_recall_continuity(session_id)
                logger.debug("当前事件不在记忆白名单中，跳过记忆召回")
                return
            logger.debug(f"[DEBUG-Recall] 获取到 unified_msg_origin: {session_id}")

            # 检测异常session_id
            if session_id and (
                "Error:" in session_id or "error:" in session_id.lower()
            ):
                logger.warning(
                    f"[{session_id}] 检测到异常的session_id，这可能导致记忆功能异常。"
                )

            async with OperationContext("记忆召回", session_id):
                prompt_text = getattr(req, "prompt", "")
                extra_parts = getattr(req, "extra_user_content_parts", [])
                has_prompt_text = isinstance(prompt_text, str) and bool(
                    prompt_text.strip()
                )
                has_extra_parts = bool(extra_parts)

                if not has_prompt_text and not has_extra_parts:
                    logger.debug(f"[{session_id}] 请求中无可用用户内容，跳过记忆召回")
                    return

                normalized = self._normalize_text_only_context_parts(req, session_id)
                if normalized > 0:
                    logger.debug(f"[{session_id}] 已归一化 {normalized} 条纯文本历史消息")

                # 自动删除旧的注入记忆
                if self.config_manager.get("recall_engine.auto_remove_injected", True):
                    removed = self._remove_injected_memories_from_context(
                        req, session_id
                    )
                    removed += self._remove_fake_tool_call_from_context(req, session_id)
                    if removed > 0:
                        logger.debug(
                            f"[{session_id}] 已清理 {removed} 处历史记忆注入片段"
                        )

                # 先提取用户消息（消息存储和召回都需要）
                actual_query = await self.message_utils.get_event_message_str(event)

                request_query = (
                    prompt_text.strip() if isinstance(prompt_text, str) else ""
                )

                # 存储用户消息（仅私聊），无论是否启用召回都需要
                is_group = event.get_message_type() == MessageType.GROUP_MESSAGE
                if not is_group and actual_query:
                    message_to_store = request_query
                    if not message_to_store:
                        message_to_store = (
                            await self.message_utils.extract_message_content(event, req)
                        )
                    if not message_to_store:
                        message_to_store = actual_query.strip()
                    await self.conversation_manager.add_message_from_event(
                        event=event,
                        role="user",
                        content=message_to_store,
                    )
                    await self.message_utils.enforce_message_limit(session_id)

                # 用户画像与相关召回相互独立：它先注入，且不受 top_k=0 影响。
                persona_id = await get_persona_id(self.context, event)
                baseline_payload = await self._inject_user_baseline(
                    event=event,
                    req=req,
                    persona_id=persona_id,
                    session_id=session_id,
                )
                baseline_content_keys = set(
                    getattr(baseline_payload, "content_keys", frozenset()) or ()
                )

                # 若 top_k <= 0，跳过记忆检索和注入，但上述清理和消息存储已执行
                top_k = self.config_manager.get("recall_engine.top_k", 5)
                if top_k <= 0:
                    await self.clear_recall_continuity(session_id)
                    logger.info(
                        f"[{session_id}] [记忆召回·查询] "
                        f"自动召回已关闭（top_k={top_k}）。"
                    )
                    return

                if not actual_query:
                    await self.clear_recall_continuity(session_id)
                    logger.warning(f"[{session_id}] 原始用户消息为空，跳过记忆召回")
                    return

                # 获取过滤配置
                filtering_config = self.config_manager.filtering_settings
                use_persona_filtering = filtering_config.get(
                    "use_persona_filtering", True
                )

                # 获取 persona_id，与 AstrBot 主流程保持一致的三级优先级：
                # 1. session_service_config（最高）
                # 2. req.conversation.persona_id（会话级）
                # 3. 全局默认人格（最低）
                # 注意：on_llm_request 钩子在 _ensure_persona_and_skills 之前触发，
                # 因此不能直接依赖 req.system_prompt 已注入人格，需自行走完整优先级。
                recall_session_id = resolve_memory_scope(self.config_manager, event)
                recall_persona_id = persona_id if use_persona_filtering else None

                importance_grace_enabled = bool(
                    self.config_manager.get(
                        "recall_engine.importance_grace_enabled", False
                    )
                )
                min_importance = float(
                    self.config_manager.get(
                        "recall_engine.min_importance_for_retrieval", 0.0
                    )
                )
                recent_enabled = bool(
                    self.config_manager.get("recall_engine.recent_block_enabled", False)
                )
                recent_start_rank = int(
                    self.config_manager.get(
                        "recall_engine.recent_block_start_parent_rank", 1
                    )
                )
                recent_parent_count = int(
                    self.config_manager.get("recall_engine.recent_block_parents", 1)
                )
                recent_window_hours = float(
                    self.config_manager.get(
                        "recall_engine.recent_block_window_hours", 48
                    )
                )

                continuity_enabled = bool(
                    self.config_manager.get(
                        "recall_engine.recall_continuity_enabled", True
                    )
                )
                if continuity_enabled:
                    continuity_window = await self._continuity_cache.take(
                        session_id,
                        memory_scope=recall_session_id,
                        persona_id=recall_persona_id,
                    )
                    previous_fact_ids = continuity_window.previous_fact_ids
                    older_fact_ids = continuity_window.older_fact_ids
                else:
                    await self.clear_recall_continuity(session_id)
                    previous_fact_ids = ()
                    older_fact_ids = ()

                fact_retriever = getattr(self.memory_engine, "fact_retriever", None)
                query_gate = getattr(fact_retriever, "query_gate_reason", None)
                gate_reason = query_gate(actual_query) if callable(query_gate) else None
                skip_current_recall = isinstance(gate_reason, str) and bool(gate_reason)

                # 使用原始用户输入作为召回关键字
                query_for_search = actual_query
                expanded_history_count = 0
                current_memories: list[HybridResult] = []
                recent_entries: list[HybridResult] = []
                recent_stats = RecentBlockStats(
                    enabled=recent_enabled,
                    status="skipped" if skip_current_recall else "disabled",
                )

                # 上下文扩展：拼接最近2轮对话作为查询，提升检索精准度
                if not skip_current_recall and self.config_manager.get(
                    "recall_engine.inject_with_recent_context", False
                ):
                    try:
                        recent_messages = (
                            await self.conversation_manager.get_context(
                                session_id,
                                max_messages=5,
                                format_for_llm=False,
                            )
                        )
                        if recent_messages and len(recent_messages) > 1:
                            # ConversationManager 返回时间升序，且当前用户消息已在
                            # 本方法前半段写入，因此末项才是当前消息。
                            context_parts = []
                            max_age_seconds = self.config_manager.get(
                                "recall_engine.recent_context_max_age_seconds", 7200
                            )
                            now = time.time()
                            skipped_by_age = 0
                            for msg in recent_messages[:-1]:
                                if max_age_seconds > 0:
                                    timestamp = self._message_timestamp_seconds(
                                        msg.get("timestamp")
                                    )
                                    if (
                                        timestamp is None
                                        or now - timestamp > max_age_seconds
                                    ):
                                        skipped_by_age += 1
                                        continue
                                content = msg.get("content", "")
                                if content and content.strip():
                                    context_parts.append(content.strip())
                            if context_parts:
                                expanded = " | ".join(context_parts)
                                query_for_search = expanded + " " + actual_query
                                expanded_history_count = len(context_parts)
                                logger.debug(
                                    f"[{session_id}] 上下文扩展按时间跳过 "
                                    f"{skipped_by_age} 条历史消息"
                                )
                    except Exception as e:
                        logger.warning(f"[{session_id}] 获取上下文扩展失败: {e}")

                logger.info(
                    f"[{session_id}] "
                    + format_query_stage(
                        RecallQueryStage(
                            query=actual_query,
                            top_k=top_k,
                            expanded_query=(
                                query_for_search if expanded_history_count else None
                            ),
                            expanded_history_count=expanded_history_count,
                            skip_reason=gate_reason if skip_current_recall else None,
                            importance_grace_enabled=importance_grace_enabled,
                            min_importance=min_importance,
                            recent_enabled=recent_enabled,
                            recent_start_rank=recent_start_rank,
                            recent_parent_count=recent_parent_count,
                            recent_window_hours=recent_window_hours,
                        )
                    )
                )

                if not skip_current_recall:
                    search_started = time.perf_counter()
                    current_memories = list(
                        await self.memory_engine.search_memories(
                            query=query_for_search,
                            k=top_k,
                            session_id=recall_session_id,
                            persona_id=recall_persona_id,
                        )
                        or []
                    )[:top_k]
                    grace_admitted_count = sum(
                        1
                        for hit in current_memories
                        if self._is_importance_grace_admitted(hit)
                    )
                    logger.info(
                        f"[{session_id}] "
                        + format_recall_stage(
                            RecallSearchStage(
                                hit_count=len(current_memories),
                                top_k=top_k,
                                elapsed_ms=(
                                    time.perf_counter() - search_started
                                )
                                * 1000,
                                importance_grace_enabled=importance_grace_enabled,
                                grace_admitted_count=grace_admitted_count,
                            )
                        )
                    )

                assembly_started = time.perf_counter()
                if not skip_current_recall:
                    # recent 块不占本轮 top_k，但排在本轮相关召回和续带之后，
                    # 三者共用同一 token 硬预算。
                    recent_entries, recent_stats = await self._build_recent_block(
                        query_for_search,
                        current_memories,
                        recall_session_id or session_id,
                        recall_persona_id,
                    )

                current_fact_ids = self._canonical_fact_ids(current_memories)
                current_fact_id_set = set(current_fact_ids)
                previous_hits = await self._load_continuity_hits(
                    previous_fact_ids,
                    generation=1,
                    memory_scope=recall_session_id,
                    persona_id=recall_persona_id,
                )
                older_hits = await self._load_continuity_hits(
                    older_fact_ids,
                    generation=2,
                    memory_scope=recall_session_id,
                    persona_id=recall_persona_id,
                )
                previous_loaded = len(previous_hits)
                older_loaded = len(older_hits)
                invalid_continuity_count = max(
                    0,
                    len(previous_fact_ids)
                    + len(older_fact_ids)
                    - previous_loaded
                    - older_loaded,
                )
                repeated_count = sum(
                    1
                    for hit in [*previous_hits, *older_hits]
                    if str(hit.metadata.get("fact_id") or "")
                    in current_fact_id_set
                )
                previous_hits = [
                    hit
                    for hit in previous_hits
                    if str(hit.metadata.get("fact_id") or "")
                    not in current_fact_id_set
                ]
                occupied_fact_ids = current_fact_id_set | set(
                    self._canonical_fact_ids(previous_hits)
                )
                older_hits = [
                    hit
                    for hit in older_hits
                    if str(hit.metadata.get("fact_id") or "")
                    not in occupied_fact_ids
                ]

                baseline_duplicate_count = 0
                if baseline_content_keys:
                    filtered_sources = []
                    for source in (
                        current_memories,
                        previous_hits,
                        older_hits,
                        recent_entries,
                    ):
                        filtered = []
                        for hit in source:
                            if self._baseline_content_key(hit.content) in baseline_content_keys:
                                baseline_duplicate_count += 1
                                continue
                            filtered.append(hit)
                        filtered_sources.append(filtered)
                    (
                        current_memories,
                        previous_hits,
                        older_hits,
                        recent_entries,
                    ) = filtered_sources

                # 固定装配优先级：本轮相关召回 → 上轮续带 → 上上轮续带 → recent。
                recalled_memories = [
                    *current_memories,
                    *previous_hits,
                    *older_hits,
                    *recent_entries,
                ]
                packing_candidate_count = (
                    len(recalled_memories) + baseline_duplicate_count
                )
                token_budget = int(
                    self.config_manager.get(
                        "recall_engine.injection_token_budget", 1600
                    )
                )
                dropped: list[dict[str, str]] = []
                reason_counts: Counter[str] = Counter()
                if baseline_duplicate_count:
                    reason_counts["baseline_duplicate"] = baseline_duplicate_count

                if packing_candidate_count:
                    packer = getattr(self.memory_engine, "pack_memory_hits", None)
                    packed = packer(recalled_memories) if callable(packer) else None
                    if packed is None or not isinstance(
                        getattr(packed, "hits", None), list
                    ):
                        packed = pack_fact_hits(
                            recalled_memories,
                            token_budget=token_budget,
                            single_fact_budget=int(
                                self.config_manager.get(
                                    "recall_engine.single_fact_token_budget", 300
                                )
                            ),
                            include_reaction=self.config_manager.get(
                                "recall_engine.include_persona_reaction", True
                            ),
                        )
                    recalled_memories = packed.hits
                    dropped = list(getattr(packed, "dropped", []) or [])
                    reason_counts.update(
                        str(item.get("reason") or "unknown") for item in dropped
                    )
                    if dropped:
                        logger.debug(f"[{session_id}] 注入丢弃明细: {dropped}")
                logger.info(
                    f"[{session_id}] "
                    + format_assembly_stage(
                        RecallAssemblyStage(
                            continuity_enabled=continuity_enabled,
                            previous_loaded=previous_loaded,
                            older_loaded=older_loaded,
                            invalid_continuity_count=invalid_continuity_count,
                            repeated_count=repeated_count,
                            previous_continuation=len(previous_hits),
                            older_continuation=len(older_hits),
                            recent=recent_stats,
                            candidate_count=packing_candidate_count,
                            packed_count=len(recalled_memories),
                            dropped_reasons=dict(reason_counts),
                            elapsed_ms=(
                                time.perf_counter() - assembly_started
                            )
                            * 1000,
                        )
                    )
                )

                configured_method = self.config_manager.get(
                    "recall_engine.injection_method", "extra_user_content"
                )
                if not packing_candidate_count or not recalled_memories:
                    logger.info(
                        f"[{session_id}] "
                        + format_injection_stage(
                            RecallInjectionStage(
                                injected=False,
                                method=str(configured_method),
                                outcome=(
                                    "no_candidates"
                                    if not packing_candidate_count
                                    else "packing_empty"
                                ),
                                total_count=0,
                                current_count=0,
                                previous_count=0,
                                older_count=0,
                                recent_summary_count=0,
                                recent_fact_count=0,
                                token_count=0,
                                token_budget=token_budget,
                                next_previous_count=0,
                                next_older_count=0,
                                total_elapsed_ms=(
                                    time.perf_counter() - recall_started
                                )
                                * 1000,
                                continuity_enabled=continuity_enabled,
                                importance_grace_enabled=importance_grace_enabled,
                                recent_enabled=recent_enabled,
                            )
                        )
                    )
                    return

                # 输出详细记忆信息
                for i, mem in enumerate(recalled_memories, 1):
                    logger.debug(
                        f"[{session_id}] 记忆 #{i}: 得分={mem.final_score:.3f}, "
                        f"重要性={mem.metadata.get('importance', 0.5):.2f}, "
                        f"内容={mem.content[:100]}..."
                    )

                # 根据配置选择注入方式（含 Provider 兼容降级）
                provider = None
                if configured_method in (
                    "fake_tool_call",
                    "fake_tool_call_deepseek_v4",
                ):
                    try:
                        provider = self.context.get_using_provider(session_id)
                    except Exception as e:
                        logger.warning(
                            f"[{session_id}] 获取当前 Provider 失败，"
                            f"将按无 Provider 继续解析注入模式: {e}"
                        )
                injection_method, fallback_reason = self.injection_adapter.resolve(
                    provider, configured_method
                )
                if fallback_reason:
                    logger.warning(
                        f"[{session_id}] 注入模式从 {configured_method} 降级为 "
                        f"{injection_method}: {fallback_reason}"
                    )

                memory_str = format_fact_hits_for_injection(
                    recalled_memories,
                    include_reaction=self.config_manager.get(
                        "recall_engine.include_persona_reaction", True
                    ),
                )
                injected = False
                method_budget_dropped = 0
                final_token_count = token_upper_bound(memory_str)

                if injection_method == "user_message_before":
                    req.prompt = memory_str + "\n\n" + (req.prompt or "")
                    injected = True
                elif injection_method == "user_message_after":
                    req.prompt = (req.prompt or "") + "\n\n" + memory_str
                    injected = True
                elif injection_method == "fake_tool_call":
                    fake_messages = []
                    before_method_budget = len(recalled_memories)
                    while recalled_memories:
                        memory_list = [
                            {
                                "id": getattr(mem, "doc_id", None),
                                "content": mem.content,
                                "score": mem.final_score,
                                "metadata": mem.metadata,
                                "timestamp": mem.metadata.get("create_time"),
                            }
                            for mem in recalled_memories
                        ]
                        fake_messages = format_memories_for_fake_tool_call(
                            memory_list,
                            query=actual_query,
                            k=len(recalled_memories),
                            session_filtered=recall_session_id is not None,
                            persona_filtered=use_persona_filtering,
                            log_completion=False,
                        )
                        final_token_count = token_upper_bound(
                            json.dumps(fake_messages, ensure_ascii=False)
                        )
                        if final_token_count <= token_budget:
                            break
                        recalled_memories.pop()
                    if not recalled_memories:
                        fake_messages = []
                        final_token_count = 0
                    method_budget_dropped = (
                        before_method_budget - len(recalled_memories)
                    )
                    if fake_messages:
                        req.contexts.extend(fake_messages)
                        injected = True
                else:
                    # extra_user_content（推荐）：追加到用户消息末尾，
                    # 不影响前缀缓存且 mark_as_temp 后不污染对话历史
                    req.extra_user_content_parts.append(
                        TextPart(text=memory_str).mark_as_temp()
                    )
                    injected = True

                if not injected:
                    logger.info(
                        f"[{session_id}] "
                        + format_injection_stage(
                            RecallInjectionStage(
                                injected=False,
                                method=injection_method,
                                outcome="method_budget_empty",
                                total_count=0,
                                current_count=0,
                                previous_count=0,
                                older_count=0,
                                recent_summary_count=0,
                                recent_fact_count=0,
                                token_count=0,
                                token_budget=token_budget,
                                next_previous_count=0,
                                next_older_count=0,
                                total_elapsed_ms=(
                                    time.perf_counter() - recall_started
                                )
                                * 1000,
                                continuity_enabled=continuity_enabled,
                                importance_grace_enabled=importance_grace_enabled,
                                recent_enabled=recent_enabled,
                                method_budget_dropped=method_budget_dropped,
                            )
                        )
                    )
                    return

                current_object_ids = {id(hit) for hit in current_memories}
                actual_current_hits = [
                    hit
                    for hit in recalled_memories
                    if id(hit) in current_object_ids
                    or str(hit.metadata.get("fact_id") or "")
                    in current_fact_id_set
                ]
                actual_current_count = len(actual_current_hits)
                actual_current_grace_count = sum(
                    1
                    for hit in actual_current_hits
                    if self._is_importance_grace_admitted(hit)
                )
                actual_previous_count = sum(
                    1
                    for hit in recalled_memories
                    if hit.metadata.get("continuity_generation") == 1
                )
                actual_older_count = sum(
                    1
                    for hit in recalled_memories
                    if hit.metadata.get("continuity_generation") == 2
                )
                actual_recent_summary_count = sum(
                    1
                    for hit in recalled_memories
                    if bool(hit.metadata.get("recent_summary"))
                )
                actual_recent_fact_count = sum(
                    1
                    for hit in recalled_memories
                    if hit.metadata.get("selection_reason") == "recent_block_fact"
                )

                next_fact_ids: list[str] = []
                next_older_fact_ids: list[str] = []
                if continuity_enabled:
                    injected_fact_ids = set(
                        self._canonical_fact_ids(recalled_memories)
                    )
                    next_fact_ids = [
                        fact_id
                        for fact_id in current_fact_ids
                        if fact_id in injected_fact_ids
                    ][:2]
                    next_older_fact_ids = [
                        fact_id
                        for fact_id in self._canonical_fact_ids(previous_hits)
                        if fact_id in injected_fact_ids
                    ][:1]
                    await self._continuity_cache.put(
                        session_id,
                        next_fact_ids,
                        next_older_fact_ids,
                        memory_scope=recall_session_id,
                        persona_id=recall_persona_id,
                    )

                logger.info(
                    f"[{session_id}] "
                    + format_injection_stage(
                        RecallInjectionStage(
                            injected=True,
                            method=injection_method,
                            outcome="injected",
                            total_count=len(recalled_memories),
                            current_count=actual_current_count,
                            previous_count=actual_previous_count,
                            older_count=actual_older_count,
                            recent_summary_count=actual_recent_summary_count,
                            recent_fact_count=actual_recent_fact_count,
                            token_count=final_token_count,
                            token_budget=token_budget,
                            next_previous_count=len(next_fact_ids),
                            next_older_count=len(next_older_fact_ids),
                            total_elapsed_ms=(
                                time.perf_counter() - recall_started
                            )
                            * 1000,
                            continuity_enabled=continuity_enabled,
                            importance_grace_enabled=importance_grace_enabled,
                            current_grace_count=actual_current_grace_count,
                            recent_enabled=recent_enabled,
                            method_budget_dropped=method_budget_dropped,
                        )
                    )
                )

                marker = getattr(self.memory_engine, "mark_memories_injected", None)
                if callable(marker):
                    marked = marker(recalled_memories)
                    if inspect.isawaitable(marked):
                        await marked

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"处理 on_llm_request 钩子时发生错误: {e}", exc_info=True)

    async def _build_recent_block(
        self,
        query: str,
        recalled_memories: list[HybridResult],
        session_id: str,
        persona_id: str | None,
    ) -> tuple[list[HybridResult], RecentBlockStats]:
        """Build the recent-memory block from a configurable parent rank.

        The recent block is a short-term continuity window (default 48h),
        independent of the top_k relevance slots:
        - skip ``recent_block_start_parent_rank - 1`` newer eligible parents;
        - include ``recent_block_parents`` consecutive active parent overviews
          toward the past (newest selected parent first);
        - up to ``recent_block_max_facts`` facts per parent are appended when
          they are lexically close to the current topic (relaxed threshold);
        - facts already selected by the main recall are not duplicated.

        Entries are plain HybridResult objects so the shared fact packer
        applies the same token budget and exact-text dedup afterwards.
        """
        stats = RecentBlockStats(enabled=False, status="disabled")
        try:
            if not self.config_manager.get(
                "recall_engine.recent_block_enabled", False
            ):
                return [], stats
            stats.enabled = True
            window_hours = float(
                self.config_manager.get(
                    "recall_engine.recent_block_window_hours", 48
                )
            )
            max_facts = int(
                self.config_manager.get(
                    "recall_engine.recent_block_max_facts", 2
                )
            )
            parent_count = max(
                1,
                int(
                    self.config_manager.get(
                        "recall_engine.recent_block_parents", 1
                    )
                ),
            )
            start_parent_rank = min(
                100,
                max(
                    1,
                    int(
                        self.config_manager.get(
                            "recall_engine.recent_block_start_parent_rank", 1
                        )
                    ),
                ),
            )
            if window_hours <= 0:
                stats.status = "no_parents"
                return [], stats

            canonical_store = getattr(self.memory_engine, "canonical_store", None)
            get_recent_parents = getattr(
                canonical_store, "get_recent_parents", None
            )
            if not callable(get_recent_parents):
                stats.status = "store_unavailable"
                return [], stats

            parents = await get_recent_parents(
                scope=session_id,
                persona_id=persona_id,
                window_hours=window_hours,
                parent_count=parent_count,
                parent_offset=start_parent_rank - 1,
            )
            if not parents:
                stats.status = "no_parents"
                return [], stats
            stats.parent_count = len(parents)

            get_facts_by_parent = getattr(
                canonical_store, "get_facts_by_parent", None
            )
            retriever = getattr(self.memory_engine, "fact_retriever", None)
            score_facts = getattr(retriever, "score_facts_lexically", None)
            facts_available = callable(get_facts_by_parent) and callable(
                score_facts
            )
            stats.status = (
                "facts_unavailable"
                if max_facts > 0 and not facts_available
                else "ok"
            )

            # 已在主召回中的 fact 不再重复入选（fact_id 精确去重，可靠）
            # 同一 set 跨多个 parent 累积，防止跨 parent 重复。
            recalled_ids = {
                str(mem.metadata.get("fact_id") or "")
                for mem in recalled_memories
                if isinstance(getattr(mem, "metadata", None), dict)
                and mem.metadata.get("fact_id")
            }

            entries: list[HybridResult] = []
            for parent in parents:
                overview = str(parent.get("overview") or "").strip()
                parent_id = str(parent.get("parent_id") or "")
                document_id = int(parent.get("document_id") or 0)
                if not overview or not parent_id:
                    continue

                entries.append(
                    HybridResult(
                        doc_id=document_id,
                        final_score=1.0,
                        rrf_score=0.0,
                        bm25_score=None,
                        vector_score=None,
                        content=overview,
                        metadata={
                            "memory_schema_version": "v3",
                            "fact_id": "",
                            "parent_id": parent_id,
                            "session_id": session_id,
                            "persona_id": persona_id,
                            "importance": 1.0,
                            "status": "active",
                            "recent_summary": True,
                            "selection_reason": "recent_block_summary",
                        },
                        score_breakdown=None,
                    )
                )
                stats.summary_count += 1

                if max_facts <= 0 or not facts_available:
                    self._log_recent_block(
                        session_id,
                        document_id,
                        parent_id,
                        overview,
                        0,
                        0,
                        "配置为仅摘要" if max_facts <= 0 else "事实检索器不可用",
                        query,
                    )
                    continue

                facts = await get_facts_by_parent(parent_id)
                if not facts:
                    self._log_recent_block(
                        session_id,
                        document_id,
                        parent_id,
                        overview,
                        0,
                        0,
                        "父记忆无事实",
                        query,
                    )
                    continue

                candidates = [
                    fact
                    for fact in facts
                    if str(fact.get("fact_id") or "") not in recalled_ids
                ]
                stats.fact_candidate_count += len(candidates)
                if not candidates:
                    self._log_recent_block(
                        session_id,
                        document_id,
                        parent_id,
                        overview,
                        0,
                        0,
                        "事实均已入选主召回",
                        query,
                    )
                    continue

                texts = [str(fact.get("fact") or "").strip() for fact in candidates]
                scores = await score_facts(query, texts)

                # 放宽一档的词面门槛（默认 0.34 → 0.204）：连续性优先，
                # 但完全无关的 fact 仍不入选。
                relaxed_lexical = float(
                    self.config_manager.get(
                        "recall_engine.fact_min_lexical_score", 0.34
                    )
                ) * 0.6
                ranked = sorted(
                    zip(candidates, scores),
                    key=lambda item: item[1],
                    reverse=True,
                )
                picked = 0
                reason = "词面命中"
                for fact, lexical in ranked:
                    if picked >= max_facts:
                        break
                    if lexical < relaxed_lexical:
                        reason = f"词面不达标 (门槛 {relaxed_lexical:.2f})"
                        break
                    text = str(fact.get("fact") or "").strip()
                    if not text:
                        continue
                    fact_id = str(fact.get("fact_id") or "")
                    entries.append(
                        HybridResult(
                            doc_id=document_id,
                            final_score=0.5 + 0.5 * lexical,
                            rrf_score=0.0,
                            bm25_score=None,
                            vector_score=None,
                            content=text,
                            metadata={
                                "memory_schema_version": "v3",
                                "fact_id": fact_id,
                                "parent_id": parent_id,
                                "session_id": session_id,
                                "persona_id": persona_id,
                                "importance": float(fact.get("importance") or 0.5),
                                "status": "active",
                                "selection_reason": "recent_block_fact",
                            },
                            score_breakdown=None,
                        )
                    )
                    recalled_ids.add(fact_id)
                    picked += 1
                    stats.fact_selected_count += 1

                self._log_recent_block(
                    session_id,
                    document_id,
                    parent_id,
                    overview,
                    len(candidates),
                    picked,
                    reason,
                    query,
                )
            return entries, stats
        except asyncio.CancelledError:
            raise
        except Exception as e:
            stats.enabled = True
            stats.status = "error"
            logger.warning(
                f"[{session_id}] recent 块构建失败，主召回继续: {type(e).__name__}"
            )
            return [], stats

    @staticmethod
    def _log_recent_block(
        session_id: str,
        document_id: int,
        parent_id: str,
        overview: str,
        candidate_count: int,
        picked_count: int,
        reason: str,
        query: str,
    ) -> None:
        """Log the recent block outcome with enough context for troubleshooting."""
        preview = " ".join(str(query or "").split())[:24]
        logger.debug(
            f"[{session_id}] recent 块: 父记忆 #{document_id} "
            f"({parent_id[:12]}..., 摘要 {len(overview)} 字), "
            f"候选 {candidate_count} 条 → 带 {picked_count} 条 [{reason}], "
            f'查询="{preview}"'
        )

    def _remove_injected_memories_from_context(
        self, req: ProviderRequest, session_id: str
    ) -> int:
        """从请求上下文中移除临时注入的记忆片段"""
        import re
        from ..base.constants import MEMORY_INJECTION_FOOTER, MEMORY_INJECTION_HEADER

        removed = 0

        # 清理 system_prompt（兼容旧版本注入残留）
        if hasattr(req, "system_prompt") and req.system_prompt:
            if isinstance(req.system_prompt, str):
                original_prompt = req.system_prompt
                if (
                    MEMORY_INJECTION_HEADER in original_prompt
                    and MEMORY_INJECTION_FOOTER in original_prompt
                ):
                    # 使用正则清理记忆片段
                    pattern = re.compile(
                        re.escape(MEMORY_INJECTION_HEADER)
                        + r".*?"
                        + re.escape(MEMORY_INJECTION_FOOTER),
                        re.DOTALL,
                    )
                    cleaned_prompt = pattern.sub("", original_prompt)
                    cleaned_prompt = re.sub(r"\n{3,}", "\n\n", cleaned_prompt).strip()
                    req.system_prompt = cleaned_prompt
                    if cleaned_prompt != original_prompt:
                        removed += 1

        # 清理 extra_user_content_parts（通过 mark_as_temp/_no_save 标记）
        parts_before = len(getattr(req, "extra_user_content_parts", []))
        if parts_before > 0:
            req.extra_user_content_parts = [
                part
                for part in req.extra_user_content_parts
                if not self._is_livingmemory_temp_part(part)
            ]
            parts_after = len(req.extra_user_content_parts)
            removed += parts_before - parts_after

        return removed

    def _is_livingmemory_temp_part(self, part) -> bool:
        """判断是否为 LivingMemory 本轮临时注入的 extra_user_content part"""
        from ..base.constants import MEMORY_INJECTION_FOOTER, MEMORY_INJECTION_HEADER

        text = getattr(part, "text", "")
        return (
            getattr(part, "_no_save", False)
            and isinstance(text, str)
            and MEMORY_INJECTION_HEADER in text
            and MEMORY_INJECTION_FOOTER in text
        )

    def _normalize_text_only_context_parts(
        self, req: ProviderRequest, session_id: str
    ) -> int:
        """把历史中的纯文本 content parts 折叠回字符串，避免污染长期上下文格式"""
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list):
            return 0

        normalized = 0
        for msg in contexts:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list) or not content:
                continue

            text_parts = []
            text_only = True
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    text_only = False
                    break
                text_parts.append(str(part.get("text", "") or ""))

            if not text_only:
                continue

            msg["content"] = "".join(text_parts)
            normalized += 1

        if normalized:
            logger.debug(f"[{session_id}] 已归一化 {normalized} 条纯文本历史 content parts")
        return normalized

    def _remove_fake_tool_call_from_context(
        self, req: ProviderRequest, session_id: str
    ) -> int:
        """从请求上下文中移除伪造的工具调用记忆（fake_tool_call 注入方式）

        识别并移除以 FAKE_TOOL_CALL_ID_PREFIX 为 ID 前缀的
        assistant(tool_calls) + tool(result) 消息对。
        """
        from ..base.constants import FAKE_TOOL_CALL_ID_PREFIX

        if not hasattr(req, "contexts") or not req.contexts:
            return 0

        removed = 0
        indices_to_remove: set[int] = set()
        fake_call_ids: set[str] = set()

        try:
            # 单轮扫描：同时收集伪造 assistant(tool_calls) 和对应 tool(result) 消息
            for i, msg in enumerate(req.contexts):
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                if role == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        tc_id = (
                            tc.get("id", "")
                            if isinstance(tc, dict)
                            else getattr(tc, "id", "")
                        )
                        if tc_id.startswith(FAKE_TOOL_CALL_ID_PREFIX):
                            fake_call_ids.add(tc_id)
                            indices_to_remove.add(i)
                elif role == "tool":
                    tc_id = msg.get("tool_call_id", "")
                    if tc_id in fake_call_ids:
                        indices_to_remove.add(i)

            # 从后往前删除，避免索引偏移
            for i in sorted(indices_to_remove, reverse=True):
                req.contexts.pop(i)
                removed += 1

        except Exception:
            pass

        return removed
