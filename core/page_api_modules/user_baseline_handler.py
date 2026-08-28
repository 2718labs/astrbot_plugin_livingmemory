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
            logger.error("[PageAPI] 获取用户底座列表失败", exc_info=True)
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
                return self.utils.error("用户底座不存在")
            return self.utils.ok(detail)
        except Exception as exc:
            logger.error("[PageAPI] 获取用户底座详情失败", exc_info=True)
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
            logger.error("[PageAPI] 保存用户底座条目失败", exc_info=True)
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
                user_id=user_id, entry_id=entry_id, revision=revision
            )
            return self.utils.ok(detail)
        except (ValueError, RuntimeError) as exc:
            return self.utils.error(str(exc))
        except Exception as exc:
            logger.error("[PageAPI] 删除用户底座条目失败", exc_info=True)
            return self.utils.error(str(exc))

    async def delete_user(self, manager) -> dict[str, Any]:
        payload = await request.get_json(silent=True) or {}
        if payload.get("confirm") is not True:
            return self.utils.error("删除整个用户底座需要确认")
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
            logger.error("[PageAPI] 删除用户底座失败", exc_info=True)
            return self.utils.error(str(exc))


__all__ = ["UserBaselineHandler"]
