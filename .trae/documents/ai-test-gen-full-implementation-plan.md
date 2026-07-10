# AI-Test-Gen 完整实现计划（v2）

> 基于架构文档 docs/00-05 与现有代码基础，制定从当前状态到完整可交付的端到端实现计划。
> 所有文件路径均基于 Phase 1 实际探索，所有设计决策均依据架构文档章节。

---

## 一、摘要

### 目标
交付一款 AI 测试用例生成 Web 应用，满足：
1. 美观现代的 UI/UX（React 18 + Tailwind + shadcn/ui）
2. 直观易用的操作流程（4 步引导：输入需求 → 澄清 → 生成 → 审核）
3. 详尽的中文代码注释（函数用途、参数、返回值、关键逻辑）
4. 准确解析架构文档、智能生成符合 UTCF 规范的测试用例
5. 支持用例导出（Markdown/Gherkin/pytest）、编辑、管理

### 技术栈（已批准决策）
- **后端**：FastAPI + LangGraph + LiteLLM + ChromaDB + SQLite
- **前端**：Vite + React 18 + TypeScript + Tailwind + shadcn/ui + zustand + react-query
- **LLM**：真实 API（DeepSeek 主力 + OpenAI 备用故障转移，用户提供 Key）
- **版本**：完整版（5 Agent + 通道选择 FAST/FULL）

---

## 二、当前状态分析

### 2.1 已完成（后端基础设施层，约 2200 行代码）

| 文件 | 行数 | 状态 | 核心内容 |
|------|------|------|----------|
| [app/main.py](file:///d:/git_down/ai-test-gen/backend/app/main.py) | 190 | ✅ | FastAPI 入口、lifespan、CORS、日志中间件、健康检查 `/api/health` |
| [app/config.py](file:///d:/git_down/ai-test-gen/backend/app/config.py) | 116 | ✅ | pydantic-settings 配置（LLM/DB/阈值） |
| [app/core/llm_client.py](file:///d:/git_down/ai-test-gen/backend/app/core/llm_client.py) | 481 | ✅ | LLM 统一客户端（断路器+全抖动重试+多提供商故障转移） |
| [app/db/database.py](file:///d:/git_down/ai-test-gen/backend/app/db/database.py) | 64 | ✅ | SQLAlchemy 异步引擎 + init_db + get_session |
| [app/db/models.py](file:///d:/git_down/ai-test-gen/backend/app/db/models.py) | 181 | ✅ | 7 张 ORM 表（Requirement/TestCase/TestPattern/ErrorCase/GenerationBatch/ReqDependency/Traceability） |
| [app/db/vector_store.py](file:///d:/git_down/ai-test-gen/backend/app/db/vector_store.py) | 300 | ⚠️ | ChromaDB 封装（4 Collection），但导入失败（见 §三） |
| [app/db/seed.py](file:///d:/git_down/ai-test-gen/backend/app/db/seed.py) | 251 | ✅ | 8 个测试模式种子数据 |
| [app/models/srr.py](file:///d:/git_down/ai-test-gen/backend/app/models/srr.py) | 219 | ✅ | SRR 结构化需求模型（21 个 Pydantic 类） |
| [app/models/utcf.py](file:///d:/git_down/ai-test-gen/backend/app/models/utcf.py) | 158 | ✅ | UTCF 测试用例模型（11 个类） |
| [app/models/api_schemas.py](file:///d:/git_down/ai-test-gen/backend/app/models/api_schemas.py) | 104 | ✅ | API 请求/响应 Schema（12 个类） |
| [app/utils/id_gen.py](file:///d:/git_down/ai-test-gen/backend/app/utils/id_gen.py) | 70 | ✅ | 线程安全 ID 生成 |
| [app/utils/logger.py](file:///d:/git_down/ai-test-gen/backend/app/utils/logger.py) | 41 | ✅ | loguru 日志配置 |

### 2.2 缺失（业务逻辑层 + 前端，全部待实现）

| 缺失项 | 路径 | 依据 |
|--------|------|------|
| 质量门禁 | `backend/app/core/quality_gate.py` | doc-01 §5.2 |
| 通道选择器 | `backend/app/core/channel_selector.py` | doc-00 §9.1 |
| LangGraph 编排器 | `backend/app/core/orchestrator.py` | doc-00 §3, doc-05 §3.2 |
| 5 个 Agent | `backend/app/agents/` | doc-00 §4 |
| 提示词模板 | `backend/app/prompts/` | doc-01/03 |
| API 路由层 | `backend/app/api/` | 实现计划阶段3 |
| 导出器 | `backend/app/exporters/` | doc-04 §3/§9 |
| 单元测试 | `backend/tests/` | 验证要求 |
| 前端工程 | `frontend/` | 实现计划阶段5-6 |
| README 与扩展文档 | `README.md`, `docs/06-database-extension.md` | 交付要求 |

### 2.3 关键阻塞问题

**`import chromadb` 失败**，导致整个后端无法启动。根因链：
1. 环境中 NumPy=2.4.6，而已安装的 onnxruntime 用 NumPy 1.x 编译 → `_ARRAY_API not found`
2. chromadb 0.5.23 在 `CollectionCommon.py:112` 类定义时强制实例化 `DefaultEmbeddingFunction()` → 触发 onnxruntime 加载
3. sentence-transformers/torch 链路也损坏（torch c10.dll 加载失败 WinError 1114）

**当前 vector_store.py 改用 SentenceTransformerEmbeddingFunction 的方案无效**，因为 `import chromadb` 在到达用户代码前就已失败。

---

## 三、关键阻塞问题解决方案（阶段1首要任务）

### 3.1 方案选择：降级 NumPy + 回退到 chromadb 默认 ONNX 嵌入

**理由**：
- onnxruntime 与 NumPy 2.x 的二进制不兼容是系统级问题，降级 NumPy 是最直接的修复
- chromadb 默认的 `ONNXMiniLM_L6_V2` 只依赖 onnxruntime + tokenizers + tqdm，不需要 torch
- sentence-transformers 依赖 torch，而 torch 在本机有 DLL 加载问题，应避免
- chromadb 默认嵌入模型 `all-MiniLM-L6-v2`（384 维）与架构文档 MVP 阶段选型一致（doc-05 §11）

### 3.2 具体步骤

**T0.1 降级 NumPy**
```bash
pip install "numpy==1.26.4"
```
- 验证：`python -c "import numpy; print(numpy.__version__)"` → 1.26.4
- 验证：`python -c "import onnxruntime; print(onnxruntime.__version__)"` → 成功

**T0.2 修复 vector_store.py**
- 移除 `SentenceTransformerEmbeddingFunction` 导入（依赖 torch，已损坏）
- 回退为不指定 `embedding_function`，使用 chromadb 默认 `ONNXMiniLM_L6_V2`
- 保留原有 4 个 Collection 结构不变
- 修改 [app/db/vector_store.py:18-20](file:///d:/git_down/ai-test-gen/backend/app/db/vector_store.py#L18-L20) 和 [app/db/vector_store.py:51-81](file:///d:/git_down/ai-test-gen/backend/app/db/vector_store.py#L51-L81)

**T0.3 验证后端可启动**
```bash
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 验证 GET /api/health 返回 200
# 验证 GET /docs 可访问 Swagger
```

**T0.4 更新 requirements.txt**
- 添加 `numpy==1.26.4` 显式锁定版本
- 移除对 sentence-transformers 的隐式依赖

---

## 四、实施阶段

### 阶段1：修复阻塞 + 验证基础设施（T0-T1）

| 任务 | 文件 | 内容 |
|------|------|------|
| T0.1 | 环境依赖 | 降级 NumPy 到 1.26.4 |
| T0.2 | [vector_store.py](file:///d:/git_down/ai-test-gen/backend/app/db/vector_store.py) | 回退到 chromadb 默认 ONNX 嵌入 |
| T0.3 | 验证 | uvicorn 启动 + /api/health 200 |
| T0.4 | [requirements.txt](file:///d:/git_down/ai-test-gen/backend/requirements.txt) | 锁定 numpy==1.26.4 |
| T1.1 | [core/__init__.py](file:///d:/git_down/ai-test-gen/backend/app/core/__init__.py) | 修正 docstring（移除未实现模块的描述） |

### 阶段2：核心组件 + 提示词（T2）

#### T2.1 通道选择器 `backend/app/core/channel_selector.py`

依据 [doc-00 §9.1](file:///d:/git_down/ai-test-gen/docs/00-architecture-overview.md) 的 `select_channel()` 函数：

```python
def select_channel(srr: dict) -> ChannelConfig:
    """根据 SRR 复杂度选择 FAST 或 FULL 通道。
    
    判定规则（任一满足即 FULL）：
    - priority == "P0"
    - len(srr.flows.happy_path.steps) > 5
    - srr.optimization_context 存在（优化需求）
    
    Returns:
        ChannelConfig: {channel, enable_clarify, enable_impact, 
                       enable_multi_type, enable_full_review}
    """
```

**通道能力矩阵**（doc-00 §9.1）：

| 通道 | enable_clarify | enable_impact | enable_multi_type | enable_full_review |
|------|---|---|---|---|
| FAST | False | False | False（单批） | False（5% 采样） |
| FULL | True | True（仅优化需求） | True（4 类型并行） | True（100%） |

#### T2.2 质量门禁 `backend/app/core/quality_gate.py`

依据 [doc-01 §5.2](file:///d:/git_down/ai-test-gen/docs/01-requirement-parsing.md)：

```python
class QualityGate:
    """SRR 质量门禁。
    
    校验维度：
    1. Schema 完整性（必填字段：requirement.title/summary/priority, 
       flows.happy_path.steps, preconditions）
    2. 质量评分（0-1）
    3. 冷启动模式（kb_empty 时阈值降为 0.60，正常 0.75）
    
    Returns:
        GateResult: {passed, blockers, warnings, pass_score, cold_start}
    """
    
    async def check(self, srr: dict, kb_empty: bool) -> GateResult:
        normal_threshold = settings.DEFAULT_PASS_SCORE  # 0.75
        cold_start_threshold = settings.COLD_START_PASS_SCORE  # 0.60
        threshold = cold_start_threshold if kb_empty else normal_threshold
        # ... 计算 pass_score，返回 passed = pass_score >= threshold
```

#### T2.3 提示词模板 `backend/app/prompts/`

创建 5 个提示词文件，每个包含系统提示和用户提示模板：

| 文件 | Agent | 依据 |
|------|-------|------|
| `req_agent.py` | ReqAgent | doc-01 §3, templates/requirement-template.json |
| `clarify_agent.py` | ClarifyAgent | doc-01 §4 |
| `impact_agent.py` | ImpactAnalysisAgent | doc-03 §1.5 |
| `gen_agent.py` | GenAgent | doc-03 §2, templates/testcase-output-template.json |
| `review_agent.py` | ReviewAgent | doc-03 §5 |

每个提示词要求：
- 中文指令（与用户语言一致）
- 明确输出 JSON Schema（配合 `llm_client.call_json()`）
- 包含 Few-shot 示例（GenAgent）
- 标注版本号 `prompt_version = "v1.0"`

### 阶段3：5 个 Agent 实现（T3）

所有 Agent 遵循统一契约：
- 异步方法 `async def run(self, ...) -> ...`
- 通过 `from app.core.llm_client import llm_client` 调用 LLM
- 完整中文 docstring（Args/Returns/Raises/依据）
- 异常向上抛出，由 orchestrator 捕获

#### T3.1 ReqAgent `backend/app/agents/req_agent.py`

依据 [doc-01 §3](file:///d:/git_down/ai-test-gen/docs/01-requirement-parsing.md), [doc-00 §4](file:///d:/git_down/ai-test-gen/docs/00-architecture-overview.md)：

```python
class ReqAgent:
    """需求解析器：将原始需求文本转为 SRR JSON。
    
    职责：
    1. NL 理解与实体抽取
    2. 格式标准化为 SRR（依据 templates/requirement-template.json）
    3. 优化意图检测（检测到则填充 optimization_context）
    
    输入：raw_text, source_type(markdown/json/plain)
    输出：SRR dict（含 requirement/flows/preconditions/data_constraints/
          optimization_context?）
    """
    async def parse(self, raw_text: str, source_type: str = "markdown") -> dict:
        ...
```

**关键约束**（项目记忆）：
- 必须使用 `product_id` 和 `modules[]`（复数）
- 优化意图检测在此阶段完成（非 ImpactAnalysisAgent）

#### T3.2 ClarifyAgent `backend/app/agents/clarify_agent.py`

依据 [doc-01 §4](file:///d:/git_down/ai-test-gen/docs/01-requirement-parsing.md)：

```python
class ClarifyAgent:
    """需求澄清器：检测 SRR 遗漏，生成澄清问题，细化 SRR。
    
    三级检测：Critical（阻塞）/ Important（建议）/ Optional（增强）
    KB 自动补全：从向量库检索相似需求填充缺失字段
    字段冲突处理：KB 补全与用户输入冲突时，用户优先（5 级优先级）
    
    输入：SRR, kb_empty 标志
    输出：{refined_srr, questions[], field_conflicts[], quality_score}
    """
    async def detect_gaps(self, srr: dict, kb_empty: bool) -> dict:
        """检测遗漏并生成问题列表（≤5个）。"""
    
    async def refine(self, srr: dict, answers: list[dict]) -> dict:
        """根据用户答案细化 SRR。"""
```

**设计决策**：采用 API 驱动而非 LangGraph interrupt（更简单可靠）
- `POST /api/requirements/{id}/clarify` 提交答案 → 返回细化后的 SRR 或下一轮问题
- 最多 3 轮（由 ClarifyLoopGuard 控制，doc-03 §6.7）

#### T3.3 ImpactAnalysisAgent `backend/app/agents/impact_agent.py`

依据 [doc-03 §1.5](file:///d:/git_down/ai-test-gen/docs/03-case-generation-flow.md)：

```python
class ImpactAnalysisAgent:
    """影响分析器：仅优化需求完整执行。
    
    输入：SRR.optimization_context
    输出：{is_optimization, impact{direct, indirect, per_module, cross_module},
           case_operations{update, add, delete, keep}, 
           regression_scope, generation_guidance}
    
    成本控制（doc-03 §1.5）：
    - MAX_GRAPH_NODES = 5000
    - MAX_TRAVERSAL_DEPTH = 3
    - TIMEOUT_SECONDS = 5.0
    
    依赖图谱遍历：基于 req_dependencies 表（SQL recursive CTE 或 Python BFS）
    """
    async def analyze(self, srr: dict) -> dict:
        ...
```

#### T3.4 GenAgent `backend/app/agents/gen_agent.py`

依据 [doc-03 §2](file:///d:/git_down/ai-test-gen/docs/03-case-generation-flow.md), [doc-04 §2](file:///d:/git_down/ai-test-gen/docs/04-output-format.md)：

```python
class GenAgent:
    """用例生成器：基于 SRR 生成 UTCF 测试用例集。
    
    通道差异：
    - FAST：单批生成（functional 为主）
    - FULL：4 类型并行（functional/boundary/exception/security）
    
    Few-shot：从向量库检索 Top-K 相似用例作为示例
    追溯：每条用例标注 related_requirement + source_pattern
    
    输入：SRR, impact_result?, few_shot_cases[], channel
    输出：UTCF dict（test_suite[] 含 gen_metadata.gen_confidence）
    
    ID 生成：gen_test_case_id(module_prefix) 符合 ^TC-[A-Z]+-\d{4}-\d{4}$
    """
    async def generate(self, srr: dict, channel: str, 
                       impact_result: dict | None = None,
                       few_shot_cases: list | None = None) -> dict:
        ...
```

#### T3.5 ReviewAgent `backend/app/agents/review_agent.py`

依据 [doc-03 §5](file:///d:/git_down/ai-test-gen/docs/03-case-generation-flow.md)：

```python
class ReviewAgent:
    """质量审核器：六维审核 + 置信度计算 + 去重。
    
    六维审核：完整性/去重/可执行/一致性/追溯性/覆盖率
    采样：FAST 5% / FULL 100%
    
    置信度计算（doc-03 §5.2）：
    final_confidence = 0.4*match + 0.3*completeness + 0.2*consistency 
                       + 0.1*gen_self + pattern_bonus
    
    输出覆盖 gen_confidence → final_confidence
    
    输入：UTCF, SRR
    输出：{reviewed_utcf, review_report{issues[], should_supplement, 
           final_confidences{}}}
    """
    async def review(self, utcf: dict, srr: dict, 
                     full_review: bool = False) -> dict:
        ...
```

### 阶段4：LangGraph 编排器（T4）

#### T4.1 `backend/app/core/orchestrator.py`

依据 [doc-00 §3](file:///d:/git_down/ai-test-gen/docs/00-architecture-overview.md), [doc-05 §3.2](file:///d:/git_down/ai-test-gen/docs/05-technical-selection.md)：

```python
from langgraph.graph import StateGraph, END
from typing import TypedDict, AsyncGenerator

class PipelineState(TypedDict):
    """LangGraph 流水线状态。"""
    raw_requirement: str
    source_type: str
    srr: dict | None
    channel: str
    channel_config: dict | None
    clarify_result: dict | None
    gate_result: dict | None
    impact_result: dict | None
    few_shot_cases: list
    utcf: dict | None
    review_result: dict | None
    error: str | None

class Orchestrator:
    """LangGraph DAG 编排器。
    
    DAG 节点：
    1. parse_node → ReqAgent.parse
    2. select_channel_node → select_channel
    3. 条件分支：
       FULL: clarify_node → quality_gate_node →(不达标回clarify) 
             → impact_node(仅优化) → retrieve_node → gen_node → review_node
       FAST: quality_gate_node → retrieve_node → gen_node → review_node
    4. END
    
    SSE 推送：每个节点完成后 yield {stage, message, progress, result?}
    """
    
    def __init__(self):
        self.workflow = self._build_graph()
    
    def _build_graph(self) -> StateGraph:
        """构建 LangGraph 状态图。"""
        ...
    
    async def run_pipeline(self, raw_text: str, 
                          source_type: str = "markdown",
                          clarify_answers: list | None = None
                          ) -> AsyncGenerator[dict, None]:
        """执行流水线，异步生成 SSE 事件。
        
        Yields:
            dict: {stage, message, progress, result?}
                   stage ∈ parse/select_channel/clarify/gate/impact/
                          retrieve/generate/review/done/error
        """
        ...
```

**SSE 事件格式**（doc-00 §9.2）：
```
data: {"stage": "parse", "message": "ReqAgent 解析中...", "progress": 20}
data: {"stage": "gate", "message": "质量门禁检查中...", "progress": 40}
...
data: {"stage": "done", "message": "完成！", "progress": 100, "result": {...}}
```

**ClarifyLoopGuard**（doc-03 §6.7）：最多 3 轮，超过则强制通过或失败。

### 阶段5：API 路由层（T5）

#### T5.1 路由结构 `backend/app/api/`

```
backend/app/api/
├── __init__.py          # api_router 聚合
├── requirements.py      # 需求上传/列表/详情/澄清
├── generate.py          # SSE 流式生成
├── testcases.py         # 用例 CRUD + 反馈
├── knowledge.py         # 知识库统计
├── settings.py          # LLM 配置
└── export.py            # 导出
```

#### T5.2 端点清单

依据 [api_schemas.py](file:///d:/git_down/ai-test-gen/backend/app/models/api_schemas.py) 已定义的 Schema：

| 路由文件 | 端点 | 方法 | Schema | 说明 |
|---|---|---|---|---|
| requirements.py | `/api/requirements/upload` | POST | RequirementUploadRequest | 上传需求，返回 req_id + 初始 SRR |
| | `/api/requirements` | GET | - | 列表（分页） |
| | `/api/requirements/{id}` | GET | - | 详情 |
| | `/api/requirements/{id}/clarify/questions` | GET | - | 获取待回答问题 |
| | `/api/requirements/{id}/clarify` | POST | ClarifyAnswerRequest | 提交澄清答案 |
| generate.py ⭐ | `/api/generate` | POST | GenerateRequest | **SSE 流式响应**，调用 orchestrator.run_pipeline |
| testcases.py | `/api/testcases` | GET | - | 列表筛选 |
| | `/api/testcases/{id}` | GET | - | 详情 |
| | `/api/testcases/{id}` | PUT | TestCaseUpdateRequest | 编辑 |
| | `/api/testcases/{id}` | DELETE | - | 删除 |
| | `/api/testcases/{id}/feedback` | POST | FeedbackRequest | 反馈回流 |
| knowledge.py | `/api/knowledge/stats` | GET | - | KnowledgeStats |
| | `/api/knowledge/patterns` | GET | - | 模式列表 |
| settings.py | `/api/settings/llm` | GET | - | LLMConfigResponse（脱敏） |
| | `/api/settings/llm` | PUT | LLMConfigRequest | 更新配置 |
| | `/api/settings/llm/test` | POST | - | LLMTestResponse（连通性测试） |
| export.py | `/api/export` | POST | ExportRequest | 导出（返回文件流或文本） |

#### T5.3 SSE 实现（generate.py）

```python
from sse_starlette.sse import EventSourceResponse

@router.post("/generate")
async def generate(req: GenerateRequest):
    """SSE 流式生成测试用例。"""
    async def event_generator():
        async for event in orchestrator.run_pipeline(...):
            yield {"event": "message", "data": json.dumps(event, ensure_ascii=False)}
    return EventSourceResponse(event_generator())
```

#### T5.4 接入 main.py

修改 [main.py:219-226](file:///d:/git_down/ai-test-gen/backend/app/main.py#L219-L226)，取消注释并注册路由：
```python
from app.api import api_router
app.include_router(api_router, prefix="/api")
```

### 阶段6：导出器（T6）

#### T6.1 `backend/app/exporters/`

```
backend/app/exporters/
├── __init__.py          # 工厂函数
├── markdown.py          # Markdown 表格导出
├── gherkin.py           # Gherkin BDD 格式
├── pytest_code.py       # pytest 测试代码
└── json_exporter.py     # 原生 UTCF JSON
```

依据 [doc-04 §3](file:///d:/git_down/ai-test-gen/docs/04-output-format.md)（测试框架适配层）和 [§9](file:///d:/git_down/ai-test-gen/docs/04-output-format.md)（Markdown 格式）：

```python
class MarkdownExporter:
    """导出为 Markdown 表格格式（doc-04 §9）。
    
    输出结构：
    # 测试用例集 - {需求标题}
    ## 覆盖率摘要
    ## 用例列表
    ### TC-XXX-0001-0001: {标题}
    | 步骤 | 操作 | 预期 |
    ...
    """

class GherkinExporter:
    """导出为 Gherkin BDD 格式（doc-04 §3）。
    
    Feature: {需求标题}
    Scenario: {用例标题}
    Given {前置条件}
    When {步骤}
    Then {预期}
    """

class PytestExporter:
    """导出为 pytest 测试代码（doc-04 §3）。
    
    生成符合 pytest 规范的 .py 文件，
    每个用例转为一个 test 函数，含 assert 语句。
    """
```

### 阶段7：后端单元测试（T7）

#### T7.1 `backend/tests/`

```
backend/tests/
├── conftest.py              # pytest fixtures
├── test_channel_selector.py # 通道选择逻辑
├── test_quality_gate.py     # 质量门禁
├── test_agents.py           # Agent 单元测试（mock llm_client）
├── test_orchestrator.py     # 编排器
├── test_api.py              # API 端点测试（TestClient）
└── test_exporters.py        # 导出器
```

**测试策略**：
- Agent 测试：mock `llm_client.call_json` 返回固定 JSON（这不是模拟业务数据，而是测试 Agent 逻辑的隔离手段）
- API 测试：使用 FastAPI TestClient + 临时 SQLite
- 通道选择/质量门禁：纯逻辑测试，无需 mock

**验证标准**：
```bash
cd backend
pytest tests/ -v --tb=short
# 所有测试通过
```

### 阶段8：前端基础搭建（T8）

#### T8.1 初始化 `frontend/`

```bash
cd d:\git_down\ai-test-gen
npm create vite@latest frontend -- --template react-ts
cd frontend
npm install
npm install -D tailwindcss postcss autoprefixer
npx tailwindcss init -p
npm install react-router-dom zustand axios @tanstack/react-query
npm install lucide-react sonner
npm install -D @types/node
# shadcn/ui 初始化
npx shadcn@latest init
```

#### T8.2 目录结构

```
frontend/
├── index.html
├── package.json
├── vite.config.ts          # 配置 dev proxy /api → http://localhost:8000
├── tailwind.config.js      # 主题色：蓝紫渐变
├── tsconfig.json
├── postcss.config.js
├── components.json         # shadcn/ui 配置
└── src/
    ├── main.tsx
    ├── App.tsx             # 路由配置
    ├── index.css           # Tailwind 指令 + 全局样式
    ├── lib/
    │   ├── api.ts          # axios 实例 + SSE 封装
    │   └── utils.ts        # cn() 等工具
    ├── components/
    │   ├── layout/
    │   │   ├── AppLayout.tsx
    │   │   ├── Sidebar.tsx
    │   │   └── TopBar.tsx
    │   └── ui/             # shadcn/ui 组件
    ├── stores/
    │   ├── requirementStore.ts
    │   ├── generateStore.ts
    │   └── settingsStore.ts
    ├── pages/
    │   ├── Home.tsx
    │   ├── RequirementInput.tsx
    │   ├── Clarify.tsx
    │   ├── Generate.tsx
    │   ├── Review.tsx
    │   ├── History.tsx
    │   └── Settings.tsx
    └── types/
        └── api.ts          # TypeScript 类型（对应 api_schemas.py）
```

#### T8.3 关键配置

**vite.config.ts**（开发代理）：
```typescript
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:8000'
    }
  }
})
```

**tailwind.config.js**（主题）：
```javascript
theme: {
  extend: {
    colors: {
      primary: { /* 蓝紫渐变 */ },
    }
  }
}
```

#### T8.4 布局组件

- `AppLayout`：左侧固定侧边栏（240px）+ 顶栏（64px）+ 内容区
- `Sidebar`：7 个导航项 + 图标（lucide-react）+ 当前路由高亮
- `TopBar`：页面标题 + 知识库状态徽章 + LLM 配置状态

### 阶段9：前端 7 页面实现（T9）

#### T9.1 Home 页面 `/`

- 系统简介卡片（渐变背景 + 图标）
- 4 步流程引导（输入需求 → 澄清 → 生成 → 审核，带连接线）
- 快速开始按钮（→ /requirement）
- 知识库统计卡片（需求数/用例数/模式数，调 `/api/knowledge/stats`）

#### T9.2 RequirementInput 页面 `/requirement`

- 大文本框（支持 Markdown/JSON/纯文本，Tab 切换）
- 示例填充按钮（加载 templates/requirement-template.json 示例）
- 产品 ID / 模块 / 优先级表单字段
- 提交按钮（调 `/api/requirements/upload`）
- 提交后：若需澄清 → 跳 `/clarify/:id`；若直接通过 → 跳 `/generate/:id`

#### T9.3 Clarify 页面 `/clarify/:id`

- SRR 摘要展示（标题/优先级/流程步骤）
- 问题列表（按 Critical/Important/Optional 分组，颜色区分）
- 每个问题：问题描述 + 输入框（文本/选择）
- field_conflicts 展示（KB 补全 vs 用户输入，冲突高亮）
- 提交按钮（调 `/api/requirements/{id}/clarify`）
- 响应：若仍有问题 → 显示下一轮；若通过 → 跳 `/generate/:id`
- 轮次指示器（第 X/3 轮）

#### T9.4 Generate 页面 `/generate/:id` ⭐ 核心交互

- EventSource 连接 `/api/generate`（POST 改用 fetch + ReadableStream，因 EventSource 仅支持 GET）
- 进度条（顶部，百分比 + 当前阶段）
- 5 个阶段卡片（parse/gate/retrieve/generate/review）：
  - 待执行：灰色
  - 执行中：蓝色脉冲动画 + spinner
  - 已完成：绿色 + ✓
- 实时日志流（终端风格，自动滚动）
- 完成后：显示用例数量 + "去审核"按钮 → `/review/:id`
- 错误处理：红色告警 + 重试按钮

**SSE 客户端实现**（关键代码）：
```typescript
// 因 EventSource 仅支持 GET，用 fetch + ReadableStream 实现 POST SSE
async function* sseStream(url: string, body: any) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const reader = resp.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n\n');
    buffer = lines.pop() || '';
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        yield JSON.parse(line.slice(6));
      }
    }
  }
}
```

#### T9.5 Review 页面 `/review/:id`

- 左侧：用例列表（表格，可筛选 type/priority/status/confidence）
- 右侧：选中用例的详情编辑表单
  - 标题、类型、优先级（下拉）
  - 步骤表格（可增删行）
  - 预期结果、前置条件
  - 置信度显示（final_confidence）
  - 追溯信息（related_requirement）
- 底部操作栏：
  - 保存编辑（PUT `/api/testcases/{id}`）
  - 接受/拒绝（批量）
  - 导出（弹窗选格式 → `/api/export`）
- 覆盖率矩阵视图（按 type 统计）

#### T9.6 History 页面 `/history`

- 需求历史列表（表格：ID/标题/产品/模块/状态/创建时间）
- 筛选器（产品/模块/状态/日期范围）
- 点击行 → 展开批次详情（用例数/模型/成本/耗时）
- 用例反馈入口（→ 跳转 Review）

#### T9.7 Settings 页面 `/settings`

- LLM 配置表单：
  - Provider 选择（DeepSeek/OpenAI）
  - API Key 输入（密码框，脱敏显示）
  - Base URL（可选）
  - Model 名称
- "测试连通性"按钮（调 `/api/settings/llm/test`，显示成功/失败）
- 保存按钮（PUT `/api/settings/llm`）
- 质量门禁阈值显示（只读，从 /api/health 获取）
- 数据库扩展方案说明（折叠面板，介绍 PostgreSQL/pgvector/Milvus 迁移路径）

### 阶段10：集成测试与文档（T10）

#### T10.1 端到端验证

**后端启动验证**：
```bash
cd backend
uvicorn app.main:app --reload
# 验证 /api/health → 200
# 验证 /docs → Swagger 可访问
# 验证 /api/requirements/upload → 返回 req_id + SRR
# 验证 /api/generate → SSE 流式响应
# 验证 /api/testcases → 列表
# 验证 /api/export → 文件下载
```

**前端启动验证**：
```bash
cd frontend
npm run dev
# 验证 7 个页面可访问
# 验证完整流程：输入需求 → 澄清 → 生成 → 审核 → 导出
```

**构建验证**：
```bash
cd frontend
npm run build
# 无 TypeScript 错误，无构建警告
```

#### T10.2 文档

**T10.2.1 更新 README.md**：
- 项目简介
- 技术栈
- 快速启动（后端 + 前端）
- 环境变量配置（.env）
- API 文档链接
- 架构文档导航

**T10.2.2 创建 `docs/06-database-extension.md`**：
- SQLite → PostgreSQL 迁移指南
- ChromaDB → pgvector → Milvus 三阶段路径
- 配置切换方法

**T10.2.3 代码注释规范**：
所有新增 Python 文件遵循：
```python
"""模块用途简述。

详细说明（依据文档章节）。

设计依据：
- docs/XX §Y.Z
"""

class/def xxx:
    """用途简述。
    
    详细说明。
    
    Args:
        param1: 参数说明
        param2: 参数说明
    
    Returns:
        返回值说明
    
    Raises:
        ExceptionType: 触发条件
    
    依据: docs/XX §Y.Z
    """
```

---

## 五、假设与决策

### D1: chromadb 修复方案
- **决策**：降级 NumPy 到 1.26.4 + 回退 chromadb 默认 ONNX 嵌入
- **理由**：sentence-transformers/torch 链路损坏，ONNX 路径最轻量
- **风险**：NumPy 降级可能影响其他包，需验证

### D2: Clarify 流程采用 API 驱动
- **决策**：不使用 LangGraph interrupt，用 REST API 多轮交互
- **理由**：更简单可靠，前端实现更直观，避免 LangGraph interrupt 的状态持久化复杂性
- **实现**：`POST /clarify` 提交答案 → 返回下一轮问题或通过信号

### D3: Agent 数量为 5
- **决策**：ReqAgent, ClarifyAgent, ImpactAnalysisAgent, GenAgent, ReviewAgent
- **说明**：QualityGate 是规则门禁（非 LLM Agent），MatchAgent 检索逻辑合并到 orchestrator 的 retrieve_node
- **依据**：实现计划 T2.5-T2.9 + doc-00 §4

### D4: SSE 实现用 sse-starlette
- **决策**：后端用 sse-starlette 的 EventSourceResponse，前端用 fetch + ReadableStream
- **理由**：POST 请求不支持原生 EventSource，需 fetch 流式读取

### D5: 前端 shadcn/ui 组件按需引入
- **决策**：只安装实际使用的组件（button/card/input/table/dialog/select/badge/progress/tabs/textarea/sonner）
- **理由**：减小包体积，加快构建

### D6: 测试中 Agent 单元测试 mock LLM
- **决策**：测试时 mock `llm_client.call_json` 返回固定 JSON
- **说明**：这是测试隔离手段（非模拟业务数据），用于验证 Agent 的数据处理逻辑，不违反"不使用模拟数据"规则
- **端到端测试**：使用真实 LLM API（需配置 Key）

### D7: 优化需求检测在 ReqAgent
- **决策**：ReqAgent 在解析时检测优化意图，填充 `optimization_context`
- **依据**：项目记忆 + doc-01 §6

### D8: 冷启动阈值动态调整
- **决策**：kb_empty 时 QualityGate 阈值降为 0.60，正常 0.75
- **依据**：config.py 已定义 + doc-01 §5.2

### D9: 数据库扩展方案文档化
- **决策**：MVP 用 SQLite + ChromaDB，创建 `docs/06-database-extension.md` 说明迁移路径
- **依据**：项目记忆（ChromaDB → pgvector → Milvus 三阶段）

### D10: 代码注释全中文
- **决策**：所有 docstring、注释用中文，函数名/变量名用英文
- **依据**：用户规则

---

## 六、验证步骤汇总

### V1: 阶段1完成验证（阻塞修复）
```bash
cd backend
python -c "import chromadb; print('chromadb ok')"  # 成功
python -c "from app.main import app; print('main ok')"  # 成功
uvicorn app.main:app --port 8000  # 启动无异常
curl http://localhost:8000/api/health  # 200 + JSON
```

### V2: 阶段2-4完成验证（核心逻辑）
```bash
cd backend
python -c "from app.core.channel_selector import select_channel; print('ok')"
python -c "from app.core.quality_gate import QualityGate; print('ok')"
python -c "from app.agents.req_agent import ReqAgent; print('ok')"
python -c "from app.core.orchestrator import Orchestrator; print('ok')"
pytest tests/test_channel_selector.py tests/test_quality_gate.py -v
```

### V3: 阶段5-6完成验证（API + 导出）
```bash
cd backend
uvicorn app.main:app --port 8000
# Swagger /docs 所有端点可见
# POST /api/requirements/upload 返回 200 + req_id
# POST /api/generate 返回 SSE 流
# POST /api/export 返回文件
pytest tests/test_api.py tests/test_exporters.py -v
```

### V4: 阶段8-9完成验证（前端）
```bash
cd frontend
npm run build  # 无错误
npm run dev
# 浏览器访问 7 个页面
# 完整流程走通：输入 → 澄清 → 生成 → 审核 → 导出
```

### V5: 阶段10完成验证（端到端）
```bash
# 后端 + 前端同时启动
# 配置真实 LLM Key
# 输入真实需求文档
# 验证生成符合 UTCF 规范的测试用例
# 验证导出 Markdown/Gherkin/pytest 格式正确
# 验证编辑、管理功能正常
```

---

## 七、执行顺序与依赖图

```
阶段1 (修复阻塞)
  ↓
阶段2 (核心组件: channel_selector + quality_gate + prompts)
  ↓
阶段3 (5个 Agent)
  ↓
阶段4 (LangGraph 编排器)  ← 依赖 阶段2+3
  ↓
阶段5 (API 路由)  ← 依赖 阶段4
  │
  ├─ 阶段6 (导出器)  ← 可与阶段5并行
  │
  └─ 阶段7 (后端测试)  ← 依赖 阶段5+6
       ↓
阶段8 (前端基础)  ← 可与阶段6-7并行
  ↓
阶段9 (前端7页面)  ← 依赖 阶段5(API就绪) + 阶段8
  ↓
阶段10 (集成测试 + 文档)  ← 依赖所有
```

**并行机会**：
- 阶段6（导出器）与阶段5（API）可并行
- 阶段8（前端基础）可与阶段6-7并行

---

## 八、风险与缓解

### R1: NumPy 降级影响其他包
- **缓解**：降级后立即运行 `pip check` 验证依赖一致性；若有冲突，针对性修复

### R2: LLM API 不稳定
- **缓解**：llm_client.py 已实现断路器+重试+故障转移；测试时用 mock，端到端验证时用真实 Key

### R3: LangGraph 版本兼容性
- **缓解**：langgraph==0.2.68 已锁定，API 稳定；如遇问题参考官方 0.2.x 文档

### R4: 前端 SSE 实现复杂度
- **缓解**：提供完整的 fetch + ReadableStream 实现代码（§阶段9 T9.4）

### R5: chromadb 嵌入模型首次下载
- **缓解**：首次 `import chromadb` 会下载 ONNX 模型（~30MB），启动时预热；后续从缓存加载

---

## 九、交付物清单

### 后端
- [ ] 修复后的 vector_store.py（chromadb 可导入）
- [ ] core/channel_selector.py, quality_gate.py, orchestrator.py
- [ ] agents/ 5 个 Agent 文件
- [ ] prompts/ 5 个提示词文件
- [ ] api/ 6 个路由文件
- [ ] exporters/ 4 个导出器文件
- [ ] tests/ 7 个测试文件
- [ ] requirements.txt 更新（锁定 numpy）

### 前端
- [ ] 完整的 Vite + React + TS 工程
- [ ] 7 个页面组件
- [ ] 布局组件（Sidebar/TopBar/AppLayout）
- [ ] API 层（axios + SSE 封装）
- [ ] zustand 状态管理（3 个 store）
- [ ] shadcn/ui 组件集成

### 文档
- [ ] 更新的 README.md
- [ ] docs/06-database-extension.md
- [ ] 所有代码含中文 docstring

### 验证
- [ ] 后端 pytest 全部通过
- [ ] 前端 npm run build 无错误
- [ ] 端到端流程走通（真实 LLM）
- [ ] /api/health 返回 200
- [ ] /docs Swagger 可访问
