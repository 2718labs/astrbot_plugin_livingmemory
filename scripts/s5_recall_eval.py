"""Deterministic S5 final-injection regression report.

This fixed harness validates the production fact policy and packer with
synthetic route signals. It does not replace Stest's real embedding/model A/B.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if "astrbot_plugin_livingmemory" not in sys.modules:
    package = types.ModuleType("astrbot_plugin_livingmemory")
    package.__path__ = [str(REPO_ROOT)]
    package.__file__ = str(REPO_ROOT / "__init__.py")
    sys.modules["astrbot_plugin_livingmemory"] = package

from astrbot_plugin_livingmemory.core.retrieval.fact_retriever import (
    CanonicalFactRetriever,
)
from astrbot_plugin_livingmemory.core.utils.fact_packing import pack_fact_hits


class _TextProcessor:
    async def tokenize_async(self, text, remove_stopwords=True):
        return [part.casefold() for part in str(text).split() if part]


class _Store:
    def __init__(self, cases, records):
        self.cases = cases
        self.records = records

    async def search_candidates(self, query, *, limit, scope, persona_id):
        return self.cases.get(query, {"bm25": [], "vector": []})

    async def get_fact_records(self, fact_ids):
        return {
            fact_id: self.records[fact_id]
            for fact_id in fact_ids
            if fact_id in self.records
        }


def _record(fact_id, parent_id, document_id, fact, search_text):
    now = time.time()
    return {
        "fact_id": fact_id,
        "parent_id": parent_id,
        "document_id": document_id,
        "fact": {
            "fact_id": fact_id,
            "parent_id": parent_id,
            "fact": fact,
            "importance": 0.8,
        },
        "search_text": search_text,
        "scope": "eval:private:user",
        "persona_id": "persona_eval",
        "importance": 0.8,
        "status": "active",
        "overview": "parent overview is never injected",
        "source_window": {"fingerprint": f"source_{parent_id}"},
        "document_metadata": {"create_time": now},
        "created_at": now,
    }


def _git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _git_dirty() -> bool:
    try:
        return bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=REPO_ROOT,
                text=True,
            ).strip()
        )
    except (OSError, subprocess.SubprocessError):
        return True


async def main() -> None:
    records = {
        "fact_companion": _record(
            "fact_companion",
            "memory_tools",
            1,
            "陪伴插件名称是 CompanionLite",
            "陪伴 插件 名称 CompanionLite 工具",
        ),
        "fact_sibling": _record(
            "fact_sibling",
            "memory_tools",
            1,
            "用户周末打算整理桌面",
            "周末 整理 桌面",
        ),
        "fact_noise": _record(
            "fact_noise",
            "memory_noise",
            2,
            "旧项目出现过依赖报错",
            "旧 项目 依赖 报错",
        ),
    }
    route_cases = {
        "陪伴 插件 名称": {
            "bm25": [
                {"fact_id": "fact_companion", "parent_id": "memory_tools", "score": -3.0}
            ],
            "vector": [
                {"fact_id": "fact_companion", "parent_id": "memory_tools", "score": 0.93}
            ],
        },
        "以前 那个 陪伴 工具 叫什么": {
            "bm25": [],
            "vector": [
                {"fact_id": "fact_companion", "parent_id": "memory_tools", "score": 0.82}
            ],
        },
        "Python 新报错 怎么办": {
            "bm25": [],
            "vector": [
                {"fact_id": "fact_noise", "parent_id": "memory_noise", "score": 0.31}
            ],
        },
    }
    eval_cases = [
        ("direct", "陪伴 插件 名称", "fact_companion"),
        ("indirect", "以前 那个 陪伴 工具 叫什么", "fact_companion"),
        ("laugh", "哈哈", None),
        ("meal", "吃什么", None),
        ("new_error", "Python 新报错 怎么办", None),
        ("goodnight", "晚安", None),
    ]
    config = {
        "fact_candidate_k": 20,
        "fact_min_lexical_score": 0.34,
        "fact_min_vector_similarity": 0.62,
        "fact_min_final_score": 0.42,
        "document_route_weight": 1.0,
        "graph_route_weight": 0.0,
        "injection_token_budget": 1200,
        "single_fact_token_budget": 320,
    }
    retriever = CanonicalFactRetriever(
        _Store(route_cases, records), _TextProcessor(), config=config
    )

    rows = []
    reciprocal_ranks = []
    negative_injections = 0
    for case_id, query, target in eval_cases:
        started = time.perf_counter()
        bundle = await retriever.search(
            query,
            limit=4,
            scope="eval:private:user",
            persona_id="persona_eval",
        )
        packed = pack_fact_hits(
            bundle.hits,
            token_budget=config["injection_token_budget"],
            single_fact_budget=config["single_fact_token_budget"],
        )
        final_ids = [hit.metadata.get("fact_id") for hit in packed.hits]
        rank = final_ids.index(target) + 1 if target in final_ids else None
        if target is not None:
            reciprocal_ranks.append(1.0 / rank if rank else 0.0)
        elif final_ids:
            negative_injections += 1
        rows.append(
            {
                "id": case_id,
                "query": query,
                "target": target,
                "candidate_fact_count": bundle.candidate_count,
                "final_fact_ids": final_ids,
                "final_fact_count": len(final_ids),
                "token_upper_bound": packed.token_count,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "explanation": bundle.explanation,
                "passed": (target in final_ids) if target else not final_ids,
            }
        )

    positive_rows = [row for row in rows if row["target"]]
    negative_rows = [row for row in rows if not row["target"]]
    report = {
        "manifest": {
            "candidate_commit": _git_head(),
            "working_tree_dirty": _git_dirty(),
            "fixture_version": "s5-fixed-v1",
            "route_signals": "synthetic-fixed",
            "token_counter": "utf8-byte-upper-bound",
            "config": config,
        },
        "metrics": {
            "hit_at_1": sum(row["final_fact_ids"][:1] == [row["target"]] for row in positive_rows)
            / len(positive_rows),
            "mrr": sum(reciprocal_ranks) / len(reciprocal_ranks),
            "negative_injection_rate": negative_injections / len(negative_rows),
            "empty_result_rate": sum(not row["final_fact_ids"] for row in rows) / len(rows),
            "max_token_upper_bound": max(row["token_upper_bound"] for row in rows),
            "max_latency_ms": max(row["latency_ms"] for row in rows),
        },
        "cases": rows,
        "all_passed": all(row["passed"] for row in rows),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
