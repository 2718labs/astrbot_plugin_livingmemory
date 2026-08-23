"""
MemoryEngine 的 MemoryEngineCrudMixin 拆分模块
自动从 core/managers/memory_engine.py 拆分，保持行为不变
"""

import asyncio
import json
from typing import Any
from ..utils.number_utils import clamp_float, safe_float
from ..models.memory_contract import concept_key, normalize_concept_name, topic_id
from ..retrieval.hybrid_retriever import HybridResult
from astrbot.api import logger
from ..memory_transfer import memory_import_key
import time


class MemoryEngineCrudMixin:
    """MemoryEngine 拆分模块：MemoryEngineCrudMixin"""

    @staticmethod
    def _canonical_document_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        """Keep only parent/source fields in the temporary document projection."""
        allowed = {
            "memory_schema_version",
            "summary_schema_version",
            "generation_version",
            "parent_id",
            "idempotency_key",
            "summary",
            "canonical_summary",
            "interaction_type",
            "source_window",
            "source_session_id",
            "summary_quality",
            "time_tags",
            "source_time_start",
            "source_time_end",
            "source_time_label",
            "memory_origin",
            "memorize_reason",
        }
        parent_metadata = {
            key: value for key, value in metadata.items() if key in allowed
        }
        facts = metadata.get("key_facts") or []
        parent_metadata["fact_ids"] = [
            str(fact.get("fact_id"))
            for fact in facts
            if isinstance(fact, dict) and fact.get("fact_id")
        ]
        parent_metadata["canonical_build_status"] = "building"
        parent_metadata["status"] = "building"
        return parent_metadata

    async def add_canonical_memory(
        self,
        *,
        metadata: dict[str, Any],
        session_id: str,
        persona_id: str | None,
        importance: float,
        source_messages: list[dict[str, Any]] | None = None,
    ) -> int:
        """Persist one S1 record through the sole S2 canonical write path."""
        if self.canonical_store is None:
            raise RuntimeError("canonical memory store is not initialized")
        facts = self.canonical_store._validated_facts(metadata)
        idempotency_key = str(metadata.get("idempotency_key") or "").strip()
        projection = "；".join(str(fact["fact"]).strip() for fact in facts)
        if not projection:
            raise ValueError("canonical memory projection is empty")

        async with self._idempotency_lock:
            existing_id = await self.canonical_store.find_active_document(
                idempotency_key
            )
            if existing_id is not None:
                logger.info(
                    "[MemoryEngine] 复用同一 canonical 来源: "
                    f"key={idempotency_key}, memory_id={existing_id}"
                )
                return existing_id

            parent_metadata = self._canonical_document_metadata(metadata)
            document_id: int | None = None
            try:
                document_id = await self._add_memory_unchecked(
                    content=projection,
                    session_id=session_id,
                    persona_id=persona_id,
                    importance=importance,
                    metadata=parent_metadata,
                    atoms=None,
                    source_messages=source_messages,
                    skip_legacy_downstream=True,
                )
                fact_ids = await self.canonical_store.persist(
                    document_id=document_id,
                    session_id=session_id,
                    persona_id=persona_id,
                    metadata=metadata,
                )
                if self.hybrid_retriever is None or not await self.hybrid_retriever.update_metadata(
                    document_id,
                    {
                        "status": "active",
                        "canonical_build_status": "active",
                        "fact_ids": fact_ids,
                    },
                ):
                    raise RuntimeError("canonical parent activation failed")
                # S3: canonical facts (with their explicit topic/participant
                # bindings) are the only graph edge evidence; the extractor
                # routes on memory_schema_version internally.
                if self.graph_memory_manager is not None:
                    graph_metadata = dict(metadata)
                    graph_metadata.update(
                        {
                            "session_id": session_id,
                            "persona_id": persona_id,
                            "importance": importance,
                        }
                    )
                    await self.graph_memory_manager.index_memory(
                        document_id, projection, graph_metadata
                    )
                self._invalidate_search_cache()
                return document_id
            except asyncio.CancelledError:
                if document_id is not None:
                    await asyncio.shield(
                        self.canonical_store.delete_by_document(document_id)
                    )
                    if self.hybrid_retriever is not None:
                        await asyncio.shield(
                            self.hybrid_retriever.delete_memory(document_id)
                        )
                    if self.graph_memory_manager is not None:
                        await asyncio.shield(
                            self.graph_memory_manager.delete_memory(document_id)
                        )
                raise
            except Exception:
                if document_id is not None:
                    try:
                        await self.canonical_store.delete_by_document(document_id)
                    except Exception:
                        logger.error("canonical fact rollback failed", exc_info=True)
                    if self.hybrid_retriever is not None:
                        await self.hybrid_retriever.delete_memory(document_id)
                    if self.graph_memory_manager is not None:
                        await self.graph_memory_manager.delete_memory(document_id)
                    if self.db_connection is not None:
                        await self.db_connection.execute(
                            "DELETE FROM memory_sources WHERE memory_id = ?",
                            (document_id,),
                        )
                        await self.db_connection.commit()
                raise

    async def add_memory(
        self,
        content: str,
        session_id: str | None = None,
        persona_id: str | None = None,
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
        atoms: list | None = None,
        preserve_create_time: bool = False,
        source_messages: list[dict[str, Any]] | None = None,
    ) -> int:
        """Add a memory, reusing an active v3 record for the same source unit."""
        if (
            isinstance(metadata, dict)
            and metadata.get("memory_schema_version") == "v3"
            and self.canonical_store is not None
        ):
            return await self.add_canonical_memory(
                metadata=metadata,
                session_id=str(session_id or metadata.get("source_session_id") or ""),
                persona_id=persona_id,
                importance=importance,
                source_messages=source_messages,
            )
        idempotency_key = str((metadata or {}).get("idempotency_key") or "").strip()
        if not idempotency_key:
            return await self._add_memory_unchecked(
                content=content,
                session_id=session_id,
                persona_id=persona_id,
                importance=importance,
                metadata=metadata,
                atoms=atoms,
                preserve_create_time=preserve_create_time,
                source_messages=source_messages,
            )

        async with self._idempotency_lock:
            existing_id = await self._find_active_idempotent_memory(idempotency_key)
            if existing_id is not None:
                logger.info(
                    "[MemoryEngine] 复用同一来源单元的 active 记忆: "
                    f"key={idempotency_key}, memory_id={existing_id}"
                )
                return existing_id
            return await self._add_memory_unchecked(
                content=content,
                session_id=session_id,
                persona_id=persona_id,
                importance=importance,
                metadata=metadata,
                atoms=atoms,
                preserve_create_time=preserve_create_time,
                source_messages=source_messages,
            )

    async def _find_active_idempotent_memory(self, key: str) -> int | None:
        if self.db_connection is None:
            return None
        cursor = await self.db_connection.execute(
            """
            SELECT id
            FROM documents
            WHERE json_extract(metadata, '$.idempotency_key') = ?
              AND COALESCE(json_extract(metadata, '$.status'), 'active') = 'active'
            ORDER BY id ASC
            LIMIT 1
            """,
            (key,),
        )
        row = await cursor.fetchone()
        return int(row["id"] if hasattr(row, "keys") else row[0]) if row else None

    async def _add_memory_unchecked(
        self,
        content: str,
        session_id: str | None = None,
        persona_id: str | None = None,
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
        atoms: list | None = None,
        preserve_create_time: bool = False,
        source_messages: list[dict[str, Any]] | None = None,
        skip_legacy_downstream: bool = False,
    ) -> int:
        """
        添加新记忆

        Args:
            content: 记忆内容
            session_id: 会话ID(支持多种格式,自动提取UUID)
            persona_id: 人格ID(支持多种格式,自动提取UUID)
            importance: 重要性(0-1)
            metadata: 额外元数据

        Returns:
            int: 记忆ID(doc_id)
        """
        if not content or not content.strip():
            raise ValueError("记忆内容不能为空")

        op_id = await self._start_write_op(
            "add",
            {
                "content_preview": content[:500],
                "session_id": session_id,
                "persona_id": persona_id,
                "importance": importance,
                "metadata": metadata or {},
                "atoms": [
                    self._serialize_atom_for_repair(atom) for atom in (atoms or [])
                ],
            },
        )

        # 准备完整元数据 - 保存完整的 unified_msg_origin，不提取UUID
        # 只在查询/过滤时才提取UUID进行匹配，存储时保留完整信息
        current_time = time.time()
        full_metadata = {
            "session_id": session_id,  # 保存完整的 unified_msg_origin
            "persona_id": persona_id,  # 保存完整的 persona_id
            "importance": max(0.0, min(1.0, importance)),  # 限制在0-1范围
            "create_time": current_time,
            "last_access_time": current_time,
            "status": "active",
        }

        # 合并用户提供的额外元数据
        # 注意：先合并外部metadata，再确保时间字段不被覆盖
        if metadata:
            full_metadata.update(metadata)
        if source_messages:
            full_metadata["has_source"] = True
            full_metadata["source_message_count"] = len(source_messages)
        if atoms:
            full_metadata["atom_types"] = sorted(
                {
                    getattr(getattr(atom, "atom_type", None), "value", "unknown")
                    for atom in atoms
                }
            )

        # 普通新增使用当前时间；物理替换保留原记忆的时间轴位置。
        preserved_create_time = None
        if preserve_create_time and metadata:
            try:
                preserved_create_time = float(metadata.get("create_time"))
            except (TypeError, ValueError):
                preserved_create_time = None
        full_metadata["create_time"] = (
            preserved_create_time
            if preserved_create_time is not None
            else current_time
        )
        full_metadata["last_access_time"] = current_time

        # 通过混合检索器添加(会同时添加到BM25和向量索引)
        if self.hybrid_retriever is None:
            raise RuntimeError("混合检索器未初始化")
        try:
            doc_id = await self.hybrid_retriever.add_memory(content, full_metadata)
            await self._advance_write_op(
                op_id,
                "document_indexed",
                memory_id=doc_id,
                payload_patch={"memory_id": doc_id},
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._advance_write_op(
                op_id,
                "document_failed",
                status="failed",
                error=str(e),
            )
            raise

        # 写入记忆原子
        atom_write_failed = False
        if (
            not skip_legacy_downstream
            and atoms
            and self.atom_store is not None
            and self.atom_enabled
        ):
            prepared_atoms = []
            for atom in atoms:
                atom.session_id = atom.session_id or session_id
                atom.persona_id = atom.persona_id or persona_id
                atom.parent_memory_id = doc_id
                prepared_atoms.append(atom)
            try:
                await self.atom_store.insert_many(prepared_atoms)
                await self._advance_write_op(
                    op_id,
                    "atoms_indexed",
                    memory_id=doc_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("[MemoryEngine] 批量写入记忆原子失败", exc_info=True)
                failed_atoms: list[dict[str, Any]] = []
                for atom in prepared_atoms:
                    if getattr(atom, "atom_id", 0):
                        continue
                    try:
                        await self.atom_store.insert(atom)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        failed_atoms.append(self._serialize_atom_for_repair(atom))
                        logger.error(
                            f"[MemoryEngine] 写入记忆原子失败: {atom.content[:80]}",
                            exc_info=True,
                        )
                if failed_atoms:
                    await self._advance_write_op(
                        op_id,
                        "atoms_partial",
                        status="needs_repair",
                        memory_id=doc_id,
                        error="atom insert failed",
                        payload_patch={"failed_atoms": failed_atoms},
                    )
                    atom_write_failed = True
                else:
                    await self._advance_write_op(
                        op_id,
                        "atoms_indexed",
                        memory_id=doc_id,
                    )
        else:
            await self._advance_write_op(op_id, "atoms_skipped", memory_id=doc_id)

        needs_repair = atom_write_failed
        if not skip_legacy_downstream and self.graph_memory_manager is not None:
            try:
                await self.graph_memory_manager.index_memory(
                    doc_id, content, full_metadata
                )
                await self._advance_write_op(
                    op_id,
                    "graph_indexed",
                    status="needs_repair" if needs_repair else "pending",
                    memory_id=doc_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self._advance_write_op(
                    op_id,
                    "graph_failed",
                    status="needs_repair",
                    memory_id=doc_id,
                    error=str(e),
                )
                needs_repair = True
                logger.error(
                    f"[MemoryEngine] 图记忆索引失败，已标记待修复 (memory_id={doc_id})",
                    exc_info=True,
                )
        else:
            await self._advance_write_op(
                op_id,
                "graph_skipped",
                status="needs_repair" if needs_repair else "pending",
                memory_id=doc_id,
            )

        if source_messages:
            try:
                await self.save_memory_source(doc_id, source_messages)
            except asyncio.CancelledError:
                await asyncio.shield(self.delete_memory(doc_id))
                await asyncio.shield(
                    self._advance_write_op(
                        op_id,
                        "source_failed",
                        status="failed",
                        memory_id=doc_id,
                        error="source write cancelled",
                    )
                )
                raise
            except Exception as exc:
                await self._advance_write_op(
                    op_id,
                    "source_failed",
                    status="failed",
                    memory_id=doc_id,
                    error=str(exc),
                )
                if not await self.delete_memory(doc_id):
                    logger.error(
                        f"[MemoryEngine] 原文写入失败且记忆回滚失败 (memory_id={doc_id})"
                    )
                raise
        if not needs_repair:
            await self._advance_write_op(
                op_id,
                "completed",
                status="completed",
                memory_id=doc_id,
            )
        self._invalidate_search_cache()
        return doc_id

    async def save_memory_source(
        self, memory_id: int, source_messages: list[dict[str, Any]]
    ) -> None:
        """Persist source messages outside retrieval metadata and indexes."""
        if self.db_connection is None:
            raise RuntimeError("数据库连接未初始化")
        now = time.time()
        await self.db_connection.execute(
            """
            INSERT INTO memory_sources(memory_id, source_json, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                source_json = excluded.source_json,
                updated_at = excluded.updated_at
            """,
            (
                int(memory_id),
                json.dumps(source_messages, ensure_ascii=False),
                now,
                now,
            ),
        )
        await self.db_connection.commit()

    async def get_memory_source(self, memory_id: int) -> list[dict[str, Any]]:
        """Return structured source messages for one memory."""
        if self.db_connection is None:
            return []
        cursor = await self.db_connection.execute(
            "SELECT source_json FROM memory_sources WHERE memory_id = ?",
            (int(memory_id),),
        )
        row = await cursor.fetchone()
        if not row:
            return []
        try:
            value = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return []
        return value if isinstance(value, list) else []

    async def get_memory_transfer_records(
        self, memory_ids: list[int] | None = None
    ) -> list[dict[str, Any]]:
        """Return portable memory records without retrieval-index internals."""
        if self.db_connection is None:
            return []

        normalized_ids: list[int] | None = None
        if memory_ids is not None:
            normalized_ids = list(dict.fromkeys(int(item) for item in memory_ids))
            if not normalized_ids:
                return []

        async def _fetch_rows(batch: list[int] | None):
            params: list[Any] = []
            where_clause = ""
            if batch is not None:
                placeholders = ",".join("?" * len(batch))
                where_clause = f"WHERE d.id IN ({placeholders})"
                params.extend(batch)
            cursor = await self.db_connection.execute(
                f"""
            SELECT d.id, d.text, d.metadata, d.created_at, d.updated_at,
                   s.source_json
            FROM documents AS d
            LEFT JOIN memory_sources AS s ON s.memory_id = d.id
            {where_clause}
            ORDER BY d.id ASC
            """,
                params,
            )
            return await cursor.fetchall()

        if normalized_ids is None:
            rows = await _fetch_rows(None)
        else:
            rows = []
            for offset in range(0, len(normalized_ids), 500):
                rows.extend(
                    await _fetch_rows(normalized_ids[offset : offset + 500])
                )
            rows.sort(key=lambda row: int(row["id"]))
        records: list[dict[str, Any]] = []
        for row in rows:
            metadata = self._safe_json_dict(row["metadata"])
            source_messages: list[dict[str, Any]] = []
            if row["source_json"]:
                try:
                    parsed_source = json.loads(row["source_json"])
                except (json.JSONDecodeError, TypeError):
                    parsed_source = []
                if isinstance(parsed_source, list):
                    source_messages = parsed_source
            records.append(
                {
                    "original_id": int(row["id"]),
                    "content": str(row["text"] or ""),
                    "importance": clamp_float(
                        metadata.get("importance"), default=0.5
                    ),
                    "session_id": metadata.get("session_id"),
                    "persona_id": metadata.get("persona_id"),
                    "metadata": metadata,
                    "source_messages": source_messages,
                    "storage_created_at": row["created_at"],
                    "storage_updated_at": row["updated_at"],
                }
            )
        return records

    async def get_memory_import_keys(self) -> set[tuple[str, str, str]]:
        """Return duplicate keys for existing memories."""
        if self.db_connection is None:
            return set()
        cursor = await self.db_connection.execute(
            "SELECT text, metadata FROM documents"
        )
        rows = await cursor.fetchall()
        keys: set[tuple[str, str, str]] = set()
        for row in rows:
            metadata = self._safe_json_dict(row["metadata"])
            keys.add(
                memory_import_key(
                    str(row["text"] or ""),
                    metadata.get("session_id"),
                    metadata.get("persona_id"),
                )
            )
        return keys

    async def get_topic_candidates(
        self, scope: str, limit: int = 50
    ) -> list[dict[str, str]]:
        """Return exact reusable v3 topic concepts from active records in scope."""
        if self.canonical_store is not None:
            return await self.canonical_store.get_topic_candidates(scope, limit)
        if self.db_connection is None or not str(scope or "").strip():
            return []
        cursor = await self.db_connection.execute(
            """
            SELECT metadata
            FROM documents
            WHERE json_extract(metadata, '$.session_id') = ?
              AND COALESCE(json_extract(metadata, '$.status'), 'active') = 'active'
            ORDER BY CAST(json_extract(metadata, '$.create_time') AS REAL) DESC, id DESC
            LIMIT ?
            """,
            (scope, max(limit * 4, limit)),
        )
        rows = await cursor.fetchall()
        candidates: dict[str, dict[str, str]] = {}
        for row in rows:
            metadata = self._safe_json_dict(row["metadata"])
            refs = metadata.get("topic_refs")
            if isinstance(refs, list):
                for ref in refs:
                    if not isinstance(ref, dict):
                        continue
                    name = normalize_concept_name(
                        str(ref.get("name") or ref.get("final_name") or "")
                    )
                    if not name:
                        continue
                    candidates.setdefault(
                        concept_key(name),
                        {
                            "topic_id": str(ref.get("topic_id") or topic_id(scope, name)),
                            "name": name,
                        },
                    )
            if not refs and isinstance(metadata.get("topics"), list):
                for raw_name in metadata["topics"]:
                    name = normalize_concept_name(str(raw_name or ""))
                    if name:
                        candidates.setdefault(
                            concept_key(name),
                            {"topic_id": topic_id(scope, name), "name": name},
                        )
            if len(candidates) >= limit:
                break
        return list(candidates.values())[:limit]

    async def get_canonical_index_status(self) -> dict[str, Any]:
        if self.canonical_store is None:
            return {
                "consistent": False,
                "facts": 0,
                "fts": 0,
                "vectors": 0,
            }
        status = await self.canonical_store.index_status()
        return {
            "consistent": status.is_consistent,
            "facts": status.fact_count,
            "fts": status.fts_count,
            "vectors": status.vector_count,
        }

    async def rebuild_canonical_indexes(self) -> dict[str, int | bool]:
        if self.canonical_store is None:
            raise RuntimeError("canonical memory store is not initialized")
        return await self.canonical_store.rebuild_indexes()

    async def search_canonical_fact_candidates(
        self,
        query: str,
        *,
        limit: int = 10,
        session_id: str | None = None,
        persona_id: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Evaluate fact indexes without switching the S5 production route."""
        if self.canonical_store is None:
            return {"bm25": [], "vector": []}
        return await self.canonical_store.search_candidates(
            query,
            limit=limit,
            scope=session_id,
            persona_id=persona_id,
        )

    async def search_memories(
        self,
        query: str,
        k: int = 5,
        session_id: str | None = None,
        persona_id: str | None = None,
    ) -> list[HybridResult]:
        """
        检索相关记忆

        Args:
            query: 查询字符串
            k: 返回数量
            session_id: 会话ID过滤(可选,应传入unified_msg_origin完整格式)
            persona_id: 人格ID过滤(可选)

        Returns:
            List[HybridResult]: 检索结果列表
        """
        if not query or not query.strip():
            return []

        cache_key = self._search_cache_key(query, k, session_id, persona_id)
        cached_results = self._get_cached_search_results(cache_key)
        if cached_results is not None:
            fact_ids = [
                str(result.metadata.get("fact_id") or "")
                for result in cached_results
                if isinstance(result.metadata, dict)
            ]
            if self.canonical_store is not None and fact_ids:
                self._create_tracked_task(
                    self.canonical_store.record_fact_event(fact_ids, "retrieved")
                )
            return cached_results

        # 如果session_id是unified_msg_origin格式，自动触发旧数据迁移
        if (
            session_id
            and ":" in session_id
            and not session_id.startswith("livingmemory:")
        ):
            # 异步触发迁移，不阻塞查询
            self._create_tracked_task(self._migrate_session_data_if_needed(session_id))

        # 【关键修改】不再提取UUID，直接使用完整的unified_msg_origin进行匹配
        # 因为现在数据库中存储的就是完整格式
        # session_id 和 persona_id 保持原样传递给检索器

        if self.fact_retriever is None:
            # Unit/maintenance adapters may inject only the legacy retriever
            # without calling initialize(). Production initialization always
            # installs CanonicalFactRetriever above.
            retriever = self.dual_route_retriever or self.hybrid_retriever
            if retriever is None:
                raise RuntimeError("canonical fact retriever is not initialized")
            results = await retriever.search(query, k, session_id, persona_id)
            results = self._filter_by_retrieval_policy(results)
            results = await self._merge_recent_memories(
                results, k, session_id, persona_id
            )
            if results:
                self._create_tracked_task(
                    self._update_access_times_internal([r.doc_id for r in results])
                )
            self._set_cached_search_results(cache_key, results)
            return results
        bundle = await self.fact_retriever.search(
            query,
            limit=max(1, int(k)),
            scope=session_id,
            persona_id=persona_id,
        )
        results = bundle.hits

        # S6: entering the accepted candidate list is only "retrieved". It no
        # longer reinforces decay; the injection adapter records "injected".
        fact_ids = [
            str(result.metadata.get("fact_id") or "")
            for result in results
            if isinstance(result.metadata, dict)
        ]
        if self.canonical_store is not None and fact_ids:
            self._create_tracked_task(
                self.canonical_store.record_fact_event(fact_ids, "retrieved")
            )

        self._set_cached_search_results(cache_key, results)
        return results

    async def explain_memory_search(
        self,
        query: str,
        k: int = 5,
        session_id: str | None = None,
        persona_id: str | None = None,
    ) -> dict[str, Any]:
        """Run the production fact policy without mutating lifecycle counters."""
        if self.fact_retriever is None or not str(query or "").strip():
            return {
                "results": [],
                "candidate_count": 0,
                "rejected": [],
                "explanation": "empty_query",
            }
        bundle = await self.fact_retriever.search(
            query,
            limit=max(1, int(k)),
            scope=session_id,
            persona_id=persona_id,
        )
        return {
            "results": bundle.hits,
            "candidate_count": bundle.candidate_count,
            "rejected": bundle.rejected,
            "explanation": bundle.explanation,
        }

    def pack_memory_hits(self, results: list[HybridResult]):
        """Apply the same hard budget used by automatic and Agent injection."""
        from ..utils.fact_packing import pack_fact_hits

        return pack_fact_hits(
            results,
            token_budget=int(self.config.get("injection_token_budget", 1600)),
            single_fact_budget=int(self.config.get("single_fact_token_budget", 260)),
            include_reaction=bool(self.config.get("include_persona_reaction", True)),
        )

    async def mark_memories_injected(self, results: list[HybridResult]) -> int:
        """Record only facts that were actually placed in the model request."""
        if self.canonical_store is None:
            return 0
        fact_ids = [
            str(result.metadata.get("fact_id") or "")
            for result in results
            if isinstance(result.metadata, dict) and result.metadata.get("fact_id")
        ]
        return await self.canonical_store.record_fact_event(fact_ids, "injected")

    async def get_memory(self, memory_id: int) -> dict[str, Any] | None:
        """
        根据ID获取记忆

        Args:
            memory_id: 记忆ID

        Returns:
            Optional[Dict]: 记忆数据,包含text和metadata
        """
        # 从faiss_db的document_storage获取文档
        try:
            # 使用 get_documents (复数) 并传入 ids 参数
            docs = await self.faiss_db.document_storage.get_documents(
                metadata_filters={}, ids=[memory_id], limit=1
            )

            if not docs or len(docs) == 0:
                return None

            doc = docs[0]
            metadata = self._safe_json_dict(doc.get("metadata"))
            if (
                self.canonical_store is not None
                and metadata.get("memory_schema_version") == "v3"
                and metadata.get("parent_id")
            ):
                facts = await self.canonical_store.get_facts_by_document(memory_id)
                metadata["key_facts"] = facts
                topic_refs: list[dict[str, Any]] = []
                participant_refs: list[dict[str, Any]] = []
                for fact in facts:
                    for ref in fact.get("topic_refs", []) or []:
                        if isinstance(ref, dict) and ref.get("topic_id") not in {
                            item.get("topic_id") for item in topic_refs
                        }:
                            topic_refs.append(ref)
                    for ref in fact.get("participant_refs", []) or []:
                        if isinstance(ref, dict) and ref.get("participant_id") not in {
                            item.get("participant_id") for item in participant_refs
                        }:
                            participant_refs.append(ref)
                metadata["topic_refs"] = topic_refs
                metadata["topics"] = [
                    str(item.get("name")) for item in topic_refs if item.get("name")
                ]
                metadata["participant_refs"] = participant_refs
                metadata["participants"] = [
                    str(item.get("name"))
                    for item in participant_refs
                    if item.get("name")
                ]
            return {
                "id": doc["id"],
                "text": doc["text"],
                "metadata": metadata,
            }
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("[MemoryEngine] 获取记忆详情失败", exc_info=True)
            return None

    async def update_memory(
        self,
        memory_id: int,
        updates: dict[str, Any],
    ) -> bool:
        """
        更新记忆（确保多数据库同步）

        支持更新内容、重要性、元数据等。采用不同策略：
        - 内容更新：先创建后删除（避免数据丢失）+ 全库同步
        - 元数据更新：三库同步更新

        Args:
            memory_id: 记忆ID
            updates: 更新字典,可包含:
                - content: 新内容 (触发完整重建)
                - importance: 新重要性
                - metadata: 元数据更新

        Returns:
            bool: 是否更新成功
        """
        # 获取当前记忆
        memory = await self.get_memory(memory_id)
        if not memory:
            logger.error(f"[更新] 记忆不存在 (memory_id={memory_id})")
            return False

        # 解析 metadata（可能是JSON字符串）
        current_metadata = memory.get("metadata", {})
        if isinstance(current_metadata, str):
            import json

            try:
                current_metadata = json.loads(current_metadata)
            except (json.JSONDecodeError, TypeError):
                current_metadata = {}
        elif not isinstance(current_metadata, dict):
            current_metadata = {}

        # 处理内容更新 (需要重建所有索引)
        if "content" in updates:
            new_content = updates["content"]
            if not new_content or not new_content.strip():
                return False

            try:
                importance = clamp_float(
                    updates.get("importance", current_metadata.get("importance", 0.5)),
                    default=0.5,
                )

                # 构建新元数据
                new_metadata = current_metadata.copy()
                metadata_patch = updates.get("metadata")
                if isinstance(metadata_patch, dict):
                    new_metadata.update(metadata_patch)
                new_metadata["updated_at"] = time.time()
                new_metadata["previous_id"] = memory_id  # 记录旧ID
                new_metadata["importance"] = importance

                # 【改进】先创建新记忆，再删除旧记忆（避免数据丢失）
                logger.info(f"[更新] 开始内容更新流程 (old_id={memory_id})")

                new_memory_id = await self.replace_memory(
                    memory_id,
                    content=new_content,
                    importance=importance,
                    metadata=new_metadata,
                )

                logger.info(
                    f"[更新] 内容更新完成 (old_id={memory_id} → new_id={new_memory_id})"
                )
                self._invalidate_search_cache()
                return True

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"[更新] 内容更新失败 (memory_id={memory_id}): {e}", exc_info=True
                )
                return False

        # 处理非内容的元数据更新（不需要重建索引）
        metadata_updates = {}

        if "importance" in updates:
            metadata_updates["importance"] = clamp_float(
                updates["importance"], default=0.5
            )

        if "metadata" in updates:
            metadata_updates.update(updates["metadata"])

        if metadata_updates:
            # 确保 current_metadata 是字典（再次检查）
            if not isinstance(current_metadata, dict):
                import json

                try:
                    current_metadata = (
                        json.loads(current_metadata)
                        if isinstance(current_metadata, str)
                        else {}
                    )
                except (json.JSONDecodeError, TypeError):
                    current_metadata = {}

            # 合并元数据
            current_metadata.update(metadata_updates)
            current_metadata["updated_at"] = time.time()

            # 【改进】使用增强的update_metadata确保三库同步
            if self.hybrid_retriever is None:
                logger.error("混合检索器未初始化")
                return False
            success = await self.hybrid_retriever.update_metadata(
                memory_id, metadata_updates
            )

            if success:
                logger.info(f"[更新] 元数据更新成功 (memory_id={memory_id})")
                if (
                    self.canonical_store is not None
                    and "importance" in metadata_updates
                ):
                    await self.canonical_store.update_document_importance(
                        memory_id,
                        float(metadata_updates["importance"]),
                    )
                if self.graph_memory_manager is not None:
                    await self.graph_memory_manager.index_memory(
                        memory_id,
                        memory["text"],
                        current_metadata,
                    )
                self._invalidate_search_cache()
            else:
                logger.error(f"[更新] 元数据更新失败 (memory_id={memory_id})")

            return success

        return True

    async def replace_memory(
        self,
        memory_id: int,
        *,
        content: str,
        metadata: dict[str, Any],
        importance: float,
    ) -> int:
        """Replace one memory and rebuild all derived data with a new ID."""
        current = await self.get_memory(memory_id)
        if not current:
            raise ValueError(f"记忆不存在 (memory_id={memory_id})")
        if not content or not content.strip():
            raise ValueError("记忆内容不能为空")

        current_metadata = self._safe_json_dict(current.get("metadata"))
        source_messages = await self.get_memory_source(memory_id)
        replacement_metadata = current_metadata.copy()
        replacement_metadata.update(metadata or {})
        replacement_metadata["previous_id"] = memory_id
        replacement_metadata["updated_at"] = time.time()
        replacement_metadata["create_time"] = current_metadata.get("create_time")
        normalized_importance = clamp_float(importance, default=0.5)
        replacement_metadata["importance"] = normalized_importance

        session_id = replacement_metadata.get("session_id")
        persona_id = replacement_metadata.get("persona_id")
        # S4: atoms are retired (S4-03B); replace no longer regenerates them.
        atoms = None

        new_memory_id: int | None = None
        add_task = self._create_tracked_task(
            self.add_memory(
                content=content,
                session_id=session_id,
                persona_id=persona_id,
                importance=normalized_importance,
                metadata=replacement_metadata,
                atoms=atoms,
                preserve_create_time=True,
                source_messages=source_messages or None,
            )
        )
        try:
            new_memory_id = await asyncio.shield(add_task)
            if new_memory_id is None:
                raise RuntimeError("新记忆创建失败")
            if not await self.delete_memory(memory_id):
                await self.delete_memory(new_memory_id)
                new_memory_id = None
                raise RuntimeError("旧记忆删除失败，已回滚新记忆")
            self._invalidate_search_cache()
            return new_memory_id
        except asyncio.CancelledError:
            if new_memory_id is None:
                try:
                    new_memory_id = await asyncio.shield(add_task)
                except Exception:
                    new_memory_id = None
            if new_memory_id is not None:
                await asyncio.shield(self.delete_memory(new_memory_id))
            self._invalidate_search_cache()
            raise
        except Exception:
            if new_memory_id is not None:
                try:
                    if await self.get_memory(new_memory_id):
                        await self.delete_memory(new_memory_id)
                except Exception:
                    logger.error(
                        f"[更新] 回滚新记忆失败 (memory_id={new_memory_id})",
                        exc_info=True,
                    )
            self._invalidate_search_cache()
            raise

    async def delete_memory(self, memory_id: int) -> bool:
        """
        删除记忆

        Args:
            memory_id: 记忆ID

        Returns:
            bool: 是否删除成功
        """

        op_id = await self._start_write_op(
            "delete",
            {"memory_id": memory_id},
            memory_id=memory_id,
        )

        # hybrid_retriever.delete_memory() 内部已按顺序删除 BM25、向量索引和 documents 表
        if self.hybrid_retriever is None:
            logger.error("混合检索器未初始化")
            await self._advance_write_op(
                op_id,
                "document_delete_failed",
                status="failed",
                error="hybrid retriever not initialized",
            )
            return False
        success = await self.hybrid_retriever.delete_memory(memory_id)
        if not success:
            await self._advance_write_op(
                op_id,
                "document_delete_failed",
                status="failed",
                error="document/vector delete failed",
            )
            return False

        await self._advance_write_op(op_id, "document_deleted", memory_id=memory_id)
        if self.db_connection is not None:
            await self.db_connection.execute(
                "DELETE FROM memory_sources WHERE memory_id = ?", (memory_id,)
            )
            await self.db_connection.commit()

        needs_repair = False
        try:
            if self.canonical_store is not None:
                await self.canonical_store.delete_by_document(memory_id)
            await self._advance_write_op(
                op_id, "canonical_facts_deleted", memory_id=memory_id
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._advance_write_op(
                op_id,
                "canonical_fact_delete_failed",
                status="needs_repair",
                memory_id=memory_id,
                error=str(e),
            )
            needs_repair = True
            logger.error(
                f"[MemoryEngine] canonical facts 删除失败 (memory_id={memory_id})",
                exc_info=True,
            )
        try:
            if self.graph_memory_manager is not None:
                await self.graph_memory_manager.delete_memory(memory_id)
            await self._advance_write_op(op_id, "graph_deleted", memory_id=memory_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._advance_write_op(
                op_id,
                "graph_delete_failed",
                status="needs_repair",
                memory_id=memory_id,
                error=str(e),
            )
            needs_repair = True
            logger.error(
                f"[MemoryEngine] 图记忆删除失败，已标记待修复 (memory_id={memory_id})",
                exc_info=True,
            )

        try:
            if self.atom_store is not None:
                await self.atom_store.delete_by_parent(memory_id)
            await self._advance_write_op(op_id, "atoms_deleted", memory_id=memory_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._advance_write_op(
                op_id,
                "atom_delete_failed",
                status="needs_repair",
                memory_id=memory_id,
                error=str(e),
            )
            needs_repair = True
            logger.error(
                f"[MemoryEngine] 记忆原子删除失败，已标记待修复 (memory_id={memory_id})",
                exc_info=True,
            )

        if not needs_repair:
            await self._advance_write_op(
                op_id,
                "completed",
                status="completed",
                memory_id=memory_id,
            )
        self._invalidate_search_cache()
        return success

    async def rebuild_graph_index(self) -> dict[str, int]:
        """Stream active documents into a safe graph-memory rebuild."""
        if self.graph_memory_manager is None:
            return {"rebuilt": 0, "skipped": 0}

        if self.db_connection is None:
            raise RuntimeError("memory database is not initialized")

        async def active_memory_batches():
            last_id = 0
            batch_size = 200
            while True:
                cursor = await self.db_connection.execute(
                    """
                    SELECT id, text, metadata
                    FROM documents
                    WHERE id > ?
                      AND COALESCE(
                          json_extract(metadata, '$.status'), 'active'
                      ) = 'active'
                    ORDER BY id
                    LIMIT ?
                    """,
                    (last_id, batch_size),
                )
                rows = await cursor.fetchall()
                if not rows:
                    break

                batch: list[tuple[int, str, dict[str, Any]]] = []
                for row in rows:
                    metadata = row["metadata"] or {}
                    if isinstance(metadata, str):
                        try:
                            metadata = json.loads(metadata)
                        except (json.JSONDecodeError, TypeError):
                            metadata = {}
                    elif not isinstance(metadata, dict):
                        metadata = {}
                    # S3: canonical parents keep only source fields in the
                    # document projection; reattach their facts so online and
                    # rebuild graph writes use the same evidence source.
                    if (
                        metadata.get("memory_schema_version") == "v3"
                        and self.canonical_store is not None
                    ):
                        facts = await self.canonical_store.get_facts_by_document(
                            int(row["id"])
                        )
                        if facts:
                            metadata = dict(metadata)
                            metadata["key_facts"] = facts
                    batch.append((int(row["id"]), str(row["text"] or ""), metadata))

                last_id = int(rows[-1]["id"])
                yield batch

        self._invalidate_search_cache()
        return await self.graph_memory_manager.rebuild_memory_batches(
            active_memory_batches()
        )

    async def update_importance(self, memory_id: int, new_importance: float) -> bool:
        """
        更新记忆重要性

        Args:
            memory_id: 记忆ID
            new_importance: 新重要性值(0-1)

        Returns:
            bool: 是否更新成功
        """
        return await self.update_memory(memory_id, {"importance": new_importance})

    async def apply_daily_decay(self, decay_rate: float, days: int = 1) -> int:
        """Apply decay to authoritative facts, not their parent projection."""
        if decay_rate <= 0 or days <= 0:
            return 0
        if self.canonical_store is None:
            logger.error("[衰减] canonical fact store 未初始化")
            return 0
        try:
            affected = await self.canonical_store.apply_daily_decay(
                min(1.0, float(decay_rate)),
                days=int(days),
                protected_threshold=clamp_float(
                    self.config.get("protected_importance_threshold"), default=1.0
                ),
                injection_window_days=float(
                    self.config.get("access_decay_window_days", 30.0)
                ),
                max_injection_count=float(
                    self.config.get("access_decay_max_count", 10.0)
                ),
                count_decay_multiplier=float(
                    self.config.get("access_count_decay_multiplier", 0.5)
                ),
            )
            logger.info(
                f"[衰减] canonical facts 衰减完成: 衰减率={decay_rate}, "
                f"天数={days}, 影响事实={affected}"
            )
            self._invalidate_search_cache()
            return affected
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[衰减] 批量衰减失败: {e}", exc_info=True)
            return 0

    async def update_access_time(self, memory_id: int) -> bool:
        """
        更新最后访问时间

        Args:
            memory_id: 记忆ID

        Returns:
            bool: 是否更新成功
        """
        return await self._update_access_time_internal(memory_id)

    async def _update_access_time_internal(self, memory_id: int) -> bool:
        """Atomically bump a single memory's access time and count."""
        return await self._update_access_times_internal([memory_id])

    async def _update_access_times_internal(self, doc_ids: list[int]) -> bool:
        """Atomically bump access time and count for multiple memories in one UPDATE.

        Args:
            doc_ids: Document ids to update.

        Returns:
            bool: True if at least one row was updated.
        """
        unique_ids = list(dict.fromkeys(int(doc_id) for doc_id in doc_ids))
        if not unique_ids:
            return False

        current_time = time.time()

        try:
            if self.db_connection is None:
                return False

            # 单条原子 SQL，避免并发召回任务对同一记忆产生丢失更新，
            # 同时将多条结果合并为一次 commit 以降低写放大。
            placeholders = ",".join("?" * len(unique_ids))
            cursor = await self.db_connection.execute(
                f"""
                UPDATE documents
                SET metadata = CASE
                    WHEN json_valid(metadata) THEN json_set(
                        json_set(metadata, '$.last_access_time', ?),
                        '$.access_count',
                        MIN(
                            COALESCE(
                                CAST(json_extract(metadata, '$.access_count') AS INTEGER),
                                0
                            ) + 1,
                            1000000
                        )
                    )
                    ELSE json_set('{{}}', '$.last_access_time', ?, '$.access_count', 1)
                END
                WHERE id IN ({placeholders})
                """,
                (current_time, current_time, *unique_ids),
            )
            await self.db_connection.commit()

            return cursor.rowcount > 0

        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 记录错误但不影响查询流程
            logger.warning(
                f"批量更新访问时间失败 (doc_ids={unique_ids}): {e}",
                exc_info=True,
            )
            return False

    async def get_session_memories(
        self,
        session_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        获取会话的所有记忆（使用分批处理和数据库排序优化）

        Args:
            session_id: 会话ID(应传入完整的unified_msg_origin格式)
            limit: 限制数量

        Returns:
            List[Dict]: 记忆列表
        """
        # 【关键修改】不再提取UUID，直接使用完整的session_id进行匹配
        # 因为现在数据库中存储的就是完整的unified_msg_origin格式

        # 使用数据库层面的过滤、排序和分页，避免加载所有数据
        try:
            if self.db_connection is None:
                return []

            cursor = await self.db_connection.execute(
                """
                SELECT id, text, metadata
                FROM documents
                WHERE json_extract(metadata, '$.session_id') = ?
                ORDER BY CAST(json_extract(metadata, '$.create_time') AS REAL) DESC
                LIMIT ?
                """,
                (session_id, limit),
            )
            rows = await cursor.fetchall()

            safe_json_dict = self._safe_json_dict
            parsed = await asyncio.to_thread(
                lambda: [safe_json_dict(r["metadata"]) for r in rows]
            )

            memories = []
            for row, metadata in zip(rows, parsed):
                memories.append(
                    {
                        "id": int(row["id"]),
                        "text": row["text"],
                        "metadata": metadata,
                    }
                )

            return memories
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                f"[MemoryEngine] 获取会话记忆失败 (session_id={session_id})",
                exc_info=True,
            )
            return []
