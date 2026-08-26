"""Regression coverage for the fixed X+2+1 recall continuity window."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, Mock, patch

import pytest
from astrbot.api.platform import MessageType
from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.event_handler import EventHandler
from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_livingmemory.core.utils.fact_packing import PackedFacts
from astrbot_plugin_livingmemory.core.utils.recall_continuity import (
    RecallContinuityCache,
)


SESSION_ID = "test:private:continuity"
PERSONA_ID = "persona-test"


def _hit(fact_id: str) -> HybridResult:
    return HybridResult(
        doc_id=abs(hash(fact_id)) % 10_000,
        final_score=0.9,
        rrf_score=0.8,
        bm25_score=0.7,
        vector_score=0.8,
        content=f"事实-{fact_id}",
        metadata={
            "fact_id": fact_id,
            "parent_id": f"parent-{fact_id}",
            "importance": 0.8,
            "create_time": time.time(),
        },
        score_breakdown=None,
    )


def _record(fact_id: str, *, scope: str = SESSION_ID, persona: str = PERSONA_ID):
    return {
        "fact_id": fact_id,
        "parent_id": f"parent-{fact_id}",
        "document_id": abs(hash(fact_id)) % 10_000,
        "fact": {"fact": f"事实-{fact_id}"},
        "scope": scope,
        "persona_id": persona,
        "importance": 0.8,
        "status": "active",
        "source_window": {"message_ids": [1]},
        "document_metadata": {"create_time": time.time()},
        "created_at": time.time(),
    }


def _engine(search_rounds: list[list[HybridResult]]) -> Mock:
    engine = Mock()
    engine.search_memories = AsyncMock(side_effect=search_rounds)
    engine.add_memory = AsyncMock(return_value=1)
    engine.add_canonical_memory = AsyncMock(return_value=1)
    engine.mark_memories_injected = AsyncMock()
    engine.pack_memory_hits = None
    engine.fact_retriever = Mock()
    engine.fact_retriever.query_gate_reason = lambda _query: None
    store = Mock()

    async def _get_records(fact_ids):
        return {fact_id: _record(fact_id) for fact_id in fact_ids}

    store.get_fact_records = AsyncMock(side_effect=_get_records)
    engine.canonical_store = store
    return engine


def _conversation_manager() -> Mock:
    manager = Mock()
    manager.add_message_from_event = AsyncMock(return_value=Mock(id=1, metadata={}))
    manager.get_session_info = AsyncMock(return_value=Mock(message_count=12))
    manager.get_messages_range = AsyncMock(
        return_value=[Mock(group_id=None), Mock(group_id=None)]
    )
    manager.invalidate_cache = AsyncMock()
    manager.clear_session = AsyncMock()
    manager.store = Mock()
    manager.store.get_message_count = AsyncMock(return_value=12)
    manager.store.update_message_metadata = AsyncMock()
    manager.store.connection = Mock()
    manager.store.connection.execute = AsyncMock(return_value=Mock(rowcount=1))
    manager.store.connection.commit = AsyncMock()
    return manager


def _handler(engine: Mock, **recall_overrides) -> EventHandler:
    recall = {
        "top_k": 5,
        "recall_continuity_enabled": True,
        "recent_block_enabled": False,
        "injection_method": "extra_user_content",
        "injection_token_budget": 4000,
        "single_fact_token_budget": 500,
    }
    recall.update(recall_overrides)
    return EventHandler(
        context=Mock(),
        config_manager=ConfigManager({"recall_engine": recall}),
        memory_engine=engine,
        memory_processor=Mock(),
        conversation_manager=_conversation_manager(),
    )


def _event(query: str, *, session_id: str = SESSION_ID) -> Mock:
    event = Mock()
    event.unified_msg_origin = session_id
    event.get_message_type = Mock(return_value=MessageType.FRIEND_MESSAGE)
    event.get_message_str = Mock(return_value=query)
    event.get_sender_id = Mock(return_value="user-test")
    event.get_sender_name = Mock(return_value="Tester")
    event.get_platform_name = Mock(return_value="test")
    event.get_messages = Mock(return_value=[])
    return event


def _request(query: str) -> Mock:
    req = Mock()
    req.prompt = query
    req.system_prompt = ""
    req.contexts = []
    req.extra_user_content_parts = []
    return req


def _injected_text(req: Mock) -> str:
    if not req.extra_user_content_parts:
        return ""
    return req.extra_user_content_parts[0].text


@pytest.mark.asyncio
async def test_three_generation_sequence_uses_current_then_two_then_one_slots():
    engine = _engine(
        [
            [_hit(value) for value in "ABCDE"],
            [_hit(value) for value in "FGHIJ"],
            [_hit(value) for value in "KLMNO"],
        ]
    )
    handler = _handler(engine)

    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value=PERSONA_ID),
    ):
        first = _request("第一轮")
        await handler.handle_memory_recall(_event("第一轮"), first)
        second = _request("第二轮")
        await handler.handle_memory_recall(_event("第二轮"), second)
        third = _request("第三轮")
        await handler.handle_memory_recall(_event("第三轮"), third)

    second_text = _injected_text(second)
    assert all(f"事实-{value}" in second_text for value in "FGHIJAB")
    assert "事实-C" not in second_text
    third_text = _injected_text(third)
    assert all(f"事实-{value}" in third_text for value in "KLMNOFGA")
    assert "事实-B" not in third_text


@pytest.mark.asyncio
async def test_two_follow_up_challenge_keeps_highest_original_evidence():
    engine = _engine(
        [
            [_hit("DIRECT"), _hit("NEIGHBOR")],
            [],
            [],
            [],
        ]
    )
    handler = _handler(engine)

    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value=PERSONA_ID),
    ):
        await handler.handle_memory_recall(_event("短句"), _request("短句"))
        first_follow_up = _request("为什么这么说")
        await handler.handle_memory_recall(
            _event("为什么这么说"), first_follow_up
        )
        second_follow_up = _request("以前真有这件事吗")
        await handler.handle_memory_recall(
            _event("以前真有这件事吗"), second_follow_up
        )
        expired = _request("换个话题")
        await handler.handle_memory_recall(_event("换个话题"), expired)

    assert "事实-DIRECT" in _injected_text(first_follow_up)
    assert "事实-NEIGHBOR" in _injected_text(first_follow_up)
    assert "事实-DIRECT" in _injected_text(second_follow_up)
    assert "事实-NEIGHBOR" not in _injected_text(second_follow_up)
    assert _injected_text(expired) == ""


@pytest.mark.asyncio
async def test_repeated_current_hit_is_deduplicated_and_refreshes_eligibility():
    engine = _engine(
        [
            [_hit("A"), _hit("B")],
            [_hit("A"), _hit("C")],
            [],
        ]
    )
    handler = _handler(engine)

    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value=PERSONA_ID),
    ):
        await handler.handle_memory_recall(_event("第一轮"), _request("第一轮"))
        second = _request("第二轮")
        await handler.handle_memory_recall(_event("第二轮"), second)
        third = _request("第三轮")
        await handler.handle_memory_recall(_event("第三轮"), third)

    assert _injected_text(second).count("事实-A") == 1
    assert "事实-B" in _injected_text(second)
    assert "事实-A" in _injected_text(third)
    assert "事实-C" in _injected_text(third)
    assert "事实-B" in _injected_text(third)
    assert _injected_text(third).count("事实-A") == 1


@pytest.mark.asyncio
async def test_lightweight_messages_age_through_two_fixed_carry_generations():
    engine = _engine([[_hit("A"), _hit("B")]])
    handler = _handler(engine)

    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value=PERSONA_ID),
    ):
        await handler.handle_memory_recall(_event("第一轮"), _request("第一轮"))
        engine.fact_retriever.query_gate_reason = lambda _query: "轻量消息"
        second = _request("嗯")
        await handler.handle_memory_recall(_event("嗯"), second)
        third = _request("晚安")
        await handler.handle_memory_recall(_event("晚安"), third)
        fourth = _request("嗯")
        await handler.handle_memory_recall(_event("嗯"), fourth)

    assert "事实-A" in _injected_text(second)
    assert "事实-B" in _injected_text(second)
    assert "事实-A" in _injected_text(third)
    assert "事实-B" not in _injected_text(third)
    assert _injected_text(fourth) == ""
    assert engine.search_memories.await_count == 1


@pytest.mark.asyncio
async def test_budget_order_and_next_state_use_only_actually_injected_current_hits():
    engine = _engine(
        [
            [_hit("A"), _hit("B")],
            [_hit("C"), _hit("D")],
            [],
        ]
    )
    pack_call = 0

    def _pack(hits):
        nonlocal pack_call
        pack_call += 1
        selected = list(hits if pack_call != 2 else hits[:1])
        return PackedFacts(
            hits=selected,
            token_count=100,
            token_budget=100,
            dropped=[
                {"fact_id": hit.metadata.get("fact_id", ""), "reason": "total_budget"}
                for hit in hits[len(selected) :]
            ],
        )

    engine.pack_memory_hits = _pack
    handler = _handler(engine)

    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value=PERSONA_ID),
    ):
        await handler.handle_memory_recall(_event("第一轮"), _request("第一轮"))
        second = _request("第二轮")
        await handler.handle_memory_recall(_event("第二轮"), second)
        third = _request("第三轮")
        await handler.handle_memory_recall(_event("第三轮"), third)

    assert "事实-C" in _injected_text(second)
    assert "事实-D" not in _injected_text(second)
    assert "事实-A" not in _injected_text(second)
    assert "事实-C" in _injected_text(third)
    assert "事实-D" not in _injected_text(third)


@pytest.mark.asyncio
async def test_missing_or_mismatched_canonical_record_is_not_carried():
    engine = _engine([[_hit("A")], []])
    handler = _handler(engine)

    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value=PERSONA_ID),
    ):
        await handler.handle_memory_recall(_event("第一轮"), _request("第一轮"))
        engine.canonical_store.get_fact_records = AsyncMock(
            return_value={"A": _record("A", scope="another-session")}
        )
        second = _request("第二轮")
        await handler.handle_memory_recall(_event("第二轮"), second)

    assert _injected_text(second) == ""


@pytest.mark.asyncio
async def test_archived_canonical_record_is_not_carried():
    engine = _engine([[_hit("A")], []])
    handler = _handler(engine)

    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value=PERSONA_ID),
    ):
        await handler.handle_memory_recall(_event("第一轮"), _request("第一轮"))
        archived = _record("A")
        archived["status"] = "archived"
        engine.canonical_store.get_fact_records = AsyncMock(
            return_value={"A": archived}
        )
        second = _request("第二轮")
        await handler.handle_memory_recall(_event("第二轮"), second)

    assert _injected_text(second) == ""


@pytest.mark.asyncio
async def test_session_reset_and_disabled_switch_clear_continuity_state():
    engine = _engine([[_hit("A")], [], [_hit("B")], [], []])
    handler = _handler(engine)

    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value=PERSONA_ID),
    ):
        event = _event("第一轮")
        await handler.handle_memory_recall(event, _request("第一轮"))
        await handler.handle_session_reset(event)
        after_reset = _request("重置后")
        await handler.handle_memory_recall(_event("重置后"), after_reset)

        await handler.handle_memory_recall(_event("新第一轮"), _request("新第一轮"))
        handler.config_manager._config["recall_engine"][
            "recall_continuity_enabled"
        ] = False
        disabled = _request("关闭后")
        await handler.handle_memory_recall(_event("关闭后"), disabled)
        handler.config_manager._config["recall_engine"][
            "recall_continuity_enabled"
        ] = True
        reenabled = _request("重新开启")
        await handler.handle_memory_recall(_event("重新开启"), reenabled)

    assert _injected_text(after_reset) == ""
    assert _injected_text(disabled) == ""
    assert _injected_text(reenabled) == ""


@pytest.mark.asyncio
async def test_top_k_zero_clears_existing_continuity_state():
    engine = _engine([[_hit("A")], []])
    handler = _handler(engine)

    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value=PERSONA_ID),
    ):
        await handler.handle_memory_recall(_event("第一轮"), _request("第一轮"))
        handler.config_manager._config["recall_engine"]["top_k"] = 0
        await handler.handle_memory_recall(_event("关闭"), _request("关闭"))
        handler.config_manager._config["recall_engine"]["top_k"] = 5
        after_reenable = _request("重开")
        await handler.handle_memory_recall(_event("重开"), after_reenable)

    assert _injected_text(after_reenable) == ""
    assert engine.search_memories.await_count == 2


@pytest.mark.asyncio
async def test_continuity_isolated_by_physical_session_and_persona():
    engine = _engine([[_hit("A")], [], []])
    handler = _handler(engine)
    persona = AsyncMock(side_effect=[PERSONA_ID, PERSONA_ID, "another-persona"])

    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=persona,
    ):
        await handler.handle_memory_recall(_event("第一轮"), _request("第一轮"))
        other_session = _request("其他会话")
        await handler.handle_memory_recall(
            _event("其他会话", session_id="test:private:other"),
            other_session,
        )
        changed_persona = _request("人格切换")
        await handler.handle_memory_recall(_event("人格切换"), changed_persona)

    assert _injected_text(other_session) == ""
    assert _injected_text(changed_persona) == ""


@pytest.mark.asyncio
async def test_shutdown_clears_all_continuity_state():
    engine = _engine([[_hit("A")]])
    handler = _handler(engine)

    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value=PERSONA_ID),
    ):
        await handler.handle_memory_recall(_event("第一轮"), _request("第一轮"))

    await handler.shutdown()
    remaining = await handler._memory_recall._continuity_cache.take(
        SESSION_ID,
        memory_scope=SESSION_ID,
        persona_id=PERSONA_ID,
    )
    assert remaining.previous_fact_ids == ()
    assert remaining.older_fact_ids == ()


@pytest.mark.asyncio
async def test_continuity_cache_is_bounded_ttl_checked_and_consume_on_read():
    now = 100.0
    cache = RecallContinuityCache(
        max_sessions=2,
        ttl_seconds=10,
        clock=lambda: now,
    )
    for session in ("s1", "s2", "s3"):
        await cache.put(
            session,
            [session],
            memory_scope=session,
            persona_id="p",
        )

    assert (
        await cache.take("s1", memory_scope="s1", persona_id="p")
    ).previous_fact_ids == ()
    second = await cache.take("s2", memory_scope="s2", persona_id="p")
    assert second.previous_fact_ids == ("s2",)
    assert second.older_fact_ids == ()
    assert (
        await cache.take("s2", memory_scope="s2", persona_id="p")
    ).previous_fact_ids == ()

    await cache.put("s4", ["A"], memory_scope="s4", persona_id="p")
    now = 111.0
    assert (
        await cache.take("s4", memory_scope="s4", persona_id="p")
    ).previous_fact_ids == ()
