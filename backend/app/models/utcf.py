"""UTCF（Universal Test Case Format）数据模型。

将架构文档 docs/04-output-format.md §1-§2 的 UTCF JSON Schema
和 templates/testcase-output-template.json 转为 Pydantic 模型。

用途：
- GenAgent 生成测试用例后输出 UTCF 实例
- ReviewAgent 审核用例时接收 UTCF
- 导出器将 UTCF 转为 Markdown/Gherkin/pytest
- 知识库存储用例的标准化格式

设计依据：
- docs/04-output-format.md:33-179（UTCF Schema 定义）
- docs/03-case-generation-flow.md:656-670（final_confidence 计算）
- templates/FIELD_DEFINITIONS.md:398-409（用例字段说明）
"""

from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field


# ============================================================================
# 测试用例子模型
# ============================================================================

class Step(BaseModel):
    """测试步骤。

    对应 UTCF.test_suite[].steps[]。
    """
    step: int = Field(..., description="步骤序号，从 1 开始")
    action: str = Field(..., description="操作描述")
    expected: str = Field(..., description="预期结果")
    data: dict | None = Field(default=None, description="步骤测试数据")


class Assertion(BaseModel):
    """断言。

    对应 UTCF.test_suite[].assertions[]。
    支持的 type 枚举（docs/04-output-format.md §1）：
    equals / contains / matches / status_code / gt / lt / not_null / custom
    """
    type: Literal[
        "equals", "contains", "matches", "status_code",
        "gt", "lt", "not_null", "custom"
    ] = Field(..., description="断言类型")
    target: str = Field(default="", description="断言目标路径，如 response.token")
    expected_value: object = Field(default=None, description="期望值")
    message: str = Field(default="", description="断言说明")


class ExecutionConfig(BaseModel):
    """执行配置。

    对应 UTCF.test_suite[].execution。
    """
    timeout_seconds: int = Field(default=15, description="超时时间（秒）")
    retry_count: int = Field(default=0, description="重试次数")
    depends_on: list[str] = Field(default_factory=list, description="依赖的用例 ID")
    skip_conditions: list[str] = Field(default_factory=list, description="跳过条件")


class GenMetadata(BaseModel):
    """生成元数据。

    存储 GenAgent 生成阶段的信息，含 gen_confidence（自评估）。
    对应 docs/03-case-generation-flow.md §5.2 的字段语义统一。
    """
    gen_confidence: float = Field(
        default=0.5, description="GenAgent 生成时 LLM 自评估置信度（0-1），仅作 ReviewAgent 输入"
    )
    gen_model: str = Field(default="", description="生成模型，如 deepseek-chat")
    gen_at: str = Field(default="", description="生成时间 ISO 8601")
    source_pattern: str = Field(default="", description="参考的测试模式 ID")


class TestCase(BaseModel):
    """测试用例（UTCF 单条）。

    对应 docs/04-output-format.md §1 的 TestCase 定义。
    ID 正则：^TC-[A-Z]+-\d{4}-\d{4}$
    """
    id: str = Field(..., description="用例 ID，如 TC-AUTH-0001-0001")
    title: str = Field(..., description="用例标题")
    type: Literal[
        "functional", "boundary", "exception", "security",
        "performance", "integration", "exploratory"
    ] = Field(..., description="用例类型")
    priority: Literal["P0", "P1", "P2", "P3"] = Field(
        default="P1", description="优先级：P0=核心流程 / P1=重要分支 / P2=边界异常 / P3=探索性"
    )
    description: str = Field(default="", description="用例描述")
    tags: list[str] = Field(default_factory=list, description="标签列表")
    preconditions: list[str] = Field(default_factory=list, description="前置条件列表")
    steps: list[Step] = Field(..., description="测试步骤列表")
    expected: str | None = Field(default=None, description="整体预期结果")
    test_data: dict = Field(default_factory=dict, description="测试数据")
    assertions: list[Assertion] = Field(default_factory=list, description="断言列表")
    cleanup: list[str] = Field(default_factory=list, description="清理操作列表")
    related_requirement: str = Field(..., description="关联的需求 ID")
    source_pattern: str = Field(default="", description="参考的测试模式 ID")
    confidence: float = Field(
        default=0.0,
        description="最终置信度（0-1），由 ReviewAgent.calculate_confidence 计算后写入"
    )
    gen_metadata: GenMetadata = Field(
        default_factory=GenMetadata, description="生成元数据（含 gen_confidence）"
    )
    execution: ExecutionConfig = Field(
        default_factory=ExecutionConfig, description="执行配置"
    )


# ============================================================================
# UTCF 元数据
# ============================================================================

class SourceRequirement(BaseModel):
    """来源需求信息。"""
    id: str = Field(..., description="需求 ID")
    title: str = Field(default="", description="需求标题")
    version: int = Field(default=1, description="需求版本")


class CoverageSummary(BaseModel):
    """覆盖率汇总。"""
    functional: int = Field(default=0, description="功能测试数")
    boundary: int = Field(default=0, description="边界测试数")
    exception: int = Field(default=0, description="异常测试数")
    security: int = Field(default=0, description="安全测试数")
    performance: int = Field(default=0, description="性能测试数")


class UTCFMetadata(BaseModel):
    """UTCF 元数据。"""
    generated_at: str = Field(default="", description="生成时间 ISO 8601")
    generator: str = Field(default="ai-test-gen v1.0.0", description="生成器标识")
    generator_model: str = Field(default="", description="生成模型")
    source_requirement: SourceRequirement | None = Field(default=None, description="来源需求")
    gen_batch_id: str = Field(default="", description="生成批次 ID")
    total_cases: int = Field(default=0, description="用例总数")
    coverage_summary: CoverageSummary = Field(
        default_factory=CoverageSummary, description="覆盖率汇总"
    )


# ============================================================================
# UTCF 主模型
# ============================================================================

class UTCF(BaseModel):
    """UTCF（Universal Test Case Format）通用测试用例格式。

    这是测试用例的标准化输出格式，对应 docs/04-output-format.md §1。
    包含元数据和测试套件（用例列表）。
    """
    format_version: str = Field(default="1.0", description="UTCF 格式版本")
    metadata: UTCFMetadata = Field(default_factory=UTCFMetadata, description="元数据")
    test_suite: list[TestCase] = Field(default_factory=list, description="测试用例列表")


# ============================================================================
# ReviewAgent 输出模型
# ============================================================================

class ReviewIssue(BaseModel):
    """审核发现的问题。"""
    case_id: str = Field(..., description="用例 ID")
    dimension: Literal[
        "completeness", "deduplication", "executability",
        "consistency", "traceability", "coverage"
    ] = Field(..., description="审核维度")
    severity: Literal["blocker", "warning", "info"] = Field(
        default="warning", description="严重程度"
    )
    message: str = Field(..., description="问题描述")
    suggestion: str = Field(default="", description="修正建议")


class ReviewReport(BaseModel):
    """ReviewAgent 审核报告。"""
    total_cases: int = Field(default=0, description="用例总数")
    passed_cases: int = Field(default=0, description="通过用例数")
    blocked_cases: int = Field(default=0, description="阻断用例数")
    issues: list[ReviewIssue] = Field(default_factory=list, description="问题列表")
    coverage_summary: CoverageSummary = Field(
        default_factory=CoverageSummary, description="覆盖率汇总"
    )
    avg_confidence: float = Field(default=0.0, description="平均置信度")
    should_supplement: bool = Field(
        default=False, description="是否需要补充生成（覆盖率不足或阻断项过多）"
    )
