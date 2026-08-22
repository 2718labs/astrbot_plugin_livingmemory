"""Focused S2 contract and fact-projection tests."""

import json
import pytest

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
    assert "source" not in fact
    assert "source_message_ids" not in fact


def test_fact_search_projection_excludes_persona_reaction():
    search_text = CanonicalMemoryStore.fact_search_text(
        {
            "fact": "张三周三要考科目二",
            "topics": ["驾考"],
            "participants": ["张三"],
            "persona_reaction": {
                "emotion": "担心",
                "thought": "希望她不要紧张",
            },
        }
    )

    assert "张三周三要考科目二" in search_text
    assert "驾考" in search_text
    assert "担心" not in search_text
    assert "希望她不要紧张" not in search_text


@pytest.mark.asyncio
async def test_mark_documents_deleted_updates_parent_and_fact_lifecycle(tmp_path):
    store = CanonicalMemoryStore(str(tmp_path / "canonical.db"), None, object())
    await store.initialize()
    try:
        now = 1.0
        await store.db.execute(
            """
            INSERT INTO memory_parents(
                parent_id, document_id, idempotency_key, scope, persona_id,
                source_json, overview, generation_version, fact_ids_json,
                status, created_at, updated_at
            ) VALUES ('p1', 7, 'i1', 's1', 'persona', ?, 'overview', 'v1', ?,
                      'archived', ?, ?)
            """,
            (json.dumps({"fingerprint": "src"}), json.dumps(["f1"]), now, now),
        )
        await store.db.execute(
            """
            INSERT INTO memory_facts(
                fact_id, parent_id, fact_json, search_text, scope, persona_id,
                importance, status, created_at, updated_at
            ) VALUES ('f1', 'p1', ?, 'fact', 's1', 'persona', 0.8,
                      'archived', ?, ?)
            """,
            (json.dumps({"fact_id": "f1", "fact": "fact"}), now, now),
        )
        await store.db.commit()

        changed = await store.mark_documents_deleted([7])
        parent = await (
            await store.db.execute(
                "SELECT status FROM memory_parents WHERE document_id = 7"
            )
        ).fetchone()
        fact = await (
            await store.db.execute(
                "SELECT status FROM memory_facts WHERE fact_id = 'f1'"
            )
        ).fetchone()

        assert changed == 1
        assert parent["status"] == "deleted"
        assert fact["status"] == "deleted"
    finally:
        await store.close()
