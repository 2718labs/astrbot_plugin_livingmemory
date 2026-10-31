"""Page API handlers for user baseline cards and manual entries."""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from quart import request


class UserBaselineHandler:
    def __init__(self, utils) -> None:
        self.utils = utils

    async def list_users(self, manager) -> dict[str, Any]:
        query = request.args
        keyword = str(query.get("keyword", "")).strip()
        try:
            page = max(1, int(query.get("page", 1)))
            page_size = min(100, max(1, int(query.get("page_size", 20))))
        except (TypeError, ValueError):
            return self.utils.error("分页参数无效")
        try:
            data = await manager.list_users(
                keyword=keyword, page=page, page_size=page_size
            )
            data["enabled"] = bool(manager.enabled)
            return self.utils.ok(data)
        except Exception as exc:
            logger.error("[PageAPI] 获取用户画像列表失败", exc_info=True)
            return self.utils.error(str(exc))

    async def dismiss_warning(self, manager) -> dict[str, Any]:
        try:
            dismissed = await manager.dismiss_generation_warning()
            return self.utils.ok({"dismissed": bool(dismissed)})
        except Exception as exc:
            logger.error("[PageAPI] 关闭用户画像告警失败", exc_info=True)
            return self.utils.error(str(exc))

    async def preview_consolidation(self, manager) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            user_id = int(payload.get("user_id", 0))
        except (TypeError, ValueError):
            return self.utils.error("user_id 必须是整数")
        if user_id <= 0:
            return self.utils.error("缺少 user_id")
        try:
            data = await manager.propose_consolidation(
                user_id=user_id, persona_id=str(payload.get("persona_id") or "")
            )
            return self.utils.ok(data)
        except (ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))
        except Exception as exc:
            logger.error("[PageAPI] 整理预览失败", exc_info=True)
            return self.utils.error(str(exc))

    async def apply_consolidation(self, manager) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            user_id = int(payload.get("user_id", 0))
        except (TypeError, ValueError):
            return self.utils.error("user_id 必须是整数")
        if user_id <= 0:
            return self.utils.error("缺少 user_id")
        try:
            detail = await manager.apply_consolidation(
                user_id=user_id,
                persona_id=str(payload.get("persona_id") or ""),
                operations=payload.get("operations"),
            )
            return self.utils.ok(detail)
        except (ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))
        except Exception as exc:
            logger.error("[PageAPI] 应用整理失败", exc_info=True)
            return self.utils.error(str(exc))

    async def save_portrait(self, manager) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            user_id = int(payload.get("user_id", 0))
        except (TypeError, ValueError):
            return self.utils.error("user_id 必须是整数")
        if user_id <= 0:
            return self.utils.error("缺少 user_id")
        persona_id = str(payload.get("persona_id") or "")
        try:
            detail = await manager.update_portrait(
                user_id=user_id,
                persona_id=persona_id,
                text=payload.get("text"),
                locked=bool(payload.get("locked", True)),
            )
            return self.utils.ok(detail)
        except (ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))
        except Exception as exc:
            logger.error("[PageAPI] 保存小作文失败", exc_info=True)
            return self.utils.error(str(exc))

    async def rewrite_portrait(self, manager) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            user_id = int(payload.get("user_id", 0))
        except (TypeError, ValueError):
            return self.utils.error("user_id 必须是整数")
        if user_id <= 0:
            return self.utils.error("缺少 user_id")
        persona_id = str(payload.get("persona_id") or "")
        try:
            detail = await manager.regenerate_portrait(
                user_id=user_id, persona_id=persona_id
            )
            return self.utils.ok(detail)
        except (ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))
        except Exception as exc:
            logger.error("[PageAPI] 重写小作文失败", exc_info=True)
            return self.utils.error(str(exc))

    async def rollback_portrait(self, manager) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            user_id = int(payload.get("user_id", 0))
            revision = int(payload.get("revision"))
        except (TypeError, ValueError):
            return self.utils.error("user_id 或 revision 无效")
        if user_id <= 0:
            return self.utils.error("缺少 user_id")
        try:
            detail = await manager.rollback_portrait(
                user_id=user_id,
                persona_id=str(payload.get("persona_id") or ""),
                revision=revision,
            )
            return self.utils.ok(detail)
        except (ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))
        except Exception as exc:
            logger.error("[PageAPI] 回滚小作文失败", exc_info=True)
            return self.utils.error(str(exc))

    async def list_portrait_revisions(self, manager) -> dict[str, Any]:
        try:
            user_id = int(request.args.get("user_id", 0))
        except (TypeError, ValueError):
            return self.utils.error("user_id 必须是整数")
        if user_id <= 0:
            return self.utils.error("缺少 user_id")
        try:
            revisions = await manager.list_portrait_revisions(
                user_id, str(request.args.get("persona_id") or "")
            )
            return self.utils.ok({"items": revisions})
        except Exception as exc:
            logger.error("[PageAPI] 获取小作文历史失败", exc_info=True)
            return self.utils.error(str(exc))

    async def detail(self, manager) -> dict[str, Any]:
        try:
            user_id = int(request.args.get("user_id", 0))
        except (TypeError, ValueError):
            return self.utils.error("user_id 必须是整数")
        if user_id <= 0:
            return self.utils.error("缺少 user_id")
        try:
            detail = await manager.get_user_detail(user_id)
            if detail is None:
                return self.utils.error("用户画像不存在")
            return self.utils.ok(detail)
        except Exception as exc:
            logger.error("[PageAPI] 获取用户画像详情失败", exc_info=True)
            return self.utils.error(str(exc))

    async def upsert_entry(self, manager) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            user_id = int(payload.get("user_id", 0))
        except (TypeError, ValueError):
            return self.utils.error("user_id 必须是整数")
        if user_id <= 0:
            return self.utils.error("缺少 user_id")
        try:
            detail = await manager.upsert_manual_entry(user_id=user_id, payload=payload)
            return self.utils.ok(detail)
        except (ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))
        except Exception as exc:
            logger.error("[PageAPI] 保存用户画像条目失败", exc_info=True)
            return self.utils.error(str(exc))

    async def delete_entry(self, manager) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        try:
            user_id = int(payload.get("user_id", 0))
            revision = int(payload.get("revision"))
        except (TypeError, ValueError):
            return self.utils.error("user_id 或 revision 无效")
        entry_id = str(payload.get("entry_id") or "").strip()
        if user_id <= 0 or not entry_id:
            return self.utils.error("缺少 user_id 或 entry_id")
        try:
            detail = await manager.delete_manual_entry(
                user_id=user_id,
                entry_id=entry_id,
                revision=revision,
                note=str(payload.get("note") or ""),
            )
            return self.utils.ok(detail)
        except (ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))
        except Exception as exc:
            logger.error("[PageAPI] 删除用户画像条目失败", exc_info=True)
            return self.utils.error(str(exc))

    async def delete_user(self, manager) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        if payload.get("confirm") is not True:
            return self.utils.error("删除整个用户画像需要确认")
        try:
            user_id = int(payload.get("user_id", 0))
            revision = int(payload.get("revision"))
        except (TypeError, ValueError):
            return self.utils.error("user_id 或 revision 无效")
        try:
            await manager.delete_user(user_id=user_id, revision=revision)
            return self.utils.ok({"deleted": True, "user_id": user_id})
        except (ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))
        except Exception as exc:
            logger.error("[PageAPI] 删除用户画像失败", exc_info=True)
            return self.utils.error(str(exc))


__all__ = ["UserBaselineHandler"]
