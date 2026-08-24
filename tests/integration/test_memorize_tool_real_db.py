"""
End-to-end proof that the memorize tool really archives into storage.

Runs the real MemoryEngine (SQLite + FAISS) and the real
MemoryMemorizeTool.call() exactly as the agent would, then inspects
the database to confirm the fact is persisted, not hallucinated.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import aiosqlite
import pytest
from astrbot.api.platform import MessageType
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.processors.memory_processor import MemoryProcessor
from astrbot_plugin_livingmemory.core.tools.memory_memorize_tool import (
    MemoryMemorizeTool,
)

from tests.test_graph_memory import _DeterministicEmbeddingProvider, _FakeFaissDB


async def _create_engine(tmp_path: Path) -> MemoryEngine:
    main_db = str(tmp_path / "mem_tool.db")
    document_vectors = FaissVecDB(
        doc_store_path=main_db,  # 与 MemoryEngine 同一文件：documents 行落在此库
        index_store_path=str(tmp_path / "mem_tool_docs.index"),
        embedding_provider=_DeterministicEmbeddingProvider(),
    )
    fact_vectors = FaissVecDB(
        doc_store_path=str(tmp_path / "mem_tool_facts.db"),
        index_store_path=str(tmp_path / "mem_tool_facts.index"),
        embedding_provider=_DeterministicEmbeddingProvider(),
    )
    await document_vectors.initialize()
    await fact_vectors.initialize()
    engine = MemoryEngine(
        db_path=main_db,
        faiss_db=document_vectors,
        fact_vector_db=fact_vectors,
        graph_vector_db=_FakeFaissDB(),
        config={
            "graph_memory_enabled": False,
            "search_cache_enabled": False,
        },
    )
    await engine.initialize()
    return engine


def _make_run_context():
    event = Mock()
    event.unified_msg_origin = "test:private:mem-tool-session"
    event.message_obj = None
    event.get_message_type = Mock(return_value=MessageType.FRIEND_MESSAGE)
    event.get_platform_name = Mock(return_value="test")
    event.get_sender_id = Mock(return_value="user-1")
    event.get_sender_name = Mock(return_value="测试用户")
    event.get_self_id = Mock(return_value="bot-1")
    run_context = Mock()
    run_context.context = Mock()
    run_context.context.event = event
    return run_context


@pytest.mark.asyncio
async def test_memorize_tool_persists_fact_into_real_storage(tmp_path):
    """agent 按说明书调用 memorize 工具后，事实真实写入 memory_facts 等表。"""
    engine = await _create_engine(tmp_path)
    try:
        tool = MemoryMemorizeTool(
            context=Mock(),
            memory_engine=engine,
            memory_processor=MemoryProcessor(llm_provider=object()),
        )
        with patch(
            "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id",
            new_callable=AsyncMock,
        ) as get_persona:
            get_persona.return_value = "persona_x"
            prepared = json.loads(
                await tool.call(
                    _make_run_context(),
                    memory="用户喜欢在雨天听爵士乐",
                    key_facts=["用户雨天会放爵士乐"],
                    participants=["测试用户"],
                    sentiment="positive",
                    importance=0.8,
                )
            )
            assert prepared["requires_topic_selection"] is True
            assert prepared["topic_candidates"] == []
            raw = await tool.call(
                _make_run_context(),
                memory="用户喜欢在雨天听爵士乐",
                new_topic="音乐偏好",
                key_facts=["用户雨天会放爵士乐"],
                participants=["测试用户"],
                sentiment="positive",
                importance=0.8,
            )

        result = json.loads(raw)
        assert result["memorized"] is True
        # v3 契约：入库的是事实投影（key_facts 优先），不是 memory 原文
        assert result["content"] == "用户雨天会放爵士乐"

        # —— 落库核验：documents（父壳）+ memory_parents + memory_facts ——
        db_path = engine.db_path
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM documents")
            doc_count = (await cursor.fetchone())[0]
            assert doc_count == 1, f"documents 应有 1 条，实际 {doc_count}"

            cursor = await db.execute("SELECT COUNT(*) FROM memory_parents")
            parent_count = (await cursor.fetchone())[0]
            assert parent_count == 1

            cursor = await db.execute("SELECT COUNT(*) FROM memory_facts")
            fact_count = (await cursor.fetchone())[0]
            assert fact_count == 1

            cursor = await db.execute(
                "SELECT fact_json FROM memory_facts LIMIT 1"
            )
            row = await cursor.fetchone()
            fact = json.loads(row[0])
            assert fact["fact"] == "用户雨天会放爵士乐"
            assert fact["topics"] == ["音乐偏好"]
            assert fact["participant_refs"][0]["participant_id"] == "test:user-1"
            assert fact["participant_refs"][0]["identity_key"] == "test:user-1"

            cursor = await db.execute(
                "SELECT overview FROM memory_parents LIMIT 1"
            )
            overview = (await cursor.fetchone())[0]
            assert overview == "用户喜欢在雨天听爵士乐"

        # —— 召回核验：同一事实能被搜回来 ——
        hits = await engine.search_memories(
            query="雨天会放爵士乐",
            k=3,
            session_id="test:private:mem-tool-session",
            persona_id="persona_x",
        )
        assert hits, "写入后应能被召回"
        assert hits[0].content == "用户雨天会放爵士乐"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_memorize_tool_empty_input_does_not_write(tmp_path):
    """空 memory 输入应拒绝且不产生任何写入。"""
    engine = await _create_engine(tmp_path)
    try:
        tool = MemoryMemorizeTool(
            context=Mock(),
            memory_engine=engine,
            memory_processor=MemoryProcessor(llm_provider=object()),
        )
        raw = await tool.call(_make_run_context(), memory="   ")
        result = json.loads(raw)
        assert result == {"memorized": False, "error": "memory is empty"}

        async with aiosqlite.connect(engine.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM documents")
            assert (await cursor.fetchone())[0] == 0
            cursor = await db.execute("SELECT COUNT(*) FROM memory_facts")
            assert (await cursor.fetchone())[0] == 0
    finally:
        await engine.close()
