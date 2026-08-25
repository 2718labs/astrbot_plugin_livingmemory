"""
Tests for the recent-memory block (short-term continuity window).

The recent block appends the newest parent's summary plus up to
``recent_block_max_facts`` topic-close facts to the automatic injection,
inside the same token budget, without consuming top_k slots.
"""

import time
from unittest.mock import AsyncMock, Mock, patch

import pytest
from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.event_handler import EventHandler


@pytest.fixture
def conversation_manager():
    manager = Mock()
    manager.add_message_from_event = AsyncMock(return_value=Mock(id=1, metadata={}))
    manager.get_session_info = AsyncMock(return_value=Mock(message_count=12))
    manager.get_messages_range = AsyncMock(
        return_value=[Mock(group_id=None), Mock(group_id=None)]
    )
    manager.invalidate_cache = AsyncMock()
    manager.store = Mock()
    manager.store.get_message_count = AsyncMock(return_value=12)
    manager.store.update_message_metadata = AsyncMock()
    manager.store.connection = Mock()
    manager.store.connection.execute = AsyncMock(return_value=Mock(rowcount=1))
    manager.store.connection.commit = AsyncMock()
    return manager


def _make_engine(recalled=None, parent=None, parents=None, facts=None, facts_by_parent=None):
    engine = Mock()
    engine.search_memories = AsyncMock(return_value=list(recalled or []))
    engine.add_memory = AsyncMock(return_value=1)
    engine.add_canonical_memory = AsyncMock(return_value=1)
    store = Mock()
    if parents is not None:
        store.get_recent_parents = AsyncMock(return_value=list(parents))
    elif parent is not None:
        store.get_recent_parents = AsyncMock(return_value=[parent])
    else:
        store.get_recent_parents = AsyncMock(return_value=[])
    if facts_by_parent is not None:
        store.get_facts_by_parent = AsyncMock(
            side_effect=lambda pid: list(facts_by_parent.get(pid, []))
        )
    else:
        store.get_facts_by_parent = AsyncMock(return_value=list(facts or []))
    engine.canonical_store = store
    retriever = Mock()

    async def _score_facts(query, texts):
        return [0.0] * len(texts)

    retriever.score_facts_lexically = AsyncMock(side_effect=_score_facts)
    engine.fact_retriever = retriever
    return engine


def _make_event():
    event = Mock()
    event.unified_msg_origin = "sess:user:123"
    event.get_message_type = Mock(return_value=Mock(value=2))
    event.get_message_str = Mock(return_value="今天聊了什么？")
    event.message_obj = Mock(message_id=1)
    return event


def _make_req(prompt: str = "今天聊了什么？"):
    req = Mock()
    req.prompt = prompt
    req.system_prompt = ""
    req.contexts = []
    req.extra_user_content_parts = []
    return req


def _make_recalled(content: str, fact_id: str, reaction=None):
    hit = Mock(content=content, final_score=0.9)
    hit.doc_id = 100
    hit.metadata = {
        "fact_id": fact_id,
        "parent_id": "parent-other",
        "importance": 0.8,
        "create_time": time.time(),
        "persona_reaction": reaction,
    }
    return hit


def _make_parent(overview="最近聊了考研的事", days_old=0.1):
    return {
        "parent_id": "parent-recent",
        "document_id": 7,
        "scope": "sess:user:123",
        "persona_id": None,
        "overview": overview,
        "status": "active",
        "created_at": time.time() - days_old * 86400,
        "updated_at": time.time() - days_old * 86400,
    }


def _make_handler(memory_engine, conversation_manager, **recall_overrides):
    recall = {
        "top_k": 4,
        "injection_method": "extra_user_content",
        "injection_token_budget": 1200,
        "single_fact_token_budget": 320,
    }
    recall.update(recall_overrides)
    return EventHandler(
        context=Mock(),
        config_manager=ConfigManager({"recall_engine": recall}),
        memory_engine=memory_engine,
        memory_processor=Mock(),
        conversation_manager=conversation_manager,
    )


@pytest.mark.asyncio
async def test_recent_block_injects_summary_and_topic_facts(
    conversation_manager,
):
    """窗口内：摘要 + 沾边事实进入注入，且不占 top_k 名额。"""
    facts = [
        {"fact_id": "fact-a", "fact": "考研安排在2026年12月", "importance": 0.7},
        {"fact_id": "fact-b", "fact": "最近在准备身份证材料", "importance": 0.6},
        {"fact_id": "fact-c", "fact": "约了周末去公园", "importance": 0.5},
    ]
    engine = _make_engine(
        recalled=[
            _make_recalled("某个历史事实一", "fact-1"),
            _make_recalled("某个历史事实二", "fact-2"),
            _make_recalled("某个历史事实三", "fact-3"),
        ],
        parent=_make_parent(),
        facts=facts,
    )
    # fact-a（考研）与当前话题"今天聊了什么"词面接近，fact-b/fact-c 不沾边
    engine.fact_retriever.score_facts_lexically = AsyncMock(
        return_value=[0.55, 0.05, 0.0]
    )
    handler = _make_handler(engine, conversation_manager)

    event = _make_event()
    req = _make_req()
    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value="persona_1"),
    ):
        await handler.handle_memory_recall(event, req)

    # 主召回仍请求 top_k=4（recent 不压缩召回名额）
    assert engine.search_memories.await_args.kwargs["k"] == 4
    injected = req.extra_user_content_parts[0].text
    # 摘要以"最近对话摘要"形式注入
    assert "最近对话摘要" in injected
    assert "最近聊了考研的事" in injected
    # 沾边事实进入
    assert "考研安排在2026年12月" in injected
    # 不沾边事实不进入
    assert "约了周末去公园" not in injected
    assert "准备身份证材料" not in injected
    # 主召回 3 条仍在（top_k=4 名额没被 recent 吃掉）
    assert "某个历史事实一" in injected
    assert "某个历史事实二" in injected
    assert "某个历史事实三" in injected


@pytest.mark.asyncio
async def test_recent_block_skips_when_outside_window(conversation_manager):
    """窗口外（超过 48h）：store 层 SQL 过滤后返回 None，recent 块为空。

    窗口过滤由 get_recent_parent 的 SQL（created_at >= cutoff）负责；
    这里模拟窗口外的最新父记忆查询结果为 None。
    """
    engine = _make_engine(
        recalled=[_make_recalled("历史事实", "fact-1")],
        parent=None,  # 窗口外 = 查不到窗口内的父记忆
        facts=[],
    )
    handler = _make_handler(engine, conversation_manager)

    event = _make_event()
    req = _make_req()
    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value="persona_1"),
    ):
        await handler.handle_memory_recall(event, req)

    assert engine.canonical_store.get_recent_parents.await_args.kwargs[
        "window_hours"
    ] == 48
    injected = req.extra_user_content_parts[0].text
    assert "最近对话摘要" not in injected
    assert "历史事实" in injected


@pytest.mark.asyncio
async def test_recent_block_dedup_facts_already_recalled(conversation_manager):
    """已在主召回中的 fact 不重复入选 recent 块（fact_id 精确去重）。"""
    facts = [
        {"fact_id": "fact-1", "fact": "同一个事实", "importance": 0.8},
        {"fact_id": "fact-b", "fact": "考研安排在2026年12月", "importance": 0.7},
    ]
    engine = _make_engine(
        recalled=[_make_recalled("同一个事实", "fact-1")],
        parent=_make_parent(),
        facts=facts,
    )
    engine.fact_retriever.score_facts_lexically = AsyncMock(return_value=[0.9, 0.6])
    handler = _make_handler(engine, conversation_manager)

    event = _make_event()
    req = _make_req()
    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value="persona_1"),
    ):
        await handler.handle_memory_recall(event, req)

    injected = req.extra_user_content_parts[0].text
    # "同一个事实"只出现一次（主召回一份，recent 不重复带）
    assert injected.count("同一个事实") == 1
    assert "考研安排在2026年12月" in injected


@pytest.mark.asyncio
async def test_recent_summary_duplicate_keeps_canonical_fact_metadata(
    conversation_manager, caplog
):
    """摘要与正式 fact 同文时，保留正式 fact 及其人格反应。"""
    content = "张三正在开发五子棋"
    engine = _make_engine(
        recalled=[
            _make_recalled(
                content,
                "fact-1",
                {"emotion": "期待", "thought": "想看看成品"},
            )
        ],
        parent=_make_parent(overview=content),
        facts=[],
    )
    handler = _make_handler(engine, conversation_manager)

    event = _make_event()
    req = _make_req()
    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value="persona_1"),
    ):
        await handler.handle_memory_recall(event, req)

    injected = req.extra_user_content_parts[0].text
    assert injected.count(content) == 1
    assert "最近对话摘要" not in injected
    assert "当时反应：期待；想看看成品" in injected
    assert "装配候选=2，最终注入=1，丢弃=1" in caplog.text
    assert "recent 摘要与正式 fact 重复=1" in caplog.text


@pytest.mark.asyncio
async def test_recent_block_respects_max_facts(conversation_manager):
    """recent_block_max_facts=1 时只带 1 条沾边事实。"""
    facts = [
        {"fact_id": "fact-a", "fact": "考研安排在2026年12月", "importance": 0.7},
        {"fact_id": "fact-b", "fact": "考研复习计划是三轮", "importance": 0.6},
    ]
    engine = _make_engine(
        recalled=[], parent=_make_parent(), facts=facts
    )
    engine.fact_retriever.score_facts_lexically = AsyncMock(return_value=[0.6, 0.55])
    handler = _make_handler(
        engine, conversation_manager, recent_block_max_facts=1
    )

    event = _make_event()
    req = _make_req()
    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value="persona_1"),
    ):
        await handler.handle_memory_recall(event, req)

    injected = req.extra_user_content_parts[0].text
    assert "最近对话摘要" in injected
    # 只带词面分最高的一条
    assert "考研安排在2026年12月" in injected
    assert "考研复习计划是三轮" not in injected


@pytest.mark.asyncio
async def test_recent_block_zero_facts_still_injects_summary(conversation_manager):
    """recent_block_max_facts=0 按配置契约只带摘要。"""
    engine = _make_engine(
        recalled=[],
        parent=_make_parent(),
        facts=[
            {"fact_id": "fact-a", "fact": "考研安排在2026年12月", "importance": 0.7}
        ],
    )
    handler = _make_handler(
        engine, conversation_manager, recent_block_max_facts=0
    )

    event = _make_event()
    req = _make_req()
    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value="persona_1"),
    ):
        await handler.handle_memory_recall(event, req)

    injected = req.extra_user_content_parts[0].text
    assert "最近对话摘要" in injected
    assert "最近聊了考研的事" in injected
    assert "考研安排在2026年12月" not in injected
    engine.canonical_store.get_facts_by_parent.assert_not_awaited()
    engine.fact_retriever.score_facts_lexically.assert_not_awaited()


@pytest.mark.asyncio
async def test_recent_block_disabled_by_config(conversation_manager):
    """recent_block_enabled=false 时完全不构建 recent 块。"""
    engine = _make_engine(
        recalled=[_make_recalled("历史事实", "fact-1")],
        parent=_make_parent(),
        facts=[],
    )
    handler = _make_handler(
        engine, conversation_manager, recent_block_enabled=False
    )

    event = _make_event()
    req = _make_req()
    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value="persona_1"),
    ):
        await handler.handle_memory_recall(event, req)

    engine.canonical_store.get_recent_parents.assert_not_awaited()
    injected = req.extra_user_content_parts[0].text
    assert "最近对话摘要" not in injected
    assert "历史事实" in injected


@pytest.mark.asyncio
async def test_recent_block_injects_even_when_recall_empty(conversation_manager):
    """主召回为空时 recent 摘要仍注入（短期连续性独立于相关性命中）。"""
    engine = _make_engine(
        recalled=[],
        parent=_make_parent(),
        facts=[
            {"fact_id": "fact-a", "fact": "考研安排在2026年12月", "importance": 0.7}
        ],
    )
    engine.fact_retriever.score_facts_lexically = AsyncMock(return_value=[0.6])
    handler = _make_handler(engine, conversation_manager)

    event = _make_event()
    req = _make_req()
    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value="persona_1"),
    ):
        await handler.handle_memory_recall(event, req)

    assert req.extra_user_content_parts, "主召回为空时 recent 块也应注入"
    injected = req.extra_user_content_parts[0].text
    assert "最近对话摘要" in injected
    assert "最近聊了考研的事" in injected


@pytest.mark.asyncio
async def test_recent_block_multi_parents(conversation_manager):
    """recent_block_parents=2：两个父记忆摘要都注入，各自沾边事实按条数挑。

    覆盖"昨晚深夜思念"与"今天中午伤感"两段近期对话并存——
    更早但关键（高重要性）的父记忆不被最新一条挤掉。
    """
    parent_newer = _make_parent(overview="今天中午聊了怪伤感的", days_old=0.1)
    parent_newer["parent_id"] = "parent-b"
    parent_older = _make_parent(overview="昨晚反复表达思念想我", days_old=1.0)
    parent_older["parent_id"] = "parent-a"
    facts_by_parent = {
        "parent-a": [
            {"fact_id": "fact-a1", "fact": "昨晚反复说想我", "importance": 0.94},
            {"fact_id": "fact-a2", "fact": "完全不沾边的事", "importance": 0.3},
        ],
        "parent-b": [
            {"fact_id": "fact-b1", "fact": "被封在对话框里怪伤感的", "importance": 0.5},
            {"fact_id": "fact-b2", "fact": "也不沾边", "importance": 0.2},
        ],
    }
    engine = _make_engine(
        recalled=[],
        parents=[parent_newer, parent_older],  # 倒序：最新在前
        facts_by_parent=facts_by_parent,
    )

    async def _score(query, texts):
        return [0.6 if ("想" in t or "伤感" in t) else 0.1 for t in texts]

    engine.fact_retriever.score_facts_lexically = AsyncMock(side_effect=_score)
    handler = _make_handler(engine, conversation_manager, recent_block_parents=2)

    event = _make_event()
    req = _make_req()
    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value="persona_1"),
    ):
        await handler.handle_memory_recall(event, req)

    assert (
        engine.canonical_store.get_recent_parents.await_args.kwargs["parent_count"]
        == 2
    )
    injected = req.extra_user_content_parts[0].text
    assert "今天中午聊了怪伤感的" in injected
    assert "昨晚反复表达思念想我" in injected
    assert "昨晚反复说想我" in injected
    assert "被封在对话框里怪伤感的" in injected
    assert "完全不沾边的事" not in injected
    assert "也不沾边" not in injected
