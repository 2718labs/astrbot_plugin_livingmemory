#!/usr/bin/env python3
"""Stest-04: time-compressed lifecycle replay on an isolated candidate store.

Synthetic scenario (no instance data, no LLM on the write side): canonical
facts are written with controlled metadata, then "time" is compressed by
running decay / cleanup / archive / restore / rebuild as explicit
parameterized operations instead of waiting on the wall clock.

Checks, per Stest.md Stest-04:
  - archived/expired facts exit production recall and injection;
  - parent summaries and old indexes cannot bypass fact status;
  - a fresh engine on the same files (restart) is equivalent;
  - rebuild does not resurrect archived facts;
  - repeated decay/cleanup is idempotent: no double decay of the same fact,
    no double archiving, no orphan FTS rows, no unbounded index growth;
  - negative queries never inject.

Every candidate database, index and report is created under a caller-provided
run directory; source databases are never touched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from stest_instance_replay import (  # noqa: E402
    _build_engine,
    _load_json,
    _provider_config,
    _safe_engine_config,
)

from astrbot.core.provider.sources.openai_embedding_source import (  # noqa: E402
    OpenAIEmbeddingProvider,
)
from astrbot_plugin_livingmemory.core.processors.memory_processor import (  # noqa: E402
    MemoryProcessor,
)
from astrbot_plugin_livingmemory.core.utils.fact_packing import (  # noqa: E402
    format_fact_hits_for_injection,
)

SCOPE = "stest04:scope:1"
NEGATIVE_QUERIES = ("哈哈", "晚安", "你在吗", "1+1等于几")

# name: (doc_importance, [fact texts], participants, age_days)
# After 45 days at decay_rate 0.01 (factor ~0.636): A/C stay above the 0.3
# cleanup threshold, B/D fall below it and are old enough to be archived.
SCENARIO: list[tuple[str, float, list[str], list[str], int]] = [
    ("A", 0.90, ["张三每周三晚上打羽毛球"], ["张三"], 100),
    ("B", 0.45, ["李四养了一只三花猫叫咪咪"], ["李四"], 100),
    ("C", 0.90, ["王五在备考注册会计师"], ["王五"], 2),
    ("D", 0.35, ["赵六上个月搬到了杭州"], ["赵六"], 100),
]
DECAY_RATE = 0.01
DECAY_DAYS = 45
CLEANUP_DAYS = 30
CLEANUP_IMPORTANCE = 0.3


def _configure_logging() -> None:
    logging.getLogger().setLevel(logging.WARNING)
    for name in ("openai", "httpx", "httpcore", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    from astrbot import logger  # noqa: PLC0415

    logger.setLevel(logging.WARNING)


def _record_metadata(processor: MemoryProcessor, *, summary: str, facts: list[str],
                     participants: list[str], importance: float) -> dict[str, Any]:
    record = processor.build_explicit_memory_record(
        memory=summary,
        source_scope=SCOPE,
        topics=["stest04"],
        key_facts=facts,
        participants=participants,
        importance=importance,
        origin="agent_memorize_tool",
    )
    return record.metadata


async def _write_memory(
    engine: Any, processor: MemoryProcessor, *, summary: str, facts: list[str],
    participants: list[str], importance: float, age_days: int,
) -> tuple[int, list[str]]:
    metadata = _record_metadata(
        processor, summary=summary, facts=facts, participants=participants,
        importance=importance,
    )
    doc_id = await engine.add_canonical_memory(
        metadata=metadata,
        session_id=SCOPE,
        persona_id=None,
        importance=importance,
        source_messages=None,
    )
    fact_ids = [str(item["fact_id"]) for item in metadata["key_facts"]]
    if age_days > 0:
        past = time.time() - age_days * 86400.0
        await engine.db_connection.execute(
            "UPDATE documents SET metadata = json_set(metadata,"
            " '$.create_time', ?, '$.last_access_time', ?) WHERE id = ?",
            (past, past, doc_id),
        )
        await engine.db_connection.commit()
    return doc_id, fact_ids, str(metadata["parent_id"])


async def _recall(engine: Any, query: str, k: int) -> dict[str, Any]:
    explained = await engine.explain_memory_search(
        query, k=k, session_id=SCOPE, persona_id=None
    )
    hits = list(explained.get("results") or [])
    packed = engine.pack_memory_hits(hits)
    payload = format_fact_hits_for_injection(packed.hits)
    return {
        "hit_ids": [
            str((getattr(hit, "metadata", {}) or {}).get("fact_id") or "")
            for hit in hits
        ],
        "packed_ids": [
            str((getattr(hit, "metadata", {}) or {}).get("fact_id") or "")
            for hit in packed.hits
        ],
        "payload": str(payload or ""),
    }


async def _doc_status(engine: Any, doc_id: int) -> str:
    memory = await engine.get_memory(doc_id)
    metadata = memory.get("metadata") if memory else {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return str(metadata.get("status") or "active")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.run_dir.exists():
        raise FileExistsError(
            f"run directory already exists; choose a new one: {args.run_dir}"
        )
    args.run_dir.mkdir(parents=True)

    astrbot_config = _load_json(args.astrbot_config)
    plugin_config = _load_json(args.plugin_config)
    provider_section = dict(plugin_config.get("provider_settings") or {})
    llm_provider_id = args.llm_provider_id or str(
        provider_section.get("llm_provider_id") or ""
    )
    embedding_provider_id = args.embedding_provider_id or str(
        provider_section.get("embedding_provider_id") or ""
    )
    if not embedding_provider_id:
        raise ValueError("embedding provider ID is required")
    embedding_config = _provider_config(astrbot_config, embedding_provider_id)
    provider_settings = dict(astrbot_config.get("provider_settings") or {})
    embedding_provider = OpenAIEmbeddingProvider(embedding_config, provider_settings)

    engine_config = _safe_engine_config(plugin_config)
    engine_config.update(
        {
            "auto_archived_enabled": True,
            "cleanup_days_threshold": CLEANUP_DAYS,
            "cleanup_importance_threshold": CLEANUP_IMPORTANCE,
        }
    )
    engine, _parent_vectors = await _build_engine(
        args.run_dir, embedding_provider, engine_config
    )
    processor = MemoryProcessor(llm_provider=object(), config={"atom_enabled": False})

    # Phase 0: write four canonical memories with controlled ages.
    docs: dict[str, int] = {}
    facts: dict[str, dict[str, Any]] = {}
    for name, importance, fact_texts, participants, age_days in SCENARIO:
        summary = fact_texts[0]
        doc_id, fact_ids, parent_id = await _write_memory(
            engine, processor, summary=summary, facts=fact_texts,
            participants=participants, importance=importance, age_days=age_days,
        )
        docs[name] = doc_id
        for fact_id in fact_ids:
            facts[fact_id] = {
                "name": name,
                "text": fact_texts[0],
                "doc_id": doc_id,
                "parent_id": parent_id,
            }
    fact_by_name = {info["name"]: fid for fid, info in facts.items()}
    summaries = {name: SCENARIO[i][2][0] for i, name in enumerate(docs)}

    checks: dict[str, Any] = {}
    summary: dict[str, Any] = {}

    # Phase 1: baseline recall before any time compression.
    baseline = {}
    for name, fact_id in fact_by_name.items():
        result = await _recall(engine, facts[fact_id]["text"], args.top_k)
        baseline[name] = {
            "hit": fact_id in result["packed_ids"],
            "injected": facts[fact_id]["text"] in result["payload"],
        }
    checks["baseline_all_recalled"] = all(
        item["hit"] and item["injected"] for item in baseline.values()
    )
    negative_baseline = [
        (await _recall(engine, query, args.top_k))["payload"]
        for query in NEGATIVE_QUERIES
    ]
    checks["baseline_negative_zero"] = all(not payload for payload in negative_baseline)

    # Phase 2: compress 45 days of decay, then run the cleanup pass.
    decayed_1 = await engine.canonical_store.apply_daily_decay(
        DECAY_RATE, days=DECAY_DAYS
    )
    archived_1 = await engine.cleanup_old_memories(
        days_threshold=CLEANUP_DAYS, importance_threshold=CLEANUP_IMPORTANCE
    )
    status_after_cleanup = {name: await _doc_status(engine, doc_id) for name, doc_id in docs.items()}
    checks["decay_touched_all_active"] = decayed_1 == 4
    checks["cleanup_archived_old_low"] = (
        archived_1 == 2
        and status_after_cleanup["B"] == "archived"
        and status_after_cleanup["D"] == "archived"
    )
    checks["high_value_and_fresh_survive"] = (
        status_after_cleanup["A"] == "active"
        and status_after_cleanup["C"] == "active"
    )

    # Phase 3: archived facts must exit production recall and injection.
    post_archive = {}
    for name, fact_id in fact_by_name.items():
        result = await _recall(engine, facts[fact_id]["text"], args.top_k)
        post_archive[name] = {
            "hit": fact_id in result["packed_ids"],
            "injected": facts[fact_id]["text"] in result["payload"],
        }
    checks["archived_exit_recall"] = not (
        post_archive["B"]["hit"] or post_archive["D"]["hit"]
    )
    checks["active_still_recalled"] = post_archive["A"]["hit"] and post_archive["C"]["hit"]
    checks["archived_exit_injection"] = not (
        post_archive["B"]["injected"] or post_archive["D"]["injected"]
    )
    # Parent summaries of archived docs must never leak through any payload.
    all_payloads = []
    for query in list(fact_by_name.values()) + list(NEGATIVE_QUERIES):
        all_payloads.append((await _recall(engine, facts[query]["text"] if query in facts else query, args.top_k))["payload"])
    checks["archived_summary_no_leak"] = all(
        summaries[name] not in payload
        for name in ("B", "D")
        for payload in all_payloads
    )
    negative_after = [
        (await _recall(engine, query, args.top_k))["payload"]
        for query in NEGATIVE_QUERIES
    ]
    checks["negative_zero_after_archive"] = all(not payload for payload in negative_after)

    index_after_archive = await engine.get_canonical_index_status()
    checks["index_consistent_after_archive"] = bool(index_after_archive.get("consistent"))
    checks["index_counts_match_active"] = (
        index_after_archive.get("facts") == 2
        and index_after_archive.get("fts") == 2
        and index_after_archive.get("vectors") == 2
    )

    # Phase 4: rebuild must not resurrect archived facts.
    await engine.rebuild_canonical_indexes()
    index_after_rebuild = await engine.get_canonical_index_status()
    post_rebuild = {}
    for name in ("B", "D"):
        result = await _recall(engine, facts[fact_by_name[name]]["text"], args.top_k)
        post_rebuild[name] = fact_by_name[name] in result["packed_ids"]
    checks["rebuild_no_resurrection"] = (
        not post_rebuild["B"] and not post_rebuild["D"]
        and bool(index_after_rebuild.get("consistent"))
        and index_after_rebuild.get("facts") == 2
    )

    # Phase 5: restart equivalence - a fresh engine on the same files.
    engine2, _v2 = await _build_engine(args.run_dir, embedding_provider, engine_config)
    index_restart = await engine2.get_canonical_index_status()
    restart_recall = {}
    for name, fact_id in fact_by_name.items():
        result = await _recall(engine2, facts[fact_id]["text"], args.top_k)
        restart_recall[name] = fact_id in result["packed_ids"]
    checks["restart_equivalent"] = (
        bool(index_restart.get("consistent"))
        and index_restart.get("facts") == index_after_rebuild.get("facts")
        and restart_recall == {
            "A": True, "B": False, "C": True, "D": False,
        }
    )

    # Phase 6: restore one archived memory re-enables status and indexes.
    restored = await engine.restore_memory(docs["B"])
    status_restored = await _doc_status(engine, docs["B"])
    restored_recall = await _recall(engine, facts[fact_by_name["B"]]["text"], args.top_k)
    index_restored = await engine.get_canonical_index_status()
    checks["restore_restores_status_and_index"] = (
        restored and status_restored == "active"
        and bool(index_restored.get("consistent"))
        and index_restored.get("facts") == 3
    )
    # A restored fact keeps its decayed importance, so the retrieval gate
    # (min_importance_for_retrieval=0.3) keeps it out of production recall
    # until it is reinforced again: status and indexes are restored, recall
    # is not bypassed. This is the intended layered behavior.
    checks["restored_below_threshold_not_recalled"] = (
        fact_by_name["B"] not in restored_recall["packed_ids"]
    )

    # Phase 7: repeated lifecycle is idempotent and leaves no orphans.
    decayed_2 = await engine.canonical_store.apply_daily_decay(
        DECAY_RATE, days=DECAY_DAYS
    )
    archived_2 = await engine.cleanup_old_memories(
        days_threshold=CLEANUP_DAYS, importance_threshold=CLEANUP_IMPORTANCE
    )
    status_final = {name: await _doc_status(engine, doc_id) for name, doc_id in docs.items()}
    orphan_fts = 0
    cursor = await engine.db_connection.execute(
        "SELECT COUNT(*) AS count FROM livingmemory_memories_fts f"
        " JOIN documents d ON d.id = f.doc_id"
        " WHERE json_extract(d.metadata, '$.status') != 'active'"
    )
    row = await cursor.fetchone()
    orphan_fts = int(row["count"]) if row else -1
    # B was restored at its decayed importance, so it is archived again;
    # D must stay archived without double processing, and archived facts
    # must never be decayed a second time.
    checks["decay_skips_archived"] = decayed_2 == 3
    checks["repeat_archive_only_expected"] = (
        archived_2 == 1
        and status_final["B"] == "archived"
        and status_final["D"] == "archived"
        and status_final["A"] == "active"
        and status_final["C"] == "active"
    )
    checks["no_orphan_fts_after_repeat"] = orphan_fts == 0
    index_final = await engine.get_canonical_index_status()
    checks["index_consistent_final"] = bool(index_final.get("consistent"))
    checks["index_counts_final"] = (
        index_final.get("facts") == 2
        and index_final.get("fts") == 2
        and index_final.get("vectors") == 2
    )
    post_repeat = {}
    for name, fact_id in fact_by_name.items():
        result = await _recall(engine, facts[fact_id]["text"], args.top_k)
        post_repeat[name] = fact_id in result["packed_ids"]
    checks["final_recall_state"] = post_repeat == {
        "A": True, "B": False, "C": True, "D": False,
    }
    negative_final = [
        (await _recall(engine, query, args.top_k))["payload"]
        for query in NEGATIVE_QUERIES
    ]
    checks["negative_zero_final"] = all(not payload for payload in negative_final)

    # Phase 8: restore again, then reinforcement re-enables production recall.
    restored_again = await engine.restore_memory(docs["B"])
    b_parent_id = facts[fact_by_name["B"]]["parent_id"]
    await engine.db_connection.execute(
        "UPDATE memory_facts SET importance = 0.6"
        " WHERE parent_id = ? AND status = 'active'",
        (b_parent_id,),
    )
    await engine.db_connection.execute(
        "UPDATE documents SET metadata = json_set(metadata, '$.importance', 0.6)"
        " WHERE id = ?",
        (docs["B"],),
    )
    await engine.db_connection.commit()
    engine._invalidate_search_cache()
    reinforced_recall = await _recall(
        engine, facts[fact_by_name["B"]]["text"], args.top_k
    )
    index_reinforced = await engine.get_canonical_index_status()
    checks["reinforcement_reenables_recall"] = (
        restored_again
        and fact_by_name["B"] in reinforced_recall["packed_ids"]
        and facts[fact_by_name["B"]]["text"] in reinforced_recall["payload"]
        and bool(index_reinforced.get("consistent"))
        and index_reinforced.get("facts") == 3
    )
    negative_reinforced = [
        (await _recall(engine, query, args.top_k))["payload"]
        for query in NEGATIVE_QUERIES
    ]
    checks["negative_zero_reinforced"] = all(
        not payload for payload in negative_reinforced
    )

    summary = {
        "scenario": {
            "docs_written": len(docs),
            "facts_written": len(facts),
            "decay_rate": DECAY_RATE,
            "decay_days": DECAY_DAYS,
            "cleanup_days": CLEANUP_DAYS,
            "cleanup_importance": CLEANUP_IMPORTANCE,
        },
        "phases": {
            "baseline": baseline,
            "decayed_first_pass": decayed_1,
            "archived_first_pass": archived_1,
            "post_archive": post_archive,
            "index_after_archive": index_after_archive,
            "index_after_rebuild": index_after_rebuild,
            "index_after_restart": index_restart,
            "index_after_restore": index_restored,
            "restart_recall": restart_recall,
            "restored": restored,
            "restored_again": restored_again,
            "status_after_restore": status_restored,
            "restored_recall_gated": fact_by_name["B"] not in restored_recall["packed_ids"],
            "decayed_second_pass": decayed_2,
            "archived_second_pass": archived_2,
            "status_final": status_final,
            "orphan_fts_rows": orphan_fts,
            "index_final": index_final,
            "post_repeat": post_repeat,
            "reinforced_recall": {
                "hit": fact_by_name["B"] in reinforced_recall["packed_ids"],
                "injected": facts[fact_by_name["B"]]["text"] in reinforced_recall["payload"],
            },
            "index_reinforced": index_reinforced,
        },
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }

    report = {
        "manifest": {
            "candidate_commit": args.candidate_commit,
            "source": "synthetic scenario, no instance data",
            "engine_config": engine_config,
            "embedding_provider_id": embedding_provider_id,
            "llm_provider_id": llm_provider_id,
        },
        "summary": summary,
    }
    (args.run_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--astrbot-config", required=True, type=Path)
    parser.add_argument("--plugin-config", required=True, type=Path)
    parser.add_argument("--candidate-commit", default="stest04")
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--llm-provider-id", default="")
    parser.add_argument("--embedding-provider-id", default="")
    return parser.parse_args()


def main() -> None:
    _configure_logging()
    args = _parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
