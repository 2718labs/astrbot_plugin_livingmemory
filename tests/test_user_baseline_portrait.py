from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_livingmemory.core.managers.user_baseline_manager import (
    PORTRAIT_MAX_CHARS,
    BaselineDeterministicFailure,
    UserBaselineManager,
)

PORTRAIT_TEXT = (
    "对方从事电气工程方向，正在准备硕士研究生招生考试。"
    "平时会让我帮忙查时间安排并设提醒，回复尽量直给、别绕弯子。"
)


class PortraitConfig:
    def __init__(self) -> None:
        self.values = {
            "user_baseline.enabled": True,
            "access_control.identity_aliases": "",
            "provider_settings.llm_provider_id": "",
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


class PortraitContext:
    """Returns prose for portrait prompts and an empty batch for entry prompts."""

    def __init__(self, portrait_text: str = PORTRAIT_TEXT) -> None:
        self.portrait_text = portrait_text
        self.prompts: list[str] = []

        async def reply(*, prompt, system_prompt=None):
            self.prompts.append(prompt)
            if "常驻使用的散文" in prompt:
                return SimpleNamespace(completion_text=self.portrait_text)
            return SimpleNamespace(completion_text='{"operations":[]}')

        self.provider = Mock()
        self.provider.text_chat = AsyncMock(side_effect=reply)

    def get_using_provider(self, *_args):
        return self.provider

    def get_provider_by_id(self, _provider_id):
        return self.provider


async def _manager(tmp_path, *, enabled: bool = True):
    context = PortraitContext()
    manager = UserBaselineManager(
        db_path=str(tmp_path / "livingmemory.db"),
        conversations_db_path=str(tmp_path / "conversations.db"),
        context=context,
        config_manager=PortraitConfig(),
    )
    await manager.initialize()
    if not enabled:
        manager.config_manager.values["user_baseline.enabled"] = False
    return manager, context


async def _seed_user(manager) -> int:
    manager.config_manager.values["user_baseline.enabled"] = True
    await manager.upsert_manual_entry(
        user_id=await _create_user(manager),
        payload={
            "persona_id": "persona-a",
            "category": "address_identity",
            "content": "用户是准备考研的电气工程专业往届生。",
            "allow_auto_update": True,
        },
    )
    return (await manager.list_users())["items"][0]["user_id"]


async def _create_user(manager) -> int:
    if manager.db is None:
        raise RuntimeError("db missing")
    import time

    now = time.time()
    cursor = await manager.db.execute(
        """
        INSERT INTO user_baseline_users(
            user_key, platform, canonical_identity, display_name, created_at, updated_at
        ) VALUES ('test:user-1', 'test', 'user-1', '测试用户', ?, ?)
        """,
        (now, now),
    )
    await manager.db.execute(
        """
        INSERT INTO user_baseline_states(user_id, persona_id, created_at, updated_at)
        VALUES (?, 'persona-a', ?, ?)
        """,
        (cursor.lastrowid, now, now),
    )
    await manager.db.commit()
    return int(cursor.lastrowid)


def _event() -> Mock:
    event = Mock()
    event.get_platform_name.return_value = "test"
    event.get_sender_id.return_value = "user-1"
    event.get_sender_name.return_value = "测试用户"
    event.unified_msg_origin = "test:session"
    return event


def test_portrait_validation_rejects_non_prose():
    manager = UserBaselineManager.__new__(UserBaselineManager)
    with pytest.raises(BaselineDeterministicFailure):
        manager._validate_portrait_text("")
    with pytest.raises(BaselineDeterministicFailure):
        manager._validate_portrait_text("- 第一条\n- 第二条")
    with pytest.raises(BaselineDeterministicFailure):
        manager._validate_portrait_text("[用户画像] 内容")
    with pytest.raises(BaselineDeterministicFailure):
        manager._validate_portrait_text("字" * (PORTRAIT_MAX_CHARS + 1))
    assert manager._validate_portrait_text(PORTRAIT_TEXT) == PORTRAIT_TEXT


def test_portrait_cleaning_strips_fence_and_label():
    manager = UserBaselineManager.__new__(UserBaselineManager)
    assert manager._clean_portrait_text("```text\n小作文：正文内容\n```") == "正文内容"
    assert manager._clean_portrait_text("用户画像: 正文内容") == "正文内容"


@pytest.mark.asyncio
async def test_rewrite_portrait_stores_text_and_revision(tmp_path):
    manager, context = await _manager(tmp_path)
    try:
        user_id = await _seed_user(manager)
        portrait = await manager.rewrite_portrait(
            user_id=user_id, persona_id="persona-a"
        )
        assert portrait is not None
        assert portrait["text"] == PORTRAIT_TEXT
        assert portrait["locked"] is False
        assert portrait["revision"] == 1
        assert any("常驻使用的散文" in prompt for prompt in context.prompts)

        revisions = await manager.list_portrait_revisions(user_id, "persona-a")
        assert [item["revision"] for item in revisions] == [1]
        assert revisions[0]["source"] == "auto"

        detail = await manager.get_user_detail(user_id)
        assert detail["portraits"][0]["text"] == PORTRAIT_TEXT
        assert detail["portraits"][0]["stale"] is False
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_injection_uses_portrait_without_duplicating_locked_entries(tmp_path):
    manager, _ = await _manager(tmp_path)
    try:
        user_id = await _seed_user(manager)
        await manager.upsert_manual_entry(
            user_id=user_id,
            payload={
                "persona_id": "persona-a",
                "category": "long_term_boundary",
                "content": "不要叫我前辈。",
            },
        )
        await manager.rewrite_portrait(user_id=user_id, persona_id="persona-a")
        injection = await manager.build_injection(
            event=_event(), persona_id="persona-a"
        )
        assert PORTRAIT_TEXT in injection.text
        assert injection.text.count("[用户画像｜长期有效]") == 1
        assert "必须原样遵守" not in injection.text
        assert "不要叫我前辈" not in injection.text
        assert injection.entries
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_stale_portrait_falls_back_to_entry_bullets(tmp_path):
    manager, _ = await _manager(tmp_path)
    try:
        user_id = await _seed_user(manager)
        await manager.rewrite_portrait(user_id=user_id, persona_id="persona-a")
        await manager.upsert_manual_entry(
            user_id=user_id,
            payload={
                "persona_id": "persona-a",
                "category": "interaction_preference",
                "content": "回复尽量简短。",
                "allow_auto_update": True,
            },
        )
        detail = await manager.get_user_detail(user_id)
        assert detail["portraits"][0]["stale"] is True

        injection = await manager.build_injection(
            event=_event(), persona_id="persona-a"
        )
        assert PORTRAIT_TEXT not in injection.text
        assert "回复尽量简短" in injection.text
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_locked_portrait_survives_rewrite_until_forced(tmp_path):
    manager, context = await _manager(tmp_path)
    try:
        user_id = await _seed_user(manager)
        await manager.rewrite_portrait(user_id=user_id, persona_id="persona-a")
        await manager.update_portrait(
            user_id=user_id,
            persona_id="persona-a",
            text="用户是考研的电气生，说话喜欢直接。",
            locked=True,
        )
        calls_before = context.provider.text_chat.await_count
        kept = await manager.rewrite_portrait(user_id=user_id, persona_id="persona-a")
        assert kept["text"] == "用户是考研的电气生，说话喜欢直接。"
        assert kept["locked"] is True
        assert context.provider.text_chat.await_count == calls_before

        forced = await manager.regenerate_portrait(
            user_id=user_id, persona_id="persona-a"
        )
        assert forced["portraits"][0]["text"] == PORTRAIT_TEXT
        assert forced["portraits"][0]["locked"] is False
        assert forced["portraits"][0]["revision"] == 3
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_portrait_rollback_restores_earlier_version(tmp_path):
    manager, _ = await _manager(tmp_path)
    try:
        user_id = await _seed_user(manager)
        await manager.rewrite_portrait(user_id=user_id, persona_id="persona-a")
        first = await manager.get_user_detail(user_id)
        first_revision = first["portraits"][0]["revision"]

        await manager.update_portrait(
            user_id=user_id,
            persona_id="persona-a",
            text="换一版说法。",
            locked=True,
        )
        detail = await manager.rollback_portrait(
            user_id=user_id, persona_id="persona-a", revision=first_revision
        )
        assert detail["portraits"][0]["text"] == PORTRAIT_TEXT
        assert detail["portraits"][0]["locked"] is True
        assert detail["portraits"][0]["revision"] == 3

        revisions = await manager.list_portrait_revisions(user_id, "persona-a")
        assert [item["revision"] for item in revisions] == [3, 2, 1]
        assert revisions[0]["source"] == "rollback"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_rewrite_without_entries_returns_none(tmp_path):
    manager, _ = await _manager(tmp_path)
    try:
        user_id = await _create_user(manager)
        assert (
            await manager.rewrite_portrait(user_id=user_id, persona_id="persona-a")
            is None
        )
    finally:
        await manager.close()

