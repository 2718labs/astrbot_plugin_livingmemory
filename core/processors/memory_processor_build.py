"""Build current storage records from admitted memory data."""

import json
from typing import Any

from ..models.conversation_models import Message
from ..models.memory_atom import MemoryAtom
from ..models.memory_contract import (
    MEMORY_GENERATION_VERSION,
    MEMORY_SCHEMA_VERSION,
    build_explicit_source_descriptor,
    build_source_descriptor,
    concept_key,
    memory_idempotency_key,
    normalize_concept_name,
    parent_memory_id,
    participant_id,
    source_ids_for_indexes,
    source_messages_for_indexes,
    stable_fact_id,
    topic_id,
    validate_and_normalize_time,
)
from ..models.memory_processing import InvalidMemoryOutputError, MemoryWriteRecord
from ..utils.memory_facts import fact_texts, unique_strings
from .atom_classifier import classify_metadata_atoms


class MemoryProcessorBuildMixin:
    """MemoryProcessor 拆分模块：MemoryProcessorBuildMixin"""

    def _build_storage_format(
        self,
        fallback_excerpt: str,
        structured_data: dict[str, Any],
        is_group_chat: bool,
    ) -> tuple[str, dict[str, Any]]:
        """
        构建存储格式

        Args:
            fallback_excerpt: 当摘要为空时使用的对话摘录
            structured_data: 结构化数据
            is_group_chat: 是否为群聊

        Returns:
            (content, metadata) 元组
        """
        summary = str(structured_data.get("summary", "")).strip()
        key_facts = structured_data.get("key_facts", [])

        # 自动总结链路在调用本方法前已经把 summary/key_facts 投影成获准事实；
        # 显式记忆工具仍可传入摘要和事实，沿用原有富文本兼容格式。
        rich_parts = [summary] if summary else []
        projected_facts = fact_texts(key_facts, limit=5)
        remaining_facts = [fact for fact in projected_facts if fact != summary]
        if remaining_facts:
            rich_parts.append("；".join(remaining_facts))
        rich_content = " | ".join(rich_parts)

        content = rich_content if rich_content else fallback_excerpt

        # canonical_summary：自定义提示词若输出了该字段则保留（供图抽取等使用），
        # 否则回退为与 content 一致的富文本，保持 v2 metadata 结构兼容。
        canonical_summary = str(structured_data.get("canonical_summary") or "").strip()
        if not canonical_summary:
            canonical_summary = rich_content

        # metadata字段:存储结构化信息
        # 注意：不要在这里设置 create_time 和 last_access_time
        # 这些字段会由 MemoryEngine.add_memory() 自动添加
        metadata = {
            "topics": structured_data.get("topics", []),
            "key_facts": key_facts,
            "sentiment": structured_data.get("sentiment", "neutral"),
            "interaction_type": "group_chat" if is_group_chat else "private_chat",
            # 双通道：canonical_summary 供图抽取等中性文本消费方使用，
            # persona_summary 保留人格风格摘要供面板展示
            "canonical_summary": canonical_summary,
            "persona_summary": summary,
            "summary_schema_version": "v2",
            # summary_quality 由具体调用链在写入前确定。
        }

        if is_group_chat and "participants" in structured_data:
            metadata["participants"] = structured_data["participants"]

        return content, metadata

    def _build_v3_storage_records(
        self,
        admitted_units: list[tuple[int, dict[str, Any]]],
        messages: list[Message],
        is_group_chat: bool,
        topic_candidates: list[dict[str, Any] | str] | None = None,
        source_scope: str | None = None,
    ) -> list[MemoryWriteRecord]:
        """Create one authoritative v3 record for each admitted centre."""
        scope = str(source_scope or messages[0].session_id or "").strip()
        source_window = build_source_descriptor(messages, scope=scope)
        candidate_topics: dict[str, dict[str, str]] = {}
        for candidate in topic_candidates or []:
            if isinstance(candidate, dict):
                name = normalize_concept_name(
                    str(candidate.get("name") or candidate.get("final_name") or "")
                )
                candidate_id = str(candidate.get("topic_id") or "").strip()
            else:
                name = normalize_concept_name(str(candidate))
                candidate_id = ""
            if name:
                candidate_topics[concept_key(name)] = {
                    "name": name,
                    "topic_id": candidate_id or topic_id(scope, name),
                }

        catalog = self._topic_catalog.setdefault(scope, {})
        for key, item in candidate_topics.items():
            catalog.setdefault(key, item)

        identities = self._extract_participant_identities(messages)
        identity_lookup: dict[str, dict[str, Any]] = {}
        for identity in identities:
            aliases = [
                identity.get("display_name"),
                identity.get("sender_id"),
                *(identity.get("aliases") or []),
            ]
            for alias in aliases:
                key = concept_key(str(alias or ""))
                if key:
                    identity_lookup[key] = identity

        def _resolve_topic(raw_name: str) -> dict[str, str]:
            raw = normalize_concept_name(raw_name)
            key = concept_key(raw)
            existing = catalog.get(key)
            if existing:
                final_name = existing["name"]
                resolved_id = existing["topic_id"]
                decision = "reused"
            else:
                final_name = raw
                resolved_id = topic_id(scope, final_name)
                decision = "created"
                catalog[key] = {"name": final_name, "topic_id": resolved_id}
            return {
                "topic_id": resolved_id,
                "raw_name": raw,
                "name": final_name,
                "decision": decision,
            }

        def _resolve_participant(raw_name: str) -> dict[str, Any]:
            name = normalize_concept_name(raw_name)
            identity = identity_lookup.get(concept_key(name))
            if identity:
                return {
                    "participant_id": str(identity["identity_key"]),
                    "name": str(identity["display_name"]),
                    "identity_key": str(identity["identity_key"]),
                    "source": "message_sender",
                }
            return {
                "participant_id": participant_id(scope, name),
                "name": name,
                "identity_key": None,
                "source": "mentioned",
            }

        records: list[MemoryWriteRecord] = []
        seen_unit_keys: dict[str, int] = {}
        for original_unit_index, unit in admitted_units:
            prepared_facts: list[dict[str, Any]] = []
            for candidate in unit["key_facts"]:
                indexes = candidate["source_indexes"]
                try:
                    direct_ids = source_ids_for_indexes(messages, indexes)
                    direct_messages = source_messages_for_indexes(messages, indexes)
                    normalized_time = validate_and_normalize_time(
                        candidate["time"], direct_messages
                    )
                except ValueError as exc:
                    raise InvalidMemoryOutputError(str(exc)) from exc
                topic_refs = [_resolve_topic(name) for name in candidate["topics"]]
                participant_refs = [
                    _resolve_participant(name) for name in candidate["participants"]
                ]
                prepared_facts.append(
                    {
                        "fact": candidate["fact"],
                        "topics": unique_strings(ref["name"] for ref in topic_refs),
                        "topic_refs": topic_refs,
                        "participants": unique_strings(
                            ref["name"] for ref in participant_refs
                        ),
                        "participant_refs": participant_refs,
                        "time": normalized_time,
                        "importance": candidate["importance"],
                        "source": candidate["source"],
                        "source_message_ids": direct_ids,
                        "persona_reaction": candidate["persona_reaction"],
                    }
                )

            unit_key: dict[str, Any] = {
                "source_message_ids": sorted(
                    {
                        str(source_id)
                        for fact in prepared_facts
                        for source_id in fact["source_message_ids"]
                    }
                ),
                "topic_ids": sorted(
                    {
                        ref["topic_id"]
                        for fact in prepared_facts
                        for ref in fact["topic_refs"]
                    }
                ),
                "participant_ids": sorted(
                    {
                        ref["participant_id"]
                        for fact in prepared_facts
                        for ref in fact["participant_refs"]
                    }
                ),
                "times": sorted(
                    {
                        str(fact["time"]["normalized"])
                        for fact in prepared_facts
                        if fact["time"]
                    }
                ),
            }
            serialized_unit_key = json.dumps(
                unit_key, ensure_ascii=False, sort_keys=True
            )
            occurrence = seen_unit_keys.get(serialized_unit_key, 0)
            seen_unit_keys[serialized_unit_key] = occurrence + 1
            if occurrence:
                unit_key["occurrence"] = occurrence
                unit_key["source_order"] = original_unit_index

            parent_id = parent_memory_id(source_window["fingerprint"], unit_key)
            signature_counts: dict[str, int] = {}
            for fact in prepared_facts:
                fact_key: dict[str, Any] = {
                    "source_message_ids": [str(item) for item in fact["source_message_ids"]],
                    "topic_ids": [ref["topic_id"] for ref in fact["topic_refs"]],
                    "participant_ids": [
                        ref["participant_id"] for ref in fact["participant_refs"]
                    ],
                    "time": fact["time"],
                    "source": fact["source"],
                }
                serialized_fact_key = json.dumps(
                    fact_key, ensure_ascii=False, sort_keys=True
                )
                fact_occurrence = signature_counts.get(serialized_fact_key, 0)
                signature_counts[serialized_fact_key] = fact_occurrence + 1
                if fact_occurrence:
                    fact_key["occurrence"] = fact_occurrence
                fact["parent_id"] = parent_id
                fact["fact_id"] = stable_fact_id(parent_id, fact_key)

            texts = [fact["fact"] for fact in prepared_facts]
            summary = texts[0]
            content = "；".join(texts)
            document_topic_refs: list[dict[str, str]] = []
            document_participant_refs: list[dict[str, Any]] = []
            for fact in prepared_facts:
                for ref in fact["topic_refs"]:
                    if ref["topic_id"] not in {
                        item["topic_id"] for item in document_topic_refs
                    }:
                        document_topic_refs.append(ref)
                for ref in fact["participant_refs"]:
                    if ref["participant_id"] not in {
                        item["participant_id"] for item in document_participant_refs
                    }:
                        document_participant_refs.append(ref)

            metadata: dict[str, Any] = {
                "memory_schema_version": MEMORY_SCHEMA_VERSION,
                "summary_schema_version": MEMORY_SCHEMA_VERSION,
                "generation_version": MEMORY_GENERATION_VERSION,
                "parent_id": parent_id,
                "idempotency_key": memory_idempotency_key(
                    source_window["fingerprint"], unit_key
                ),
                "summary": summary,
                "canonical_summary": summary,
                "topics": [item["name"] for item in document_topic_refs],
                "topic_refs": document_topic_refs,
                "participants": [
                    item["name"] for item in document_participant_refs
                ],
                "participant_refs": document_participant_refs,
                "participant_identities": identities,
                "key_facts": prepared_facts,
                "sentiment": unit["sentiment"],
                "interaction_type": (
                    "group_chat" if is_group_chat else "private_chat"
                ),
                "source_window": dict(source_window),
                "source_session_id": scope,
                "summary_quality": "normal",
            }
            records.append(
                MemoryWriteRecord(
                    content=content,
                    metadata=metadata,
                    importance=max(fact["importance"] for fact in prepared_facts),
                )
            )
        return records

    def build_explicit_memory_record(
        self,
        *,
        memory: str,
        source_scope: str,
        topics: list[str] | None = None,
        key_facts: list[str] | None = None,
        participants: list[str] | None = None,
        sentiment: str = "neutral",
        importance: float = 0.7,
        topic_candidates: list[dict[str, Any] | str] | None = None,
        source_reference: str | int | None = None,
        origin: str = "agent_memorize_tool",
        is_group_chat: bool = False,
    ) -> MemoryWriteRecord:
        """Build an explicit remember request into the same v3 fact contract."""
        overview = str(memory or "").strip()
        scope = str(source_scope or "").strip()
        if not overview or not scope:
            raise ValueError("explicit memory requires content and scope")

        normalized_importance = self._validate_importance(importance)
        normalized_sentiment = str(sentiment or "neutral").strip().lower()
        if normalized_sentiment not in {"positive", "neutral", "negative"}:
            normalized_sentiment = "neutral"
        fact_values = fact_texts(key_facts or [], limit=5) or [overview]
        fact_values = unique_strings(fact_values)[:5]
        topic_names = unique_strings(
            normalize_concept_name(item) for item in (topics or []) if item
        )[:5]
        participant_names = unique_strings(
            normalize_concept_name(item) for item in (participants or []) if item
        )[:8]

        topic_catalog: dict[str, dict[str, str]] = {}
        for candidate in topic_candidates or []:
            if isinstance(candidate, dict):
                name = normalize_concept_name(
                    str(candidate.get("name") or candidate.get("final_name") or "")
                )
                resolved_id = str(candidate.get("topic_id") or "").strip()
            else:
                name = normalize_concept_name(str(candidate))
                resolved_id = ""
            if name:
                topic_catalog[concept_key(name)] = {
                    "topic_id": resolved_id or topic_id(scope, name),
                    "name": name,
                }
        topic_refs: list[dict[str, str]] = []
        for name in topic_names:
            existing = topic_catalog.get(concept_key(name))
            topic_refs.append(
                {
                    "topic_id": (
                        existing["topic_id"] if existing else topic_id(scope, name)
                    ),
                    "raw_name": name,
                    "name": existing["name"] if existing else name,
                    "decision": "reused" if existing else "created",
                }
            )
        participant_refs = [
            {
                "participant_id": participant_id(scope, name),
                "name": name,
                "identity_key": None,
                "source": "explicit",
            }
            for name in participant_names
        ]

        source_window = build_explicit_source_descriptor(
            scope,
            {
                "overview": overview,
                "facts": fact_values,
                "topics": topic_names,
                "participants": participant_names,
            },
            origin=origin,
            source_reference=source_reference,
        )
        unit_key = {
            "source_message_ids": [str(item) for item in source_window["message_ids"]],
            "topic_ids": [item["topic_id"] for item in topic_refs],
            "participant_ids": [item["participant_id"] for item in participant_refs],
            "origin": origin,
        }
        parent_id = parent_memory_id(source_window["fingerprint"], unit_key)
        prepared_facts: list[dict[str, Any]] = []
        for index, text in enumerate(fact_values):
            fact_key = {
                "source_message_ids": [
                    str(item) for item in source_window["message_ids"]
                ],
                "fact": concept_key(text),
                "index": index,
            }
            prepared_facts.append(
                {
                    "fact_id": stable_fact_id(parent_id, fact_key),
                    "parent_id": parent_id,
                    "fact": text,
                    "topics": [item["name"] for item in topic_refs],
                    "topic_refs": topic_refs,
                    "participants": participant_names,
                    "participant_refs": participant_refs,
                    "time": None,
                    "importance": normalized_importance,
                    "source": "user_explicit",
                    "source_message_ids": list(source_window["message_ids"]),
                    "persona_reaction": None,
                }
            )

        metadata: dict[str, Any] = {
            "memory_schema_version": MEMORY_SCHEMA_VERSION,
            "summary_schema_version": MEMORY_SCHEMA_VERSION,
            "generation_version": MEMORY_GENERATION_VERSION,
            "parent_id": parent_id,
            "idempotency_key": memory_idempotency_key(
                source_window["fingerprint"], unit_key
            ),
            "summary": overview,
            "canonical_summary": overview,
            "topics": [item["name"] for item in topic_refs],
            "topic_refs": topic_refs,
            "participants": participant_names,
            "participant_refs": participant_refs,
            "key_facts": prepared_facts,
            "sentiment": normalized_sentiment,
            "interaction_type": "group_chat" if is_group_chat else "private_chat",
            "source_window": source_window,
            "source_session_id": scope,
            "summary_quality": "normal",
            "memory_origin": origin,
        }
        return MemoryWriteRecord(
            content="；".join(fact_values),
            metadata=metadata,
            importance=normalized_importance,
        )

    def classify_atoms_from_metadata(
        self,
        metadata: dict[str, Any],
        parent_importance: float = 0.5,
        session_id: str | None = None,
        persona_id: str | None = None,
    ) -> list[MemoryAtom]:
        """Generate time-aware memory atoms from key_facts in metadata.

        This is a post-processing step after process_conversation().
        It does NOT make additional LLM calls — classification is rule-based.
        """
        if not self.config.get("atom_enabled", True):
            return []
        if not metadata.get("key_facts"):
            return []
        atoms = classify_metadata_atoms(
            metadata=metadata,
            parent_importance=parent_importance,
            session_id=session_id,
            persona_id=persona_id,
        )
        metadata["atom_types"] = sorted({atom.atom_type.value for atom in atoms})
        return atoms

    async def merge_memories(self, memories: list[dict]) -> dict[str, Any]:
        """把一组零散记忆合并为一条精炼记忆（供记忆库整合使用）。

        Args:
            memories: 待合并的记忆列表，每条为 {"content": str, "metadata": dict}。

        Returns:
            包含 summary/key_facts/topics/importance 的字典。

        Raises:
            RuntimeError: LLM 不可用或解析失败时抛出。
        """
        items: list[dict[str, Any]] = []
        for i, mem in enumerate(memories, 1):
            metadata = mem.get("metadata") or {}
            summary = str(
                metadata.get("persona_summary")
                or str(mem.get("content", "")).strip()
            ).strip()
            items.append(
                {
                    "id": i,
                    "summary": summary,
                    "key_facts": metadata.get("key_facts") or [],
                    "topics": metadata.get("topics") or [],
                }
            )

        system_prompt = (
            "你是记忆整理助手。把多条关于同一主题或会话的零散记忆合并为一条精炼、"
            "信息无损的记忆摘要。保留所有关键事实与具体细节，去重并消除相互矛盾，"
            "避免泛化和丢失专有名词。只输出 JSON，不要输出任何其他内容。"
        )
        prompt = (
            f"以下是一组需要合并的记忆（共 {len(items)} 条）：\n"
            f"{json.dumps(items, ensure_ascii=False, indent=2)}\n\n"
            "请将它们合并为一条记忆，按如下 JSON 格式输出：\n"
            '{"summary": "合并后的精炼摘要", "key_facts": ["事实1", "事实2"], '
            '"topics": ["主题1"], "importance": 0.5}'
        )

        text = await self._call_llm_with_retry(prompt, system_prompt)
        data = self._parse_merge_response(text)

        summary = str(data.get("summary", "")).strip()
        if not summary:
            raise RuntimeError("合并结果缺少 summary")

        return {
            "summary": summary,
            "key_facts": self._ensure_list(data.get("key_facts", []))[:5],
            "topics": self._ensure_list(data.get("topics", []))[:5],
            "importance": self._validate_importance(data.get("importance", 0.5)),
        }

    def _parse_merge_response(self, text: str) -> dict[str, Any]:
        """解析合并 LLM 响应中的 JSON，失败时抛出异常。"""
        candidates = [text]
        fixed = self._try_fix_json(text)
        if fixed != text.strip():
            candidates.append(fixed)

        from ..utils import extract_json_from_response

        extracted = extract_json_from_response(text)
        if extracted != text.strip():
            candidates.append(extracted)

        last_error: Exception | None = None
        for candidate in candidates:
            try:
                data = json.loads(self._try_fix_json(candidate))
            except (json.JSONDecodeError, TypeError) as e:
                last_error = e
                continue
            if isinstance(data, dict):
                return data

        raise RuntimeError(f"合并结果 JSON 解析失败: {last_error}")
