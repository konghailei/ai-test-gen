"""API 请求/响应 Schema。

定义前后端交互的数据结构，作为 OpenAPI 文档的来源。
"""

from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field


# ============================================================================
# 通用响应
# ============================================================================

class ApiResponse(BaseModel):
    """统一 API 响应格式。"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="提示消息")
    data: Any = Field(default=None, description="响应数据")


# ============================================================================
# 需求相关
# ============================================================================

class RequirementUploadRequest(BaseModel):
    """需求上传请求。"""
    raw_text: str = Field(..., min_length=1, max_length=100000, description="需求原始文本（Markdown 或纯文本）")
    product_id: str = Field(default="PROD-DEFAULT", max_length=64, description="产品 ID")
    project_id: str | None = Field(default=None, description="所属项目/版本 ID")
    product_name: str = Field(default="", description="产品名称")
    title: str = Field(default="", max_length=512, description="需求标题（可选，留空则从文本提取）")
    priority: Literal["P0", "P1", "P2", "P3"] = Field(default="P2", description="优先级 P0-P3")
    modules: list[str] = Field(default_factory=list, description="关联模块列表")


class MultiDocUploadRequest(BaseModel):
    """多文档上传请求（v2.1 新增）。"""
    files: list[Any] = Field(..., description="多文件列表（需求文档 + 设计文档等）")
    product_id: str = Field(default="PROD-DEFAULT", max_length=64, description="产品 ID")
    product_name: str = Field(default="", description="产品名称")
    title: str = Field(default="", max_length=512, description="需求标题（可选）")
    priority: Literal["P0", "P1", "P2", "P3"] = Field(default="P2", description="优先级 P0-P3")
    modules: list[str] = Field(default_factory=list, description="关联模块列表")


class KBAccessLogEntry(BaseModel):
    """KB 访问日志条目（v2.1 新增）。"""
    action: str = Field(..., description="操作类型：kb_query / kb_unresolved / kb_augment / check_kb_status")
    query: str | None = Field(default=None, description="检索语句")
    result_count: int | None = Field(default=None, description="检索返回数量")
    top_similarity: float | None = Field(default=None, description="最高相似度")
    top_match_id: str | None = Field(default=None, description="最匹配的需求ID")
    field: str | None = Field(default=None, description="补全字段名")
    source_req_id: str | None = Field(default=None, description="源需求ID")
    similarity: float | None = Field(default=None, description="相似度")
    auto_filled: bool | None = Field(default=None, description="是否自动填充")
    kb_empty: bool | None = Field(default=None, description="知识库是否为空")
    product_first_requirement: bool | None = Field(default=None, description="是否产品首个需求")
    duration_ms: int = Field(default=0, description="耗时（毫秒）")
    status: str = Field(default="success", description="状态：success / failed / degraded")
    error: str | None = Field(default=None, description="错误信息")
    timestamp: str = Field(default="", description="操作时间")


class RequirementUpdateRequest(BaseModel):
    """需求编辑请求（更新 SRR 字段）。"""
    srr_json: dict = Field(..., description="更新后的完整 SRR JSON")
    raw_text: str | None = Field(default=None, description="更新原始文本（可选）")
    # v3.4（docs/15 §4.8）：乐观锁 —— 提交时携带打开编辑页时的修订号，
    # 与当前 revision 不一致 → 409（防止两人并发编辑互相覆盖）。缺省 = 兼容旧客户端。
    expected_revision: int | None = Field(
        default=None, description="期望的需求修订号（乐观锁；不传则跳过校验）"
    )


class RequirementSummary(BaseModel):
    """需求列表项（摘要）。"""
    id: str
    title: str
    product_id: str
    project_id: str = Field(default="", description="所属项目 ID")
    priority: str
    status: str
    source_type: str = Field(default="", description="来源格式：markdown / docx / txt")
    modules: list = Field(default_factory=list, description="关联模块 ID 列表")
    tags: list = Field(default_factory=list, description="标签列表（用于检索）")
    created_at: str


class ClarifyAnswerRequest(BaseModel):
    """澄清问题回答请求（v2.3 新增 extra_notes）。"""
    answers: dict[str, str] = Field(
        ..., description="问题 ID 到答案的映射，如 {'Q001': '密码长度 8-20'}"
    )
    extra_notes: str = Field(
        default="", description="额外补充说明（用户附加的自由文本）"
    )


class RegenerateRequest(BaseModel):
    """重新生成需求分析请求（澄清完成后补充信息重新解析）。"""
    extra_notes: str = Field(
        ..., description="用户补充说明，追加到原始需求文本后重新解析"
    )


# ============================================================================
# 生成相关
# ============================================================================

class GenerateRequest(BaseModel):
    """用例生成请求。"""
    req_id: str = Field(..., description="需求 ID")
    channel: Literal["FAST", "FULL"] | None = Field(
        default=None, description="通道选择（留空则自动选择）"
    )
    # v3.1.3 断点续跑：resume=True 时从最近一次中断/作废任务的 checkpoint 继续，
    # 跳过已生成完成的功能点，仅补未完成部分。
    resume: bool = Field(default=False, description="是否断点续跑")
    resume_from_task: str = Field(
        default="", max_length=64, description="指定续跑来源任务 ID（留空则自动查找最近可续任务）"
    )
    # v3.4（docs/15 §4.2 第 4 条）：续跑前校验输入指纹（SRR/FP 集合/需求修订/prompt/模型/
    # 合同版本）。任一不一致 → 409 拒绝续跑；只有显式 fallback_to_restart=true 才允许
    # 放弃旧 checkpoint 从头开始。默认 False = 保守（宁可拒绝也不把旧产物拼到新输入上）。
    fallback_to_restart: bool = Field(
        default=False, description="输入指纹不一致时是否放弃旧断点从头生成"
    )


# ============================================================================
# 用例管理
# ============================================================================

class TestCaseUpdateRequest(BaseModel):
    """用例编辑请求。"""
    title: str | None = None
    type: str | None = None
    priority: str | None = None
    description: str | None = None
    preconditions: list[str] | None = None
    steps: list[dict] | None = None
    expected: str | None = None
    test_data: dict | None = None
    assertions: list[dict] | None = None
    functional_point: str | None = None
    req_id: str | None = None


class TestCaseCreateRequest(BaseModel):
    """用例新增请求（人工创建）。"""
    req_id: str = Field(..., min_length=1, max_length=64, description="关联需求 ID（必填）")
    product_id: str | None = Field(default=None, max_length=64, description="产品 ID（可选，留空则从需求自动推导）")
    title: str = Field(..., min_length=1, max_length=255, description="用例标题")
    type: str = Field(default="functional", max_length=32, description="用例类型")
    priority: str = Field(default="P1", max_length=8, description="优先级 P0-P3")
    description: str = Field(default="", max_length=4000, description="用例描述")
    preconditions: list[str] | None = Field(default=None, description="前置条件列表")
    steps: list[dict] | None = Field(default=None, description="测试步骤列表")
    expected: str = Field(default="", max_length=2000, description="整体预期结果")
    assertions: list[dict] | None = Field(default=None, description="断言列表")
    functional_point: str | None = Field(default=None, max_length=255, description="功能点名称（可选，来自 XMind 树位置归属）")


class ExportRequest(BaseModel):
    """导出请求。"""
    format: Literal["markdown", "gherkin", "pytest", "json", "xmind", "xmind8"] = Field(
        default="markdown", description="导出格式"
    )
    case_ids: list[str] | None = Field(
        default=None, description="指定用例 ID（留空则导出全部）"
    )
    project_name: str = Field(
        default="", max_length=255, description="项目名称（XMind 导出时作为根标题）"
    )
    include_details: bool = Field(
        default=True, description="是否导出用例详细步骤和前置条件（XMind 格式适用，False 时用例仅保留标题）"
    )


class FeedbackRequest(BaseModel):
    """审核反馈请求。"""
    accepted: bool = Field(..., description="是否采纳")
    comments: str = Field(default="", max_length=2000, description="反馈评论")


# ============================================================================
# 设置相关
# ============================================================================

# 内置供应商预设
BUILTIN_PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek（推荐，中文优化）",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "openai": {
        "label": "OpenAI（备用）",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
    },
    "qwen": {
        "label": "通义千问（阿里云）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "glm": {
        "label": "GLM（智谱AI）",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-5.2",
    },
    "claude": {
        "label": "Claude（Anthropic）",
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-3-5-sonnet-20241022",
    },
}


class LLMConfigItem(BaseModel):
    """单条供应商配置。"""
    id: int = Field(default=0, description="数据库 ID（0 表示新增）")
    provider: str = Field(default="custom", description="供应商标识")
    provider_label: str = Field(default="", description="供应商显示名称")
    api_key: str = Field(default="", description="API Key（写时明文，读时脱敏）")
    base_url: str = Field(default="", description="API 基础地址")
    model: str = Field(default="", description="模型名称")
    is_primary: bool = Field(default=False, description="是否为主供应商")
    enable_thinking: bool = Field(default=False, description="是否启用思考模式（仅对支持思考的模型生效）")
    thinking_params: str = Field(default="", description="自定义思考参数（JSON 字符串，为空时使用内置规则）")


class LLMConfigRequest(BaseModel):
    """LLM 配置更新请求（支持批量更新）。"""
    providers: list[LLMConfigItem] = Field(
        ..., description="供应商配置列表（全量替换）"
    )


class LLMConfigResponse(BaseModel):
    """LLM 配置响应（多个供应商 + 脱敏 Key）。"""
    providers: list[LLMConfigItem] = Field(
        default_factory=list, description="供应商配置列表（Key 已脱敏）"
    )
    builtin_providers: dict = Field(
        default_factory=lambda: BUILTIN_PROVIDERS,
        description="内置供应商预设（可参考填写）",
    )


class LLMTestResponse(BaseModel):
    """LLM 连通性测试响应。"""
    success: bool
    latency_ms: int = Field(description="响应延迟（毫秒）")
    provider: str = Field(default="", description="测试的供应商")
    model: str = Field(default="", description="模型名")
    message: str


# ============================================================================
# 知识库
# ============================================================================

class KnowledgeStats(BaseModel):
    """知识库统计。"""
    total_requirements: int = 0
    total_test_cases: int = 0
    total_patterns: int = 0
    total_batches: int = 0
    kb_empty: bool = True


# ============================================================================
# 知识问答
# ============================================================================

class KbQAAskRequest(BaseModel):
    """知识问答提问请求。"""
    question: str = Field(..., min_length=1, max_length=10000, description="用户问题")
    product_id: str | None = Field(default=None, max_length=64, description="产品 ID（可选，用于产品隔离）")
    modules: list[str] | None = Field(default=None, description="按模块过滤（多选，可选，仅在所选模块范围内问答）")


class KbQAAskResponse(BaseModel):
    """知识问答响应。"""
    id: str = Field(..., description="问答记录 ID")
    question: str = Field(..., description="用户问题")
    answer: str = Field(..., description="AI 回答")
    reference_sources: list[dict] = Field(default_factory=list, description="参考来源")


class KbQACharRequest(BaseModel):
    """知识问答追问请求。"""
    record_id: str = Field(..., description="原始问答记录 ID")
    question: str = Field(..., min_length=1, max_length=10000, description="追问内容")


class KbQACorrectRequest(BaseModel):
    """知识问答纠错请求（v2.8.56：提交审核流程）。"""
    id: str = Field(..., description="问答记录 ID")
    corrected_answer: str = Field(..., min_length=1, max_length=50000, description="纠错后的回答")
    correction_notes: str = Field(default="", max_length=2000, description="纠错说明")
    # v2.8.56: 审核流程字段
    correction_module: str = Field(default="", max_length=128, description="纠正关联的模块名（用于按模块筛选审核列表）")
    correction_target_type: Literal["requirement", "functional_point"] = Field(
        default="requirement", description="纠正目标颗粒度：整篇需求 / 单个功能点"
    )
    correction_target_id: str = Field(default="", max_length=64, description="纠正目标 ID（FP_ID 或 req_id）")


class KbQAArchiveRequest(BaseModel):
    """知识问答归档请求。"""
    id: str = Field(..., description="问答记录 ID")
    archive_type: Literal["requirement", "test_case"] = Field(default="requirement", description="归档类型")
    title: str = Field(default="", max_length=512, description="归档标题（可选，留空自动生成）")
    content: str = Field(..., min_length=1, max_length=50000, description="归档内容")


class KbQAReviewRequest(BaseModel):
    """v2.8.56: 纠正审核请求。"""
    id: str = Field(..., description="问答记录 ID")
    review_notes: str = Field(default="", max_length=2000, description="审核意见")


class KbQARecord(BaseModel):
    """知识问答记录（列表项）。"""
    id: str
    question: str
    answer: str
    corrected_answer: str | None = None
    correction_notes: str = ""
    archived: bool = False
    archived_type: str = ""
    archived_id: str = ""
    reference_sources: list[dict] = Field(default_factory=list)
    created_at: str
    updated_at: str
    # v2.8.56: 审核流程字段
    correction_status: str = "none"
    correction_module: str = ""
    correction_target_type: str = "requirement"
    correction_target_id: str = ""
    reviewed_by: str = ""
    reviewed_at: str | None = None
    review_notes: str = ""
