"""提示词持久化存储 — 基于 PostgreSQL 的简单 key-value store。

每个模块的每个提示词标识为一个 key（如 "req_agent.SYSTEM_PROMPT"），
value 为提示词文本。default_value 保存初始默认值，支持一键恢复。

使用 SQLAlchemy 同步 engine（psycopg2 驱动），保持同步接口，
供 prompt_manager 在 Agent 初始化和 API 路由中同步调用。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db.models import PromptOverride
from app.utils.logger import logger


# 同步 engine（延迟初始化，避免循环导入）
_sync_engine = None
_SyncSession = None


def _get_sync_session() -> Session:
    """获取同步 Session（延迟初始化）。"""
    global _sync_engine, _SyncSession
    if _sync_engine is None:
        _sync_engine = create_engine(
            settings.pg_sync_url,
            echo=False,
            pool_size=5,
            pool_timeout=10,
        )
        _SyncSession = sessionmaker(_sync_engine, expire_on_commit=False)
        logger.info("[PromptStore] 同步 PG engine 已初始化")
    return _SyncSession()


def _ensure_table() -> None:
    """确保 prompt_overrides 表存在（首次调用时建表）。"""
    global _sync_engine
    if _sync_engine is None:
        _get_sync_session()
    from app.db.database import Base
    Base.metadata.create_all(_sync_engine, tables=[PromptOverride.__table__])


def get_all_prompts() -> list[dict]:
    """获取所有提示词（含覆盖状态）。"""
    _ensure_table()
    with _get_sync_session() as session:
        rows = session.execute(
            select(PromptOverride).order_by(PromptOverride.module, PromptOverride.name)
        ).scalars().all()
        return [
            {
                "id": r.id,
                "module": r.module,
                "name": r.name,
                "value": r.value,
                "default_value": r.default_value,
                "description": r.description,
                "updated_at": r.updated_at.isoformat() if r.updated_at else "",
            }
            for r in rows
        ]


def get_prompt(module: str, name: str) -> str | None:
    """获取指定提示词的当前值（有覆盖返回覆盖值，否则返回 None 表示用默认）。"""
    _ensure_table()
    pid = f"{module}.{name}"
    with _get_sync_session() as session:
        row = session.execute(
            select(PromptOverride).where(PromptOverride.id == pid)
        ).scalar_one_or_none()
        return row.value if row else None


def get_prompt_default(module: str, name: str) -> str | None:
    """获取默认值。"""
    _ensure_table()
    pid = f"{module}.{name}"
    with _get_sync_session() as session:
        row = session.execute(
            select(PromptOverride).where(PromptOverride.id == pid)
        ).scalar_one_or_none()
        return row.default_value if row else None


def register_default(
    module: str,
    name: str,
    default_value: str,
    description: str = "",
) -> None:
    """注册/更新默认值（不会覆盖已有的用户自定义 value）。"""
    _ensure_table()
    pid = f"{module}.{name}"
    now = datetime.now(timezone.utc)
    with _get_sync_session() as session:
        existing = session.execute(
            select(PromptOverride).where(PromptOverride.id == pid)
        ).scalar_one_or_none()

        if existing:
            if existing.value == existing.default_value:
                existing.default_value = default_value
                existing.value = default_value
                existing.description = description
                existing.updated_at = now
            else:
                existing.default_value = default_value
                existing.description = description
                existing.updated_at = now
        else:
            session.add(PromptOverride(
                id=pid,
                module=module,
                name=name,
                value=default_value,
                default_value=default_value,
                description=description,
                updated_at=now,
            ))
        session.commit()


def update_prompt(module: str, name: str, value: str) -> None:
    """更新用户自定义的提示词。"""
    _ensure_table()
    pid = f"{module}.{name}"
    now = datetime.now(timezone.utc)
    with _get_sync_session() as session:
        row = session.execute(
            select(PromptOverride).where(PromptOverride.id == pid)
        ).scalar_one_or_none()
        if row:
            row.value = value
            row.updated_at = now
            session.commit()
        else:
            logger.warning("[PromptStore] 更新失败：提示词 {} 不存在，请先 register_default", pid)


def reset_prompt(module: str, name: str) -> None:
    """恢复默认值。"""
    _ensure_table()
    pid = f"{module}.{name}"
    now = datetime.now(timezone.utc)
    with _get_sync_session() as session:
        row = session.execute(
            select(PromptOverride).where(PromptOverride.id == pid)
        ).scalar_one_or_none()
        if row:
            row.value = row.default_value
            row.updated_at = now
            session.commit()
