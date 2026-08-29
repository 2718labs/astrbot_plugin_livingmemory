from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from astrbot.api.platform import MessageType
from astrbot_plugin_livingmemory.core.base.config_manager import ConfigManager
from astrbot_plugin_livingmemory.core.event_handler import EventHandler
from astrbot_plugin_livingmemory.core.managers.user_baseline_manager import (
    BASELINE_BATCH_WINDOWS,
    BASELINE_MAX_WINDOWS_PER_SCOPE,
    BASELINE_TOKEN_BUDGET,
    BaselineInjection,
    UserBaselineManager,
)
from astrbot_plugin_livingmemory.core.models.conversation_models import Message
from astrbot_plugin_livingmemory.storage.conversation_store import ConversationStore


class MutableConfig:
    def __init__(self, enabled: bool = False):
        self.values = {
            "user_baseline.enabled": enabled,
            "access_control.identity_aliases": "",
            "provider_settings.llm_provider_id": "",
            "reflection_engine.summary_trigger_rounds": 10,
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeContext:
    def __init__(self, response_text: str = '{"operations":[]}'):
        self.provider = Mock()
        self.provider.text_chat = AsyncMock(
            return_value=SimpleNamespace(completion_text=response_text)
        )

    def get_using_provider(self, *_args):
        return self.provider

    def get_provider_by_id(self, _provider_id):
        return self.provider


def _window(index: int, *, sender_id: str = "user-1", group_id=None):
    timestamp = 1_700_000_000 + index * 60
    return [
        Message(
            id=index * 2 - 1,
            session_id="test:session",
            role="user",
            content=f"用户窗口 {index}",
            sender_id=sender_id,
            sender_name="测试用户",
            group_id=group_id,
            platform="test",
            timestamp=timestamp,
        ),
        Message(
            id=index * 2,
            session_id="test:session",
            role="assistant",
            content=f"回复 {index}",
            sender_id="bot",
            sender_name="Bot",
            group_id=group_id,
            platform="test",
            timestamp=timestamp + 1,
            metadata={"is_bot_message": True},
        ),
    ]


async def _manager(tmp_path, *, enabled=False, response_text='{"operations":[]}'):
    config = MutableConfig(enabled=enabled)
    context = FakeContext(response_text)
    manager = UserBaselineManager(
        db_path=str(tmp_path / "livingmemory.db"),
        conversations_db_path=str(tmp_path / "conversations.db"),
        context=context,
        config_manager=config,
    )
    await manager.initialize()
    config.values["user_baseline.enabled"] = True
    return manager, config, context


@pytest.mark.asyncio
async def test_ten_windows_and_six_hour_cooldown_are_both_required(tmp_path):
    manager, _, _ = await _manager(tmp_path)
    try:
        for index in range(1, BASELINE_BATCH_WINDOWS):
            await manager.register_summary_window(
                session_id="test:session",
                history_messages=_window(index),
                persona_id="persona-a",
                schedule_generation=False,
            )
        users = await manager.list_users()
        user_id = users["items"][0]["user_id"]
        assert await manager._claim_generation_batch(user_id, "persona-a") is None

        await manager.register_summary_window(
            session_id="test:session",
            history_messages=_window(BASELINE_BATCH_WINDOWS),
            persona_id="persona-a",
            schedule_generation=False,
        )
        claim = await manager._claim_generation_batch(user_id, "persona-a")
        assert claim is not None
        assert len(claim[0]) == BASELINE_BATCH_WINDOWS
        assert await manager._claim_generation_batch(user_id, "persona-a") is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_invalid_json_retries_immediately_then_discards_profile_copies(
    tmp_path, caplog
):
    manager, _, context = await _manager(tmp_path, response_text="not json")
    try:
        for index in range(1, BASELINE_BATCH_WINDOWS + 1):
            await manager.register_summary_window(
                session_id="test:session",
                history_messages=_window(index),
                persona_id="persona-a",
                schedule_generation=False,
            )
        user_id = (await manager.list_users())["items"][0]["user_id"]
        await manager._maybe_generate(user_id, "persona-a")
        assert context.provider.text_chat.await_count == 2
        detail = await manager.get_user_detail(user_id)
        assert detail["states"][0]["pending_count"] == 0
        assert detail["states"][0]["cooldown_remaining"] > 0
        async with manager.db.execute(
            "SELECT COUNT(*) AS n FROM user_baseline_windows"
        ) as cur:
            row = await cur.fetchone()
        assert int(row["n"]) == 0
        async with manager.db.execute(
            "SELECT COUNT(*) AS n FROM user_baseline_evidence"
        ) as cur:
            evidence_row = await cur.fetchone()
        assert int(evidence_row["n"]) == 0
        listing = await manager.list_users()
        assert listing["generation_warning"]["occurred_at"] > 0
        assert listing["generation_warning"]["reasons"] == [
            "LLM 输出不是合法 JSON",
            "LLM 输出不是合法 JSON",
        ]
        assert "首次：LLM 输出不是合法 JSON" in caplog.text
        assert "重试：LLM 输出不是合法 JSON" in caplog.text
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_all_invalid_batch_retries_immediately_then_skips_without_blocking(
    tmp_path,
):
    response_text = json.dumps(
        {
            "operations": [
                {
                    "op": "add",
                    "category": "address_identity",
                    "content": "用户喜欢吃某种食物。",
                    "evidence_ids": ["W1:M1"],
                }
            ]
        },
        ensure_ascii=False,
    )
    manager, _, context = await _manager(tmp_path, response_text=response_text)
    try:
        for index in range(1, BASELINE_BATCH_WINDOWS + 1):
            await manager.register_summary_window(
                session_id="test:session",
                history_messages=_window(index),
                persona_id="persona-a",
                schedule_generation=False,
            )
        user_id = (await manager.list_users())["items"][0]["user_id"]
        await manager._maybe_generate(user_id, "persona-a")

        detail = await manager.get_user_detail(user_id)
        assert context.provider.text_chat.await_count == 2
        assert detail["states"][0]["pending_count"] == 0
        assert detail["states"][0]["cooldown_remaining"] > 0
        assert detail["entries"] == []

        async with manager.db.execute(
            "SELECT COUNT(*) AS n FROM user_baseline_windows"
        ) as cur:
            discarded = await cur.fetchone()
        assert int(discarded["n"]) == 0
        async with manager.db.execute(
            "SELECT COUNT(*) AS n FROM user_baseline_evidence"
        ) as cur:
            evidence_row = await cur.fetchone()
        assert int(evidence_row["n"]) == 0

        for index in range(
            BASELINE_BATCH_WINDOWS + 1, BASELINE_BATCH_WINDOWS * 2 + 1
        ):
            await manager.register_summary_window(
                session_id="test:session",
                history_messages=_window(index),
                persona_id="persona-a",
                schedule_generation=False,
            )
        context.provider.text_chat.return_value = SimpleNamespace(
            completion_text=json.dumps(
                {
                    "operations": [
                        {
                            "op": "add",
                            "category": "relationship",
                            "content": "用户与当前 persona 已明确约定为长期协作伙伴。",
                            "evidence_ids": ["W9:M17"],
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )
        await manager.db.execute(
            """
            UPDATE user_baseline_states SET last_attempt_at = 0
            WHERE user_id = ? AND persona_id = ?
            """,
            (user_id, "persona-a"),
        )
        await manager.db.commit()
        await manager._maybe_generate(user_id, "persona-a")

        detail = await manager.get_user_detail(user_id)
        assert context.provider.text_chat.await_count == 3
        assert detail["states"][0]["pending_count"] == 0
        assert len(detail["entries"]) == 1
        async with manager.db.execute(
            "SELECT status, COUNT(*) AS n FROM user_baseline_windows GROUP BY status"
        ) as cur:
            status_counts = {
                str(row["status"]): int(row["n"]) for row in await cur.fetchall()
            }
        assert status_counts == {"consumed": BASELINE_BATCH_WINDOWS}
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_provider_failure_keeps_profile_batch_and_evidence(tmp_path):
    manager, _, context = await _manager(tmp_path)
    context.provider.text_chat.side_effect = RuntimeError("provider unavailable")
    try:
        for index in range(1, BASELINE_BATCH_WINDOWS + 1):
            await manager.register_summary_window(
                session_id="test:session",
                history_messages=_window(index),
                persona_id="persona-a",
                schedule_generation=False,
            )
        user_id = (await manager.list_users())["items"][0]["user_id"]
        await manager._maybe_generate(user_id, "persona-a")

        assert context.provider.text_chat.await_count == 1
        detail = await manager.get_user_detail(user_id)
        assert detail["states"][0]["pending_count"] == BASELINE_BATCH_WINDOWS
        assert detail["states"][0]["cooldown_remaining"] > 0
        async with manager.db.execute(
            "SELECT COUNT(*) AS n FROM user_baseline_evidence"
        ) as cur:
            evidence_row = await cur.fetchone()
        assert int(evidence_row["n"]) == BASELINE_BATCH_WINDOWS * 2
        assert (await manager.list_users())["generation_warning"] is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_explicit_empty_operations_consumes_batch(tmp_path):
    manager, _, context = await _manager(tmp_path)
    try:
        for index in range(1, BASELINE_BATCH_WINDOWS + 1):
            await manager.register_summary_window(
                session_id="test:session",
                history_messages=_window(index),
                persona_id="persona-a",
                schedule_generation=False,
            )
        user_id = (await manager.list_users())["items"][0]["user_id"]
        await manager._maybe_generate(user_id, "persona-a")

        detail = await manager.get_user_detail(user_id)
        assert context.provider.text_chat.await_count == 1
        assert detail["states"][0]["pending_count"] == 0
        async with manager.db.execute(
            "SELECT status, COUNT(*) AS n FROM user_baseline_windows GROUP BY status"
        ) as cur:
            row = await cur.fetchone()
        assert dict(row) == {"status": "consumed", "n": BASELINE_BATCH_WINDOWS}
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_generation_consumes_oldest_ten_and_keeps_extra_window(tmp_path):
    manager, _, context = await _manager(tmp_path)
    try:
        for index in range(1, BASELINE_BATCH_WINDOWS + 2):
            await manager.register_summary_window(
                session_id="test:session",
                history_messages=_window(index),
                persona_id="persona-a",
                schedule_generation=False,
            )
        user_id = (await manager.list_users())["items"][0]["user_id"]
        context.provider.text_chat.return_value = SimpleNamespace(
            completion_text=json.dumps(
                {
                    "operations": [
                        {
                            "op": "add",
                            "category": "relationship",
                            "content": "用户与当前 persona 已明确确认彼此是朋友。",
                            "evidence_ids": ["W1:M1"],
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )
        await manager._maybe_generate(user_id, "persona-a")
        detail = await manager.get_user_detail(user_id)
        assert detail["states"][0]["pending_count"] == 1
        assert len(detail["entries"]) == 1
        assert detail["entries"][0]["locked"] is False
        assert detail["entries"][0]["evidence"] == [
            {
                "id": "W1:M1",
                "text": "用户窗口 1",
                "role": "user",
                "speaker_name": "测试用户",
                "timestamp": 1_700_000_060,
            }
        ]
        assert context.provider.text_chat.await_count == 1
        async with manager.db.execute(
            "SELECT evidence_id FROM user_baseline_evidence ORDER BY evidence_id"
        ) as cur:
            evidence_ids = {
                str(row["evidence_id"]) for row in await cur.fetchall()
            }
        assert evidence_ids == {"W1:M1", "W9:M17", "W9:M18"}
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_manual_edit_locks_unlocks_and_delete_suppresses_old_evidence(tmp_path):
    manager, _, _ = await _manager(tmp_path)
    try:
        await manager.register_summary_window(
            session_id="test:session",
            history_messages=_window(1),
            persona_id="persona-a",
            schedule_generation=False,
        )
        user_id = (await manager.list_users())["items"][0]["user_id"]
        detail = await manager.upsert_manual_entry(
            user_id=user_id,
            payload={
                "persona_id": "persona-a",
                "category": "interaction_preference",
                "content": "用户希望我回复时直说结论。",
                "enabled": True,
            },
        )
        entry = detail["entries"][0]
        assert entry["locked"] is True

        detail = await manager.upsert_manual_entry(
            user_id=user_id,
            payload={
                "entry_id": entry["entry_id"],
                "revision": entry["revision"],
                "locked": False,
            },
        )
        entry = detail["entries"][0]
        assert entry["locked"] is False

        detail = await manager.delete_manual_entry(
            user_id=user_id,
            entry_id=entry["entry_id"],
            revision=entry["revision"],
        )
        assert detail["entries"] == []
        assert await manager._is_suppressed_locked(
            user_id=user_id,
            persona_id="persona-a",
            content="用户希望我回复时直说结论。",
        )
        assert not await manager._is_suppressed_locked(
            user_id=user_id,
            persona_id="persona-a",
            content="用户希望我回复时少用列表。",
        )
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_manual_edit_during_generation_keeps_claimed_evidence(tmp_path):
    manager, _, _ = await _manager(tmp_path)
    try:
        for index in range(1, BASELINE_BATCH_WINDOWS + 1):
            await manager.register_summary_window(
                session_id="test:session",
                history_messages=_window(index),
                persona_id="persona-a",
                schedule_generation=False,
            )
        user_id = (await manager.list_users())["items"][0]["user_id"]
        claim = await manager._claim_generation_batch(user_id, "persona-a")
        assert claim is not None
        windows, claimed_revision = claim

        await manager.upsert_manual_entry(
            user_id=user_id,
            payload={
                "persona_id": "persona-a",
                "category": "interaction_preference",
                "content": "用户希望回复控制在三句话内。",
            },
        )
        applied = await manager._apply_generation(
            user_id=user_id,
            persona_id="persona-a",
            windows=windows,
            user_revision=claimed_revision,
            operations=[],
        )
        assert applied is None
        detail = await manager.get_user_detail(user_id)
        assert detail["states"][0]["pending_count"] == BASELINE_BATCH_WINDOWS
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_generation_prompt_and_backend_respect_dynamic_editability(tmp_path):
    manager, _, context = await _manager(tmp_path)
    try:
        await manager.register_summary_window(
            session_id="test:session",
            history_messages=_window(1),
            persona_id="persona-a",
            schedule_generation=False,
        )
        user_id = (await manager.list_users())["items"][0]["user_id"]

        async def add_entry(content, *, persona_id, category, unlock=False):
            detail = await manager.upsert_manual_entry(
                user_id=user_id,
                payload={
                    "persona_id": persona_id,
                    "category": category,
                    "content": content,
                },
            )
            entry = next(
                item for item in detail["entries"] if item["content"] == content
            )
            if unlock:
                detail = await manager.upsert_manual_entry(
                    user_id=user_id,
                    payload={
                        "entry_id": entry["entry_id"],
                        "revision": entry["revision"],
                        "locked": False,
                    },
                )
                entry = next(
                    item for item in detail["entries"] if item["content"] == content
                )
            return entry

        editable_entry = await add_entry(
            "用户希望回复控制在三句话内。",
            persona_id="persona-a",
            category="interaction_preference",
            unlock=True,
        )
        locked_entry = await add_entry(
            "用户希望回复中不要使用反问句。",
            persona_id="persona-a",
            category="interaction_preference",
        )
        global_entry = await add_entry(
            "用户要求所有 persona 都不得伪造事实。",
            persona_id="",
            category="global_constraint",
            unlock=True,
        )

        prompt_windows = [
            {
                "window_id": 1,
                "messages": [item.to_dict() for item in _window(1)],
                "started_at": 1.0,
                "ended_at": 2.0,
            }
        ]
        generation = await manager._call_generation_llm(
            user_id=user_id,
            persona_id="persona-a",
            windows=prompt_windows,
        )
        prompt = context.provider.text_chat.await_args.kwargs["prompt"]
        existing_json = prompt.split("现有画像：", 1)[1].split("\n\n适用删除记录：", 1)[0]
        prompt_entries = {item["content"]: item for item in json.loads(existing_json)}
        assert all("entry_id" not in item for item in prompt_entries.values())
        assert editable_entry["entry_id"] not in prompt
        assert locked_entry["entry_id"] not in prompt
        assert global_entry["entry_id"] not in prompt
        assert {item["entry_ref"] for item in prompt_entries.values()} == set(
            generation.entry_refs
        )
        assert prompt_entries[editable_entry["content"]]["editable"] is True
        assert prompt_entries[locked_entry["content"]]["editable"] is False
        assert prompt_entries[global_entry["content"]]["editable"] is False
        editable_ref = prompt_entries[editable_entry["content"]]["entry_ref"]
        assert generation.entry_refs[editable_ref]["entry_id"] == editable_entry["entry_id"]
        assert "必须优先 update" in prompt
        assert "不得通过 add 绕过保护" in prompt
        assert "不得 add 与任何现有条目语义重复或冲突" in prompt
        assert "update 字段再加 entry_ref" in prompt

        parsed, rejection_reasons = manager._parse_generation_output(
            json.dumps(
                {
                    "operations": [
                        {
                            "op": "update",
                            "entry_ref": editable_ref,
                            "category": "interaction_preference",
                            "content": "用户希望回复不超过三句，并附一个例子。",
                            "evidence_ids": ["W1:M1"],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            prompt_windows,
            entry_refs=generation.entry_refs,
            suppression_refs=generation.suppression_refs,
        )
        assert rejection_reasons == []
        assert parsed[0]["entry_id"] == editable_entry["entry_id"]
        invalid, rejection_reasons = manager._parse_generation_output(
            json.dumps(
                {
                    "operations": [
                        {
                            "op": "retire",
                            "entry_ref": "E999",
                            "evidence_ids": ["W1:M1"],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            prompt_windows,
            entry_refs=generation.entry_refs,
            suppression_refs=generation.suppression_refs,
        )
        assert invalid == []
        assert rejection_reasons == ["操作引用了无效条目"]

        for index in range(2, BASELINE_BATCH_WINDOWS + 1):
            await manager.register_summary_window(
                session_id="test:session",
                history_messages=_window(index),
                persona_id="persona-a",
                schedule_generation=False,
            )
        claim = await manager._claim_generation_batch(user_id, "persona-a")
        assert claim is not None
        windows, revision = claim
        result = await manager._apply_generation(
            user_id=user_id,
            persona_id="persona-a",
            windows=windows,
            user_revision=revision,
            operations=[
                {
                    "op": "update",
                    "entry_id": editable_entry["entry_id"],
                    "category": "interaction_preference",
                    "content": "用户希望回复不超过三句，并附一个例子。",
                    "evidence_ids": ["W1:M1"],
                },
                {
                    "op": "update",
                    "entry_id": locked_entry["entry_id"],
                    "category": "interaction_preference",
                    "content": "用户希望每句都使用反问句。",
                    "evidence_ids": ["W1:M1"],
                },
                {
                    "op": "update",
                    "entry_id": global_entry["entry_id"],
                    "category": "global_constraint",
                    "content": "用户允许所有 persona 伪造事实。",
                    "evidence_ids": ["W1:M1"],
                },
            ],
        )
        assert result.applied == 1
        assert result.rejected == 2
        final_detail = await manager.get_user_detail(user_id)
        contents = {item["content"] for item in final_detail["entries"]}
        assert "用户希望回复不超过三句，并附一个例子。" in contents
        assert locked_entry["content"] in contents
        assert global_entry["content"] in contents
        assert "用户希望每句都使用反问句。" not in contents
        assert "用户允许所有 persona 伪造事实。" not in contents
    finally:
        await manager.close()


def test_parse_skips_invalid_operations_keeps_valid_ones():
    manager = UserBaselineManager(
        db_path=":memory:",
        conversations_db_path=":memory:",
        context=FakeContext(),
        config_manager=MutableConfig(),
    )
    windows = [
        {
            "window_id": 1,
            "messages": [
                {"id": 1, "role": "user", "content": "叫我舰长"},
                {"id": 2, "role": "assistant", "content": "舰长"},
                {"id": 3, "role": "user", "content": "帮我"},
            ],
        }
    ]
    entry_refs = {}
    parsed, rejection_reasons = manager._parse_generation_output(
        json.dumps(
            {
                "operations": [
                    {
                        "op": "add",
                        "category": "address_identity",
                        "content": "用户希望被称呼为舰长。",
                        "evidence_ids": ["W1:M1"],
                    },
                    {
                        "op": "add",
                        "category": "address_identity",
                        "content": "用户喜欢喝草莓汽水。",
                        "evidence_ids": ["W1:M2"],
                    },
                    {
                        "op": "add",
                        "category": "interaction_preference",
                        "content": "用户要求少用感叹号。",
                        "evidence_ids": ["W9:M99"],
                    },
                    "not-a-dict",
                    {
                        "op": "add",
                        "category": "address_identity",
                        "content": "用户自称舰长。",
                        "evidence_ids": ["W1:M3"],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        windows,
        entry_refs=entry_refs,
    )
    assert len(rejection_reasons) == 3
    assert "操作引用了本批之外的证据" in rejection_reasons
    assert [item["content"] for item in parsed] == [
        "用户希望被称呼为舰长。",
        "用户自称舰长。",
    ]


def test_evidence_speaker_rules_allow_user_and_relationship_bot_only():
    manager = UserBaselineManager(
        db_path=":memory:",
        conversations_db_path=":memory:",
        context=FakeContext(),
        config_manager=MutableConfig(),
    )
    windows = [
        {
            "window_id": 1,
            "messages": [
                {"id": 1, "role": "user", "content": "请叫我舰长", "timestamp": 10},
                {"id": 2, "role": "assistant", "content": "我们是搭档", "timestamp": 11},
                {"id": 3, "role": "unknown", "content": "未知来源", "timestamp": 12},
            ],
        }
    ]
    parsed, rejection_reasons = manager._parse_generation_output(
        json.dumps(
            {
                "operations": [
                    {
                        "op": "add",
                        "category": "address_identity",
                        "content": "用户希望被称为舰长。",
                        "evidence_ids": ["W1:M1"],
                    },
                    {
                        "op": "add",
                        "category": "relationship",
                        "content": "用户与当前 persona 已确认是搭档。",
                        "evidence_ids": ["W1:M2"],
                    },
                    {
                        "op": "add",
                        "category": "interaction_preference",
                        "content": "用户要求简短回复。",
                        "evidence_ids": ["W1:M2"],
                    },
                    {
                        "op": "add",
                        "category": "long_term_boundary",
                        "content": "用户要求尊重未知边界。",
                        "evidence_ids": ["W1:M3"],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        windows,
        entry_refs={},
    )
    assert [item["category"] for item in parsed] == [
        "address_identity",
        "relationship",
    ]
    assert rejection_reasons == [
        "条目未引用目标用户原话",
        "条目未引用目标用户原话",
    ]


@pytest.mark.asyncio
async def test_evidence_metadata_manual_lifecycle_and_scope_dedup(tmp_path):
    manager, _, _ = await _manager(tmp_path)
    try:
        await manager.register_summary_window(
            session_id="test:session",
            history_messages=_window(1),
            persona_id="persona-a",
            schedule_generation=False,
        )
        user_id = (await manager.list_users())["items"][0]["user_id"]
        async with manager.db.execute(
            """
            SELECT message_role, speaker_name, message_timestamp
            FROM user_baseline_evidence WHERE evidence_id = 'W1:M1'
            """
        ) as cur:
            evidence = await cur.fetchone()
        assert dict(evidence) == {
            "message_role": "user",
            "speaker_name": "测试用户",
            "message_timestamp": 1_700_000_060,
        }

        detail = await manager.upsert_manual_entry(
            user_id=user_id,
            payload={
                "persona_id": "persona-a",
                "category": "interaction_preference",
                "content": "用户希望先说结论。",
                "allow_auto_update": True,
            },
        )
        entry = detail["entries"][0]
        await manager.db.execute(
            "UPDATE user_baseline_entries SET evidence_json = '[\"W1:M1\"]' WHERE entry_id = ?",
            (entry["entry_id"],),
        )
        await manager.db.commit()
        detail = await manager.upsert_manual_entry(
            user_id=user_id,
            payload={
                "entry_id": entry["entry_id"],
                "revision": entry["revision"],
                "enabled": False,
                "allow_auto_update": False,
                "locked": False,
            },
        )
        entry = detail["entries"][0]
        assert entry["evidence"][0]["role"] == "user"
        assert entry["allow_auto_update"] is False

        detail = await manager.upsert_manual_entry(
            user_id=user_id,
            payload={
                "entry_id": entry["entry_id"],
                "revision": entry["revision"],
                "persona_id": "persona-a",
                "category": "interaction_preference",
                "content": "用户希望先说结论，再给依据。",
                "enabled": True,
                "allow_auto_update": True,
            },
        )
        entry = detail["entries"][0]
        assert entry["evidence"] == []
        assert entry["source_type"] == "manual"
        assert entry["allow_auto_update"] is True

        detail = await manager.upsert_manual_entry(
            user_id=user_id,
            payload={
                "entry_id": entry["entry_id"],
                "revision": entry["revision"],
                "persona_id": "persona-a",
                "category": "interaction_preference",
                "content": "用户希望先说结论，再给简短依据。",
                "enabled": True,
            },
        )
        entry = detail["entries"][0]
        assert entry["allow_auto_update"] is False

        detail = await manager.upsert_manual_entry(
            user_id=user_id,
            payload={
                "entry_id": entry["entry_id"],
                "revision": entry["revision"],
                "persona_id": "",
                "category": "interaction_preference",
                "content": entry["content"],
                "enabled": True,
                "allow_auto_update": True,
            },
        )
        assert detail["entries"][0]["allow_auto_update"] is False
        with pytest.raises(ValueError, match="已存在相同内容"):
            await manager.upsert_manual_entry(
                user_id=user_id,
                payload={
                    "persona_id": "persona-b",
                    "category": "long_term_boundary",
                    "content": entry["content"],
                },
            )
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_apply_result_consumes_no_change_and_keeps_all_rejected(tmp_path):
    manager, _, _ = await _manager(tmp_path)
    try:
        await manager.register_summary_window(
            session_id="test:session",
            history_messages=_window(1),
            persona_id="persona-a",
            schedule_generation=False,
        )
        user_id = (await manager.list_users())["items"][0]["user_id"]
        revision = (await manager.get_user_detail(user_id))["revision"]
        result = await manager._apply_generation(
            user_id=user_id,
            persona_id="persona-a",
            windows=[{"window_id": 1, "ended_at": 1_700_000_061}],
            user_revision=revision,
            operations=[
                {
                    "op": "add",
                    "category": "interaction_preference",
                    "content": "用户希望先说结论。",
                    "evidence_ids": ["W1:M1"],
                },
                {
                    "op": "add",
                    "category": "long_term_boundary",
                    "content": "用户希望先说结论。",
                    "evidence_ids": ["W1:M1"],
                },
            ],
        )
        assert result == result.__class__(applied=1, no_change=1, rejected=0)
        async with manager.db.execute(
            "SELECT status FROM user_baseline_windows WHERE window_id = 1"
        ) as cur:
            assert (await cur.fetchone())["status"] == "consumed"

        await manager.register_summary_window(
            session_id="test:session",
            history_messages=_window(2),
            persona_id="persona-a",
            schedule_generation=False,
        )
        revision = (await manager.get_user_detail(user_id))["revision"]
        result = await manager._apply_generation(
            user_id=user_id,
            persona_id="persona-a",
            windows=[{"window_id": 2, "ended_at": 1_700_000_121}],
            user_revision=revision,
            operations=[
                {
                    "op": "update",
                    "entry_id": "missing",
                    "category": "interaction_preference",
                    "content": "用户希望简短回复。",
                    "evidence_ids": ["W2:M3"],
                }
            ],
        )
        assert result.rejected == 1
        async with manager.db.execute(
            "SELECT status FROM user_baseline_windows WHERE window_id = 2"
        ) as cur:
            assert (await cur.fetchone())["status"] == "pending"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_deleted_conclusion_requires_new_evidence_then_clears_suppression(tmp_path):
    manager, _, _ = await _manager(tmp_path)
    try:
        await manager.register_summary_window(
            session_id="test:session",
            history_messages=_window(1),
            persona_id="persona-a",
            schedule_generation=False,
        )
        user_id = (await manager.list_users())["items"][0]["user_id"]
        detail = await manager.upsert_manual_entry(
            user_id=user_id,
            payload={
                "persona_id": "persona-a",
                "category": "interaction_preference",
                "content": "用户希望回复时直说结论。",
            },
        )
        entry = detail["entries"][0]
        await manager.delete_manual_entry(
            user_id=user_id,
            entry_id=entry["entry_id"],
            revision=entry["revision"],
        )
        async with manager.db.execute(
            "SELECT suppression_id, deleted_at, content FROM user_baseline_suppressions"
        ) as cur:
            suppression = await cur.fetchone()
        refs = {
            "D1": {
                "suppression_id": int(suppression["suppression_id"]),
                "deleted_at": float(suppression["deleted_at"]),
                "content": str(suppression["content"]),
                "persona_id": "persona-a",
            }
        }
        old_windows = [
            {
                "window_id": 1,
                "messages": [item.to_dict() for item in _window(1)],
            }
        ]
        parsed, rejection_reasons = manager._parse_generation_output(
            json.dumps(
                {
                    "operations": [
                        {
                            "op": "add",
                            "category": "interaction_preference",
                            "content": suppression["content"],
                            "evidence_ids": ["W1:M1"],
                            "suppression_ref": "D1",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            old_windows,
            entry_refs={},
            suppression_refs=refs,
        )
        assert parsed == []
        assert rejection_reasons == ["恢复删除结论必须引用删除后的新证据"]

        future = time.time() + 10
        messages = _window(2)
        for offset, message in enumerate(messages):
            message.id = 20 + offset
            message.timestamp = future + offset
        await manager.register_summary_window(
            session_id="test:session",
            history_messages=messages,
            persona_id="persona-a",
            schedule_generation=False,
        )
        async with manager.db.execute(
            "SELECT MAX(window_id) AS window_id FROM user_baseline_windows"
        ) as cur:
            window_id = int((await cur.fetchone())["window_id"])
        new_windows = [
            {
                "window_id": window_id,
                "messages": [item.to_dict() for item in messages],
                "ended_at": future + 1,
            }
        ]
        parsed, rejection_reasons = manager._parse_generation_output(
            json.dumps(
                {
                    "operations": [
                        {
                            "op": "add",
                            "category": "interaction_preference",
                            "content": suppression["content"],
                            "evidence_ids": [f"W{window_id}:M20"],
                            "suppression_ref": "D1",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            new_windows,
            entry_refs={},
            suppression_refs=refs,
        )
        assert rejection_reasons == []
        revision = (await manager.get_user_detail(user_id))["revision"]
        result = await manager._apply_generation(
            user_id=user_id,
            persona_id="persona-a",
            windows=new_windows,
            user_revision=revision,
            operations=parsed,
        )
        assert result.applied == 1
        async with manager.db.execute(
            "SELECT COUNT(*) AS n FROM user_baseline_suppressions"
        ) as cur:
            assert int((await cur.fetchone())["n"]) == 0
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_empty_persona_skips_windows_but_global_manual_entry_injects(tmp_path):
    manager, _, _ = await _manager(tmp_path)
    try:
        assert await manager.register_summary_window(
            session_id="test:session",
            history_messages=_window(1),
            persona_id="",
            schedule_generation=False,
        ) == 0
        assert (await manager.list_users())["total"] == 0

        await manager.register_summary_window(
            session_id="test:session",
            history_messages=_window(1),
            persona_id="persona-a",
            schedule_generation=False,
        )
        user_id = (await manager.list_users())["items"][0]["user_id"]
        await manager.upsert_manual_entry(
            user_id=user_id,
            payload={
                "persona_id": "",
                "category": "global_constraint",
                "content": "用户要求不要伪造事实。",
            },
        )
        event = Mock()
        event.get_platform_name.return_value = "test"
        event.get_sender_id.return_value = "user-1"
        event.get_sender_name.return_value = "测试用户"
        event.unified_msg_origin = "test:session"
        injection = await manager.build_injection(event=event, persona_id=None)
        assert "不要伪造事实" in injection.text
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_bootstrap_uses_first_hand_sources_without_calling_llm(tmp_path):
    livingmemory_db = tmp_path / "livingmemory.db"
    source = [item.to_dict() for item in _window(1)]
    with sqlite3.connect(livingmemory_db) as db:
        db.executescript(
            """
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE memory_sources (
                memory_id INTEGER PRIMARY KEY,
                source_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        db.execute(
            "INSERT INTO documents(id, text, metadata) VALUES (1, ?, ?)",
            (
                "二手 canonical 内容不应成为输入",
                json.dumps(
                    {"source_session_id": "test:session", "persona_id": "persona-a"}
                ),
            ),
        )
        db.execute(
            "INSERT INTO memory_sources(memory_id, source_json, created_at, updated_at) VALUES (1, ?, 1, 1)",
            (json.dumps(source, ensure_ascii=False),),
        )

    config = MutableConfig(enabled=True)
    context = FakeContext()
    manager = UserBaselineManager(
        db_path=str(livingmemory_db),
        conversations_db_path=str(tmp_path / "conversations.db"),
        context=context,
        config_manager=config,
    )
    await manager.initialize()
    try:
        users = await manager.list_users()
        assert users["total"] == 1
        assert users["items"][0]["progress_count"] == 1
        context.provider.text_chat.assert_not_awaited()
        assert await manager.bootstrap_existing_data() == 0
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_bootstrap_does_not_join_across_already_registered_messages(tmp_path):
    manager, _, _ = await _manager(tmp_path)
    store = ConversationStore(str(tmp_path / "conversations.db"))
    await store.initialize()
    try:
        await manager.db.execute(
            "CREATE TABLE documents (id INTEGER PRIMARY KEY, metadata TEXT NOT NULL)"
        )
        await manager.db.execute(
            "INSERT INTO documents(id, metadata) VALUES (1, ?)",
            (
                json.dumps(
                    {
                        "source_session_id": "test:bootstrap",
                        "persona_id": "persona-a",
                    }
                ),
            ),
        )
        await manager.db.commit()

        for index in range(1, 6):
            await store.add_message(
                Message(
                    id=0,
                    session_id="test:bootstrap",
                    role="assistant" if index in {2, 5} else "user",
                    content=f"message-{index}",
                    sender_id="bot" if index in {2, 5} else "user-1",
                    sender_name="Bot" if index in {2, 5} else "测试用户",
                    platform="test",
                    timestamp=1_700_000_000 + index,
                    metadata={"is_bot_message": index in {2, 5}},
                )
            )
        await store.connection.execute(
            "UPDATE sessions SET metadata = ? WHERE session_id = ?",
            (json.dumps({"last_summarized_index": 5}), "test:bootstrap"),
        )
        await store.connection.commit()

        seeded = await manager._bootstrap_conversation_windows(
            {("test:bootstrap", 3)}
        )
        assert seeded == 2
        async with manager.db.execute(
            "SELECT messages_json FROM user_baseline_windows ORDER BY window_id"
        ) as cursor:
            windows = [json.loads(row["messages_json"]) for row in await cursor.fetchall()]
        assert [[item["id"] for item in window] for window in windows] == [
            [1, 2],
            [4, 5],
        ]
    finally:
        await store.close()
        await manager.close()


@pytest.mark.asyncio
async def test_evidence_mirror_survives_window_trim(tmp_path):
    manager, _, _ = await _manager(tmp_path)
    try:
        for index in range(1, BASELINE_MAX_WINDOWS_PER_SCOPE + 3):
            await manager.register_summary_window(
                session_id="test:session",
                history_messages=_window(index),
                persona_id="persona-a",
                schedule_generation=False,
            )
        user_id = (await manager.list_users())["items"][0]["user_id"]
        async with manager.db.execute(
            "SELECT window_id FROM user_baseline_windows WHERE user_id = ? ORDER BY window_id",
            (user_id,),
        ) as cur:
            window_ids = [int(row["window_id"]) for row in await cur.fetchall()]
        consumed = window_ids[:-1]
        for window_id in consumed:
            await manager.db.execute(
                "UPDATE user_baseline_windows SET status = 'consumed' WHERE window_id = ?",
                (window_id,),
            )
        await manager.db.commit()
        await manager._trim_windows_locked(user_id, "persona-a")
        await manager.db.commit()
        async with manager.db.execute(
            "SELECT window_id FROM user_baseline_windows WHERE user_id = ? ORDER BY window_id",
            (user_id,),
        ) as cur:
            remaining = await cur.fetchall()
        assert len(remaining) == BASELINE_MAX_WINDOWS_PER_SCOPE + 1
        oldest = int(remaining[0]["window_id"])
        assert oldest > 1
        async with manager.db.execute(
            "SELECT COUNT(*) AS n FROM user_baseline_evidence WHERE evidence_id = ?",
            ("W1:M1",),
        ) as cur:
            row = await cur.fetchone()
        assert int(row["n"]) == 1
        async with manager.db.execute(
            "SELECT message_text FROM user_baseline_evidence WHERE evidence_id = ?",
            (f"W{oldest}:M{oldest * 2 - 1}",),
        ) as cur:
            text_row = await cur.fetchone()
        assert text_row is not None
        assert str(text_row["message_text"]) == f"用户窗口 {oldest}"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_evidence_mirror_pruned_when_no_active_entry_references(tmp_path):
    manager, _, _ = await _manager(tmp_path)
    try:
        await manager.register_summary_window(
            session_id="test:session",
            history_messages=_window(1),
            persona_id="persona-a",
            schedule_generation=False,
        )
        user_id = (await manager.list_users())["items"][0]["user_id"]
        await manager.db.execute(
            "UPDATE user_baseline_windows SET status = 'consumed' WHERE user_id = ?",
            (user_id,),
        )
        await manager.db.commit()
        shared = await manager.upsert_manual_entry(
            user_id=user_id,
            payload={
                "persona_id": "persona-a",
                "category": "address_identity",
                "content": "共享证据条目",
            },
        )
        shared_entry = next(
            item for item in shared["entries"] if item["content"] == "共享证据条目"
        )
        await manager.db.execute(
            """
            UPDATE user_baseline_entries
            SET evidence_json = ?
            WHERE entry_id = ?
            """,
            ('["W1:M1"]', shared_entry["entry_id"]),
        )
        await manager.db.commit()
        await manager._prune_unreferenced_evidence_locked(user_id)
        await manager.db.commit()
        async with manager.db.execute(
            "SELECT evidence_id FROM user_baseline_evidence WHERE user_id = ? ORDER BY evidence_id",
            (user_id,),
        ) as cur:
            kept = {str(row["evidence_id"]) for row in await cur.fetchall()}
        assert "W1:M1" in kept
        assert "W1:M2" not in kept

        await manager.delete_manual_entry(
            user_id=user_id,
            entry_id=shared_entry["entry_id"],
            revision=shared_entry["revision"],
        )
        async with manager.db.execute(
            "SELECT COUNT(*) AS n FROM user_baseline_evidence WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        assert int(row["n"]) == 0
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_evidence_prune_preserves_pending_windows_then_removes_consumed(
    tmp_path,
):
    manager, _, _ = await _manager(tmp_path)
    try:
        await manager.register_summary_window(
            session_id="test:session",
            history_messages=_window(1),
            persona_id="persona-a",
            schedule_generation=False,
        )
        user_id = (await manager.list_users())["items"][0]["user_id"]

        await manager._prune_unreferenced_evidence_locked(user_id)
        await manager.db.commit()
        async with manager.db.execute(
            "SELECT evidence_id FROM user_baseline_evidence WHERE user_id = ?",
            (user_id,),
        ) as cur:
            pending_evidence = {
                str(row["evidence_id"]) for row in await cur.fetchall()
            }
        assert pending_evidence == {"W1:M1", "W1:M2"}

        await manager.db.execute(
            "UPDATE user_baseline_windows SET status = 'consumed' WHERE user_id = ?",
            (user_id,),
        )
        await manager._prune_unreferenced_evidence_locked(user_id)
        await manager.db.commit()
        async with manager.db.execute(
            "SELECT COUNT(*) AS n FROM user_baseline_evidence WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        assert int(row["n"]) == 0
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_initialize_repairs_missing_pending_evidence_mirrors(tmp_path):
    manager, _, _ = await _manager(tmp_path)
    db_path = manager.db_path
    try:
        await manager.register_summary_window(
            session_id="test:session",
            history_messages=_window(1),
            persona_id="persona-a",
            schedule_generation=False,
        )
        await manager.db.execute("DELETE FROM user_baseline_evidence")
        await manager.db.commit()
    finally:
        await manager.close()

    repaired = UserBaselineManager(
        db_path=db_path,
        conversations_db_path=str(tmp_path / "conversations.db"),
        context=FakeContext(),
        config_manager=MutableConfig(enabled=False),
    )
    await repaired.initialize()
    try:
        async with repaired.db.execute(
            """
            SELECT evidence_id, message_role, speaker_name, message_timestamp
            FROM user_baseline_evidence ORDER BY evidence_id
            """
        ) as cur:
            evidence = [dict(row) for row in await cur.fetchall()]
        assert evidence == [
            {
                "evidence_id": "W1:M1",
                "message_role": "user",
                "speaker_name": "测试用户",
                "message_timestamp": 1_700_000_060,
            },
            {
                "evidence_id": "W1:M2",
                "message_role": "assistant",
                "speaker_name": "Bot",
                "message_timestamp": 1_700_000_061,
            },
        ]
    finally:
        await repaired.close()


@pytest.mark.asyncio
async def test_initialize_migrates_existing_baseline_schema(tmp_path):
    db_path = tmp_path / "livingmemory.db"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE user_baseline_windows (
                window_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL,
                session_id TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                message_ids_json TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL NOT NULL,
                source_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                consumed_at REAL,
                UNIQUE(user_id, persona_id, fingerprint)
            );
            CREATE TABLE user_baseline_evidence (
                evidence_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                message_text TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE user_baseline_suppressions (
                suppression_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                deleted_at REAL NOT NULL,
                evidence_cutoff REAL NOT NULL,
                UNIQUE(user_id, persona_id, category, content_hash)
            );
            """
        )

    manager = UserBaselineManager(
        db_path=str(db_path),
        conversations_db_path=str(tmp_path / "conversations.db"),
        context=FakeContext(),
        config_manager=MutableConfig(enabled=False),
    )
    await manager.initialize()
    try:
        async with manager.db.execute(
            "PRAGMA table_info(user_baseline_evidence)"
        ) as cur:
            columns = {str(row["name"]) for row in await cur.fetchall()}
        assert {"message_role", "speaker_name", "message_timestamp"} <= columns
        async with manager.db.execute(
            "PRAGMA table_info(user_baseline_suppressions)"
        ) as cur:
            columns = {str(row["name"]) for row in await cur.fetchall()}
        assert "content" in columns
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_persona_entries_precede_global_and_manual_save_enforces_budget(tmp_path):
    manager, _, _ = await _manager(tmp_path)
    try:
        await manager.register_summary_window(
            session_id="test:session",
            history_messages=_window(1),
            persona_id="persona-a",
            schedule_generation=False,
        )
        user_id = (await manager.list_users())["items"][0]["user_id"]
        await manager.upsert_manual_entry(
            user_id=user_id,
            payload={
                "persona_id": "",
                "category": "global_constraint",
                "content": "用户要求任何 persona 都不要伪造事实。",
            },
        )
        await manager.upsert_manual_entry(
            user_id=user_id,
            payload={
                "persona_id": "persona-a",
                "category": "address_identity",
                "content": "当前 persona 可以称呼用户为舰长。",
            },
        )
        event = Mock()
        event.get_platform_name.return_value = "test"
        event.get_sender_id.return_value = "user-1"
        event.get_sender_name.return_value = "测试用户"
        event.unified_msg_origin = "test:session"
        injection = await manager.build_injection(event=event, persona_id="persona-a")
        assert injection.text.index("舰长") < injection.text.index("伪造事实")
        assert injection.token_count <= BASELINE_TOKEN_BUDGET

        for index in range(3):
            await manager.upsert_manual_entry(
                user_id=user_id,
                payload={
                    "persona_id": "persona-a",
                    "category": "long_term_boundary",
                    "content": ("界" * 179) + str(index),
                },
            )
        with pytest.raises(ValueError, match="800 token"):
            await manager.upsert_manual_entry(
                user_id=user_id,
                payload={
                    "persona_id": "persona-a",
                    "category": "long_term_boundary",
                    "content": ("界" * 179) + "3",
                },
            )
    finally:
        await manager.close()


def test_automatic_quality_gate_rejects_secondary_profile_material():
    with pytest.raises(ValueError, match="普通兴趣"):
        UserBaselineManager._validate_entry_content(
            "address_identity", "用户喜欢喝草莓汽水。", automatic=True
        )
    with pytest.raises(ValueError, match="普通兴趣"):
        UserBaselineManager._validate_entry_content(
            "interaction_preference", "用户喜欢喝草莓汽水。", automatic=True
        )
    with pytest.raises(ValueError, match="近期情绪"):
        UserBaselineManager._validate_entry_content(
            "relationship", "用户感到难过。", automatic=True
        )
    with pytest.raises(ValueError, match="性格推断"):
        UserBaselineManager._validate_entry_content(
            "address_identity", "用户看起来是敏感的人。", automatic=True
        )


def test_automatic_quality_gate_accepts_legitimate_interaction_preference():
    samples = [
        "用户允许 Bot 称呼其为舰长。",
        "用户希望对话中少用感叹号。",
        "用户希望 Bot 回复时先确认收到。",
        "用户要求 Bot 每段最多写三句。",
        "用户偏好短段落而非长列表。",
    ]
    for content in samples:
        UserBaselineManager._validate_entry_content(
            "interaction_preference", content, automatic=True
        )


def test_automatic_quality_gate_accepts_legitimate_relationship():
    samples = [
        "用户与 Bot 之间以“舰长”和“领航员”互称。",
        "用户确认 Bot 是长期协作伙伴。",
        "用户和 Bot 已约定以搭档身份相处。",
        "用户与 Bot 建立固定的协作关系。",
    ]
    for content in samples:
        UserBaselineManager._validate_entry_content(
            "relationship", content, automatic=True
        )


def test_group_windows_exclude_other_members_from_each_target():
    manager = UserBaselineManager(
        db_path=":memory:",
        conversations_db_path=":memory:",
        context=FakeContext(),
        config_manager=MutableConfig(),
    )
    messages = [
        *_window(1, sender_id="user-a", group_id="group-1"),
        Message(
            id=3,
            session_id="group",
            role="user",
            content="另一个成员的私密内容",
            sender_id="user-b",
            sender_name="B",
            group_id="group-1",
            platform="test",
        ),
    ]
    serialized, targets = manager._window_targets(messages)
    target_a = targets["test:user-a"]
    filtered = manager._messages_for_target(serialized, target_a)
    assert all(item.get("sender_id") != "user-b" for item in filtered)
    assert any(item.get("role") == "assistant" for item in filtered)


@pytest.mark.asyncio
async def test_top_k_zero_still_injects_user_baseline():
    baseline = Mock()
    baseline.build_injection = AsyncMock(
        return_value=BaselineInjection(
            text="[用户画像｜长期有效]\n- [称呼与身份] 用户可被称为舰长。\n[/用户画像]",
            entries=({"content": "用户可被称为舰长。"},),
            token_count=31,
            content_keys=frozenset({"用户可被称为舰长。"}),
        )
    )
    engine = Mock()
    engine.search_memories = AsyncMock()
    conversation = Mock()
    conversation.add_message_from_event = AsyncMock()
    conversation.clear_session = AsyncMock()
    conversation.store = Mock()
    handler = EventHandler(
        context=Mock(),
        config_manager=ConfigManager({"recall_engine": {"top_k": 0}}),
        memory_engine=engine,
        memory_processor=Mock(),
        conversation_manager=conversation,
        user_baseline_manager=baseline,
    )
    event = Mock()
    event.unified_msg_origin = "test:private"
    event.get_message_type.return_value = MessageType.FRIEND_MESSAGE
    event.get_message_str.return_value = "你好"
    event.get_sender_id.return_value = "user-1"
    event.get_sender_name.return_value = "测试用户"
    event.get_platform_name.return_value = "test"
    event.get_messages.return_value = []
    request = Mock(prompt="你好", contexts=[], extra_user_content_parts=[])

    with patch(
        "astrbot_plugin_livingmemory.core.event_handler_modules.memory_recall.get_persona_id",
        new=AsyncMock(return_value="persona-a"),
    ):
        await handler.handle_memory_recall(event, request)

    assert len(request.extra_user_content_parts) == 1
    assert "用户画像" in request.extra_user_content_parts[0].text
    engine.search_memories.assert_not_awaited()
