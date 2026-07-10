# AI-Test-Gen 实现计划（续作版）

> 本计划基于对现有代码库的真实探索，承接上一轮已批准的方案。目标：开发一款 AI 驱动测试用例自动生成程序，满足美观 UI、直观流程、详尽注释三大要求。

---

## 一、摘要（Summary）

基于 `docs/00-05` 架构文档，构建一个 **Web 应用（React + FastAPI）**，将非结构化软件需求经 5 个 Agent 流水线（ReqAgent → ClarifyAgent → QualityGate → ImpactAnalysisAgent → GenAgent → ReviewAgent）自动转化为符合 UTCF 规范的可执行测试用例，支持 Markdown/Gherkin/pytest 导出与编辑管理。

**已批准的关键决策**（承上一轮，无需重新确认）：
- 形态：Web 应用（React 18 前端 + FastAPI 后端）
- LLM：真实 API（用户在 `.env` 提供 Key，DeepSeek 主力 + OpenAI 备用故障转移）
- 版本：完整版（5 Agent + 通道选择 FAST/FULL）
- 数据库：SQLite + ChromaDB（嵌入式向量库），后续给出 PostgreSQL/pgvector/Milvus 扩展方案

**用户硬性规则**：
- 提交最终结果前必须先测试验证
- 不使用模拟数据（确需时须先请求允许）
- 中文回答与中文代码注释
- 不变更整体逻辑，可优化但不能改变所需结果

---

## 二、当前状态分析（Current State Analysis）

### 2.1 已完成（阶段1基础设施，约70%）

| 文件 | 行数 | 状态 | 说明 |
|------|------|------|------|
| `backend/requirements.txt` | 39 | ✅ | 依赖锁定完整（fastapi/litellm/langgraph/chromadb/sqlalchemy 等） |
| `backend/.env.example` | 35 | ✅ | 覆盖全部配置项 |
| `backend/app/__init__.py` | 13 | ✅ | 版本号 1.0.0 |
| `backend/app/config.py` | 150 | ✅ | Settings 类 + `sqlite_url`/`llm_config`/`fallback_llm_config`/`ensure_data_dirs` |
| `backend/app/utils/logger.py` | 50 | ✅ | loguru 控制台彩色 + 文件轮转 |
| `backend/app/utils/id_gen.py` | 83 | ✅ | 4 个 ID 生成函数（REQ/TC/BATCH/ERR） |
| `backend/app/models/srr.py` | 282 | ✅ | 21 个类（含 OptimizationContext、FieldConflict、ValidationResult） |
| `backend/app/models/utcf.py` | 194 | ✅ | 11 个类（含 `gen_confidence` vs `confidence` 区分、ReviewReport） |
| `backend/app/models/api_schemas.py` | 140 | ⚠️ | 12 个模式；`Literal` 导入在文件末尾（依赖延迟求值，运行不报错但不规范） |
| `backend/app/db/models.py` | 223 | ✅ | 7 张 ORM 表（SQLite 兼容：JSON/DateTime/普通索引） |
| `backend/app/db/database.py` | 83 | ⚠️ | 引擎/Session OK，但 `init_db()` 导入不存在的 `seed.py` → 启动必崩 |
| `backend/app/db/vector_store.py` | 351 | ⚠️ | 4 Collection 封装完整；`add_pattern()` 第207行 `metadatas` 传 dict 而非 `[dict]` → 运行时报错 |

### 2.2 缺失模块

| 层 | 缺失内容 | 影响 |
|----|---------|------|
| 基础设施 | `core/llm_client.py`、`db/seed.py`、`main.py` | 应用无法启动 |
| 业务逻辑 | `agents/`（5个Agent）、`core/`（quality_gate、channel_selector、orchestrator、prompts） | 核心功能全无 |
| API 路由 | `api/`（requirements/generate/testcases/knowledge/settings/export） | 前后端无法通信 |
| 导出器 | `exporters/`（markdown/gherkin/pytest） | 无法导出 |
| 前端 | `frontend/`（Vite+React+TS+Tailwind+shadcn/ui + 7页面） | 无 UI |
| 测试/文档 | `tests/`、README | 无法验证 |

### 2.3 待修复的2个运行时缺陷

1. **`db/seed.py` 缺失** → `database.py:init_db()` 执行 `from app.db.seed import seed_patterns` 抛 `ImportError`，应用启动即崩
2. **`vector_store.py:add_pattern()`** → `metadatas` 应为 `[dict]` 列表以匹配 `ids=[...]`/`documents=[...]` 的形状

### 2.4 设计依据锚点（已核实）

- 通道选择逻辑：`docs/00-architecture-overview.md` §9.1 `select_channel()`（P0/步骤>5/优化_context → FULL）
- SRR Schema：`docs/01-requirement-parsing.md` §2.2（`product_id` + `modules[]` 复数）
- QualityGate 冷启动阈值：`docs/01` §5.2（0.75 → 冷启动 0.60）
- ClarifyAgent 三级检测 + 字段冲突优先级：`docs/01` §4.3-§4.4（P0-P4 五级）
- ImpactAnalysisAgent 成本控制：`docs/03` §1.5（MAX_GRAPH_NODES=5000、深度3、超时5s）
- GenAgent `gen_confidence` vs ReviewAgent `final_confidence`：`docs/03` §5.2
- UTCF Schema：`docs/04-output-format.md` §2.1（ID 正则 `^TC-[A-Z]+-\d{4}-\d{4}$`）
- 死锁预防 5 策略：`docs/03` §6.7
- 变异测试 8 操作符：`docs/03` §5.3

---

## 三、假设与决策（Assumptions & Decisions）

| # | 决策 | 理由 |
|---|------|------|
| D1 | 承接已批准的4项架构决策（见摘要） | 用户上轮已明确，无需重新确认 |
| D2 | LLM 客户端基于 LiteLLM，实现故障转移+指数退避全抖动重试+断路器 | 对应 `docs/03` §6.2-6.4 |
| D3 | LangGraph 编排 DAG：入口 → select_channel → 条件分支（FAST/FULL） | 对应 `docs/00` §3 |
| D4 | 前端用 Vite+React18+TS+Tailwind+shadcn/ui，状态管理用 zustand | 现代标准，shadcn/ui 美观且可定制 |
| D5 | 生成进度用 SSE 推送（FastAPI StreamingResponse） | 对应 `docs/00` §9.2 Phase 2 推荐 |
| D6 | 修复 `id_gen.py` 并发隐患：加 `asyncio.Lock` 或改用进程内原子计数 | FastAPI 异步并发下避免重复 ID |
| D7 | 导出器为纯函数模块，输入 UTCF，输出字符串 | 解耦，便于测试 |
| D8 | 种子数据写入 test_patterns 表 + 向量库（约8-10个通用测试模式） | 对应 `templates/knowledge-base-schema.json` seed_patterns |
| D9 | 前端7页面：Home/RequirementInput/Clarify/Generate/Review/History/Settings | 对应计划 §五 |
| D10 | 测试：后端 pytest（Agent 单测 + API 集成），前端暂不强制单测 | 满足"提交前先测试"规则 |

---

## 四、后端实现计划（Proposed Changes — Backend）

### 阶段 1：补全基础设施 + 修复缺陷

#### T1.1 修复 `backend/app/db/vector_store.py`（bug）
- **what**：`add_pattern()` 第207行 `metadatas={...}` → `metadatas=[{...}]`
- **why**：ChromaDB `upsert` 要求 ids/documents/metadatas 三者同为列表且长度一致
- **how**：Edit 工具精确替换该行

#### T1.2 修复 `backend/app/models/api_schemas.py`（风格）
- **what**：将文件末尾的 `from typing import Literal` 移至文件顶部
- **why**：规范导入位置，避免依赖 `from __future__ import annotations` 延迟求值
- **how**：Edit 删除末尾导入 + 顶部新增导入

#### T1.3 修复 `backend/app/utils/id_gen.py`（并发隐患，D6）
- **what**：为模块级计数器加 `threading.Lock`，保证递增原子性
- **why**：FastAPI 异步 + 线程池并发下避免同秒重复 ID
- **how**：Edit 包裹计数器自增逻辑

#### T1.4 创建 `backend/app/core/__init__.py`
- 空包初始化 + 文档字符串

#### T1.5 创建 `backend/app/core/llm_client.py`（⭐核心，D2）
- **what**：LiteLLM 封装类 `LLMClient`
- **关键方法**：
  - `async call(prompt, temperature=0.3, response_format=None, model=None) -> str` — 主入口
  - `async call_json(prompt, schema_hint) -> dict` — 强制 JSON 输出 + 解析校验
  - 内部：故障转移（主→备，连续失败3次临时禁用10分钟）、指数退避全抖动重试（最多5次）、断路器（错误率>20%降级）
- **why**：所有 Agent 的 LLM 调用统一入口，保障稳定性（`docs/03` §6.2-6.4）
- **how**：基于 `litellm.acompletion`，用 `tenacity` 实现重试，自定义断路器状态机
- **导出**：`llm_client` 全局单例

#### T1.6 创建 `backend/app/db/seed.py`（D8）
- **what**：`async seed_patterns(session, vstore)` 写入约8-10个通用测试模式（CRUD/状态机/认证/校验/工作流/并发/异步/一致性）
- **why**：修复 `database.py:init_db()` 的 ImportError；为 ClarifyAgent 遗漏检测 + GenAgent 模式复用提供基础
- **how**：定义模式 JSON 列表 → upsert 到 `test_patterns` 表 → `vstore.add_pattern()` 入向量库（幂等，按 name 去重）
- **数据来源**：`templates/knowledge-base-schema.json` 的 `seed_patterns` 列表（真实模式定义，非模拟数据）

#### T1.7 创建 `backend/app/main.py`（应用入口）
- **what**：FastAPI 应用实例
- **关键内容**：
  - `lifespan` 上下文：启动时 `init_db()` + `setup_logger()` + 向量库初始化；关闭时清理
  - CORS 中间件（白名单 `settings.FRONTEND_URL`）
  - 全局异常处理器（捕获异常 → 统一 `ApiResponse` 格式）
  - 请求日志中间件（记录 method/path/duration）
  - 健康检查 `GET /api/health`
  - 路由注册（阶段3填充）
- **why**：应用可启动是后续所有开发的前提

#### T1.8 阶段1验证
- 运行 `uvicorn app.main:app`，确认启动无 ImportError
- `GET /api/health` 返回 200
- 检查 `data/aitestgen.db` 创建 + `test_patterns` 表有种子数据

---

### 阶段 2：核心组件 + 5 个 Agent + LangGraph 编排

#### T2.1 创建 `backend/app/core/channel_selector.py`（D3）
- **what**：`select_channel(srr: dict) -> dict`
- **实现**：直接移植 `docs/00` §9.1 的修复版函数（priority/steps/optimization_context 判定）
- **返回**：`{channel, enable_clarify, enable_impact_analysis, enable_multi_type_generation, enable_full_review}`

#### T2.2 创建 `backend/app/core/quality_gate.py`
- **what**：`QualityGate` 类
- **方法**：
  - `evaluate(srr) -> {pass_score, cold_start, checks: [...], passed: bool}`
  - `_resolve_pass_score()`：冷启动检测（KB 为空 → 0.60，否则 0.75）
  - 7 项检查：Schema 校验、关键字段完整性、歧义性评分、遗漏评分、追溯性、一致性、优先级合理性
- **依据**：`docs/01` §5.2 + `docs/03` 死锁预防（ClarifyLoopGuard 最大3轮）

#### T2.3 创建 `backend/app/core/prompts/`（Prompt 模板包）
- **文件**：`__init__.py`、`req_parse.py`、`clarify.py`、`gen.py`、`review.py`、`impact.py`
- **what**：每个文件导出 `SYSTEM_PROMPT` + `build_user_prompt(context) -> str` 函数
- **why**：Prompt 与代码解耦，便于版本管理和 A/B 测试
- **依据**：`docs/01` §3.1 ReqAgent Prompt、`docs/03` GenAgent/ReviewAgent 设计

#### T2.4 创建 `backend/app/agents/__init__.py`
- 导出 5 个 Agent 类

#### T2.5 创建 `backend/app/agents/req_agent.py`
- **what**：`ReqAgent.parse(raw_text, source_type) -> SRR`
- **流程**：格式检测 → 预处理 → LLM 结构化解析（`prompts.req_parse`）→ Pydantic 校验 → 实体链接 → test_points_hint 推断
- **优化需求检测**：若文本含优化语义 → 标记 `optimization_context`（对应 `docs/01` §6）

#### T2.6 创建 `backend/app/agents/clarify_agent.py`
- **what**：`ClarifyAgent.clarify(srr, history) -> {questions, augmented_srr, field_conflicts, quality_score}`
- **三级检测**：`check_completeness`（Checklist）、`detect_ambiguity`（规则+LLM）、`detect_gaps`（KB 对比）
- **KB 补全**：`kb_augment_requirement` 含 `_resolve_field_conflict`（P0-P4 优先级，对应 `docs/01` §4.4.1）
- **问题生成**：合并 field_conflicts + 未解决问题 → 一次性问题列表（最多5个，Critical→Important→Optional 排序）
- **死锁预防**：`ClarifyLoopGuard` 限制最多3轮

#### T2.7 创建 `backend/app/agents/impact_analysis_agent.py`
- **what**：`ImpactAnalysisAgent.analyze(srr) -> dict`
- **依据**：`docs/03` §1.5 完整设计
- **成本控制**：MAX_GRAPH_NODES=5000、MAX_TRAVERSAL_DEPTH=3、TIMEOUT_SECONDS=5.0、`asyncio.wait_for` 超时降级
- **新需求**：`optimization_context` 为空 → 返回 `{is_optimization: False}` 跳过
- **优化需求**：版本链遍历 + 依赖图谱（用 `req_dependencies` 表 recursive CTE 替代 Neo4j）+ 跨模块分析 + case_operations + regression_scope

#### T2.8 创建 `backend/app/agents/gen_agent.py`
- **what**：`GenAgent.generate(srr, impact, kb_results, channel) -> UTCF`
- **多维度生成**：FAST 通道单批生成；FULL 通道并行生成 functional/boundary/exception/security 四类
- **追溯标注**：每个 TestCase 关联 `source_requirement` + `requirement_field`
- **置信度**：写入 `gen_metadata.gen_confidence`（自评，权重0.1）
- **成本控制**：用例数上限、单次 LLM 调用 token 预算

#### T2.9 创建 `backend/app/agents/review_agent.py`
- **what**：`ReviewAgent.review(utcf, srr) -> ReviewReport`
- **六维审核**：完整性/去重/可执行/一致性/优先级/覆盖率
- **追溯性验证**：每个用例必须有需求依据
- **最终置信度**：`calculate_confidence()` 计算 `final_confidence`，写入 `TestCase.confidence`（区别于 `gen_confidence`）
- **采样策略**：FAST 通道5%采样，FULL 通道100%审核

#### T2.10 创建 `backend/app/core/orchestrator.py`（⭐LangGraph DAG，D3）
- **what**：`build_graph() -> CompiledGraph` + `async run_pipeline(raw_text, ...) -> AsyncGenerator[SSEEvent]`
- **节点**：`parse_node` → `select_channel_node` → 条件分支
  - FULL：`clarify_node`（含人机交互中断点）→ `quality_gate_node`（不达标回 clarify）→ `impact_node` → `retrieve_node` → `gen_node` → `review_node`
  - FAST：`quality_gate_node` → `retrieve_node` → `gen_node` → `review_node`
- **SSE 推送**：每个节点完成后 yield `{stage, message, progress, result?}`
- **人机交互**：ClarifyAgent 需人工回答时，LangGraph `interrupt` 暂停，前端提交答案后 `Command(resume=...)` 继续
- **回流存储**：`review_node` 后写入 SQLite + 向量库 + 追溯矩阵

#### T2.11 阶段2验证
- 单元测试每个 Agent（用真实 LLM 或用户提供的 Key）
- `pytest tests/test_agents.py` 通过
- 手动调用 `run_pipeline` 验证 SSE 事件流

---

### 阶段 3：API 路由层

#### T3.1 创建 `backend/app/api/__init__.py`
- 导出 `api_router`（聚合所有子路由）

#### T3.2 创建 `backend/app/api/requirements.py`
- **端点**：
  - `POST /api/requirements/upload` — 上传需求文本，返回 requirement_id + 初始 SRR
  - `GET /api/requirements` — 列表（分页）
  - `GET /api/requirements/{id}` — 详情
  - `POST /api/requirements/{id}/clarify` — 提交澄清答案，返回下一轮或通过
  - `GET /api/requirements/{id}/clarify/questions` — 获取待回答问题

#### T3.3 创建 `backend/app/api/generate.py`（⭐SSE，D5）
- **端点**：`POST /api/generate` → `StreamingResponse(media_type="text/event-stream")`
- **流程**：接收 requirement_id → 调用 `orchestrator.run_pipeline` → 逐个 yield SSE 事件
- **事件格式**：`data: {stage, message, progress, result?}\n\n`

#### T3.4 创建 `backend/app/api/testcases.py`
- **端点**：`GET /api/testcases`（列表筛选）、`GET /api/testcases/{id}`、`PUT /api/testcases/{id}`（编辑）、`DELETE /api/testcases/{id}`、`POST /api/testcases/{id}/feedback`（反馈回流）

#### T3.5 创建 `backend/app/api/knowledge.py`
- **端点**：`GET /api/knowledge/stats`（返回 `KnowledgeStats`）、`GET /api/knowledge/patterns`

#### T3.6 创建 `backend/app/api/settings.py`
- **端点**：`GET /api/settings/llm`（返回脱敏配置）、`PUT /api/settings/llm`（更新）、`POST /api/settings/llm/test`（测试连通性）

#### T3.7 创建 `backend/app/api/export.py`
- **端点**：`POST /api/export`（接收 case_ids + format，返回文件流或文本）

#### T3.8 更新 `backend/app/main.py`
- 注册 `api_router`，补充 OpenAPI 文档元数据

#### T3.9 阶段3验证
- `pytest tests/test_api.py` 集成测试
- Swagger UI (`/docs`) 手动验证各端点

---

### 阶段 4：导出器

#### T4.1 创建 `backend/app/exporters/__init__.py`
- 导出 `export_to_markdown(utcf) -> str`、`export_to_gherkin(utcf) -> str`、`export_to_pytest(utcf) -> str`

#### T4.2 创建 `backend/app/exporters/markdown_exporter.py`
- **what**：UTCF → Markdown 表格（标题/步骤/预期/前置/数据）
- **依据**：`docs/04` §3 Markdown 模板

#### T4.3 创建 `backend/app/exporters/gherkin_exporter.py`
- **what**：UTCF → Gherkin（Feature/Scenario/Given/When/Then）
- **依据**：`docs/04` §4 Gherkin 转换规则

#### T4.4 创建 `backend/app/exporters/pytest_exporter.py`
- **what**：UTCF → pytest 测试函数（参数化 + assert）
- **依据**：`docs/04` §5 pytest 转换规则

#### T4.5 阶段4验证
- `pytest tests/test_exporters.py`：给定样例 UTCF，验证三种格式输出正确

---

## 五、前端实现计划（Proposed Changes — Frontend）

### 阶段 5：前端基础搭建

#### T5.1 初始化 `frontend/`
- Vite + React 18 + TypeScript 模板
- 安装：`tailwindcss`、`shadcn/ui`、`react-router-dom`、`zustand`、`axios`、`lucide-react`、`@tanstack/react-query`、`sonner`（toast）

#### T5.2 配置 Tailwind + shadcn/ui
- `tailwind.config.ts`（主题色：现代蓝紫渐变 + 中性灰）、`components.json`、`lib/utils.ts`
- 主题：浅色为主，支持暗色切换；圆角、阴影、间距符合现代 UI/UX

#### T5.3 布局组件
- `components/layout/AppLayout.tsx` — 侧边栏 + 顶栏 + 内容区
- `components/layout/Sidebar.tsx` — 导航菜单（7页面入口 + 图标）
- `components/layout/TopBar.tsx` — 标题 + 知识库状态徽章 + 主题切换

#### T5.4 路由 `App.tsx`
- `/` → Home、`/requirement` → RequirementInput、`/clarify/:id` → Clarify、`/generate/:id` → Generate、`/review/:id` → Review、`/history` → History、`/settings` → Settings

#### T5.5 API 层 `lib/api.ts`
- axios 实例（baseURL `/api`，拦截器统一错误处理）
- 封装所有后端端点调用 + SSE EventSource 封装

#### T5.6 状态管理 `stores/`
- `useRequirementStore`、`useGenerateStore`、`useSettingsStore`（zustand）

#### T5.7 公共组件 `components/ui/`
- shadcn/ui 基础组件：Button、Card、Input、Textarea、Select、Table、Dialog、Badge、Progress、Tabs、Toast

### 阶段 6：前端页面实现（7页面）

#### T6.1 `pages/Home.tsx`
- 系统介绍 + 流程引导卡片（4步：输入需求→澄清→生成→审核）+ 快速入口 + 知识库统计

#### T6.2 `pages/RequirementInput.tsx`
- 大文本框 + 格式选择（Markdown/JSON/纯文本）+ 示例填充按钮 + 提交 → 跳转 Clarify 或 Generate

#### T6.3 `pages/Clarify.tsx`
- 展示 SRR 摘要 + 问题列表（按优先级分组，Critical/Important/Optional）+ 批量回答输入 + field_conflicts 展示 → 提交 → 显示下一轮或通过

#### T6.4 `pages/Generate.tsx`（⭐SSE 实时反馈）
- EventSource 连接 `/api/generate` + 进度条（按 progress 百分比）+ 阶段卡片（parse/gate/retrieve/generate/review）+ 实时日志流 + 完成后跳转 Review

#### T6.5 `pages/Review.tsx`
- UTCF 用例列表（左侧树/表）+ 用例详情编辑（右侧表单：标题/步骤/预期/优先级/类型）+ 批量操作（接受/拒绝/导出）+ 追溯矩阵视图 + 覆盖率矩阵

#### T6.6 `pages/History.tsx`
- 需求历史列表（筛选：产品/模块/状态/日期）+ 批次详情 + 用例反馈入口

#### T6.7 `pages/Settings.tsx`
- LLM 配置表单（provider/key/base_url/model）+ 测试连通性按钮 + 质量门禁阈值调整 + 数据库扩展方案说明

#### T6.8 阶段5-6验证
- `npm run build` 无 TS 错误
- 手动走通完整流程：输入需求 → 澄清 → 生成（看 SSE 进度）→ 审核 → 导出

---

## 六、集成测试与文档（阶段7）

#### T7.1 后端测试 `backend/tests/`
- `conftest.py`（测试夹具：内存 SQLite + 临时 ChromaDB）
- `test_agents.py`（各 Agent 单测，可 mock LLM 或用真实 Key）
- `test_api.py`（API 集成测试，FastAPI TestClient）
- `test_exporters.py`（导出格式验证）
- `test_quality_gate.py`（阈值与冷启动）

#### T7.2 端到端验证
- 启动后端 + 前端
- 用真实需求文档（如 `docs/01` §2.2 登录功能示例）走完整链路
- 验证：SRR 解析正确 → 澄清问题合理 → 生成用例符合 UTCF → 审核报告完整 → 三种导出格式可用

#### T7.3 文档
- 更新 `README.md`：快速启动、环境变量、API 文档链接、架构图
- 创建 `docs/06-database-extension.md`：SQLite→PostgreSQL、ChromaDB→pgvector→Milvus 扩展方案

#### T7.4 最终验证（满足用户"提交前先测试"规则）
- 全部测试通过
- 端到端流程跑通
- 提交结果

---

## 七、执行顺序与依赖

```
阶段1（修复+补全基础设施）─┐
                          ├─→ 阶段2（Agents+核心）─┐
                          │                         ├─→ 阶段3（API）─→ 阶段4（导出器）─┐
                          │                         │                                   ├─→ 阶段7（测试+文档）
                          └─→ 阶段5（前端基础）─→ 阶段6（前端页面）──────────────────┘
```

每阶段完成即验证，符合"提交前先测试"规则。

---

## 八、验证步骤汇总

| 阶段 | 验证命令/动作 | 通过标准 |
|------|--------------|---------|
| 1 | `uvicorn app.main:app` + `GET /api/health` | 启动无错 + 健康检查200 + 种子数据写入 |
| 2 | `pytest tests/test_agents.py` | 5 Agent 单测通过 |
| 3 | `pytest tests/test_api.py` + Swagger 手动测试 | API 端点全部可用 |
| 4 | `pytest tests/test_exporters.py` | 三种导出格式输出正确 |
| 5 | `npm run build` | 无 TS 错误 |
| 6 | 手动走通完整流程 | 输入→澄清→生成→审核→导出 全链路通畅 |
| 7 | 全部测试 + 端到端 | 满足提交标准 |

---

## 九、风险与缓解

| 风险 | 缓解 |
|------|------|
| LLM API Key 未配置导致无法测试 | 阶段2起需用户提供 `.env`；测试可 mock LLM 但主流程用真实 Key |
| LangGraph 中断-恢复机制复杂 | ClarifyAgent 人机交互用 LangGraph `interrupt` + 前端轮询/EventSource |
| 前端工作量大 | 复用 shadcn/ui 组件库降低工作量；7页面分优先级（Generate/Review 核心） |
| ChromaDB 嵌入模型需下载 | 用 ChromaDB 默认 embedding（all-MiniLM-L6-v2）或配置 bge-large-zh |
| 并发 ID 冲突 | T1.3 修复 id_gen 加锁 |

---

## 十、注

- 本计划所有文件路径、类名、方法签名均基于 Phase 1 真实探索的现有代码（`backend/app/` 下 13 个文件已读取确认）
- 所有设计依据均标注对应架构文档章节，可追溯
- 计划为决策完整型：执行者无需再做架构选择，按阶段顺序实现即可
