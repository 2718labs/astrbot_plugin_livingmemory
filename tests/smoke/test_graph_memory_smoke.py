"""Smoke tests for graph memory critical user flows."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
from astrbot.api.platform import MessageType
from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.command_handler import CommandHandler
from astrbot_plugin_livingmemory.core.event_handler import EventHandler
from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.processors.memory_processor import MemoryProcessor

from tests.test_graph_memory import _DeterministicEmbeddingProvider, _FakeFaissDB

SESSION_ID = "smoke-session"
PERSONA_ID = "persona_smoke"
MEMORY_CONTENT = "project sync record"
MEMORY_FACT = "alice scheduled the project sync meeting at 3pm tomorrow"

async def _create_engine(tmp_path: Path) -> MemoryEngine:
    document_vectors = FaissVecDB(
        doc_store_path=str(tmp_path / "smoke_memory.db"),
        index_store_path=str(tmp_path / "smoke_memory.index"),
        embedding_provider=_DeterministicEmbeddingProvider(),
    )
    fact_vectors = FaissVecDB(
        doc_store_path=str(tmp_path / "smoke_facts.db"),
        index_store_path=str(tmp_path / "smoke_facts.index"),
        embedding_provider=_DeterministicEmbeddingProvider(),
    )
    await document_vectors.initialize()
    await fact_vectors.initialize()
    engine = MemoryEngine(
        db_path=str(tmp_path / "smoke_memory.db"),
        faiss_db=document_vectors,
        fact_vector_db=fact_vectors,
        graph_vector_db=_FakeFaissDB(),
        config={
            "fallback_enabled": True,
            "graph_memory_enabled": True,
            "document_route_weight": 0.6,
            "graph_route_weight": 0.4,
            "cross_route_bonus": 0.08,
        },
    )
    await engine.initialize()
    return engine


async def _seed_memory(engine: MemoryEngine) -> int:
    record = MemoryProcessor(llm_provider=object()).build_explicit_memory_record(
        memory=MEMORY_CONTENT,
        source_scope=SESSION_ID,
        topics=["project sync"],
        key_facts=[MEMORY_FACT],
        participants=["alice"],
        importance=0.9,
        origin="test_fixture",
    )
    return await engine.add_canonical_memory(
        metadata=record.metadata,
        session_id=SESSION_ID,
        persona_id=PERSONA_ID,
        importance=record.importance,
    )


async def _settle_background_tasks() -> None:
    await asyncio.sleep(0.05)


def _make_event(query: str):
    event = Mock()
    event.unified_msg_origin = SESSION_ID
    event.get_message_type = Mock(return_value=MessageType.FRIEND_MESSAGE)
    event.get_sender_id = Mock(return_value="user-1")
    event.get_self_id = Mock(return_value="bot-1")
    event.get_sender_name = Mock(return_value="SmokeUser")
    event.message_str = query
    event.get_message_str = Mock(return_value=query)
    event.get_messages = Mock(return_value=[])
    event.get_platform_name = Mock(return_value="test")
    event.plain_result = lambda message: message
    return event


def _make_request(prompt: str):
    req = Mock()
    req.prompt = prompt
    req.system_prompt = ""
    req.contexts = []
    req.extra_user_content_parts = []
    return req


@pytest.mark.asyncio
async def test_smoke_search_by_participant_hits_graph_route(tmp_path: Path):
    engine = await _create_engine(tmp_path)
    try:
        memory_id = await _seed_memory(engine)
        results = await engine.search_memories(
            query="alice",
            k=3,
            session_id=SESSION_ID,
            persona_id=PERSONA_ID,
        )

        assert results
        assert results[0].doc_id == memory_id
        assert results[0].metadata["fact_id"]
        assert (results[0].score_breakdown or {}).get("graph_keyword_raw", 0.0) > 0
    finally:
        await _settle_background_tasks()
        await engine.close()


@pytest.mark.asyncio
async def test_smoke_search_by_summary_includes_four_mode_breakdown(tmp_path: Path):
    engine = await _create_engine(tmp_path)
    try:
        memory_id = await _seed_memory(engine)
        results = await engine.search_memories(
            query=MEMORY_FACT,
            k=3,
            session_id=SESSION_ID,
            persona_id=PERSONA_ID,
        )

        assert results
        assert results[0].doc_id == memory_id
        breakdown = results[0].score_breakdown or {}
        assert max(
            breakdown.get("fact_lexical_raw", 0.0),
            breakdown.get("fact_vector_raw", 0.0),
        ) > 0
        assert breakdown.get("graph_calibrated", 0.0) > 0
        assert "graph_keyword_raw" in breakdown
        assert "graph_vector_raw" in breakdown
    finally:
        await _settle_background_tasks()
        await engine.close()


@pytest.mark.asyncio
async def test_smoke_rebuild_graph_command_reports_success(tmp_path: Path):
    engine = await _create_engine(tmp_path)
    try:
        memory_id = await _seed_memory(engine)
        assert engine.graph_memory_manager is not None
        await engine.graph_memory_manager.delete_memory(memory_id)

        handler = CommandHandler(
            context=Mock(),
            config_manager=ConfigManager(),
            memory_engine=engine,
            conversation_manager=None,
            index_validator=None,
        )
        messages = [
            message
            async for message in handler.handle_rebuild_graph(
                _make_event("/lmem rebuild-graph")
            )
        ]

        assert len(messages) == 2
        assert messages[0].endswith("...")
        assert any(char.isdigit() for char in messages[1])
    finally:
        await _settle_background_tasks()
        await engine.close()


@pytest.mark.asyncio
async def test_smoke_event_recall_injects_memory_into_prompt(tmp_path: Path):
    engine = await _create_engine(tmp_path)
    try:
        await _seed_memory(engine)
        conversation_manager = Mock()
        conversation_manager.add_message_from_event = AsyncMock()
        conversation_manager.store = None

        handler = EventHandler(
            context=Mock(),
            config_manager=ConfigManager(
                {
                    "recall_engine": {
                        "top_k": 3,
                        "injection_method": "extra_user_content",
                    },
                    "session_manager": {"max_messages_per_session": 100},
                }
            ),
            memory_engine=engine,
            memory_processor=Mock(),
            conversation_manager=conversation_manager,
        )
        req = _make_request("who is alice?")

        with patch(
            "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
            new_callable=AsyncMock,
        ) as get_persona:
            get_persona.return_value = PERSONA_ID
            await handler.handle_memory_recall(_make_event("alice"), req)

        assert len(req.extra_user_content_parts) == 1
        assert "<RAG-Faiss-Memory>" in req.extra_user_content_parts[0].text
        assert MEMORY_FACT in req.extra_user_content_parts[0].text
        # 刚写入的父记忆在 recent 窗口内，overview 只允许以"最近对话摘要"
        # 形式出现（recent 块），不允许作为普通事实行泄漏。
        text = req.extra_user_content_parts[0].text
        assert "最近对话摘要：project sync record" in text
        assert "\n- project sync record" not in text
        conversation_manager.add_message_from_event.assert_awaited_once()
    finally:
        await _settle_background_tasks()
        await engine.close()
