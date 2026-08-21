"""Extract graph-memory structures from a stored memory document."""

from __future__ import annotations

import hashlib
from typing import Any

from ..models.memory_contract import (
    MEMORY_SCHEMA_VERSION,
    participant_id as participant_stable_id,
    topic_id as topic_stable_id,
)
from ..utils.memory_facts import fact_texts_from_metadata

from ..models.graph_models import ExtractedGraph, GraphEdge, GraphEntry, GraphNode
from .entity_resolver import EntityResolver


class GraphExtractor:
    """Turn memory summaries into nodes, edges, and searchable graph entries."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.max_topics = int(self.config.get("graph_max_topics", 6))
        self.max_participants = int(self.config.get("graph_max_participants", 8))
        self.max_facts = int(self.config.get("graph_max_facts", 8))

    def extract(
        self,
        source_memory_id: int,
        content: str,
        metadata: dict[str, Any] | None,
        atoms: list | None = None,
    ) -> ExtractedGraph:
        """Build a graph snapshot from one memory document.

        S3: only S1/S2 canonical facts (with their own topic/participant
        bindings) are evidence for graph edges. Legacy documents without
        explicit fact relations contribute nodes and searchable entries but
        no combinatorial edges.
        S4: the pre-canonical atom extraction path is retired (S4-03B);
        atoms are never produced or consumed by the new chain, so any
        atom payload is ignored here.
        """
        if (
            metadata
            and metadata.get("memory_schema_version") == MEMORY_SCHEMA_VERSION
            and metadata.get("key_facts")
        ):
            return self._extract_from_canonical(
                source_memory_id, metadata["key_facts"], metadata
            )
        return self._extract_legacy(source_memory_id, content, metadata)

    def _participant_nodes(
        self, metadata: dict[str, Any]
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """Return display name, canonical identity, and metadata for people."""
        resolved: list[tuple[str, str, dict[str, Any]]] = []
        identities = metadata.get("participant_identities")
        if isinstance(identities, list):
            seen: set[str] = set()
            for item in identities:
                if not isinstance(item, dict):
                    continue
                identity_key = EntityResolver.canonicalize(
                    str(item.get("identity_key") or "")
                )
                display_name = str(
                    item.get("display_name") or item.get("sender_id") or ""
                ).strip()
                if not identity_key or not display_name or identity_key in seen:
                    continue
                seen.add(identity_key)
                aliases = EntityResolver.dedupe_preserve_order(
                    [str(alias) for alias in item.get("aliases", []) if alias]
                )
                resolved.append(
                    (
                        display_name,
                        f"account:{identity_key}",
                        {
                            "identity_key": identity_key,
                            "sender_id": str(item.get("sender_id") or ""),
                            "platform": str(item.get("platform") or "unknown"),
                            "aliases": aliases,
                            "is_bot": bool(item.get("is_bot", False)),
                        },
                    )
                )
                if len(resolved) >= self.max_participants:
                    break
        if resolved:
            return resolved

        participants = EntityResolver.dedupe_preserve_order(
            [str(item) for item in metadata.get("participants", []) if item]
        )[: self.max_participants]
        return [
            (participant, EntityResolver.canonicalize(participant), {})
            for participant in participants
        ]

    def _extract_from_canonical(
        self,
        source_memory_id: int,
        facts: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> ExtractedGraph:
        """S3: build the graph only from explicit fact-level bindings.

        Every edge must trace back to one canonical fact: the fact's own
        topic_refs / participant_refs (S1/S2 stable IDs) decide the edges,
        and the edge carries the fact evidence. No cross-product edges, no
        summary/persona_reaction as evidence.
        """
        graph = ExtractedGraph()
        node_map: dict[str, GraphNode] = {}
        scope = str(
            metadata.get("source_session_id") or metadata.get("session_id") or ""
        ).strip()
        session_id = metadata.get("source_session_id") or metadata.get("session_id")
        persona_id = metadata.get("persona_id")
        summary = str(
            metadata.get("canonical_summary") or metadata.get("summary") or ""
        )
        importance = metadata.get("importance", 0.5)

        def _add_node(
            node_type: str,
            value: str,
            canonical_value: str,
            extra: dict[str, Any] | None = None,
        ) -> str:
            if not canonical_value or not str(value or "").strip():
                return ""
            node = GraphNode(
                node_type=node_type,
                value=str(value).strip(),
                canonical_value=canonical_value,
                metadata=extra or {},
            )
            node_map[node.node_key] = node
            return node.node_key

        def _entry_metadata(confidence: float, **extra: Any) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "source_memory_id": source_memory_id,
                "session_id": session_id,
                "persona_id": persona_id,
                "importance": importance,
                "create_time": metadata.get("create_time"),
                "last_access_time": metadata.get("last_access_time"),
                "canonical_summary": summary,
                "summary_schema_version": metadata.get("summary_schema_version"),
                "graph_confidence": confidence,
                "source_window": metadata.get("source_window"),
            }
            payload.update(extra)
            return payload

        for fact in facts:
            if not isinstance(fact, dict):
                continue
            fact_id = str(fact.get("fact_id") or "").strip()
            fact_text = str(fact.get("fact") or "").strip()
            parent_id = str(fact.get("parent_id") or "").strip()
            if not fact_id or not fact_text:
                continue
            fact_key = _add_node(
                "fact",
                fact_text,
                fact_id,
                {"fact_id": fact_id, "parent_id": parent_id},
            )
            if not fact_key:
                continue

            graph.entries.append(
                GraphEntry(
                    entry_key=hashlib.sha1(
                        f"fact|{source_memory_id}|{fact_id}|{fact_text}".encode("utf-8")
                    ).hexdigest(),
                    source_memory_id=source_memory_id,
                    session_id=session_id,
                    persona_id=persona_id,
                    entry_type="fact",
                    content=f"Fact: {fact_text}",
                    metadata=_entry_metadata(0.9, fact_id=fact_id),
                    node_keys=[fact_key],
                    relation_type="fact",
                )
            )

            evidence = [
                {
                    "source_memory_id": source_memory_id,
                    "fact_id": fact_id,
                    "parent_id": parent_id,
                    "source_message_ids": [
                        str(item)
                        for item in (fact.get("source_message_ids") or [])
                        if item
                    ],
                }
            ]

            # Explicit topic bindings: topic -> fact (describes)
            topic_bindings = [
                (
                    str(ref.get("topic_id") or "").strip(),
                    str(ref.get("name") or ref.get("raw_name") or "").strip(),
                )
                for ref in (fact.get("topic_refs") or [])
                if isinstance(ref, dict)
            ]
            if not topic_bindings:
                topic_bindings = [
                    (
                        topic_stable_id(scope, str(name)),
                        str(name).strip(),
                    )
                    for name in (fact.get("topics") or [])
                    if str(name or "").strip()
                ]
            for topic_identifier, topic_name in topic_bindings:
                if not topic_identifier or not topic_name:
                    continue
                topic_key = _add_node(
                    "topic",
                    topic_name,
                    topic_identifier,
                    {"topic_id": topic_identifier},
                )
                if not topic_key:
                    continue
                graph.edges.append(
                    GraphEdge(
                        source_key=topic_key,
                        target_key=fact_key,
                        relation_type="describes",
                        source_memory_id=source_memory_id,
                        confidence=0.9,
                        metadata={"fact_id": fact_id, "summary": summary},
                        evidence=evidence,
                    )
                )
                graph.entries.append(
                    GraphEntry(
                        entry_key=hashlib.sha1(
                            (
                                f"edge|{source_memory_id}|describes|{topic_key}|"
                                f"{fact_key}|{fact_text}"
                            ).encode("utf-8")
                        ).hexdigest(),
                        source_memory_id=source_memory_id,
                        session_id=session_id,
                        persona_id=persona_id,
                        entry_type="edge",
                        content=(
                            f"Topic {topic_name} describes fact: {fact_text}"
                        ),
                        metadata=_entry_metadata(0.9, fact_id=fact_id),
                        node_keys=[topic_key, fact_key],
                        relation_type="describes",
                    )
                )

            # Explicit participant bindings: person -> fact (mentioned_in)
            participant_bindings = [
                (
                    str(ref.get("participant_id") or "").strip(),
                    str(ref.get("name") or "").strip(),
                )
                for ref in (fact.get("participant_refs") or [])
                if isinstance(ref, dict)
            ]
            if not participant_bindings:
                participant_bindings = [
                    (
                        participant_stable_id(scope, str(name)),
                        str(name).strip(),
                    )
                    for name in (fact.get("participants") or [])
                    if str(name or "").strip()
                ]
            for participant_identifier, participant_name in participant_bindings:
                if not participant_identifier or not participant_name:
                    continue
                person_key = _add_node(
                    "person",
                    participant_name,
                    participant_identifier,
                    {"participant_id": participant_identifier},
                )
                if not person_key:
                    continue
                graph.edges.append(
                    GraphEdge(
                        source_key=person_key,
                        target_key=fact_key,
                        relation_type="mentioned_in",
                        source_memory_id=source_memory_id,
                        confidence=0.9,
                        metadata={"fact_id": fact_id, "summary": summary},
                        evidence=evidence,
                    )
                )
                graph.entries.append(
                    GraphEntry(
                        entry_key=hashlib.sha1(
                            (
                                f"edge|{source_memory_id}|mentioned_in|{person_key}|"
                                f"{fact_key}|{fact_text}"
                            ).encode("utf-8")
                        ).hexdigest(),
                        source_memory_id=source_memory_id,
                        session_id=session_id,
                        persona_id=persona_id,
                        entry_type="edge",
                        content=(
                            f"Participant {participant_name} is linked to "
                            f"fact: {fact_text}"
                        ),
                        metadata=_entry_metadata(0.9, fact_id=fact_id),
                        node_keys=[person_key, fact_key],
                        relation_type="mentioned_in",
                    )
                )

        graph.nodes = list(node_map.values())
        return graph

    def _extract_legacy(
        self,
        source_memory_id: int,
        content: str,
        metadata: dict[str, Any] | None,
    ) -> ExtractedGraph:
        """Original graph extraction from metadata (backward-compatible path)."""
        metadata = metadata or {}
        graph = ExtractedGraph()

        session_id = metadata.get("session_id")
        persona_id = metadata.get("persona_id")
        summary = metadata.get("canonical_summary") or content

        topics = EntityResolver.dedupe_preserve_order(
            [str(item) for item in metadata.get("topics", []) if item]
        )[: self.max_topics]
        participants = self._participant_nodes(metadata)
        key_facts = EntityResolver.dedupe_preserve_order(
            fact_texts_from_metadata(metadata, limit=self.max_facts)
        )[: self.max_facts]

        if not key_facts and summary:
            key_facts = [summary]

        node_map: dict[str, GraphNode] = {}

        def _add_node(
            node_type: str, value: str, extra: dict[str, Any] | None = None
        ) -> str:
            canonical_value = EntityResolver.canonicalize(value)
            if not canonical_value:
                return ""
            node = GraphNode(
                node_type=node_type,
                value=value.strip(),
                canonical_value=canonical_value,
                metadata=extra or {},
            )
            node_map[node.node_key] = node
            return node.node_key

        topic_keys = [_add_node("topic", topic) for topic in topics]
        participant_keys = []
        for participant, canonical_value, identity_metadata in participants:
            node = GraphNode(
                node_type="person",
                value=participant,
                canonical_value=canonical_value,
                metadata=identity_metadata,
            )
            node_map[node.node_key] = node
            participant_keys.append(node.node_key)
        fact_keys = [
            _add_node("fact", fact, {"summary": summary}) for fact in key_facts
        ]

        topic_keys = [item for item in topic_keys if item]
        participant_keys = [item for item in participant_keys if item]
        fact_keys = [item for item in fact_keys if item]

        graph.nodes.extend(node_map.values())

        def _add_entry(
            entry_type: str,
            content_text: str,
            node_keys: list[str],
            relation_type: str | None = None,
            confidence: float = 0.8,
        ) -> None:
            payload = (
                f"{entry_type}|{source_memory_id}|{relation_type or ''}|"
                f"{'|'.join(node_keys)}|{content_text}"
            )
            entry_key = hashlib.sha1(payload.encode("utf-8")).hexdigest()
            entry_metadata = {
                "source_memory_id": source_memory_id,
                "session_id": session_id,
                "persona_id": persona_id,
                "importance": metadata.get("importance", 0.5),
                "create_time": metadata.get("create_time"),
                "last_access_time": metadata.get("last_access_time"),
                "canonical_summary": summary,
                "summary_schema_version": metadata.get("summary_schema_version"),
                "graph_confidence": confidence,
                "source_window": metadata.get("source_window"),
            }
            graph.entries.append(
                GraphEntry(
                    entry_key=entry_key,
                    source_memory_id=source_memory_id,
                    session_id=session_id,
                    persona_id=persona_id,
                    entry_type=entry_type,
                    content=content_text,
                    metadata=entry_metadata,
                    node_keys=node_keys,
                    relation_type=relation_type,
                )
            )

        for fact_key in fact_keys:
            fact_value = node_map[fact_key].value
            _add_entry(
                "fact",
                f"Fact: {fact_value}. Summary: {summary}",
                [fact_key],
                relation_type="fact",
                confidence=0.9,
            )

        for topic_key in topic_keys:
            topic_value = node_map[topic_key].value
            _add_entry(
                "topic",
                f"Topic: {topic_value}. Summary: {summary}",
                [topic_key],
                relation_type="topic",
                confidence=0.75,
            )

        for person_key in participant_keys:
            person_value = node_map[person_key].value
            _add_entry(
                "participant",
                f"Participant: {person_value}. Summary: {summary}",
                [person_key],
                relation_type="participant",
                confidence=0.7,
            )

        # S3: legacy documents carry no explicit fact-level bindings, so they
        # contribute nodes and searchable entries but no combinatorial edges.
        # Topic x fact, person x fact and person x person cross products are
        # removed (I09); edges only come from the canonical fact path.

        if not graph.entries and summary:
            summary_key = _add_node("summary", summary)
            if summary_key:
                graph.nodes = list(node_map.values())
                _add_entry(
                    "summary",
                    f"Summary: {summary}",
                    [summary_key],
                    relation_type="summary",
                    confidence=0.6,
                )

        return graph


__all__ = ["GraphExtractor"]
