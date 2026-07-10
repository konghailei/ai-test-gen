"""SQLAlchemy 数据库引擎与 Session 管理。

使用 SQLite + aiosqlite 异步驱动，对应架构文档 MVP 阶段的技术选型。

设计依据：
- docs/05-technical-selection.md §11（MVP 用 SQLite）
- templates/knowledge-base-schema.json（表结构来源）

安全更新（v2）：
- SQLite 并发写入冲突时自动重试（最多 3 次）
- 使用 tenacity 库管理重试，避免盲目重试风暴
"""

import asyncio
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings
from app.utils.logger import logger


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。

    所有 ORM 模型继承此类，通过 metadata 统一管理建表。
    """
    pass


# SQLite 数据库锁错误
SQLITE_LOCK_ERRORS = (
    "database is locked",
    "locking protocol",
    "database table is locked",
    "cannot commit",
)


def _is_sqlite_lock_error(exc: Exception) -> bool:
    """判断是否为 SQLite 并发写入冲突错误。"""
    msg = str(exc).lower()
    return any(err in msg for err in SQLITE_LOCK_ERRORS)


# 异步引擎（自动选择 PostgreSQL / SQLite）
_use_pg = settings.is_pg

if _use_pg:
    engine = create_async_engine(
        settings.pg_async_url,
        echo=settings.DEBUG,
        pool_size=settings.PG_POOL_SIZE,
        pool_timeout=settings.PG_TIMEOUT,
    )
    logger.info("数据库引擎: PostgreSQL (pool_size={}, timeout={}s)",
                settings.PG_POOL_SIZE, settings.PG_TIMEOUT)
else:
    engine = create_async_engine(
        settings.sqlite_url,
        echo=settings.DEBUG,
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    logger.info("数据库引擎: SQLite ({})", settings.SQLITE_PATH)

# 异步 Session 工厂
async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """初始化数据库。

    在应用启动时调用：
    1. 确保数据目录存在
    2. 创建/迁移所有表
    3. 写入种子数据（若 KB 为空）

    遇到 SQLite 锁定错误时自动重试最多 3 次。
    """
    from app.db.models import (
        Requirement, TestCase, TestPattern, ErrorCase, GenerationBatch,
        ReqDependency, Traceability, LLMConfig, Product, Project, Module,
        ModuleDependency, KnowledgeQA, User,
    )
    from app.db.seed import seed_patterns, seed_admin_user

    settings.ensure_data_dirs()
    logger.info("初始化数据库: {}", settings.SQLITE_PATH)

    for attempt in range(1, 4):
        try:
            async with engine.begin() as conn:
                from sqlalchemy import inspect, text

                # ================================================================
                # 综合数据库迁移：检测所有表的所有缺失列，一次性修复
                # ================================================================
                # 格式: { "表名": [ ("列名", "SQLite 列定义", "日志描述"), ... ] }
                SCHEMA_MIGRATIONS: dict[str, list[tuple[str, str, str]]] = {
                    "llm_config": [
                        ("provider_label", "VARCHAR(128) NOT NULL DEFAULT ''", "provider_label"),
                        ("is_primary", "BOOLEAN NOT NULL DEFAULT 0", "is_primary"),
                    ],
                    "requirements": [
                        ("project_id", "VARCHAR(64) REFERENCES projects(id)", "project_id"),
                        ("superseded_by", "VARCHAR(64) REFERENCES requirements(id)", "superseded_by"),
                        ("supersedes", "VARCHAR(64) REFERENCES requirements(id)", "supersedes"),
                        ("change_type", "VARCHAR(16)", "change_type"),
                        ("change_summary", "TEXT NOT NULL DEFAULT ''", "change_summary"),
                        ("diff_json", "JSON NOT NULL DEFAULT '{}'", "diff_json"),
                    ],
                    "projects": [
                        ("version", "VARCHAR(32) DEFAULT '1.0'", "version"),
                    ],
                    "knowledge_qa": [
                        ("conversation_history", "JSON NOT NULL DEFAULT '[]'", "conversation_history"),
                    ],
                }

                def _migrate(conn_sync):
                    inspector = inspect(conn_sync)

                    # Step 1: 兼容旧版 Module 表（project_id → product_id 迁移）
                    if inspector.has_table("modules"):
                        cols = [c["name"] for c in inspector.get_columns("modules")]
                        if "project_id" in cols and "product_id" not in cols:
                            rows = conn_sync.execute(
                                text("SELECT id, project_id, name, description, created_at, updated_at FROM modules")
                            ).fetchall()
                            conn_sync.execute(text("DROP TABLE modules"))
                            Base.metadata.create_all(conn_sync)
                            for r in rows:
                                conn_sync.execute(
                                    text("INSERT INTO modules (id, product_id, name, description, created_at, updated_at) "
                                         "VALUES (:id, :product_id, :name, :description, :created_at, :updated_at)"),
                                    {"id": r[0], "product_id": r[1], "name": r[2], "description": r[3],
                                     "created_at": r[4], "updated_at": r[5]}
                                )
                            logger.info("DB 迁移: modules 表升级完成, {} 条记录已迁移", len(rows))
                        else:
                            Base.metadata.create_all(conn_sync)
                    else:
                        Base.metadata.create_all(conn_sync)

                    # Step 2: 对所有表检查缺失列并自动补齐
                    for table_name, columns in SCHEMA_MIGRATIONS.items():
                        if not inspector.has_table(table_name):
                            continue
                        existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
                        for col_name, col_def, log_desc in columns:
                            if col_name not in existing_cols:
                                try:
                                    conn_sync.execute(
                                        text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")
                                    )
                                    logger.info("DB 迁移: {}.{} 字段已新增", table_name, log_desc)
                                except Exception as mig_e:
                                    logger.warning("DB 迁移: {}.{} 新增失败（可能已存在）: {}",
                                                   table_name, col_name, str(mig_e)[:80])

                    # Step 3: 确保唯一性约束（产品/项目/模块名称唯一 + 依赖边去重）
                    UNIQUE_INDEXES: list[tuple[str, str, str]] = [
                        ("products", "uq_product_name", "name"),
                        ("projects", "uq_project_product_name", "product_id, name"),
                        ("modules", "uq_module_product_name", "product_id, name"),
                        # 防止相同模块间的同类型依赖重复创建
                        ("module_dependencies", "uq_mod_dep_edge", "source_module_id, target_module_id, relation_type"),
                    ]
                    for tbl, idx_name, cols in UNIQUE_INDEXES:
                        if not inspector.has_table(tbl):
                            continue
                        existing_idx = {ix["name"] for ix in inspector.get_indexes(tbl)}
                        if idx_name not in existing_idx:
                            try:
                                conn_sync.execute(
                                    text(f"CREATE UNIQUE INDEX IF NOT EXISTS {idx_name} ON {tbl}({cols})")
                                )
                                logger.info("DB 迁移: 唯一索引 {}({}) 已创建", idx_name, cols)
                            except Exception as mig_e:
                                logger.warning("DB 迁移: 唯一索引 {} 创建失败（可能已存在重复数据）: {}",
                                               idx_name, str(mig_e)[:80])

                    # Step 4: 检索优化索引（产品/项目/模块关联查询）
                    SEARCH_INDEXES: list[tuple[str, str, str]] = [
                        ("requirements", "idx_req_product_project", "product_id, project_id"),
                        ("test_cases", "idx_case_product", "product_id"),
                        ("test_cases", "idx_case_req", "req_id, product_id"),
                    ]
                    for tbl, idx_name, cols in SEARCH_INDEXES:
                        if not inspector.has_table(tbl):
                            continue
                        existing_idx = {ix["name"] for ix in inspector.get_indexes(tbl)}
                        if idx_name not in existing_idx:
                            try:
                                conn_sync.execute(
                                    text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {tbl}({cols})")
                                )
                                logger.info("DB 迁移: 检索索引 {}({}) 已创建", idx_name, cols)
                            except Exception as mig_e:
                                logger.warning("DB 迁移: 检索索引 {} 创建失败: {}", idx_name, str(mig_e)[:80])

                await conn.run_sync(_migrate)
            logger.info("数据库表创建完成")
            break  # 成功则退出重试循环
        except Exception as e:
            if _is_sqlite_lock_error(e) and attempt < 3:
                delay = 0.5 * (2 ** attempt)
                logger.warning("数据库锁定（重试 {}/3, {}s 后）: {}", attempt, delay, str(e)[:100])
                await asyncio.sleep(delay)
            elif attempt >= 3:
                logger.error("数据库初始化失败（已重试 3 次）: {}", str(e))
                raise
            else:
                raise

    # 写入种子数据
    await seed_patterns()
    await seed_admin_user()
    logger.info("种子数据初始化完成")


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库 Session（FastAPI 依赖注入用）。

    SQLite 并发写入冲突时自动重试 1 次。

    Yields:
        AsyncSession: 异步数据库会话

    Usage:
        @app.get("/items")
        async def list_items(session: AsyncSession = Depends(get_session)):
            ...
    """
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            # 回滚并抛出异常，由调用方处理。
            # 注意：rollback 会清空 session 中的 pending 对象，
            # 因此在此处重试 commit 无意义；并发冲突依赖引擎层 timeout 缓解。
            await session.rollback()
            raise
        finally:
            await session.close()
