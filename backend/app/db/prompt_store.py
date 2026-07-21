"""提示词持久化存储 — 基于 PostgreSQL 的简单 key-value store。

每个模块的每个提示词标识为一个 key（如 "req_agent.SYSTEM_PROMPT"），
value 为提示词文本。default_value 保存初始默认值，支持一键恢复。

v2.3 新增版本管理：
- 所有修改自动写入 prompt_history 表
- 支持查看历史版本列表
- 支持从任意历史版本恢复

使用 SQLAlchemy 同步 engine（psycopg2 驱动），保持同步接口，
供 prompt_manager 在 Agent 初始化和 API 路由中同步调用。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db.models import PromptOverride, PromptHistory
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
    """确保 prompt_overrides 和 prompt_history 表存在（首次调用时建表）。

    v2.3: 同时补齐 prompt_overrides.updated_by 列（已有表需要 ALTER TABLE）。
    """
    global _sync_engine
    if _sync_engine is None:
        _get_sync_session()
    from app.db.database import Base
    Base.metadata.create_all(_sync_engine, tables=[PromptOverride.__table__, PromptHistory.__table__])
    # v2.3: 补齐 prompt_overrides.updated_by 列（已存在的表不会被 create_all 修改）
    try:
        with _sync_engine.connect() as conn:
            # 检查列是否存在
            result = conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'prompt_overrides' AND column_name = 'updated_by'"
            ))
            if result.fetchone() is None:
                conn.execute(text(
                    "ALTER TABLE prompt_overrides ADD COLUMN updated_by VARCHAR(64) NOT NULL DEFAULT ''"
                ))
                conn.commit()
                logger.info("[PromptStore] 已补齐 prompt_overrides.updated_by 列")
    except Exception as e:
        logger.warning(f"[PromptStore] 检查/补齐 updated_by 列失败: {e}")


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
                "updated_by": getattr(r, "updated_by", "") or "",
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


def update_prompt(module: str, name: str, value: str, changed_by: str = "api") -> None:
    """更新用户自定义的提示词。

    v2.3: 自动写入 prompt_history 表记录修改历史。
    """
    _ensure_table()
    pid = f"{module}.{name}"
    now = datetime.now(timezone.utc)
    with _get_sync_session() as session:
        row = session.execute(
            select(PromptOverride).where(PromptOverride.id == pid)
        ).scalar_one_or_none()
        if row:
            old_value = row.value or ""
            # v2.3: 仅当值确实变化时写入历史
            if old_value != value:
                # 生成修改摘要
                old_len = len(old_value)
                new_len = len(value)
                diff_chars = abs(new_len - old_len)
                if diff_chars == 0:
                    summary = f"内容修改（字符数不变，{old_len}字）"
                else:
                    direction = "增加" if new_len > old_len else "减少"
                    summary = f"内容修改（{direction} {diff_chars} 字，{old_len} → {new_len}）"

                session.add(PromptHistory(
                    prompt_id=pid,
                    module=module,
                    name=name,
                    old_value=old_value,
                    new_value=value,
                    changed_by=changed_by,
                    changed_at=now,
                    change_summary=summary,
                ))
                row.value = value
                row.updated_by = changed_by
                row.updated_at = now
                session.commit()
                logger.info("[PromptStore] 提示词已更新: {} (by={}, diff={})", pid, changed_by, summary)
        else:
            logger.warning("[PromptStore] 更新失败：提示词 {} 不存在，请先 register_default", pid)


def reset_prompt(module: str, name: str, changed_by: str = "api") -> None:
    """恢复默认值。

    v2.3: 自动写入 prompt_history 表记录恢复操作。
    """
    _ensure_table()
    pid = f"{module}.{name}"
    now = datetime.now(timezone.utc)
    with _get_sync_session() as session:
        row = session.execute(
            select(PromptOverride).where(PromptOverride.id == pid)
        ).scalar_one_or_none()
        if row:
            old_value = row.value or ""
            new_value = row.default_value or ""
            # v2.3: 写入恢复默认值的历史记录
            if old_value != new_value:
                session.add(PromptHistory(
                    prompt_id=pid,
                    module=module,
                    name=name,
                    old_value=old_value,
                    new_value=new_value,
                    changed_by=changed_by,
                    changed_at=now,
                    change_summary="恢复为默认值",
                ))
            row.value = row.default_value
            row.updated_by = changed_by
            row.updated_at = now
            session.commit()


def list_prompt_history(module: str, name: str, limit: int = 50) -> list[dict]:
    """获取提示词修改历史列表。

    v2.3 新增。返回最近 N 次修改记录，按时间倒序。
    """
    _ensure_table()
    pid = f"{module}.{name}"
    with _get_sync_session() as session:
        rows = session.execute(
            select(PromptHistory)
            .where(PromptHistory.prompt_id == pid)
            .order_by(PromptHistory.changed_at.desc())
            .limit(limit)
        ).scalars().all()
        return [
            {
                "id": r.id,
                "prompt_id": r.prompt_id,
                "module": r.module,
                "name": r.name,
                "old_value": r.old_value,
                "new_value": r.new_value,
                "changed_by": r.changed_by,
                "changed_at": r.changed_at.isoformat() if r.changed_at else "",
                "change_summary": r.change_summary,
            }
            for r in rows
        ]


def restore_prompt_from_history(history_id: int, changed_by: str = "api") -> dict:
    """从历史版本恢复提示词。

    v2.3 新增。将指定历史版本的 new_value 恢复为当前提示词值，
    并记录本次恢复操作到历史表。

    Args:
        history_id: 历史记录 ID
        changed_by: 操作人

    Returns:
        dict: 含恢复后的提示词信息

    Raises:
        ValueError: 历史记录不存在
    """
    _ensure_table()
    with _get_sync_session() as session:
        history = session.execute(
            select(PromptHistory).where(PromptHistory.id == history_id)
        ).scalar_one_or_none()
        if not history:
            raise ValueError(f"历史记录 {history_id} 不存在")

        pid = history.prompt_id
        # 要恢复的值 = 历史记录的 new_value（即那一次修改后的值）
        restore_value = history.new_value

        row = session.execute(
            select(PromptOverride).where(PromptOverride.id == pid)
        ).scalar_one_or_none()
        if not row:
            raise ValueError(f"提示词 {pid} 不存在")

        old_value = row.value or ""
        now = datetime.now(timezone.utc)

        # 写入恢复操作的历史记录
        session.add(PromptHistory(
            prompt_id=pid,
            module=row.module,
            name=row.name,
            old_value=old_value,
            new_value=restore_value,
            changed_by=changed_by,
            changed_at=now,
            change_summary=f"从历史版本 #{history_id} 恢复",
        ))

        row.value = restore_value
        row.updated_by = changed_by
        row.updated_at = now
        session.commit()

        logger.info(
            "[PromptStore] 提示词已从历史恢复: pid={}, history_id={}, by={}",
            pid, history_id, changed_by,
        )
        return {
            "prompt_id": pid,
            "module": row.module,
            "name": row.name,
            "value": restore_value,
            "restored_from_history_id": history_id,
            "changed_by": changed_by,
        }
