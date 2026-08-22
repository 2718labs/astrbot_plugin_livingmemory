"""S5 production fact retrieval, abstention and injection budget regressions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_livingmemory.core.retrieval.fact_retriever import (
    CanonicalFactRetriever,
)
from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_livingmemory.core.utils.fact_packing import (
    fact_entry_text,
    format_fact_hits_for_injection,
    pack_fact_hits,
    token_upper_bound,
)


class _TextProcessor:
    async def tokenize_async(self, text, remove_stopwords=True):
        return [item.casefold() for item in str(text).split() if item]


def _record(fact_id: str, text: str, *, importance: float = 0.8):
    return {
        "fact_id": fact_id,
        "parent_id": "parent_shared",
        "document_id": 7,
        "fact": {
            "fact_id": fact_id,
            "parent_id": "parent_shared",
            "fact": text,
            "importance": importance,
            "topics": ["private topic"],
            "persona_reaction": {"emotion": "开心", "thought": "可以接着聊"},
        },
        "search_text": text,
        "scope": "scope:1",
        "persona_id": "p1",
        "importance": importance,
        "status": "active",
        "overview": "This parent overview contains an unrelated sibling fact.",
        "source_window": {"fingerprint": "source"},
        "document_metadata": {"create_time": 100.0},
        "created_at": 100.0,
    }


@pytest.mark.asyncio
async def test_lightweight_query_abstains_before_search():
    store = SimpleNamespace(
        search_candidates=AsyncMock(),
        get_fact_records=AsyncMock(),
    )
    retriever = CanonicalFactRetriever(store, _TextProcessor())

    bundle = await retriever.search("哈哈", limit=4)

    assert bundle.hits == []
    assert bundle.explanation == "lightweight_message_without_history_reference"
    store.search_candidates.assert_not_awaited()


def test_open_ended_question_is_not_hardcoded_as_lightweight() -> None:
    assert CanonicalFactRetriever.query_gate_reason("吃什么") is None


@pytest.mark.asyncio
async def test_fact_hit_never_expands_to_parent_siblings():
    store = SimpleNamespace(
        search_candidates=AsyncMock(
            return_value={
                "bm25": [{"fact_id": "fact_target", "parent_id": "parent_shared", "score": -2.0}],
                "vector": [{"fact_id": "fact_target", "parent_id": "parent_shared", "score": 0.91}],
            }
        ),
        get_fact_records=AsyncMock(
            return_value={"fact_target": _record("fact_target", "plugin name companionlite")}
        ),
    )
    retriever = CanonicalFactRetriever(store, _TextProcessor())

    bundle = await retriever.search("plugin name", limit=4, scope="scope:1")

    assert [hit.content for hit in bundle.hits] == ["plugin name companionlite"]
    assert bundle.hits[0].metadata["fact_id"] == "fact_target"
    assert "overview" not in bundle.hits[0].metadata
    store.get_fact_records.assert_awaited_once_with(["fact_target"])


@pytest.mark.asyncio
async def test_weak_vector_candidate_is_rejected_instead_of_filling_top_k():
    store = SimpleNamespace(
        search_candidates=AsyncMock(
            return_value={
                "bm25": [],
                "vector": [{"fact_id": "fact_weak", "parent_id": "parent_shared", "score": 0.31}],
            }
        ),
        get_fact_records=AsyncMock(
            return_value={"fact_weak": _record("fact_weak", "unrelated archive note")}
        ),
    )
    retriever = CanonicalFactRetriever(store, _TextProcessor())

    bundle = await retriever.search("new compiler error", limit=4)

    assert bundle.hits == []
    assert bundle.explanation == "all_candidates_rejected_by_relevance_policy"
    assert bundle.rejected[0]["reason"] == "insufficient_relevance_evidence"


@pytest.mark.asyncio
async def test_weak_graph_route_cannot_displace_strong_fact():
    store = SimpleNamespace(
        search_candidates=AsyncMock(
            return_value={
                "bm25": [{"fact_id": "fact_good", "parent_id": "parent_shared", "score": -3.0}],
                "vector": [{"fact_id": "fact_good", "parent_id": "parent_shared", "score": 0.92}],
            }
        ),
        get_fact_records=AsyncMock(
            return_value={
                "fact_good": _record("fact_good", "driving test booking"),
                "fact_weak": _record("fact_weak", "unrelated graph node"),
            }
        ),
    )
    graph = SimpleNamespace(
        search=AsyncMock(
            return_value=[
                SimpleNamespace(
                    metadata={"fact_id": "fact_weak"},
                    keyword_score=0.05,
                    vector_score=0.08,
                )
            ]
        )
    )
    retriever = CanonicalFactRetriever(
        store,
        _TextProcessor(),
        graph,
        {"document_route_weight": 0.65, "graph_route_weight": 0.35},
    )

    bundle = await retriever.search("driving test", limit=4)

    assert [hit.metadata["fact_id"] for hit in bundle.hits] == ["fact_good"]
    assert any(item["fact_id"] == "fact_weak" for item in bundle.rejected)
    breakdown = bundle.hits[0].score_breakdown or {}
    assert breakdown["graph_calibrated"] == 0.0


def _hit(fact_id: str, content: str, reaction=None) -> HybridResult:
    return HybridResult(
        doc_id=1,
        final_score=0.9,
        rrf_score=0.0,
        bm25_score=None,
        vector_score=0.9,
        content=content,
        metadata={
            "fact_id": fact_id,
            "parent_id": "parent",
            "persona_reaction": reaction,
            "topics": ["must not be injected"],
        },
    )


def test_fact_packer_keeps_complete_facts_and_stops_at_hard_budget():
    first = _hit("f1", "first complete fact")
    second = _hit("f2", "second complete fact")
    renderer = lambda hits: "|".join(hit.content for hit in hits)
    budget = token_upper_bound("first complete fact")

    packed = pack_fact_hits(
        [first, second],
        token_budget=budget,
        single_fact_budget=100,
        renderer=renderer,
    )

    assert [hit.metadata["fact_id"] for hit in packed.hits] == ["f1"]
    assert packed.token_count <= packed.token_budget
    assert packed.dropped == [{"fact_id": "f2", "reason": "total_budget"}]


def test_minimal_fact_format_separates_optional_reaction():
    hit = _hit(
        "f1",
        "用户周三考科目二",
        {"emotion": "有点担心", "thought": "希望她别紧张"},
    )

    entry = fact_entry_text(hit)
    payload = format_fact_hits_for_injection([hit])

    assert "用户周三考科目二" in entry
    assert "当时反应：有点担心；希望她别紧张" in entry
    assert "must not be injected" not in payload
    assert "Importance" not in payload
    assert "Memory write time" not in payload
