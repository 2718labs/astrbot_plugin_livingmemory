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
        candidate_participants = [
            name for name in (participants or []) if name in fact
        ]
        if not candidate_participants and "张三" in fact:
            candidate_participants = ["张三"]
        key_facts.append(
            {
                "fact": fact,
                "action": action,
                "topics": list(topics or []),
                "participants": candidate_participants,
                "time": None,
                "importance": fact_importance,
                "source": "user_explicit",
                "source_indexes": [1],
                "persona_reaction": None,
            }
        )
    memory = {
        "summary": summary,
        "topics": topics or [],
        "key_facts": key_facts,
        "sentiment": sentiment,
        "importance": importance,
    }
    if canonical_summary is not None:
        memory["canonical_summary"] = canonical_summary
    return json.dumps({"memories": [memory]}, ensure_ascii=False)


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
    assert [item["fact"] for item in result.metadata["key_facts"]] == [
        "张三周三参加科目二考试"
    ]
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

    assert parsed["memories"][0]["key_facts"][0]["action"] == "store"
    invalid = json.loads(valid)
    del invalid["memories"][0]["key_facts"][0]["action"]
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
    assert [item["fact"] for item in result.metadata["key_facts"]] == [
        "张三明天下午三点开会"
    ]
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

    assert metadata["canonical_summary"] == "明天下午三点开会"
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
    assert metadata["summary"] == "张三明天下午三点参加会议"
    assert "persona_summary" not in metadata


@pytest.mark.asyncio
async def test_empty_candidate_list_is_a_valid_skip():
    llm = _DummyLLMProvider(json.dumps({"memories": []}, ensure_ascii=False))
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
    assert metadata["participants"] == ["张三"]


@pytest.mark.asyncio
async def test_process_group_chat_uses_neutral_derived_summary():
    """群聊 v3 只保留由 facts 派生的中性 summary。"""
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
    assert "persona_summary" not in metadata
    assert metadata.get("summary_schema_version") == "v3"
    assert "私有化 LLM" in metadata["canonical_summary"]
    assert "私有化 LLM" in content
    assert "数据安全" in content


@pytest.mark.asyncio
async def test_process_group_chat_missing_participants_is_invalid():
    payload = json.loads(
        _memory_json(
            ("张三确认参加周五会议", "store", 0.5),
            topics=["会议"],
            importance=0.5,
        )
    )
    del payload["memories"][0]["key_facts"][0]["participants"]
    llm = _DummyLLMProvider(json.dumps(payload, ensure_ascii=False))
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
async def test_process_private_chat_keeps_fact_participant_binding():
    """私聊 v3 同样保留 fact 自身的 participant 绑定。"""
    llm = _DummyLLMProvider(
        _memory_json("张三明天下午三点开会", topics=["会议"])
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    _, metadata, _ = await processor.process_conversation(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert metadata["participants"] == ["张三"]
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
    assert metadata["participants"] == ["成员0", "成员1"]
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


# ── S1 v3 output contract ───────────────────────────────────────────────────


def _unit_from_json(value: str) -> dict:
    return json.loads(value)["memories"][0]


@pytest.mark.asyncio
async def test_s1_mixed_window_splits_into_multiple_single_center_records():
    driving = _unit_from_json(
        _memory_json(
            "张三周三参加科目二考试",
            topics=["驾考"],
            importance=0.8,
        )
    )
    music = _unit_from_json(
        _memory_json(
            "张三长期喜欢爵士乐",
            topics=["音乐偏好"],
            importance=0.7,
        )
    )
    llm = _DummyLLMProvider(
        json.dumps({"memories": [driving, music]}, ensure_ascii=False)
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    result = await processor.process_conversation_result(_make_messages())

    records = result.iter_records()
    assert result.status == "store"
    assert len(records) == 2
    assert [record.content for record in records] == [
        "张三周三参加科目二考试",
        "张三长期喜欢爵士乐",
    ]
    assert records[0].metadata["parent_id"] != records[1].metadata["parent_id"]
    assert records[0].metadata["idempotency_key"] != records[1].metadata["idempotency_key"]
    for record in records:
        fact = record.metadata["key_facts"][0]
        assert isinstance(fact, dict)
        assert fact["parent_id"] == record.metadata["parent_id"]
        assert fact["fact_id"].startswith("fact_")
        assert "action" not in fact
        assert "reason" not in fact
        assert "persona_summary" not in record.metadata


@pytest.mark.asyncio
async def test_s1_source_retry_keeps_parent_fact_and_idempotency_ids_stable():
    first = _unit_from_json(
        _memory_json("张三长期喜欢爵士乐", topics=["音乐偏好"])
    )
    second = json.loads(json.dumps(first, ensure_ascii=False))
    second["key_facts"][0]["fact"] = "爵士乐是张三长期稳定的音乐偏好"
    llm = _DummyLLMProvider(
        [
            json.dumps({"memories": [first]}, ensure_ascii=False),
            json.dumps({"memories": [second]}, ensure_ascii=False),
        ]
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)
    messages = _make_messages()

    first_result = await processor.process_conversation_result(messages)
    second_result = await processor.process_conversation_result(messages)

    first_meta = first_result.metadata
    second_meta = second_result.metadata
    assert first_meta["source_window"]["fingerprint"] == second_meta["source_window"]["fingerprint"]
    assert first_meta["parent_id"] == second_meta["parent_id"]
    assert first_meta["idempotency_key"] == second_meta["idempotency_key"]
    assert first_meta["key_facts"][0]["fact_id"] == second_meta["key_facts"][0]["fact_id"]


@pytest.mark.asyncio
async def test_s1_fact_source_time_and_reaction_are_bound_to_the_fact():
    messages = _make_messages()
    messages[0].content = "我周三参加科目二考试"
    messages[0].timestamp = datetime(2026, 8, 20, 9, 0).timestamp()
    unit = _unit_from_json(
        _memory_json("张三周三参加科目二考试", topics=["驾考"])
    )
    fact = unit["key_facts"][0]
    fact["time"] = {
        "raw": "周三",
        "normalized": "2026-08-26",
        "precision": "day",
    }
    fact["persona_reaction"] = {
        "emotion": "有些替他紧张",
        "thought": "希望他顺利通过",
    }
    llm = _DummyLLMProvider(
        json.dumps({"memories": [unit]}, ensure_ascii=False)
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    result = await processor.process_conversation_result(messages)

    stored_fact = result.metadata["key_facts"][0]
    assert stored_fact["source_message_ids"] == [1]
    assert stored_fact["time"] == fact["time"]
    assert stored_fact["persona_reaction"] == fact["persona_reaction"]
    assert result.metadata["source_window"]["first_message_id"] == 1
    assert result.metadata["source_window"]["last_message_id"] == 2
    assert result.metadata["source_window"]["message_count"] == 2


@pytest.mark.asyncio
async def test_s1_message_timestamp_stays_source_metadata_when_fact_has_no_time():
    messages = _make_messages()
    messages[0].content = "张三说宝是她的开机键"
    messages[0].timestamp = datetime(2026, 8, 19, 10, 8).timestamp()
    messages[1].timestamp = datetime(2026, 8, 19, 10, 9).timestamp()
    unit = _unit_from_json(
        _memory_json("张三说宝是她的开机键", topics=["称呼习惯"])
    )
    processor = MemoryProcessor(
        llm_provider=_DummyLLMProvider(
            json.dumps({"memories": [unit]}, ensure_ascii=False)
        ),
        context=None,
    )

    result = await processor.process_conversation_result(messages)

    assert result.status == "store"
    assert result.metadata["key_facts"][0]["time"] is None
    assert result.metadata["source_time_label"] == "2026-08-19"


@pytest.mark.asyncio
async def test_s1_message_timestamp_cannot_be_copied_into_fact_time():
    messages = _make_messages()
    messages[0].content = "张三说宝是她的开机键"
    messages[0].timestamp = datetime(2026, 8, 19, 10, 8).timestamp()
    unit = _unit_from_json(
        _memory_json("张三说宝是她的开机键", topics=["称呼习惯"])
    )
    unit["key_facts"][0]["time"] = {
        "raw": "2026-08-19",
        "normalized": "2026-08-19",
        "precision": "day",
    }
    processor = MemoryProcessor(
        llm_provider=_DummyLLMProvider(
            json.dumps({"memories": [unit]}, ensure_ascii=False)
        ),
        context=None,
    )

    result = await processor.process_conversation_result(messages)

    assert result.status == "invalid"
    assert "cited message body" in (result.error or "")
    assert "message timestamp" in (result.error or "")


@pytest.mark.asyncio
async def test_s1_mismatched_relative_time_is_saved_unverified():
    # 换算分歧不再拒绝整条记忆：时间保留模型结果并标记 unverified
    messages = _make_messages()
    messages[0].content = "我周三参加科目二考试"
    messages[0].timestamp = datetime(2026, 8, 20, 9, 0).timestamp()
    unit = _unit_from_json(
        _memory_json("张三周三参加科目二考试", topics=["驾考"])
    )
    unit["key_facts"][0]["time"] = {
        "raw": "周三",
        "normalized": "2026-08-27",
        "precision": "day",
    }
    response = json.dumps({"memories": [unit]}, ensure_ascii=False)
    processor = MemoryProcessor(
        llm_provider=_DummyLLMProvider(response), context=None
    )

    result = await processor.process_conversation_result(messages)

    assert result.status == "store"
    stored_time = result.metadata["key_facts"][0]["time"]
    assert stored_time["normalized"] == "2026-08-27"
    assert stored_time["unverified"] is True


@pytest.mark.asyncio
async def test_s1_relative_time_uses_the_message_that_contains_raw():
    # 跨天窗口：raw 出现在第二条消息，基准必须取第二条，不能误杀正确结果
    messages = _make_messages()
    messages[0].content = "前天我去爬了山"
    messages[0].timestamp = datetime(2026, 8, 18, 23, 50).timestamp()
    messages[1].content = "昨天报名了驾考"
    messages[1].timestamp = datetime(2026, 8, 19, 9, 0).timestamp()
    unit = _unit_from_json(
        _memory_json("张三昨天报名了驾考", topics=["驾考"])
    )
    unit["key_facts"][0]["source_indexes"] = [1, 2]
    unit["key_facts"][0]["time"] = {
        "raw": "昨天",
        "normalized": "2026-08-18",
        "precision": "day",
    }
    processor = MemoryProcessor(
        llm_provider=_DummyLLMProvider(
            json.dumps({"memories": [unit]}, ensure_ascii=False)
        ),
        context=None,
    )

    result = await processor.process_conversation_result(messages)

    assert result.status == "store"
    stored_time = result.metadata["key_facts"][0]["time"]
    assert stored_time["normalized"] == "2026-08-18"
    assert "unverified" not in stored_time


def test_s1_output_contract_distinguishes_fact_time_from_message_timestamp():
    contract = MemoryProcessor._build_admission_output_contract(False)

    assert "time.raw 必须逐字来自所引用消息正文" in contract
    assert "禁止复制消息头的发送时间" in contract


def test_s0_output_contract_uses_final_correction_and_future_reuse_value():
    contract = MemoryProcessor._build_admission_output_contract(False)

    assert "后面的明确否认、纠正、澄清或形成的约定" in contract
    assert "不得把已被否认的旧说法另存为事实" in contract
    assert "几周或几个月后的另一场对话" in contract
    assert "同一个玩笑在一个窗口内重复多次仍是一次性玩笑" in contract
    assert "明确的互动偏好、边界及已接受的未来约定" in contract
    assert "助手自己复述的旧记忆" in contract
    assert "单次喊昵称、使用亲昵称呼或做出某个动作不自动等于稳定偏好" in contract
    assert "真实发生过本身不等于值得长期保存" in contract


@pytest.mark.asyncio
async def test_s1_topic_candidate_is_reused_with_same_stable_id():
    unit = _unit_from_json(
        _memory_json("张三正在开发记忆插件", topics=["插件开发"])
    )
    processor = MemoryProcessor(
        llm_provider=_DummyLLMProvider(
            json.dumps({"memories": [unit]}, ensure_ascii=False)
        ),
        context=None,
    )

    result = await processor.process_conversation_result(
        _make_messages(),
        topic_candidates=[{"topic_id": "topic_existing", "name": "插件开发"}],
    )

    topic_ref = result.metadata["key_facts"][0]["topic_refs"][0]
    assert topic_ref == {
        "topic_id": "topic_existing",
        "raw_name": "插件开发",
        "name": "插件开发",
        "decision": "reused",
    }


def test_s1_atom_projection_uses_each_fact_own_entities():
    processor = MemoryProcessor(context=None)
    metadata = {
        "key_facts": [
            {
                "fact": "张三周五发布版本",
                "topics": ["发布"],
                "participants": ["张三"],
                "importance": 0.8,
                "fact_id": "fact_1",
                "parent_id": "memory_1",
            },
            {
                "fact": "李四长期喜欢爵士乐",
                "topics": ["音乐偏好"],
                "participants": ["李四"],
                "importance": 0.7,
                "fact_id": "fact_2",
                "parent_id": "memory_1",
            },
        ]
    }

    atoms = processor.classify_atoms_from_metadata(metadata)

    assert atoms[0].entities == ["发布", "张三"]
    assert atoms[1].entities == ["音乐偏好", "李四"]
    assert atoms[0].metadata["fact_id"] == "fact_1"
    assert atoms[1].metadata["fact_id"] == "fact_2"
