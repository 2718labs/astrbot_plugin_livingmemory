"""
Tests for MemoryProcessor.
"""

import json
import tempfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.core.processors.memory_processor import MemoryProcessor
from astrbot_plugin_livingmemory.core.prompts.prompt_manager import (
    get_prompt_manager,
    init_prompt_manager,
)


class _DummyLLMProvider:
    def __init__(self, completion_text: str | list[str]):
        self._completion_texts = (
            [completion_text] if isinstance(completion_text, str) else completion_text
        )
        self._response_index = 0
        self.text_chat = AsyncMock(side_effect=self._chat)

    async def _chat(self, prompt: str, system_prompt: str):
        index = min(self._response_index, len(self._completion_texts) - 1)
        self._response_index += 1
        return SimpleNamespace(completion_text=self._completion_texts[index])


def _memory_json(
    *facts: str | tuple[str, str, float],
    summary: str = "候选事实已提取",
    topics: list[str] | None = None,
    sentiment: str = "neutral",
    importance: float = 0.8,
    participants: list[str] | None = None,
    canonical_summary: str | None = None,
) -> str:
    key_facts = []
    for item in facts:
        fact, action, fact_importance = (
            item if isinstance(item, tuple) else (item, "store", importance)
        )
        key_facts.append(
            {"fact": fact, "action": action, "importance": fact_importance}
        )
    payload = {
        "summary": summary,
        "topics": topics or [],
        "key_facts": key_facts,
        "sentiment": sentiment,
        "importance": importance,
    }
    if participants is not None:
        payload["participants"] = participants
    if canonical_summary is not None:
        payload["canonical_summary"] = canonical_summary
    return json.dumps(payload, ensure_ascii=False)


def _make_messages():
    return [
        Message(
            id=1,
            session_id="s1",
            role="user",
            content="明天下午三点开会",
            sender_id="u1",
            sender_name="张三",
            group_id=None,
            platform="test",
            metadata={},
        ),
        Message(
            id=2,
            session_id="s1",
            role="assistant",
            content="收到，我会提醒你",
            sender_id="bot",
            sender_name="Bot",
            group_id=None,
            platform="test",
            metadata={"is_bot_message": True},
        ),
    ]


@pytest.mark.asyncio
async def test_process_conversation_success():
    llm = _DummyLLMProvider(
        _memory_json("张三明天下午三点开会", topics=["会议提醒"])
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    content, metadata, importance = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert "张三" in content
    assert metadata["interaction_type"] == "private_chat"
    assert "会议提醒" in metadata["topics"]
    assert importance == 0.8


@pytest.mark.asyncio
async def test_process_conversation_rejects_non_json_after_one_repair():
    llm = _DummyLLMProvider("summary=测试, importance=0.6")
    processor = MemoryProcessor(llm_provider=llm, context=None)

    result = await processor.process_conversation_result(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert result.status == "invalid"
    assert result.content == ""
    assert llm.text_chat.await_count == 2


@pytest.mark.asyncio
async def test_process_conversation_accepts_one_format_repair():
    repaired = _memory_json("张三周三参加科目二考试", topics=["驾考"])
    llm = _DummyLLMProvider(["not-json", repaired])
    processor = MemoryProcessor(llm_provider=llm, context=None)

    result = await processor.process_conversation_result(_make_messages())

    assert result.status == "store"
    assert result.metadata["key_facts"] == ["张三周三参加科目二考试"]
    assert llm.text_chat.await_count == 2


@pytest.mark.asyncio
async def test_all_skipped_and_low_importance_facts_make_valid_skip():
    llm = _DummyLLMProvider(
        _memory_json(
            ("张三刚才说有点饿", "skip", 0.4),
            ("张三发了一个表情", "store", 0.2),
            summary="",
            importance=0.4,
        )
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    result = await processor.process_conversation_result(_make_messages())

    assert result.status == "skip"
    assert result.stored_fact_count == 0
    assert result.skipped_fact_count == 2


def test_strict_format_gate_accepts_complete_fence_and_rejects_missing_action():
    processor = MemoryProcessor(llm_provider=Mock(), context=None)
    valid = _memory_json("张三周三参加科目二考试", topics=["驾考"])

    parsed = processor._parse_llm_response(f"```json\n{valid}\n```", False)

    assert parsed["key_facts"][0]["action"] == "store"
    invalid = json.loads(valid)
    del invalid["key_facts"][0]["action"]
    with pytest.raises(ValueError, match="action"):
        processor._parse_llm_response(json.dumps(invalid, ensure_ascii=False), False)

    conflicting = json.loads(valid)
    conflicting["memory_action"] = "skip"
    with pytest.raises(ValueError, match="memory_action"):
        processor._parse_llm_response(
            json.dumps(conflicting, ensure_ascii=False), False
        )


class TestPromptLiveReload:
    """验证 WebUI 保存后 MemoryProcessor 立即使用新 prompt（不依赖实例字段缓存）。"""

    def test_get_chat_prompt_reads_from_prompt_manager(self):
        custom_text = "自定义私聊 prompt 内容 [{conversation}]"
        with tempfile.TemporaryDirectory() as tmpdir:
            init_prompt_manager(tmpdir)
            get_prompt_manager().update_prompt("private_chat_prompt", custom_text)

            llm = _DummyLLMProvider("{}")
            processor = MemoryProcessor(llm_provider=llm, context=None)

            live = processor._get_chat_prompt(is_group_chat=False)
            assert live == custom_text

            # 清理：重置为默认，避免影响其他测试
            get_prompt_manager().reset_prompt("private_chat_prompt")

    def test_get_chat_prompt_returns_valid_content(self):
        llm = _DummyLLMProvider("{}")
        processor = MemoryProcessor(llm_provider=llm, context=None)
        live = processor._get_chat_prompt(is_group_chat=False)
        assert isinstance(live, str) and len(live) > 50
        assert "{conversation}" in live


@pytest.mark.asyncio
async def test_persona_prompt_is_included_when_available():
    llm = _DummyLLMProvider(
        """{
            "summary":"我愉快地记录了这次交流",
            "topics":["闲聊"],
            "key_facts":["用户问候"],
            "sentiment":"positive",
            "importance":0.5
        }"""
    )
    context = Mock()
    context.persona_manager = Mock()
    context.persona_manager.get_persona = AsyncMock(
        return_value=SimpleNamespace(system_prompt="你是活泼助手")
    )

    processor = MemoryProcessor(llm_provider=llm, context=context)

    system_prompt = await processor._build_system_prompt_with_persona("persona_1")
    assert "人格设定" in system_prompt
    assert "活泼助手" in system_prompt


# ── S0 admission and current-storage projection ───────────────────────────────


@pytest.mark.asyncio
async def test_mixed_fact_admission_excludes_skipped_text_from_storage():
    llm = _DummyLLMProvider(
        _memory_json(
            ("张三明天下午三点开会", "store", 0.8),
            ("张三刚才随口说有点饿", "skip", 0.3),
            summary="这段原始总结不应直接进入存储",
            topics=["会议提醒"],
        )
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    result = await processor.process_conversation_result(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert result.status == "store"
    assert result.stored_fact_count == 1
    assert result.skipped_fact_count == 1
    assert result.content == "张三明天下午三点开会"
    assert result.metadata["key_facts"] == ["张三明天下午三点开会"]
    assert "随口说有点饿" not in json.dumps(result.metadata, ensure_ascii=False)
    assert "这段原始总结" not in json.dumps(result.metadata, ensure_ascii=False)


@pytest.mark.asyncio
async def test_source_time_tags_come_from_message_timestamps_without_rewriting_summary():
    llm = _DummyLLMProvider(
        _memory_json("发布计划已确认", topics=["发布"])
    )
    messages = _make_messages()
    messages[0].timestamp = datetime(2025, 5, 1, 9, 0).timestamp()
    messages[1].timestamp = datetime(2025, 5, 2, 10, 0).timestamp()
    processor = MemoryProcessor(llm_provider=llm, context=None)

    content, metadata, _ = await processor.process_conversation(messages)

    assert content == "发布计划已确认"
    assert metadata["canonical_summary"] == "发布计划已确认"
    assert metadata["time_tags"] == ["2025-05-01", "2025-05-02"]
    assert metadata["source_time_label"] == "2025-05-01 - 2025-05-02"


def test_atom_classification_persists_parent_memory_types():
    processor = MemoryProcessor(context=None)
    metadata = {
        "key_facts": ["明天下午发布新版本", "用户喜欢爵士乐"],
        "topics": ["发布", "音乐"],
    }

    atoms = processor.classify_atoms_from_metadata(metadata)

    assert len(atoms) == 2
    assert metadata["atom_types"] == ["planned", "preference"]


@pytest.mark.asyncio
async def test_admitted_facts_form_current_canonical_projection():
    llm = _DummyLLMProvider(
        _memory_json(
            ("明天下午三点开会", "store", 0.7),
            ("张三需要准备PPT", "store", 0.6),
            summary="旧式第一人称总结",
            topics=["备忘"],
            importance=0.7,
        )
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    content, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert "明天下午三点开会" in metadata["canonical_summary"]
    assert "张三需要准备PPT" in metadata["canonical_summary"]
    assert "旧式第一人称总结" not in metadata["canonical_summary"]
    assert "明天下午三点开会" in content
    assert "张三需要准备PPT" in content


@pytest.mark.asyncio
async def test_summary_quality_normal_for_valid_response():
    """有效的 LLM 响应应标记为 summary_quality=normal。"""
    llm = _DummyLLMProvider(
        _memory_json("张三明天下午三点开会", topics=["会议"])
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    _, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert metadata.get("summary_quality") == "normal"


@pytest.mark.asyncio
async def test_empty_llm_summary_does_not_hide_an_admitted_fact():
    llm = _DummyLLMProvider(
        _memory_json(
            ("张三明天下午三点参加会议", "store", 0.5),
            summary="",
            topics=["会议"],
            importance=0.5,
        )
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    _, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert metadata.get("summary_quality") == "normal"
    assert metadata["persona_summary"] == "张三明天下午三点参加会议"


@pytest.mark.asyncio
async def test_empty_candidate_list_is_a_valid_skip():
    llm = _DummyLLMProvider(_memory_json(summary="", topics=[], importance=0.0))
    processor = MemoryProcessor(llm_provider=llm, context=None)

    result = await processor.process_conversation_result(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert result.status == "skip"
    assert result.stored_fact_count == 0


@pytest.mark.asyncio
async def test_generic_store_fact_is_invalid_instead_of_written():
    llm = _DummyLLMProvider(
        _memory_json(
            ("某用户说了话", "store", 0.5),
            topics=["闲聊"],
            importance=0.5,
        )
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    result = await processor.process_conversation_result(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert result.status == "invalid"
    assert result.content == ""


def test_validate_summary_quality_directly():
    """直接测试 _validate_summary_quality 的各种边界情况。"""
    from unittest.mock import MagicMock

    processor = MemoryProcessor(llm_provider=MagicMock(), context=None)

    # 正常情况
    assert (
        processor._validate_summary_quality(
            {
                "summary": "用户明确表示喜欢吃寿司",
                "key_facts": ["用户喜欢寿司"],
                "importance": 0.7,
            }
        )
        == "normal"
    )

    # summary 过短
    assert (
        processor._validate_summary_quality(
            {
                "summary": "短",
                "key_facts": ["fact"],
                "importance": 0.5,
            }
        )
        == "low"
    )

    # importance 超出范围
    assert (
        processor._validate_summary_quality(
            {
                "summary": "用户明确表示喜欢吃寿司",
                "key_facts": ["用户喜欢寿司"],
                "importance": 1.5,
            }
        )
        == "low"
    )

    # 泛化词检测
    assert (
        processor._validate_summary_quality(
            {
                "summary": "有人提到了一些事情",
                "key_facts": ["有人说话"],
                "importance": 0.5,
            }
        )
        == "low"
    )


def test_build_memory_from_structured_data_uses_standard_storage_format():
    processor = MemoryProcessor(llm_provider=Mock(), context=None)

    content, metadata, importance = processor.build_memory_from_structured_data(
        {
            "summary": "用户希望主动记忆工具复用自动总结格式",
            "topics": ["LivingMemory", "主动记忆"],
            "key_facts": ["主动记忆应复用 MemoryProcessor 格式化流程"],
            "sentiment": "neutral",
            "importance": 0.8,
        },
        is_group_chat=False,
        fallback_excerpt="fallback",
    )

    assert content == metadata["canonical_summary"]
    assert metadata["persona_summary"] == "用户希望主动记忆工具复用自动总结格式"
    assert metadata["topics"] == ["LivingMemory", "主动记忆"]
    assert metadata["key_facts"] == ["主动记忆应复用 MemoryProcessor 格式化流程"]
    assert metadata["sentiment"] == "neutral"
    assert metadata["interaction_type"] == "private_chat"
    assert metadata["summary_schema_version"] == "v2"
    assert metadata["summary_quality"] == "normal"
    assert importance == 0.8


def test_build_memory_from_structured_data_flags_low_quality_for_out_of_range_importance():
    """与自动总结路径一致：原始 importance 越界时应判为 low quality。"""
    processor = MemoryProcessor(llm_provider=Mock(), context=None)

    _, metadata, importance = processor.build_memory_from_structured_data(
        {
            "summary": "用户希望主动记忆工具复用自动总结格式",
            "topics": ["测试"],
            "key_facts": ["importance 越界"],
            "sentiment": "neutral",
            "importance": 1.5,
        },
        is_group_chat=False,
        fallback_excerpt="fallback",
    )

    assert metadata["summary_quality"] == "low"
    assert importance == 1.0


# ── 群聊路径测试 ──────────────────────────────────────────────────────────────


def _make_group_messages():
    """构造一组群聊消息（含 group_id）"""
    return [
        Message(
            id=1,
            session_id="aiocqhttp:GroupMessage:88888",
            role="user",
            content="大家觉得 AI 工具怎么样？",
            sender_id="10001",
            sender_name="张三",
            group_id="88888",
            platform="aiocqhttp",
            metadata={},
        ),
        Message(
            id=2,
            session_id="aiocqhttp:GroupMessage:88888",
            role="user",
            content="我觉得 ChatGPT 写代码效率提升了 30%",
            sender_id="10002",
            sender_name="李四",
            group_id="88888",
            platform="aiocqhttp",
            metadata={},
        ),
        Message(
            id=3,
            session_id="aiocqhttp:GroupMessage:88888",
            role="assistant",
            content="AI 工具确实能提升效率，但需要仔细审查生成的代码",
            sender_id="bot",
            sender_name="Bot",
            group_id="88888",
            platform="aiocqhttp",
            metadata={"is_bot_message": True},
        ),
    ]


@pytest.mark.asyncio
async def test_process_group_chat_sets_interaction_type():
    """群聊路径应将 interaction_type 设置为 group_chat。"""
    llm = _DummyLLMProvider(
        _memory_json(
            ("张三认为 ChatGPT 效率提升 30%", "store", 0.75),
            ("李四认为需要仔细审查 AI 生成代码", "store", 0.7),
            topics=["AI工具", "工作效率"],
            sentiment="positive",
            importance=0.75,
            participants=["张三", "李四"],
        )
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    content, metadata, importance = await processor.process_conversation(
        messages=_make_group_messages(),
        is_group_chat=True,
        persona_id=None,
    )

    assert metadata["interaction_type"] == "group_chat"
    assert importance == 0.75


@pytest.mark.asyncio
async def test_process_group_chat_extracts_participants():
    """群聊路径应正确提取 participants 字段。"""
    llm = _DummyLLMProvider(
        _memory_json(
            ("张三认为 ChatGPT 效率提升 30%", "store", 0.7),
            topics=["AI工具"],
            sentiment="positive",
            importance=0.7,
            participants=["张三", "李四", "王五"],
        )
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    _, metadata, _ = await processor.process_conversation(
        messages=_make_group_messages(),
        is_group_chat=True,
        persona_id=None,
    )

    assert "participants" in metadata
    assert "张三" in metadata["participants"]
    assert "李四" in metadata["participants"]
    assert "王五" in metadata["participants"]


@pytest.mark.asyncio
async def test_process_group_chat_dual_channel_summary():
    """群聊路径也应生成双通道摘要（canonical_summary + persona_summary）。"""
    llm = _DummyLLMProvider(
        _memory_json(
            "张三建议公司内部部署私有化 LLM",
            "李四提醒注意数据安全",
            topics=["AI工具", "数据安全"],
            sentiment="positive",
            participants=["张三", "李四"],
        )
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    content, metadata, _ = await processor.process_conversation(
        messages=_make_group_messages(),
        is_group_chat=True,
        persona_id=None,
    )

    assert "canonical_summary" in metadata
    assert "persona_summary" in metadata
    assert metadata.get("summary_schema_version") == "v2"
    # canonical_summary 应包含 key_facts
    assert "私有化 LLM" in metadata["canonical_summary"]
    assert "私有化 LLM" in content
    assert "数据安全" in content


@pytest.mark.asyncio
async def test_process_group_chat_missing_participants_is_invalid():
    llm = _DummyLLMProvider(
        _memory_json(
            ("张三确认参加周五会议", "store", 0.5),
            topics=["会议"],
            importance=0.5,
        )
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    result = await processor.process_conversation_result(
        messages=_make_group_messages(),
        is_group_chat=True,
        persona_id=None,
    )

    assert result.status == "invalid"
    assert "participants" in (result.error or "")
    assert llm.text_chat.await_count == 2


@pytest.mark.asyncio
async def test_process_private_chat_no_participants_field():
    """私聊路径不应在 metadata 中包含 participants 字段。"""
    llm = _DummyLLMProvider(
        _memory_json("张三明天下午三点开会", topics=["会议"])
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    _, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert "participants" not in metadata
    assert metadata["interaction_type"] == "private_chat"


@pytest.mark.asyncio
async def test_process_group_chat_long_content():
    """群聊长内容（多条消息）应正常处理，不崩溃。"""
    long_messages = []
    for i in range(20):
        long_messages.append(
            Message(
                id=i + 1,
                session_id="aiocqhttp:GroupMessage:99999",
                role="user",
                content=f"成员{i % 5} 说：这是第 {i + 1} 条消息，内容比较详细，包含了很多信息。"
                * 3,
                sender_id=str(10000 + i % 5),
                sender_name=f"成员{i % 5}",
                group_id="99999",
                platform="aiocqhttp",
                metadata={},
            )
        )

    llm = _DummyLLMProvider(
        _memory_json(
            ("成员0提出采用新讨论方案", "store", 0.6),
            ("成员1确认负责整理结论", "store", 0.6),
            topics=["群聊", "讨论"],
            importance=0.6,
            participants=["成员0", "成员1", "成员2", "成员3", "成员4"],
        )
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    content, metadata, importance = await processor.process_conversation(
        messages=long_messages,
        is_group_chat=True,
        persona_id=None,
    )

    assert isinstance(content, str) and len(content) > 0
    assert metadata["interaction_type"] == "group_chat"
    assert len(metadata["participants"]) == 5
    assert 0.0 <= importance <= 1.0


@pytest.mark.asyncio
async def test_process_group_chat_generic_store_fact_is_invalid():
    llm = _DummyLLMProvider(
        _memory_json(
            ("有人说话了", "store", 0.4),
            topics=["闲聊"],
            importance=0.4,
            participants=["某用户"],
        )
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    result = await processor.process_conversation_result(
        messages=_make_group_messages(),
        is_group_chat=True,
        persona_id=None,
    )

    assert result.status == "invalid"


def test_format_conversation_sanitizes_multimodal_private_message():
    processor = MemoryProcessor(llm_provider=None, context=None)
    message = Message(
        id=1,
        session_id="s1",
        role="user",
        content=[
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
            {"type": "text", "text": "这张图里有会议安排"},
        ],
        sender_id="u1",
        sender_name="张三",
        group_id=None,
        platform="test",
        metadata={},
    )

    formatted = processor._format_conversation([message])

    assert "这张图里有会议安排" in formatted
    assert "image_url" not in formatted
    assert "example.test" not in formatted


def test_format_conversation_uses_placeholder_for_image_only_group_message():
    processor = MemoryProcessor(llm_provider=None, context=None)
    message = Message(
        id=1,
        session_id="g1",
        role="user",
        content=[
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}
        ],
        sender_id="u1",
        sender_name="张三",
        group_id="group1",
        platform="test",
        metadata={},
    )

    formatted = processor._format_conversation([message])

    assert "张三" in formatted
    assert "[图片消息]" in formatted
    assert "image_url" not in formatted
