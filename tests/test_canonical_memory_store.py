"""Focused S2 contract and fact-projection tests."""

import json
import time

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


def test_explicit_memory_does_not_guess_between_same_named_identities():
    processor = MemoryProcessor(llm_provider=object())
    identities = [
        {
            "identity_key": "test:u1",
            "sender_id": "u1",
            "platform": "test",
            "display_name": "张三",
            "aliases": ["张三"],
            "is_bot": False,
        },
        {
            "identity_key": "test:bot",
            "sender_id": "bot",
            "platform": "test",
            "display_name": "张三",
            "aliases": ["张三"],
            "is_bot": True,
        },
    ]

    record = processor.build_explicit_memory_record(
        memory="张三正在开发五子棋",
        source_scope="scope:user:1",
        participants=["张三"],
        participant_identities=identities,
    )

    assert {
        identity["identity_key"]
        for identity in record.metadata["participant_identities"]
    } == {"test:u1", "test:bot"}
    assert record.metadata["participants"] == []
    assert record.metadata["key_facts"][0]["participants"] == []
    assert record.metadata["key_facts"][0]["participant_refs"] == []


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


@pytest.mark.asyncio
async def test_get_recent_parent_returns_newest_inside_window(tmp_path):
    """窗口内取最新一条 active 父记忆；窗口外与无记录返回 None。"""
    store = CanonicalMemoryStore(str(tmp_path / "recent.db"), None, object())
    await store.initialize()
    try:
        now = time.time()
        for index, (pid, age_days, status) in enumerate(
            [
                ("p-old", 10.0, "active"),
                ("p-recent", 1.5, "active"),
                ("p-archived", 0.1, "archived"),
            ],
            start=1,
        ):
            created = now - age_days * 86400
            await store.db.execute(
                """
                INSERT INTO memory_parents(
                    parent_id, document_id, idempotency_key, scope, persona_id,
                    source_json, overview, generation_version, fact_ids_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, 's1', 'persona', ?, ?, 'v1', ?,
                          ?, ?, ?)
                """,
                (
                    pid,
                    index,
                    f"i-{pid}",
                    json.dumps({"fingerprint": "src"}),
                    f"overview-{pid}",
                    json.dumps([f"f-{pid}"]),
                    status,
                    created,
                    now,
                ),
            )
        await store.db.commit()

        recent = await store.get_recent_parent(
            scope="s1", persona_id="persona", window_hours=48
        )
        assert recent is not None
        assert recent["parent_id"] == "p-recent"
        assert recent["overview"] == "overview-p-recent"
        assert recent["document_id"] == 2

        # 窗口外：p-old 是唯一 active 但超出 48h
        outside = await store.get_recent_parent(
            scope="s1", persona_id="persona", window_hours=24
        )
        assert outside is None

        # persona 不匹配
        other = await store.get_recent_parent(
            scope="s1", persona_id="other", window_hours=48
        )
        assert other is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_get_facts_by_parent_returns_only_active_facts(tmp_path):
    """按父记忆取 active facts，archived 不返回。"""
    store = CanonicalMemoryStore(str(tmp_path / "facts.db"), None, object())
    await store.initialize()
    try:
        now = time.time()
        await store.db.execute(
            """
            INSERT INTO memory_parents(
                parent_id, document_id, idempotency_key, scope, persona_id,
                source_json, overview, generation_version, fact_ids_json,
                status, created_at, updated_at
            ) VALUES ('p1', 1, 'i1', 's1', 'persona', ?, 'ov', 'v1', ?,
                      'active', ?, ?)
            """,
            (json.dumps({"fingerprint": "src"}), json.dumps(["f1", "f2"]), now, now),
        )
        for index, (fact_id, status) in enumerate([("f1", "active"), ("f2", "archived")]):
            await store.db.execute(
                """
                INSERT INTO memory_facts(
                    fact_id, parent_id, fact_json, search_text, scope, persona_id,
                    importance, status, created_at, updated_at
                ) VALUES (?, 'p1', ?, ?, 's1', 'persona', 0.8,
                          ?, ?, ?)
                """,
                (
                    fact_id,
                    json.dumps({"fact_id": fact_id, "fact": f"fact-{index}"}),
                    f"search-{index}",
                    status,
                    now,
                    now,
                ),
            )
        await store.db.commit()

        facts = await store.get_facts_by_parent("p1")
        assert len(facts) == 1
        assert facts[0]["fact_id"] == "f1"
        assert facts[0]["fact"] == "fact-0"
        assert facts[0]["importance"] == 0.8

        assert await store.get_facts_by_parent("no-such-parent") == []
    finally:
        await store.close()
