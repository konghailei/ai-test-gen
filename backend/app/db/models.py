"""SQLAlchemy ORM 模型。

将 templates/knowledge-base-schema.json 的表结构转为 SQLAlchemy 模型。
PostgreSQL 原生支持 JSON/JSONB/TEXT[]/TIMESTAMPTZ/GIN 索引。

设计依据：
- templates/knowledge-base-schema.json（5 张核心表）
- docs/02-knowledge-base.md:319-453（额外 3 张表：req_dependencies / traceability / req_patterns）
  （req_patterns 表及其向量库 req_pats 已于 2026-10-22 删除：设计从未落地、零消费方，
   遗漏检测实际由规则层 gap 检测承担）
"""

from __future__ import annotations
from datetime import datetime, date, timezone
from sqlalchemy import (
    String, Integer, Float, Text, DateTime, Date, Boolean, ForeignKey, Index, JSON,
    BigInteger, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.database import Base


# ============================================================================
# 1. 需求表（requirements）
# ============================================================================

class Requirement(Base):
    """需求元数据表。

    对应 templates/knowledge-base-schema.json:104-133。
    包含产品隔离、版本管理、模块关联、项目归属。
    """
    __tablename__ = "requirements"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(64), index=True, default="PROD-DEFAULT")
    project_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("projects.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    priority: Mapped[str] = mapped_column(String(4), default="P2")
    module: Mapped[str] = mapped_column(String(128), default="")  # 【已废弃 v2.2】改用 modules[] 数组字段
    modules: Mapped[list] = mapped_column(JSON, default=list)  # 替代 module，支持多模块关联
    tags: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="analyzing")  # analyzing/draft/pending_review/confirmed/active/superseded/deprecated/archived
    srr_json: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    # v2.17: 补充文件内容（重新分析时上传的补充需求文件，不替代原始 raw_text）
    supplementary_text: Mapped[str] = mapped_column(Text, default="")
    # v2.18: docx 中的原始图片（base64 data_url），供前端展示，与 raw_text 中的文字描述对应
    raw_images: Mapped[list] = mapped_column(JSON, default=list)
    source_type: Mapped[str] = mapped_column(String(32), default="markdown")
    source_url: Mapped[str] = mapped_column(String(512), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    superseded_by: Mapped[str | None] = mapped_column(String(64), ForeignKey("requirements.id"), nullable=True)
    # v2.2: 增加 supersedes 字段，支持版本链双向追溯
    supersedes: Mapped[str | None] = mapped_column(String(64), ForeignKey("requirements.id"), nullable=True)

    # 版本链字段（docs/02 额外定义）
    change_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    change_summary: Mapped[str] = mapped_column(Text, default="")
    diff_json: Mapped[dict] = mapped_column(JSON, default=dict)

    # 上线时间：归档时填写，用于 KB 检索时区分最新需求（同功能以靠后时间为准）
    # v2.45 修复：统一使用 timezone=True，与 Project.go_live_date 保持一致
    go_live_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 归档时间：归档时设置，用户可手动修改。区别于 updated_at（任何字段修改都会刷新）。
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 向量化状态追踪
    # null = 未尝试 / 'processing' = Celery 任务进行中 / 'completed' = 已写入 Milvus / 'failed' = 失败（含重试耗尽）
    vectorization_status: Mapped[str | None] = mapped_column(String(20), nullable=True, default=None)
    vectorization_error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)

    # v2.33: 多人协作冲突检测——每次 srr_json 被修改时自增，前端轮询对比 revision 判断是否需要刷新
    revision: Mapped[int] = mapped_column(Integer, default=1)
    # v2.49：需求分析澄清是否追问「可执行性要素缺口」（入口/字段/场景数据/故障注入）。
    # 需求级选项由上传页勾选（默认 True）；与系统级 CLARIFY_EXECUTABILITY_GAPS_ENABLED 取 AND。
    executability_clarify: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        Index("idx_req_product_module", "product_id", "module"),
        Index("idx_req_status", "status"),
        Index("idx_req_product_project", "product_id", "project_id"),
    )


# ============================================================================
# 2. 测试用例表（test_cases）
# ============================================================================

class TestCase(Base):
    """测试用例元数据表。

    对应 templates/knowledge-base-schema.json:135-170。
    case_json 存储完整 UTCF 数据。
    """
    __tablename__ = "test_cases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    req_id: Mapped[str] = mapped_column(String(64), ForeignKey("requirements.id"), index=True)
    product_id: Mapped[str] = mapped_column(String(64), index=True, default="PROD-DEFAULT")
    module: Mapped[str] = mapped_column(String(128), default="")  # 【已废弃 v2.2】改用 modules[]
    modules: Mapped[list] = mapped_column(JSON, default=list)
    title: Mapped[str] = mapped_column(String(512))
    type: Mapped[str] = mapped_column(String(32), default="functional")
    priority: Mapped[str] = mapped_column(String(4), default="P1")
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft/reviewed/approved/rejected
    case_json: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(32), default="ai_generated")
    gen_batch_id: Mapped[str] = mapped_column(String(64), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reviewed_by: Mapped[str] = mapped_column(String(64), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    feedback: Mapped[dict] = mapped_column(JSON, default=dict)
    execution_count: Mapped[int] = mapped_column(Integer, default=0)
    last_pass_rate: Mapped[float] = mapped_column(Float, default=0.0)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    # 归档时间：归档时设置，用户可手动修改。区别于 updated_at（任何字段修改都会刷新）。
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # v3.4.1 修复 H-3：向量化状态追踪（与 Requirement / FunctionalPoint 对齐）。
    # 此前 TestCase 缺这两列，而 tasks/embedding_tasks.py 对其赋值——SQLAlchemy 对未声明
    # 属性的赋值静默忽略，commit 空转，前端"用例向量化状态"永久显示幽灵 pending。
    # null = 未尝试 / 'processing' / 'completed' / 'failed'
    vectorization_status: Mapped[str | None] = mapped_column(String(20), nullable=True, default=None)
    vectorization_error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)

    __table_args__ = (
        Index("idx_case_type", "type"),
        Index("idx_case_priority", "priority"),
        Index("idx_case_status", "status"),
        Index("idx_case_confidence", "confidence"),
        Index("idx_case_batch", "gen_batch_id"),
        Index("idx_case_product", "product_id"),
        Index("idx_case_req", "req_id", "product_id"),
    )


# ============================================================================
# 3. 测试模式表（test_patterns）
# ============================================================================

class TestPattern(Base):
    """测试模式库表。

    对应 templates/knowledge-base-schema.json:172-182。
    存储 GenAgent 参考的测试用例骨架模式。
    """
    __tablename__ = "test_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    category: Mapped[str] = mapped_column(String(64), default="crud")
    pattern_json: Mapped[dict] = mapped_column(JSON, default=dict)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    success_rate: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# ============================================================================
# 4. 错误案例表（error_cases）
# ============================================================================

class ErrorCase(Base):
    """错误案例库表。

    对应 templates/knowledge-base-schema.json:184-194。
    存储 ReviewAgent 复核的 error_case 记录。
    """
    __tablename__ = "error_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    error_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    original_case_id: Mapped[str] = mapped_column(String(64), ForeignKey("test_cases.id"))
    error_type: Mapped[str] = mapped_column(String(64), default="incomplete")
    error_detail: Mapped[str] = mapped_column(Text, default="")
    feedback: Mapped[dict] = mapped_column(JSON, default=dict)
    corrected_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# ============================================================================
# 5. 生成批次表（generation_batches）
# ============================================================================

class GenerationBatch(Base):
    """生成批次记录表。

    对应 templates/knowledge-base-schema.json:196-211。
    用于成本统计与效果分析。
    """
    __tablename__ = "generation_batches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    req_id: Mapped[str] = mapped_column(String(64), ForeignKey("requirements.id"), index=True)
    total_cases: Mapped[int] = mapped_column(Integer, default=0)
    accepted_cases: Mapped[int] = mapped_column(Integer, default=0)
    rejected_cases: Mapped[int] = mapped_column(Integer, default=0)
    model_used: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="v1.0")
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_cost: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    channel: Mapped[str] = mapped_column(String(8), default="FAST")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# ============================================================================
# 6. 需求依赖关系表（req_dependencies，doc-02 §350-358）
# ============================================================================

class ReqDependency(Base):
    """需求依赖关系表（依赖图谱边表）。

    对应 docs/02-knowledge-base.md:350-358。
    ImpactAnalysisAgent 用于影响范围遍历。
    """
    __tablename__ = "req_dependencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_req_id: Mapped[str] = mapped_column(String(64), ForeignKey("requirements.id"), index=True)
    target_req_id: Mapped[str] = mapped_column(String(64), ForeignKey("requirements.id"), index=True)
    relation_type: Mapped[str] = mapped_column(String(32), default="depends_on")
    # replaces / depends_on / related_to / conflicts
    strength: Mapped[str] = mapped_column(String(8), default="weak")  # strong / weak
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_dep_source_target", "source_req_id", "target_req_id"),
    )


# ============================================================================
# 7. 追溯矩阵表（traceability，doc-02 §429-438）
# ============================================================================

class Traceability(Base):
    """需求-用例追溯矩阵表。

    对应 docs/02-knowledge-base.md:429-438。
    支持用例↔需求双向追溯，用于覆盖率计算和影响分析。
    """
    __tablename__ = "traceability"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    test_case_id: Mapped[str] = mapped_column(String(64), ForeignKey("test_cases.id"), index=True)
    requirement_id: Mapped[str] = mapped_column(String(64), ForeignKey("requirements.id"), index=True)
    requirement_field: Mapped[str] = mapped_column(String(128), default="")
    # 如 flows.happy_path / boundary_conditions / business_rules
    trace_type: Mapped[str] = mapped_column(String(16), default="direct")
    # direct / indirect / regression
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    evidence: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# ============================================================================
# 8. LLM 配置持久化表（settings.py 存储前端配置）
# ============================================================================

class LLMConfig(Base):
    """LLM 提供商配置持久化表。

    支持多供应商配置，每条记录代表一个供应商。
    is_primary=True 的条目为当前激活的主供应商。
    启动时从本表读取 is_primary 记录 → 覆盖 settings 单例 → 运行时生效。

    API Key 使用 Fernet 对称加密后存储（v2 安全升级）。
    旧版明文 Key 首次读取时自动迁移为加密存储。

    provider 字段有唯一约束，避免同名供应商重复。

    PUT /api/settings/llm 写入本表。
    GET /api/settings/llm 返回所有供应商列表。
    """
    __tablename__ = "llm_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64), unique=True, index=True, default="deepseek")
    provider_label: Mapped[str] = mapped_column(String(128), default="")
    api_key: Mapped[str] = mapped_column(String(2048), default="")
    base_url: Mapped[str] = mapped_column(String(512), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    # v2.36: 思考模式开关（仅对支持思考的模型生效，如 deepseek-r1/glm-4.5/qwen3-reasoning 等）
    enable_thinking: Mapped[bool] = mapped_column(Boolean, default=False)
    # v2.37: 自定义思考参数（JSON 字符串，为空时使用内置规则；非空时优先使用自定义参数）
    # 示例：'{"thinking": {"type": "enabled", "budget_tokens": 8000}}' 或 '{"enable_thinking": true}'
    thinking_params: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # ========== v3.5.6：启用/禁用开关（持久化，取代进程内存的临时禁用） ==========
    # 背景（2026-10-09 自举实证）：原禁用机制是 `llm_client._provider_disabled_until`
    # （进程内存 + 600s 自动过期）。问题：①重启/多进程不一致（web 与 worker 各有一份）；
    # ②"硬失败"（额度耗尽 401/403）禁用 600s 后自动恢复，会反复撞同一家死供应商、
    # 白等首响应超时；③运维看不到"某家被禁用了"。
    # 改造：硬失败 → 持久化禁用（enabled=False，不自动恢复，管理员手动启用）+ 写通知；
    # 软失败（超时/5xx）→ 不再禁用，改由调用内等待重试兜底。
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 禁用原因：人工禁用固定为"人工禁用"；系统禁用写具体硬失败原因（如"403 额度耗尽"）
    disabled_reason: Mapped[str] = mapped_column(Text, default="")
    # 禁用来源：'manual'（管理员手动）/ 'system'（硬失败自动）；空串=未禁用
    disabled_by: Mapped[str] = mapped_column(String(16), default="")
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ============================================================================
# 8.1 管理员通知表（v3.5.6 新增）
# ============================================================================

class AdminNotification(Base):
    """管理员通知（v3.5.6）。

    首期用途：LLM 供应商被禁用时通知管理员及时排查。
    - 硬失败（额度耗尽/认证失败）→ 系统自动禁用供应商 → 写一条通知；
    - 三家供应商全部调用失败（软失败耗尽）→ 写一条通知（便于及时排查，非禁用）。
    前端：顶部导航铃铛（未读数）+ 通知消息页面。
    """
    __tablename__ = "admin_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 通知类型：llm_provider_disabled（供应商硬失败被禁用）/ llm_all_providers_failed（全部失败）
    type: Mapped[str] = mapped_column(String(64), index=True, default="")
    title: Mapped[str] = mapped_column(String(256), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    # 关联对象（如 type='llm_provider_disabled' 时 related_id=provider 名）
    related_type: Mapped[str] = mapped_column(String(64), default="")
    related_id: Mapped[str] = mapped_column(String(128), default="")
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


# ============================================================================
# 9. 产品表（products）
# ============================================================================

class Product(Base):
    """产品表。顶层组织单元，一个产品下有多个项目。"""
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("name", name="uq_product_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    visible_to_user_ids: Mapped[list[int]] = mapped_column(JSON, default=list, comment="可查看此产品的用户ID列表，空列表表示所有人可见")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# ============================================================================
# 10. 项目表（projects，属于产品）
# ============================================================================

class Project(Base):
    """项目/版本表。属于一个产品，一个项目下有多个需求，涉及多个模块。

    v2.45: 归档操作从需求级迁移到项目级。项目归档后，项目下所有 confirmed 需求自动归档。
    go_live_date / archived_at 提升到项目级，需求级字段保留作快照（向下兼容）。
    """
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("product_id", "name", name="uq_project_product_name"),
        # 同一产品下版本号不可重复。应用层在 create/update 时查重并返回 409；
        # 存量数据存在重复（PROD-57100-974 下有 3 个 v1.0），故未强制 ALTER 加索引，
        # 以免破坏现有数据。新部署（干净库）将由 create_all 自动建立此约束。
        UniqueConstraint("product_id", "version", name="uq_project_product_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(64), ForeignKey("products.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[str] = mapped_column(String(32), default="1.0")
    # v2.46: 项目归档两态机（v2.47 收敛：移除 pending_archive 中间态）
    #   active   — 活跃：可编辑需求/用例/FP
    #   archived — 已归档：写入知识库，生成依赖版本快照，供后续需求检索
    status: Mapped[str] = mapped_column(
        String(16), default="active",
        comment="v2.46 项目归档状态：active（活跃）/ archived（已归档）"
    )
    go_live_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="v2.45 项目级上线时间（归档时设置，恢复活动时清空）"
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="v2.45 项目归档时间"
    )
    archived_by: Mapped[str] = mapped_column(
        String(64), default="",
        comment="v2.45 归档操作人"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# ============================================================================
# 11. 模块表（modules，属于项目）
# ============================================================================

class Module(Base):
    """模块表。属于一个产品（与项目并列），是需求/用例的最小分类单元。"""
    __tablename__ = "modules"
    __table_args__ = (
        UniqueConstraint("product_id", "name", name="uq_module_product_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(64), ForeignKey("products.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    pos_x: Mapped[float] = mapped_column(Float, default=0.0)
    pos_y: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# ============================================================================
# 12. 模块依赖关系表（module_dependencies，图谱边表）
# ============================================================================

class ModuleDependency(Base):
    """模块依赖关系表。

    定义模块间的相互依赖关系，ImpactAnalysisAgent 基于此图做影响范围分析。
    节点 = 模块，边 = 依赖关系。
    关系类型（与 docs/02 §2.4 一致）：
    - depends_on: 模块 A 的功能依赖模块 B（B 是先置条件）
    - related_to: 模块 A 与模块 B 相关（同一功能域）
    - conflicts:  模块 A 与模块 B 冲突（不应同时生效）
    """
    __tablename__ = "module_dependencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_module_id: Mapped[str] = mapped_column(String(64), ForeignKey("modules.id", ondelete="CASCADE"), index=True)
    target_module_id: Mapped[str] = mapped_column(String(64), ForeignKey("modules.id", ondelete="CASCADE"), index=True)
    relation_type: Mapped[str] = mapped_column(String(32), default="depends_on")
    strength: Mapped[str] = mapped_column(String(8), default="weak")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_mod_dep_source_target", "source_module_id", "target_module_id"),
        UniqueConstraint("source_module_id", "target_module_id", "relation_type", name="uq_mod_dep_edge"),
    )


# ============================================================================
# 用户系统
# ============================================================================

class KnowledgeQA(Base):
    """知识问答记录表（v2.4 新增，v2.8.56 增加审核流程）。

    存储用户提问、AI 回答、人工纠错及归档状态。
    conversation_history 存储多轮对话记录。

    v2.8.56 审核流程：
        - /correct 提交纠正后 status=pending_review
        - 审核通过后 status=approved，自动归档到向量库
        - 审核拒绝后 status=rejected
        - correction_module 用于按模块筛选审核列表
        - correction_target_type 支持 requirement / functional_point 两种颗粒度
    """
    __tablename__ = "knowledge_qa"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, default="")
    reference_sources: Mapped[list] = mapped_column(JSON, default=list)
    conversation_history: Mapped[list] = mapped_column(JSON, default=list)
    corrected_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction_notes: Mapped[str] = mapped_column(Text, default="")
    # v3.4（docs/15 §4.9）：LLM 调用失败时的降级文本不得混入正常问答资产——
    # answer_status='failed' 的记录不进纠错归档候选（"AI 服务不可用"不是知识）
    answer_status: Mapped[str] = mapped_column(String(16), default="ok", index=True,
                                               comment="ok / failed")
    error_message: Mapped[str] = mapped_column(Text, default="", comment="失败原因（status=failed 时）")
    # v3.4（docs/15 §4.9）：claims 证据链——回答的关键结论逐条挂 source_ids，
    # 服务端校验 source_ids 必须来自本次检索的 reference_sources
    claims: Mapped[list] = mapped_column(JSON, default=list, comment="[{claim, source_ids}]")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    archived_type: Mapped[str] = mapped_column(String(32), default="")
    archived_id: Mapped[str] = mapped_column(String(64), default="")
    # 产品隔离：问答记录归属的产品 ID（归档时用于关联到正确产品，而非硬编码 PROD-DEFAULT）
    product_id: Mapped[str] = mapped_column(String(64), default="PROD-DEFAULT", index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # v2.8.56: 纠正审核流程字段
    # 纠正状态：none（未纠正）/ pending_review（待审核）/ approved（已通过）/ rejected（已拒绝）
    correction_status: Mapped[str] = mapped_column(String(32), default="none", index=True)
    # 纠正关联的模块名（用于按模块筛选审核列表，从问答上下文中提取或用户指定）
    correction_module: Mapped[str] = mapped_column(String(128), default="", index=True)
    # 纠正目标颗粒度：requirement（整篇需求）/ functional_point（单个功能点）
    correction_target_type: Mapped[str] = mapped_column(String(32), default="requirement")
    # 纠正目标 ID（functional_point 颗粒度时为 FP_ID，requirement 颗粒度时为 req_id）
    correction_target_id: Mapped[str] = mapped_column(String(64), default="")
    # 审核人
    reviewed_by: Mapped[str] = mapped_column(String(64), default="")
    # 审核时间
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 审核意见
    review_notes: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        Index("idx_kqa_created_at", "created_at"),
        Index("idx_kqa_product", "product_id"),
        Index("idx_kqa_correction_status", "correction_status"),
        Index("idx_kqa_correction_module", "correction_module"),
    )


# ============================================================================
# 知识库专属设置（v2.13 新增 - KB LLM 供应商选择等）
# ============================================================================

class KBSetting(Base):
    """知识库专属设置（key-value 方式存储，如 KB 问答使用的 LLM 供应商）。"""
    __tablename__ = "kb_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# ============================================================================
# 异步分析任务（v2.14 新增 - 需求解析/澄清/重生成/影响分析/用例生成等长耗时任务）
# ============================================================================

class AnalysisTask(Base):
    """异步分析任务（需求解析/澄清/重生成/影响分析/用例生成等长耗时任务）。

    后端 upload/clarify/regenerate/impact 端点改为异步后，立即创建本记录并返回 task_id，
    前端轮询任务状态获取真实进度。进程重启时 stale 的 running 任务标记为 interrupted。
    """
    __tablename__ = "analysis_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # TASK-{ms}-{seq}
    task_type: Mapped[str] = mapped_column(String(32), index=True)
    # upload_requirement / submit_clarify / regenerate / analyze_impact / generate_cases / merge_fp
    req_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("requirements.id"), nullable=True, index=True)
    product_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending / running / completed / failed / interrupted
    phase: Mapped[str] = mapped_column(String(32), default="")
    # parse / kb_search / clarify / regenerate / impact / generate / review / format / done
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    message: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)  # 任务结果（req_id/questions 等）
    error: Mapped[str] = mapped_column(Text, default="")
    # v2.23: 阶段耗时追踪 — 自动记录每个 phase 的 start/end/duration_ms
    # 结构：{"parse": {"start": "ISO", "end": "ISO", "duration_ms": 12345}, ...}
    phase_timings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(64), default="api")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        """序列化为前端可用的字典。"""
        return {
            "id": self.id,
            "task_type": self.task_type,
            "req_id": self.req_id,
            "product_id": self.product_id,
            "status": self.status,
            "phase": self.phase,
            "progress": self.progress,
            "message": self.message,
            "result_json": self.result_json or {},
            "error": self.error,
            "phase_timings": self.phase_timings or {},
            "created_at": self.created_at.isoformat() if self.created_at else "",
            "updated_at": self.updated_at.isoformat() if self.updated_at else "",
            "completed_at": self.completed_at.isoformat() if self.completed_at else "",
        }


# ============================================================================
# 用户与角色（v2.12 新增 - 基于角色的菜单权限管理）
# ============================================================================

DEFAULT_MENUS = [
    "dashboard", "requirements", "generate", "testcases",
    "dependency_graph", "knowledge", "knowledge_qa", "correction_review", "projects_modules",
    "admin", "admin_dashboard", "admin_products", "admin_prompts", "admin_settings",
    "admin_users", "admin_roles", "admin_kb", "admin_logs",
]

class Role(Base):
    """系统角色，通过 menus JSON 数组定义可访问的菜单权限。"""

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False, comment="角色名: admin / user / viewer"
    )
    label: Mapped[str] = mapped_column(
        String(64), default="", comment="显示标签"
    )
    menus: Mapped[list[str]] = mapped_column(
        JSON, default=list, comment="可访问菜单列表"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

class User(Base):
    """用户账户。

    存储用户认证信息，密码使用 PBKDF2-SHA256 哈希存储。
    首次启动自动创建内定管理员（admin / 888888）。
    v2.12: role_id 关联 Role 表，灵活权限控制。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False, comment="用户名"
    )
    password_hash: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="PBKDF2 密码哈希"
    )
    role_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("roles.id"), default=None, comment="角色ID"
    )
    display_name: Mapped[str] = mapped_column(
        String(128), default="", comment="显示名称"
    )
    email: Mapped[str] = mapped_column(
        String(128), default="", comment="邮箱"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, comment="是否启用"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


# ============================================================================
# 13. 提示词覆盖表（prompt_overrides，供 prompt_store 使用）
# ============================================================================

class PromptOverride(Base):
    """提示词持久化覆盖表。

    每个模块的每个提示词标识为一个 key（如 "req_agent.SYSTEM_PROMPT"），
    value 为用户自定义的提示词文本，default_value 保存初始默认值。
    支持一键恢复默认值。

    v2.3 新增版本管理：
    - updated_by: 记录最后修改人
    - 所有修改历史写入 PromptHistory 表，支持恢复到任意历史版本
    """
    __tablename__ = "prompt_overrides"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, comment="主键，格式: module.name")
    module: Mapped[str] = mapped_column(String(64), index=True, comment="模块名: req_agent/clarify_agent 等")
    name: Mapped[str] = mapped_column(String(64), comment="提示词名: SYSTEM_PROMPT 等")
    value: Mapped[str] = mapped_column(Text, comment="当前提示词值（用户自定义或默认）")
    default_value: Mapped[str] = mapped_column(Text, comment="代码中的默认值")
    description: Mapped[str] = mapped_column(Text, default="", comment="提示词描述")
    updated_by: Mapped[str] = mapped_column(String(64), default="", comment="v2.3 最后修改人")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )


class PromptHistory(Base):
    """提示词修改历史表（v2.3 新增）。

    记录每次提示词修改的完整历史，包含修改时间、修改人、修改前后的值。
    支持从历史版本恢复提示词，确保可追溯性和操作安全性。
    """
    __tablename__ = "prompt_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prompt_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("prompt_overrides.id", ondelete="CASCADE"),
        index=True, comment="关联的提示词ID（module.name 格式）"
    )
    module: Mapped[str] = mapped_column(String(64), index=True, comment="模块名")
    name: Mapped[str] = mapped_column(String(64), comment="提示词名")
    old_value: Mapped[str] = mapped_column(Text, comment="修改前的值")
    new_value: Mapped[str] = mapped_column(Text, comment="修改后的值")
    changed_by: Mapped[str] = mapped_column(String(64), default="api", comment="修改人")
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    change_summary: Mapped[str] = mapped_column(
        Text, default="",
        comment="修改摘要（自动生成：字符数变化、是否大幅修改等）"
    )

    __table_args__ = (
        Index("idx_prompthist_prompt", "prompt_id", "changed_at"),
    )


# ============================================================================
# 15. 功能点表（functional_points，v1.4）
# ============================================================================

class FunctionalPoint(Base):
    """功能点表。需求按功能点拆分的最小可测单元。

    设计依据：industry BP→FP→Data 三级粒度模型（51testing/UML.org）
    FP 原子性三要素：明确触发操作 + 可验证业务结果 + 有限元素边界
    """
    __tablename__ = "functional_points"
    __table_args__ = (
        UniqueConstraint("requirement_id", "name", name="uq_fp_req_name"),
        Index("idx_fp_module", "module_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    requirement_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("requirements.id", ondelete="CASCADE"), index=True
    )
    module_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("modules.id", ondelete="SET NULL"), nullable=True, index=True
    )
    modules: Mapped[list] = mapped_column(
        JSON, default=list, comment="v2.2: 多模块关联（module_id 字段列表），替代单 module_id"
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    fp_type: Mapped[str] = mapped_column(
        String(32), default="workflow",
        comment="calculation / constraint / workflow / api_behavior / data_record / config"
    )
    priority: Mapped[str] = mapped_column(String(4), default="P2")
    srr_json: Mapped[dict] = mapped_column(JSON, default=dict, comment="功能点专属完整子 SRR（requirement/flows/business_rules/constraints/acceptance_criteria/test_hints）")
    status: Mapped[str] = mapped_column(
        String(16), default="pending_review", comment="pending_review / approved / archived（FP 审核生命周期）"
    )
    # v2.45: FP 概括描述（从 srr_json.sub_srr.summary 同步，用于依赖图谱节点点击查看）
    summary: Mapped[str] = mapped_column(
        Text, default="",
        comment="v2.45 FP 实现内容概括描述（归档时从 srr_json.sub_srr.summary 同步）"
    )
    # v2.45: 与其他 FP 关系概括描述（聚合 FPDependency.description，用于依赖图谱节点点击查看）
    relations_summary: Mapped[str] = mapped_column(
        Text, default="",
        comment="v2.45 与其他功能点关系概括描述（归档时聚合生成）"
    )
    atomicity_check: Mapped[dict] = mapped_column(
        JSON, default=dict,
        comment="三要素自检：trigger / result / boundary，缺项标记 needs_review"
    )
    test_hints: Mapped[list] = mapped_column(JSON, default=list, comment="测试提示（种子用例）")
    vectorization_status: Mapped[str | None] = mapped_column(String(20), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )


# ============================================================================
# 15.5 功能点权威完整版 SRR（functional_point_crsr，v3.2 新增）
# ============================================================================

class FunctionalPointCRSR(Base):
    """功能点跨版本聚合的权威完整版 SRR（Consolidated Requirement Specification）。

    背景：同一功能点常被多个项目版本分别完善，各版本 FP 记录分散存放规则，
    库中无"总结性整体 SRR"。参考腾讯 Agent Memory L0-L3 分层思想：
    - L0/L1（证据层）：各版本 functional_points.srr_json（保留，不删改）
    - L2（场景层）：本表固化的聚合 CRSR，项目归档时重建
    聚合规则：标量取最新非归档版本；数组按内容签名去重并集；冲突以最新版本为准。

    消费方：依赖图谱节点点击、影响分析受影响 FP 展开（PG 直读）；
    后续知识库问答可将其入向量库（doc_type=fp_crsr）。
    """
    __tablename__ = "functional_point_crsr"
    __table_args__ = (
        UniqueConstraint("product_id", "fp_name", name="uq_crsr_product_fpname"),
        Index("idx_crsr_product", "product_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fp_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 聚合后的整体 SRR（字段级合并结果）
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    # 证据链：参与聚合的各版本 FP 记录 ID
    source_fp_ids: Mapped[list] = mapped_column(JSON, default=list)
    # 固化历史（追溯用）：[{version_id, snapshot_at, source_fp_ids}]，version_id 格式 {project.version}-{project.name}
    version_refs: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )


# ============================================================================
# 15.6 功能点权威完整版用例集（functional_point_cases，与 CRSR 对称）
# ============================================================================

class FunctionalPointCases(Base):
    """功能点最新版用例集（Consolidated Test Cases，各版本 approved 用例聚合）。

    背景：同一功能点常被多个项目版本分别完善，各版本下都可能有各自的已批准用例，
    库中无"该 FP 当前应回归的权威用例集"。参考腾讯 Agent Memory L0-L3 分层：
    - L0/L1（证据层）：各版本 test_cases.case_json（保留，不删改）
    - L2（场景层）：本表固化的聚合"最新版用例集"，项目归档时重建

    聚合规则（与 fp_crsr 同构对称）：
    - 同一 FP 的 approved 用例按最新版判定口径（项目上线时间 go_live_date 最大者；
      同项目内按用例 updated_at 取最新）收敛为一份回归基线；
    - 消费方：影响分析确认受影响功能点时，直接取本表最新版用例归入回归测试，
      避免每次从历史堆里现场挑选（回归测试选择 RTS 思路）。

    设计依据：
    - docs/00 §3 阶段④.5（影响分析 → 测试设计）与 docs/03 §5（回归）
    - fp_crsr.py（功能点跨版本聚合 SRR）的同构实现
    """
    __tablename__ = "functional_point_cases"
    __table_args__ = (
        UniqueConstraint("product_id", "fp_name", name="uq_fpc_product_fpname"),
        Index("idx_fpc_product", "product_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fp_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 聚合后的最新版用例集（Toolbox 列表，来自各版本 approved 用例按版本身份收敛）
    content: Mapped[list] = mapped_column(JSON, default=list)
    # 来源维度：最新判定命中的上线时间（ISO），用于追溯"为什么认为它最新"
    go_live_date: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_version: Mapped[str] = mapped_column(String(32), default="", comment="命中最新版的项目版本号（快照，来自 project.version）")
    # 证据链：参与聚合的用例 ID 列表
    source_case_ids: Mapped[list] = mapped_column(JSON, default=list)
    # 固化历史（追溯用）：[{version_id, snapshot_at, source_case_ids}]
    version_refs: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )


# ============================================================================
# 16. 功能点依赖表（fp_dependencies，v1.4）
# ============================================================================

class FPDependency(Base):
    """功能点依赖关系表。

    关系类型扩展自 docs/02 §2.4，支持 5 种关联：
    - depends_on: A 的前置条件是 B（B 完成 A 才能执行）
    - data_flows: A 的输出数据是 B 的输入
    - invariants: A 和 B 共享同一全局约束
    - lifecycle_scenarios: A 和 B 属于同一端到端流程
    - conflicts_with: A 和 B 互斥（不应同时生效）

    v2.3 新增审核流程：
    - review_status: pending（待审核）/ approved（已审核）/ rejected（已驳回）
    - 仅 approved 状态的依赖参与影响分析
    - 审核状态变更自动写入 FPDependencyHistory 表
    """
    __tablename__ = "fp_dependencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_fp_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("functional_points.id", ondelete="CASCADE"), index=True
    )
    target_fp_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("functional_points.id", ondelete="CASCADE"), index=True
    )
    relation_type: Mapped[str] = mapped_column(
        String(32), default="depends_on",
        comment="depends_on / data_flows / invariants / lifecycle_scenarios / conflicts_with"
    )
    description: Mapped[str] = mapped_column(Text, default="", comment="关系说明")
    review_status: Mapped[str] = mapped_column(
        String(16), default="pending",
        comment="v2.3 审核状态：pending（待审核）/ approved（已审核）/ rejected（已驳回）"
        # v2.45: 移除列级 index=True，改用 __table_args__ 中显式 Index 统一命名
    )
    reviewed_by: Mapped[str] = mapped_column(String(64), default="", comment="审核人")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), default="api", comment="创建人")
    # v2.45: 是否启用（项目归档时设为 False 冻结为历史版本；项目恢复活动时设为 True 重新启用）
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True,
        comment="v2.45 是否启用：项目归档时 False（冻结历史版本），恢复活动时 True"
    )
    # v2.45: 所属版本快照外键（当前版本为 null；归档后指向 fp_dependency_versions.id）
    version_pk: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("fp_dependency_versions.id", ondelete="SET NULL"),
        nullable=True,
        comment="v2.45 所属版本快照ID（当前版本为 null）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("idx_fp_dep_src_tgt", "source_fp_id", "target_fp_id"),
        Index("idx_fp_dep_review", "review_status"),
        Index("idx_fp_dep_active", "is_active"),
        # v2.45: 统一命名索引，避免列级 index=True 自动生成 ix_fp_dependencies_*
        # 与本表显式 Index 并存造成冗余；与 database.py SEARCH_INDEXES 命名一致
        Index("idx_fp_dep_version", "version_pk"),
        UniqueConstraint("source_fp_id", "target_fp_id", "relation_type", name="uq_fp_dep_edge"),
    )


class FPDependencyHistory(Base):
    """功能点依赖关系变动历史表（v2.3 新增）。

    记录每次依赖关系的创建、审核、修改、删除操作，
    关联显示对应需求变更导致的依赖关系变化，支持历史版本回溯查看。
    """
    __tablename__ = "fp_dependency_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dependency_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("fp_dependencies.id", ondelete="SET NULL"),
        nullable=True, index=True, comment="关联的依赖ID（删除后置空）"
    )
    source_fp_id: Mapped[str] = mapped_column(String(64), index=True, comment="源功能点ID")
    target_fp_id: Mapped[str] = mapped_column(String(64), index=True, comment="目标功能点ID")
    relation_type: Mapped[str] = mapped_column(String(32), comment="关系类型")
    description: Mapped[str] = mapped_column(Text, default="", comment="关系说明")
    action: Mapped[str] = mapped_column(
        String(16),
        comment="操作类型：created / approved / rejected / modified / deleted"
    )
    old_review_status: Mapped[str] = mapped_column(String(16), default="", comment="变更前审核状态")
    new_review_status: Mapped[str] = mapped_column(String(16), default="", comment="变更后审核状态")
    # v2.45: 追踪 is_active / version_pk 变更（项目归档冻结/恢复活动解冻时的审计）
    old_is_active: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="变更前是否启用")
    new_is_active: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="变更后是否启用")
    old_version_pk: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="变更前版本快照ID")
    new_version_pk: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="变更后版本快照ID")
    changed_by: Mapped[str] = mapped_column(String(64), default="api", comment="操作人")
    change_reason: Mapped[str] = mapped_column(Text, default="", comment="变更原因（关联需求变更说明）")
    related_req_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
        comment="关联需求ID（如因需求变更触发的依赖调整）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    __table_args__ = (
        Index("idx_fp_dephist_fp", "source_fp_id", "target_fp_id"),
        Index("idx_fp_dephist_action", "action"),
    )


# ============================================================================
# 17.5 功能点依赖版本快照表（fp_dependency_versions，v2.45 新增）
# ============================================================================

class FPDependencyVersion(Base):
    """功能点依赖关系版本快照表（v2.45 新增）。

    项目归档时生成一个版本快照，记录该项目下所有功能点依赖关系。
    项目恢复活动时，对应版本 is_active 设为 False，不再参与新需求影响分析。

    版本号格式：{project.version}-{project.name}，如 "1.0-用户管理系统"。
    同一项目重新归档时，版本号后缀递增：1.0-用户管理系统-v2。
    """
    __tablename__ = "fp_dependency_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_id: Mapped[str] = mapped_column(
        String(128), unique=True, index=True,
        comment="版本号：{project.version}-{project.name}，重新归档时后缀递增 -v2/-v3"
    )
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), index=True,
        comment="关联项目ID"
    )
    project_version: Mapped[str] = mapped_column(
        String(32), comment="项目版本号快照（如 1.0）"
    )
    project_name: Mapped[str] = mapped_column(
        String(255), comment="项目名称快照"
    )
    go_live_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="上线时间快照（从 Project.go_live_date 同步）"
    )
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        comment="快照时间"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, index=True,
        comment="是否启用：项目恢复活动时设为 False，新需求影响分析不再考虑该版本"
    )
    archived_by: Mapped[str] = mapped_column(
        String(64), default="", comment="归档操作人"
    )
    requirement_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="归档需求数量"
    )
    fp_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="归档功能点数量"
    )
    dependency_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="快照依赖关系数量"
    )
    change_summary: Mapped[str] = mapped_column(
        Text, default="", comment="变更说明（可选）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        Index("idx_fp_depver_project", "project_id", "is_active"),
    )


# ============================================================================
# 17.6 功能点依赖版本快照明细表（fp_dependency_version_items，v2.45 新增）
# ============================================================================

class FPDependencyVersionItem(Base):
    """功能点依赖关系版本快照明细表（v2.45 新增）。

    存储每个版本快照下的具体依赖关系明细，用于版本对比和历史回溯。
    """
    __tablename__ = "fp_dependency_version_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_pk: Mapped[int] = mapped_column(
        Integer, ForeignKey("fp_dependency_versions.id", ondelete="CASCADE"),
        index=True, comment="版本快照外键"
    )
    source_fp_id: Mapped[str] = mapped_column(
        String(64), index=True, comment="源功能点ID"
    )
    source_fp_name: Mapped[str] = mapped_column(
        String(255), comment="源功能点名称（快照）"
    )
    target_fp_id: Mapped[str] = mapped_column(
        String(64), index=True, comment="目标功能点ID"
    )
    target_fp_name: Mapped[str] = mapped_column(
        String(255), comment="目标功能点名称（快照）"
    )
    relation_type: Mapped[str] = mapped_column(
        String(32), comment="关系类型：depends_on / data_flows / invariants / lifecycle_scenarios / conflicts_with"
    )
    description: Mapped[str] = mapped_column(
        Text, default="", comment="关系说明（快照）"
    )
    review_status: Mapped[str] = mapped_column(
        String(16), comment="快照时审核状态：pending / approved / rejected"
    )
    source_req_id: Mapped[str] = mapped_column(
        String(64), index=True, comment="源FP所属需求ID（跨需求追溯）"
    )
    source_req_title: Mapped[str] = mapped_column(
        String(512), default="", comment="源FP所属需求标题（快照）"
    )
    # v2.45: 补充 target 侧需求字段，支持跨需求双向追溯（快照表，FP 删除后仍可追溯）
    target_req_id: Mapped[str] = mapped_column(
        String(64), index=True, comment="目标FP所属需求ID（跨需求追溯）"
    )
    target_req_title: Mapped[str] = mapped_column(
        String(512), default="", comment="目标FP所属需求标题（快照）"
    )

    __table_args__ = (
        Index("idx_fp_depitem_version", "version_pk"),
        Index("idx_fp_depitem_fp", "source_fp_id", "target_fp_id"),
    )


# ============================================================================
# 17. LLM 调用日志表（llm_call_logs，记录每次 LLM 调用的完整输入输出）
# ============================================================================

class LLMCallLog(Base):
    """LLM 调用日志表。

    记录每次 LLM 调用的完整 system_prompt / user_prompt / response / parsed_json，
    用于排查 Agent 输出问题、审计 LLM 行为、统计 token 消耗。

    写入由 llm_client._log_call 触发，使用独立 DB session，不阻断主流程。
    """
    __tablename__ = "llm_call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_name: Mapped[str] = mapped_column(
        String(64), default="unknown", index=True,
        comment="调用方 Agent 名称（ReqAgent/ClarifyAgent/GenAgent/ReviewAgent/FpAgent 等）",
    )
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True, comment="关联的异步任务 ID")
    req_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True, comment="关联的需求 ID")
    provider: Mapped[str] = mapped_column(String(32), default="", comment="LLM 提供商（openai/azure/anthropic/deepseek 等）")
    model: Mapped[str] = mapped_column(String(64), default="", comment="模型名称")
    system_prompt: Mapped[str] = mapped_column(Text, default="", comment="系统提示词")
    user_prompt: Mapped[str] = mapped_column(Text, default="", comment="用户提示词")
    response: Mapped[str] = mapped_column(Text, default="", comment="LLM 返回的完整响应")
    parsed_json: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="解析后的 JSON（如 call_json 成功）")
    temperature: Mapped[float] = mapped_column(Float, default=0.3, comment="温度参数")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, comment="调用耗时（毫秒）")
    token_usage: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="token 使用量（prompt/completion/total_tokens）")
    success: Mapped[bool] = mapped_column(Boolean, default=True, index=True, comment="是否成功")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败时的错误信息")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    __table_args__ = (
        Index("idx_llmlog_agent_created", "agent_name", "created_at"),
        Index("idx_llmlog_req_created", "req_id", "created_at"),
    )


# ============================================================================
# 18. 用户操作日志表（operation_logs，记录用户关键操作）
# ============================================================================

class OperationLog(Base):
    """用户操作日志表。

    记录用户的关键操作（登录/上传/澄清/重生成/删除/归档/FP 合并复用/提示词更新等），
    用于审计与问题排查。写入由 operation_logger.log_operation 触发，不阻断主流程。
    """
    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True, comment="操作用户 ID")
    username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True, comment="操作用户名")
    action: Mapped[str] = mapped_column(
        String(64), index=True,
        comment="操作类型（login/change_password/upload_requirement/clarify/regenerate/generate_testcase/delete_requirement/archive/fp_merge/fp_reuse/fp_delete/prompt_update 等）",
    )
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="操作目标类型（requirement/fp/testcase/prompt/product）")
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True, comment="操作目标 ID")
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="操作详情（修改前后值、请求参数摘要）")
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="客户端 IP")
    success: Mapped[bool] = mapped_column(Boolean, default=True, index=True, comment="是否成功")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败时的错误信息")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    __table_args__ = (
        Index("idx_oplog_action_created", "action", "created_at"),
        Index("idx_oplog_user_created", "username", "created_at"),
    )


# ============================================================================
# 19. 评审报告表（review_reports，v3.1.3 评审报告中心）
# ============================================================================

class ReviewReportRecord(Base):
    """评审报告表（v3.1.3 新增，评审报告中心 P1）。

    持久化两类评审报告，改变此前报告仅随 SSE 事件"飘过"不落库的问题：
    - report_type="srr"：需求分析阶段 QualityGate 质量门禁报告
    - report_type="case"：用例生成阶段 ReviewAgent 6 维度审核报告

    设计要点：
    - dimension_scores：维度级置信度 JSON，人工审核时给出各维度参考方向。
      结构 {dim: {"score": float, "issue_count": int}}
    - issues：问题列表 JSON，每项含稳定 issue_id 与 adoption_status 字段，
      预留 P2 逐条采纳（pending/accepted/rejected）与 P3 多报告聚合整合
      生成新版提示词的消费入口
    - summary：原始报告全文（gate_dict / review report dict），详情页渲染用
    """
    __tablename__ = "review_reports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="报告 ID: RR-{时间戳}-{随机}")
    report_type: Mapped[str] = mapped_column(String(8), index=True, comment="报告类型: srr / case")
    req_id: Mapped[str] = mapped_column(String(64), index=True, comment="关联需求 ID")
    product_id: Mapped[str] = mapped_column(String(64), index=True, comment="产品隔离")
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True, comment="关联生成任务 ID")
    req_title: Mapped[str] = mapped_column(String(512), default="", comment="需求标题快照（列表展示用）")
    gen_batch_id: Mapped[str] = mapped_column(String(64), default="", comment="关联生成批次（case 报告）")
    overall_confidence: Mapped[float] = mapped_column(Float, default=0.0, comment="总体置信度（case=avg_confidence / srr=overall_score）")
    passed: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否通过（case=无 blocker 且不补生成 / srr=gate passed）")
    issue_count: Mapped[int] = mapped_column(Integer, default=0, comment="问题总数")
    dimension_scores: Mapped[dict] = mapped_column(JSON, default=dict, comment="维度级置信度 {dim: {score, issue_count}}")
    issues: Mapped[list] = mapped_column(JSON, default=list, comment="问题列表，含 adoption_status 预留采纳状态")
    summary: Mapped[dict] = mapped_column(JSON, default=dict, comment="原始报告全文")
    created_by: Mapped[str] = mapped_column(String(64), default="pipeline", comment="来源: pipeline / backfill / api")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    __table_args__ = (
        Index("idx_rreport_product_type", "product_id", "report_type", "created_at"),
        Index("idx_rreport_req", "req_id", "created_at"),
    )


# ============================================================================
# 20. 提示词整合记录表（prompt_integrations，v3.1.3 评审报告中心 P3）
# ============================================================================

class PromptIntegrationRecord(Base):
    """提示词整合记录表（v3.1.3 P3 新增）。

    记录每次"多报告已采纳意见 + 人工意见 → LLM 整合 → 提示词新版本"操作：
    - 消费来源：report_ids + accepted_issues（哪些报告的哪些意见被整合）
    - 整合产物：target_prompt_id 指向 prompt_overrides，prompt_history_id
      指向本次写入产生的 prompt_history 记录（回退锚点，配合
      POST /api/admin/prompts/{module}/{name}/restore/{history_id} 使用）
    - 增量补丁式设计：prompt 主体不动，只更新末尾"历史评审沉淀规则"区段
      （见 app/core/prompt_evolution.py 的标记协议）
    """
    __tablename__ = "prompt_integrations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="整合 ID: PI-{时间戳}-{随机}")
    product_id: Mapped[str] = mapped_column(String(64), index=True, comment="产品隔离")
    report_ids: Mapped[list] = mapped_column(JSON, default=list, comment="本次消费的评审报告 ID 列表")
    report_types: Mapped[list] = mapped_column(JSON, default=list, comment="报告类型快照 [srr/case]")
    target_prompt_id: Mapped[str] = mapped_column(String(128), comment="目标提示词 ID（module.name）")
    accepted_count: Mapped[int] = mapped_column(Integer, default=0, comment="消费的已采纳意见数")
    accepted_issues: Mapped[list] = mapped_column(JSON, default=list, comment="消费的意见摘要列表")
    manual_notes: Mapped[str] = mapped_column(Text, default="", comment="人工补充意见")
    integration_summary: Mapped[str] = mapped_column(Text, default="", comment="LLM 生成的整合摘要")
    prompt_history_id: Mapped[int] = mapped_column(Integer, default=0, comment="本次写入产生的 prompt_history 记录 ID（回退锚点）")
    # P2-5 候选区（书 6.10.4/8.3）：进化产物先进候选区，回归确认后才激活落 override
    candidate_prompt: Mapped[str] = mapped_column(Text, default="", comment="候选提示词全文（status=pending 时非空）")
    status: Mapped[str] = mapped_column(String(16), default="activated", comment="pending=候选待激活 / activated=已激活 / discarded=已废弃")
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="激活时间")
    activated_by: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="激活操作人")
    created_by: Mapped[str] = mapped_column(String(64), default="api", comment="操作人")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    __table_args__ = (
        Index("idx_pinteg_product", "product_id", "created_at"),
        Index("idx_pinteg_target", "target_prompt_id", "created_at"),
    )


# ============================================================================
# 20. 评估运行表（evaluation_runs，v3.4 / docs/15 §4.5 P0-5）
# ============================================================================


class EvaluationRun(Base):
    """评估运行记录（服务器端证据）。

    为什么必须有这张表：此前"候选提示词激活"只校验客户端提交的 `confirmed_eval: bool`，
    等于自证清白——任何调用方都能把未经回归的提示词推上生产。
    现在激活必须引用一条**服务器端 passed** 的评估运行，且其 `prompt_hash` 必须
    与被激活的候选文本逐字一致（防止"拿旧评估顶替新候选"）。

    记录内容（docs/15 §4.5 建议）：dataset hash、prompt hash、model/provider、
    schema version、指标、artifact URI —— 便于事后复盘"这条提示词当时凭什么上线"。
    """
    __tablename__ = "evaluation_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="评估 ID: EV-{时间戳}-{随机}")
    kind: Mapped[str] = mapped_column(String(32), index=True, comment="评估类型: prompt_candidate / retrieval / ...")
    target: Mapped[str] = mapped_column(String(128), index=True, comment="被评估对象（如 prompts.gen_agent）")
    status: Mapped[str] = mapped_column(String(16), index=True, comment="passed / failed / error")
    dataset_name: Mapped[str] = mapped_column(String(128), default="", comment="数据集名称")
    dataset_hash: Mapped[str] = mapped_column(String(64), default="", comment="数据集内容 hash（样本+期望）")
    prompt_hash: Mapped[str] = mapped_column(String(64), index=True, comment="被测提示词文本 hash（激活时逐字校验）")
    sample_count: Mapped[int] = mapped_column(Integer, default=0, comment="样本数")
    passed_count: Mapped[int] = mapped_column(Integer, default=0, comment="达标样本数")
    threshold: Mapped[float] = mapped_column(Float, default=0.0, comment="通过阈值（达标率）")
    score: Mapped[float] = mapped_column(Float, default=0.0, comment="本次得分（达标率）")
    model: Mapped[str] = mapped_column(String(128), default="", comment="评估所用模型")
    provider: Mapped[str] = mapped_column(String(64), default="", comment="供应商")
    schema_version: Mapped[str] = mapped_column(String(32), default="", comment="输出/评分口径版本")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict, comment="指标明细（含逐样本结果）")
    artifact_uri: Mapped[str] = mapped_column(String(512), default="", comment="产物路径（可复现）")
    notes: Mapped[str] = mapped_column(Text, default="", comment="备注 / 失败原因")
    used_for_integration_id: Mapped[str] = mapped_column(String(64), default="", comment="被哪条候选激活消费（回填）")
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="被消费时间")
    created_by: Mapped[str] = mapped_column(String(64), default="pipeline", comment="来源: pipeline / cli / api")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    __table_args__ = (
        Index("idx_evalrun_target", "target", "status", "created_at"),
    )

# ============================================================================
# 21. 向量库写入出箱（vector_outbox，v3.4 / docs/15 §4.4）
# ============================================================================


class VectorOutbox(Base):
    """Transactional Outbox：PG 业务行与"要写进 Milvus 的意图"同事务提交。

    为什么需要（审查证据）：
      - Review 回流里 PG 记录与 Milvus 向量分属两套系统、不在同一事务：
        Milvus 写甚至发生在 PG 最终 commit **之前**，失败时 PG 有、向量没有；
      - TestCase 没有类似 Requirement 的 vectorization_status，"没写成功"无处记录；
      - 审批/驳回（feedback）同样是 PG + Milvus 非原子双写。

    语义：
      - 业务事务内只写 TestCase + Traceability + Outbox（本表），**commit 后**由 relay
        至少一次投递；Milvus 侧按主键幂等 upsert/delete，故重复投递不产生重复副作用；
      - `revision` + `content_hash` 随 metadata 写进 Milvus，对账时能识别"陈旧向量"；
      - `status`：pending（待投递）/ done（已投递）/ failed（超过重试上限，等对账或人工）。
    """
    __tablename__ = "vector_outbox"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="事件 ID: VO-{时间戳}-{随机}")
    aggregate_type: Mapped[str] = mapped_column(String(32), index=True, comment="聚合类型: test_case / requirement")
    aggregate_id: Mapped[str] = mapped_column(String(64), index=True, comment="聚合 ID（用例 ID / 需求 ID）")
    action: Mapped[str] = mapped_column(String(16), comment="upsert / delete")
    # revision 用"变更时刻的 epoch 毫秒"做单调标记（~1.8e12）→ 必须 BIGINT，int32 会溢出
    revision: Mapped[int] = mapped_column(BigInteger, default=1, comment="PG 侧修订号（epoch 毫秒，单调递增）")
    content_hash: Mapped[str] = mapped_column(String(64), default="", comment="内容 hash（识别陈旧向量）")
    payload: Mapped[dict] = mapped_column(JSON, default=dict, comment="投递所需数据（case_json / metadata）")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True, comment="pending / done / failed")
    retry_count: Mapped[int] = mapped_column(Integer, default=0, comment="已重试次数")
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="下次可投递时间（退避）"
    )
    error: Mapped[str] = mapped_column(Text, default="", comment="最近一次失败原因")
    created_by: Mapped[str] = mapped_column(String(64), default="pipeline", comment="来源: pipeline / api / reconcile")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="投递完成时间")
    # v4.1（缺陷 B / T3）：认领租约——认领后置 delivering + lease_until，投递在事务外
    # 并行执行；中途崩溃由租约过期回收（Milvus 幂等，重复投递无副作用）。
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="本行被 relay 认领（置 delivering）的时间"
    )
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="delivering 租约到期时间（过期可被重新认领）"
    )

    __table_args__ = (
        Index("idx_outbox_dispatch", "status", "next_retry_at", "created_at"),
        Index("idx_outbox_aggregate", "aggregate_type", "aggregate_id", "created_at"),
    )


# ============================================================================
# 27. 去重留痕表（dedup_removals，v3.5.10 docs/32 B-6 闸门3）
# ============================================================================

class DedupRemoval(Base):
    """去重留痕表：被去重剔除/保留判定的可审计记录（docs/32 B-6 第 4 件套）。

    背景（docs/29 P0）：语义去重误删不可逆且历史发生率 25-34%——被删用例直接
    从内存消失，日志/SSE 里的 `semantic_dedup_removed` 只活到页面刷新。本表把
    判定落库，让"误删"从不可逆事故降级为可纠错操作：
    - decision="removed"：被合并剔除（removed_case_json 存完整用例，供一键恢复）；
    - decision="kept"：闸门1 护栏拦截 / 闸门2 LLM 裁决非重复（reason 带命中维度
      或 llm_distinct，审计"为什么没删"）。

    恢复语义：restore 接口把 removed_case_json 写回 test_cases（新 ID 或原 ID）
    并置 restored=True，与 error_cases 复活同型。
    """
    __tablename__ = "dedup_removals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    removal_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, comment="留痕 ID: DR-{时间戳}-{随机}")
    task_id: Mapped[str] = mapped_column(String(64), default="", index=True, comment="关联生成任务 ID")
    gen_batch_id: Mapped[str] = mapped_column(String(64), default="", index=True, comment="生成批次 ID")
    req_id: Mapped[str] = mapped_column(String(64), default="", index=True, comment="关联需求 ID")
    decision: Mapped[str] = mapped_column(String(16), default="removed", comment="removed（剔除）/ kept（保留判定）")
    removed_id: Mapped[str] = mapped_column(String(64), default="", comment="被去重用例 ID（或标题快照）")
    removed_title: Mapped[str] = mapped_column(String(256), default="", comment="被去重用例标题快照")
    kept_id: Mapped[str] = mapped_column(String(64), default="", comment="保留代表用例 ID（或标题快照）")
    kept_title: Mapped[str] = mapped_column(String(256), default="", comment="保留代表用例标题快照")
    reason: Mapped[str] = mapped_column(String(512), default="", comment="判定依据（相似度/护栏命中维度/llm_distinct）")
    removed_case_json: Mapped[dict] = mapped_column(JSON, default=dict, comment="被去重用例完整 JSON（一键恢复数据源）")
    restored: Mapped[bool] = mapped_column(Boolean, default=False, index=True, comment="是否已恢复（误删复活）")
    restored_case_id: Mapped[str] = mapped_column(String(64), default="", comment="恢复后生成的用例 ID")
    restored_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="恢复时间"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )

    __table_args__ = (
        Index("idx_dedup_removal_task", "task_id", "decision", "created_at"),
    )
