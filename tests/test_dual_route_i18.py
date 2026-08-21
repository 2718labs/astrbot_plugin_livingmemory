"""S3-06: I18 regression sample for dual-route signal normalization.

Reproduces the "each route divides by its own maximum" defect: a weak graph
candidate is normalized to a full signal just because it ranks first inside
its own (weak) route. S3 only locks the failing sample and the correct
behaviour (zero-weight bypass, S3-05); the fusion formula fix itself is S5.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_livingmemory.core.retrieval.dual_route_retriever import (
    DualRouteRetriever,
)
from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import HybridResult


def _hybrid_result(
    doc_id: int,
    final_score: float,
    content: str = "some memory content",
) -> HybridResult:
    return HybridResult(
        doc_id=doc_id,
        final_score=final_score,
        rrf_score=final_score,
        bm25_score=0.5,
        vector_score=0.5,
        content=content,
        metadata={"importance": 0.8, "session_id": "s1"},
        score_breakdown={"rrf_normalized": final_score},
    )


def _graph_result(doc_id: int, final_score: float) -> SimpleNamespace:
    return SimpleNamespace(
        doc_id=doc_id,
        final_score=final_score,
        rrf_score=final_score,
        keyword_score=0.1,
        vector_score=0.1,
        content="graph candidate content",
        metadata={"importance": 0.8, "session_id": "s1"},
        score_breakdown={"graph_final_score": final_score},
    )


async def _memory_loader(doc_id: int) -> dict:
    return {"text": f"memory {doc_id} text", "metadata": {"importance": 0.8}}


@pytest.mark.asyncio
async def test_i18_weak_graph_first_candidate_gets_full_signal():
    """Regression sample: weak graph route winner is normalized to 1.0.

    Document route has one strong correct candidate (0.9); graph route has
    one weak/irrelevant candidate (0.05). Because each route divides by its
    own maximum, the weak graph candidate still receives graph_signal = 1.0
    and can compete with the strong document candidate.

    This reproduces I18. The expected fix (S5): a route without reliable
    candidates must be allowed to contribute nothing. This test locks the
    current behaviour so the sample cannot silently drift.
    """
    document_retriever = AsyncMock()
    document_retriever.search = AsyncMock(
        return_value=[_hybrid_result(doc_id=101, final_score=0.9)]
    )
    graph_retriever = AsyncMock()
    graph_retriever.search = AsyncMock(
        return_value=[_graph_result(doc_id=202, final_score=0.05)]
    )

    retriever = DualRouteRetriever(
        document_retriever=document_retriever,
        graph_retriever=graph_retriever,
        memory_loader=_memory_loader,
        config={
            "document_route_weight": 0.65,
            "graph_route_weight": 0.35,
            "cross_route_bonus": 0.08,
            "dynamic_route_weighting": False,
        },
    )

    results = await retriever.search("驾考在哪报名", k=4)

    graph_entry = next(item for item in results if item.doc_id == 202)
    breakdown = graph_entry.score_breakdown or {}

    # I18 reproduced: the weak graph candidate is normalized to a full signal
    # just because it ranks first inside its own weak route.
    assert breakdown["graph_route_score"] == 1.0
    assert breakdown["document_route_score"] == 0.0
    # Because the weak graph candidate is not excluded, it appears in results
    # with the same normalized signal as the strong document candidate.
    assert 202 in {item.doc_id for item in results}
    assert breakdown["dual_route_final_score"] >= 0.35


@pytest.mark.asyncio
async def test_zero_graph_weight_bypasses_graph_route():
    """S3-05 correct behaviour: graph_route_weight <= 0 never queries graph."""
    document_retriever = AsyncMock()
    document_retriever.search = AsyncMock(
        return_value=[_hybrid_result(doc_id=101, final_score=0.9)]
    )
    graph_retriever = AsyncMock()
    graph_retriever.search = AsyncMock(
        return_value=[_graph_result(doc_id=202, final_score=0.9)]
    )

    retriever = DualRouteRetriever(
        document_retriever=document_retriever,
        graph_retriever=graph_retriever,
        memory_loader=_memory_loader,
        config={
            "document_route_weight": 1.0,
            "graph_route_weight": 0.0,
            "cross_route_bonus": 0.0,
            "dynamic_route_weighting": False,
        },
    )

    results = await retriever.search("驾考在哪报名", k=4)

    graph_retriever.search.assert_not_awaited()
    assert [item.doc_id for item in results] == [101]
