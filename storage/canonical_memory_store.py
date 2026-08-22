"""Authoritative parent/fact storage and derived fact indexes for S2."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from astrbot.api import logger

from ..core.models.memory_contract import MEMORY_SCHEMA_VERSION
from ..core.retrieval.vector_retriever import delete_faiss_documents_by_ids
from ..core.utils.memory_facts import unique_strings


@dataclass(slots=True, frozen=True)
class FactIndexStatus:
    """Consistency counts for the S2 fact projections."""

    fact_count: int
    fts_count: int
    vector_count: int

    @property
    def is_consistent(self) -> bool:
        return self.fact_count == self.fts_count == self.vector_count


class CanonicalMemoryStore:
    """Keep canonical facts authoritative and search indexes replaceable.

    ``memory_parents`` and ``memory_facts`` live in LivingMemory's main SQLite
    database.  The FTS table and the dedicated FAISS database are projections
    that can be rebuilt only from active ``memory_facts`` rows.
    """

    FTS_TABLE = "livingmemory_facts_fts"

    def __init__(
        self,
        db_path: str,
        fact_vector_db: Any,
        text_processor: Any,
    ) -> None:
        self.db_path = str(db_path)
        self.fact_vector_db = fact_vector_db
        self.text_processor = text_processor
        self.db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode = WAL")
        await self.db.execute("PRAGMA busy_timeout = 10000")
        await self.db.execute("PRAGMA foreign_keys = ON")
        await self.db.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS memory_parents (
                parent_id TEXT PRIMARY KEY,
                document_id INTEGER NOT NULL UNIQUE,
                idempotency_key TEXT NOT NULL UNIQUE,
                scope TEXT NOT NULL,
                persona_id TEXT,
                source_json TEXT NOT NULL,
                overview TEXT NOT NULL,
                generation_version TEXT NOT NULL,
                fact_ids_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_memory_parents_scope_status
            ON memory_parents(scope, status);

            CREATE TABLE IF NOT EXISTS memory_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fact_id TEXT NOT NULL UNIQUE,
                parent_id TEXT NOT NULL,
                fact_json TEXT NOT NULL,
                search_text TEXT NOT NULL,
                scope TEXT NOT NULL,
                persona_id TEXT,
                importance REAL NOT NULL,
                vector_doc_id INTEGER UNIQUE,
                status TEXT NOT NULL,
                last_retrieved_at REAL,
                retrieval_count INTEGER NOT NULL DEFAULT 0,
                last_injected_at REAL,
                injection_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(parent_id) REFERENCES memory_parents(parent_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_memory_facts_parent
            ON memory_facts(parent_id);

            CREATE INDEX IF NOT EXISTS idx_memory_facts_scope_status
            ON memory_facts(scope, status);

            CREATE VIRTUAL TABLE IF NOT EXISTS {self.FTS_TABLE}
            USING fts5(
                content,
                fact_id UNINDEXED,
                parent_id UNINDEXED,
                tokenize='unicode61'
            );
            """
        )
        cursor = await self.db.execute("PRAGMA table_info(memory_facts)")
        existing_columns = {str(row["name"]) for row in await cursor.fetchall()}
        lifecycle_columns = {
            "last_retrieved_at": "REAL",
            "retrieval_count": "INTEGER NOT NULL DEFAULT 0",
            "last_injected_at": "REAL",
            "injection_count": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in lifecycle_columns.items():
            if column not in existing_columns:
                await self.db.execute(
                    f"ALTER TABLE memory_facts ADD COLUMN {column} {declaration}"
                )
        await self.db.commit()

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None

    @staticmethod
    def _validated_facts(metadata: dict[str, Any]) -> list[dict[str, Any]]:
        if metadata.get("memory_schema_version") != MEMORY_SCHEMA_VERSION:
            raise ValueError("canonical write requires the v3 memory contract")
        parent_id = str(metadata.get("parent_id") or "").strip()
        idempotency_key = str(metadata.get("idempotency_key") or "").strip()
        facts = metadata.get("key_facts")
        if not parent_id or not idempotency_key:
            raise ValueError("canonical write requires parent_id and idempotency_key")
        if not isinstance(facts, list) or not facts:
            raise ValueError("canonical write requires at least one fact object")

        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, raw_fact in enumerate(facts):
            if not isinstance(raw_fact, dict):
                raise ValueError(f"key_facts[{index}] must be an object")
            fact = dict(raw_fact)
            fact_id = str(fact.get("fact_id") or "").strip()
            fact_text = str(fact.get("fact") or "").strip()
            if not fact_id or not fact_text:
                raise ValueError(f"key_facts[{index}] requires fact_id and fact")
            if str(fact.get("parent_id") or "") != parent_id:
                raise ValueError(f"key_facts[{index}] has a different parent_id")
            if fact_id in seen_ids:
                raise ValueError(f"duplicate fact_id: {fact_id}")
            seen_ids.add(fact_id)
            normalized.append(fact)
        return normalized

    @staticmethod
    def fact_search_text(fact: dict[str, Any]) -> str:
        """Build the only text allowed into fact FTS/vector projections."""
        parts = [str(fact.get("fact") or "").strip()]
        parts.extend(str(item).strip() for item in fact.get("participants", []) or [])
        parts.extend(str(item).strip() for item in fact.get("topics", []) or [])
        return " ".join(unique_strings(item for item in parts if item))

    async def find_active_document(self, idempotency_key: str) -> int | None:
        if self.db is None:
            return None
        cursor = await self.db.execute(
            """
            SELECT document_id
            FROM memory_parents
            WHERE idempotency_key = ? AND status = 'active'
            LIMIT 1
            """,
            (str(idempotency_key),),
        )
        row = await cursor.fetchone()
        return int(row["document_id"]) if row else None

    async def persist(
        self,
        *,
        document_id: int,
        session_id: str,
        persona_id: str | None,
        metadata: dict[str, Any],
    ) -> list[str]:
        """Persist canonical rows and both fact indexes, rolling back failures."""
        if self.db is None:
            raise RuntimeError("canonical memory store is not initialized")
        if self.fact_vector_db is None:
            raise RuntimeError("fact vector database is not initialized")

        facts = self._validated_facts(metadata)
        parent_id = str(metadata["parent_id"])
        idempotency_key = str(metadata["idempotency_key"])
        source = metadata.get("source_window")
        if not isinstance(source, dict) or not source.get("fingerprint"):
            raise ValueError("canonical write requires a stable source_window")
        overview = str(
            metadata.get("canonical_summary") or metadata.get("summary") or ""
        ).strip()
        if not overview:
            raise ValueError("canonical write requires a neutral overview")

        now = time.time()
        vector_ids: list[int] = []
        async with self._write_lock:
            existing_id = await self.find_active_document(idempotency_key)
            if existing_id is not None:
                if existing_id != int(document_id):
                    raise ValueError(
                        "canonical source was persisted by another document"
                    )
                return [str(fact["fact_id"]) for fact in facts]

            try:
                await self.db.execute(
                    """
                    INSERT INTO memory_parents(
                        parent_id, document_id, idempotency_key, scope,
                        persona_id, source_json, overview, generation_version,
                        fact_ids_json, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'building', ?, ?)
                    """,
                    (
                        parent_id,
                        int(document_id),
                        idempotency_key,
                        str(session_id),
                        persona_id,
                        json.dumps(source, ensure_ascii=False, sort_keys=True),
                        overview,
                        str(metadata.get("generation_version") or ""),
                        json.dumps(
                            [str(fact["fact_id"]) for fact in facts],
                            ensure_ascii=False,
                        ),
                        now,
                        now,
                    ),
                )

                prepared: list[tuple[dict[str, Any], str, str]] = []
                for fact in facts:
                    search_text = self.fact_search_text(fact)
                    tokens = await self.text_processor.tokenize_async(
                        search_text, remove_stopwords=False
                    )
                    processed_text = " ".join(tokens)
                    cursor = await self.db.execute(
                        """
                        INSERT INTO memory_facts(
                            fact_id, parent_id, fact_json, search_text, scope,
                            persona_id, importance, vector_doc_id, status,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'building', ?, ?)
                        """,
                        (
                            str(fact["fact_id"]),
                            parent_id,
                            json.dumps(fact, ensure_ascii=False, sort_keys=True),
                            search_text,
                            str(session_id),
                            persona_id,
                            float(fact.get("importance", 0.5)),
                            now,
                            now,
                        ),
                    )
                    del cursor
                    prepared.append((fact, search_text, processed_text))
                await self.db.commit()

                for fact, search_text, processed_text in prepared:
                    vector_id = int(
                        await self.fact_vector_db.insert(
                            content=search_text,
                            metadata={
                                "fact_id": str(fact["fact_id"]),
                                "parent_id": parent_id,
                                "session_id": str(session_id),
                                "persona_id": persona_id,
                                "importance": float(fact.get("importance", 0.5)),
                                "status": "active",
                                "schema_version": MEMORY_SCHEMA_VERSION,
                            },
                        )
                    )
                    vector_ids.append(vector_id)
                    await self.db.execute(
                        "UPDATE memory_facts SET vector_doc_id = ? WHERE fact_id = ?",
                        (vector_id, str(fact["fact_id"])),
                    )
                    await self.db.execute(
                        f"INSERT INTO {self.FTS_TABLE}(content, fact_id, parent_id) "
                        "VALUES (?, ?, ?)",
                        (processed_text, str(fact["fact_id"]), parent_id),
                    )

                await self.db.execute(
                    """
                    UPDATE memory_facts
                    SET status = 'active', updated_at = ?
                    WHERE parent_id = ?
                    """,
                    (time.time(), parent_id),
                )
                await self.db.execute(
                    """
                    UPDATE memory_parents
                    SET status = 'active', updated_at = ?
                    WHERE parent_id = ?
                    """,
                    (time.time(), parent_id),
                )
                await self.db.commit()
                return [str(fact["fact_id"]) for fact in facts]
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._rollback_parent(parent_id, vector_ids=vector_ids)
                )
                raise
            except Exception:
                await self._rollback_parent(parent_id, vector_ids=vector_ids)
                raise

    async def _rollback_parent(
        self, parent_id: str, *, vector_ids: list[int] | None = None
    ) -> None:
        if self.db is None:
            return
        if vector_ids and self.fact_vector_db is not None:
            try:
                await delete_faiss_documents_by_ids(self.fact_vector_db, vector_ids)
            except Exception:
                logger.error("回滚 fact 向量失败", exc_info=True)
        await self.db.execute(
            f"DELETE FROM {self.FTS_TABLE} WHERE parent_id = ?", (parent_id,)
        )
        await self.db.execute(
            "DELETE FROM memory_parents WHERE parent_id = ?", (parent_id,)
        )
        await self.db.commit()

    async def delete_by_document(self, document_id: int) -> None:
        if self.db is None:
            return
        async with self._write_lock:
            cursor = await self.db.execute(
                "SELECT parent_id FROM memory_parents WHERE document_id = ?",
                (int(document_id),),
            )
            row = await cursor.fetchone()
            if not row:
                return
            parent_id = str(row["parent_id"])
            cursor = await self.db.execute(
                "SELECT vector_doc_id FROM memory_facts WHERE parent_id = ?",
                (parent_id,),
            )
            vector_ids = [
                int(item["vector_doc_id"])
                for item in await cursor.fetchall()
                if item["vector_doc_id"] is not None
            ]
            if vector_ids and self.fact_vector_db is not None:
                deleted = await delete_faiss_documents_by_ids(
                    self.fact_vector_db, vector_ids
                )
                if deleted is None:
                    raise RuntimeError("fact vector storage cannot delete by integer id")
            await self.db.execute(
                f"DELETE FROM {self.FTS_TABLE} WHERE parent_id = ?", (parent_id,)
            )
            await self.db.execute(
                "DELETE FROM memory_parents WHERE parent_id = ?", (parent_id,)
            )
            await self.db.commit()

    async def archive_documents(self, document_ids: list[int]) -> int:
        """Archive canonical facts and remove both searchable projections."""
        if self.db is None:
            return 0
        unique_ids = list(dict.fromkeys(int(item) for item in document_ids))
        if not unique_ids:
            return 0
        placeholders = ",".join("?" * len(unique_ids))
        async with self._write_lock:
            cursor = await self.db.execute(
                f"""
                SELECT p.parent_id, f.vector_doc_id
                FROM memory_parents p
                JOIN memory_facts f ON f.parent_id = p.parent_id
                WHERE p.document_id IN ({placeholders})
                  AND p.status = 'active' AND f.status = 'active'
                """,
                unique_ids,
            )
            rows = await cursor.fetchall()
            parent_ids = list(dict.fromkeys(str(row["parent_id"]) for row in rows))
            vector_ids = [
                int(row["vector_doc_id"])
                for row in rows
                if row["vector_doc_id"] is not None
            ]
            if not parent_ids:
                return 0
            if vector_ids and self.fact_vector_db is not None:
                deleted = await delete_faiss_documents_by_ids(
                    self.fact_vector_db, vector_ids
                )
                if deleted is None:
                    raise RuntimeError("fact vector storage cannot archive by integer id")
            parent_placeholders = ",".join("?" * len(parent_ids))
            await self.db.execute(
                f"DELETE FROM {self.FTS_TABLE} WHERE parent_id IN ({parent_placeholders})",
                parent_ids,
            )
            now = time.time()
            await self.db.execute(
                f"""
                UPDATE memory_facts
                SET status = 'archived', vector_doc_id = NULL, updated_at = ?
                WHERE parent_id IN ({parent_placeholders}) AND status = 'active'
                """,
                (now, *parent_ids),
            )
            await self.db.execute(
                f"""
                UPDATE memory_parents SET status = 'archived', updated_at = ?
                WHERE parent_id IN ({parent_placeholders}) AND status = 'active'
                """,
                (now, *parent_ids),
            )
            await self.db.commit()
            return len(parent_ids)

    async def restore_document(self, document_id: int) -> bool:
        """Restore archived canonical facts and rebuild their projections."""
        if self.db is None or self.fact_vector_db is None:
            return False
        async with self._write_lock:
            cursor = await self.db.execute(
                """
                SELECT p.parent_id, f.id, f.fact_id, f.search_text, f.scope,
                       f.persona_id, f.importance
                FROM memory_parents p
                JOIN memory_facts f ON f.parent_id = p.parent_id
                WHERE p.document_id = ? AND p.status = 'archived'
                ORDER BY f.id ASC
                """,
                (int(document_id),),
            )
            rows = await cursor.fetchall()
            if not rows:
                return False
            parent_id = str(rows[0]["parent_id"])
            inserted_vector_ids: list[int] = []
            try:
                for row in rows:
                    vector_id = int(
                        await self.fact_vector_db.insert(
                            content=str(row["search_text"]),
                            metadata={
                                "fact_id": str(row["fact_id"]),
                                "parent_id": parent_id,
                                "session_id": str(row["scope"]),
                                "persona_id": row["persona_id"],
                                "importance": float(row["importance"]),
                                "status": "active",
                                "schema_version": MEMORY_SCHEMA_VERSION,
                            },
                        )
                    )
                    inserted_vector_ids.append(vector_id)
                    tokens = await self.text_processor.tokenize_async(
                        str(row["search_text"]), remove_stopwords=False
                    )
                    await self.db.execute(
                        f"INSERT INTO {self.FTS_TABLE}(content, fact_id, parent_id) VALUES (?, ?, ?)",
                        (" ".join(tokens), str(row["fact_id"]), parent_id),
                    )
                    await self.db.execute(
                        "UPDATE memory_facts SET vector_doc_id = ?, status = 'active', updated_at = ? WHERE id = ?",
                        (vector_id, time.time(), int(row["id"])),
                    )
                await self.db.execute(
                    "UPDATE memory_parents SET status = 'active', updated_at = ? WHERE parent_id = ?",
                    (time.time(), parent_id),
                )
                await self.db.commit()
                return True
            except asyncio.CancelledError:
                await asyncio.shield(self._rollback_restored_parent(parent_id, inserted_vector_ids))
                raise
            except Exception:
                await self._rollback_restored_parent(parent_id, inserted_vector_ids)
                raise

    async def _rollback_restored_parent(
        self, parent_id: str, vector_ids: list[int]
    ) -> None:
        if self.db is None:
            return
        if vector_ids and self.fact_vector_db is not None:
            await delete_faiss_documents_by_ids(self.fact_vector_db, vector_ids)
        await self.db.execute(
            f"DELETE FROM {self.FTS_TABLE} WHERE parent_id = ?", (parent_id,)
        )
        await self.db.execute(
            "UPDATE memory_facts SET vector_doc_id = NULL, status = 'archived' WHERE parent_id = ?",
            (parent_id,),
        )
        await self.db.execute(
            "UPDATE memory_parents SET status = 'archived' WHERE parent_id = ?",
            (parent_id,),
        )
        await self.db.commit()

    async def get_facts_by_document(self, document_id: int) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        cursor = await self.db.execute(
            """
            SELECT f.fact_json, f.status, f.importance,
                   f.last_retrieved_at, f.retrieval_count,
                   f.last_injected_at, f.injection_count
            FROM memory_facts f
            JOIN memory_parents p ON p.parent_id = f.parent_id
            WHERE p.document_id = ?
            ORDER BY f.id ASC
            """,
            (int(document_id),),
        )
        rows = await cursor.fetchall()
        facts: list[dict[str, Any]] = []
        for row in rows:
            try:
                value = json.loads(row["fact_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, dict):
                value["lifecycle"] = {
                    "status": str(row["status"]),
                    "importance": float(row["importance"]),
                    "last_retrieved_at": row["last_retrieved_at"],
                    "retrieval_count": int(row["retrieval_count"] or 0),
                    "last_injected_at": row["last_injected_at"],
                    "injection_count": int(row["injection_count"] or 0),
                }
                facts.append(value)
        return facts

    async def get_fact_records(
        self, fact_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Load authoritative facts and their parent/source metadata in one query."""
        if self.db is None:
            return {}
        unique_ids = list(
            dict.fromkeys(str(fact_id).strip() for fact_id in fact_ids if fact_id)
        )
        if not unique_ids:
            return {}
        placeholders = ",".join("?" * len(unique_ids))
        cursor = await self.db.execute(
            f"""
            SELECT f.fact_id, f.parent_id, f.fact_json, f.search_text,
                   f.scope, f.persona_id, f.importance, f.status,
                   f.last_retrieved_at, f.retrieval_count,
                   f.last_injected_at, f.injection_count,
                   f.created_at, f.updated_at,
                   p.document_id, p.overview, p.source_json,
                   d.metadata AS document_metadata
            FROM memory_facts f
            JOIN memory_parents p ON p.parent_id = f.parent_id
            JOIN documents d ON d.id = p.document_id
            WHERE f.fact_id IN ({placeholders})
              AND f.status = 'active'
              AND p.status = 'active'
              AND COALESCE(json_extract(d.metadata, '$.status'), 'active') = 'active'
            """,
            unique_ids,
        )
        records: dict[str, dict[str, Any]] = {}
        for row in await cursor.fetchall():
            try:
                fact = json.loads(row["fact_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(fact, dict):
                continue
            try:
                source = json.loads(row["source_json"])
            except (json.JSONDecodeError, TypeError):
                source = {}
            try:
                document_metadata = json.loads(row["document_metadata"])
            except (json.JSONDecodeError, TypeError):
                document_metadata = {}
            fact_id = str(row["fact_id"])
            records[fact_id] = {
                "fact_id": fact_id,
                "parent_id": str(row["parent_id"]),
                "document_id": int(row["document_id"]),
                "fact": fact,
                "search_text": str(row["search_text"] or ""),
                "scope": str(row["scope"] or ""),
                "persona_id": row["persona_id"],
                "importance": float(row["importance"]),
                "status": str(row["status"]),
                "overview": str(row["overview"] or ""),
                "source_window": source if isinstance(source, dict) else {},
                "document_metadata": (
                    document_metadata if isinstance(document_metadata, dict) else {}
                ),
                "last_retrieved_at": row["last_retrieved_at"],
                "retrieval_count": int(row["retrieval_count"] or 0),
                "last_injected_at": row["last_injected_at"],
                "injection_count": int(row["injection_count"] or 0),
                "created_at": float(row["created_at"]),
                "updated_at": float(row["updated_at"]),
            }
        return records

    async def record_fact_event(self, fact_ids: list[str], event: str) -> int:
        """Record candidate or injection events without conflating the two."""
        if self.db is None or event not in {"retrieved", "injected"}:
            return 0
        unique_ids = list(
            dict.fromkeys(str(fact_id).strip() for fact_id in fact_ids if fact_id)
        )
        if not unique_ids:
            return 0
        placeholders = ",".join("?" * len(unique_ids))
        now = time.time()
        timestamp_column = (
            "last_retrieved_at" if event == "retrieved" else "last_injected_at"
        )
        count_column = "retrieval_count" if event == "retrieved" else "injection_count"
        async with self._write_lock:
            cursor = await self.db.execute(
                f"""
                UPDATE memory_facts
                SET {timestamp_column} = ?,
                    {count_column} = MIN(COALESCE({count_column}, 0) + 1, 1000000)
                WHERE fact_id IN ({placeholders}) AND status = 'active'
                """,
                (now, *unique_ids),
            )
            if event == "injected":
                await self.db.execute(
                    f"""
                    UPDATE documents
                    SET metadata = CASE
                        WHEN json_valid(metadata) THEN json_set(
                            json_set(metadata, '$.last_access_time', ?),
                            '$.access_count',
                            MIN(COALESCE(CAST(json_extract(metadata, '$.access_count') AS INTEGER), 0) + 1, 1000000)
                        )
                        ELSE json_set('{{}}', '$.last_access_time', ?, '$.access_count', 1)
                    END
                    WHERE id IN (
                        SELECT DISTINCT p.document_id
                        FROM memory_facts f
                        JOIN memory_parents p ON p.parent_id = f.parent_id
                        WHERE f.fact_id IN ({placeholders})
                    )
                    """,
                    (now, now, *unique_ids),
                )
            await self.db.commit()
            return int(cursor.rowcount or 0)

    async def update_document_importance(
        self, document_id: int, importance: float
    ) -> int:
        """Apply a parent-level manual edit to all authoritative child facts."""
        if self.db is None:
            return 0
        normalized = max(0.0, min(1.0, float(importance)))
        now = time.time()
        async with self._write_lock:
            cursor = await self.db.execute(
                """
                SELECT f.id, f.fact_json
                FROM memory_facts f
                JOIN memory_parents p ON p.parent_id = f.parent_id
                WHERE p.document_id = ? AND f.status = 'active'
                """,
                (int(document_id),),
            )
            updates: list[tuple[float, str, float, int]] = []
            for row in await cursor.fetchall():
                try:
                    fact = json.loads(row["fact_json"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(fact, dict):
                    continue
                fact["importance"] = normalized
                updates.append(
                    (
                        normalized,
                        json.dumps(fact, ensure_ascii=False, sort_keys=True),
                        now,
                        int(row["id"]),
                    )
                )
            if updates:
                await self.db.executemany(
                    "UPDATE memory_facts SET importance = ?, fact_json = ?, updated_at = ? WHERE id = ?",
                    updates,
                )
                await self.db.commit()
            return len(updates)

    async def apply_daily_decay(
        self,
        decay_rate: float,
        *,
        days: int = 1,
        protected_threshold: float = 1.0,
        injection_window_days: float = 30.0,
        max_injection_count: float = 10.0,
        count_decay_multiplier: float = 0.5,
    ) -> int:
        """Decay authoritative facts; only actual injection slows the decay."""
        if self.db is None or decay_rate <= 0 or days <= 0:
            return 0
        rate = min(1.0, float(decay_rate))
        now = time.time()
        window_start = now - max(1.0, float(injection_window_days)) * 86400.0
        count_multiplier = max(0.0, min(1.0, float(count_decay_multiplier)))
        async with self._write_lock:
            cursor = await self.db.execute(
                """
                SELECT id, fact_json, importance, last_injected_at, injection_count
                FROM memory_facts
                WHERE status = 'active'
                """
            )
            updates: list[tuple[float, str, int, float, int]] = []
            for row in await cursor.fetchall():
                importance = max(0.0, min(1.0, float(row["importance"])))
                if importance >= protected_threshold:
                    continue
                injection_count = float(row["injection_count"] or 0)
                last_injected = float(row["last_injected_at"] or 0)
                recent_factor = 1.0 if last_injected >= window_start else 0.5
                use_factor = min(
                    1.0, injection_count / max(1.0, max_injection_count)
                )
                effective_rate = rate * (1 - 0.5 * use_factor * recent_factor)
                decayed = max(
                    0.01, round(importance * ((1 - effective_rate) ** days), 4)
                )
                try:
                    fact = json.loads(row["fact_json"])
                except (json.JSONDecodeError, TypeError):
                    fact = {}
                if isinstance(fact, dict):
                    fact["importance"] = decayed
                updates.append(
                    (
                        decayed,
                        json.dumps(fact, ensure_ascii=False, sort_keys=True),
                        int(injection_count * count_multiplier),
                        now,
                        int(row["id"]),
                    )
                )
            if not updates:
                return 0
            await self.db.executemany(
                """
                UPDATE memory_facts
                SET importance = ?, fact_json = ?, injection_count = ?, updated_at = ?
                WHERE id = ?
                """,
                updates,
            )
            await self.db.execute(
                """
                UPDATE documents
                SET metadata = json_set(
                    CASE WHEN json_valid(metadata) THEN metadata ELSE '{}' END,
                    '$.importance',
                    COALESCE((
                        SELECT MAX(f.importance)
                        FROM memory_parents p
                        JOIN memory_facts f ON f.parent_id = p.parent_id
                        WHERE p.document_id = documents.id AND f.status = 'active'
                    ), json_extract(metadata, '$.importance'), 0.5)
                )
                WHERE id IN (SELECT document_id FROM memory_parents)
                """
            )
            await self.db.commit()
        return len(updates)

    async def get_topic_candidates(
        self, scope: str, limit: int = 50
    ) -> list[dict[str, str]]:
        if self.db is None or not str(scope or "").strip():
            return []
        cursor = await self.db.execute(
            """
            SELECT fact_json
            FROM memory_facts
            WHERE scope = ? AND status = 'active'
            ORDER BY id DESC
            LIMIT ?
            """,
            (str(scope), max(int(limit) * 4, int(limit))),
        )
        candidates: dict[str, dict[str, str]] = {}
        for row in await cursor.fetchall():
            try:
                fact = json.loads(row["fact_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            for ref in fact.get("topic_refs", []) if isinstance(fact, dict) else []:
                if not isinstance(ref, dict):
                    continue
                topic_ref_id = str(ref.get("topic_id") or "").strip()
                name = str(ref.get("name") or "").strip()
                if topic_ref_id and name:
                    candidates.setdefault(topic_ref_id, {"topic_id": topic_ref_id, "name": name})
            if len(candidates) >= int(limit):
                break
        return list(candidates.values())[: int(limit)]

    async def index_status(self) -> FactIndexStatus:
        if self.db is None:
            return FactIndexStatus(0, 0, 0)
        cursor = await self.db.execute(
            "SELECT COUNT(*) AS count FROM memory_facts WHERE status = 'active'"
        )
        fact_count = int((await cursor.fetchone())["count"])
        cursor = await self.db.execute(
            f"SELECT COUNT(DISTINCT fact_id) AS count FROM {self.FTS_TABLE}"
        )
        fts_count = int((await cursor.fetchone())["count"])
        index = getattr(
            getattr(self.fact_vector_db, "embedding_storage", None), "index", None
        )
        if index is not None:
            vector_count = int(getattr(index, "ntotal", 0) or 0)
        else:
            count_documents = getattr(
                getattr(self.fact_vector_db, "document_storage", None),
                "count_documents",
                None,
            )
            vector_count = (
                int(await count_documents(metadata_filters={}))
                if callable(count_documents)
                else 0
            )
        return FactIndexStatus(fact_count, fts_count, vector_count)

    async def search_candidates(
        self,
        query: str,
        *,
        limit: int = 10,
        scope: str | None = None,
        persona_id: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Expose separate fact routes for evaluation without changing recall."""
        if self.db is None or self.fact_vector_db is None or not query.strip():
            return {"bm25": [], "vector": []}
        tokens = await self.text_processor.tokenize_async(
            query, remove_stopwords=False
        )
        fts_query = " OR ".join(
            f'"{str(token).replace(chr(34), chr(34) * 2)}"'
            for token in tokens
            if str(token).strip()
        )
        bm25: list[dict[str, Any]] = []
        if fts_query:
            filters = [
                "f.status = 'active'",
                "p.status = 'active'",
                "COALESCE(json_extract(d.metadata, '$.status'), 'active') = 'active'",
            ]
            parameters: list[Any] = [fts_query]
            if scope is not None:
                filters.append("f.scope = ?")
                parameters.append(scope)
            if persona_id is not None:
                filters.append("f.persona_id = ?")
                parameters.append(persona_id)
            parameters.append(max(1, int(limit)))
            cursor = await self.db.execute(
                f"""
                SELECT idx.fact_id, idx.parent_id,
                       bm25({self.FTS_TABLE}) AS score
                FROM {self.FTS_TABLE} idx
                JOIN memory_facts f ON f.fact_id = idx.fact_id
                JOIN memory_parents p ON p.parent_id = idx.parent_id
                JOIN documents d ON d.id = p.document_id
                WHERE {self.FTS_TABLE} MATCH ?
                  AND {' AND '.join(filters)}
                ORDER BY score ASC
                LIMIT ?
                """,
                parameters,
            )
            bm25 = [
                {
                    "fact_id": str(row["fact_id"]),
                    "parent_id": str(row["parent_id"]),
                    "score": float(row["score"]),
                }
                for row in await cursor.fetchall()
            ]

        active_filters = [
            "f.status = 'active'",
            "p.status = 'active'",
            "COALESCE(json_extract(d.metadata, '$.status'), 'active') = 'active'",
        ]
        active_parameters: list[Any] = []
        if scope is not None:
            active_filters.append("f.scope = ?")
            active_parameters.append(scope)
        if persona_id is not None:
            active_filters.append("f.persona_id = ?")
            active_parameters.append(persona_id)
        cursor = await self.db.execute(
            f"""
            SELECT f.fact_id
            FROM memory_facts f
            JOIN memory_parents p ON p.parent_id = f.parent_id
            JOIN documents d ON d.id = p.document_id
            WHERE {' AND '.join(active_filters)}
            """,
            active_parameters,
        )
        active_fact_ids = {str(row["fact_id"]) for row in await cursor.fetchall()}

        vector_results = await self.fact_vector_db.retrieve(
            query=query,
            k=max(10, int(limit) * 5),
            fetch_k=max(10, int(limit) * 5),
            rerank=False,
            metadata_filters=None,
        )
        vector: list[dict[str, Any]] = []
        for result in vector_results:
            data = getattr(result, "data", {}) or {}
            metadata = data.get("metadata") or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
            if not isinstance(metadata, dict):
                continue
            if str(metadata.get("status") or "") != "active":
                continue
            if scope is not None and metadata.get("session_id") != scope:
                continue
            if persona_id is not None and metadata.get("persona_id") != persona_id:
                continue
            fact_id = str(metadata.get("fact_id") or "")
            parent_id = str(metadata.get("parent_id") or "")
            if fact_id not in active_fact_ids:
                continue
            if fact_id and parent_id:
                vector.append(
                    {
                        "fact_id": fact_id,
                        "parent_id": parent_id,
                        "score": float(getattr(result, "similarity", 0.0)),
                    }
                )
            if len(vector) >= max(1, int(limit)):
                break
        return {"bm25": bm25, "vector": vector}

    async def rebuild_indexes(self) -> dict[str, int | bool]:
        """Rebuild both projections only from active canonical fact rows."""
        if self.db is None or self.fact_vector_db is None:
            raise RuntimeError("canonical fact indexes are not initialized")
        async with self._write_lock:
            cursor = await self.db.execute(
                """
                SELECT id, fact_id, parent_id, fact_json, search_text,
                       scope, persona_id, importance
                FROM memory_facts
                WHERE status = 'active'
                ORDER BY id ASC
                """
            )
            facts = await cursor.fetchall()

            documents = await self.fact_vector_db.document_storage.get_documents(
                metadata_filters={}, offset=0, limit=max(len(facts) * 2 + 100, 100)
            )
            existing_vector_ids = [int(item["id"]) for item in documents]
            if existing_vector_ids:
                deleted = await delete_faiss_documents_by_ids(
                    self.fact_vector_db, existing_vector_ids
                )
                if deleted is None:
                    raise RuntimeError("fact vector storage cannot be rebuilt")

            await self.db.execute(f"DELETE FROM {self.FTS_TABLE}")
            await self.db.execute(
                "UPDATE memory_facts SET vector_doc_id = NULL WHERE status = 'active'"
            )
            await self.db.commit()

            processed = 0
            for row in facts:
                vector_id = int(
                    await self.fact_vector_db.insert(
                        content=str(row["search_text"]),
                        metadata={
                            "fact_id": str(row["fact_id"]),
                            "parent_id": str(row["parent_id"]),
                            "session_id": str(row["scope"]),
                            "persona_id": row["persona_id"],
                            "importance": float(row["importance"]),
                            "status": "active",
                            "schema_version": MEMORY_SCHEMA_VERSION,
                        },
                    )
                )
                tokens = await self.text_processor.tokenize_async(
                    str(row["search_text"]), remove_stopwords=False
                )
                await self.db.execute(
                    f"INSERT INTO {self.FTS_TABLE}(content, fact_id, parent_id) "
                    "VALUES (?, ?, ?)",
                    (" ".join(tokens), str(row["fact_id"]), str(row["parent_id"])),
                )
                await self.db.execute(
                    "UPDATE memory_facts SET vector_doc_id = ?, updated_at = ? WHERE id = ?",
                    (vector_id, time.time(), int(row["id"])),
                )
                processed += 1
            await self.db.commit()
            status = await self.index_status()
            return {
                "success": status.is_consistent,
                "processed": processed,
                "facts": status.fact_count,
                "fts": status.fts_count,
                "vectors": status.vector_count,
            }


__all__ = ["CanonicalMemoryStore", "FactIndexStatus"]
