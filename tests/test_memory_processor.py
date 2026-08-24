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
        candidate = {
            "fact": fact,
            "topics": list(topics or []),
            "importance": fact_importance,
        }
        # Used only to build an explicitly invalid legacy payload in format tests.
        if action == "skip":
            candidate["action"] = "skip"
        key_facts.append(candidate)
    memory = {"key_facts": key_facts}
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
    repair_prompt = llm.text_chat.await_args_list[1].kwargs["prompt"]
    assert "每条 memory 只包含 key_facts" in repair_prompt
    assert "source_indexes" not in repair_prompt


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
async def test_fragmented_output_is_compacted_once_before_storage():
    def _fact(index: int) -> dict:
        return {
            "fact": f"张三关于同一件事的过程片段 {index}",
            "topics": ["同一件事"],
            "importance": 0.5 + index / 100,
        }

    fragmented = json.dumps(
        {
            "memories": [
                {"key_facts": [_fact(index) for index in range(1, 4)]},
                {"key_facts": [_fact(index) for index in range(4, 8)]},
            ]
        },
        ensure_ascii=False,
    )
    compacted = json.dumps(
        {
            "memories": [
                {
                    "key_facts": [
                        {
                            "fact": "张三完整说明了同一件事的原因、发展和最终结果",
                            "topics": ["同一件事"],
                            "importance": 0.57,
                        }
                    ]
                }
            ]
        },
        ensure_ascii=False,
    )
    llm = _DummyLLMProvider([fragmented, compacted])
    processor = MemoryProcessor(llm_provider=llm, context=None)

    result = await processor.process_conversation_result(_make_messages())

    assert result.status == "store"
    assert result.stored_fact_count == 1
    assert result.metadata["key_facts"][0]["fact"] == (
        "张三完整说明了同一件事的原因、发展和最终结果"
    )
    assert llm.text_chat.await_count == 2
    repair_prompt = llm.text_chat.await_args_list[1].kwargs["prompt"]
    assert "总 fact 最多 5 条" in repair_prompt
    assert "合并同一事件" in repair_prompt
    assert "不得新增事实" in repair_prompt


def test_original_five_fact_capacity_is_preserved_across_multiple_memories():
    processor = MemoryProcessor(llm_provider=Mock(), context=None)

    def _fact(index: int) -> dict:
        return {
            "fact": f"可独立接续的事实 {index}",
            "topics": [f"主题 {index}"],
            "importance": 0.7,
        }

    payload = {
        "memories": [
            {"key_facts": [_fact(1), _fact(2)]},
            {"key_facts": [_fact(3), _fact(4), _fact(5)]},
        ]
    }

    parsed = processor._parse_llm_response(
        json.dumps(payload, ensure_ascii=False), False
    )

    assert sum(len(unit["key_facts"]) for unit in parsed["memories"]) == 5


@pytest.mark.asyncio
async def test_fragmented_output_is_invalid_when_compaction_still_exceeds_limit():
    facts = [
        {
            "fact": f"张三关于同一件事的过程片段 {index}",
            "topics": ["同一件事"],
            "importance": 0.6,
        }
        for index in range(1, 8)
    ]
    fragmented = json.dumps(
        {"memories": [{"key_facts": facts[:3]}, {"key_facts": facts[3:]}]},
        ensure_ascii=False,
    )
    llm = _DummyLLMProvider([fragmented, fragmented])
    processor = MemoryProcessor(llm_provider=llm, context=None)

    result = await processor.process_conversation_result(_make_messages())

    assert result.status == "invalid"
    assert (result.error or "").startswith("输出过碎：")
    assert llm.text_chat.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("importance", [0.0, 0.2])
async def test_importance_is_metadata_not_a_storage_threshold(importance):
    llm = _DummyLLMProvider(
        _memory_json(
            ("张三发了一个表情", "store", importance),
            topics=["闲聊"],
        )
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)

    result = await processor.process_conversation_result(_make_messages())

    assert result.status == "store"
    assert result.stored_fact_count == 1
    assert result.importance == importance
    assert llm.text_chat.await_count == 1


@pytest.mark.asyncio
async def test_legacy_action_skip_is_not_part_of_current_contract():
    legacy = _memory_json(("张三刚才说有点饿", "skip", 0.4))
    llm = _DummyLLMProvider([legacy, legacy])
    processor = MemoryProcessor(llm_provider=llm, context=None)

    result = await processor.process_conversation_result(_make_messages())

    assert result.status == "invalid"
    assert "不得包含字段: action" in (result.error or "")
    assert llm.text_chat.await_count == 2


def test_strict_format_gate_accepts_complete_fence_and_rejects_missing_importance():
    processor = MemoryProcessor(llm_provider=Mock(), context=None)
    valid = _memory_json("张三周三参加科目二考试", topics=["驾考"])

    parsed = processor._parse_llm_response(f"```json\n{valid}\n```", False)

    assert parsed["memories"][0]["key_facts"][0]["fact"] == "张三周三参加科目二考试"
    invalid = json.loads(valid)
    del invalid["memories"][0]["key_facts"][0]["importance"]
    with pytest.raises(ValueError, match="importance"):
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
    # S4: atom generation is retired; the pure classifier helper still works
    # when explicitly enabled (component-level coverage only).
    processor = MemoryProcessor(context=None, config={"atom_enabled": True})
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
    assert llm.text_chat.await_count == 1


@pytest.mark.asyncio
async def test_zero_character_llm_response_retries_and_can_recover(monkeypatch):
    llm = _DummyLLMProvider(
        [
            "",
            _memory_json(
                ("张三明确要求以后不要反复解释这个称呼", "store", 0.8),
                topics=["互动边界"],
            ),
        ]
    )
    processor = MemoryProcessor(llm_provider=llm, context=None)
    sleep = AsyncMock()
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.processors.memory_processor.asyncio.sleep",
        sleep,
    )

    result = await processor.process_conversation_result(
        messages=_make_messages(),
        is_group_chat=False,
        persona_id=None,
    )

    assert result.status == "store"
    assert result.stored_fact_count == 1
    assert llm.text_chat.await_count == 2
    assert llm.text_chat.await_args_list[0].kwargs == llm.text_chat.await_args_list[1].kwargs
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_zero_character_llm_response_raises_after_retry_limit(monkeypatch):
    llm = _DummyLLMProvider(["", "", ""])
    processor = MemoryProcessor(llm_provider=llm, context=None)
    sleep = AsyncMock()
    monkeypatch.setattr(
        "astrbot_plugin_livingmemory.core.processors.memory_processor.asyncio.sleep",
        sleep,
    )

    with pytest.raises(RuntimeError, match="0 字符"):
        await processor.process_conversation_result(
            messages=_make_messages(),
            is_group_chat=False,
            persona_id=None,
        )

    assert llm.text_chat.await_count == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fact",
    [
        "某用户说了话",
        "后来张三决定周三参加考试",
    ],
)
async def test_hardcoded_subject_phrases_do_not_reject_candidate(fact):
    llm = _DummyLLMProvider(
        _memory_json(
            (fact, "store", 0.5),
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

    assert result.status == "store"
    assert result.metadata["key_facts"][0]["fact"] == fact


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
            ("李四认为 ChatGPT 写代码效率提升了 30%", "store", 0.75),
            topics=["AI工具", "工作效率"],
            sentiment="positive",
            importance=0.75,
            participants=["李四"],
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
            ("李四认为 ChatGPT 写代码效率提升了 30%", "store", 0.8),
            topics=["AI工具", "工作效率"],
            sentiment="positive",
            participants=["李四"],
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
    assert "效率提升" in metadata["canonical_summary"]
    assert "效率提升" in content


@pytest.mark.asyncio
async def test_process_group_chat_derives_participants_from_named_speakers():
    payload = json.loads(
        _memory_json(
            ("张三确认参加周五会议", "store", 0.5),
            topics=["会议"],
            importance=0.5,
        )
    )
    llm = _DummyLLMProvider(json.dumps(payload, ensure_ascii=False))
    processor = MemoryProcessor(llm_provider=llm, context=None)

    result = await processor.process_conversation_result(
        messages=_make_group_messages(),
        is_group_chat=True,
        persona_id=None,
    )

    assert result.status == "store"
    assert result.metadata["key_facts"][0]["participants"] == ["张三"]
    assert llm.text_chat.await_count == 1


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
        if i == 0:
            content = "我提出采用新讨论方案"
        elif i == 1:
            content = "我确认负责整理结论"
        else:
            content = (
                f"成员{i % 5} 说：这是第 {i + 1} 条消息，内容比较详细，包含了很多信息。"
                * 3
            )
        long_messages.append(
            Message(
                id=i + 1,
                session_id="aiocqhttp:GroupMessage:99999",
                role="user",
                content=content,
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
async def test_s1_repeat_window_keeps_ids_stable_across_fact_paraphrase():
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
async def test_s1_absolute_date_and_reaction_are_bound_to_fact_without_time_object():
    messages = _make_messages()
    messages[0].content = "我下周三参加科目二考试"
    messages[0].timestamp = datetime(2026, 8, 20, 9, 0).timestamp()
    unit = _unit_from_json(
        _memory_json("张三将在2026年8月26日参加科目二考试", topics=["驾考"])
    )
    fact = unit["key_facts"][0]
    # A stale custom prompt may still emit the retired field. It is not part
    # of the authoritative contract and must not reach storage.
    fact["time"] = {
        "raw": "七声",
        "normalized": "2026-08-19",
        "precision": "exact",
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
    assert "source_message_ids" not in stored_fact
    assert "time" not in stored_fact
    assert stored_fact["fact"] == "张三将在2026年8月26日参加科目二考试"
    assert stored_fact["persona_reaction"] == fact["persona_reaction"]
    assert result.metadata["source_window"]["first_message_id"] == 1
    assert result.metadata["source_window"]["last_message_id"] == 2
    assert result.metadata["source_window"]["message_count"] == 2


@pytest.mark.asyncio
async def test_s1_message_timestamp_stays_source_metadata_without_fact_time():
    messages = _make_messages()
    messages[0].content = "记忆助手说宝是她的开机键"
    messages[0].timestamp = datetime(2026, 8, 19, 10, 8).timestamp()
    messages[1].timestamp = datetime(2026, 8, 19, 10, 9).timestamp()
    unit = _unit_from_json(
        _memory_json("记忆助手说宝是她的开机键", topics=["称呼习惯"])
    )
    processor = MemoryProcessor(
        llm_provider=_DummyLLMProvider(
            json.dumps({"memories": [unit]}, ensure_ascii=False)
        ),
        context=None,
    )

    result = await processor.process_conversation_result(messages)

    assert result.status == "store"
    assert "time" not in result.metadata["key_facts"][0]
    assert result.metadata["source_time_label"] == "2026-08-19"


@pytest.mark.asyncio
async def test_s1_retired_time_field_cannot_invalidate_an_otherwise_valid_fact():
    messages = _make_messages()
    messages[0].content = "记忆助手说宝是她的开机键"
    messages[0].timestamp = datetime(2026, 8, 19, 10, 8).timestamp()
    unit = _unit_from_json(
        _memory_json("记忆助手说宝是她的开机键", topics=["称呼习惯"])
    )
    unit["key_facts"][0]["time"] = {
        "raw": "2026-08-19 10:03:07",
        "normalized": "2026-08-19 10:03:07",
        "precision": "exact",
    }
    processor = MemoryProcessor(
        llm_provider=_DummyLLMProvider(
            json.dumps({"memories": [unit]}, ensure_ascii=False)
        ),
        context=None,
    )

    result = await processor.process_conversation_result(messages)

    assert result.status == "store"
    assert "time" not in result.metadata["key_facts"][0]


def test_s1_output_contract_rewrites_relative_time_inside_fact_text():
    contract = MemoryProcessor._build_admission_output_contract(False)

    assert "相对时间" in contract
    assert "改写为具体日期" in contract
    assert "不要把消息发送时间本身写成事实" in contract
    assert '"time"' not in contract


def test_s1_output_contract_keeps_bot_and_user_roles_distinct():
    contract = MemoryProcessor._build_admission_output_contract(False)

    assert "描述当前 Bot 自己时只用第一人称“我”" in contract
    assert "[Bot: ...] 不是用户" in contract
    assert "source_indexes" not in contract


def test_s1_output_contract_prevents_utterance_level_fragmentation():
    contract = MemoryProcessor._build_admission_output_contract(False)

    assert "每条最多 5 个 fact" in contract
    assert "整个窗口合计最多 5 个 fact" in contract
    assert "同一事件、同一段关系变化或同一结论的过程话语必须合并" in contract
    assert "不要为了覆盖每句话而拆成多条 fact" in contract
    assert "允许包含同一事件的原因、发展与结果" in contract


def test_s0_output_contract_keeps_agreements_as_ordinary_facts():
    contract = MemoryProcessor._build_admission_output_contract(False)

    assert "后面的明确否认、纠正、澄清或形成的约定" in contract
    assert "不得保存已被否认的版本" in contract
    assert "承诺、约定、边界和偏好按普通事实保存" in contract
    assert "短期定时任务不由记忆系统代办" in contract
    assert "未经对方确认的建议或旧约定，不得写入" in contract
    assert "几周或几个月后的另一场对话" not in contract
    assert "单次玩笑、昵称或亲昵称呼不自动成为稳定偏好" in contract
    assert "单次重要冲突、修复或共同意义仍可保存" in contract
    assert "Bot 复述的旧记忆" in contract


@pytest.mark.asyncio
async def test_legacy_fact_source_fields_are_discarded():
    unit = _unit_from_json(
        _memory_json("张三过去答应每天九点打卡", topics=["作息"])
    )
    fact = unit["key_facts"][0]
    fact["source"] = "inferred"
    fact["source_indexes"] = [2]
    processor = MemoryProcessor(
        llm_provider=_DummyLLMProvider(
            json.dumps({"memories": [unit]}, ensure_ascii=False)
        ),
        context=None,
    )

    result = await processor.process_conversation_result(_make_messages())

    assert result.status == "store"
    stored_fact = result.metadata["key_facts"][0]
    assert "source" not in stored_fact
    assert "source_message_ids" not in stored_fact


@pytest.mark.asyncio
async def test_bot_third_person_wording_does_not_reject_the_whole_window():
    messages = _make_messages()
    messages[0].sender_name = "张三"
    messages[0].content = "我周三参加科目二考试"
    messages[1].sender_name = "记忆助手"
    messages[1].content = "知道了"
    unit = _unit_from_json(
        _memory_json(
            "记忆助手周三参加科目二考试",
            topics=["驾考"],
            participants=["记忆助手"],
        )
    )
    processor = MemoryProcessor(
        llm_provider=_DummyLLMProvider(
            json.dumps({"memories": [unit]}, ensure_ascii=False)
        ),
        context=None,
    )

    result = await processor.process_conversation_result(messages)

    assert result.status == "store"
    assert result.metadata["key_facts"][0]["fact"] == "记忆助手周三参加科目二考试"


@pytest.mark.asyncio
async def test_bot_fact_reuses_first_person_persona_semantics():
    messages = [
        Message(
            id=1,
            session_id="s1",
            role="user",
            content="别再反复解释这个称呼了",
            sender_id="u1",
            sender_name="张三",
            platform="test",
            metadata={},
        ),
        Message(
            id=2,
            session_id="s1",
            role="assistant",
            content="我答应以后不再反复解释这个称呼",
            sender_id="2783785959",
            sender_name="2783785959",
            platform="test",
            metadata={"is_bot_message": True},
        ),
    ]
    payload = json.loads(
        _memory_json(
            ("我答应以后不再反复解释这个称呼", "store", 0.8),
            topics=["互动边界"],
            participants=[],
        )
    )
    llm = _DummyLLMProvider(json.dumps(payload, ensure_ascii=False))
    processor = MemoryProcessor(llm_provider=llm, context=None)

    result = await processor.process_conversation_result(
        messages=messages, persona_id="Angelica"
    )

    assert result.status == "store"
    stored_fact = result.metadata["key_facts"][0]
    assert stored_fact["fact"] == "我答应以后不再反复解释这个称呼"
    assert stored_fact["participant_refs"] == []


@pytest.mark.asyncio
async def test_bot_reply_can_bind_window_user_without_repeating_nickname():
    messages = [
        Message(
            id=1,
            session_id="s1",
            role="user",
            content="别再反复解释这个称呼了",
            sender_id="u1",
            sender_name="张三",
            platform="test",
            metadata={},
        ),
        Message(
            id=2,
            session_id="s1",
            role="assistant",
            content="行，以后不挂在嘴边解释",
            sender_id="bot",
            sender_name="Bot",
            platform="test",
            metadata={"is_bot_message": True},
        ),
    ]
    payload = json.loads(
        _memory_json(
            ("我答应张三以后不再反复解释这个称呼", "store", 0.8),
            topics=["互动边界"],
            participants=["张三"],
        )
    )
    processor = MemoryProcessor(
        llm_provider=_DummyLLMProvider(json.dumps(payload, ensure_ascii=False)),
        context=None,
    )

    result = await processor.process_conversation_result(messages=messages)

    assert result.status == "store"
    participant_ref = result.metadata["key_facts"][0]["participant_refs"][0]
    assert participant_ref["participant_id"] == "test:u1"
    assert participant_ref["source"] == "message_sender"


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
    # S4: atom generation is retired; the pure classifier helper still works
    # when explicitly enabled (component-level coverage only).
    processor = MemoryProcessor(context=None, config={"atom_enabled": True})
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


@pytest.mark.asyncio
async def test_machine_id_topic_name_falls_back_to_candidate_human_name():
    """LLM 把 topic_id 当名字输出时，应回退到候选池里对应的人话名字。"""
    unit = _unit_from_json(
        _memory_json(
            "张三正在开发记忆插件",
            topics=["topic_15be93ff9c30f0e9b16c659e"],
        )
    )
    processor = MemoryProcessor(
        llm_provider=_DummyLLMProvider(
            json.dumps({"memories": [unit]}, ensure_ascii=False)
        ),
        context=None,
    )

    result = await processor.process_conversation_result(
        _make_messages(),
        topic_candidates=[
            {
                "topic_id": "topic_15be93ff9c30f0e9b16c659e",
                "name": "记忆恢复",
            }
        ],
    )

    assert result.status == "store"
    topic_ref = result.metadata["key_facts"][0]["topic_refs"][0]
    assert topic_ref["name"] == "记忆恢复"
    assert topic_ref["topic_id"] == "topic_15be93ff9c30f0e9b16c659e"
    assert topic_ref["decision"] == "reused"
    # topics 数组不应再含机器 ID
    assert result.metadata["key_facts"][0]["topics"] == ["记忆恢复"]


@pytest.mark.asyncio
async def test_machine_id_topic_name_dropped_when_no_candidate():
    """机器 ID 主题名且候选池无对应项时，该主题被丢弃而非入库。"""
    unit = _unit_from_json(
        _memory_json(
            "张三正在开发记忆插件",
            topics=["topic_15be93ff9c30f0e9b16c659e"],
        )
    )
    processor = MemoryProcessor(
        llm_provider=_DummyLLMProvider(
            json.dumps({"memories": [unit]}, ensure_ascii=False)
        ),
        context=None,
    )

    result = await processor.process_conversation_result(
        _make_messages(),
        topic_candidates=[
            {"topic_id": "topic_other", "name": "插件开发"},
        ],
    )

    assert result.status == "store"
    key_fact = result.metadata["key_facts"][0]
    assert key_fact["topic_refs"] == []
    assert key_fact["topics"] == []


@pytest.mark.asyncio
async def test_topic_candidates_prompt_hides_machine_ids():
    """prompt 中的 topic 候选只含名字，不暴露 topic_id。"""
    captured: dict[str, str] = {}

    class _CapturingProvider(_DummyLLMProvider):
        async def _chat(self, prompt: str, system_prompt: str):
            captured["prompt"] = prompt
            return await super()._chat(prompt, system_prompt)

    unit = _unit_from_json(_memory_json("张三正在开发记忆插件", topics=["插件开发"]))
    processor = MemoryProcessor(
        llm_provider=_CapturingProvider(
            json.dumps({"memories": [unit]}, ensure_ascii=False)
        ),
        context=None,
    )

    await processor.process_conversation_result(
        _make_messages(),
        topic_candidates=[
            {"topic_id": "topic_secret_id", "name": "插件开发"},
        ],
    )

    assert "topic_secret_id" not in captured["prompt"]
    assert '"插件开发"' in captured["prompt"]
