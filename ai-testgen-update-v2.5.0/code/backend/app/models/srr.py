"""SRR（Structured Requirement Representation）数据模型。

将架构文档 docs/01-requirement-parsing.md §2.1 的 SRR JSON Schema
和 templates/requirement-template.json 转为 Pydantic 模型。

用途：
- ReqAgent 解析需求后输出 SRR 实例
- ClarifyAgent 接收和细化 SRR
- QualityGate 校验 SRR 完整性
- GenAgent 基于 SRR 生成测试用例

设计依据：
- docs/01-requirement-parsing.md:82-130（SRR JSON 示例）
- docs/01-requirement-parsing.md:252-270（字段校验清单 Critical/Important/Optional）
- docs/01-requirement-parsing.md:1541-1570（optimization_context 优化需求上下文）
- docs/01-requirement-parsing.md:444-460（field_conflicts 字段冲突）
"""

from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator


# ============================================================================
# 基础子模型
# ============================================================================

class Requirement(BaseModel):
    """需求基本信息。

    对应 SRR.requirement 字段，包含需求的标识和元数据。
    """
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="", description="需求唯一 ID，如 REQ-2024-001")
    product_id: str = Field(default="PROD-DEFAULT", description="产品 ID，用于按产品隔离")
    product_name: str = Field(default="", description="产品名称")
    title: str = Field(default="", description="需求标题")
    priority: Literal["P0", "P1", "P2", "P3"] = Field(default="P2", description="优先级")
    modules: list[str] = Field(default_factory=list, description="关联模块列表（支持多模块）")
    summary: str = Field(default="", description="需求概述，一句话描述核心功能")
    change_type: Literal["new", "optimized"] | None = Field(
        default=None, description="变更类型：new=新需求 / optimized=优化需求"
    )
    supersedes: str | None = Field(
        default=None, description="优化需求时，被替代的原需求 ID"
    )


class Step(BaseModel):
    """流程步骤。

    对应 SRR.flows.happy_path.steps[] 和 alternate_flows[].steps[]。
    """
    model_config = ConfigDict(extra="ignore")

    order: int = Field(default=0, description="步骤序号，从 1 开始")
    action: str = Field(..., description="用户操作描述")
    expect: str = Field(..., description="预期结果")


class HappyPath(BaseModel):
    """正常流程（Happy Path）。

    对应 SRR.flows.happy_path，描述需求的核心正常流程。
    """
    model_config = ConfigDict(extra="ignore")

    steps: list[Step] = Field(default_factory=list, description="流程步骤列表")


class AlternateFlow(BaseModel):
    """异常/分支流程。

    对应 SRR.flows.alternate_flows[]，描述异常或分支场景。
    name 可为空（由 trigger 自动生成）。
    """
    model_config = ConfigDict(extra="ignore")

    name: str = Field(default="", description="流程名称，如「账号不存在」")
    trigger: str = Field(default="", description="触发条件")
    expect: str = Field(default="", description="预期结果")


class Flows(BaseModel):
    """流程集合。

    对应 SRR.flows，包含正常流程和异常流程。
    """
    model_config = ConfigDict(extra="ignore")

    happy_path: HappyPath = Field(default_factory=HappyPath, description="正常流程")
    alternate_flows: list[AlternateFlow] = Field(
        default_factory=list, description="异常/分支流程列表"
    )


class BoundaryCondition(BaseModel):
    """边界条件。

    对应 SRR.boundary_conditions[]。
    """
    model_config = ConfigDict(extra="ignore")

    field: str = Field(default="", description="字段名")
    condition: str = Field(default="", description="边界条件描述")
    expect: str = Field(default="", description="预期结果")


class BusinessRule(BaseModel):
    """业务规则。

    对应功能点 SRR 的 business_rules[] 与总 SRR 的
    cross_fp_interactions.shared_business_rules[]（v2.34 起，总 SRR 顶层
    不再保留 business_rules 字段）。
    """
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="", description="规则 ID")
    rule: str = Field(default="", description="规则描述")
    type: Literal["validation", "calculation", "workflow", "permission", "security",
                   "session", "timeout", "rate_limit", "notification", "data_retention",
                   "audit", "encryption", "integration"] = Field(
        default="validation", description="规则类型"
    )


class DataConstraint(BaseModel):
    """数据约束。

    对应 SRR.data_constraints[]，定义字段的类型、必填性和约束。
    """
    model_config = ConfigDict(extra="ignore")

    field: str = Field(default="", description="字段名")
    type: str = Field(default="string", description="字段类型，如 string / integer / date")
    required: bool = Field(default=False, description="是否必填")
    constraints: str = Field(default="", description="约束描述，如「长度 6-20」「>0」")
    example: str = Field(default="", description="示例值")


class NonFunctional(BaseModel):
    """非功能性需求。

    对应 SRR.non_functional，包含性能、安全等非功能性要求。
    """
    model_config = ConfigDict(extra="ignore")

    performance: str = Field(default="", description="性能要求")
    security: str = Field(default="", description="安全要求")
    compatibility: str = Field(default="", description="兼容性要求")
    accessibility: str = Field(default="", description="可访问性要求")


class Relationship(BaseModel):
    """需求关联关系。

    对应 SRR.relationships[]，描述与其他需求的依赖/冲突关系。
    """
    model_config = ConfigDict(extra="ignore")

    target: str = Field(default="", description="目标需求 ID")
    type: Literal["depends_on", "related_to", "conflicts", "replaces"] = Field(
        default="related_to", description="关系类型"
    )
    strength: Literal["strong", "weak"] = Field(default="weak", description="关系强度")
    note: str = Field(default="", description="关系说明")


class TestPoint(BaseModel):
    """测试点提示。

    对应 SRR.test_points_hint[]，由 ReqAgent 提取，指导 GenAgent 生成。
    """
    model_config = ConfigDict(extra="ignore")

    category: Literal["functional", "boundary", "exception", "security", "performance"] = Field(
        default="functional", description="测试点类别"
    )
    point: str = Field(default="", description="测试关注点描述")


# ============================================================================
# v2.1 新增字段模型（丰富需求映射完整性）
# ============================================================================

class AcceptanceCriterion(BaseModel):
    """验收标准。

    对应 SRR.acceptance_criteria[]，描述需求必须满足的验收条件。
    """
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="", description="验收标准 ID，如 AC-01")
    description: str = Field(default="", description="验收条件描述")
    type: Literal["functional", "performance", "security", "usability",
                  "compatibility", "reliability"] = Field(
        default="functional", description="验收类型"
    )


class ApiDefinition(BaseModel):
    """接口定义。

    对应 SRR.api_definitions[]，描述需求涉及的 API 接口。
    """
    model_config = ConfigDict(extra="ignore")

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = Field(
        default="GET", description="HTTP 方法"
    )
    path: str = Field(default="", description="接口路径")
    summary: str = Field(default="", description="接口功能描述")
    request_body: str = Field(default="", description="请求体结构描述")
    response_body: str = Field(default="", description="响应体结构描述")
    error_responses: list[str] = Field(default_factory=list, description="错误响应描述")


class UserRole(BaseModel):
    """用户角色。

    对应 SRR.user_roles[]，描述使用该需求的用户角色及其权限。
    """
    model_config = ConfigDict(extra="ignore")

    role: str = Field(default="", description="角色名")
    description: str = Field(default="", description="角色描述")
    permissions: list[str] = Field(default_factory=list, description="权限列表")


class ExternalDependency(BaseModel):
    """外部依赖。

    对应 SRR.dependencies[]，描述需求依赖的外部系统或服务。
    """
    model_config = ConfigDict(extra="ignore")

    name: str = Field(default="", description="依赖名称，如「短信服务」")
    type: str = Field(default="", description="依赖类型，如 API/DB/第三方服务")
    description: str = Field(default="", description="依赖说明")
    impact_if_unavailable: str = Field(default="", description="不可用时的业务影响")


class UiRule(BaseModel):
    """界面/交互规则。

    对应 SRR.ui_rules[]，描述 UI 行为、交互逻辑、展示规则。
    """
    model_config = ConfigDict(extra="ignore")

    element: str = Field(default="", description="UI 元素，如「登录按钮」")
    behavior: str = Field(default="", description="行为描述，如「频繁点击限流」")
    condition: str = Field(default="", description="触发条件")


class DataModel(BaseModel):
    """数据模型。

    对应 SRR.data_model，描述核心业务实体及其关系。
    """
    model_config = ConfigDict(extra="ignore")

    entity_name: str = Field(default="", description="实体名")
    description: str = Field(default="", description="实体描述")
    fields: list[DataConstraint] = Field(default_factory=list, description="实体的字段定义")
    relationships: list[str] = Field(default_factory=list, description="与其他实体的关系")


class Assumption(BaseModel):
    """假设与约束。

    对应 SRR.assumptions[]，记录需求分析过程中的假设条件和约束。
    """
    model_config = ConfigDict(extra="ignore")

    content: str = Field(default="", description="假设或约束内容")
    type: Literal["assumption", "constraint", "out_of_scope"] = Field(
        default="assumption", description="类型"
    )


class FPSummary(BaseModel):
    """v2.22: 功能点概要（总 SRR 中的 FP 全景视图）。

    对应 SRR.fp_summary[]，仅记录功能点的概要信息，
    不展开内部细节（内部细节由功能点 SRR sub_srr 承载）。
    后端基于实际 FunctionalPoint 列表生成，不依赖 LLM 输出。
    """
    model_config = ConfigDict(extra="ignore")

    name: str = Field(default="", description="功能点名称")
    fp_type: Literal[
        "workflow", "constraint", "calculation",
        "api_behavior", "data_record", "config",
    ] = Field(default="workflow", description="功能点类型")
    priority: Literal["P0", "P1", "P2", "P3"] = Field(default="P2", description="优先级")
    summary: str = Field(default="", description="功能点一句话概要")
    module: str = Field(default="", description="所属模块")


# ============================================================================
# 字段冲突模型（KB 补全时产生，docs/01 §4.4.1）
# ============================================================================

class FieldConflict(BaseModel):
    """字段冲突记录。

    当 KB 自动补全的值与用户原文冲突时，记录冲突供人工解决。
    对应 docs/01-requirement-parsing.md §4.4.1。
    """
    field: str = Field(..., description="冲突字段路径，如 data_constraints.timeout")
    existing_value: object = Field(..., description="已有值（用户原文）")
    existing_source: str = Field(..., description="已有值来源：USER_INPUT / HUMAN_ANSWERED / KB_INFERRED")
    suggested_value: object = Field(..., description="KB 建议值")
    suggested_source: str = Field(default="KB_INFERRED", description="建议值来源")
    kb_similarity: float = Field(default=0.0, description="KB 检索相似度")
    kb_source_req_id: str = Field(default="", description="KB 来源需求 ID")
    resolution: Literal["pending_human", "resolved_keep", "resolved_replace"] = Field(
        default="pending_human", description="解决状态"
    )


# ============================================================================
# 优化需求上下文（docs/01 §7）
# ============================================================================

class OriginalRequirement(BaseModel):
    """原始需求引用（优化需求时）。"""
    id: str = Field(..., description="原始需求 ID")
    title: str = Field(default="", description="原始需求标题")
    version: int = Field(default=1, description="原始需求版本")
    srr_json: dict | None = Field(default=None, description="原始 SRR（可选）")


class OriginalTestCase(BaseModel):
    """原始测试用例引用（优化需求时）。"""
    id: str = Field(..., description="原始用例 ID")
    title: str = Field(default="", description="原始用例标题")
    type: str = Field(default="functional", description="原始用例类型")
    status: str = Field(default="active", description="原始用例状态")


class Diff(BaseModel):
    """需求变更差异。"""
    added: list[str] = Field(default_factory=list, description="新增内容")
    removed: list[str] = Field(default_factory=list, description="删除内容")
    modified: list[str] = Field(default_factory=list, description="修改内容")


class GenerationGuidance(BaseModel):
    """用例生成指导（优化需求时）。"""
    must_update_cases: list[str] = Field(default_factory=list, description="必须更新的用例 ID")
    must_add_cases: list[str] = Field(default_factory=list, description="必须新增的用例类型")
    can_delete_cases: list[str] = Field(default_factory=list, description="可删除的用例 ID")
    regression_scope: list[str] = Field(default_factory=list, description="回归测试范围")


class OptimizationContext(BaseModel):
    """优化需求上下文。

    当需求为优化/修改/升级需求时，携带原始需求和变更信息。
    对应 docs/01-requirement-parsing.md §7。
    """
    original_requirement: OriginalRequirement
    original_test_cases: list[OriginalTestCase] = Field(default_factory=list)
    diff: Diff = Field(default_factory=Diff)
    generation_guidance: GenerationGuidance | None = Field(default=None)


# ============================================================================
# v2.32: 跨功能点交互规则（总 SRR 核心职责，业界最佳实践）
# ============================================================================

class CrossFpFlow(BaseModel):
    """跨功能点流程交互。

    描述两个或多个功能点之间的调用顺序、数据传递契约。
    """
    model_config = ConfigDict(extra="ignore")

    name: str = Field(default="", description="交互名称")
    source_fp: str = Field(default="", description="发起方功能点名称")
    target_fp: str = Field(default="", description="接收方功能点名称")
    interaction_type: Literal["sequence", "data_flow", "event", "dependency"] = Field(
        default="sequence", description="交互类型：顺序调用/数据流/事件/依赖"
    )
    description: str = Field(default="", description="交互描述：调用时机、传递数据、返回结果")


class SharedBusinessRule(BaseModel):
    """跨功能点共享业务规则。

    同时适用于 ≥2 个功能点的规则（如全局权限模型、统一编码规则）。
    """
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default="", description="规则 ID")
    rule: str = Field(default="", description="规则描述")
    applied_fps: list[str] = Field(default_factory=list, description="适用的功能点名称列表")


class FpContract(BaseModel):
    """功能点契约。

    功能点之间的输入/输出契约（A 的输出是 B 的输入）。
    """
    model_config = ConfigDict(extra="ignore")

    provider_fp: str = Field(default="", description="提供方功能点")
    consumer_fp: str = Field(default="", description="消费方功能点")
    data_fields: list[str] = Field(default_factory=list, description="传递的数据字段")
    contract_desc: str = Field(default="", description="契约描述：数据格式、约束、时机")


class CrossFpInteractions(BaseModel):
    """跨功能点交互规则（总 SRR 核心字段）。

    v2.32: 总 SRR 的核心职责——描述功能点之间的交互规则，
    而非功能点内部细节（内部细节由功能点 SRR 承载）。
    对应业界最佳实践：IEEE 830 §2 产品视角、ISO 29148 SyRS 层级。
    """
    model_config = ConfigDict(extra="ignore")

    cross_fp_flows: list[CrossFpFlow] = Field(
        default_factory=list, description="跨 FP 流程交互列表"
    )
    shared_business_rules: list[SharedBusinessRule] = Field(
        default_factory=list, description="跨 FP 共享业务规则"
    )
    fp_contracts: list[FpContract] = Field(
        default_factory=list, description="FP 之间的输入/输出契约"
    )
    global_constraints: list[str] = Field(
        default_factory=list, description="全局约束（跨 FP 适用的强制约束）"
    )


# ============================================================================
# v2.32: 历史数据影响分析（优化需求独立章节，参考 Volere §22）
# ============================================================================

class LegacyDataImpact(BaseModel):
    """历史数据影响分析。

    v2.32: 优化/改进类需求必须包含此字段，描述对已有历史数据的影响。
    作为总 SRR 的独立章节（参考 Volere §22 迁移到新产品）。
    v2.40: 基于业界数据迁移影响分析框架（DZone/SAP/Volere）精简字段，
    从 10 个子字段合并为 4 个核心维度 + 2 个元数据字段：
      - 影响范围（impact_scope）：合并原 data_inventory
      - 迁移方案（migration_plan）：合并原 migration_strategy + field_mapping + compatibility_analysis
      - 验证与回滚（rollback_and_validation）：合并原 rollback_strategy + verification_method
      - 风险与缓解（risks）：独立保留
      - 元数据：is_optimization（标识位）、user_clarification（用户原始输入）
    旧数据中的 field_mapping/compatibility_analysis 等字段通过 validator 自动迁移到新字段。
    """
    model_config = ConfigDict(extra="ignore")

    # ---- 元数据 ----
    is_optimization: bool = Field(default=False, description="是否为优化需求")
    user_clarification: str = Field(
        default="",
        description="用户在澄清环节填写的具体影响范围（原始输入，LLM 据此生成完整分析）",
    )

    # ---- 核心维度（用户编辑的 4 个字段）----
    impact_scope: str = Field(
        default="",
        description="影响范围：受影响的功能点、数据实体（主数据/在途数据/历史数据/日志）、上下游系统、用户角色",
    )
    migration_plan: str = Field(
        default="",
        description="迁移方案：含迁移策略（全量/增量/双写/停机）、字段映射（老→新）、兼容性分析（格式/编码/关联完整性）",
    )
    rollback_and_validation: str = Field(
        default="",
        description="验证与回滚：回滚策略（备份/回滚窗口/触发条件）+ 验证方法（数量校验/业务关联校验/并行比对）",
    )
    risks: list[str] = Field(
        default_factory=list,
        description="风险与缓解：业务逻辑丢失、数据不一致、停机超时等风险及应对",
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data: Any) -> Any:
        """v2.40: 旧数据兼容——把已废弃的子字段内容合并到新字段。

        旧 schema 有 10 个字段，新 schema 只有 6 个。为避免数据丢失，
        旧数据中的 migration_strategy/field_mapping/compatibility_analysis
        合并到 migration_plan；rollback_strategy/verification_method 合并到
        rollback_and_validation；data_inventory 合并到 impact_scope。
        仅当新字段为空且旧字段有内容时才合并。
        """
        if not isinstance(data, dict):
            return data

        # 迁移：旧 migration_strategy + field_mapping + compatibility_analysis → migration_plan
        if not data.get("migration_plan"):
            parts: list[str] = []
            old_strategy = data.get("migration_strategy", "")
            if old_strategy and isinstance(old_strategy, str) and old_strategy.strip():
                parts.append(f"【迁移策略】{old_strategy.strip()}")
            old_mapping = data.get("field_mapping", [])
            if isinstance(old_mapping, list) and old_mapping:
                mapping_text = "; ".join(str(m) for m in old_mapping if m)
                if mapping_text:
                    parts.append(f"【字段映射】{mapping_text}")
            old_compat = data.get("compatibility_analysis", "")
            if old_compat and isinstance(old_compat, str) and old_compat.strip():
                parts.append(f"【兼容性分析】{old_compat.strip()}")
            if parts:
                data["migration_plan"] = "\n".join(parts)

        # 迁移：旧 rollback_strategy + verification_method → rollback_and_validation
        if not data.get("rollback_and_validation"):
            parts: list[str] = []
            old_rollback = data.get("rollback_strategy", "")
            if old_rollback and isinstance(old_rollback, str) and old_rollback.strip():
                parts.append(f"【回滚策略】{old_rollback.strip()}")
            old_verify = data.get("verification_method", "")
            if old_verify and isinstance(old_verify, str) and old_verify.strip():
                parts.append(f"【验证方法】{old_verify.strip()}")
            if parts:
                data["rollback_and_validation"] = "\n".join(parts)

        # 迁移：旧 data_inventory → impact_scope（追加到末尾）
        old_inventory = data.get("data_inventory", [])
        if isinstance(old_inventory, list) and old_inventory:
            inventory_text = "; ".join(str(i) for i in old_inventory if i)
            if inventory_text:
                existing_scope = data.get("impact_scope", "") or ""
                if inventory_text not in existing_scope:
                    data["impact_scope"] = (
                        f"{existing_scope}\n【数据盘点】{inventory_text}".strip()
                        if existing_scope
                        else f"【数据盘点】{inventory_text}"
                    )

        return data


# ============================================================================
# SRR 主模型
# ============================================================================

class SRR(BaseModel):
    """SRR（Structured Requirement Representation）结构化需求表示。

    这是系统的核心数据结构，贯穿需求解析→澄清→质量门禁→用例生成全流程。
    对应 docs/01-requirement-parsing.md §2.1。

    v2.32 字段重构（基于业界最佳实践 IEEE 830 / ISO 29148 / Volere）：
    - **总 SRR 移除**：boundary_conditions / data_constraints / acceptance_criteria
      （字段级内容下沉到功能点 SRR，避免重复维护与不一致）
    - **总 SRR 新增**：cross_fp_interactions（跨功能点交互规则，总 SRR 核心职责）
    - **总 SRR 新增**：legacy_data_impact（历史数据影响分析，优化需求独立章节）
    - **v2.34 移除**：business_rules 顶层字段（与 cross_fp_interactions.shared_business_rules 重复）
      跨 FP 共享规则统一由 cross_fp_interactions.shared_business_rules 承载
    - **保留全局基线**：non_functional（全局非功能基线）
    - **保留追溯**：fp_summary / relationships / dependencies / api_definitions

    字段校验级别：
    - Critical（必填）：requirement.title, requirement.summary, flows.happy_path
    - Important：preconditions, flows.alternate_flows, cross_fp_interactions
    - Optional：non_functional, relationships, legacy_data_impact, optimization_context
    """
    srr_version: str = Field(default="1.0", description="SRR 版本号")
    requirement: Requirement = Field(..., description="需求基本信息")
    # v2.22: 功能点概要（由后端基于实际 FP 列表生成，LLM 首次解析时可输出初步概要）
    fp_summary: list[FPSummary] = Field(
        default_factory=list, description="功能点概要列表（仅概要，不展开内部细节）"
    )
    preconditions: list[str] = Field(default_factory=list, description="前置条件列表")
    flows: Flows = Field(default_factory=Flows, description="流程集合")
    # v2.34: 总 SRR 不再保留 business_rules 顶层字段，跨 FP 共享规则
    # 统一写入 cross_fp_interactions.shared_business_rules（避免重复维护）
    non_functional: NonFunctional = Field(
        default_factory=NonFunctional, description="全局非功能性需求基线"
    )
    relationships: list[Relationship] = Field(
        default_factory=list, description="需求关联关系"
    )
    test_points_hint: list[TestPoint] = Field(
        default_factory=list, description="测试点提示列表"
    )
    api_definitions: list[ApiDefinition] = Field(
        default_factory=list, description="接口定义列表"
    )
    user_roles: list[UserRole] = Field(
        default_factory=list, description="用户角色列表"
    )
    dependencies: list[ExternalDependency] = Field(
        default_factory=list, description="外部依赖列表"
    )
    ui_rules: list[UiRule] = Field(
        default_factory=list, description="界面/交互规则列表"
    )
    data_model: list[DataModel] = Field(
        default_factory=list, description="数据模型列表（全局 ERD 与数据字典）"
    )
    assumptions: list[Assumption] = Field(
        default_factory=list, description="假设与约束列表"
    )
    additional_details: str = Field(
        default="", description="兜底字段：无法映射到标准字段的原文内容"
    )
    field_conflicts: list[FieldConflict] = Field(
        default_factory=list, description="字段冲突列表（KB 补全时产生）"
    )
    optimization_context: OptimizationContext | None = Field(
        default=None, description="优化需求上下文（仅优化需求时存在）"
    )
    # ========== v2.32 新增字段 ==========
    cross_fp_interactions: CrossFpInteractions = Field(
        default_factory=CrossFpInteractions,
        description="跨功能点交互规则（总 SRR 核心职责：FP 间调用顺序/共享规则/契约/全局约束）",
    )
    legacy_data_impact: LegacyDataImpact | None = Field(
        default=None,
        description="历史数据影响分析（优化需求独立章节，参考 Volere §22）",
    )
    # ===================================

    # 澄清元数据（由 ClarifyAgent 写入）
    _clarification: dict = {}


class ValidationResult(BaseModel):
    """SRR 字段校验结果。

    对应 docs/01-requirement-parsing.md §3.4 的三级校验清单。
    """
    critical_missing: list[str] = Field(default_factory=list, description="Critical 字段缺失列表")
    important_missing: list[str] = Field(default_factory=list, description="Important 字段缺失列表")
    optional_missing: list[str] = Field(default_factory=list, description="Optional 字段缺失列表")
    is_valid: bool = Field(default=False, description="是否通过校验（无 Critical 缺失）")
    score: float = Field(default=0.0, description="完整度评分 0.0-1.0")
