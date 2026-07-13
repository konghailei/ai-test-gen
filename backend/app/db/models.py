"""SQLAlchemy ORM 模型。

将 templates/knowledge-base-schema.json 的表结构转为 SQLAlchemy 模型。
PostgreSQL 原生支持 JSON/JSONB/TEXT[]/TIMESTAMPTZ/GIN 索引。

设计依据：
- templates/knowledge-base-schema.json（5 张核心表）
- docs/02-knowledge-base.md:319-453（额外 3 张表：req_dependencies / traceability / req_patterns）
"""

from __future__ import annotations
from datetime import datetime, date, timezone
from sqlalchemy import (
    String, Integer, Float, Text, DateTime, Date, Boolean, ForeignKey, Index, JSON,
    UniqueConstraint,
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
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/superseded/deprecated/archived
    srr_json: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_text: Mapped[str] = mapped_column(Text, default="")
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
    go_live_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 归档时间：归档时设置，用户可手动修改。区别于 updated_at（任何字段修改都会刷新）。
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


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
    """项目/版本表。属于一个产品，一个项目下有多个需求，涉及多个模块。"""
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("product_id", "name", name="uq_project_product_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(64), ForeignKey("products.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[str] = mapped_column(String(32), default="1.0")
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
    """知识问答记录表（v2.4 新增）。

    存储用户提问、AI 回答、人工纠错及归档状态。
    conversation_history 存储多轮对话记录。
    """
    __tablename__ = "knowledge_qa"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, default="")
    reference_sources: Mapped[list] = mapped_column(JSON, default=list)
    conversation_history: Mapped[list] = mapped_column(JSON, default=list)
    corrected_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction_notes: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    archived_type: Mapped[str] = mapped_column(String(32), default="")
    archived_id: Mapped[str] = mapped_column(String(64), default="")
    # 产品隔离：问答记录归属的产品 ID（归档时用于关联到正确产品，而非硬编码 PROD-DEFAULT）
    product_id: Mapped[str] = mapped_column(String(64), default="PROD-DEFAULT", index=True)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_kqa_created_at", "created_at"),
        Index("idx_kqa_product", "product_id"),
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
# 用户与角色（v2.12 新增 - 基于角色的菜单权限管理）
# ============================================================================

DEFAULT_MENUS = [
    "dashboard", "requirements", "generate", "testcases",
    "dependency_graph", "knowledge", "knowledge_qa", "projects_modules",
    "admin", "admin_dashboard", "admin_products", "admin_prompts", "admin_settings",
    "admin_users", "admin_roles", "admin_kb",
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
    """
    __tablename__ = "prompt_overrides"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, comment="主键，格式: module.name")
    module: Mapped[str] = mapped_column(String(64), index=True, comment="模块名: req_agent/clarify_agent 等")
    name: Mapped[str] = mapped_column(String(64), comment="提示词名: SYSTEM_PROMPT 等")
    value: Mapped[str] = mapped_column(Text, comment="当前提示词值（用户自定义或默认）")
    default_value: Mapped[str] = mapped_column(Text, comment="代码中的默认值")
    description: Mapped[str] = mapped_column(Text, default="", comment="提示词描述")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )


# ============================================================================
# 14. 需求模式表（req_patterns，doc-02 §6 需求模式库）
# ============================================================================

class ReqPattern(Base):
    """需求模式库表。

    对应 docs/02-knowledge-base.md §6。
    存储同类需求的抽象维度集合，供 ClarifyAgent 遗漏检测使用。
    与 Milvus req_pats collection 配合，支持语义检索同类需求模式。
    """
    __tablename__ = "req_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, comment="模式名称")
    modules: Mapped[list] = mapped_column(JSON, default=list, comment="适用模块列表")
    required_dimensions: Mapped[list] = mapped_column(JSON, default=list, comment="该类需求的必备维度")
    common_boundary: Mapped[list] = mapped_column(JSON, default=list, comment="常见边界条件")
    risk_points: Mapped[list] = mapped_column(JSON, default=list, comment="常见风险点")
    pattern_json: Mapped[dict] = mapped_column(JSON, default=dict, comment="完整模式定义")
    usage_count: Mapped[int] = mapped_column(Integer, default=0, comment="被引用次数")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )
