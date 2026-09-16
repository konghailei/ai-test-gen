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
# v3.1.2 修复「切换产品加载项目列表失败」：
# 根因是池内连接偶发僵死（网络中断/PG 侧连接失效后，池仍把死连接发给请求），
# 查询发出后永远无响应 → 请求挂死 → 用户刷新放弃（nginx 499）→ 前端报错。
# 防御（宁报错不挂死）：
# - pool_pre_ping：每次取连接前探活，自动剔除僵死连接
# - pool_recycle：30 分钟强制回收长连接，避免半开连接
# - statement_timeout：PG 侧语句 60s 超时，任何挂死查询会被强制取消并报错
# - idle_in_transaction_session_timeout：事务中 idle 超 120s 的会话强制断开，防止长事务持锁
# - SQL echo 与 DEBUG 解耦（SQL_ECHO 单独控制）：全量 SQL 日志拖慢事件循环
engine = create_async_engine(
    settings.pg_async_url,
    echo=settings.SQL_ECHO,
    pool_size=settings.PG_POOL_SIZE,
    pool_timeout=settings.PG_TIMEOUT,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=1800,
    connect_args={
        "timeout": 10,           # TCP 连接建立超时（秒）
        "command_timeout": 60,   # 单语句执行超时（秒）
        "server_settings": {
            "statement_timeout": "60000",
            # v3.3.2: 调大事务空闲容忍度。finalize/澄清重解析等同步方案会长时间持有连接等待 LLM
            # 生成（平均可达 200s+），原 120s 的 idle_in_transaction_session_timeout 会用空闲
            # 超时主动断开连接，导致 commit 时报 "connection is closed"。调至 900s 仅放宽长事务
            # 空闲容忍，不变更业务逻辑与结果（原值主要防御极端异常连接，900s 已足够）。
            "idle_in_transaction_session_timeout": "900000",
        },
    },
)
logger.info("数据库引擎: PostgreSQL (pool_size={}, timeout={}s, pre_ping=True, recycle=1800s, statement_timeout=60s)",
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
        KBSetting, AnalysisTask, FPDependencyHistory, PromptHistory,
        LLMCallLog, OperationLog,
        # v2.45: 补全 v2.45 新增模型的导入（虽然 Base.metadata.create_all 会自动建表，
        # 但显式导入保持与 models.py 定义一致，避免未来重构遗漏）
        FunctionalPoint, FPDependency, FPDependencyVersion,
        FPDependencyVersionItem, Role,
        # v3.2: 功能点权威完整版 SRR（CRSR）
        FunctionalPointCRSR,
        # v3.4: 功能点最新版用例集（与 CRSR 对称，回归基线）
        FunctionalPointCases,
        # v3.4: 评估运行记录（prompt 候选激活的服务器端证据，docs/15 §4.5 P0-5）
        EvaluationRun,
        # v3.4: 向量库写入出箱（PG→Milvus 事务性 outbox，docs/15 §4.4）
        VectorOutbox,
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
                # v2.36: 思考模式开关
                ("enable_thinking", "BOOLEAN NOT NULL DEFAULT FALSE", "enable_thinking"),
                # v2.37: 自定义思考参数（JSON 字符串）
                ("thinking_params", "TEXT NOT NULL DEFAULT ''", "thinking_params"),
            ],
            "requirements": [
                ("project_id", "VARCHAR(64) REFERENCES projects(id)", "project_id"),
                ("superseded_by", "VARCHAR(64) REFERENCES requirements(id)", "superseded_by"),
                ("supersedes", "VARCHAR(64) REFERENCES requirements(id)", "supersedes"),
                ("change_type", "VARCHAR(16)", "change_type"),
                ("change_summary", "TEXT NOT NULL DEFAULT ''", "change_summary"),
                ("diff_json", "JSON NOT NULL DEFAULT '{}'", "diff_json"),
                # v2.45 修复：go_live_date 改为 TIMESTAMP WITH TIME ZONE，与 models.py 一致
                ("go_live_date", "TIMESTAMP WITH TIME ZONE", "go_live_date"),
                # 归档时间真实字段（区别于 updated_at，避免被任何字段修改污染）
                ("archived_at", "TIMESTAMP WITH TIME ZONE", "archived_at"),
                # 向量化状态追踪
                ("vectorization_status", "VARCHAR(20)", "vectorization_status"),
                ("vectorization_error", "TEXT", "vectorization_error"),
                # v2.17: 补充文件内容（重新分析时上传的补充需求文件，不替代原始 raw_text）
                ("supplementary_text", "TEXT NOT NULL DEFAULT ''", "supplementary_text"),
                # v2.18: docx 原始图片（base64 data_url），供前端展示对比
                ("raw_images", "JSON NOT NULL DEFAULT '[]'::json", "raw_images"),
                # v2.33: 多人协作冲突检测——每次 srr_json 被修改时自增
                ("revision", "INTEGER NOT NULL DEFAULT 1", "revision"),
            ],
            "test_cases": [
                # 用例归档时间真实字段
                ("archived_at", "TIMESTAMP WITH TIME ZONE", "archived_at"),
            ],
            "projects": [
                # v2.45 修复：version 字段补 NOT NULL，与 models.py 一致
                ("version", "VARCHAR(32) NOT NULL DEFAULT '1.0'", "version"),
                # v2.45: 项目级归档（从需求级迁移）
                ("status", "VARCHAR(16) NOT NULL DEFAULT 'active'", "status"),
                ("go_live_date", "TIMESTAMP WITH TIME ZONE", "go_live_date"),
                ("archived_at", "TIMESTAMP WITH TIME ZONE", "archived_at"),
                ("archived_by", "VARCHAR(64) NOT NULL DEFAULT ''", "archived_by"),
            ],
            "knowledge_qa": [
                ("conversation_history", "JSON NOT NULL DEFAULT '[]'", "conversation_history"),
                # 产品隔离字段（替代归档时硬编码的 PROD-DEFAULT）
                ("product_id", "VARCHAR(64) DEFAULT 'PROD-DEFAULT'", "product_id"),
                # v2.8.56: 纠正审核流程字段
                ("correction_status", "VARCHAR(32) NOT NULL DEFAULT 'none'", "correction_status"),
                ("correction_module", "VARCHAR(128) NOT NULL DEFAULT ''", "correction_module"),
                # v3.4（docs/15 §4.9）：LLM 失败降级标记——failed 记录不进纠错候选
                ("answer_status", "VARCHAR(16) NOT NULL DEFAULT 'ok'", "answer_status"),
                ("error_message", "TEXT NOT NULL DEFAULT ''", "error_message"),
                # v3.4（docs/15 §4.9）：claims 证据链（逐条结论挂来源 ID）
                ("claims", "JSON NOT NULL DEFAULT '[]'", "claims"),
                ("correction_target_type", "VARCHAR(32) NOT NULL DEFAULT 'requirement'", "correction_target_type"),
                ("correction_target_id", "VARCHAR(64) NOT NULL DEFAULT ''", "correction_target_id"),
                ("reviewed_by", "VARCHAR(64) NOT NULL DEFAULT ''", "reviewed_by"),
                ("reviewed_at", "TIMESTAMP WITH TIME ZONE", "reviewed_at"),
                ("review_notes", "TEXT NOT NULL DEFAULT ''", "review_notes"),
            ],
            # v1.4: 功能点新表（Base.metadata.create_all 自动创建）
            "functional_points": [
                # v1.5.1: FP 独立生命周期；v2.45: 默认值修正为合法状态 pending_review
                ("status", "VARCHAR(16) NOT NULL DEFAULT 'pending_review'", "status"),
                # v2.2: 多模块关联字段（替代单一 module_id，支持 FP 关联多个模块）
                ("modules", "JSON NOT NULL DEFAULT '[]'::json", "modules"),
                # v2.45: 概括描述字段（归档时从 srr_json 同步，用于依赖图谱节点详情）
                ("summary", "TEXT NOT NULL DEFAULT ''", "summary"),
                ("relations_summary", "TEXT NOT NULL DEFAULT ''", "relations_summary"),
            ],
            # v2.3: FP 依赖审核流程字段
            "fp_dependencies": [
                ("review_status", "VARCHAR(16) NOT NULL DEFAULT 'pending'", "review_status"),
                ("reviewed_by", "VARCHAR(64) NOT NULL DEFAULT ''", "reviewed_by"),
                ("reviewed_at", "TIMESTAMP WITH TIME ZONE", "reviewed_at"),
                ("created_by", "VARCHAR(64) NOT NULL DEFAULT 'api'", "created_by"),
                # v2.45 修复：updated_at 补 NOT NULL 和默认值，与 models.py 一致
                ("updated_at", "TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()", "updated_at"),
                # v2.45: 版本管理（项目归档时冻结为历史版本，恢复活动时重新启用）
                ("is_active", "BOOLEAN NOT NULL DEFAULT TRUE", "is_active"),
                # v2.45 修复：version_pk 补外键约束，与 models.py 一致
                ("version_pk", "INTEGER REFERENCES fp_dependency_versions(id) ON DELETE SET NULL", "version_pk"),
            ],
            # v2.8.53 修复：fp_dependency_history 审计字段（v2.45 新增，存量库缺列导致归档 500）
            # 场景：项目归档冻结依赖时写入 old_is_active/new_is_active/old_version_pk/new_version_pk，
            # 旧库未建这 4 列 -> INSERT 报 UndefinedColumnError -> 归档 500。
            "fp_dependency_history": [
                ("old_is_active", "BOOLEAN", "old_is_active"),
                ("new_is_active", "BOOLEAN", "new_is_active"),
                ("old_version_pk", "INTEGER", "old_version_pk"),
                ("new_version_pk", "INTEGER", "new_version_pk"),
            ],
            # v2.3: 提示词版本管理字段
            "prompt_overrides": [
                ("updated_by", "VARCHAR(64) NOT NULL DEFAULT ''", "updated_by"),
            ],
            # v2.23: 任务阶段耗时追踪
            "analysis_tasks": [
                ("phase_timings", "JSON NOT NULL DEFAULT '{}'::json", "phase_timings"),
            ],
            # v2.45: fp_dependency_version_items 跨需求双向追溯字段
            # 该表为 v2.45 新表，整体由 create_all 创建；此处补条目是为
            # 早期 v2.45 构建已建表但缺这 4 列的存量库提供 ALTER TABLE 补列能力。
            # NOT NULL 列需带 DEFAULT '' 以便存量行填充，避免 ALTER 失败。
            "fp_dependency_version_items": [
                ("source_req_id", "VARCHAR(64) NOT NULL DEFAULT ''", "source_req_id"),
                ("source_req_title", "VARCHAR(512) NOT NULL DEFAULT ''", "source_req_title"),
                ("target_req_id", "VARCHAR(64) NOT NULL DEFAULT ''", "target_req_id"),
                ("target_req_title", "VARCHAR(512) NOT NULL DEFAULT ''", "target_req_title"),
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
                ("module_dependencies", "uq_mod_dep_edge", "source_module_id, target_module_id, relation_type"),
                # v2.3: FP 依赖边去重
                ("fp_dependencies", "uq_fp_dep_edge", "source_fp_id, target_fp_id, relation_type"),
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

            # Step 3b: 活动生成任务唯一索引（partial index —— 上面的 (tbl,name,cols) 元组
            # 描述不了 WHERE 条件，故单列一步）。
            # v3.4（docs/15 §4.3）：生成互斥的 **DB 层最终防线**，配合 core/task_guard 的
            # 事务内 advisory lock 使用——保证同一需求同时最多一个 pending/running 的生成任务。
            if inspector.has_table("analysis_tasks"):
                try:
                    conn_sync.execute(text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_analysis_tasks_active_generation "
                        "ON analysis_tasks (req_id) "
                        "WHERE status IN ('pending','running') "
                        "AND task_type IN ('generate_cases','regenerate')"
                    ))
                    logger.info("DB 迁移: 活动生成任务唯一索引已就绪（生成互斥）")
                except Exception as mig_e:
                    logger.warning(
                        "DB 迁移: 活动生成任务唯一索引创建失败（可能有并发的历史残留任务）: {}",
                        str(mig_e)[:120],
                    )

            # Step 3c: vector_outbox.revision 列类型兜底
            # v3.4（docs/15 §4.4）：revision 语义是"变更时刻的 epoch 毫秒"（~1.8e12），
            # 建表初期若用 INTEGER 会直接 int32 溢出报错；create_all 不会修改已存在列的
            # 类型，故这里显式且幂等地 ALTER（已是 BIGINT 时为无操作）。
            if inspector.has_table("vector_outbox"):
                try:
                    conn_sync.execute(text(
                        "ALTER TABLE vector_outbox ALTER COLUMN revision TYPE BIGINT"
                    ))
                except Exception as mig_e:
                    logger.warning("DB 迁移: vector_outbox.revision 扩为 BIGINT 失败: {}", str(mig_e)[:120])

            # Step 4: 检索优化索引（产品/项目/模块关联查询）
            SEARCH_INDEXES: list[tuple[str, str, str]] = [
                ("requirements", "idx_req_product_project", "product_id, project_id"),
                ("test_cases", "idx_case_product", "product_id"),
                ("test_cases", "idx_case_req", "req_id, product_id"),
                # v2.45: fp_dependencies 新增列的索引（存量库 ALTER TABLE 不自动建索引）
                ("fp_dependencies", "idx_fp_dep_active", "is_active"),
                ("fp_dependencies", "idx_fp_dep_version", "version_pk"),
                ("fp_dependencies", "idx_fp_dep_review", "review_status"),
                # v2.8.56: 知识问答纠正审核筛选索引
                ("knowledge_qa", "idx_kqa_correction_status", "correction_status"),
                ("knowledge_qa", "idx_kqa_correction_module", "correction_module"),
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
            BM25_SETUPS: list[tuple[str, str, str, str]] = [
                ("requirements", "search_vec",
                 "title || ' ' || COALESCE(raw_text, '')",
                 "NEW.title || ' ' || COALESCE(NEW.raw_text, '')"),
                ("test_cases", "search_vec",
                 "title || ' ' || COALESCE(case_json::text, '')",
                 "NEW.title || ' ' || COALESCE(NEW.case_json::text, '')"),
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

            for table, vec_col, ts_expr, ts_expr_new in BM25_SETUPS:
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

                # v2.8.55 Bug C-1: 创建触发器，INSERT/UPDATE 时自动维护 search_vec
                # 之前仅有一次性 UPDATE，后续新增/修改的数据 search_vec 为 NULL，BM25 检索失效
                try:
                    sp_trig = conn_sync.begin_nested()
                    fn_name = f"search_vec_update_{table}"
                    trig_name = f"trg_{table}_search_vec"
                    conn_sync.execute(text(f"""
                        CREATE OR REPLACE FUNCTION {fn_name}() RETURNS trigger AS $$
                        BEGIN
                            NEW.{vec_col} := to_tsvector('{ts_config}', {ts_expr_new});
                            RETURN NEW;
                        END;
                        $$ LANGUAGE plpgsql
                    """))
                    conn_sync.execute(text(f"DROP TRIGGER IF EXISTS {trig_name} ON {table}"))
                    conn_sync.execute(text(f"""
                        CREATE TRIGGER {trig_name}
                        BEFORE INSERT OR UPDATE ON {table}
                        FOR EACH ROW EXECUTE FUNCTION {fn_name}()
                    """))
                    # 回填历史 NULL 行（触发器仅对后续 INSERT/UPDATE 生效）
                    conn_sync.execute(text(
                        f"UPDATE {table} SET {vec_col} = to_tsvector('{ts_config}', {ts_expr}) "
                        f"WHERE {vec_col} IS NULL"
                    ))
                    sp_trig.commit()
                    logger.info("DB 迁移: {}.{} 触发器 {} 已创建（自动维护 search_vec）",
                                table, vec_col, trig_name)
                except Exception as trig_e:
                    sp_trig.rollback()
                    logger.warning("DB 迁移: {}.{} 触发器创建失败（已跳过）: {}",
                                   table, vec_col, str(trig_e)[:120])

            # Step 6: go_live_date 列类型升级（DATE/TIMESTAMP → TIMESTAMP WITH TIME ZONE）
            if inspector.has_table("requirements"):
                req_cols = {c["name"]: c for c in inspector.get_columns("requirements")}
                col_info = req_cols.get("go_live_date")
                if col_info:
                    col_type_str = str(col_info.get("type", "")).upper()
                    # v2.45: 统一升级为 TIMESTAMP WITH TIME ZONE（DATE 或无时区的 TIMESTAMP 都需升级）
                    needs_upgrade = ("DATE" in col_type_str and "TIMESTAMP" not in col_type_str) or \
                                    ("TIMESTAMP" in col_type_str and "WITH TIME ZONE" not in col_type_str)
                    if needs_upgrade:
                        try:
                            sp = conn_sync.begin_nested()
                            conn_sync.execute(text(
                                "ALTER TABLE requirements "
                                "ALTER COLUMN go_live_date TYPE TIMESTAMP WITH TIME ZONE "
                                "USING go_live_date::timestamp with time zone"
                            ))
                            sp.commit()
                            logger.info("DB 迁移: requirements.go_live_date → TIMESTAMP WITH TIME ZONE 升级完成")
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
            # 联动回退卡死在 analyzing 的需求（避免澄清/确认/提交全部拒绝）
            req_result = await cleanup_session.execute(
                update(Requirement)
                .where(Requirement.status == "analyzing")
                .values(status="draft")
            )
            if req_result.rowcount > 0:
                await cleanup_session.commit()
                logger.info("[DB] 回退卡死需求: {} 个 analyzing → draft", req_result.rowcount)
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
