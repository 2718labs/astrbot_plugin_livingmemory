"""Production retrieval over authoritative canonical facts (S5)."""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..utils.short_query_match import (
    is_weak_short_cjk_query,
    short_cjk_phrase_score,
)
from .hybrid_retriever import HybridResult


@dataclass(slots=True)
class FactSearchBundle:
    """Fact candidates plus the reasons that candidates were kept or rejected."""

    hits: list[HybridResult] = field(default_factory=list)
    candidate_count: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)
    explanation: str = ""


class CanonicalFactRetriever:
    """Retrieve, calibrate and abstain over the one authoritative fact layer."""

    _LIGHTWEIGHT_MESSAGES = {
        "哈",
        "哈哈",
        "哈哈哈",
        "笑死",
        "嗯",
        "嗯嗯",
        "哦",
        "噢",
        "好",
        "好的",
        "行",
        "在吗",
        "你在吗",
        "早",
        "早安",
        "晚安",
        "拜拜",
        "再见",
        "谢谢",
        "谢了",
        "hi",
        "hello",
        "hey",
        "bye",
        "goodnight",
        "thanks",
    }
    _MEMORY_CUES = (
        "记得",
        "还记得",
        "之前",
        "以前",
        "上次",
        "曾经",
        "我说过",
        "你说过",
        "答应",
        "约定",
        "来着",
        "remember",
        "last time",
        "before",
        "previously",
    )

    # 重要性宽容准入（可选开关 importance_grace_enabled）：
    # 写死的高价值判定与放宽幅度，不对外暴露数值配置。
    _GRACE_THRESHOLD = 0.8
    _GRACE_FACTOR = 0.8

    def __init__(
        self,
        canonical_store: Any,
        text_processor: Any,
        graph_retriever: Any | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.store = canonical_store
        self.text_processor = text_processor
        self.graph_retriever = graph_retriever
        self.config = config or {}
        self.min_lexical = float(self.config.get("fact_min_lexical_score", 0.34))
        self.min_vector = float(self.config.get("fact_min_vector_similarity", 0.62))
        self.min_graph = float(self.config.get("fact_min_graph_score", 0.62))
        self.min_final = float(self.config.get("fact_min_final_score", 0.42))
        self.min_importance = float(
            self.config.get("min_importance_for_retrieval", 0.0)
        )
        self.importance_grace_enabled = bool(
            self.config.get("importance_grace_enabled", False)
        )
        self.document_weight = float(self.config.get("document_route_weight", 0.65))
        self.graph_weight = float(self.config.get("graph_route_weight", 0.35))
        self.cross_route_bonus = float(self.config.get("cross_route_bonus", 0.08))

    @staticmethod
    def _normalized_lightweight_text(query: str) -> str:
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", query).casefold()

    @classmethod
    def query_gate_reason(cls, query: str) -> str | None:
        normalized = cls._normalized_lightweight_text(query)
        if not normalized:
            return "empty_or_non_text"
        if normalized in cls._LIGHTWEIGHT_MESSAGES:
            return "lightweight_message_without_history_reference"
        if is_weak_short_cjk_query(query):
            return "weak_standalone_reference_without_history"
        return None

    @classmethod
    def _has_memory_cue(cls, query: str) -> bool:
        normalized = query.casefold()
        return any(cue in normalized for cue in cls._MEMORY_CUES)

    @staticmethod
    def _clamp_score(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _lexical_score(query_tokens: list[str], fact_tokens: list[str]) -> float:
        query_set = {str(item).casefold() for item in query_tokens if str(item).strip()}
        fact_set = {str(item).casefold() for item in fact_tokens if str(item).strip()}
        if not query_set or not fact_set:
            return 0.0
        overlap = len(query_set & fact_set)
        if overlap == 0:
            return 0.0
        coverage = overlap / len(query_set)
        jaccard = overlap / len(query_set | fact_set)
        return min(1.0, 0.75 * coverage + 0.25 * jaccard)

    async def score_facts_lexically(
        self, query: str, fact_texts: list[str]
    ) -> list[float]:
        """Lexical-only relevance scores for a small closed fact set.

        Used by the recent-memory block to pick the 1-2 facts of the newest
        parent that are closest to the current topic.  No embedding call: the
        same tokenizer and overlap scoring as the production route.
        """
        cleaned = str(query or "").strip()
        if not cleaned or not fact_texts:
            return [0.0] * len(fact_texts)
        query_tokens = await self.text_processor.tokenize_async(
            cleaned, remove_stopwords=False
        )
        scores: list[float] = []
        for text in fact_texts:
            fact_tokens = await self.text_processor.tokenize_async(
                str(text or ""), remove_stopwords=False
            )
            token_score = self._lexical_score(query_tokens, fact_tokens)
            phrase_score = short_cjk_phrase_score(cleaned, str(text or ""))
            scores.append(max(token_score, phrase_score))
        return scores

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        scope: str | None = None,
        persona_id: str | None = None,
    ) -> FactSearchBundle:
        cleaned_query = str(query or "").strip()
        gate_reason = self.query_gate_reason(cleaned_query)
        if gate_reason:
            return FactSearchBundle(explanation=gate_reason)

        candidate_limit = max(int(limit), int(self.config.get("fact_candidate_k", 20)))
        fact_routes_coro = self.store.search_candidates(
            cleaned_query,
            limit=candidate_limit,
            scope=scope,
            persona_id=persona_id,
        )
        graph_coro = None
        if self.graph_retriever is not None and self.graph_weight > 0:
            graph_coro = self.graph_retriever.search(
                cleaned_query,
                candidate_limit,
                scope,
                persona_id,
            )

        if graph_coro is None:
            routes = await fact_routes_coro
            graph_results: list[Any] = []
        else:
            routes, graph_results = await asyncio.gather(
                fact_routes_coro,
                graph_coro,
            )

        bm25_map = {
            str(item.get("fact_id")): float(item.get("score", 0.0))
            for item in routes.get("bm25", [])
            if item.get("fact_id")
        }
        vector_map = {
            str(item.get("fact_id")): self._clamp_score(item.get("score"))
            for item in routes.get("vector", [])
            if item.get("fact_id")
        }
        phrase_map = {
            str(item.get("fact_id")): self._clamp_score(item.get("score"))
            for item in routes.get("phrase", [])
            if item.get("fact_id")
        }
        graph_map: dict[str, dict[str, float]] = {}
        for result in graph_results:
            metadata = getattr(result, "metadata", {}) or {}
            fact_id = str(metadata.get("fact_id") or "").strip()
            if not fact_id:
                continue
            keyword = self._clamp_score(getattr(result, "keyword_score", 0.0))
            vector = self._clamp_score(getattr(result, "vector_score", 0.0))
            raw = max(keyword, vector)
            if raw <= graph_map.get(fact_id, {}).get("signal", 0.0):
                continue
            graph_map[fact_id] = {
                "signal": raw,
                "keyword": keyword,
                "vector": vector,
            }

        fact_ids = list(
            dict.fromkeys([*phrase_map, *bm25_map, *vector_map, *graph_map])
        )
        records = await self.store.get_fact_records(fact_ids)
        if not records:
            return FactSearchBundle(
                candidate_count=len(fact_ids),
                explanation="no_active_canonical_fact_candidates",
            )

        query_tokens = await self.text_processor.tokenize_async(
            cleaned_query, remove_stopwords=False
        )
        tokenized_facts = await asyncio.gather(
            *(
                self.text_processor.tokenize_async(
                    str(record.get("search_text") or ""), remove_stopwords=False
                )
                for record in records.values()
            )
        )
        fact_tokens = {
            fact_id: tokens
            for fact_id, tokens in zip(records, tokenized_facts, strict=True)
        }

        memory_cue = self._has_memory_cue(cleaned_query)
        min_lexical = self.min_lexical * (0.75 if memory_cue else 1.0)
        min_vector = self.min_vector * (0.88 if memory_cue else 1.0)
        min_final = self.min_final * (0.82 if memory_cue else 1.0)
        now = time.time()
        hits: list[HybridResult] = []
        rejected: list[dict[str, Any]] = []

        for fact_id, record in records.items():
            fact = record["fact"]
            content = str(fact.get("fact") or "").strip()
            token_lexical = self._lexical_score(
                query_tokens, fact_tokens[fact_id]
            )
            phrase_lexical = (
                short_cjk_phrase_score(cleaned_query, content)
                if fact_id in phrase_map
                else 0.0
            )
            lexical = max(token_lexical, phrase_lexical)
            vector = vector_map.get(fact_id, 0.0)
            graph_values = graph_map.get(fact_id, {})
            graph = self._clamp_score(graph_values.get("signal", 0.0))
            importance = self._clamp_score(record.get("importance", 0.5))
            if importance < self.min_importance:
                rejected.append(
                    {
                        "fact_id": fact_id,
                        "parent_id": record["parent_id"],
                        "reason": "below_importance_threshold",
                        "importance": round(importance, 4),
                    }
                )
                continue

            # 重要性宽容准入：importance 达 0.8 的记忆放宽向量/词面准入门槛。
            # 写侧已按价值打分（0.9+ 承诺/边界、0.7 计划/偏好），
            # 读侧对高价值记忆多给一次机会；普通记忆门槛不变。
            grace = (
                self.importance_grace_enabled
                and importance >= self._GRACE_THRESHOLD
            )
            eff_min_lexical = (
                min_lexical * self._GRACE_FACTOR if grace else min_lexical
            )
            eff_min_vector = (
                min_vector * self._GRACE_FACTOR if grace else min_vector
            )
            standard_document_reliable = (
                lexical >= min_lexical
                or vector >= min_vector
                or (
                    lexical >= min_lexical * 0.6
                    and vector >= min_vector * 0.82
                )
            )
            document_reliable = (
                lexical >= eff_min_lexical
                or vector >= eff_min_vector
                or (
                    lexical >= eff_min_lexical * 0.6
                    and vector >= eff_min_vector * 0.82
                )
            )
            graph_reliable = graph >= self.min_graph
            grace_admitted = (
                grace
                and document_reliable
                and not standard_document_reliable
                and not graph_reliable
            )
            if not document_reliable and not graph_reliable:
                rejected.append(
                    {
                        "fact_id": fact_id,
                        "parent_id": record["parent_id"],
                        "reason": "insufficient_relevance_evidence",
                        "lexical": round(lexical, 4),
                        "token_lexical": round(token_lexical, 4),
                        "short_phrase": round(phrase_lexical, 4),
                        "vector": round(vector, 4),
                        "graph": round(graph, 4),
                    }
                )
                continue

            document_signal = max(lexical, vector) if document_reliable else 0.0
            graph_signal = graph if graph_reliable else 0.0
            if document_signal and graph_signal:
                total_weight = max(0.0001, self.document_weight + self.graph_weight)
                relevance = (
                    self.document_weight * document_signal
                    + self.graph_weight * graph_signal
                ) / total_weight
                relevance = min(1.0, relevance + self.cross_route_bonus)
                route = "fact+graph"
            elif document_signal:
                relevance = document_signal
                route = "fact"
            else:
                relevance = graph_signal
                route = "graph"

            doc_metadata = record.get("document_metadata") or {}
            create_time = float(
                doc_metadata.get("create_time") or record.get("created_at") or now
            )
            days_old = max(0.0, (now - create_time) / 86400.0)
            recency_tiebreaker = 0.02 * math.exp(-0.01 * days_old)
            final_score = min(
                1.0,
                relevance * 0.93 + importance * 0.05 + recency_tiebreaker,
            )
            if final_score < min_final:
                rejected.append(
                    {
                        "fact_id": fact_id,
                        "parent_id": record["parent_id"],
                        "reason": "below_final_relevance_threshold",
                        "final_score": round(final_score, 4),
                    }
                )
                continue

            reaction = fact.get("persona_reaction")
            metadata = {
                "memory_schema_version": "v3",
                "fact_id": fact_id,
                "parent_id": record["parent_id"],
                "session_id": record["scope"],
                "persona_id": record["persona_id"],
                "importance": importance,
                "status": record["status"],
                "create_time": create_time,
                "persona_reaction": reaction,
                "has_source": bool(record.get("source_window")),
                "retrieval_route": route,
                "selection_reason": "relevance_threshold_passed",
            }
            hits.append(
                HybridResult(
                    doc_id=int(record["document_id"]),
                    final_score=final_score,
                    rrf_score=0.0,
                    bm25_score=bm25_map.get(fact_id),
                    vector_score=vector if fact_id in vector_map else None,
                    content=content,
                    metadata=metadata,
                    score_breakdown={
                        "fact_lexical_raw": round(lexical, 4),
                        "fact_token_lexical_raw": round(token_lexical, 4),
                        "fact_short_phrase_raw": round(phrase_lexical, 4),
                        "fact_vector_raw": round(vector, 4),
                        "graph_keyword_raw": round(
                            float(graph_values.get("keyword", 0.0)), 4
                        ),
                        "graph_vector_raw": round(
                            float(graph_values.get("vector", 0.0)), 4
                        ),
                        "graph_calibrated": round(graph_signal, 4),
                        "document_calibrated": round(document_signal, 4),
                        "importance": round(importance, 4),
                        "importance_grace_admitted": 1.0 if grace_admitted else 0.0,
                        "recency_tiebreaker": round(recency_tiebreaker, 4),
                        "final_score": round(final_score, 4),
                    },
                )
            )

        hits.sort(key=lambda item: item.final_score, reverse=True)
        if not hits:
            explanation = "all_candidates_rejected_by_relevance_policy"
        else:
            explanation = "relevant_canonical_facts_selected"
        return FactSearchBundle(
            hits=hits[: max(1, int(limit))],
            candidate_count=len(fact_ids),
            rejected=rejected,
            explanation=explanation,
        )


__all__ = ["CanonicalFactRetriever", "FactSearchBundle"]
