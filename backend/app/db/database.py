"""SQLAlchemy 数据库引擎与 Session 管理。

使用 PostgreSQL + asyncpg 异步驱动，作为系统唯一关系数据库后端。
（SQLite 已弃用移除，统一使用 PostgreSQL）

设计依据：
- docs/05-technical-selection.md §11（PostgreSQL + Milvus）
- templates/knowledge-base-schema.json（表结构来源）
"""

import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings
from app.utils.logger import logger


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。

    所有 ORM 模型继承此类，通过 metadata 统一管理建表。
    """
    pass


# 异步引擎（PostgreSQL only）
engine = create_async_engine(
    settings.pg_async_url,
    echo=settings.DEBUG,
    pool_size=settings.PG_POOL_SIZE,
    pool_timeout=settings.PG_TIMEOUT,
)
logger.info("数据库引擎: PostgreSQL (pool_size={}, timeout={}s)",
            settings.PG_POOL_SIZE, settings.PG_TIMEOUT)

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
    """
    from app.db.models import (
        Requirement, TestCase, TestPattern, ErrorCase, GenerationBatch,
        ReqDependency, Traceability, LLMConfig, Product, Project, Module,
        ModuleDependency, KnowledgeQA, User, PromptOverride, ReqPattern,
        KBSetting, AnalysisTask,
    )
    from app.db.seed import seed_patterns, seed_admin_user

    settings.ensure_data_dirs()
    logger.info("初始化 PostgreSQL 数据库")

    async with engine.begin() as conn:
        from sqlalchemy import inspect, text

        # ================================================================
        # 综合数据库迁移：检测所有表的所有缺失列，一次性修复
        # ================================================================
        SCHEMA_MIGRATIONS: dict[str, list[tuple[str, str, str]]] = {
            "llm_config": [
                ("provider_label", "VARCHAR(128) NOT NULL DEFAULT ''", "provider_label"),
                ("is_primary", "BOOLEAN NOT NULL DEFAULT FALSE", "is_primary"),
            ],
            "requirements": [
                ("project_id", "VARCHAR(64) REFERENCES projects(id)", "project_id"),
                ("superseded_by", "VARCHAR(64) REFERENCES requirements(id)", "superseded_by"),
                ("supersedes", "VARCHAR(64) REFERENCES requirements(id)", "supersedes"),
                ("change_type", "VARCHAR(16)", "change_type"),
                ("change_summary", "TEXT NOT NULL DEFAULT ''", "change_summary"),
                ("diff_json", "JSON NOT NULL DEFAULT '{}'", "diff_json"),
                ("go_live_date", "TIMESTAMP", "go_live_date"),
                # 归档时间真实字段（区别于 updated_at，避免被任何字段修改污染）
                ("archived_at", "TIMESTAMP WITH TIME ZONE", "archived_at"),
                # 向量化状态追踪
                ("vectorization_status", "VARCHAR(20)", "vectorization_status"),
                ("vectorization_error", "TEXT", "vectorization_error"),
            ],
            "test_cases": [
                # 用例归档时间真实字段
                ("archived_at", "TIMESTAMP WITH TIME ZONE", "archived_at"),
            ],
            "projects": [
                ("version", "VARCHAR(32) DEFAULT '1.0'", "version"),
            ],
            "knowledge_qa": [
                ("conversation_history", "JSON NOT NULL DEFAULT '[]'", "conversation_history"),
                # 产品隔离字段（替代归档时硬编码的 PROD-DEFAULT）
                ("product_id", "VARCHAR(64) DEFAULT 'PROD-DEFAULT'", "product_id"),
            ],
            # v1.4: 功能点新表（Base.metadata.create_all 自动创建）
            "functional_points": [],
            "fp_dependencies": [],
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

            # Step 5: BM25 全文检索 — zhparser 中文分词 + tsvector + GIN 索引
            # 为 requirements 和 test_cases 表添加 search_vec 列（docs/02 §3.1 混合检索）
            # 安全策略：检测 text search config 是否可用，不可用时跳过（不影响启动）
            BM25_SETUPS: list[tuple[str, str, str]] = [
                ("requirements", "search_vec", "title || ' ' || COALESCE(raw_text, '')"),
                ("test_cases", "search_vec", "title || ' ' || COALESCE(case_json::text, '')"),
            ]

            # 检测可用的中文分词配置：优先 chinese(zhparser)，回退 simple
            ts_config = "chinese"
            try:
                cfg_check = conn_sync.execute(
                    text("SELECT 1 FROM pg_ts_config WHERE cfgname = 'chinese'")
                ).fetchone()
                if not cfg_check:
                    logger.warning(
                        "DB 迁移: 'chinese' 文本搜索配置不存在（zhparser 扩展未安装），"
                        "BM25 将使用 'simple' 配置（中文分词效果有限，建议安装 zhparser）"
                    )
                    ts_config = "simple"
            except Exception:
                ts_config = "simple"

            for table, vec_col, ts_expr in BM25_SETUPS:
                if not inspector.has_table(table):
                    continue
                existing_cols = {c["name"] for c in inspector.get_columns(table)}
                if vec_col not in existing_cols:
                    # 使用 savepoint 隔离失败，避免污染外层事务导致 init_db 整体失败
                    try:
                        sp = conn_sync.begin_nested()
                        conn_sync.execute(text(
                            f"ALTER TABLE {table} ADD COLUMN {vec_col} tsvector"
                        ))
                        conn_sync.execute(text(
                            f"UPDATE {table} SET {vec_col} = to_tsvector('{ts_config}', {ts_expr})"
                        ))
                        conn_sync.execute(text(
                            f"CREATE INDEX idx_{table}_{vec_col} ON {table} USING GIN({vec_col})"
                        ))
                        sp.commit()
                        logger.info("DB 迁移: {}.{} tsvector({}) + GIN 索引已创建", table, vec_col, ts_config)
                    except Exception as mig_e:
                        sp.rollback()
                        logger.warning("DB 迁移: {}.{} BM25 设置失败（已跳过，不影响启动）: {}",
                                       table, vec_col, str(mig_e)[:120])

            # Step 6: go_live_date 列类型升级 DATE → TIMESTAMP（上线时间精确到时分秒）
            if inspector.has_table("requirements"):
                req_cols = {c["name"]: c for c in inspector.get_columns("requirements")}
                col_info = req_cols.get("go_live_date")
                if col_info:
                    col_type_str = str(col_info.get("type", "")).upper()
                    if "DATE" in col_type_str and "TIMESTAMP" not in col_type_str:
                        try:
                            sp = conn_sync.begin_nested()
                            conn_sync.execute(text(
                                "ALTER TABLE requirements "
                                "ALTER COLUMN go_live_date TYPE TIMESTAMP "
                                "USING go_live_date::timestamp"
                            ))
                            sp.commit()
                            logger.info("DB 迁移: requirements.go_live_date DATE → TIMESTAMP 升级完成")
                        except Exception as mig_e:
                            sp.rollback()
                            logger.warning("DB 迁移: go_live_date 类型升级失败: {}", str(mig_e)[:120])

        await conn.run_sync(_migrate)
    logger.info("数据库表创建完成")

    # 清理 stale 任务：进程重启后所有 running 任务无法继续，标记为 interrupted
    try:
        async with async_session() as cleanup_session:
            from sqlalchemy import update
            result = await cleanup_session.execute(
                update(AnalysisTask)
                .where(AnalysisTask.status.in_(["pending", "running"]))
                .values(status="interrupted", error="进程重启，任务中断", completed_at=datetime.now(timezone.utc))
            )
            await cleanup_session.commit()
            if result.rowcount > 0:
                logger.info("[DB] 清理 stale 分析任务: {} 个 running/pending 标记为 interrupted", result.rowcount)
    except Exception as e:
        logger.warning("[DB] stale 任务清理失败（不影响启动）: {}", str(e)[:150])

    # 写入种子数据
    await seed_patterns()
    await seed_admin_user()
    logger.info("种子数据初始化完成")


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库 Session（FastAPI 依赖注入用）。

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
            await session.rollback()
            raise
        finally:
            await session.close()
