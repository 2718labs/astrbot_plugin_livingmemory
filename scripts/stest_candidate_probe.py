#!/usr/bin/env python3
"""Probe an isolated Stest candidate with natural queries and final packing.

The script reopens an already-built candidate directory.  It never opens or
writes the running LivingMemory database.  Generated queries and injection
payloads stay in a private report beside the candidate artifacts.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from stest_instance_replay import (
    NEGATIVE_QUERIES,
    OpenAIEmbeddingProvider,
    ProviderOpenAIOfficial,
    _build_engine,
    _configure_logging,
    _load_json,
    _provider_config,
    _result_view,
    _safe_engine_config,
)

from astrbot_plugin_livingmemory.core.utils.fact_packing import (
    format_fact_hits_for_injection,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_from_response(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1] if "\n" in value else ""
        value = value.rsplit("```", 1)[0].strip()
    start = value.find("{")
    end = value.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("query generator did not return a JSON object")
    parsed = json.loads(value[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("query generator JSON root must be an object")
    return parsed


async def _generate_queries(
    provider: ProviderOpenAIOfficial, facts: list[dict[str, str]]
) -> list[dict[str, str]]:
    prompt = (
        "为下面每条长期事实各写一个自然的中文追问，用来测试记忆召回。"
        "问题应像当事人在后续聊天里会说的话，可以省略主语并使用第一人称；"
        "不要使用‘前天’‘昨天’‘上周’这类会随评测日期改变的相对时间；"
        "若不保留一两个事实中的识别词就会变成无法回答的谜语，应保留必要识别词；"
        "不要逐字复制整条事实，不要在问题里直接给出答案，也不要合并两条。"
        "只输出 JSON：{\"queries\":[{\"fact_id\":\"...\",\"query\":\"...\"}]}。\n\n"
        + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
    )
    response = await provider.text_chat(
        prompt=prompt,
        system_prompt="你只负责生成检索评测问题，不回答问题。",
    )
    parsed = _json_from_response(response.completion_text)
    rows = parsed.get("queries")
    if not isinstance(rows, list):
        raise ValueError("query generator omitted queries array")
    expected = {item["fact_id"] for item in facts}
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        fact_id = str(row.get("fact_id") or "").strip()
        query = str(row.get("query") or "").strip()
        if fact_id not in expected or fact_id in seen or not query:
            continue
        seen.add(fact_id)
        output.append({"fact_id": fact_id, "query": query})
    if seen != expected:
        missing = sorted(expected - seen)
        raise ValueError(f"query generator omitted fact IDs: {missing}")
    return output


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    source_report_path = args.candidate_dir / "report.private.json"
    if not source_report_path.is_file():
        raise FileNotFoundError(source_report_path)
    private_output = args.candidate_dir / f"{args.report_stem}.private.json"
    summary_output = args.candidate_dir / f"{args.report_stem}.summary.json"
    if private_output.exists() or summary_output.exists():
        raise FileExistsError("natural probe report already exists in candidate directory")

    source_report = _load_json(source_report_path)
    fact_records = list(source_report.get("facts") or [])
    generation_input: list[dict[str, str]] = []
    parent_by_fact: dict[str, str] = {}
    overview_by_fact: dict[str, str] = {}
    fact_text_by_id: dict[str, str] = {}
    scope_by_fact: dict[str, str] = {}
    persona_by_fact: dict[str, str] = {}
    for record in fact_records:
        fact = dict(record.get("fact") or {})
        fact_id = str(fact.get("fact_id") or record.get("fact_id") or "")
        fact_text = str(fact.get("fact") or "").strip()
        if not fact_id or not fact_text:
            continue
        generation_input.append({"fact_id": fact_id, "fact": fact_text})
        fact_text_by_id[fact_id] = fact_text
        parent_by_fact[fact_id] = str(record.get("parent_id") or "")
        overview_by_fact[fact_id] = str(record.get("overview") or "").strip()
        scope_by_fact[fact_id] = str(record.get("scope") or "")
        persona_by_fact[fact_id] = str(record.get("persona_id") or "")
    if not generation_input:
        raise RuntimeError("candidate report has no facts")

    astrbot_config = _load_json(args.astrbot_config)
    plugin_config = _load_json(args.plugin_config)
    provider_section = dict(plugin_config.get("provider_settings") or {})
    llm_provider_id = str(provider_section.get("llm_provider_id") or "")
    embedding_provider_id = str(provider_section.get("embedding_provider_id") or "")
    llm_config = _provider_config(astrbot_config, llm_provider_id)
    embedding_config = _provider_config(astrbot_config, embedding_provider_id)
    provider_settings = dict(astrbot_config.get("provider_settings") or {})

    # This is a dedicated evaluation provider instance, so the deterministic
    # query-generation override cannot affect normal AstrBot conversations.
    custom_extra_body = dict(llm_config.get("custom_extra_body") or {})
    custom_extra_body.update({"temperature": 0.1, "top_p": 1})
    llm_config["custom_extra_body"] = custom_extra_body
    llm_provider = ProviderOpenAIOfficial(llm_config, provider_settings)
    embedding_provider = OpenAIEmbeddingProvider(embedding_config, provider_settings)
    engine_config = _safe_engine_config(plugin_config)
    engine, parent_vectors = await _build_engine(
        args.candidate_dir, embedding_provider, engine_config
    )
    try:
        rebuild_result = None
        if args.rebuild_indexes:
            rebuild_result = await engine.canonical_store.rebuild_indexes()
        if args.query_source:
            prior_report = _load_json(args.query_source)
            queries = [
                {
                    "fact_id": str(item.get("target_fact_id") or ""),
                    "query": str(item.get("query") or ""),
                }
                for item in prior_report.get("queries") or []
                if isinstance(item, dict)
            ]
            expected_ids = {item["fact_id"] for item in generation_input}
            if {item["fact_id"] for item in queries} != expected_ids:
                raise ValueError("query source does not cover the candidate fact set")
        else:
            queries = await _generate_queries(llm_provider, generation_input)
        query_by_fact = {item["fact_id"]: item["query"] for item in queries}
        rows: list[dict[str, Any]] = []
        for item in generation_input:
            fact_id = item["fact_id"]
            query = query_by_fact[fact_id]
            explained = await engine.explain_memory_search(
                query,
                k=args.top_k,
                session_id=scope_by_fact[fact_id] or None,
                persona_id=persona_by_fact[fact_id] or None,
            )
            hits = list(explained.get("results") or [])
            hit_ids = [
                str((getattr(hit, "metadata", {}) or {}).get("fact_id") or "")
                for hit in hits
            ]
            packed = engine.pack_memory_hits(hits)
            packed_ids = [
                str((getattr(hit, "metadata", {}) or {}).get("fact_id") or "")
                for hit in packed.hits
            ]
            payload = format_fact_hits_for_injection(packed.hits)
            target_parent = parent_by_fact[fact_id]
            substituted_sibling_ids = [
                packed_id
                for packed_id in packed_ids
                if packed_id != fact_id
                and parent_by_fact.get(packed_id, "") == target_parent
            ]
            overview = overview_by_fact[fact_id]
            rows.append(
                {
                    "target_fact_id": fact_id,
                    "query": query,
                    "hit_rank": hit_ids.index(fact_id) + 1 if fact_id in hit_ids else None,
                    "target_injected": fact_id in packed_ids,
                    "candidate_count": int(explained.get("candidate_count") or 0),
                    "hits": [_result_view(hit) for hit in hits],
                    "rejected": list(explained.get("rejected") or []),
                    "packed_fact_ids": packed_ids,
                    "injection_token_estimate": packed.token_count,
                    "injection_payload": payload,
                    "dropped": packed.dropped,
                    "same_parent_target_substitution_ids": substituted_sibling_ids,
                    "parent_summary_leaked": bool(
                        overview
                        and overview not in fact_text_by_id.values()
                        and overview in payload
                    ),
                }
            )

        default_scope = scope_by_fact[generation_input[0]["fact_id"]]
        default_persona = persona_by_fact[generation_input[0]["fact_id"]]
        negatives: list[dict[str, Any]] = []
        for query in NEGATIVE_QUERIES:
            explained = await engine.explain_memory_search(
                query,
                k=args.top_k,
                session_id=default_scope or None,
                persona_id=default_persona or None,
            )
            hits = list(explained.get("results") or [])
            packed = engine.pack_memory_hits(hits)
            negatives.append(
                {
                    "query": query,
                    "accepted_count": len(hits),
                    "injected_count": len(packed.hits),
                    "hits": [_result_view(hit) for hit in hits],
                }
            )

        ranks = [row["hit_rank"] for row in rows]
        summary = {
            "natural_query_count": len(rows),
            "hit_at_1": sum(rank == 1 for rank in ranks),
            "hit_at_k": sum(rank is not None for rank in ranks),
            "target_injected_count": sum(row["target_injected"] for row in rows),
            "same_parent_target_substitution_count": sum(
                len(row["same_parent_target_substitution_ids"]) for row in rows
            ),
            "parent_summary_leak_count": sum(
                row["parent_summary_leaked"] for row in rows
            ),
            "negative_injection_count": sum(
                row["injected_count"] > 0 for row in negatives
            ),
            "negative_query_count": len(negatives),
            "max_injection_token_estimate": max(
                row["injection_token_estimate"] for row in rows
            ),
            "index_status": await engine.get_canonical_index_status(),
        }
        manifest = {
            "candidate_label": args.candidate_label,
            "source_report": str(source_report_path),
            "source_report_sha256": _sha256(source_report_path),
            "query_generator_provider": llm_provider_id,
            "query_generator_temperature": 0.1,
            "query_source": str(args.query_source) if args.query_source else None,
            "embedding_provider": embedding_provider_id,
            "top_k": args.top_k,
            "indexes_rebuilt_before_probe": bool(args.rebuild_indexes),
            "rebuild_result": rebuild_result,
        }
        private_report = {
            "manifest": manifest,
            "summary": summary,
            "queries": rows,
            "negative_queries": negatives,
        }
        private_output.write_text(
            json.dumps(private_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        public_report = {"manifest": manifest, "summary": summary}
        summary_output.write_text(
            json.dumps(public_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return public_report
    finally:
        await engine.close()
        await parent_vectors.close()
        await llm_provider.terminate()
        await embedding_provider.terminate()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-dir", required=True, type=Path)
    parser.add_argument("--astrbot-config", required=True, type=Path)
    parser.add_argument("--plugin-config", required=True, type=Path)
    parser.add_argument("--candidate-label", required=True)
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--report-stem", default="natural_probe")
    parser.add_argument("--rebuild-indexes", action="store_true")
    parser.add_argument("--query-source", type=Path)
    return parser.parse_args()


def main() -> None:
    _configure_logging()
    result = asyncio.run(_run(_parse_args()))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
