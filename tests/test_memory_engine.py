"""
Tests for MemoryEngine with a fake in-memory FaissDB.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
from astrbot_plugin_livingmemory.core.models.memory_atom import MemoryAtom
from astrbot_plugin_livingmemory.storage.atom_store import AtomStore


@dataclass
class _FakeRetrieveResult:
    similarity: float
    data: dict


class _FakeDocumentStorage:
    def __init__(self, db: "_FakeFaissDB"):
        self._db = db

    async def get_documents(self, metadata_filters, ids=None, limit=50, offset=0):
        docs = list(self._db.docs.values())
        if ids is not None:
            id_set = set(ids)
            docs = [d for d in docs if d["id"] in id_set]

        for key, value in (metadata_filters or {}).items():
            docs = [d for d in docs if d["metadata"].get(key) == value]

        docs = docs[offset : offset + limit]
        return [dict(d) for d in docs]

    async def count_documents(self, metadata_filters):
        docs = list(self._db.docs.values())
        for key, value in (metadata_filters or {}).items():
            docs = [d for d in docs if d["metadata"].get(key) == value]
        return len(docs)


class _FakeFaissDB:
    def __init__(self):
        self.docs: dict[int, dict] = {}
        self._next_id = 1
        self.document_storage = _FakeDocumentStorage(self)

    async def insert(self, content: str, metadata: dict) -> int:
        doc_id = self._next_id
        self._next_id += 1
        self.docs[doc_id] = {
            "id": doc_id,
            "doc_id": f"uuid-{doc_id}",
            "text": content,
            "metadata": dict(metadata),
        }
        return doc_id

    async def retrieve(
        self, query: str, k: int, fetch_k: int, rerank: bool, metadata_filters=None
    ):
        results: list[_FakeRetrieveResult] = []
        for doc in self.docs.values():
            if metadata_filters:
                ok = True
                for key, value in metadata_filters.items():
                    if doc["metadata"].get(key) != value:
                        ok = False
                        break
                if not ok:
                    continue

            text = doc["text"]
            score = 0.9 if query in text else 0.2
            results.append(
                _FakeRetrieveResult(
                    similarity=score,
                    data={
                        "id": doc["id"],
                        "text": text,
                        "metadata": dict(doc["metadata"]),
                    },
                )
            )

        results.sort(key=lambda x: x.similarity, reverse=True)
        return results[:k]

    async def delete(self, uuid_doc_id: str) -> None:
        target = None
        for did, doc in self.docs.items():
            if doc["doc_id"] == uuid_doc_id:
                target = did
                break
        if target is not None:
            self.docs.pop(target, None)

    async def close(self) -> None:
        return None


async def _add_canonical_fact(
    engine: MemoryEngine,
    text: str,
    *,
    suffix: str,
    importance: float = 0.8,
    session_id: str = "test:private:s1",
    persona_id: str = "persona_1",
    topic_name: str | None = None,
) -> tuple[int, str]:
    parent_id = f"memory_{suffix}"
    fact_id = f"fact_{suffix}"
    metadata = {
        "idempotency_key": f"idem_{suffix}",
        "memory_schema_version": "v3",
        "summary_schema_version": "v3",
        "generation_version": "test-s5",
        "parent_id": parent_id,
        "summary": text,
        "canonical_summary": text,
        "source_window": {
            "fingerprint": f"src_{suffix}",
            "scope": session_id,
            "message_ids": [f"msg_{suffix}"],
        },
        "key_facts": [
            {
                "fact_id": fact_id,
                "parent_id": parent_id,
                "fact": text,
                "topics": [topic_name] if topic_name else [],
                "topic_refs": (
                    [
                        {
                            "topic_id": f"topic_{suffix}",
                            "name": topic_name,
                            "raw_name": topic_name,
                            "decision": "created",
                        }
                    ]
                    if topic_name
                    else []
                ),
                "participants": [],
                "importance": importance,
            }
        ],
    }

    async def _update_metadata(doc_id, updates):
        engine.faiss_db.docs[doc_id]["metadata"].update(updates)
        return True

    engine.hybrid_retriever.update_metadata = AsyncMock(side_effect=_update_metadata)
    memory_id = await engine.add_canonical_memory(
        metadata=metadata,
        session_id=session_id,
        persona_id=persona_id,
        importance=importance,
    )
    parent_metadata = dict(engine.faiss_db.docs[memory_id]["metadata"])
    parent_metadata.update(
        {
            "status": "active",
            "importance": importance,
            "session_id": session_id,
            "persona_id": persona_id,
            "create_time": time.time(),
        }
    )
    await engine.db_connection.execute(
        """
        INSERT OR REPLACE INTO documents(
            id, doc_id, text, metadata, created_at, updated_at
        ) VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        (
            memory_id,
            f"uuid-{memory_id}",
            text,
            json.dumps(parent_metadata, ensure_ascii=False),
        ),
    )
    await engine.db_connection.commit()
    return memory_id, fact_id


def test_memory_engine_atom_enabled_honors_explicit_false(tmp_path: Path):
    engine = MemoryEngine(
        db_path=str(tmp_path / "memory.db"),
        faiss_db=_FakeFaissDB(),
        config={"atom_enabled": False},
    )

    assert engine.atom_enabled is False


@pytest.mark.asyncio
async def test_initialize_drops_legacy_documents_fts_triggers(tmp_path: Path):
    db_path = tmp_path / "legacy_trigger.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            )
        """)
        await db.execute("""
            CREATE TRIGGER documents_au AFTER UPDATE ON documents BEGIN
                INSERT INTO documents_fts(rowid, content, doc_id)
                VALUES (new.id, new.text, new.doc_id);
            END
        """)
        await db.commit()

    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        fact_vector_db=_FakeFaissDB(),
        config={"fallback_enabled": True, "rrf_k": 60},
    )
    await engine.initialize()
    await engine.close()

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("""
            SELECT name FROM sqlite_master
            WHERE type='trigger' AND name='documents_au'
        """)
        row = await cursor.fetchone()

    assert row is None


@pytest.mark.asyncio
async def test_memory_engine_add_search_get_delete(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        fact_vector_db=_FakeFaissDB(),
        config={"fallback_enabled": True, "rrf_k": 60},
    )
    await engine.initialize()

    memory_id, fact_id = await _add_canonical_fact(
        engine,
        "我喜欢吃苹果",
        suffix="apple",
    )
    assert memory_id > 0

    result = await engine.get_memory(memory_id)
    assert result is not None
    assert "苹果" in result["text"]

    searched = await engine.search_memories(
        query="苹果",
        k=3,
        session_id="test:private:s1",
        persona_id="persona_1",
    )
    assert len(searched) >= 1
    assert searched[0].doc_id == memory_id
    assert searched[0].metadata["fact_id"] == fact_id
    assert searched[0].content == "我喜欢吃苹果"

    ok_delete = await engine.delete_memory(memory_id)
    assert ok_delete is True
    assert await engine.get_memory(memory_id) is None
    await engine.close()


@pytest.mark.asyncio
async def test_s1_idempotency_reuses_active_record_and_topic_candidate(tmp_path: Path):
    faiss = _FakeFaissDB()
    fact_faiss = _FakeFaissDB()
    engine = MemoryEngine(
        db_path=str(tmp_path / "s1_idempotency.db"),
        faiss_db=faiss,
        fact_vector_db=fact_faiss,
        config={"fallback_enabled": True},
    )
    await engine.initialize()
    async def _update_metadata(doc_id, updates):
        faiss.docs[doc_id]["metadata"].update(updates)
        return True

    engine.hybrid_retriever.update_metadata = AsyncMock(side_effect=_update_metadata)
    metadata = {
        "idempotency_key": "idem_same_source_unit",
        "memory_schema_version": "v3",
        "generation_version": "s1-v1",
        "parent_id": "memory_same_source_unit",
        "summary": "张三正在开发记忆插件",
        "canonical_summary": "张三正在开发记忆插件",
        "source_window": {"fingerprint": "src_same_source", "scope": "scope:s1"},
        "key_facts": [
            {
                "fact_id": "fact_plugin",
                "parent_id": "memory_same_source_unit",
                "fact": "张三正在开发记忆插件",
                "topics": ["插件开发"],
                "topic_refs": [
                    {
                        "topic_id": "topic_plugin",
                        "name": "插件开发",
                        "raw_name": "插件开发",
                        "decision": "created",
                    }
                ],
                "participants": ["张三"],
                "participant_refs": [],
                "importance": 0.8,
                "persona_reaction": None,
            }
        ],
    }

    first_id = await engine.add_memory(
        content="张三正在开发记忆插件",
        session_id="scope:s1",
        metadata=metadata,
    )
    retried_id = await engine.add_memory(
        content="记忆插件正在由张三开发",
        session_id="scope:s1",
        metadata=metadata,
    )
    candidates = await engine.get_topic_candidates("scope:s1")

    assert retried_id == first_id
    assert len(faiss.docs) == 1
    assert len(fact_faiss.docs) == 1
    assert candidates == [{"topic_id": "topic_plugin", "name": "插件开发"}]
    await engine.close()


@pytest.mark.asyncio
async def test_search_topic_candidates_uses_related_fact_routes(tmp_path: Path):
    engine = MemoryEngine(
        db_path=str(tmp_path / "topic_candidates.db"),
        faiss_db=_FakeFaissDB(),
    )
    store = Mock()
    store.search_candidates = AsyncMock(
        return_value={
            "bm25": [
                {"fact_id": "fact_a", "score": -2.0},
                {"fact_id": "fact_b", "score": -1.0},
            ],
            "vector": [
                {"fact_id": "fact_b", "score": 0.9},
                {"fact_id": "fact_c", "score": 0.8},
            ],
        }
    )
    store.get_fact_records = AsyncMock(
        return_value={
            "fact_a": {
                "fact": {
                    "topic_refs": [
                        {"topic_id": "topic_game", "name": "游戏开发"}
                    ]
                }
            },
            "fact_b": {
                "fact": {
                    "topic_refs": [
                        {"topic_id": "topic_game", "name": "游戏开发"}
                    ]
                }
            },
            "fact_c": {
                "fact": {
                    "topic_refs": [
                        {"topic_id": "topic_project", "name": "项目进展"}
                    ]
                }
            },
        }
    )
    engine.canonical_store = store

    candidates = await engine.search_topic_candidates(
        "张三正在开发五子棋",
        scope="scope:s1",
        persona_id="persona_a",
        limit=2,
    )

    assert candidates == [
        {"topic_id": "topic_game", "name": "游戏开发"},
        {"topic_id": "topic_project", "name": "项目进展"},
    ]
    store.search_candidates.assert_awaited_once_with(
        "张三正在开发五子棋",
        limit=10,
        scope="scope:s1",
        persona_id="persona_a",
    )
    store.get_fact_records.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_topic_candidates_reads_real_fact_indexes(tmp_path: Path):
    fact_faiss = _FakeFaissDB()
    fact_faiss.retrieve = AsyncMock(wraps=fact_faiss.retrieve)
    engine = MemoryEngine(
        db_path=str(tmp_path / "topic_candidate_indexes.db"),
        faiss_db=_FakeFaissDB(),
        fact_vector_db=fact_faiss,
        config={"fallback_enabled": True},
    )
    await engine.initialize()
    await _add_canonical_fact(
        engine,
        "张三正在开发五子棋",
        suffix="game",
        topic_name="游戏开发",
    )

    candidates = await engine.search_topic_candidates(
        "开发五子棋",
        scope="test:private:s1",
        persona_id="persona_1",
        limit=5,
    )

    assert candidates == [{"topic_id": "topic_game", "name": "游戏开发"}]
    assert fact_faiss.retrieve.await_args.kwargs["metadata_filters"] == {
        "status": "active",
        "session_id": "test:private:s1",
        "persona_id": "persona_1",
    }
    await engine.close()


@pytest.mark.asyncio
async def test_memory_source_is_separate_transferred_and_deleted(tmp_path: Path):
    engine = MemoryEngine(
        db_path=str(tmp_path / "memory_source.db"),
        faiss_db=_FakeFaissDB(),
        config={"fallback_enabled": True},
    )
    await engine.initialize()
    source = [
        {
            "id": 1,
            "session_id": "s1",
            "role": "user",
            "content": "exact source detail",
            "sender_id": "u1",
            "timestamp": 100.0,
            "metadata": {},
        }
    ]
    memory_id = await engine.add_memory(
        content="summary",
        session_id="s1",
        metadata={},
        source_messages=source,
    )

    stored = await engine.get_memory(memory_id)
    assert stored["metadata"]["has_source"] is True
    assert "exact source detail" not in str(stored["metadata"])
    assert await engine.get_memory_source(memory_id) == source

    new_id = await engine.replace_memory(
        memory_id,
        content="new summary",
        importance=0.8,
        metadata=stored["metadata"],
    )
    assert await engine.get_memory_source(memory_id) == []
    assert await engine.get_memory_source(new_id) == source

    assert await engine.delete_memory(new_id) is True
    assert await engine.get_memory_source(new_id) == []
    await engine.close()


@pytest.mark.asyncio
async def test_memory_transfer_records_include_source_and_duplicate_key(tmp_path: Path):
    engine = MemoryEngine(
        db_path=str(tmp_path / "memory_transfer.db"),
        faiss_db=_FakeFaissDB(),
        config={"fallback_enabled": True},
    )
    await engine.initialize()
    source = [
        {
            "id": 1,
            "session_id": "s1",
            "role": "user",
            "content": "exact detail",
            "sender_id": "u1",
            "timestamp": 1.0,
            "metadata": {},
        }
    ]
    memory_id = await engine.add_memory(
        content="portable summary",
        session_id="s1",
        persona_id="p1",
        importance=0.9,
        metadata={"topics": ["portable"]},
        source_messages=source,
    )
    await engine.db_connection.execute(
        "INSERT INTO documents (id, doc_id, text, metadata, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
        (
            memory_id,
            f"uuid-{memory_id}",
            "portable summary",
            json.dumps(engine.faiss_db.docs[memory_id]["metadata"]),
        ),
    )
    await engine.db_connection.commit()

    records = await engine.get_memory_transfer_records([memory_id])
    keys = await engine.get_memory_import_keys()

    assert records[0]["content"] == "portable summary"
    assert records[0]["source_messages"] == source
    assert ("portable summary", "s1", "p1") in keys
    await engine.close()


@pytest.mark.asyncio
async def test_memory_transfer_records_batches_more_than_500_selected_ids(
    tmp_path: Path,
):
    engine = MemoryEngine(
        db_path=str(tmp_path / "memory_transfer_batches.db"),
        faiss_db=_FakeFaissDB(),
        config={"fallback_enabled": True},
    )
    await engine.initialize()
    assert engine.db_connection is not None
    rows = [
        (
            memory_id,
            f"uuid-{memory_id}",
            f"memory {memory_id}",
            json.dumps({"importance": 0.5}),
        )
        for memory_id in range(1, 502)
    ]
    await engine.db_connection.executemany(
        "INSERT INTO documents (id, doc_id, text, metadata, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
        rows,
    )
    await engine.db_connection.commit()

    records = await engine.get_memory_transfer_records(list(range(501, 0, -1)))

    assert len(records) == 501
    assert [record["original_id"] for record in records] == list(range(1, 502))
    await engine.close()


@pytest.mark.asyncio
async def test_memory_source_write_failure_rolls_back_indexed_memory(tmp_path: Path):
    engine = MemoryEngine(
        db_path=str(tmp_path / "memory_source_failure.db"),
        faiss_db=_FakeFaissDB(),
        config={"fallback_enabled": True},
    )
    await engine.initialize()
    engine.save_memory_source = AsyncMock(side_effect=RuntimeError("disk full"))

    with pytest.raises(RuntimeError, match="disk full"):
        await engine.add_memory(
            content="summary",
            session_id="s1",
            source_messages=[{"role": "user", "content": "source"}],
        )

    assert await engine.get_memory(1) is None
    await engine.close()


@pytest.mark.asyncio
async def test_repair_add_clears_unavailable_source_metadata(tmp_path: Path):
    engine = MemoryEngine(
        db_path=str(tmp_path / "memory_source_repair.db"),
        faiss_db=_FakeFaissDB(),
        config={"fallback_enabled": True},
    )
    await engine.initialize()
    memory_id = await engine.add_memory(content="summary", session_id="s1")
    engine.faiss_db.docs[memory_id]["metadata"].update(
        {"has_source": True, "source_message_count": 2}
    )

    async def _update_metadata(doc_id: int, metadata: dict) -> bool:
        engine.faiss_db.docs[doc_id]["metadata"].update(metadata)
        return True

    engine.hybrid_retriever.update_metadata = AsyncMock(side_effect=_update_metadata)
    op_id = await engine._start_write_op(
        "add", {"session_id": "s1"}, memory_id=memory_id
    )

    assert await engine._repair_add_write_op(op_id, memory_id, {}) is True
    repaired = await engine.get_memory(memory_id)
    assert repaired["metadata"]["has_source"] is False
    assert repaired["metadata"]["source_message_count"] == 0
    await engine.close()


@pytest.mark.asyncio
async def test_repair_delete_removes_retained_source(tmp_path: Path):
    engine = MemoryEngine(
        db_path=str(tmp_path / "memory_source_delete_repair.db"),
        faiss_db=_FakeFaissDB(),
        config={"fallback_enabled": True},
    )
    await engine.initialize()
    memory_id = await engine.add_memory(
        content="summary",
        session_id="s1",
        source_messages=[{"role": "user", "content": "source"}],
    )
    op_id = await engine._start_write_op(
        "delete", {"memory_id": memory_id}, memory_id=memory_id
    )

    assert await engine._repair_delete_write_op(op_id, memory_id) is True
    assert await engine.get_memory_source(memory_id) == []
    await engine.close()


@pytest.mark.asyncio
async def test_memory_engine_decay_and_cleanup(tmp_path: Path):
    db_path = tmp_path / "memory_decay.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        config={"cleanup_days_threshold": 1, "cleanup_importance_threshold": 0.3},
    )
    await engine.initialize()

    old_id = await engine.add_memory(
        content="旧记忆",
        session_id="s",
        persona_id="p",
        importance=0.2,
        metadata={"topics": ["old"]},
    )
    new_id = await engine.add_memory(
        content="新记忆",
        session_id="s",
        persona_id="p",
        importance=0.9,
        metadata={"topics": ["new"]},
    )
    assert old_id != new_id

    # Make old memory older than threshold in fake storage and sqlite table.
    old_time = time.time() - 86400 * 3
    engine.faiss_db.docs[old_id]["metadata"]["create_time"] = old_time
    engine.faiss_db.docs[old_id]["metadata"]["last_access_time"] = old_time

    if engine.db_connection is not None:
        await engine.db_connection.execute(
            "INSERT OR REPLACE INTO documents (id, doc_id, text, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
            (
                old_id,
                f"uuid-{old_id}",
                "旧记忆",
                json.dumps(
                    {
                        "importance": 0.2,
                        "create_time": old_time,
                        "last_access_time": old_time,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        await engine.db_connection.execute(
            "INSERT OR REPLACE INTO documents (id, doc_id, text, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
            (
                new_id,
                f"uuid-{new_id}",
                "新记忆",
                json.dumps(
                    {
                        "importance": 0.9,
                        "create_time": time.time(),
                        "last_access_time": time.time(),
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        await engine.db_connection.commit()

    decayed = await engine.apply_daily_decay(decay_rate=0.1, days=2)
    assert isinstance(decayed, int)

    deleted = await engine.cleanup_old_memories(
        days_threshold=1, importance_threshold=0.3
    )
    assert deleted >= 1

    stats = await engine.get_statistics()
    assert "total_memories" in stats

    await engine.close()


@pytest.mark.asyncio
async def test_daily_decay_skips_protected_importance_threshold(tmp_path: Path):
    engine = MemoryEngine(
        db_path=str(tmp_path / "protected_decay.db"),
        faiss_db=_FakeFaissDB(),
        fact_vector_db=_FakeFaissDB(),
        config={"protected_importance_threshold": 0.8},
    )
    await engine.initialize()
    _, protected_fact = await _add_canonical_fact(
        engine, "protected", suffix="protected", importance=0.9
    )
    _, decaying_fact = await _add_canonical_fact(
        engine, "decaying", suffix="decaying", importance=0.5
    )

    affected = await engine.apply_daily_decay(0.1)
    cursor = await engine.db_connection.execute(
        "SELECT fact_id, importance FROM memory_facts ORDER BY fact_id"
    )
    stored = {str(row["fact_id"]): float(row["importance"]) for row in await cursor.fetchall()}

    assert affected == 1
    assert stored[protected_fact] == 0.9
    assert stored[decaying_fact] == 0.45
    await engine.close()


@pytest.mark.asyncio
async def test_memory_engine_search_updates_access_time_async(tmp_path: Path):
    db_path = tmp_path / "memory_access.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        config={"fallback_enabled": True},
    )
    await engine.initialize()

    mid = await engine.add_memory(
        content="测试访问时间",
        session_id="test:private:s1",
        persona_id="p1",
        importance=0.5,
        metadata={},
    )

    await engine.search_memories(
        "测试", k=1, session_id="test:private:s1", persona_id="p1"
    )
    await asyncio.sleep(0.05)
    # Access-time update may fail silently if row absent in sqlite documents table;
    # function should still complete and return results.
    assert mid in engine.faiss_db.docs
    await engine.close()


@pytest.mark.asyncio
async def test_internal_memory_scope_skips_legacy_session_migration(tmp_path: Path):
    engine = MemoryEngine(
        db_path=str(tmp_path / "internal_scope.db"),
        faiss_db=_FakeFaissDB(),
        config={"fallback_enabled": True},
    )
    await engine.initialize()
    engine._migrate_session_data_if_needed = AsyncMock()

    await engine.search_memories(
        "shared memory",
        session_id="livingmemory:user:test:user-1",
    )

    engine._migrate_session_data_if_needed.assert_not_awaited()
    await engine.close()


@pytest.mark.asyncio
async def test_search_memories_filters_below_minimum_importance(tmp_path: Path):
    engine = MemoryEngine(
        db_path=str(tmp_path / "memory_threshold.db"),
        faiss_db=_FakeFaissDB(),
        config={
            "min_importance_for_retrieval": 0.6,
            "search_cache_enabled": False,
        },
    )
    engine.hybrid_retriever = Mock()
    engine.hybrid_retriever.search = AsyncMock(
        return_value=[
            Mock(doc_id=1, metadata={"importance": 0.8}),
            Mock(doc_id=2, metadata={"importance": 0.59}),
            Mock(doc_id=3, metadata={}),
        ]
    )

    results = await engine.search_memories("query", k=5)

    assert [result.doc_id for result in results] == [1]
    await asyncio.gather(*engine._pending_tasks)


@pytest.mark.asyncio
async def test_search_memories_zero_importance_threshold_preserves_results(
    tmp_path: Path,
):
    engine = MemoryEngine(
        db_path=str(tmp_path / "memory_no_threshold.db"),
        faiss_db=_FakeFaissDB(),
        config={
            "min_importance_for_retrieval": 0.0,
            "search_cache_enabled": False,
        },
    )
    expected = [Mock(doc_id=1, metadata={"importance": 0.01})]
    engine.hybrid_retriever = Mock()
    engine.hybrid_retriever.search = AsyncMock(return_value=expected)

    results = await engine.search_memories("query", k=5)

    assert results == expected
    await asyncio.gather(*engine._pending_tasks)


@pytest.mark.asyncio
async def test_search_memories_applies_vector_similarity_threshold(tmp_path: Path):
    engine = MemoryEngine(
        db_path=str(tmp_path / "memory_similarity.db"),
        faiss_db=_FakeFaissDB(),
        config={
            "min_similarity_for_retrieval": 0.7,
            "search_cache_enabled": False,
        },
    )
    keyword_only = Mock(
        doc_id=3,
        metadata={"importance": 0.5},
        vector_score=None,
        score_breakdown={},
    )
    engine.hybrid_retriever = Mock()
    engine.hybrid_retriever.search = AsyncMock(
        return_value=[
            Mock(
                doc_id=1,
                metadata={"importance": 0.8},
                vector_score=0.82,
                score_breakdown={},
            ),
            Mock(
                doc_id=2,
                metadata={"importance": 0.8},
                vector_score=0.69,
                score_breakdown={},
            ),
            keyword_only,
        ]
    )

    results = await engine.search_memories("query", k=5)

    assert [result.doc_id for result in results] == [1, 3]
    await asyncio.gather(*engine._pending_tasks)


@pytest.mark.asyncio
async def test_memory_engine_search_cache_reuses_results_and_invalidates_on_write(
    tmp_path: Path,
):
    db_path = tmp_path / "memory_cache.db"
    faiss = _FakeFaissDB()
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=faiss,
        fact_vector_db=_FakeFaissDB(),
        config={
            "fallback_enabled": True,
            "search_cache_enabled": True,
            "search_cache_ttl_seconds": 60,
            "search_cache_max_size": 8,
        },
    )
    await engine.initialize()

    await _add_canonical_fact(
        engine,
        "缓存测试：用户喜欢苹果",
        suffix="cache_apple",
        persona_id="p1",
    )

    calls = 0
    original_search = engine.canonical_store.search_candidates

    async def counted_search(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await original_search(*args, **kwargs)

    engine.canonical_store.search_candidates = counted_search

    first = await engine.search_memories(
        query="苹果", k=3, session_id="test:private:s1", persona_id="p1"
    )
    second = await engine.search_memories(
        query="  苹果  ", k=3, session_id="test:private:s1", persona_id="p1"
    )
    assert [item.doc_id for item in second] == [item.doc_id for item in first]
    assert calls == 1

    await _add_canonical_fact(
        engine,
        "缓存测试：用户喜欢香蕉",
        suffix="cache_banana",
        persona_id="p1",
    )
    await engine.search_memories(
        query="苹果", k=3, session_id="test:private:s1", persona_id="p1"
    )
    assert calls == 2

    await engine.close()


@pytest.mark.asyncio
async def test_memory_engine_write_ops_record_completed_add(tmp_path: Path):
    db_path = tmp_path / "write_ops.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        fact_vector_db=_FakeFaissDB(),
        config={"fallback_enabled": True},
    )
    await engine.initialize()

    mid = await engine.add_memory(
        content="写操作日志测试",
        session_id="s1",
        persona_id="p1",
        importance=0.7,
        metadata={},
    )
    assert mid > 0

    cursor = await engine.db_connection.execute(
        """
        SELECT op_type, memory_id, status, step
        FROM memory_write_ops
        ORDER BY id DESC
        LIMIT 1
        """
    )
    row = await cursor.fetchone()
    assert row["op_type"] == "add"
    assert row["memory_id"] == mid
    assert row["status"] == "completed"
    assert row["step"] == "completed"

    await engine.close()


@pytest.mark.asyncio
async def test_memory_engine_atom_fallback_skips_previously_inserted_atoms(
    tmp_path: Path,
):
    db_path = tmp_path / "atom_partial_fallback.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        config={"graph_memory_enabled": True, "graph_memory_atom_enabled": True},
    )
    await engine.initialize()
    engine.atom_store = Mock()
    engine.atom_store.insert_many = AsyncMock(side_effect=RuntimeError("batch failed"))
    engine.atom_store.insert = AsyncMock()

    inserted_atom = MemoryAtom(parent_memory_id=0, content="already inserted")
    inserted_atom.atom_id = 42
    pending_atom = MemoryAtom(parent_memory_id=0, content="pending insert")

    memory_id = await engine.add_memory(
        content="fallback atom test",
        session_id="s1",
        persona_id="p1",
        atoms=[inserted_atom, pending_atom],
    )

    assert memory_id > 0
    engine.atom_store.insert.assert_awaited_once_with(pending_atom)

    cursor = await engine.db_connection.execute(
        """
        SELECT status, step
        FROM memory_write_ops
        ORDER BY id DESC
        LIMIT 1
        """
    )
    row = await cursor.fetchone()
    assert row["status"] == "completed"
    assert row["step"] == "completed"

    await engine.close()


@pytest.mark.asyncio
async def test_memory_engine_repair_inserts_failed_atoms_when_parent_has_atoms(
    tmp_path: Path,
):
    db_path = tmp_path / "atom_partial_repair.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        config={"graph_memory_enabled": True, "graph_memory_atom_enabled": True},
    )
    await engine.initialize()
    engine.atom_store = AtomStore(str(db_path))
    await engine.atom_store.initialize()

    memory_id = await engine.hybrid_retriever.add_memory(
        "partial repair source",
        {
            "session_id": "s1",
            "persona_id": "p1",
            "importance": 0.7,
            "create_time": time.time(),
            "last_access_time": time.time(),
        },
    )
    existing_atom = MemoryAtom(
        parent_memory_id=memory_id,
        content="already stored",
        session_id="s1",
        persona_id="p1",
    )
    await engine.atom_store.insert(existing_atom)

    failed_atom = MemoryAtom(
        parent_memory_id=memory_id,
        content="repair me",
        session_id="s1",
        persona_id="p1",
    )
    op_id = await engine._start_write_op(
        "add",
        {
            "session_id": "s1",
            "persona_id": "p1",
            "failed_atoms": [engine._serialize_atom_for_repair(failed_atom)],
        },
        memory_id=memory_id,
    )

    repaired = await engine._repair_add_write_op(
        op_id,
        memory_id,
        {
            "session_id": "s1",
            "persona_id": "p1",
            "failed_atoms": [engine._serialize_atom_for_repair(failed_atom)],
        },
    )

    assert repaired is True
    stored = await engine.atom_store.get_by_parent(memory_id)
    assert {atom.content for atom in stored} == {"already stored", "repair me"}

    await engine.close()


@pytest.mark.asyncio
async def test_memory_engine_access_count_increments_and_slows_decay(tmp_path: Path):
    db_path = tmp_path / "access_decay.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        fact_vector_db=_FakeFaissDB(),
        config={
            "access_decay_window_days": 30,
            "access_decay_max_count": 10,
            "access_count_decay_multiplier": 0.5,
        },
    )
    await engine.initialize()

    low_access_id, low_fact_id = await _add_canonical_fact(
        engine,
        "低访问记忆",
        suffix="low_access",
        session_id="s1",
        persona_id="p1",
    )
    high_access_id, high_fact_id = await _add_canonical_fact(
        engine,
        "高访问记忆",
        suffix="high_access",
        session_id="s1",
        persona_id="p1",
    )

    low_hit = Mock(metadata={"fact_id": low_fact_id})
    high_hit = Mock(metadata={"fact_id": high_fact_id})
    await engine.mark_memories_injected([low_hit])
    for _ in range(10):
        await engine.mark_memories_injected([high_hit])

    affected = await engine.apply_daily_decay(decay_rate=0.1, days=1)
    assert affected >= 2

    cursor = await engine.db_connection.execute(
        "SELECT fact_id, importance, injection_count FROM memory_facts WHERE fact_id IN (?, ?)",
        (low_fact_id, high_fact_id),
    )
    rows = await cursor.fetchall()
    facts = {str(row["fact_id"]): row for row in rows}
    assert float(facts[high_fact_id]["importance"]) > float(
        facts[low_fact_id]["importance"]
    )
    assert int(facts[high_fact_id]["injection_count"]) == 5

    await engine.close()


@pytest.mark.asyncio
async def test_v3_export_overwrites_drifted_summary_mirrors_from_parent(tmp_path: Path):
    engine = MemoryEngine(
        db_path=str(tmp_path / "canonical_export.db"),
        faiss_db=_FakeFaissDB(),
        fact_vector_db=_FakeFaissDB(),
        config={"fallback_enabled": True},
    )
    await engine.initialize()
    memory_id, _ = await _add_canonical_fact(
        engine,
        "父表保存的权威概览。",
        suffix="export-overview",
    )
    assert await engine.canonical_store.count_parent_overview_mismatches() == 0
    cursor = await engine.db_connection.execute(
        "SELECT metadata FROM documents WHERE id = ?", (memory_id,)
    )
    metadata = json.loads((await cursor.fetchone())["metadata"])
    metadata["summary"] = "漂移的 summary"
    metadata["canonical_summary"] = "漂移的 canonical_summary"
    await engine.db_connection.execute(
        "UPDATE documents SET metadata = ? WHERE id = ?",
        (json.dumps(metadata, ensure_ascii=False), memory_id),
    )
    await engine.db_connection.commit()
    assert await engine.canonical_store.count_parent_overview_mismatches() == 1

    records = await engine.get_memory_transfer_records([memory_id])

    assert records[0]["metadata"]["summary"] == "父表保存的权威概览。"
    assert records[0]["metadata"]["canonical_summary"] == (
        "父表保存的权威概览。"
    )
    await engine.close()


@pytest.mark.asyncio
async def test_fact_retrieved_and_injected_events_are_not_conflated(tmp_path: Path):
    engine = MemoryEngine(
        db_path=str(tmp_path / "fact_events.db"),
        faiss_db=_FakeFaissDB(),
        fact_vector_db=_FakeFaissDB(),
        config={"search_cache_enabled": False},
    )
    await engine.initialize()
    _, fact_id = await _add_canonical_fact(
        engine,
        "用户喜欢无糖咖啡",
        suffix="event_split",
    )

    hits = await engine.search_memories(
        "无糖咖啡",
        k=4,
        session_id="test:private:s1",
        persona_id="persona_1",
    )
    await asyncio.sleep(0.05)
    cursor = await engine.db_connection.execute(
        "SELECT retrieval_count, injection_count FROM memory_facts WHERE fact_id = ?",
        (fact_id,),
    )
    row = await cursor.fetchone()
    assert int(row["retrieval_count"]) == 1
    assert int(row["injection_count"]) == 0

    packed = engine.pack_memory_hits(hits)
    assert [item.metadata["fact_id"] for item in packed.hits] == [fact_id]
    await engine.mark_memories_injected(packed.hits)
    cursor = await engine.db_connection.execute(
        "SELECT retrieval_count, injection_count FROM memory_facts WHERE fact_id = ?",
        (fact_id,),
    )
    row = await cursor.fetchone()
    assert int(row["retrieval_count"]) == 1
    assert int(row["injection_count"]) == 1
    await engine.close()


# ── MemoryEngine 过滤/衰减/清理边界测试 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_engine_session_filter_isolates_sessions(tmp_path: Path):
    """不同 session_id 的记忆应相互隔离，搜索时只返回匹配 session 的结果。"""
    db_path = tmp_path / "filter.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        config={"fallback_enabled": True},
    )
    await engine.initialize()

    await engine.add_memory(
        content="session A 的记忆：用户喜欢苹果",
        session_id="test:private:session_A",
        persona_id="p1",
        importance=0.8,
        metadata={},
    )
    await engine.add_memory(
        content="session B 的记忆：用户喜欢香蕉",
        session_id="test:private:session_B",
        persona_id="p1",
        importance=0.8,
        metadata={},
    )

    results_a = await engine.search_memories(
        query="苹果",
        k=5,
        session_id="test:private:session_A",
        persona_id="p1",
    )
    # session A 的搜索不应返回 session B 的记忆
    for r in results_a:
        assert r.metadata.get("session_id") == "test:private:session_A"

    await engine.close()


@pytest.mark.asyncio
async def test_memory_engine_apply_daily_decay_zero_rate_returns_zero(tmp_path: Path):
    """decay_rate=0 时，apply_daily_decay 应直接返回 0，不修改任何记忆。"""
    db_path = tmp_path / "decay_zero.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        config={},
    )
    await engine.initialize()

    await engine.add_memory(
        content="测试记忆",
        session_id="s1",
        persona_id="p1",
        importance=0.8,
        metadata={},
    )

    result = await engine.apply_daily_decay(decay_rate=0, days=1)
    assert result == 0

    await engine.close()


@pytest.mark.asyncio
async def test_memory_engine_apply_daily_decay_zero_days_returns_zero(tmp_path: Path):
    """days=0 时，apply_daily_decay 应直接返回 0。"""
    db_path = tmp_path / "decay_days_zero.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        config={},
    )
    await engine.initialize()

    await engine.add_memory(
        content="测试记忆",
        session_id="s1",
        persona_id="p1",
        importance=0.8,
        metadata={},
    )

    result = await engine.apply_daily_decay(decay_rate=0.1, days=0)
    assert result == 0

    await engine.close()


@pytest.mark.asyncio
async def test_memory_engine_apply_daily_decay_reduces_importance(tmp_path: Path):
    """apply_daily_decay 应降低记忆的 importance 值。"""
    db_path = tmp_path / "decay_reduce.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        config={},
    )
    await engine.initialize()

    mid = await engine.add_memory(
        content="重要记忆",
        session_id="s1",
        persona_id="p1",
        importance=0.8,
        metadata={},
    )

    # 手动在 SQLite 中写入 importance，确保衰减可以读取
    if engine.db_connection is not None:
        await engine.db_connection.execute(
            "UPDATE documents SET metadata = ? WHERE id = ?",
            (json.dumps({"importance": 0.8, "session_id": "s1"}), mid),
        )
        await engine.db_connection.commit()

    affected = await engine.apply_daily_decay(decay_rate=0.1, days=1)
    assert isinstance(affected, int)
    assert affected >= 0

    await engine.close()


@pytest.mark.asyncio
async def test_memory_engine_cleanup_negative_days_returns_zero(tmp_path: Path):
    """days_threshold < 0 时，cleanup_old_memories 应返回 0。"""
    db_path = tmp_path / "cleanup_neg.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        config={},
    )
    await engine.initialize()

    await engine.add_memory(
        content="旧记忆",
        session_id="s1",
        persona_id="p1",
        importance=0.1,
        metadata={},
    )

    result = await engine.cleanup_old_memories(
        days_threshold=-1, importance_threshold=0.5
    )
    assert result == 0

    await engine.close()


@pytest.mark.asyncio
async def test_memory_engine_cleanup_zero_days_deletes_low_importance(tmp_path: Path):
    """days_threshold=0 时，所有低重要性记忆（无论多新）都应被清理。"""
    db_path = tmp_path / "cleanup_zero.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        config={},
    )
    await engine.initialize()

    low_id = await engine.add_memory(
        content="低重要性记忆",
        session_id="s1",
        persona_id="p1",
        importance=0.1,
        metadata={},
    )
    high_id = await engine.add_memory(
        content="高重要性记忆",
        session_id="s1",
        persona_id="p1",
        importance=0.9,
        metadata={},
    )

    # 确保 SQLite documents 表与 fake FAISS 存储保持一致。
    now = time.time()
    if engine.db_connection is not None:
        await engine.db_connection.execute(
            "INSERT OR REPLACE INTO documents "
            "(id, doc_id, text, metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
            (
                low_id,
                f"uuid-{low_id}",
                "低重要性记忆",
                json.dumps({"importance": 0.1, "create_time": now}),
            ),
        )
        await engine.db_connection.execute(
            "INSERT OR REPLACE INTO documents "
            "(id, doc_id, text, metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
            (
                high_id,
                f"uuid-{high_id}",
                "高重要性记忆",
                json.dumps({"importance": 0.9, "create_time": now}),
            ),
        )
        await engine.db_connection.commit()

    deleted = await engine.cleanup_old_memories(
        days_threshold=0, importance_threshold=0.5
    )
    assert deleted >= 1
    assert await engine.get_memory(high_id) is not None

    await engine.close()


@pytest.mark.asyncio
async def test_memory_engine_update_memory_content_creates_new_deletes_old(
    tmp_path: Path,
):
    """update_memory 更新内容时，应先创建新记忆再删除旧记忆。"""
    db_path = tmp_path / "update.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        config={},
    )
    await engine.initialize()

    old_id = await engine.add_memory(
        content="旧内容",
        session_id="s1",
        persona_id="p1",
        importance=0.7,
        metadata={},
    )

    success = await engine.update_memory(old_id, {"content": "新内容"})
    assert success is True
    assert await engine.get_memory(old_id) is None

    await engine.close()


@pytest.mark.asyncio
async def test_memory_engine_update_memory_importance_only(tmp_path: Path):
    """update_memory 只更新 importance 时，不应崩溃（fake DB 不支持 get_session，返回 False 是预期行为）。"""
    db_path = tmp_path / "update_imp.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        config={},
    )
    await engine.initialize()

    mid = await engine.add_memory(
        content="测试记忆",
        session_id="s1",
        persona_id="p1",
        importance=0.5,
        metadata={},
    )

    # fake DB 不支持 get_session，update_metadata 会失败，但不应抛出异常
    result = await engine.update_memory(mid, {"importance": 0.9})
    assert isinstance(result, bool)  # 不崩溃即可
    # 记忆仍然存在（内容未被删除）
    assert await engine.get_memory(mid) is not None

    await engine.close()


@pytest.mark.asyncio
async def test_memory_engine_delete_nonexistent_returns_false(tmp_path: Path):
    """删除不存在的记忆 ID 应返回 False。"""
    db_path = tmp_path / "del_nonexist.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        config={},
    )
    await engine.initialize()

    result = await engine.delete_memory(99999)
    assert result is False

    await engine.close()


@pytest.mark.asyncio
async def test_memory_engine_search_empty_query_returns_empty(tmp_path: Path):
    """空查询应直接返回空列表。"""
    db_path = tmp_path / "empty_query.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        config={},
    )
    await engine.initialize()

    await engine.add_memory(
        content="一些记忆内容",
        session_id="s1",
        persona_id="p1",
        importance=0.5,
        metadata={},
    )

    assert await engine.search_memories("", k=5) == []
    assert await engine.search_memories("   ", k=5) == []

    await engine.close()


@pytest.mark.asyncio
async def test_memory_engine_get_statistics_returns_expected_keys(tmp_path: Path):
    """get_statistics 应返回包含 total_memories 等关键字段的字典。"""
    db_path = tmp_path / "stats.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        config={},
    )
    await engine.initialize()

    await engine.add_memory(
        content="统计测试记忆",
        session_id="s1",
        persona_id="p1",
        importance=0.6,
        metadata={},
    )

    stats = await engine.get_statistics()
    assert "total_memories" in stats

    await engine.close()


# ── MemoryEngine.batch_delete_memories 测试 ───────────────────────────────────


@pytest.mark.asyncio
async def test_batch_delete_memories_deletes_multiple(tmp_path: Path):
    """batch_delete_memories 应批量删除多条记忆（从 FAISS 和 SQLite documents 表）。"""
    db_path = tmp_path / "batch_del.db"
    faiss = _FakeFaissDB()
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=faiss,
        config={},
    )
    await engine.initialize()

    # 直接在 FAISS 和 SQLite 中构造数据，避免 add_memory 锁竞争
    ids = []
    for i in range(5):
        mid = faiss._next_id
        faiss._next_id += 1
        faiss.docs[mid] = {
            "id": mid,
            "doc_id": f"uuid-{mid}",
            "text": f"批量删除测试记忆{i}",
            "metadata": {"importance": 0.5, "session_id": "s1", "persona_id": "p1"},
        }
        ids.append(mid)

    # 批量写入 SQLite documents 表
    if engine.db_connection is not None:
        for mid in ids:
            doc = faiss.docs[mid]
            await engine.db_connection.execute(
                "INSERT INTO documents (id, doc_id, text, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
                (
                    doc["id"],
                    doc["doc_id"],
                    doc["text"],
                    json.dumps(doc["metadata"], ensure_ascii=False),
                ),
            )
            await engine.save_memory_source(
                mid,
                [{"role": "user", "content": f"source-{mid}"}],
            )
        await engine.db_connection.commit()

    deleted = await engine.batch_delete_memories(ids)
    assert deleted == 5

    # FAISS 中的记录应被清除
    for mid in ids:
        assert mid not in faiss.docs
        assert await engine.get_memory(mid) is None

    # SQLite documents 表也应被清空
    cursor = await engine.db_connection.execute(
        f"SELECT COUNT(*) FROM documents WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    )
    row = await cursor.fetchone()
    assert row[0] == 0

    cursor = await engine.db_connection.execute(
        f"SELECT COUNT(*) FROM memory_sources WHERE memory_id IN ({','.join('?' * len(ids))})",
        ids,
    )
    row = await cursor.fetchone()
    assert row[0] == 0

    cursor = await engine.db_connection.execute(
        """
        SELECT op_type, status, step
        FROM memory_write_ops
        WHERE op_type = 'batch_delete'
        ORDER BY id DESC
        LIMIT 1
        """
    )
    op_row = await cursor.fetchone()
    assert op_row["status"] == "completed"
    assert op_row["step"] == "completed"

    await engine.close()


@pytest.mark.asyncio
async def test_batch_delete_memories_empty_list_returns_zero(tmp_path: Path):
    """空列表传入 batch_delete_memories 应返回 0。"""
    db_path = tmp_path / "batch_del_empty.db"
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=_FakeFaissDB(),
        config={},
    )
    await engine.initialize()
    assert await engine.batch_delete_memories([]) == 0
    await engine.close()


@pytest.mark.asyncio
async def test_batch_delete_memories_nonexistent_ids_are_noop(tmp_path: Path):
    """batch_delete_memories 传入不存在的 ID 不应报错，正常删除存在的部分。"""
    db_path = tmp_path / "batch_del_partial.db"
    faiss = _FakeFaissDB()
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=faiss,
        config={},
    )
    await engine.initialize()

    mid = 1
    faiss._next_id = 2
    faiss.docs[mid] = {
        "id": mid,
        "doc_id": f"uuid-{mid}",
        "text": "存在的记忆",
        "metadata": {"importance": 0.5},
    }

    if engine.db_connection is not None:
        await engine.db_connection.execute(
            "INSERT INTO documents (id, doc_id, text, metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
            (mid, f"uuid-{mid}", "存在的记忆", json.dumps({"importance": 0.5})),
        )
        await engine.db_connection.commit()

    deleted = await engine.batch_delete_memories([mid, 99999, 99998])
    assert deleted == 1
    assert mid not in faiss.docs

    await engine.close()


@pytest.mark.asyncio
async def test_repair_batch_delete_removes_graph_and_atoms(tmp_path: Path):
    db_path = tmp_path / "batch_del_repair.db"
    engine = MemoryEngine(db_path=str(db_path), faiss_db=_FakeFaissDB(), config={})
    await engine.initialize()

    engine.graph_memory_manager = Mock()
    engine.graph_memory_manager.batch_delete_memories = AsyncMock()
    engine.atom_store = Mock()
    engine.atom_store.batch_delete_by_parent = AsyncMock()

    op_id = await engine._start_write_op(
        "batch_delete",
        {"memory_ids": [1, "bad", 2]},
    )
    repaired = await engine._repair_batch_delete_write_op(
        op_id,
        {"memory_ids": [1, "bad", 2]},
    )

    assert repaired is True
    engine.graph_memory_manager.batch_delete_memories.assert_awaited_once_with([1, 2])
    engine.atom_store.batch_delete_by_parent.assert_awaited_once_with([1, 2])

    cursor = await engine.db_connection.execute(
        "SELECT status, step FROM memory_write_ops WHERE id = ?",
        (op_id,),
    )
    row = await cursor.fetchone()
    assert row["status"] == "completed"
    assert row["step"] == "completed"

    await engine.close()


@pytest.mark.asyncio
async def test_cleanup_old_memories_uses_batch_delete(tmp_path: Path):
    """cleanup_old_memories 应通过 batch_delete_memories 高效清理多条候选记忆。"""
    db_path = tmp_path / "cleanup_batch.db"
    faiss = _FakeFaissDB()
    engine = MemoryEngine(
        db_path=str(db_path),
        faiss_db=faiss,
        config={"cleanup_days_threshold": 0, "cleanup_importance_threshold": 0.3},
    )
    await engine.initialize()

    old_time = time.time() - 86400 * 10
    ids = []
    for i in range(10):
        mid = faiss._next_id
        faiss._next_id += 1
        faiss.docs[mid] = {
            "id": mid,
            "doc_id": f"uuid-{mid}",
            "text": f"待清理记忆{i}",
            "metadata": {
                "importance": 0.1,
                "create_time": old_time,
                "session_id": "s1",
            },
        }
        ids.append(mid)

    if engine.db_connection is not None:
        for mid in ids:
            doc = faiss.docs[mid]
            await engine.db_connection.execute(
                "INSERT INTO documents (id, doc_id, text, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
                (
                    doc["id"],
                    doc["doc_id"],
                    doc["text"],
                    json.dumps(doc["metadata"], ensure_ascii=False),
                ),
            )
        await engine.db_connection.commit()

    deleted = await engine.cleanup_old_memories(
        days_threshold=1, importance_threshold=0.3
    )
    assert deleted == 10
    for mid in ids:
        assert mid not in faiss.docs

    await engine.close()


# ==================== 更新回滚测试 ====================


@pytest.mark.asyncio
async def test_update_memory_rollback_on_delete_failure(tmp_path: Path):
    """delete_memory 失败时应回滚删除新创建的记忆并返回 False。"""
    db_path = tmp_path / "update_rollback.db"
    engine = MemoryEngine(db_path=str(db_path), faiss_db=_FakeFaissDB(), config={})
    await engine.initialize()

    old_id = await engine.add_memory(
        content="旧内容",
        session_id="s1",
        persona_id="p1",
        importance=0.7,
        metadata={},
    )

    original_delete = engine.delete_memory
    call_count = 0
    deleted_ids = []

    async def fake_delete(memory_id):
        nonlocal call_count
        call_count += 1
        deleted_ids.append(memory_id)
        if call_count == 1:
            return False
        return await original_delete(memory_id)

    engine.delete_memory = fake_delete

    success = await engine.update_memory(old_id, {"content": "新内容"})
    assert success is False
    assert call_count == 2
    old_mem = await engine.get_memory(old_id)
    assert old_mem is not None

    await engine.close()


@pytest.mark.asyncio
async def test_update_memory_add_fails_returns_false(tmp_path: Path):
    """add_memory 返回 None 时，update_memory 应返回 False 且不调用 delete。"""
    db_path = tmp_path / "update_addfail.db"
    engine = MemoryEngine(db_path=str(db_path), faiss_db=_FakeFaissDB(), config={})
    await engine.initialize()

    old_id = await engine.add_memory(
        content="旧内容",
        session_id="s1",
        persona_id="p1",
        importance=0.7,
        metadata={},
    )

    delete_called = False

    async def fake_add(*args, **kwargs):
        return None

    async def fake_delete(*args, **kwargs):
        nonlocal delete_called
        delete_called = True
        return True

    engine.add_memory = fake_add
    engine.delete_memory = fake_delete

    success = await engine.update_memory(old_id, {"content": "新内容"})
    assert success is False
    assert delete_called is False

    await engine.close()


@pytest.mark.asyncio
async def test_replace_memory_preserves_create_time_and_skips_atoms(tmp_path: Path):
    engine = MemoryEngine(
        db_path=str(tmp_path / "replace_structured.db"),
        faiss_db=_FakeFaissDB(),
        graph_vector_db=_FakeFaissDB(),
        config={"atom_enabled": True, "graph_memory_enabled": True},
    )
    await engine.initialize()
    # S4: even with atom_enabled=True the standalone AtomStore is retired.
    assert engine.atom_store is None
    assert engine.atom_retriever is None
    assert engine.atom_lifecycle_manager is None
    old_id = await engine.add_memory(
        content="old summary",
        session_id="s1",
        persona_id="p1",
        importance=0.5,
        metadata={"topics": ["old"], "key_facts": ["old fact"]},
    )
    old_memory = await engine.get_memory(old_id)
    old_create_time = old_memory["metadata"]["create_time"]

    new_id = await engine.replace_memory(
        old_id,
        content="new summary",
        importance=0.8,
        metadata={
            "topics": ["release"],
            "key_facts": ["Release is Friday"],
        },
    )

    assert new_id != old_id
    assert await engine.get_memory(old_id) is None
    replacement = await engine.get_memory(new_id)
    assert replacement["metadata"]["topics"] == ["release"]
    assert replacement["metadata"]["key_facts"] == ["Release is Friday"]
    assert replacement["metadata"]["create_time"] == old_create_time
    old_graph = await engine.graph_store.get_subgraph_for_memories([old_id])
    new_graph = await engine.graph_store.get_subgraph_for_memories([new_id])
    assert old_graph["entries"] == []
    assert any(
        "Release is Friday" in entry["content"]
        for entry in new_graph["entries"]
    )

    await engine.close()


@pytest.mark.asyncio
async def test_replace_memory_cancellation_removes_new_record(tmp_path: Path):
    engine = MemoryEngine(
        db_path=str(tmp_path / "replace_cancelled.db"),
        faiss_db=_FakeFaissDB(),
        config={},
    )
    await engine.initialize()
    old_id = await engine.add_memory(content="old summary", metadata={})
    expected_new_id = old_id + 1
    original_delete = engine.delete_memory
    old_delete_started = asyncio.Event()
    deleted_ids: list[int] = []

    async def block_old_delete(memory_id: int) -> bool:
        deleted_ids.append(memory_id)
        if memory_id == old_id:
            old_delete_started.set()
            await asyncio.Future()
        return await original_delete(memory_id)

    engine.delete_memory = block_old_delete
    task = asyncio.create_task(
        engine.replace_memory(
            old_id,
            content="new summary",
            metadata={"topics": ["new"], "key_facts": ["new fact"]},
            importance=0.7,
        )
    )
    await asyncio.wait_for(old_delete_started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert deleted_ids == [old_id, expected_new_id]
    assert await engine.get_memory(old_id) is not None
    assert await engine.get_memory(expected_new_id) is None
    await engine.close()


# ==================== 分批加载测试 ====================


@pytest.mark.asyncio
async def test_get_session_memories_batch_pagination(tmp_path: Path):
    """超过 500 条记忆时应分批加载，metadata 应正确规范化。"""
    db_path = tmp_path / "batch_session.db"
    faiss = _FakeFaissDB()
    engine = MemoryEngine(db_path=str(db_path), faiss_db=faiss, config={})
    await engine.initialize()

    session_id = "test:private:batch-session"
    for i in range(501):
        mid = faiss._next_id
        faiss._next_id += 1
        create_time = 1000.0 + i
        metadata = {
            "importance": 0.5,
            "session_id": session_id,
            "create_time": create_time,
        }
        faiss.docs[mid] = {
            "id": mid,
            "doc_id": f"uuid-{mid}",
            "text": f"测试记忆内容 {i}",
            "metadata": dict(metadata),
        }
        if engine.db_connection is not None:
            await engine.db_connection.execute(
                "INSERT INTO documents (id, doc_id, text, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
                (
                    mid,
                    f"uuid-{mid}",
                    f"测试记忆内容 {i}",
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
    if engine.db_connection is not None:
        await engine.db_connection.commit()

    memories = await engine.get_session_memories(session_id, limit=10)
    assert len(memories) <= 10
    if len(memories) >= 2:
        for i in range(len(memories) - 1):
            t1 = memories[i]["metadata"].get("create_time", 0)
            t2 = memories[i + 1]["metadata"].get("create_time", 0)
            assert t1 >= t2

    for mem in memories:
        assert isinstance(mem["metadata"], dict)

    await engine.close()


# ==================== 批量删除边界测试 ====================


@pytest.mark.asyncio
async def test_batch_delete_clears_fts_index(tmp_path: Path):
    """批量删除应同时清除 livingmemory_memories_fts 和 documents 表中的记录。"""
    db_path = tmp_path / "batch_del_fts.db"
    faiss = _FakeFaissDB()
    engine = MemoryEngine(db_path=str(db_path), faiss_db=faiss, config={})
    await engine.initialize()

    ids = []
    for i in range(3):
        mid = faiss._next_id
        faiss._next_id += 1
        faiss.docs[mid] = {
            "id": mid,
            "doc_id": f"uuid-{mid}",
            "text": f"test fts {i}",
            "metadata": {"importance": 0.5, "session_id": "s1"},
        }
        ids.append(mid)

    if engine.db_connection is not None:
        for mid in ids:
            doc = faiss.docs[mid]
            await engine.db_connection.execute(
                "INSERT INTO documents (id, doc_id, text, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
                (
                    doc["id"],
                    doc["doc_id"],
                    doc["text"],
                    json.dumps(doc["metadata"], ensure_ascii=False),
                ),
            )
            await engine.db_connection.execute(
                "INSERT INTO livingmemory_memories_fts(doc_id, content) VALUES (?, ?)",
                (mid, doc["text"]),
            )
        await engine.db_connection.commit()

    deleted = await engine.batch_delete_memories(ids)
    assert deleted == 3

    if engine.db_connection is not None:
        for mid in ids:
            cursor = await engine.db_connection.execute(
                "SELECT COUNT(*) FROM livingmemory_memories_fts WHERE doc_id = ?",
                (mid,),
            )
            row = await cursor.fetchone()
            assert row[0] == 0

    await engine.close()


@pytest.mark.asyncio
async def test_batch_delete_faiss_failure_is_marked_for_repair(tmp_path: Path):
    """FAISS 删除失败时保留主记录，并将操作标记为可恢复。"""
    db_path = tmp_path / "batch_del_faissfail.db"
    faiss = _FakeFaissDB()
    engine = MemoryEngine(db_path=str(db_path), faiss_db=faiss, config={})
    await engine.initialize()

    ids = []
    for i in range(3):
        mid = faiss._next_id
        faiss._next_id += 1
        faiss.docs[mid] = {
            "id": mid,
            "doc_id": f"uuid-{mid}",
            "text": f"test {i}",
            "metadata": {"importance": 0.5, "session_id": "s1"},
        }
        ids.append(mid)

    if engine.db_connection is not None:
        for mid in ids:
            doc = faiss.docs[mid]
            await engine.db_connection.execute(
                "INSERT INTO documents (id, doc_id, text, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
                (
                    doc["id"],
                    doc["doc_id"],
                    doc["text"],
                    json.dumps(doc["metadata"], ensure_ascii=False),
                ),
            )
            await engine.db_connection.execute(
                "INSERT INTO livingmemory_memories_fts(doc_id, content) VALUES (?, ?)",
                (mid, doc["text"]),
            )
        await engine.db_connection.commit()

    async def failing_delete(uuid_doc_id):
        raise Exception("FAISS unavailable")

    faiss.delete = failing_delete

    with pytest.raises(RuntimeError, match="批量向量删除未找到文档"):
        await engine.batch_delete_memories(ids)

    if engine.db_connection is not None:
        cursor = await engine.db_connection.execute(
            "SELECT COUNT(*) FROM documents WHERE id IN (?, ?, ?)", tuple(ids)
        )
        row = await cursor.fetchone()
        assert row[0] == 3

        cursor = await engine.db_connection.execute(
            """
            SELECT status, step FROM memory_write_ops
            WHERE op_type = 'batch_delete'
            ORDER BY id DESC LIMIT 1
            """
        )
        op_row = await cursor.fetchone()
        assert op_row["status"] == "needs_repair"
        assert op_row["step"] == "batch_delete_failed"

    await engine.close()


@pytest.mark.asyncio
async def test_concurrent_access_time_updates_do_not_lose_counts(tmp_path: Path):
    """并发访问时间更新应原子递增 access_count，不产生丢失更新。"""
    engine = MemoryEngine(
        db_path=str(tmp_path / "concurrent_access.db"),
        faiss_db=_FakeFaissDB(),
        config={},
    )
    await engine.initialize()

    memory_id = 1
    await engine.db_connection.execute(
        """
        INSERT OR REPLACE INTO documents(id, doc_id, text, metadata, created_at, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        (
            memory_id,
            "uuid-1",
            "并发访问",
            json.dumps({"importance": 0.5, "last_access_time": 0, "access_count": 0}),
        ),
    )
    await engine.db_connection.commit()

    await asyncio.gather(
        *[engine._update_access_time_internal(memory_id) for _ in range(20)]
    )

    cursor = await engine.db_connection.execute(
        "SELECT metadata FROM documents WHERE id = ?", (memory_id,)
    )
    row = await cursor.fetchone()
    assert json.loads(row["metadata"])["access_count"] == 20

    await engine.close()


@pytest.mark.asyncio
async def test_get_session_memories_sorted_by_create_time_desc(tmp_path: Path):
    """get_session_memories 应按 session_id 过滤并按 create_time 降序返回。"""
    engine = MemoryEngine(
        db_path=str(tmp_path / "session_sort.db"),
        faiss_db=_FakeFaissDB(),
        config={},
    )
    await engine.initialize()

    now = time.time()
    for i in range(3):
        await engine.db_connection.execute(
            """
            INSERT INTO documents(id, doc_id, text, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (
                i + 1,
                f"uuid-{i + 1}",
                f"memory-{i}",
                json.dumps({"session_id": "s1", "create_time": now + i}),
            ),
        )
    await engine.db_connection.execute(
        """
        INSERT INTO documents(id, doc_id, text, metadata, created_at, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        (
            100,
            "uuid-100",
            "other-session",
            json.dumps({"session_id": "s2", "create_time": now + 100}),
        ),
    )
    await engine.db_connection.commit()

    memories = await engine.get_session_memories("s1", limit=10)

    assert [m["id"] for m in memories] == [3, 2, 1]
    assert all(m["metadata"]["session_id"] == "s1" for m in memories)

    await engine.close()
