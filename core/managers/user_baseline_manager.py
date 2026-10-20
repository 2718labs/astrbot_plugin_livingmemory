"""Durable, user-scoped baseline facts and their first-hand evidence queue."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import aiosqlite
from astrbot.api import logger

from ..memory_scope import parse_identity_aliases
from ..models.conversation_models import Message
from ..utils.fact_packing import token_upper_bound

BASELINE_BATCH_WINDOWS = 8
BASELINE_COOLDOWN_SECONDS = 6 * 60 * 60
BASELINE_MAX_ENTRIES = 12
BASELINE_TOKEN_BUDGET = 800
BASELINE_MAX_WINDOWS_PER_SCOPE = 40
PORTRAIT_TOKEN_BUDGET = 360
PORTRAIT_MAX_CHARS = 420
PORTRAIT_HISTORY_LIMIT = 20
BASELINE_SUPPRESSION_LIMIT = 10
BASELINE_SUPPRESSION_KEEP = 50
BASELINE_SUPPRESSION_NOTE_MAX = 120
GLOBAL_PERSONA = ""

BASELINE_CATEGORIES = {
    "address_identity",
    "relationship",
    "interaction_preference",
    "long_term_boundary",
    "global_constraint",
}

_CATEGORY_LABELS = {
    "address_identity": "称呼与身份",
    "relationship": "关系锚点",
    "interaction_preference": "互动偏好",
    "long_term_boundary": "长期边界",
    "global_constraint": "硬约束",
}

_TEMPORARY_PATTERN = re.compile(
    r"(?:今天|今晚|明天|下周|刚才|现在|目前|这会儿|最近|暂时|临时|待会|一会儿|本次|这次|当前任务)"
)
_MOOD_PATTERN = re.compile(
    r"(?:心情|情绪|难过|伤心|开心|生气|烦躁|焦虑|困|累|郁闷|沮丧|兴奋)"
)
_MACHINE_ID_PATTERN = re.compile(
    r"(?:\b(?:window|entry|message|fact|parent)[_-]?id\b|\b[WM]\d+\b)",
    re.IGNORECASE,
)
_ORDINARY_INTEREST_PATTERN = re.compile(
    r"(?:喜欢|爱)(?:吃|喝|看|听|玩|买|收藏)|(?:最爱|偏爱)(?:的)?(?:食物|饮料|电影|音乐|游戏)"
)
_PERSONALITY_INFERENCE_PATTERN = re.compile(
    r"(?:性格|人格|看起来|似乎|大概|可能是|应该是).{0,12}(?:的人|性格|内向|外向|敏感|理性|感性)"
)


@dataclass(slots=True, frozen=True)
class BaselineInjection:
    text: str
    entries: tuple[dict[str, Any], ...]
    token_count: int
    content_keys: frozenset[str]


@dataclass(slots=True, frozen=True)
class BaselineGenerationResponse:
    text: str
    entry_refs: dict[str, dict[str, Any]]
    suppression_refs: dict[str, dict[str, Any]]


@dataclass(slots=True, frozen=True)
class BaselineApplyResult:
    applied: int = 0
    no_change: int = 0
    rejected: int = 0
    rejection_reasons: tuple[str, ...] = ()


class BaselineDeterministicFailure(ValueError):
    """A generation result failed its deterministic output contract."""


@dataclass(slots=True, frozen=True)
class BaselineIdentity:
    platform: str
    canonical_identity: str
    display_name: str
    sender_ids: frozenset[str]

    @property
    def user_key(self) -> str:
        return f"{self.platform.casefold()}:{self.canonical_identity.casefold()}"


def _flat_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _content_key(value: Any) -> str:
    return _flat_text(value).casefold()


def _content_hash(value: Any) -> str:
    return hashlib.sha256(_content_key(value).encode("utf-8")).hexdigest()


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _message_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        data = dict(message)
    elif hasattr(message, "to_dict"):
        data = dict(message.to_dict())
    else:
        data = {
            key: getattr(message, key, None)
            for key in (
                "id",
                "session_id",
                "role",
                "content",
                "sender_id",
                "sender_name",
                "group_id",
                "platform",
                "timestamp",
                "metadata",
            )
        }
    data["content"] = Message.content_to_text(data.get("content"))
    data["metadata"] = _json_dict(data.get("metadata"))
    return data


class UserBaselineManager:
    """Own baseline storage, generation cadence, injection, and manual edits."""

    batch_windows = BASELINE_BATCH_WINDOWS
    token_budget = BASELINE_TOKEN_BUDGET

    def __init__(
        self,
        *,
        db_path: str,
        conversations_db_path: str,
        context: Any,
        config_manager: Any,
    ) -> None:
        self.db_path = str(db_path)
        self.conversations_db_path = str(conversations_db_path)
        self.context = context
        self.config_manager = config_manager
        self.db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._scope_locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._generation_tasks: set[asyncio.Task] = set()
        self._portrait_tasks: set[asyncio.Task] = set()
        self._portrait_locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._closing = False

    @property
    def enabled(self) -> bool:
        return bool(self.config_manager.get("user_baseline.enabled", False))

    @property
    def portrait_enabled(self) -> bool:
        return bool(self.config_manager.get("user_baseline.portrait", True))

    async def initialize(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode = WAL")
        await self.db.execute("PRAGMA busy_timeout = 10000")
        await self.db.execute("PRAGMA foreign_keys = ON")
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS user_baseline_users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_key TEXT NOT NULL UNIQUE,
                platform TEXT NOT NULL,
                canonical_identity TEXT NOT NULL,
                display_name TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_baseline_states (
                user_id INTEGER NOT NULL,
                persona_id TEXT NOT NULL DEFAULT '',
                last_attempt_at REAL,
                last_success_at REAL,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(user_id, persona_id),
                FOREIGN KEY(user_id) REFERENCES user_baseline_users(user_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_baseline_windows (
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
                UNIQUE(user_id, persona_id, fingerprint),
                FOREIGN KEY(user_id) REFERENCES user_baseline_users(user_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_user_baseline_windows_pending
            ON user_baseline_windows(user_id, persona_id, status, window_id);

            CREATE TABLE IF NOT EXISTS user_baseline_entries (
                entry_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                persona_id TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                content_key TEXT NOT NULL,
                source_type TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                locked INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                retired_at REAL,
                FOREIGN KEY(user_id) REFERENCES user_baseline_users(user_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_user_baseline_entries_scope
            ON user_baseline_entries(user_id, persona_id, enabled, retired_at);

            CREATE TABLE IF NOT EXISTS user_baseline_suppressions (
                suppression_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                deleted_at REAL NOT NULL,
                evidence_cutoff REAL NOT NULL,
                UNIQUE(user_id, persona_id, category, content_hash),
                FOREIGN KEY(user_id) REFERENCES user_baseline_users(user_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_baseline_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_baseline_evidence (
                evidence_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                message_text TEXT NOT NULL,
                message_role TEXT NOT NULL DEFAULT 'unknown',
                speaker_name TEXT NOT NULL DEFAULT '',
                message_timestamp REAL,
                created_at REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES user_baseline_users(user_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_baseline_portraits (
                user_id INTEGER NOT NULL,
                persona_id TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL DEFAULT '',
                entries_hash TEXT NOT NULL DEFAULT '',
                locked INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'auto',
                revision INTEGER NOT NULL DEFAULT 0,
                token_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY(user_id, persona_id),
                FOREIGN KEY(user_id) REFERENCES user_baseline_users(user_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_baseline_portrait_revisions (
                portrait_revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL,
                text TEXT NOT NULL,
                entries_hash TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'auto',
                created_at REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES user_baseline_users(user_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_user_baseline_portrait_revisions
            ON user_baseline_portrait_revisions(user_id, persona_id, revision);
            """
        )
        cursor = await self.db.execute("PRAGMA table_info(user_baseline_evidence)")
        evidence_columns = {str(row["name"]) for row in await cursor.fetchall()}
        for column, definition in (
            ("message_role", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("speaker_name", "TEXT NOT NULL DEFAULT ''"),
            ("message_timestamp", "REAL"),
        ):
            if column not in evidence_columns:
                await self.db.execute(
                    f"ALTER TABLE user_baseline_evidence ADD COLUMN {column} {definition}"
                )
        cursor = await self.db.execute("PRAGMA table_info(user_baseline_suppressions)")
        suppression_columns = {str(row["name"]) for row in await cursor.fetchall()}
        if "content" not in suppression_columns:
            await self.db.execute(
                "ALTER TABLE user_baseline_suppressions "
                "ADD COLUMN content TEXT NOT NULL DEFAULT ''"
            )
        if "note" not in suppression_columns:
            await self.db.execute(
                "ALTER TABLE user_baseline_suppressions "
                "ADD COLUMN note TEXT NOT NULL DEFAULT ''"
            )
        await self.db.commit()
        await self._repair_evidence_mirrors()
        if self.enabled:
            await self.bootstrap_existing_data()

    async def close(self) -> None:
        self._closing = True
        for task in list(self._generation_tasks) + list(self._portrait_tasks):
            task.cancel()
        if self._generation_tasks or self._portrait_tasks:
            await asyncio.gather(
                *self._generation_tasks, *self._portrait_tasks, return_exceptions=True
            )
        self._generation_tasks.clear()
        self._portrait_tasks.clear()
        if self.db is not None:
            await self.db.close()
            self.db = None

    def _aliases(self) -> dict[str, str]:
        return parse_identity_aliases(
            self.config_manager.get("access_control.identity_aliases", "")
        )

    def _identity_for_message(self, message: dict[str, Any]) -> BaselineIdentity | None:
        role = str(message.get("role") or "").casefold()
        metadata = _json_dict(message.get("metadata"))
        if role != "user" or metadata.get("is_bot_message"):
            return None
        platform = _flat_text(message.get("platform")) or "unknown"
        sender_id = _flat_text(message.get("sender_id"))
        sender_name = _flat_text(message.get("sender_name"))
        if not sender_id and not sender_name:
            return None
        aliases = self._aliases()
        canonical = ""
        for candidate in (
            f"{platform}:{sender_id}" if sender_id else "",
            sender_id,
            sender_name,
        ):
            if candidate and candidate.casefold() in aliases:
                canonical = aliases[candidate.casefold()]
                break
        canonical = canonical or sender_id or sender_name
        display_name = (
            canonical if canonical != sender_id else (sender_name or canonical)
        )
        return BaselineIdentity(
            platform=platform,
            canonical_identity=canonical,
            display_name=display_name,
            sender_ids=frozenset(item for item in (sender_id,) if item),
        )

    def _window_targets(
        self, messages: Iterable[Any]
    ) -> tuple[list[dict[str, Any]], dict[str, BaselineIdentity]]:
        serialized = [_message_dict(message) for message in messages]
        targets: dict[str, BaselineIdentity] = {}
        sender_sets: dict[str, set[str]] = {}
        for message in serialized:
            identity = self._identity_for_message(message)
            if identity is None:
                continue
            sender_id = _flat_text(message.get("sender_id"))
            existing = targets.get(identity.user_key)
            if existing is None:
                targets[identity.user_key] = identity
                sender_sets[identity.user_key] = set(identity.sender_ids)
            else:
                sender_sets[identity.user_key].update(identity.sender_ids)
                if identity.display_name:
                    targets[identity.user_key] = BaselineIdentity(
                        platform=existing.platform,
                        canonical_identity=existing.canonical_identity,
                        display_name=identity.display_name,
                        sender_ids=frozenset(sender_sets[identity.user_key]),
                    )
            if sender_id:
                sender_sets[identity.user_key].add(sender_id)
        for user_key, identity in list(targets.items()):
            targets[user_key] = BaselineIdentity(
                platform=identity.platform,
                canonical_identity=identity.canonical_identity,
                display_name=identity.display_name,
                sender_ids=frozenset(sender_sets[user_key]),
            )
        return serialized, targets

    @staticmethod
    def _messages_for_target(
        messages: list[dict[str, Any]], identity: BaselineIdentity
    ) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "").casefold()
            metadata = _json_dict(message.get("metadata"))
            is_bot = role == "assistant" or bool(metadata.get("is_bot_message"))
            sender_id = _flat_text(message.get("sender_id"))
            if is_bot or sender_id in identity.sender_ids:
                filtered.append(message)
        return filtered

    @staticmethod
    def _window_fingerprint(
        *, session_id: str, persona_id: str, messages: list[dict[str, Any]]
    ) -> str:
        material = {
            "session": session_id,
            "persona": persona_id,
            "messages": [
                {
                    "id": int(message.get("id") or 0),
                    "role": str(message.get("role") or ""),
                    "sender": str(message.get("sender_id") or ""),
                    "content": _flat_text(message.get("content")),
                    "timestamp": float(message.get("timestamp") or 0.0),
                }
                for message in messages
            ],
        }
        return hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    async def register_summary_window(
        self,
        *,
        session_id: str,
        history_messages: Iterable[Any],
        persona_id: str | None,
        source_type: str = "live",
        schedule_generation: bool = True,
    ) -> int:
        """Copy one valid main-memory window into each real user's evidence queue."""
        if not self.enabled or self.db is None:
            return 0
        normalized_persona = _flat_text(persona_id)
        if not normalized_persona:
            return 0
        serialized, targets = self._window_targets(history_messages)
        if not serialized or not targets:
            return 0
        inserted = 0
        for identity in targets.values():
            target_messages = self._messages_for_target(serialized, identity)
            if not any(
                str(item.get("role") or "").casefold() == "user"
                for item in target_messages
            ):
                continue
            result = await self._register_target_window(
                identity=identity,
                persona_id=normalized_persona,
                session_id=session_id,
                messages=target_messages,
                source_type=source_type,
            )
            if result is None:
                continue
            user_id, pending_count = result
            inserted += 1
            if not source_type.startswith("bootstrap_"):
                logger.info(
                    f"[用户画像] 新窗口已登记；累计进度 "
                    f"{min(pending_count, BASELINE_BATCH_WINDOWS)}/{BASELINE_BATCH_WINDOWS}。"
                )
            if schedule_generation:
                self._schedule_generation(user_id, normalized_persona)
        return inserted

    async def _register_target_window(
        self,
        *,
        identity: BaselineIdentity,
        persona_id: str,
        session_id: str,
        messages: list[dict[str, Any]],
        source_type: str,
    ) -> tuple[int, int] | None:
        if self.db is None:
            return None
        now = time.time()
        timestamps = [
            float(item.get("timestamp") or 0.0)
            for item in messages
            if float(item.get("timestamp") or 0.0) > 0
        ]
        started_at = min(timestamps or [now]) or now
        ended_at = max(timestamps or [now]) or now
        message_ids = list(
            dict.fromkeys(
                int(item.get("id") or 0)
                for item in messages
                if int(item.get("id") or 0) > 0
            )
        )
        fingerprint = self._window_fingerprint(
            session_id=session_id, persona_id=persona_id, messages=messages
        )
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self.db.execute(
                    """
                    INSERT INTO user_baseline_users(
                        user_key, platform, canonical_identity, display_name,
                        revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(user_key) DO UPDATE SET
                        display_name = excluded.display_name,
                        updated_at = excluded.updated_at
                    """,
                    (
                        identity.user_key,
                        identity.platform,
                        identity.canonical_identity,
                        identity.display_name,
                        now,
                        now,
                    ),
                )
                cursor = await self.db.execute(
                    "SELECT user_id FROM user_baseline_users WHERE user_key = ?",
                    (identity.user_key,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise RuntimeError("用户画像身份写入失败")
                user_id = int(row["user_id"])
                await self.db.execute(
                    """
                    INSERT INTO user_baseline_states(
                        user_id, persona_id, revision, created_at, updated_at
                    ) VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(user_id, persona_id) DO NOTHING
                    """,
                    (user_id, persona_id, now, now),
                )
                cursor = await self.db.execute(
                    """
                    INSERT OR IGNORE INTO user_baseline_windows(
                        user_id, persona_id, fingerprint, session_id,
                        messages_json, message_ids_json, started_at, ended_at,
                        source_type, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        user_id,
                        persona_id,
                        fingerprint,
                        session_id,
                        json.dumps(messages, ensure_ascii=False),
                        json.dumps(message_ids),
                        started_at,
                        ended_at,
                        source_type,
                        now,
                    ),
                )
                was_inserted = cursor.rowcount > 0
                if was_inserted:
                    await self._backfill_evidence_locked(
                        user_id=user_id,
                        window_id=int(cursor.lastrowid),
                        messages=messages,
                        created_at=now,
                    )
                cursor = await self.db.execute(
                    """
                    SELECT COUNT(*) AS pending_count
                    FROM user_baseline_windows
                    WHERE user_id = ? AND persona_id = ? AND status = 'pending'
                    """,
                    (user_id, persona_id),
                )
                pending_row = await cursor.fetchone()
                pending_count = int(pending_row["pending_count"] if pending_row else 0)
                await self._trim_windows_locked(user_id, persona_id)
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        return (user_id, pending_count) if was_inserted else None

    async def _backfill_evidence_locked(
        self,
        *,
        user_id: int,
        window_id: int,
        messages: list[dict[str, Any]],
        created_at: float,
    ) -> None:
        """Mirror first-hand message texts into a window-independent evidence
        table so the WebUI can show quoted sources even after the original
        window rows are trimmed by BASELINE_MAX_WINDOWS_PER_SCOPE."""
        if self.db is None:
            return
        rows = []
        for message in messages:
            message_id = int(message.get("id") or 0)
            text = _flat_text(message.get("content"))
            if message_id <= 0 or not text:
                continue
            metadata = _json_dict(message.get("metadata"))
            raw_role = str(message.get("role") or "").casefold()
            if raw_role == "assistant" or metadata.get("is_bot_message"):
                message_role = "assistant"
            elif raw_role == "user":
                message_role = "user"
            else:
                message_role = "unknown"
            try:
                message_timestamp = float(message.get("timestamp") or 0.0) or None
            except (TypeError, ValueError):
                message_timestamp = None
            rows.append(
                (
                    f"W{window_id}:M{message_id}",
                    user_id,
                    text,
                    message_role,
                    _flat_text(message.get("sender_name")),
                    message_timestamp,
                    created_at,
                )
            )
        if not rows:
            return
        await self.db.executemany(
            """
            INSERT OR IGNORE INTO user_baseline_evidence(
                evidence_id, user_id, message_text, message_role,
                speaker_name, message_timestamp, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await self.db.executemany(
            """
            UPDATE user_baseline_evidence
            SET message_role = CASE
                    WHEN message_role IN ('', 'unknown') THEN ? ELSE message_role END,
                speaker_name = CASE
                    WHEN speaker_name = '' THEN ? ELSE speaker_name END,
                message_timestamp = COALESCE(message_timestamp, ?)
            WHERE evidence_id = ? AND user_id = ?
            """,
            [
                (row[3], row[4], row[5], row[0], row[1])
                for row in rows
            ],
        )

    async def _trim_windows_locked(self, user_id: int, persona_id: str) -> None:
        if self.db is None:
            return
        cursor = await self.db.execute(
            """
            SELECT window_id FROM user_baseline_windows
            WHERE user_id = ? AND persona_id = ?
              AND status = 'consumed'
            ORDER BY window_id DESC
            """,
            (user_id, persona_id),
        )
        rows = await cursor.fetchall()
        stale = [int(row["window_id"]) for row in rows[BASELINE_MAX_WINDOWS_PER_SCOPE:]]
        if stale:
            placeholders = ",".join("?" for _ in stale)
            await self.db.execute(
                f"DELETE FROM user_baseline_windows WHERE window_id IN ({placeholders})",
                stale,
            )

    def _schedule_generation(self, user_id: int, persona_id: str) -> None:
        if self._closing:
            return
        task = asyncio.create_task(self._maybe_generate(user_id, persona_id))
        self._generation_tasks.add(task)
        task.add_done_callback(self._generation_tasks.discard)

    async def _maybe_generate(self, user_id: int, persona_id: str) -> None:
        lock = self._scope_locks.setdefault((user_id, persona_id), asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            claim = await self._claim_generation_batch(user_id, persona_id)
            if claim is None:
                return
            windows, user_revision = claim
            failure_reasons: list[str] = []
            for attempt in range(2):
                try:
                    generation = await self._call_generation_llm(
                        user_id=user_id,
                        persona_id=persona_id,
                        windows=windows,
                    )
                    operations, parse_rejection_reasons = self._parse_generation_output(
                        generation.text,
                        windows,
                        entry_refs=generation.entry_refs,
                        suppression_refs=generation.suppression_refs,
                    )
                    result = await self._apply_generation(
                        user_id=user_id,
                        persona_id=persona_id,
                        windows=windows,
                        user_revision=user_revision,
                        operations=operations,
                        initial_rejection_reasons=parse_rejection_reasons,
                    )
                    if result is None:
                        logger.info(
                            "[用户画像] 生成期间检测到人工编辑，本批未应用；"
                            "画像证据副本已保留。"
                        )
                        return
                    if (
                        result.applied == 0
                        and result.no_change == 0
                        and result.rejected
                    ):
                        raise BaselineDeterministicFailure(
                            self._summarize_rejection_reasons(
                                result.rejection_reasons
                            )
                            or "所有操作均未通过校验"
                        )
                    logger.info(
                        f"[用户画像] 生成完成；应用 {result.applied} 项，"
                        f"无变化 {result.no_change} 项，拒绝 {result.rejected} 项，"
                        f"消费 {len(windows)} 个窗口。"
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except BaselineDeterministicFailure as exc:
                    failure_reasons.append(str(exc))
                    if attempt == 0:
                        logger.info(
                            f"[用户画像] 本批生成结果无效（{failure_reasons[0]}），"
                            "立即重试一次。"
                        )
                        continue
                    try:
                        discarded = await self._discard_failed_batch(
                            user_id=user_id,
                            persona_id=persona_id,
                            windows=windows,
                            user_revision=user_revision,
                            failure_reasons=tuple(failure_reasons),
                        )
                    except Exception:
                        logger.warning(
                            "[用户画像] 本批连续两次生成结果无效；"
                            "跳过失败，画像证据副本已保留。",
                            exc_info=True,
                        )
                        return
                    if discarded:
                        logger.warning(
                            "[用户画像] 本批连续两次生成结果无效"
                            f"（首次：{failure_reasons[0]}；"
                            f"重试：{failure_reasons[1]}），"
                            f"已丢弃 {len(windows)} 个画像窗口副本；后续窗口不受影响。"
                        )
                    else:
                        logger.info(
                            "[用户画像] 生成期间检测到人工编辑；"
                            "本批未跳过，画像证据副本已保留。"
                        )
                    return
                except Exception as exc:
                    logger.warning(
                        "[用户画像] 生成失败，画像证据副本已保留并进入 6 小时冷却: "
                        f"{exc}"
                    )
                    return

    async def _claim_generation_batch(
        self, user_id: int, persona_id: str
    ) -> tuple[list[dict[str, Any]], int] | None:
        if self.db is None or not self.enabled:
            return None
        now = time.time()
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    """
                    SELECT last_attempt_at FROM user_baseline_states
                    WHERE user_id = ? AND persona_id = ?
                    """,
                    (user_id, persona_id),
                )
                state = await cursor.fetchone()
                if state is None:
                    await self.db.rollback()
                    return None
                last_attempt = float(state["last_attempt_at"] or 0.0)
                if last_attempt and now - last_attempt < BASELINE_COOLDOWN_SECONDS:
                    await self.db.rollback()
                    return None
                cursor = await self.db.execute(
                    """
                    SELECT window_id, messages_json, started_at, ended_at
                    FROM user_baseline_windows
                    WHERE user_id = ? AND persona_id = ? AND status = 'pending'
                    ORDER BY window_id ASC LIMIT ?
                    """,
                    (user_id, persona_id, BASELINE_BATCH_WINDOWS),
                )
                rows = await cursor.fetchall()
                if len(rows) < BASELINE_BATCH_WINDOWS:
                    await self.db.rollback()
                    return None
                cursor = await self.db.execute(
                    "SELECT revision FROM user_baseline_users WHERE user_id = ?",
                    (user_id,),
                )
                user_row = await cursor.fetchone()
                if user_row is None:
                    await self.db.rollback()
                    return None
                await self.db.execute(
                    """
                    UPDATE user_baseline_states
                    SET last_attempt_at = ?, updated_at = ?
                    WHERE user_id = ? AND persona_id = ?
                    """,
                    (now, now, user_id, persona_id),
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        windows = [
            {
                "window_id": int(row["window_id"]),
                "messages": _json_list(row["messages_json"]),
                "started_at": float(row["started_at"] or 0.0),
                "ended_at": float(row["ended_at"] or 0.0),
            }
            for row in rows
        ]
        return windows, int(user_row["revision"])

    async def _discard_failed_batch(
        self,
        *,
        user_id: int,
        persona_id: str,
        windows: list[dict[str, Any]],
        user_revision: int,
        failure_reasons: tuple[str, str],
    ) -> bool:
        """Delete one twice-invalid profile batch without touching source memory."""
        if self.db is None:
            return False
        window_ids = [int(item["window_id"]) for item in windows]
        if not window_ids:
            return False
        placeholders = ",".join("?" for _ in window_ids)
        now = time.time()
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    "SELECT revision FROM user_baseline_users WHERE user_id = ?",
                    (user_id,),
                )
                user_row = await cursor.fetchone()
                if user_row is None or int(user_row["revision"]) != user_revision:
                    await self.db.rollback()
                    return False
                cursor = await self.db.execute(
                    f"""
                    SELECT window_id
                    FROM user_baseline_windows
                    WHERE user_id = ? AND persona_id = ? AND status = 'pending'
                      AND window_id IN ({placeholders})
                    """,
                    [user_id, persona_id, *window_ids],
                )
                rows = await cursor.fetchall()
                if len(rows) != len(window_ids):
                    await self.db.rollback()
                    return False
                await self.db.execute(
                    f"""
                    DELETE FROM user_baseline_windows
                    WHERE user_id = ? AND persona_id = ?
                      AND status = 'pending'
                      AND window_id IN ({placeholders})
                    """,
                    [user_id, persona_id, *window_ids],
                )
                await self._prune_unreferenced_evidence_locked(user_id)
                await self.db.execute(
                    """
                    INSERT OR REPLACE INTO user_baseline_meta(key, value, updated_at)
                    VALUES ('generation-warning-v1', ?, ?)
                    """,
                    (
                        json.dumps(
                            {"reasons": list(failure_reasons)},
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        return True

    def _resolve_provider(self):
        provider_id = _flat_text(
            self.config_manager.get("provider_settings.llm_provider_id", "")
        )
        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
                if provider:
                    return provider
            except Exception:
                pass
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    async def _call_generation_llm(
        self,
        *,
        user_id: int,
        persona_id: str,
        windows: list[dict[str, Any]],
    ) -> BaselineGenerationResponse:
        provider = self._resolve_provider()
        if provider is None:
            raise RuntimeError("LLM Provider 不可用")
        existing = await self._load_entries(user_id, persona_id, include_disabled=True)
        entry_refs = {
            f"E{index}": {
                "entry_id": item["entry_id"],
                "category": item["category"],
                "persona_id": item["persona_id"],
            }
            for index, item in enumerate(existing, start=1)
        }
        existing_payload = [
            {
                "entry_ref": f"E{index}",
                "category": item["category"],
                "content": item["content"],
                "persona_id": item["persona_id"] or None,
                "locked": bool(item["locked"]),
                "enabled": bool(item["enabled"]),
                "editable": bool(
                    persona_id
                    and item["persona_id"] == persona_id
                    and not item["locked"]
                ),
            }
            for index, item in enumerate(existing, start=1)
        ]
        suppression_rows: list[aiosqlite.Row] = []
        if self.db is not None:
            cursor = await self.db.execute(
                """
                SELECT suppression_id, persona_id, content, note, deleted_at
                FROM user_baseline_suppressions
                WHERE user_id = ? AND persona_id IN (?, '') AND content != ''
                ORDER BY deleted_at DESC, suppression_id DESC
                LIMIT ?
                """,
                (user_id, persona_id, BASELINE_SUPPRESSION_LIMIT),
            )
            suppression_rows = await cursor.fetchall()
        suppression_refs = {
            f"D{index}": {
                "suppression_id": int(row["suppression_id"]),
                "persona_id": str(row["persona_id"] or ""),
                "content": str(row["content"]),
                "deleted_at": float(row["deleted_at"]),
            }
            for index, row in enumerate(suppression_rows, start=1)
        }
        suppression_payload = [
            {
                "suppression_ref": ref,
                "scope": value["persona_id"] or "global",
                "content": value["content"],
                "note": _flat_text(suppression_rows[index - 1]["note"]),
                "deleted_at": value["deleted_at"],
            }
            for index, (ref, value) in enumerate(suppression_refs.items(), start=1)
        ]
        evidence_blocks: list[str] = []
        for window in windows:
            lines = [f"[W{window['window_id']}]"]
            for message in window["messages"]:
                role = str(message.get("role") or "user")
                name = _flat_text(message.get("sender_name")) or (
                    "Bot" if role == "assistant" else "用户"
                )
                message_id = int(message.get("id") or 0)
                content = _flat_text(message.get("content"))
                if content:
                    lines.append(
                        f"- W{window['window_id']}:M{message_id} {name}: {content}"
                    )
            evidence_blocks.append("\n".join(lines))
        prompt = (
            "请从以下一手对话证据中维护用户画像。只输出合法 JSON，"
            '结构为 {"operations":[...]}; operations 只允许 add、update、retire。\n'
            "允许类别：address_identity（称呼或身份锚点）、relationship（明确发生过的关系锚点）、"
            "interaction_preference（用户明确要求 Bot 如何互动）、long_term_boundary（长期边界）、"
            "global_constraint（极少量全局硬约束）。\n"
            "禁止写近期情绪、性格推断、普通兴趣、当前任务、短期约定或关系好感度；"
            "除 relationship 外不能从 Bot 单方面说法推断用户事实。"
            "每个操作必须带 evidence_ids，且只能引用下方 W:M。\n"
            "add 字段：op/category/content/evidence_ids；update 字段再加 entry_ref；"
            "retire 字段：op/entry_ref/evidence_ids。entry_ref 只能原样使用现有画像中的 E 编号。"
            "现有画像中的 editable 由系统实时计算："
            "只有 editable=true 的条目允许 update 或 retire；editable=false 的条目只供参考，"
            "不得修改、退役，也不得通过 add 绕过保护来改写其结论。\n"
            "若新证据是在补充、修正或自然演化某条 editable=true 的条目，必须优先 update，"
            "可在证据支持下调整措辞、侧重点和关系表述；只有没有可承接条目时才 add。"
            "不得 add 与任何现有条目语义重复或冲突的近义版本。"
            "若确有删除后的新证据恢复某条已删除结论，add/update 必须带对应 suppression_ref；"
            "suppression_ref 只能原样使用删除记录中的 D 编号。"
            "删除记录中的 note 是用户删除时写的原因，只用来避免重复生成同类结论，"
            "不得写进任何条目内容。"
            "内容必须是中性、自包含、长期有效的一句话，不得包含证据编号。\n\n"
            f"当前 persona：{persona_id or '未命名'}\n"
            f"现有画像：{json.dumps(existing_payload, ensure_ascii=False)}\n\n"
            f"适用删除记录：{json.dumps(suppression_payload, ensure_ascii=False)}\n\n"
            "本批证据：\n" + "\n\n".join(evidence_blocks)
        )
        response = await provider.text_chat(
            prompt=prompt,
            system_prompt=(
                "你只从给定的一手对话证据维护少量长期有效用户锚点，"
                "优先演化可编辑条目，保护只读锚点，不推测人格。"
            ),
        )
        text = getattr(response, "completion_text", "")
        if not isinstance(text, str) or not text.strip():
            raise BaselineDeterministicFailure("LLM 返回空响应")
        return BaselineGenerationResponse(
            text=text,
            entry_refs=entry_refs,
            suppression_refs=suppression_refs,
        )

    @staticmethod
    def _strip_json_fence(value: str) -> str:
        text = value.strip()
        if text.startswith("```"):
            first_newline = text.find("\n")
            if first_newline >= 0:
                text = text[first_newline + 1 :]
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        return text.strip()

    def _parse_generation_output(
        self,
        response_text: str,
        windows: list[dict[str, Any]],
        *,
        entry_refs: dict[str, dict[str, Any]],
        suppression_refs: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        try:
            payload = json.loads(self._strip_json_fence(response_text))
        except (TypeError, json.JSONDecodeError) as exc:
            raise BaselineDeterministicFailure("LLM 输出不是合法 JSON") from exc
        operations = payload.get("operations") if isinstance(payload, dict) else None
        if not isinstance(operations, list):
            raise BaselineDeterministicFailure("LLM 输出缺少 operations 数组")
        evidence_by_id = {
            f"W{window['window_id']}:M{int(message.get('id') or 0)}": {
                "role": (
                    "assistant"
                    if str(message.get("role") or "").casefold() == "assistant"
                    or _json_dict(message.get("metadata")).get("is_bot_message")
                    else "user"
                    if str(message.get("role") or "").casefold() == "user"
                    else "unknown"
                ),
                "timestamp": self._message_timestamp(message.get("timestamp")),
            }
            for window in windows
            for message in window["messages"]
            if int(message.get("id") or 0) > 0
        }
        parsed: list[dict[str, Any]] = []
        rejection_reasons: list[str] = []
        for index, raw in enumerate(operations):
            item = self._parse_operation(
                raw=raw,
                evidence_by_id=evidence_by_id,
                entry_refs=entry_refs,
                suppression_refs=suppression_refs or {},
            )
            if isinstance(item, dict):
                parsed.append(item)
            else:
                rejection_reasons.append(item)
                logger.debug(
                    f"[用户画像] 跳过第 {index + 1} 条 operation（{item}）"
                )
        return parsed, rejection_reasons

    @staticmethod
    def _summarize_rejection_reasons(reasons: tuple[str, ...] | list[str]) -> str:
        counts = Counter(reason for reason in reasons if reason)
        return "；".join(
            f"{count} 个{reason}" for reason, count in counts.items()
        )

    @staticmethod
    def _message_timestamp(value: Any) -> float | None:
        try:
            timestamp = float(value or 0.0)
        except (TypeError, ValueError):
            return None
        return timestamp if timestamp > 0 else None

    @staticmethod
    def _parse_operation(
        *,
        raw: Any,
        evidence_by_id: dict[str, dict[str, Any]],
        entry_refs: dict[str, dict[str, Any]],
        suppression_refs: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | str:
        if not isinstance(raw, dict):
            return "操作不是对象"
        op = _flat_text(raw.get("op")).casefold()
        if op not in {"add", "update", "retire"}:
            return "操作类型无效"
        evidence_ids = raw.get("evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            return "操作未引用证据"
        normalized_evidence = [_flat_text(item) for item in evidence_ids]
        if any(item not in evidence_by_id for item in normalized_evidence):
            return "操作引用了本批之外的证据"
        item: dict[str, Any] = {
            "op": op,
            "evidence_ids": list(dict.fromkeys(normalized_evidence)),
        }
        if op in {"update", "retire"}:
            entry_ref = _flat_text(raw.get("entry_ref")).upper()
            if not entry_ref:
                return "更新或退役操作缺少条目引用"
            entry = entry_refs.get(entry_ref)
            if entry is None:
                return "操作引用了无效条目"
            item["entry_id"] = entry["entry_id"]
        if op in {"add", "update"}:
            category = _flat_text(raw.get("category"))
            content = _flat_text(raw.get("content"))
            try:
                UserBaselineManager._validate_entry_content(
                    category, content, automatic=True
                )
            except ValueError as exc:
                return str(exc)
            item.update(category=category, content=content)
        else:
            category = str(entry["category"])
        qualifying_roles = {"user", "assistant"} if category == "relationship" else {"user"}
        if not any(
            evidence_by_id[evidence_id]["role"] in qualifying_roles
            for evidence_id in item["evidence_ids"]
        ):
            if category == "relationship":
                return "关系条目未引用目标用户原话或当前 persona 的 Bot 发言"
            return "条目未引用目标用户原话"
        suppression_ref = _flat_text(raw.get("suppression_ref")).upper()
        if suppression_ref:
            if op not in {"add", "update"}:
                return "退役操作不能引用删除记录"
            suppression = suppression_refs.get(suppression_ref)
            if suppression is None:
                return "操作引用了无效删除记录"
            if not any(
                evidence_by_id[evidence_id]["role"] in qualifying_roles
                and evidence_by_id[evidence_id]["timestamp"] is not None
                and evidence_by_id[evidence_id]["timestamp"]
                > float(suppression["deleted_at"])
                for evidence_id in item["evidence_ids"]
            ):
                return "恢复删除结论必须引用删除后的新证据"
            item["suppression_id"] = int(suppression["suppression_id"])
        return item

    @staticmethod
    def _validate_entry_content(
        category: str, content: str, *, automatic: bool = False
    ) -> None:
        if category not in BASELINE_CATEGORIES:
            raise ValueError("画像类别无效")
        if not content:
            raise ValueError("画像正文不能为空")
        if len(content) > 180:
            raise ValueError("画像正文不能超过 180 字")
        if _MACHINE_ID_PATTERN.search(content):
            raise ValueError("画像正文不能包含内部编号")
        if not automatic:
            return
        if _ORDINARY_INTEREST_PATTERN.search(content):
            raise ValueError("普通兴趣不能进入用户画像")
        if _PERSONALITY_INFERENCE_PATTERN.search(content):
            raise ValueError("性格推断不能进入用户画像")
        if _MOOD_PATTERN.search(content) and category != "long_term_boundary":
            raise ValueError("近期情绪不能进入用户画像")
        if _TEMPORARY_PATTERN.search(content):
            raise ValueError("短期状态或约定不能进入用户画像")
        if category == "interaction_preference" and _ORDINARY_INTEREST_PATTERN.search(
            content
        ):
            raise ValueError("普通兴趣不能伪装为互动偏好")
        if category == "relationship" and _ORDINARY_INTEREST_PATTERN.search(content):
            raise ValueError("关系锚点缺少明确关系证据")

    async def _prune_unreferenced_evidence_locked(self, user_id: int) -> None:
        """Keep profile evidence copies needed by entries or pending work."""
        if self.db is None:
            return
        cursor = await self.db.execute(
            """
            SELECT evidence_json FROM user_baseline_entries
            WHERE user_id = ? AND retired_at IS NULL
            """,
            (user_id,),
        )
        referenced: set[str] = set()
        for row in await cursor.fetchall():
            for item in _json_list(row["evidence_json"]):
                evidence_id = _flat_text(item)
                if evidence_id:
                    referenced.add(evidence_id)
        cursor = await self.db.execute(
            """
            SELECT window_id, message_ids_json FROM user_baseline_windows
            WHERE user_id = ? AND status = 'pending'
            """,
            (user_id,),
        )
        for row in await cursor.fetchall():
            window_id = int(row["window_id"])
            for item in _json_list(row["message_ids_json"]):
                try:
                    message_id = int(item)
                except (TypeError, ValueError):
                    continue
                if message_id > 0:
                    referenced.add(f"W{window_id}:M{message_id}")
        if not referenced:
            await self.db.execute(
                "DELETE FROM user_baseline_evidence WHERE user_id = ?",
                (user_id,),
            )
            return
        placeholders = ",".join("?" for _ in referenced)
        await self.db.execute(
            f"""
            DELETE FROM user_baseline_evidence
            WHERE user_id = ? AND evidence_id NOT IN ({placeholders})
            """,
            [user_id, *sorted(referenced)],
        )

    async def _repair_evidence_mirrors(self) -> None:
        """Rebuild recoverable mirrors, then discard rows no longer needed."""
        if self.db is None:
            return
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    """
                    SELECT user_id, window_id, messages_json, created_at
                    FROM user_baseline_windows
                    ORDER BY window_id
                    """
                )
                rows = await cursor.fetchall()
                user_ids: set[int] = set()
                for row in rows:
                    user_id = int(row["user_id"])
                    user_ids.add(user_id)
                    await self._backfill_evidence_locked(
                        user_id=user_id,
                        window_id=int(row["window_id"]),
                        messages=_json_list(row["messages_json"]),
                        created_at=float(row["created_at"] or time.time()),
                    )
                for user_id in user_ids:
                    await self._prune_unreferenced_evidence_locked(user_id)
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise

    async def _apply_generation(
        self,
        *,
        user_id: int,
        persona_id: str,
        windows: list[dict[str, Any]],
        user_revision: int,
        operations: list[dict[str, Any]],
        initial_rejection_reasons: list[str] | tuple[str, ...] = (),
    ) -> BaselineApplyResult | None:
        if self.db is None:
            return None
        now = time.time()
        window_ids = [int(item["window_id"]) for item in windows]
        applied = 0
        no_change = 0
        rejection_reasons = list(initial_rejection_reasons)
        rejected = len(rejection_reasons)
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    "SELECT revision FROM user_baseline_users WHERE user_id = ?",
                    (user_id,),
                )
                row = await cursor.fetchone()
                if row is None or int(row["revision"]) != user_revision:
                    await self.db.rollback()
                    return None
                for index, operation in enumerate(operations):
                    op = operation["op"]
                    if op == "add":
                        if "suppression_id" not in operation and await self._is_suppressed_locked(
                            user_id=user_id,
                            persona_id=persona_id,
                            content=operation["content"],
                        ):
                            no_change += 1
                            continue
                        if await self._has_duplicate_entry_locked(
                            user_id=user_id,
                            persona_id=persona_id,
                            content_key=_content_key(operation["content"]),
                        ):
                            no_change += 1
                            continue
                        savepoint = f"baseline_add_{index}"
                        await self.db.execute(f"SAVEPOINT {savepoint}")
                        await self.db.execute(
                            """
                            INSERT INTO user_baseline_entries(
                                entry_id, user_id, persona_id, category, content,
                                content_key, source_type, evidence_json, locked,
                                enabled, revision, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, 'automatic', ?, 0, 1, 1, ?, ?)
                            """,
                            (
                                uuid.uuid4().hex,
                                user_id,
                                persona_id,
                                operation["category"],
                                operation["content"],
                                _content_key(operation["content"]),
                                json.dumps(
                                    operation["evidence_ids"], ensure_ascii=False
                                ),
                                now,
                                now,
                            ),
                        )
                        try:
                            await self._validate_user_budgets_locked(user_id)
                        except ValueError:
                            await self.db.execute(f"ROLLBACK TO {savepoint}")
                            await self.db.execute(f"RELEASE {savepoint}")
                            rejected += 1
                            rejection_reasons.append(
                                "新增条目超出数量或 token 预算"
                            )
                            continue
                        await self.db.execute(f"RELEASE {savepoint}")
                        if "suppression_id" in operation:
                            await self.db.execute(
                                "DELETE FROM user_baseline_suppressions "
                                "WHERE suppression_id = ? AND user_id = ?",
                                (operation["suppression_id"], user_id),
                            )
                        applied += 1
                    else:
                        cursor = await self.db.execute(
                            """
                            SELECT * FROM user_baseline_entries
                            WHERE entry_id = ? AND user_id = ? AND persona_id = ?
                              AND retired_at IS NULL
                            """,
                            (operation["entry_id"], user_id, persona_id),
                        )
                        entry = await cursor.fetchone()
                        if entry is None:
                            rejected += 1
                            rejection_reasons.append(
                                "引用条目不属于当前 persona 或已不存在"
                            )
                            continue
                        if bool(entry["locked"]):
                            rejected += 1
                            rejection_reasons.append("条目不允许自动更新")
                            continue
                        if op == "retire":
                            await self.db.execute(
                                """
                                UPDATE user_baseline_entries
                                SET enabled = 0, retired_at = ?, evidence_json = ?,
                                    revision = revision + 1, updated_at = ?
                                WHERE entry_id = ?
                                """,
                                (
                                    now,
                                    "[]",
                                    now,
                                    operation["entry_id"],
                                ),
                            )
                            applied += 1
                        else:
                            if "suppression_id" not in operation and await self._is_suppressed_locked(
                                user_id=user_id,
                                persona_id=persona_id,
                                content=operation["content"],
                            ):
                                no_change += 1
                                continue
                            if await self._has_duplicate_entry_locked(
                                user_id=user_id,
                                persona_id=persona_id,
                                content_key=_content_key(operation["content"]),
                                exclude_entry_id=operation["entry_id"],
                            ):
                                rejected += 1
                                rejection_reasons.append(
                                    "更新后的内容与其他条目重复"
                                )
                                continue
                            merged_evidence = list(
                                dict.fromkeys(
                                    [
                                        *_json_list(entry["evidence_json"]),
                                        *operation["evidence_ids"],
                                    ]
                                )
                            )
                            unchanged = (
                                str(entry["category"]) == operation["category"]
                                and str(entry["content"]) == operation["content"]
                            )
                            if unchanged:
                                if merged_evidence != _json_list(entry["evidence_json"]):
                                    await self.db.execute(
                                        """
                                        UPDATE user_baseline_entries
                                        SET source_type = 'automatic', evidence_json = ?,
                                            revision = revision + 1, updated_at = ?
                                        WHERE entry_id = ?
                                        """,
                                        (
                                            json.dumps(merged_evidence, ensure_ascii=False),
                                            now,
                                            operation["entry_id"],
                                        ),
                                    )
                                if "suppression_id" in operation:
                                    await self.db.execute(
                                        "DELETE FROM user_baseline_suppressions "
                                        "WHERE suppression_id = ? AND user_id = ?",
                                        (operation["suppression_id"], user_id),
                                    )
                                no_change += 1
                                continue
                            savepoint = f"baseline_update_{index}"
                            await self.db.execute(f"SAVEPOINT {savepoint}")
                            await self.db.execute(
                                """
                                UPDATE user_baseline_entries
                                SET category = ?, content = ?, content_key = ?,
                                    source_type = 'automatic', evidence_json = ?,
                                    revision = revision + 1, updated_at = ?
                                WHERE entry_id = ?
                                """,
                                (
                                    operation["category"],
                                    operation["content"],
                                    _content_key(operation["content"]),
                                    json.dumps(merged_evidence, ensure_ascii=False),
                                    now,
                                    operation["entry_id"],
                                ),
                            )
                            try:
                                await self._validate_user_budgets_locked(user_id)
                            except ValueError:
                                await self.db.execute(f"ROLLBACK TO {savepoint}")
                                await self.db.execute(f"RELEASE {savepoint}")
                                rejected += 1
                                rejection_reasons.append(
                                    "更新后超出数量或 token 预算"
                                )
                                continue
                            await self.db.execute(f"RELEASE {savepoint}")
                            if "suppression_id" in operation:
                                await self.db.execute(
                                    "DELETE FROM user_baseline_suppressions "
                                    "WHERE suppression_id = ? AND user_id = ?",
                                    (operation["suppression_id"], user_id),
                                )
                            applied += 1
                result = BaselineApplyResult(
                    applied=applied,
                    no_change=no_change,
                    rejected=rejected,
                    rejection_reasons=tuple(rejection_reasons),
                )
                if not applied and not no_change and rejected:
                    await self.db.rollback()
                    return result
                placeholders = ",".join("?" for _ in window_ids)
                await self.db.execute(
                    f"""
                    UPDATE user_baseline_windows
                    SET status = 'consumed', consumed_at = ?
                    WHERE window_id IN ({placeholders}) AND status = 'pending'
                    """,
                    [now, *window_ids],
                )
                await self._prune_unreferenced_evidence_locked(user_id)
                await self.db.execute(
                    """
                    UPDATE user_baseline_states
                    SET last_success_at = ?, revision = revision + 1, updated_at = ?
                    WHERE user_id = ? AND persona_id = ?
                    """,
                    (now, now, user_id, persona_id),
                )
                await self.db.execute(
                    """
                    UPDATE user_baseline_users
                    SET revision = revision + 1, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (now, user_id),
                )
                await self._trim_windows_locked(user_id, persona_id)
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        return result

    async def _has_duplicate_entry_locked(
        self,
        *,
        user_id: int,
        persona_id: str,
        content_key: str,
        exclude_entry_id: str = "",
    ) -> bool:
        if self.db is None:
            return False
        scope_sql = "" if not persona_id else "AND persona_id IN (?, '')"
        params: list[Any] = [user_id, content_key]
        if persona_id:
            params.append(persona_id)
        exclude_sql = ""
        if exclude_entry_id:
            exclude_sql = "AND entry_id != ?"
            params.append(exclude_entry_id)
        cursor = await self.db.execute(
            f"""
            SELECT 1 FROM user_baseline_entries
            WHERE user_id = ? AND content_key = ? {scope_sql} {exclude_sql}
              AND retired_at IS NULL LIMIT 1
            """,
            params,
        )
        return await cursor.fetchone() is not None

    async def _is_suppressed_locked(
        self,
        *,
        user_id: int,
        persona_id: str,
        content: str,
    ) -> bool:
        if self.db is None:
            return False
        cursor = await self.db.execute(
            """
            SELECT content_hash FROM user_baseline_suppressions
            WHERE user_id = ? AND persona_id IN (?, '')
            """,
            (user_id, persona_id),
        )
        rows = await cursor.fetchall()
        content_hash = _content_hash(content)
        return any(str(row["content_hash"]) == content_hash for row in rows)

    async def resolve_event_identity(self, event: Any) -> BaselineIdentity | None:
        platform = ""
        sender_id = ""
        sender_name = ""
        for method_name, target in (
            ("get_platform_name", "platform"),
            ("get_sender_id", "sender_id"),
            ("get_sender_name", "sender_name"),
        ):
            method = getattr(event, method_name, None)
            try:
                value = method() if callable(method) else getattr(event, target, "")
            except Exception:
                value = ""
            if target == "platform":
                platform = _flat_text(value)
            elif target == "sender_id":
                sender_id = _flat_text(value)
            else:
                sender_name = _flat_text(value)
        if not sender_id and not sender_name:
            session_id = _flat_text(getattr(event, "unified_msg_origin", ""))
            if "GroupMessage" in session_id:
                return None
            fallback = await self._unique_private_sender(session_id)
            if fallback is None:
                return None
            platform, sender_id, sender_name = fallback
        return self._identity_for_message(
            {
                "role": "user",
                "platform": platform,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "metadata": {},
            }
        )

    async def _unique_private_sender(
        self, session_id: str
    ) -> tuple[str, str, str] | None:
        if not session_id or not Path(self.conversations_db_path).exists():
            return None
        async with aiosqlite.connect(self.conversations_db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT sender_id, MAX(COALESCE(sender_name, '')) AS sender_name,
                       MAX(COALESCE(platform, 'unknown')) AS platform
                FROM messages
                WHERE session_id = ? AND role = 'user' AND sender_id != ''
                GROUP BY sender_id LIMIT 2
                """,
                (session_id,),
            )
            rows = await cursor.fetchall()
        if len(rows) != 1:
            return None
        row = rows[0]
        return str(row["platform"]), str(row["sender_id"]), str(row["sender_name"])

    async def build_injection(
        self, *, event: Any, persona_id: str | None
    ) -> BaselineInjection:
        if not self.enabled or self.db is None:
            return BaselineInjection("", (), 0, frozenset())
        identity = await self.resolve_event_identity(event)
        if identity is None:
            return BaselineInjection("", (), 0, frozenset())
        cursor = await self.db.execute(
            "SELECT user_id FROM user_baseline_users WHERE user_key = ?",
            (identity.user_key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return BaselineInjection("", (), 0, frozenset())
        entries = await self._load_entries(
            int(row["user_id"]), _flat_text(persona_id), include_disabled=False
        )
        user_id = int(row["user_id"])
        persona = _flat_text(persona_id)
        packed = self._pack_entries(entries)
        portrait = await self._usable_portrait(user_id, persona, entries)
        if portrait is not None:
            text = self._render_portrait_block(portrait["text"])
            tokens = token_upper_bound(text) if text else 0
            if text and tokens <= BASELINE_TOKEN_BUDGET:
                return BaselineInjection(
                    text=text,
                    entries=packed.entries,
                    token_count=tokens,
                    content_keys=packed.content_keys,
                )
        elif entries:
            self._schedule_portrait_rewrite(user_id, persona)
        return packed

    async def _load_entries(
        self, user_id: int, persona_id: str, *, include_disabled: bool
    ) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        enabled_clause = "" if include_disabled else "AND enabled = 1"
        cursor = await self.db.execute(
            f"""
            SELECT * FROM user_baseline_entries
            WHERE user_id = ? AND persona_id IN (?, '') AND retired_at IS NULL
              {enabled_clause}
            ORDER BY CASE WHEN persona_id = ? THEN 0 ELSE 1 END,
                     CASE WHEN source_type = 'manual' THEN 0 ELSE 1 END,
                     created_at ASC
            """,
            (user_id, persona_id, persona_id),
        )
        return [self._entry_row(row) for row in await cursor.fetchall()]

    @staticmethod
    def _entry_row(row: aiosqlite.Row) -> dict[str, Any]:
        return {
            "entry_id": str(row["entry_id"]),
            "user_id": int(row["user_id"]),
            "persona_id": str(row["persona_id"] or ""),
            "category": str(row["category"]),
            "content": str(row["content"]),
            "source_type": str(row["source_type"]),
            "evidence": _json_list(row["evidence_json"]),
            "locked": bool(row["locked"]),
            "allow_auto_update": not bool(row["locked"]),
            "enabled": bool(row["enabled"]),
            "revision": int(row["revision"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    async def _attach_evidence_texts(
        self, entries: list[dict[str, Any]], *, user_id: int
    ) -> list[dict[str, Any]]:
        """Replace bare evidence ids (W{n}:M{m}) with {id, text} objects using
        the window-independent evidence mirror."""
        if not entries or self.db is None:
            return entries
        evidence_ids: set[str] = set()
        for entry in entries:
            for evidence in entry.get("evidence") or []:
                evidence_id = _flat_text(
                    evidence.get("id") if isinstance(evidence, dict) else evidence
                )
                if evidence_id:
                    evidence_ids.add(evidence_id)
        if not evidence_ids:
            return entries
        placeholders = ",".join("?" for _ in evidence_ids)
        cursor = await self.db.execute(
            f"""
            SELECT evidence_id, message_text, message_role,
                   speaker_name, message_timestamp
            FROM user_baseline_evidence
            WHERE user_id = ? AND evidence_id IN ({placeholders})
            """,
            [user_id, *sorted(evidence_ids)],
        )
        evidence_by_id = {
            str(row["evidence_id"]): {
                "text": str(row["message_text"]),
                "role": str(row["message_role"] or "unknown"),
                "speaker_name": str(row["speaker_name"] or ""),
                "timestamp": row["message_timestamp"],
            }
            for row in await cursor.fetchall()
        }
        for entry in entries:
            decorated: list[dict[str, Any]] = []
            for evidence in entry.get("evidence") or []:
                evidence_id = _flat_text(
                    evidence.get("id") if isinstance(evidence, dict) else evidence
                )
                decorated.append(
                    {
                        "id": evidence_id,
                        "text": evidence_by_id.get(evidence_id, {}).get("text", ""),
                        "role": evidence_by_id.get(evidence_id, {}).get(
                            "role", "unknown"
                        ),
                        "speaker_name": evidence_by_id.get(evidence_id, {}).get(
                            "speaker_name", ""
                        ),
                        "timestamp": evidence_by_id.get(evidence_id, {}).get(
                            "timestamp"
                        ),
                    }
                )
            entry["evidence"] = decorated
        return entries

    @staticmethod
    def _render_entries(entries: list[dict[str, Any]]) -> str:
        if not entries:
            return ""
        lines = [
            "[用户画像｜长期有效]",
            "以下信息长期有效；若与用户本轮原话冲突，以本轮原话为准。",
        ]
        for entry in entries:
            label = _CATEGORY_LABELS.get(entry["category"], entry["category"])
            lines.append(f"- [{label}] {entry['content']}")
        lines.append("[/用户画像]")
        return "\n".join(lines)

    @classmethod
    def _pack_entries(cls, entries: list[dict[str, Any]]) -> BaselineInjection:
        selected: list[dict[str, Any]] = []
        for entry in entries:
            if len(selected) >= BASELINE_MAX_ENTRIES:
                break
            candidate = [*selected, entry]
            text = cls._render_entries(candidate)
            if token_upper_bound(text) > BASELINE_TOKEN_BUDGET:
                continue
            selected = candidate
        text = cls._render_entries(selected)
        return BaselineInjection(
            text=text,
            entries=tuple(selected),
            token_count=token_upper_bound(text) if text else 0,
            content_keys=frozenset(_content_key(item["content"]) for item in selected),
        )

    # ---- 小作文：由当前生效条目派生的一段散文，供注入使用 ----

    @staticmethod
    def _portrait_fingerprint(entries: list[dict[str, Any]]) -> str:
        payload = [
            [
                str(item.get("entry_id") or ""),
                str(item.get("category") or ""),
                str(item.get("content") or ""),
                bool(item.get("locked")),
            ]
            for item in entries
        ]
        payload.sort()
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        return digest.hexdigest()[:32]

    async def _load_portrait(
        self, user_id: int, persona_id: str
    ) -> dict[str, Any] | None:
        if self.db is None:
            return None
        cursor = await self.db.execute(
            """
            SELECT * FROM user_baseline_portraits
            WHERE user_id = ? AND persona_id = ?
            """,
            (user_id, persona_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "user_id": int(row["user_id"]),
            "persona_id": str(row["persona_id"] or ""),
            "text": str(row["text"] or ""),
            "entries_hash": str(row["entries_hash"] or ""),
            "locked": bool(row["locked"]),
            "source": str(row["source"] or "auto"),
            "revision": int(row["revision"]),
            "token_count": int(row["token_count"]),
            "updated_at": float(row["updated_at"]),
        }

    @staticmethod
    def _render_portrait_block(text: str) -> str:
        body = text.strip()
        if not body:
            return ""
        return "\n".join(
            [
                "[用户画像｜长期有效]",
                "以下信息长期有效；若与用户本轮原话冲突，以本轮原话为准。",
                body,
                "[/用户画像]",
            ]
        )

    @staticmethod
    def _clean_portrait_text(value: Any) -> str:
        text = _flat_text(value)
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text[:4].casefold() == "text":
                text = text[4:]
        text = re.sub(r"^(?:小作文|用户画像)\s*[:：]\s*", "", text.strip())
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _validate_portrait_text(text: str) -> str:
        if not text:
            raise BaselineDeterministicFailure("小作文为空")
        if len(text) > PORTRAIT_MAX_CHARS:
            raise BaselineDeterministicFailure(
                f"小作文超过 {PORTRAIT_MAX_CHARS} 字上限"
            )
        if re.search(r"(?m)^\s*(?:[-*•]|\d+[.、)）])\s*", text):
            raise BaselineDeterministicFailure("小作文必须是散文而不是列表")
        if "[" in text or "]" in text:
            raise BaselineDeterministicFailure("小作文不能包含标记符号")
        if token_upper_bound(text) > PORTRAIT_TOKEN_BUDGET:
            raise BaselineDeterministicFailure(
                f"小作文超过 {PORTRAIT_TOKEN_BUDGET} token 预算"
            )
        return text

    async def _call_portrait_llm(
        self, *, entries: list[dict[str, Any]], previous_text: str
    ) -> str:
        provider = self._resolve_provider()
        if provider is None:
            raise RuntimeError("LLM Provider 不可用")
        payload = [
            {
                "category": _CATEGORY_LABELS.get(item["category"], item["category"]),
                "content": item["content"],
                "locked": bool(item["locked"]),
            }
            for item in entries
        ]
        prompt = (
            "下面是一份用户画像的条目清单，请把它写成一段常驻使用的散文。\n"
            "要求：\n"
            f"1. 中文，{PORTRAIT_MAX_CHARS} 字以内，一到两段，只输出正文。\n"
            "2. 不要列表、编号、标题、Markdown、括号注释，也不要用方括号。\n"
            "3. 顺序：称呼与身份 → 与 Bot 的关系 → 互动偏好 → 长期边界与硬约束。\n"
            "4. 只覆盖给定条目，不新增事实；不推测性格、情绪、好感度、兴趣或当前任务。\n"
            "5. 合并同义条目，读起来像一段人话，不要逐条翻译清单。\n"
            "6. locked=true 的条目是用户亲手写定的措辞，必须完整吸收进正文，"
            "意思不得弱化、反转或省略；它们不会另外重复列出。\n"
            "7. 若给了上一版，保持前后一致，只体现条目的实际变化。\n\n"
            f"条目：{json.dumps(payload, ensure_ascii=False)}\n\n"
            "上一版："
            + (previous_text.strip() or "（无）")
        )
        response = await provider.text_chat(
            prompt=prompt,
            system_prompt=(
                "你只根据给定的画像条目写一段中性的常驻介绍，"
                "不新增事实，不推断人格，不改写用户锁定的原话。"
            ),
        )
        text = getattr(response, "completion_text", "")
        if not isinstance(text, str) or not text.strip():
            raise BaselineDeterministicFailure("LLM 返回空响应")
        return self._validate_portrait_text(self._clean_portrait_text(text))

    async def _trim_portrait_history_locked(
        self, user_id: int, persona_id: str
    ) -> None:
        if self.db is None:
            return
        await self.db.execute(
            """
            DELETE FROM user_baseline_portrait_revisions
            WHERE user_id = ? AND persona_id = ? AND portrait_revision_id NOT IN (
                SELECT portrait_revision_id FROM user_baseline_portrait_revisions
                WHERE user_id = ? AND persona_id = ?
                ORDER BY revision DESC, portrait_revision_id DESC
                LIMIT ?
            )
            """,
            (
                user_id,
                persona_id,
                user_id,
                persona_id,
                PORTRAIT_HISTORY_LIMIT,
            ),
        )

    async def _store_portrait_locked(
        self,
        *,
        user_id: int,
        persona_id: str,
        text: str,
        entries_hash: str,
        locked: bool,
        source: str,
    ) -> dict[str, Any]:
        if self.db is None:
            raise RuntimeError("用户画像尚未初始化")
        current = await self._load_portrait(user_id, persona_id)
        revision = (current["revision"] + 1) if current else 1
        now = time.time()
        await self.db.execute(
            """
            INSERT INTO user_baseline_portraits(
                user_id, persona_id, text, entries_hash, locked, source,
                revision, token_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, persona_id) DO UPDATE SET
                text = excluded.text,
                entries_hash = excluded.entries_hash,
                locked = excluded.locked,
                source = excluded.source,
                revision = excluded.revision,
                token_count = excluded.token_count,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                persona_id,
                text,
                entries_hash,
                int(bool(locked)),
                source,
                revision,
                token_upper_bound(text),
                now,
            ),
        )
        await self.db.execute(
            """
            INSERT INTO user_baseline_portrait_revisions(
                user_id, persona_id, revision, text, entries_hash, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, persona_id, revision, text, entries_hash, source, now),
        )
        await self._trim_portrait_history_locked(user_id, persona_id)
        return {
            "user_id": user_id,
            "persona_id": persona_id,
            "text": text,
            "entries_hash": entries_hash,
            "locked": bool(locked),
            "source": source,
            "revision": revision,
            "token_count": token_upper_bound(text),
            "updated_at": now,
        }

    async def rewrite_portrait(
        self,
        *,
        user_id: int,
        persona_id: str,
        force: bool = False,
        source: str = "auto",
    ) -> dict[str, Any] | None:
        """Rewrite the portrait for one persona from its current entries."""
        if self.db is None or not self.portrait_enabled:
            return None
        lock = self._portrait_locks.setdefault((user_id, persona_id), asyncio.Lock())
        if lock.locked():
            return None
        async with lock:
            entries = await self._load_entries(
                user_id, persona_id, include_disabled=False
            )
            if not entries:
                return None
            current = await self._load_portrait(user_id, persona_id)
            if current and current["locked"] and not force:
                return current
            fingerprint = self._portrait_fingerprint(entries)
            if current and current["entries_hash"] == fingerprint and not force:
                return current
            text = await self._call_portrait_llm(
                entries=entries,
                previous_text=current["text"] if current else "",
            )
            async with self._write_lock:
                portrait = await self._store_portrait_locked(
                    user_id=user_id,
                    persona_id=persona_id,
                    text=text,
                    entries_hash=fingerprint,
                    locked=False,
                    source=source,
                )
                await self.db.commit()
            return portrait

    def _schedule_portrait_rewrite(self, user_id: int, persona_id: str) -> None:
        if self._closing or not self.portrait_enabled:
            return
        lock = self._portrait_locks.get((user_id, persona_id))
        if lock is not None and lock.locked():
            return
        task = asyncio.create_task(
            self._safe_rewrite_portrait(user_id, persona_id)
        )
        self._portrait_tasks.add(task)
        task.add_done_callback(self._portrait_tasks.discard)

    async def _safe_rewrite_portrait(self, user_id: int, persona_id: str) -> None:
        try:
            await self.rewrite_portrait(user_id=user_id, persona_id=persona_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[用户画像] 小作文重写失败，条目已生效：{exc}")

    async def _portrait_personas(self, user_id: int, persona_id: str) -> list[str]:
        if persona_id:
            return [persona_id]
        if self.db is None:
            return [GLOBAL_PERSONA]
        cursor = await self.db.execute(
            "SELECT persona_id FROM user_baseline_states WHERE user_id = ?",
            (user_id,),
        )
        personas = [str(row["persona_id"] or "") for row in await cursor.fetchall()]
        return personas or [GLOBAL_PERSONA]

    async def _refresh_portraits_for(self, user_id: int, persona_id: str) -> None:
        for persona in await self._portrait_personas(user_id, persona_id):
            self._schedule_portrait_rewrite(user_id, persona)

    async def _usable_portrait(
        self, user_id: int, persona_id: str, entries: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        if not self.portrait_enabled:
            return None
        portrait = await self._load_portrait(user_id, persona_id)
        if portrait is None or not portrait["text"]:
            return None
        if portrait["locked"]:
            return portrait
        if portrait["entries_hash"] != self._portrait_fingerprint(entries):
            return None
        return portrait

    async def update_portrait(
        self, *, user_id: int, persona_id: str, text: Any, locked: bool = True
    ) -> dict[str, Any]:
        """Store a user-written portrait; locked portraits survive rewrites."""
        if self.db is None:
            raise RuntimeError("用户画像尚未初始化")
        cleaned = self._clean_portrait_text(text)
        if not cleaned:
            raise ValueError("小作文不能为空")
        if len(cleaned) > PORTRAIT_MAX_CHARS:
            raise ValueError(f"小作文不能超过 {PORTRAIT_MAX_CHARS} 字")
        if "[" in cleaned or "]" in cleaned:
            raise ValueError("小作文不能包含方括号")
        if token_upper_bound(cleaned) > PORTRAIT_TOKEN_BUDGET:
            raise ValueError(f"小作文不能超过 {PORTRAIT_TOKEN_BUDGET} token")
        entries = await self._load_entries(
            user_id, persona_id, include_disabled=False
        )
        fingerprint = self._portrait_fingerprint(entries)
        async with self._write_lock:
            await self._store_portrait_locked(
                user_id=user_id,
                persona_id=persona_id,
                text=cleaned,
                entries_hash=fingerprint,
                locked=locked,
                source="manual",
            )
            await self.db.commit()
        detail = await self.get_user_detail(user_id)
        if detail is None:
            raise RuntimeError("用户画像不存在")
        return detail

    async def regenerate_portrait(
        self, *, user_id: int, persona_id: str
    ) -> dict[str, Any]:
        """Force a rewrite even when the current portrait is locked."""
        if self.db is None:
            raise RuntimeError("用户画像尚未初始化")
        portrait = await self.rewrite_portrait(
            user_id=user_id, persona_id=persona_id, force=True, source="manual"
        )
        if portrait is None:
            raise ValueError("没有可用于生成小作文的条目")
        detail = await self.get_user_detail(user_id)
        if detail is None:
            raise RuntimeError("用户画像不存在")
        return detail

    async def list_portrait_revisions(
        self, user_id: int, persona_id: str, limit: int = PORTRAIT_HISTORY_LIMIT
    ) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        cursor = await self.db.execute(
            """
            SELECT revision, text, entries_hash, source, created_at
            FROM user_baseline_portrait_revisions
            WHERE user_id = ? AND persona_id = ?
            ORDER BY revision DESC, portrait_revision_id DESC
            LIMIT ?
            """,
            (user_id, persona_id, max(1, int(limit))),
        )
        return [
            {
                "revision": int(row["revision"]),
                "text": str(row["text"] or ""),
                "entries_hash": str(row["entries_hash"] or ""),
                "source": str(row["source"] or "auto"),
                "created_at": float(row["created_at"]),
            }
            for row in await cursor.fetchall()
        ]

    async def rollback_portrait(
        self, *, user_id: int, persona_id: str, revision: int
    ) -> dict[str, Any]:
        if self.db is None:
            raise RuntimeError("用户画像尚未初始化")
        cursor = await self.db.execute(
            """
            SELECT text, entries_hash FROM user_baseline_portrait_revisions
            WHERE user_id = ? AND persona_id = ? AND revision = ?
            ORDER BY portrait_revision_id DESC LIMIT 1
            """,
            (user_id, persona_id, int(revision)),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError("小作文历史版本不存在")
        async with self._write_lock:
            await self._store_portrait_locked(
                user_id=user_id,
                persona_id=persona_id,
                text=str(row["text"] or ""),
                entries_hash=str(row["entries_hash"] or ""),
                locked=True,
                source="rollback",
            )
            await self.db.commit()
        detail = await self.get_user_detail(user_id)
        if detail is None:
            raise RuntimeError("用户画像不存在")
        return detail

    @classmethod
    def _validate_budget(cls, entries: list[dict[str, Any]]) -> int:
        enabled = [item for item in entries if item.get("enabled", True)]
        if len(enabled) > BASELINE_MAX_ENTRIES:
            raise ValueError(f"同一常驻块最多 {BASELINE_MAX_ENTRIES} 条")
        tokens = token_upper_bound(cls._render_entries(enabled)) if enabled else 0
        if tokens > BASELINE_TOKEN_BUDGET:
            raise ValueError(f"常驻块超过 {BASELINE_TOKEN_BUDGET} token 硬预算")
        return tokens

    async def list_users(
        self, *, keyword: str = "", page: int = 1, page_size: int = 20
    ) -> dict[str, Any]:
        if self.db is None:
            return {
                "items": [],
                "page": page,
                "page_size": page_size,
                "total": 0,
                "batch_windows": BASELINE_BATCH_WINDOWS,
                "token_budget": BASELINE_TOKEN_BUDGET,
                "generation_warning": None,
            }
        cursor = await self.db.execute(
            """
            SELECT value, updated_at FROM user_baseline_meta
            WHERE key = 'generation-warning-v1'
            """
        )
        warning_row = await cursor.fetchone()
        generation_warning = None
        if warning_row:
            warning_value = _json_dict(warning_row["value"])
            reasons = [
                _flat_text(item)
                for item in _json_list(warning_value.get("reasons"))[:2]
            ]
            if not warning_value.get("dismissed"):
                generation_warning = {
                    "occurred_at": float(warning_row["updated_at"]),
                    "reasons": reasons,
                }
        where = ""
        params: list[Any] = []
        if keyword:
            where = "WHERE display_name LIKE ? OR platform LIKE ? OR canonical_identity LIKE ?"
            pattern = f"%{keyword}%"
            params.extend([pattern, pattern, pattern])
        cursor = await self.db.execute(
            f"SELECT COUNT(*) AS count FROM user_baseline_users {where}", params
        )
        count_row = await cursor.fetchone()
        total = int(count_row["count"] if count_row else 0)
        offset = (page - 1) * page_size
        cursor = await self.db.execute(
            f"""
            SELECT u.*,
                   COUNT(DISTINCT CASE WHEN s.persona_id != '' THEN s.persona_id END) AS persona_count,
                   COUNT(DISTINCT CASE WHEN e.enabled = 1 AND e.retired_at IS NULL THEN e.entry_id END) AS entry_count,
                   COALESCE(MAX(p.pending_count), 0) AS progress_count,
                   MAX(s.last_success_at) AS last_success_at,
                   MAX(s.last_attempt_at) AS last_attempt_at
            FROM user_baseline_users u
            LEFT JOIN user_baseline_states s ON s.user_id = u.user_id
            LEFT JOIN user_baseline_entries e ON e.user_id = u.user_id
            LEFT JOIN (
                SELECT user_id, persona_id, COUNT(*) AS pending_count
                FROM user_baseline_windows WHERE status = 'pending'
                GROUP BY user_id, persona_id
            ) p ON p.user_id = s.user_id AND p.persona_id = s.persona_id
            {where}
            GROUP BY u.user_id
            ORDER BY u.updated_at DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, offset],
        )
        now = time.time()
        items = []
        for row in await cursor.fetchall():
            last_attempt = float(row["last_attempt_at"] or 0.0)
            items.append(
                {
                    "user_id": int(row["user_id"]),
                    "display_name": str(row["display_name"]),
                    "platform": str(row["platform"]),
                    "canonical_identity": str(row["canonical_identity"]),
                    "persona_count": int(row["persona_count"] or 0),
                    "entry_count": int(row["entry_count"] or 0),
                    "progress_count": min(
                        int(row["progress_count"] or 0), BASELINE_BATCH_WINDOWS
                    ),
                    "last_success_at": row["last_success_at"],
                    "cooldown_remaining": max(
                        0, int(BASELINE_COOLDOWN_SECONDS - (now - last_attempt))
                    )
                    if last_attempt
                    else 0,
                    "revision": int(row["revision"]),
                }
            )
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "batch_windows": BASELINE_BATCH_WINDOWS,
            "token_budget": BASELINE_TOKEN_BUDGET,
            "generation_warning": generation_warning,
        }

    async def dismiss_generation_warning(self) -> bool:
        """Hide the recorded generation warning until the next failure lands."""
        if self.db is None:
            return False
        cursor = await self.db.execute(
            """
            SELECT value FROM user_baseline_meta
            WHERE key = 'generation-warning-v1'
            """
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        value = _json_dict(row["value"])
        if value.get("dismissed"):
            return True
        value["dismissed"] = 1
        await self.db.execute(
            """
            UPDATE user_baseline_meta SET value = ?
            WHERE key = 'generation-warning-v1'
            """,
            (json.dumps(value, ensure_ascii=False),),
        )
        await self.db.commit()
        return True

    async def get_user_detail(self, user_id: int) -> dict[str, Any] | None:
        if self.db is None:
            return None
        cursor = await self.db.execute(
            "SELECT * FROM user_baseline_users WHERE user_id = ?", (user_id,)
        )
        user = await cursor.fetchone()
        if user is None:
            return None
        cursor = await self.db.execute(
            "SELECT * FROM user_baseline_entries WHERE user_id = ? AND retired_at IS NULL ORDER BY persona_id, category, created_at",
            (user_id,),
        )
        entries = [self._entry_row(row) for row in await cursor.fetchall()]
        cursor = await self.db.execute(
            """
            SELECT s.*,
                   SUM(CASE WHEN w.status = 'pending' THEN 1 ELSE 0 END) AS pending_count
            FROM user_baseline_states s
            LEFT JOIN user_baseline_windows w
              ON w.user_id = s.user_id AND w.persona_id = s.persona_id
            WHERE s.user_id = ? GROUP BY s.user_id, s.persona_id
            ORDER BY s.persona_id
            """,
            (user_id,),
        )
        now = time.time()
        states = []
        for row in await cursor.fetchall():
            last_attempt = float(row["last_attempt_at"] or 0.0)
            states.append(
                {
                    "persona_id": str(row["persona_id"] or ""),
                    "progress_count": min(
                        int(row["pending_count"] or 0), BASELINE_BATCH_WINDOWS
                    ),
                    "pending_count": int(row["pending_count"] or 0),
                    "last_attempt_at": row["last_attempt_at"],
                    "last_success_at": row["last_success_at"],
                    "cooldown_remaining": max(
                        0, int(BASELINE_COOLDOWN_SECONDS - (now - last_attempt))
                    )
                    if last_attempt
                    else 0,
                }
            )
        personas = sorted(
            {str(item["persona_id"]) for item in entries if item["persona_id"]}
            | {str(item["persona_id"]) for item in states if item["persona_id"]}
        )
        token_usage = {
            persona: self._pack_entries(
                [
                    item
                    for item in entries
                    if item["enabled"] and item["persona_id"] in {"", persona}
                ]
            ).token_count
            for persona in personas or [""]
        }
        portraits: list[dict[str, Any]] = []
        if self.portrait_enabled:
            cursor = await self.db.execute(
                """
                SELECT * FROM user_baseline_portraits
                WHERE user_id = ? ORDER BY persona_id
                """,
                (user_id,),
            )
            for row in await cursor.fetchall():
                persona = str(row["persona_id"] or "")
                visible = [
                    item
                    for item in entries
                    if item["enabled"] and item["persona_id"] in {"", persona}
                ]
                portraits.append(
                    {
                        "persona_id": persona,
                        "text": str(row["text"] or ""),
                        "locked": bool(row["locked"]),
                        "source": str(row["source"] or "auto"),
                        "revision": int(row["revision"]),
                        "token_count": int(row["token_count"]),
                        "updated_at": float(row["updated_at"]),
                        "stale": str(row["entries_hash"] or "")
                        != self._portrait_fingerprint(visible),
                    }
                )
        entries = await self._attach_evidence_texts(entries, user_id=user_id)
        cursor = await self.db.execute(
            """
            SELECT persona_id, category, content, note, deleted_at
            FROM user_baseline_suppressions
            WHERE user_id = ? AND content != ''
            ORDER BY deleted_at DESC, suppression_id DESC
            LIMIT ?
            """,
            (user_id, BASELINE_SUPPRESSION_LIMIT),
        )
        graveyard = [
            {
                "persona_id": str(row["persona_id"] or ""),
                "category": str(row["category"]),
                "content": str(row["content"]),
                "note": str(row["note"] or ""),
                "deleted_at": float(row["deleted_at"]),
            }
            for row in await cursor.fetchall()
        ]
        return {
            "user_id": int(user["user_id"]),
            "display_name": str(user["display_name"]),
            "platform": str(user["platform"]),
            "canonical_identity": str(user["canonical_identity"]),
            "revision": int(user["revision"]),
            "enabled": self.enabled,
            "entries": entries,
            "states": states,
            "personas": personas,
            "token_usage": token_usage,
            "token_budget": BASELINE_TOKEN_BUDGET,
            "batch_windows": BASELINE_BATCH_WINDOWS,
            "entry_limit": BASELINE_MAX_ENTRIES,
            "portraits": portraits,
            "graveyard": graveyard,
            "portrait_enabled": self.portrait_enabled,
            "portrait_token_budget": PORTRAIT_TOKEN_BUDGET,
            "portrait_max_chars": PORTRAIT_MAX_CHARS,
        }

    async def upsert_manual_entry(
        self, *, user_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if self.db is None:
            raise RuntimeError("用户画像尚未初始化")
        entry_id = _flat_text(payload.get("entry_id"))
        expected_revision = payload.get("revision")
        now = time.time()
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                existing = None
                if entry_id:
                    cursor = await self.db.execute(
                        "SELECT * FROM user_baseline_entries WHERE entry_id = ? AND user_id = ? AND retired_at IS NULL",
                        (entry_id, user_id),
                    )
                    existing = await cursor.fetchone()
                    if existing is None:
                        raise ValueError("用户画像条目不存在")
                    if expected_revision is None or int(expected_revision) != int(
                        existing["revision"]
                    ):
                        raise ValueError("条目已被其他操作修改，请刷新后重试")
                if existing is not None and set(payload).issubset(
                    {
                        "entry_id",
                        "revision",
                        "locked",
                        "allow_auto_update",
                        "enabled",
                    }
                ):
                    if "allow_auto_update" in payload:
                        locked = int(not bool(payload["allow_auto_update"]))
                    else:
                        locked = int(bool(payload.get("locked", existing["locked"])))
                    if not str(existing["persona_id"] or ""):
                        locked = 1
                    enabled = int(bool(payload.get("enabled", existing["enabled"])))
                    await self.db.execute(
                        "UPDATE user_baseline_entries SET locked = ?, enabled = ?, revision = revision + 1, updated_at = ? WHERE entry_id = ?",
                        (locked, enabled, now, entry_id),
                    )
                else:
                    category = _flat_text(
                        payload.get("category")
                        if "category" in payload
                        else (existing["category"] if existing else "")
                    )
                    content = _flat_text(
                        payload.get("content")
                        if "content" in payload
                        else (existing["content"] if existing else "")
                    )
                    persona_id = _flat_text(
                        payload.get("persona_id")
                        if "persona_id" in payload
                        else (existing["persona_id"] if existing else "")
                    )
                    enabled = int(
                        bool(
                            payload.get(
                                "enabled", existing["enabled"] if existing else True
                            )
                        )
                    )
                    self._validate_entry_content(category, content, automatic=False)
                    if await self._has_duplicate_entry_locked(
                        user_id=user_id,
                        persona_id=persona_id,
                        content_key=_content_key(content),
                        exclude_entry_id=entry_id,
                    ):
                        raise ValueError("用户画像中已存在相同内容")
                    substantive = existing is not None and any(
                        (
                            str(existing["persona_id"] or "") != persona_id,
                            str(existing["category"]) != category,
                            str(existing["content"]) != content,
                        )
                    )
                    if "allow_auto_update" in payload:
                        locked = int(not bool(payload["allow_auto_update"]))
                    elif "locked" in payload:
                        locked = int(bool(payload["locked"]))
                    else:
                        locked = (
                            1
                            if existing is None or substantive
                            else int(existing["locked"])
                        )
                    if not persona_id:
                        locked = 1
                    if existing is None:
                        entry_id = uuid.uuid4().hex
                        await self.db.execute(
                            """
                            INSERT INTO user_baseline_entries(
                                entry_id, user_id, persona_id, category, content,
                                content_key, source_type, evidence_json, locked,
                                enabled, revision, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, 'manual', '[]', ?, ?, 1, ?, ?)
                            """,
                            (
                                entry_id,
                                user_id,
                                persona_id,
                                category,
                                content,
                                _content_key(content),
                                locked,
                                enabled,
                                now,
                                now,
                            ),
                        )
                    else:
                        source_type = "manual" if substantive else str(existing["source_type"])
                        evidence_json = "[]" if substantive else str(existing["evidence_json"])
                        await self.db.execute(
                            """
                            UPDATE user_baseline_entries
                            SET persona_id = ?, category = ?, content = ?, content_key = ?,
                                source_type = ?, evidence_json = ?, locked = ?, enabled = ?,
                                revision = revision + 1, updated_at = ?
                            WHERE entry_id = ?
                            """,
                            (
                                persona_id,
                                category,
                                content,
                                _content_key(content),
                                source_type,
                                evidence_json,
                                locked,
                                enabled,
                                now,
                                entry_id,
                            ),
                        )
                await self._validate_user_budgets_locked(user_id)
                await self.db.execute(
                    "UPDATE user_baseline_users SET revision = revision + 1, updated_at = ? WHERE user_id = ?",
                    (now, user_id),
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        detail = await self.get_user_detail(user_id)
        if detail is None:
            raise RuntimeError("用户画像不存在")
        return detail

    async def _validate_user_budgets_locked(self, user_id: int) -> None:
        if self.db is None:
            return
        cursor = await self.db.execute(
            "SELECT * FROM user_baseline_entries WHERE user_id = ? AND enabled = 1 AND retired_at IS NULL",
            (user_id,),
        )
        entries = [self._entry_row(row) for row in await cursor.fetchall()]
        personas = {item["persona_id"] for item in entries if item["persona_id"]}
        for persona in personas or {""}:
            applicable = [
                item for item in entries if item["persona_id"] in {"", persona}
            ]
            self._validate_budget(applicable)

    async def delete_manual_entry(
        self,
        *,
        user_id: int,
        entry_id: str,
        revision: int,
        note: str = "",
    ) -> dict[str, Any]:
        if self.db is None:
            raise RuntimeError("用户画像尚未初始化")
        now = time.time()
        note_text = " ".join(_flat_text(note).split())[:BASELINE_SUPPRESSION_NOTE_MAX]
        async with self._write_lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.db.execute(
                    "SELECT * FROM user_baseline_entries WHERE entry_id = ? AND user_id = ? AND retired_at IS NULL",
                    (entry_id, user_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ValueError("用户画像条目不存在")
                if int(row["revision"]) != int(revision):
                    raise ValueError("条目已被其他操作修改，请刷新后重试")
                await self.db.execute(
                    """
                    INSERT INTO user_baseline_suppressions(
                        user_id, persona_id, category, content, content_hash,
                        note, deleted_at, evidence_cutoff
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, persona_id, category, content_hash) DO UPDATE SET
                        content = excluded.content,
                        note = excluded.note,
                        deleted_at = excluded.deleted_at,
                        evidence_cutoff = excluded.evidence_cutoff
                    """,
                    (
                        user_id,
                        str(row["persona_id"] or ""),
                        str(row["category"]),
                        str(row["content"]),
                        _content_hash(row["content"]),
                        note_text,
                        now,
                        now,
                    ),
                )
                await self._trim_suppressions_locked(
                    user_id, str(row["persona_id"] or "")
                )
                await self.db.execute(
                    "DELETE FROM user_baseline_entries WHERE entry_id = ?", (entry_id,)
                )
                await self._prune_unreferenced_evidence_locked(user_id)
                await self.db.execute(
                    "UPDATE user_baseline_users SET revision = revision + 1, updated_at = ? WHERE user_id = ?",
                    (now, user_id),
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        detail = await self.get_user_detail(user_id)
        if detail is None:
            raise RuntimeError("用户画像不存在")
        return detail

    async def _trim_suppressions_locked(
        self, user_id: int, persona_id: str
    ) -> None:
        """Keep the graveyard bounded; the oldest deletions fall out first."""
        if self.db is None:
            return
        await self.db.execute(
            """
            DELETE FROM user_baseline_suppressions
            WHERE user_id = ? AND persona_id = ? AND suppression_id NOT IN (
                SELECT suppression_id FROM user_baseline_suppressions
                WHERE user_id = ? AND persona_id = ?
                ORDER BY deleted_at DESC, suppression_id DESC
                LIMIT ?
            )
            """,
            (
                user_id,
                persona_id,
                user_id,
                persona_id,
                BASELINE_SUPPRESSION_KEEP,
            ),
        )

    async def delete_user(self, *, user_id: int, revision: int) -> None:
        if self.db is None:
            raise RuntimeError("用户画像尚未初始化")
        async with self._write_lock:
            cursor = await self.db.execute(
                "DELETE FROM user_baseline_users WHERE user_id = ? AND revision = ?",
                (user_id, revision),
            )
            if cursor.rowcount != 1:
                await self.db.rollback()
                raise ValueError("用户画像已被其他操作修改，请刷新后重试")
            await self.db.commit()

    async def bootstrap_existing_data(self) -> int:
        """Seed first-hand windows once; never call the LLM during startup."""
        if self.db is None or not self.enabled:
            return 0
        cursor = await self.db.execute(
            "SELECT value FROM user_baseline_meta WHERE key = 'bootstrap-v1'"
        )
        if await cursor.fetchone() is not None:
            return 0
        seeded = 0
        seen_message_ids: set[tuple[str, int]] = set()
        try:
            cursor = await self.db.execute(
                """
                SELECT s.source_json, d.metadata
                FROM memory_sources s JOIN documents d ON d.id = s.memory_id
                ORDER BY s.created_at ASC
                """
            )
            for row in await cursor.fetchall():
                source = _json_list(row["source_json"])
                metadata = _json_dict(row["metadata"])
                session_id = _flat_text(
                    metadata.get("source_session_id") or metadata.get("session_id")
                )
                persona_id = _flat_text(metadata.get("persona_id"))
                if not source or not session_id or not persona_id:
                    continue
                seeded += await self.register_summary_window(
                    session_id=session_id,
                    history_messages=source,
                    persona_id=persona_id,
                    source_type="bootstrap_source",
                    schedule_generation=False,
                )
                for message in source:
                    message_id = int(message.get("id") or 0)
                    if message_id:
                        seen_message_ids.add((session_id, message_id))
            seeded += await self._bootstrap_conversation_windows(seen_message_ids)
        except Exception:
            logger.warning(
                "[用户画像] 旧原文预热失败；不会改用二手记忆。", exc_info=True
            )
        now = time.time()
        await self.db.execute(
            "INSERT OR REPLACE INTO user_baseline_meta(key, value, updated_at) VALUES ('bootstrap-v1', ?, ?)",
            (json.dumps({"seeded": seeded}), now),
        )
        await self.db.commit()
        if seeded:
            logger.info(
                f"[用户画像] 已从一手原文预热 {seeded} 个窗口；等待下次成功总结再生成。"
            )
        return seeded

    async def _bootstrap_conversation_windows(
        self, seen_message_ids: set[tuple[str, int]]
    ) -> int:
        path = Path(self.conversations_db_path)
        if not path.exists() or self.db is None:
            return 0
        persona_by_session: dict[str, str] = {}
        cursor = await self.db.execute(
            """
            SELECT json_extract(metadata, '$.source_session_id') AS source_session_id,
                   json_extract(metadata, '$.persona_id') AS persona_id,
                   MAX(id) AS latest_id
            FROM documents
            WHERE json_extract(metadata, '$.source_session_id') IS NOT NULL
            GROUP BY source_session_id
            """
        )
        for row in await cursor.fetchall():
            session_id = _flat_text(row["source_session_id"])
            if session_id:
                persona_id = _flat_text(row["persona_id"])
                if persona_id:
                    persona_by_session[session_id] = persona_id
        if not persona_by_session:
            return 0
        trigger_rounds = max(
            1,
            int(
                self.config_manager.get("reflection_engine.summary_trigger_rounds", 10)
            ),
        )
        window_size = trigger_rounds * 2
        seeded = 0
        async with aiosqlite.connect(path) as conversations:
            conversations.row_factory = aiosqlite.Row
            cursor = await conversations.execute(
                "SELECT session_id, metadata FROM sessions ORDER BY created_at ASC"
            )
            for session in await cursor.fetchall():
                session_id = str(session["session_id"])
                if session_id not in persona_by_session:
                    continue
                metadata = _json_dict(session["metadata"])
                summarized_count = int(metadata.get("last_summarized_index") or 0)
                if summarized_count < 2:
                    continue
                cursor = await conversations.execute(
                    "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC LIMIT ?",
                    (session_id, summarized_count),
                )
                segments: list[list[dict[str, Any]]] = []
                current_segment: list[dict[str, Any]] = []
                for row in await cursor.fetchall():
                    message_id = int(row["id"])
                    if (session_id, message_id) in seen_message_ids:
                        if current_segment:
                            segments.append(current_segment)
                            current_segment = []
                        continue
                    current_segment.append(dict(row))
                if current_segment:
                    segments.append(current_segment)
                for segment in segments:
                    for offset in range(0, len(segment), window_size):
                        chunk = segment[offset : offset + window_size]
                        if len(chunk) < 2:
                            continue
                        seeded += await self.register_summary_window(
                            session_id=session_id,
                            history_messages=chunk,
                            persona_id=persona_by_session[session_id],
                            source_type="bootstrap_conversation",
                            schedule_generation=False,
                        )
        return seeded


__all__ = [
    "BASELINE_BATCH_WINDOWS",
    "BASELINE_CATEGORIES",
    "BASELINE_COOLDOWN_SECONDS",
    "BASELINE_MAX_ENTRIES",
    "BASELINE_TOKEN_BUDGET",
    "BaselineInjection",
    "UserBaselineManager",
]
