"""Tests for the active long-term memory memorize tool."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from astrbot.api.platform import MessageType

from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.models.memory_processing import MemoryWriteRecord
from astrbot_plugin_livingmemory.core.processors.memory_processor import MemoryProcessor
from astrbot_plugin_livingmemory.core.tools.memory_memorize_tool import (
    MemoryMemorizeTool,
)


@pytest.fixture
def memory_engine():
    engine = Mock()
    engine.add_memory = AsyncMock(return_value=42)
    engine.add_canonical_memory = AsyncMock(return_value=42)
    engine.get_topic_candidates = AsyncMock(return_value=[])
    engine.search_topic_candidates = AsyncMock(return_value=[])
    return engine


@pytest.fixture
def memory_processor():
    processor = Mock()
    processor.build_explicit_memory_record = Mock(
        return_value=MemoryWriteRecord(
            content="不加糖",
            metadata={
                "memory_schema_version": "v3",
                "topics": ["饮食偏好"],
                "key_facts": [{"fact": "不加糖"}],
                "sentiment": "neutral",
                "interaction_type": "private_chat",
                "canonical_summary": "用户喜欢黑咖啡",
                "source_window": {"fingerprint": "src_explicit"},
                "source_session_id": "test:private:session-1",
                "memory_origin": "agent_memorize_tool",
                "summary_quality": "normal",
            },
            importance=0.8,
        )
    )
    return processor


def _make_run_context(message_type=MessageType.FRIEND_MESSAGE):
    event = Mock()
    event.unified_msg_origin = "test:private:session-1"
    event.message_obj = None
    event.get_message_type = Mock(return_value=message_type)
    event.get_platform_name = Mock(return_value="test")
    event.get_sender_id = Mock(return_value="user-1")
    event.get_sender_name = Mock(return_value="张三")
    event.get_self_id = Mock(return_value="bot-1")

    run_context = Mock()
    run_context.context = Mock()
    run_context.context.event = event
    return run_context


@pytest.mark.asyncio
async def test_memory_memorize_tool_first_call_returns_relevant_topics_without_write(
    memory_engine, memory_processor
):
    memory_engine.search_topic_candidates.return_value = [
        {"topic_id": "topic_game", "name": "游戏开发"}
    ]
    tool = MemoryMemorizeTool(
        context=Mock(),
        memory_engine=memory_engine,
        memory_processor=memory_processor,
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id",
        new_callable=AsyncMock,
        return_value="persona_a",
):
        raw_result = await tool.call(
            _make_run_context(), memory="张三正在开发五子棋"
        )

    result = json.loads(raw_result)
    assert result["memorized"] is False
    assert result["requires_topic_selection"] is True
    assert result["topic_candidates"] == [
        {"topic_id": "topic_game", "name": "游戏开发"}
    ]
    assert "__none__" in result["guidance"]
    assert "具体日期" in result["guidance"]
    assert "persona_reaction" in result["guidance"]
    assert "key_facts" in result["guidance"]
    memory_engine.search_topic_candidates.assert_awaited_once_with(
        "张三正在开发五子棋",
        scope="test:private:session-1",
        persona_id="persona_a",
        limit=5,
    )
    memory_processor.build_explicit_memory_record.assert_not_called()
    memory_engine.add_canonical_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_memorize_tool_writes_current_session_and_persona(
    memory_engine, memory_processor
):
    memory_engine.search_topic_candidates.return_value = [
        {"topic_id": "topic_food", "name": "饮食偏好"}
    ]
    tool = MemoryMemorizeTool(
        context=Mock(),
        memory_engine=memory_engine,
        memory_processor=memory_processor,
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        raw_result = await tool.call(
            _make_run_context(),
            memory="用户喜欢黑咖啡",
            topic="topic_food",
            key_facts=["不加糖"],
            importance=0.8,
        )

    result = json.loads(raw_result)
    assert result["memorized"] is True
    assert result["session_id"] == "test:private:session-1"
    assert result["persona_id"] == "persona_a"
    memory_engine.add_canonical_memory.assert_awaited_once()
    call_kwargs = memory_engine.add_canonical_memory.await_args.kwargs
    assert call_kwargs["session_id"] == "test:private:session-1"
    assert call_kwargs["persona_id"] == "persona_a"
    build_kwargs = memory_processor.build_explicit_memory_record.call_args.kwargs
    assert build_kwargs["topics"] == ["饮食偏好"]
    assert build_kwargs["topic_candidates"] == [
        {"topic_id": "topic_food", "name": "饮食偏好"}
    ]
    assert build_kwargs["persona_reaction"] is None


@pytest.mark.asyncio
async def test_memory_memorize_tool_writes_resolved_user_scope(
    memory_engine, memory_processor
):
    tool = MemoryMemorizeTool(
        context=Mock(),
        config_manager=ConfigManager(
            {"filtering_settings": {"memory_scope_mode": "user"}}
        ),
        memory_engine=memory_engine,
        memory_processor=memory_processor,
    )
    run_context = _make_run_context()
    event = run_context.context.event
    event.get_platform_name = Mock(return_value="test")
    event.get_sender_id = Mock(return_value="user-1")

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id",
        new_callable=AsyncMock,
        return_value="persona_a",
    ):
        await tool.call(
            run_context, memory="remember this", topic="__none__"
        )

    build_kwargs = memory_processor.build_explicit_memory_record.call_args.kwargs
    call_kwargs = memory_engine.add_canonical_memory.await_args.kwargs
    assert call_kwargs["session_id"] == "livingmemory:user:test:user-1"
    assert build_kwargs["source_scope"] == "livingmemory:user:test:user-1"


@pytest.mark.asyncio
async def test_memory_memorize_tool_uses_memory_processor_format(
    memory_engine, memory_processor
):
    tool = MemoryMemorizeTool(
        context=Mock(),
        memory_engine=memory_engine,
        memory_processor=memory_processor,
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        await tool.call(
            _make_run_context(),
            memory="用户喜欢黑咖啡",
            topic="饮食偏好",
            key_facts=["不加糖"],
            sentiment="neutral",
            importance=2.0,
        )

    memory_processor.build_explicit_memory_record.assert_called_once_with(
        memory="用户喜欢黑咖啡",
        source_scope="test:private:session-1",
        topics=["饮食偏好"],
        key_facts=["不加糖"],
        participants=[],
        sentiment="neutral",
        importance=2.0,
        topic_candidates=[],
        participant_identities=[
            {
                "identity_key": "test:user-1",
                "sender_id": "user-1",
                "platform": "test",
                "display_name": "张三",
                "aliases": ["张三"],
                "is_bot": False,
            },
            {
                "identity_key": "test:bot-1",
                "sender_id": "bot-1",
                "platform": "test",
                "display_name": "bot-1",
                "aliases": ["bot-1"],
                "is_bot": True,
            },
        ],
        source_reference=None,
        origin="agent_memorize_tool",
        is_group_chat=False,
        persona_reaction=None,
    )
    call_kwargs = memory_engine.add_canonical_memory.await_args.kwargs
    assert call_kwargs["importance"] == 0.8
    assert call_kwargs["metadata"]["memory_origin"] == "agent_memorize_tool"
    assert "memorize_reason" not in call_kwargs["metadata"]


@pytest.mark.asyncio
async def test_memory_memorize_tool_reuses_event_sender_identity(memory_engine):
    tool = MemoryMemorizeTool(
        context=Mock(),
        memory_engine=memory_engine,
        memory_processor=MemoryProcessor(llm_provider=object()),
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id",
        new_callable=AsyncMock,
        return_value="persona_a",
):
        raw_result = await tool.call(
            _make_run_context(),
            memory="张三正在开发五子棋",
            participants=["张三"],
            topic="__none__",
        )

    assert json.loads(raw_result)["memorized"] is True
    metadata = memory_engine.add_canonical_memory.await_args.kwargs["metadata"]
    user_identity = next(
        item
        for item in metadata["participant_identities"]
        if not item["is_bot"]
    )
    assert user_identity["identity_key"] == "test:user-1"
    participant_ref = metadata["key_facts"][0]["participant_refs"][0]
    assert participant_ref["participant_id"] == "test:user-1"
    assert participant_ref["identity_key"] == "test:user-1"
    assert participant_ref["source"] == "message_sender"


@pytest.mark.asyncio
async def test_memory_memorize_tool_rejects_topic_id_outside_current_candidates(
    memory_engine, memory_processor
):
    memory_engine.search_topic_candidates.return_value = [
        {"topic_id": "topic_game", "name": "游戏开发"}
    ]
    tool = MemoryMemorizeTool(
        context=Mock(),
        memory_engine=memory_engine,
        memory_processor=memory_processor,
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id",
        new_callable=AsyncMock,
        return_value="persona_a",
):
        raw_result = await tool.call(
            _make_run_context(),
            memory="张三正在开发五子棋",
            topic="topic_invented",
        )

    result = json.loads(raw_result)
    assert result["memorized"] is False
    assert result["error"] == "topic is not a current candidate for this memory"
    memory_engine.add_canonical_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_memorize_tool_rejects_new_topic_matching_candidate(
    memory_engine, memory_processor
):
    memory_engine.search_topic_candidates.return_value = [
        {"topic_id": "topic_game", "name": "游戏开发"}
    ]
    tool = MemoryMemorizeTool(
        context=Mock(),
        memory_engine=memory_engine,
        memory_processor=memory_processor,
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id",
        new_callable=AsyncMock,
        return_value="persona_a",
    ):
        raw_result = await tool.call(
            _make_run_context(),
            memory="张三正在开发五子棋",
            topic="游戏开发",
        )

    result = json.loads(raw_result)
    assert result["memorized"] is False
    assert result["error"] == "topic already exists; use its topic_id"
    assert result["existing_topic"] == {"topic_id": "topic_game", "name": "游戏开发"}
    memory_engine.add_canonical_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_memorize_tool_detects_group_chat(memory_engine, memory_processor):
    tool = MemoryMemorizeTool(
        context=Mock(),
        memory_engine=memory_engine,
        memory_processor=memory_processor,
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        await tool.call(
            _make_run_context(MessageType.GROUP_MESSAGE),
            memory="群里约定周五复盘",
            topic="__none__",
        )

    assert memory_processor.build_explicit_memory_record.call_args.kwargs[
        "is_group_chat"
    ] is True


@pytest.mark.asyncio
async def test_memory_memorize_tool_normalizes_invalid_sentiment(
    memory_engine, memory_processor
):
    tool = MemoryMemorizeTool(
        context=Mock(),
        memory_engine=memory_engine,
        memory_processor=memory_processor,
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        await tool.call(
            _make_run_context(),
            memory="用户希望记住插件行为",
            topic="__none__",
            sentiment="SURPRISED",
        )

    assert (
        memory_processor.build_explicit_memory_record.call_args.kwargs["sentiment"]
        == "neutral"
    )


@pytest.mark.asyncio
async def test_memory_memorize_tool_handles_non_string_sentiment(
    memory_engine, memory_processor
):
    tool = MemoryMemorizeTool(
        context=Mock(),
        memory_engine=memory_engine,
        memory_processor=memory_processor,
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        await tool.call(
            _make_run_context(),
            memory="用户希望记住插件行为",
            topic="__none__",
            sentiment=1,
        )

    assert (
        memory_processor.build_explicit_memory_record.call_args.kwargs["sentiment"]
        == "neutral"
    )


@pytest.mark.asyncio
async def test_memory_memorize_tool_returns_error_for_empty_memory(
    memory_engine, memory_processor
):
    tool = MemoryMemorizeTool(
        context=Mock(),
        memory_engine=memory_engine,
        memory_processor=memory_processor,
    )

    raw_result = await tool.call(_make_run_context(), memory="   ")
    result = json.loads(raw_result)

    assert result == {"memorized": False, "error": "memory is empty"}
    memory_processor.build_explicit_memory_record.assert_not_called()
    memory_engine.add_canonical_memory.assert_not_called()


@pytest.mark.asyncio
async def test_memory_memorize_tool_returns_not_initialized_error(memory_engine):
    tool = MemoryMemorizeTool(
        context=None,
        memory_engine=memory_engine,
        memory_processor=None,
    )

    raw_result = await tool.call(_make_run_context(), memory="需要记住的内容")
    result = json.loads(raw_result)

    assert result == {
        "memorized": False,
        "error": "memory memorize tool is not initialized",
    }
    memory_engine.add_canonical_memory.assert_not_called()


@pytest.mark.asyncio
async def test_memory_memorize_tool_hides_internal_exception_details(
    memory_engine, memory_processor
):
    tool = MemoryMemorizeTool(
        context=Mock(),
        memory_engine=memory_engine,
        memory_processor=memory_processor,
    )
    memory_engine.add_canonical_memory = AsyncMock(
        side_effect=RuntimeError("secret db path")
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        raw_result = await tool.call(
            _make_run_context(), memory="异常测试", topic="__none__"
        )

    result = json.loads(raw_result)
    assert result == {"memorized": False, "error": "internal_error"}
    assert "secret db path" not in raw_result


@pytest.mark.asyncio
async def test_memory_memorize_tool_propagates_cancellation(
    memory_engine, memory_processor
):
    tool = MemoryMemorizeTool(
        context=Mock(),
        memory_engine=memory_engine,
        memory_processor=memory_processor,
    )
    memory_engine.add_canonical_memory = AsyncMock(side_effect=asyncio.CancelledError())

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id",
        new_callable=AsyncMock,
    ) as get_persona:
        get_persona.return_value = "persona_a"
        with pytest.raises(asyncio.CancelledError):
            await tool.call(
                _make_run_context(), memory="取消测试", topic="__none__"
            )


@pytest.mark.asyncio
async def test_memory_memorize_tool_passes_persona_reaction(
    memory_engine, memory_processor
):
    tool = MemoryMemorizeTool(
        context=Mock(),
        memory_engine=memory_engine,
        memory_processor=memory_processor,
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id",
        new_callable=AsyncMock,
        return_value="persona_a",
    ):
        await tool.call(
            _make_run_context(),
            memory="用户坦白最近压力很大",
            topic="__none__",
            persona_reaction={"emotion": "心疼", "thought": "我想记住多关心他"},
        )

    build_kwargs = memory_processor.build_explicit_memory_record.call_args.kwargs
    assert build_kwargs["persona_reaction"] == {
        "emotion": "心疼",
        "thought": "我想记住多关心他",
    }


@pytest.mark.asyncio
async def test_memory_memorize_tool_drops_invalid_persona_reaction(
    memory_engine, memory_processor
):
    tool = MemoryMemorizeTool(
        context=Mock(),
        memory_engine=memory_engine,
        memory_processor=memory_processor,
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id",
        new_callable=AsyncMock,
        return_value="persona_a",
    ):
        await tool.call(
            _make_run_context(),
            memory="用户希望记住插件行为",
            topic="__none__",
            persona_reaction={"emotion": "  ", "thought": ""},
        )
        await tool.call(
            _make_run_context(),
            memory="用户希望记住插件行为",
            topic="__none__",
            persona_reaction="不是字典",
        )

    build_kwargs = memory_processor.build_explicit_memory_record.call_args.kwargs
    assert build_kwargs["persona_reaction"] is None


@pytest.mark.asyncio
async def test_memory_memorize_tool_accepts_none_topic_case_insensitive(
    memory_engine, memory_processor
):
    tool = MemoryMemorizeTool(
        context=Mock(),
        memory_engine=memory_engine,
        memory_processor=memory_processor,
    )

    with patch(
        "astrbot_plugin_livingmemory.core.tools.memory_memorize_tool.get_persona_id",
        new_callable=AsyncMock,
        return_value="persona_a",
    ):
        raw_result = await tool.call(
            _make_run_context(), memory="记住这条", topic="__NONE__"
        )

    assert json.loads(raw_result)["memorized"] is True
    assert memory_processor.build_explicit_memory_record.call_args.kwargs[
        "topics"
    ] == []