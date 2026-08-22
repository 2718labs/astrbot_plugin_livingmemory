#!/usr/bin/env python3
"""Replay legacy LivingMemory source windows through the v3 pipeline.

The source databases are always opened in SQLite read-only mode.  Every
candidate database, index and report is created under a caller-provided run
directory, so this script cannot migrate or replace the running instance.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sqlite3
import sys
import time
import types
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "astrbot_plugin_livingmemory"


def _install_package_alias() -> None:
    if PACKAGE_NAME in sys.modules:
        return
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(REPO_ROOT)]
    package.__file__ = str(REPO_ROOT / "__init__.py")
    sys.modules[PACKAGE_NAME] = package


_install_package_alias()

from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB  # noqa: E402
from astrbot.core.provider.sources.openai_embedding_source import (  # noqa: E402
    OpenAIEmbeddingProvider,
)
from astrbot.core.provider.sources.openai_source import (  # noqa: E402
    ProviderOpenAIOfficial,
)

from astrbot_plugin_livingmemory.core.managers.memory_engine import (  # noqa: E402
    MemoryEngine,
)
from astrbot_plugin_livingmemory.core.models.conversation_models import (  # noqa: E402
    Message,
)
from astrbot_plugin_livingmemory.core.processors.memory_processor import (  # noqa: E402
    MemoryProcessor,
)
from astrbot_plugin_livingmemory.core.utils.fact_packing import (  # noqa: E402
    format_fact_hits_for_injection,
)


NEGATIVE_QUERIES = ("哈哈", "晚安", "你在吗", "1+1等于几")


def _configure_logging() -> None:
    """Keep private prompts and source text out of normal command output."""
    logging.getLogger().setLevel(logging.WARNING)
    for name in ("openai", "httpx", "httpcore", "httpx2", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    from astrbot import logger

    logger.setLevel(logging.INFO)


@dataclass(slots=True)
class SourceWindow:
    memory_id: int
    persona_id: str
    scope: str
    old_text: str
    old_metadata: dict[str, Any]
    messages: list[Message]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _readonly_connection(path: Path) -> sqlite3.Connection:
    absolute = path.resolve().as_posix()
    connection = sqlite3.connect(f"file:///{absolute}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _load_source_windows(
    path: Path, limit: int | None, memory_ids: set[int] | None = None
) -> list[SourceWindow]:
    with _readonly_connection(path) as connection:
        rows = connection.execute(
            """
            SELECT d.id, d.text, d.metadata, s.source_json
            FROM documents d
            JOIN memory_sources s ON s.memory_id = d.id
            WHERE COALESCE(json_extract(d.metadata, '$.status'), 'active') = 'active'
            ORDER BY COALESCE(
                json_extract(d.metadata, '$.source_time_start'), d.created_at
            ), d.id
            """
        ).fetchall()

    selected_rows = [
        row for row in rows if not memory_ids or int(row["id"]) in memory_ids
    ]
    if limit:
        selected_rows = selected_rows[:limit]
    windows: list[SourceWindow] = []
    for row in selected_rows:
        metadata = _json_object(row["metadata"])
        raw_messages = json.loads(row["source_json"])
        if not isinstance(raw_messages, list) or not raw_messages:
            continue
        messages = [Message.from_dict(item) for item in raw_messages]
        windows.append(
            SourceWindow(
                memory_id=int(row["id"]),
                persona_id=str(metadata.get("persona_id") or ""),
                scope=str(
                    metadata.get("session_id")
                    or metadata.get("source_session_id")
                    or messages[0].session_id
                ),
                old_text=str(row["text"] or ""),
                old_metadata=metadata,
                messages=messages,
            )
        )
    return windows


def _load_persona_prompt(path: Path, persona_id: str) -> str:
    if not persona_id:
        return ""
    with _readonly_connection(path) as connection:
        row = connection.execute(
            "SELECT system_prompt FROM personas WHERE persona_id = ? LIMIT 1",
            (persona_id,),
        ).fetchone()
    return str(row["system_prompt"] or "") if row else ""


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _provider_config(config: dict[str, Any], provider_id: str) -> dict[str, Any]:
    provider = next(
        (
            dict(item)
            for item in config.get("provider", [])
            if isinstance(item, dict) and item.get("id") == provider_id
        ),
        None,
    )
    if provider is None:
        raise ValueError(f"provider not found: {provider_id}")
    source_id = str(provider.get("provider_source_id") or "")
    if source_id:
        source = next(
            (
                dict(item)
                for item in config.get("provider_sources", [])
                if isinstance(item, dict) and item.get("id") == source_id
            ),
            None,
        )
        if source is None:
            raise ValueError(f"provider source not found: {source_id}")
        provider = {**source, **provider}
        provider["id"] = provider_id
    return provider


class _Persona:
    def __init__(self, system_prompt: str) -> None:
        self.system_prompt = system_prompt


class _PersonaManager:
    def __init__(self, persona_id: str, system_prompt: str) -> None:
        self.persona_id = persona_id
        self.system_prompt = system_prompt

    async def get_persona(self, persona_id: str) -> _Persona | None:
        if persona_id != self.persona_id or not self.system_prompt:
            return None
        return _Persona(self.system_prompt)


class _ProcessorContext:
    def __init__(self, persona_id: str, system_prompt: str) -> None:
        self.persona_manager = _PersonaManager(persona_id, system_prompt)


def _safe_engine_config(plugin_config: dict[str, Any]) -> dict[str, Any]:
    recall = dict(plugin_config.get("recall_engine") or {})
    graph = dict(plugin_config.get("graph_memory") or {})
    return {
        **recall,
        "graph_memory_enabled": True,
        "graph_route_weight": float(graph.get("graph_route_weight", 0.0)),
        "document_route_weight": float(graph.get("document_route_weight", 0.65)),
        "cross_route_bonus": float(graph.get("cross_route_bonus", 0.0)),
        "dynamic_route_weighting": bool(
            graph.get("dynamic_route_weighting", False)
        ),
        "atom_enabled": False,
        "injection_token_budget": int(recall.get("injection_token_budget", 1200)),
        "single_fact_token_budget": int(
            recall.get("single_fact_token_budget", 320)
        ),
        "include_persona_reaction": bool(
            recall.get("include_persona_reaction", True)
        ),
        "write_op_repair_enabled": True,
    }


async def _build_engine(
    run_dir: Path,
    embedding_provider: OpenAIEmbeddingProvider,
    engine_config: dict[str, Any],
) -> tuple[MemoryEngine, FaissVecDB]:
    main_db = run_dir / "candidate_livingmemory.db"
    parent_vectors = FaissVecDB(
        doc_store_path=str(main_db),
        index_store_path=str(run_dir / "candidate_parent.index"),
        embedding_provider=embedding_provider,
    )
    fact_vectors = FaissVecDB(
        doc_store_path=str(run_dir / "candidate_fact_vectors.db"),
        index_store_path=str(run_dir / "candidate_facts.index"),
        embedding_provider=embedding_provider,
    )
    graph_vectors = FaissVecDB(
        doc_store_path=str(run_dir / "candidate_graph_vectors.db"),
        index_store_path=str(run_dir / "candidate_graph.index"),
        embedding_provider=embedding_provider,
    )
    await parent_vectors.initialize()
    await fact_vectors.initialize()
    await graph_vectors.initialize()
    engine = MemoryEngine(
        db_path=str(main_db),
        faiss_db=parent_vectors,
        fact_vector_db=fact_vectors,
        graph_vector_db=graph_vectors,
        config=engine_config,
    )
    await engine.initialize()
    return engine, parent_vectors


def _fact_texts(metadata: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for item in metadata.get("key_facts") or []:
        text = str(item.get("fact") if isinstance(item, dict) else item).strip()
        if text:
            values.append(text)
    return values


def _result_view(hit: Any) -> dict[str, Any]:
    metadata = getattr(hit, "metadata", {}) or {}
    score_breakdown = getattr(hit, "score_breakdown", None) or {}
    return {
        "fact_id": str(metadata.get("fact_id") or ""),
        "parent_id": str(metadata.get("parent_id") or ""),
        "content": str(getattr(hit, "content", "") or ""),
        "score": round(
            float(
                getattr(hit, "final_score", None)
                or getattr(hit, "score", 0.0)
                or 0.0
            ),
            6,
        ),
        "lexical_score": round(float(getattr(hit, "bm25_score", 0.0) or 0.0), 6),
        "vector_score": round(float(getattr(hit, "vector_score", 0.0) or 0.0), 6),
        "score_breakdown": score_breakdown,
        "routes": list(metadata.get("routes") or []),
        "reasons": list(metadata.get("reasons") or []),
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    for source_path in (args.source_memory_db, args.persona_db):
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
    if args.run_dir.exists():
        raise FileExistsError(
            f"run directory already exists; choose a new one: {args.run_dir}"
        )
    args.run_dir.mkdir(parents=True)

    requested_ids = set(args.memory_id or [])
    windows = _load_source_windows(
        args.source_memory_db, args.limit, requested_ids or None
    )
    if not windows:
        raise RuntimeError("no source windows with retained messages")
    persona_ids = {item.persona_id for item in windows if item.persona_id}
    if len(persona_ids) > 1:
        raise RuntimeError(f"multiple personas in one replay: {sorted(persona_ids)}")
    persona_id = next(iter(persona_ids), "")
    persona_prompt = _load_persona_prompt(args.persona_db, persona_id)

    astrbot_config = _load_json(args.astrbot_config)
    plugin_config = _load_json(args.plugin_config)
    configured_memory_provider = str(
        (plugin_config.get("provider_settings") or {}).get("llm_provider_id") or ""
    )
    llm_provider_id = args.llm_provider_id or configured_memory_provider
    embedding_provider_id = args.embedding_provider_id or str(
        (plugin_config.get("provider_settings") or {}).get(
            "embedding_provider_id"
        )
        or ""
    )
    if not llm_provider_id or not embedding_provider_id:
        raise ValueError("memory LLM and embedding provider IDs are required")
    llm_config = _provider_config(astrbot_config, llm_provider_id)
    embedding_config = _provider_config(astrbot_config, embedding_provider_id)
    provider_settings = dict(astrbot_config.get("provider_settings") or {})

    llm_provider = ProviderOpenAIOfficial(llm_config, provider_settings)
    embedding_provider = OpenAIEmbeddingProvider(
        embedding_config, provider_settings
    )
    engine_config = _safe_engine_config(plugin_config)
    engine, parent_vectors = await _build_engine(
        args.run_dir, embedding_provider, engine_config
    )
    processor = MemoryProcessor(
        context=_ProcessorContext(persona_id, persona_prompt),
        llm_provider=llm_provider,
        config={
            "include_source_time_tags": bool(
                (plugin_config.get("reflection_engine") or {}).get(
                    "include_source_time_tags", True
                )
            ),
            "atom_enabled": False,
        },
    )

    started = time.perf_counter()
    window_results: list[dict[str, Any]] = []
    document_to_source: dict[int, int] = {}
    try:
        for index, window in enumerate(windows, 1):
            topic_candidates = await engine.get_topic_candidates(window.scope)
            result = await processor.process_conversation_result(
                messages=window.messages,
                is_group_chat=any(message.group_id for message in window.messages),
                persona_id=window.persona_id or None,
                topic_candidates=topic_candidates,
                source_scope=window.scope,
            )
            row: dict[str, Any] = {
                "source_memory_id": window.memory_id,
                "source_message_count": len(window.messages),
                "old_document_chars": len(window.old_text),
                "old_fact_count": len(_fact_texts(window.old_metadata)),
                "status": result.status,
                "stored_fact_count": result.stored_fact_count,
                "skipped_fact_count": result.skipped_fact_count,
                "error": result.error,
                "records": [],
            }
            if result.status == "store":
                for record in result.iter_records():
                    document_id = await engine.add_canonical_memory(
                        metadata=record.metadata,
                        session_id=window.scope,
                        persona_id=window.persona_id or None,
                        importance=record.importance,
                        source_messages=[message.to_dict() for message in window.messages],
                    )
                    document_to_source[document_id] = window.memory_id
                    row["records"].append(
                        {
                            "document_id": document_id,
                            "parent_id": record.metadata.get("parent_id"),
                            "summary": record.metadata.get("canonical_summary"),
                            "importance": record.importance,
                            "facts": record.metadata.get("key_facts") or [],
                        }
                    )
            window_results.append(row)
            print(
                f"[{index}/{len(windows)}] old={window.memory_id} "
                f"status={result.status} store={result.stored_fact_count} "
                f"skip={result.skipped_fact_count}",
                flush=True,
            )

        index_status = await engine.get_canonical_index_status()
        fact_id_cursor = await engine.canonical_store.db.execute(
            "SELECT fact_id FROM memory_facts WHERE status = 'active' ORDER BY id"
        )
        fact_ids = [
            str(row["fact_id"]) for row in await fact_id_cursor.fetchall()
        ]
        fact_record_map = await engine.canonical_store.get_fact_records(fact_ids)
        fact_records = [
            fact_record_map[fact_id]
            for fact_id in fact_ids
            if fact_id in fact_record_map
        ]
        exact_recall: list[dict[str, Any]] = []
        related_sibling_co_injections = 0
        queries_with_related_siblings = 0
        parent_summary_leaks = 0
        max_injection_bytes = 0
        for fact_record in fact_records:
            fact = dict(fact_record.get("fact") or {})
            target_id = str(fact.get("fact_id") or fact_record.get("fact_id") or "")
            query = str(fact.get("fact") or "").strip()
            explained = await engine.explain_memory_search(
                query,
                k=args.top_k,
                session_id=str(fact_record.get("scope") or "") or None,
                persona_id=str(fact_record.get("persona_id") or "") or None,
            )
            hits = list(explained.get("results") or [])
            packed = engine.pack_memory_hits(hits)
            payload = format_fact_hits_for_injection(
                packed.hits,
                include_reaction=bool(engine_config["include_persona_reaction"]),
            )
            payload_bytes = len(payload.encode("utf-8"))
            max_injection_bytes = max(max_injection_bytes, payload_bytes)
            hit_ids = [
                str((getattr(hit, "metadata", {}) or {}).get("fact_id") or "")
                for hit in hits
            ]
            parent_id = str(fact_record.get("parent_id") or "")
            siblings = [
                str((item.get("fact") or {}).get("fact") or "")
                for item in fact_records
                if str(item.get("parent_id") or "") == parent_id
                and str(item.get("fact_id") or "") != target_id
            ]
            leaked_siblings = [text for text in siblings if text and text in payload]
            related_sibling_co_injections += len(leaked_siblings)
            queries_with_related_siblings += int(bool(leaked_siblings))
            overview = str(fact_record.get("overview") or "").strip()
            summary_leaked = bool(
                overview
                and overview != query
                and overview not in siblings
                and overview in payload
            )
            parent_summary_leaks += int(summary_leaked)
            exact_recall.append(
                {
                    "target_fact_id": target_id,
                    "query": query,
                    "hit_rank": (
                        hit_ids.index(target_id) + 1 if target_id in hit_ids else None
                    ),
                    "candidate_count": int(explained.get("candidate_count") or 0),
                    "hits": [_result_view(hit) for hit in hits],
                    "packed_fact_ids": [
                        str(
                            (getattr(hit, "metadata", {}) or {}).get("fact_id")
                            or ""
                        )
                        for hit in packed.hits
                    ],
                    "injection_bytes": payload_bytes,
                    "injection_payload": payload,
                    "dropped": packed.dropped,
                    "sibling_leaks": leaked_siblings,
                    "parent_summary_leaked": summary_leaked,
                }
            )

        negative_results: list[dict[str, Any]] = []
        default_scope = windows[0].scope
        default_persona = windows[0].persona_id or None
        for query in NEGATIVE_QUERIES:
            explained = await engine.explain_memory_search(
                query,
                k=args.top_k,
                session_id=default_scope,
                persona_id=default_persona,
            )
            hits = list(explained.get("results") or [])
            packed = engine.pack_memory_hits(hits)
            payload = format_fact_hits_for_injection(packed.hits)
            negative_results.append(
                {
                    "query": query,
                    "candidate_count": int(explained.get("candidate_count") or 0),
                    "accepted_count": len(hits),
                    "injected_count": len(packed.hits),
                    "injection_bytes": len(payload.encode("utf-8")),
                    "hits": [_result_view(hit) for hit in hits],
                }
            )

        statuses = [str(item["status"]) for item in window_results]
        exact_ranks = [item["hit_rank"] for item in exact_recall]
        old_chars = [int(item["old_document_chars"]) for item in window_results]
        summary = {
            "source_windows": len(windows),
            "source_messages": sum(len(item.messages) for item in windows),
            "old_fact_count": sum(int(item["old_fact_count"]) for item in window_results),
            "old_document_chars_total": sum(old_chars),
            "old_document_chars_average": round(sum(old_chars) / len(old_chars), 2),
            "store_windows": statuses.count("store"),
            "skip_windows": statuses.count("skip"),
            "invalid_windows": statuses.count("invalid"),
            "candidate_parents": sum(len(item["records"]) for item in window_results),
            "candidate_facts": len(fact_records),
            "candidate_reactions": sum(
                1
                for item in fact_records
                if (item.get("fact") or {}).get("persona_reaction")
            ),
            "exact_hit_at_1": sum(rank == 1 for rank in exact_ranks),
            "exact_hit_at_k": sum(rank is not None for rank in exact_ranks),
            "exact_query_count": len(exact_ranks),
            "exact_injection_hit_count": sum(
                item["target_fact_id"] in item["packed_fact_ids"]
                for item in exact_recall
            ),
            "negative_injection_count": sum(
                item["injected_count"] > 0 for item in negative_results
            ),
            "negative_query_count": len(negative_results),
            "max_injection_bytes": max_injection_bytes,
            "related_sibling_co_injections": related_sibling_co_injections,
            "queries_with_related_siblings": queries_with_related_siblings,
            "parent_summary_leaks": parent_summary_leaks,
            "index_status": index_status,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
        manifest = {
            "candidate_commit": args.candidate_commit,
            "source_memory_db": {
                "path": str(args.source_memory_db),
                "sha256": _sha256(args.source_memory_db),
            },
            "persona_db": {
                "path": str(args.persona_db),
                "sha256": _sha256(args.persona_db),
            },
            "llm_provider_id": llm_provider_id,
            "llm_model": str(llm_config.get("model") or ""),
            "embedding_provider_id": embedding_provider_id,
            "embedding_model": str(embedding_config.get("embedding_model") or ""),
            "embedding_dimensions": int(
                embedding_config.get("embedding_dimensions") or 0
            ),
            "persona_id": persona_id,
            "persona_prompt_sha256": hashlib.sha256(
                persona_prompt.encode("utf-8")
            ).hexdigest(),
            "engine_config": engine_config,
            "top_k": args.top_k,
            "source_memory_ids": sorted(requested_ids),
            "negative_queries": list(NEGATIVE_QUERIES),
        }
        report = {
            "manifest": manifest,
            "summary": summary,
            "windows": window_results,
            "facts": fact_records,
            "exact_recall": exact_recall,
            "negative_recall": negative_results,
        }
        with (args.run_dir / "report.private.json").open(
            "w", encoding="utf-8"
        ) as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, default=str)
        with (args.run_dir / "summary.json").open("w", encoding="utf-8") as stream:
            json.dump(
                {"manifest": manifest, "summary": summary},
                stream,
                ensure_ascii=False,
                indent=2,
            )
        return {"manifest": manifest, "summary": summary}
    finally:
        await engine.close()
        await parent_vectors.close()
        await llm_provider.terminate()
        await embedding_provider.terminate()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-memory-db", required=True, type=Path)
    parser.add_argument("--persona-db", required=True, type=Path)
    parser.add_argument("--astrbot-config", required=True, type=Path)
    parser.add_argument("--plugin-config", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--llm-provider-id", default="")
    parser.add_argument("--embedding-provider-id", default="")
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--memory-id",
        action="append",
        type=int,
        help="Replay only this legacy memory ID; may be repeated.",
    )
    return parser.parse_args()


def main() -> None:
    _configure_logging()
    args = _parse_args()
    result = asyncio.run(_run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
