"""Focused S2 contract and fact-projection tests."""

from astrbot_plugin_livingmemory.core.processors.memory_processor import MemoryProcessor
from astrbot_plugin_livingmemory.storage.canonical_memory_store import (
    CanonicalMemoryStore,
)


def test_explicit_memory_uses_same_v3_fact_contract_and_stable_ids():
    processor = MemoryProcessor(llm_provider=object())

    first = processor.build_explicit_memory_record(
        memory="张三不喝加糖咖啡",
        source_scope="scope:user:1",
        topics=["咖啡偏好"],
        key_facts=["张三喝咖啡时不加糖"],
        participants=["张三"],
        importance=0.8,
    )
    second = processor.build_explicit_memory_record(
        memory="张三不喝加糖咖啡",
        source_scope="scope:user:1",
        topics=["咖啡偏好"],
        key_facts=["张三喝咖啡时不加糖"],
        participants=["张三"],
        importance=0.8,
    )

    assert first.metadata["memory_schema_version"] == "v3"
    assert first.metadata["parent_id"] == second.metadata["parent_id"]
    assert first.metadata["idempotency_key"] == second.metadata["idempotency_key"]
    assert first.metadata["key_facts"] == second.metadata["key_facts"]
    fact = first.metadata["key_facts"][0]
    assert fact["fact"] == "张三喝咖啡时不加糖"
    assert fact["topics"] == ["咖啡偏好"]
    assert fact["participants"] == ["张三"]
    assert fact["source"] == "user_explicit"


def test_fact_search_projection_excludes_persona_reaction():
    search_text = CanonicalMemoryStore.fact_search_text(
        {
            "fact": "张三周三要考科目二",
            "topics": ["驾考"],
            "participants": ["张三"],
            "time": {"normalized": "2026-08-26"},
            "persona_reaction": {
                "emotion": "担心",
                "thought": "希望她不要紧张",
            },
        }
    )

    assert "张三周三要考科目二" in search_text
    assert "驾考" in search_text
    assert "2026-08-26" in search_text
    assert "担心" not in search_text
    assert "希望她不要紧张" not in search_text
