# 11 — 架构设计文档与历次变更总览

> AI 测试用例生成系统整体架构设计逻辑、系统结构说明与版本演进  
> 版本: v2.2 | 文档日期: 2026-07-21

本文档是系统的**架构主索引**，整合整体架构设计逻辑、系统结构说明、以及自 v1.1.0 → v2.2.0 的历次架构变更要点。其他分册文档（00–10）按主题展开细节，本文档提供横向串联与纵向演进视图。

---

## 目录

1. [整体架构设计逻辑](#1-整体架构设计逻辑)
2. [系统结构说明](#2-系统结构说明)
3. [核心数据流与 Agent 流水线](#3-核心数据流与-agent-流水线)
4. [数据模型与存储层](#4-数据模型与存储层)
5. [稳定性与安全设计](#5-稳定性与安全设计)
6. [历次版本变更架构设计](#6-历次版本变更架构设计)
7. [架构演进路线图](#7-架构演进路线图)

---

## 1. 整体架构设计逻辑

### 1.1 系统定位

构建一套**"需求驱动 + AI 自动生成测试用例"**平台，核心理念：

> 将非结构化/半结构化的软件需求文档，通过 LLM 解析为结构化表示（SRR），经需求澄清闭环确保需求质量，结合历史知识库的上下文检索，自动生成高质量、可执行、可追溯、可回流的测试用例（UTCF）。

**目标场景**：中大型软件项目中的功能测试、回归测试、API 测试用例自动生成，减少手工编写工作量 60%+。

### 1.2 五层架构全景

```
┌─────────────────────────────────────────────────────────────────────┐
│                     接入层 (Access Layer)                           │
│  Web UI (React 18) | REST API (FastAPI) | SSE 流式                  │
│  🔒 L1: Token Bucket 限速 | JWT 认证 | RBAC 角色权限                │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────┐
│                   编排层 (Orchestration Layer)                      │
│  LangGraph DAG Pipeline:                                            │
│  ReqAgent → ClarifyAgent → QualityGate → ImpactAgent                │
│            → GenAgent → ReviewAgent                                 │
│  通道选择: FAST (简单需求) / FULL (复杂需求)                         │
│  🔒 L2: 断路器 | 指数退避重试 | 多 LLM 提供商故障转移                │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────┐
│                    引擎层 (Engine Layer)                            │
│  PromptEngine | EmbeddingEngine | RAGEngine | FormatEngine          │
│  UTCF Schema 校验 | LLM-as-Judge 质量监控 | Embedding 漂移检测       │
│  🔒 L3: 输出 Schema 校验 | 5% 采样 Judge | 质量评分                  │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────┐
│                    数据层 (Data Layer)                              │
│  PostgreSQL 16 (主数据库) | Milvus (向量库) | Redis (缓存/DLQ)       │
│  Celery 异步任务 | Fernet 对称加密 (API Key)                         │
│  🔒 L4: 死信队列 (DLQ) | 幂等守卫 | KB 静默跳过兜底                  │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.3 关键设计原则

| 原则 | 含义 | 落地实现 |
|------|------|----------|
| **需求质量优先** | 需求进入生成前必须通过 ClarifyAgent + QualityGate | 3 轮迭代门禁 + 冷启动放宽阈值 |
| **闭环积累** | 每次生成的用例经确认后自动回写知识库 | ReviewAgent.persist_to_kb() + 向量双写 |
| **全链路可追溯** | 每个用例必须可追溯到具体需求字段 | Traceability 表 + related_fp 标注 |
| **稳定性保障** | 所有外部依赖假设会失败，四层防护 | L1 限速 / L2 断路器 / L3 监控 / L4 DLQ |
| **可插拔架构** | LLM、向量库、测试框架均可替换 | LiteLLM 统一接口 + Milvus 抽象层 |
| **可观测性** | 全链路 Trace + 质量指标 + 稳定性指标 | LangFuse + Prometheus + 健康检查 |

---

## 2. 系统结构说明

### 2.1 项目整体目录结构

```
ai-test-gen/
├── backend/                      # 后端 FastAPI 服务
│   ├── app/
│   │   ├── main.py               # FastAPI 应用入口（lifespan + 中间件 + 路由注册）
│   │   ├── config.py             # 全局配置（Pydantic Settings）
│   │   ├── __init__.py           # __version__ 定义
│   │   ├── VERSION               # 版本号文件（当前 2.1.1）
│   │   ├── api/                  # API 路由层（12 个子路由）
│   │   ├── agents/               # Agent 流水线（5 个核心 Agent）
│   │   ├── core/                 # 核心服务（编排器、限流、认证、DLQ 等）
│   │   ├── db/                   # 数据层（SQLAlchemy ORM + Milvus + 混合检索）
│   │   ├── exporters/            # 导出器（Markdown/Gherkin/pytest/XMind）
│   │   ├── models/               # 数据模型（SRR/UTCF/API Schema）
│   │   ├── prompts/              # LLM 提示词模板（按 Agent 分文件）
│   │   ├── tasks/                # Celery 异步任务（向量化等）
│   │   └── utils/                # 工具（加密、日志、ID 生成、密码哈希）
│   └── tests/                    # 单元测试与集成测试
├── frontend/                     # 前端 React 应用
│   ├── src/
│   │   ├── App.tsx               # 路由根组件（含 4 类守卫）
│   │   ├── main.tsx              # Vite 入口
│   │   ├── auth.tsx              # 认证上下文（AuthProvider）
│   │   ├── api/client.ts         # API 客户端（统一 fetch 封装）
│   │   ├── store/                # 状态管理（ProductContext）
│   │   ├── pages/                # 业务页面（23 个）
│   │   ├── components/           # 通用组件（Layout/Sidebar/XMindEditor 等）
│   │   └── types/                # TypeScript 类型定义
│   └── package.json              # 前端版本（与 backend/VERSION 同步）
├── docs/                         # 架构文档（00–11 共 12 篇）
├── docker-compose.yml            # 容器编排（backend/frontend/postgres/milvus/redis/celery）
└── templates/                    # 知识库 Schema、种子数据
```

### 2.2 后端模块结构

#### 2.2.1 API 路由层（`backend/app/api/`）

由 [api/__init__.py](file:///e:/git-down/ai-test-gen/backend/app/api/__init__.py) 聚合为统一 `api_router`，由 `main.py` 注册到 `/api` 前缀：

| 路由 | 前缀 | 职责 |
|------|------|------|
| auth | `/api/auth` | 登录、JWT 颁发、用户认证 |
| requirements | `/api/requirements` | 需求上传/列表/详情/澄清/重新生成/归档/XMind 导出 |
| generate | `/api/generate`（无前缀） | SSE 流式生成测试用例（核心端点） |
| testcases | `/api/testcases` | 用例列表/详情/编辑/删除/反馈回流 |
| knowledge | `/api/knowledge` | 知识库统计/测试模式列表/向量重处理 |
| knowledge-qa | `/api/knowledge-qa` | 知识库问答（含人工纠错） |
| settings | `/api/settings` | LLM 配置读取/更新/连通性测试 |
| export | `/api/export` | 用例导出（Markdown/Gherkin/pytest/JSON/XMind） |
| products | `/api/products` | 产品 + 项目 + 模块 + 依赖图谱 |
| prompts | `/api/prompts`（无前缀） | 提示词覆盖管理 |
| roles | `/api/admin` | 角色 CRUD + 菜单权限 |
| users | `/api/admin` | 用户 CRUD + 角色分配 |

#### 2.2.2 Agent 流水线层（`backend/app/agents/`）

5 个核心 Agent，由 [core/orchestrator.py](file:///e:/git-down/ai-test-gen/backend/app/core/orchestrator.py) 通过 LangGraph StateGraph 编排：

| Agent | 文件 | 职责 | 输入 → 输出 |
|-------|------|------|------------|
| ReqAgent | `req_agent.py` | 需求解析 | 原始文本 → SRR JSON + interaction_srr + FP 列表 |
| ClarifyAgent | `clarify_agent.py` | 需求澄清 | SRR → 细化 SRR + 问题列表 + KB 补全记录 |
| ImpactAgent | `impact_agent.py` | 影响分析 | SRR + 优化上下文 → 影响范围报告 + 生成指导 |
| GenAgent | `gen_agent.py` | 用例生成 | SRR + FP + 影响指导 → UTCF 用例集 |
| ReviewAgent | `review_agent.py` | 用例审核 | 候选用例集 → 审核报告 + KB 回流 |

**QualityGate**（`core/quality_gate.py`）虽名为 Agent，实为门禁组件，对 SRR 执行 7 项检查（Schema/关键字段/质量评分/Critical/验收标准/步骤数/冷启动）。

#### 2.2.3 核心服务层（`backend/app/core/`）

| 模块 | 职责 |
|------|------|
| orchestrator.py | LangGraph DAG 编排器（流水线入口 + SSE 事件分发） |
| channel_selector.py | 通道选择器（FAST / FULL 自动判定） |
| quality_gate.py | 质量门禁（Blocker 阻断 / Warning 放行） |
| llm_client.py | LLM 统一调用客户端（含重试、超时、故障转移） |
| auth.py | JWT 认证中间件 + 权限校验 |
| rate_limiter.py | 内存 Token Bucket 限流 |
| dlq.py | **v2.2 新增** 死信队列（Redis List 实现） |
| prompt_manager.py | 提示词管理（运行时从 DB 加载覆盖值） |
| channel_selector.py | 通道选择（v1.6 起全部走 FULL） |
| exceptions.py | 业务异常定义 |

#### 2.2.4 数据层（`backend/app/db/`）

| 模块 | 职责 |
|------|------|
| database.py | SQLAlchemy 异步引擎 + Session 工厂 + init_db |
| models.py | 16 张 ORM 表定义（Requirement/TestCase/FP 等） |
| milvus_store.py | Milvus 向量库封装（5 个 Collection） |
| hybrid_search.py | 混合检索（向量 + BM25 关键词 + RRF 融合 + Reranker） |
| reranker.py | Cross-Encoder 重排器（bge-reranker-base） |
| kb_cache.py | KB 查询缓存（降低重复检索成本） |
| prompt_store.py | 提示词覆盖持久化（同步访问） |
| seed.py | 初始化种子数据（管理员/默认产品/测试模式） |

#### 2.2.5 其他后端模块

| 模块 | 职责 |
|------|------|
| `exporters/` | 5 种导出格式（Markdown/Gherkin/pytest/XMind 8/XMind 2020+）+ 需求 XMind 导出 |
| `models/srr.py` | SRR (Software Requirement Representation) Pydantic 模型 |
| `models/utcf.py` | UTCF (Universal Test Case Format) Pydantic 模型 |
| `prompts/` | 7 个 Agent 的 LLM 提示词模板（按 Agent 分文件） |
| `tasks/celery_app.py` | Celery 异步任务队列 |
| `tasks/embedding_tasks.py` | 异步向量化任务（PG → Milvus 双写） |
| `utils/crypto.py` | Fernet 对称加密（API Key 加密存储） |
| `utils/logger.py` | 日志系统（loguru） |

### 2.3 前端模块结构

#### 2.3.1 路由结构（`frontend/src/App.tsx`）

4 类路由守卫层层包裹：

| 守卫 | 作用 |
|------|------|
| `AuthProvider` | 全局认证上下文（顶层包裹） |
| `ProductProvider` | 全局产品选择上下文（顶层包裹） |
| `AuthGuard` | 未登录 → 重定向 `/login` |
| `ProductGuard` | 未选产品且为管理员 → 重定向 `/admin` |
| `AdminGuard` | 非管理员 → 重定向 `/dashboard` |
| `AdminHomeGuard` | 已登录但无权限 → 重定向 `/login` |

**路由分组**：
- **主系统路由**（需选产品）：`/dashboard`、`/requirements`、`/testcases`、`/knowledge`、`/knowledge-qa`、`/dependency-graph`、`/projects-modules`、`/projects`、`/modules`、`/tasks`、`/manual`
- **管理后台路由**（不需要选产品）：`/admin`（首页）、`/admin/products`、`/admin/prompts`、`/admin/knowledge`、`/admin/users`、`/admin/roles`、`/admin/settings`

#### 2.3.2 状态管理（`frontend/src/store/`）

采用 React Context 轻量方案（无 Redux）：

| Context | 文件 | 职责 |
|---------|------|------|
| `ProductContext` | `store/ProductContext.tsx` | 当前选中产品（localStorage 持久化） |
| `AuthContext` | `auth.tsx` | 当前登录用户、JWT Token、权限列表 |

#### 2.3.3 业务页面（`frontend/src/pages/`，共 23 个）

按功能分类：

| 类别 | 页面 |
|------|------|
| 认证 | Login |
| 主流程 | Dashboard、Requirements、RequirementDetail、RequirementClarify、TestCases、Generate、Tasks |
| 知识库 | KnowledgeBase、KnowledgeQA |
| 项目结构 | Products、ProjectsModules、ProjectManagement、ModuleManagement、DependencyGraph |
| 管理 | AdminDashboard、AdminKnowledgeBase、Settings、PromptManager、UserManagement、RoleManagement |
| 其他 | UserManual |

#### 2.3.4 通用组件（`frontend/src/components/`，共 14 个）

| 组件 | 职责 |
|------|------|
| Layout / AdminLayout | 主系统 / 管理后台布局（含侧边栏） |
| Sidebar / AdminSidebar | 侧边栏导航（按权限渲染菜单） |
| ErrorBoundary | 全局错误边界（捕获渲染异常） |
| LoadingSpinner | 加载动画 |
| SSEProgress | SSE 流式进度展示 |
| CaseEditDialog | 用例编辑弹窗 |
| XMindEditor | XMind 在线编辑器（v1.9.0 引入） |
| FunctionalPoints | 功能点列表组件 |
| FPSRRViewer | FP 子 SRR 查看器 |
| FieldEditor | SRR 字段编辑器（v1.7.0 引入） |
| VersionHistoryPanel | 需求历史版本面板（v2.1.0 引入） |
| ArrayEditor | 数组字段编辑器 |

### 2.4 部署拓扑

```
                    ┌──────────────┐
                    │   Nginx LB   │  (可选反向代理)
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
        ┌─────▼─────┐ ┌───▼───┐ ┌─────▼─────┐
        │ FastAPI   │ │Celery │ │ Frontend  │
        │ backend   │ │worker │ │ Vite build│
        │ :8000     │ │       │ │ :5173/80  │
        └─────┬─────┘ └───┬───┘ └───────────┘
              │            │
        ┌─────┴────────────┴─────┐
        │                        │
   ┌────▼────┐  ┌──────┐  ┌─────▼─────┐
   │PostgreSQL│  │Redis │  │  Milvus   │
   │  :5432  │  │:6379 │  │  :19530   │
   │ai_test_ │  │      │  │ 5 Collect │
   │  gen    │  │      │  │  -ions    │
   └─────────┘  └──────┘  └───────────┘
```

Docker Compose 服务：`backend`、`frontend`、`postgres`（含 zhparser 中文分词）、`milvus`、`redis`、`celery`。

---

## 3. 核心数据流与 Agent 流水线

### 3.1 LangGraph DAG 结构

由 [core/orchestrator.py](file:///e:/git-down/ai-test-gen/backend/app/core/orchestrator.py#L260-L306) 定义：

```
START → parse → select_channel
                      ↓
                [route_after_channel]
                 /              \
               FULL             FAST
                ↓                ↓
            clarify          gate(直接)
                ↓                ↓
           [route_after_gate]    ↓
            /     |     \        ↓
         retry  pass  force    ↓
           ↓     ↓      ↓       ↓
        clarify impact impact   ↓
                  ↓      ↓      ↓
               generate ←──────┘
                  ↓
                 review
                  ↓
                 END
```

### 3.2 7 个节点职责

| 节点 | SSE stage | 进度 | 职责 |
|------|-----------|------|------|
| `_parse_node` | parse | 20% | 使用已确认的 SRR（不重新解析） |
| `_select_channel_node` | select_channel | 25% | 通道选择（FAST / FULL） |
| `_clarify_node` | clarify | 30% | ClarifyAgent 澄清 + KB 补全 |
| `_gate_node` | gate | 40% | QualityGate 门禁检查 |
| `_impact_node` | impact | 50% | ImpactAgent 影响分析（仅优化需求） |
| `_gen_node` | generate | 70% | GenAgent 生成用例 + FP 定向 + 一致性检查 |
| `_review_node` | review | 90% | ReviewAgent 审核 + KB 回流 |

### 3.3 通道选择策略

由 [core/channel_selector.py](file:///e:/git-down/ai-test-gen/backend/app/core/channel_selector.py) 决定：

| 通道 | 触发条件 | 启用能力 |
|------|---------|---------|
| **FULL** | v1.6 起所有需求统一走 FULL | Clarify + Impact + 多类型生成 + 完整审核 |
| **FAST** | （保留通道，当前未启用） | 跳过 Clarify + 单次批量生成 + 采样审核 |

**Clarify 回环保护**（`ClarifyLoopGuard`）：最多 3 轮，超过则强制通过 + 警告，避免死锁。

### 3.4 端到端数据流

```
用户上传需求文档
     ↓
① ReqAgent 解析 → SRR JSON (interaction_srr) + FP 列表 (含 sub_srr)
     ↓
② ClarifyAgent 澄清 → 完整性/歧义性/遗漏检测 + KB 基线补全 + 人工问题
     ↓ （最多 3 轮迭代）
③ QualityGate 门禁 → Schema + 7 项检查 + 动态阈值（冷启动 0.60 / 正常 0.75）
     ↓
④ ImpactAgent 影响分析 → 仅优化需求触发，版本链遍历 + 回归推荐
     ↓
⑤ GenAgent 生成 → FP 定向 + Few-shot 注入 + 影响指导 → UTCF 用例集
     ↓
⑥ ReviewAgent 审核 → 六维审核 + 追溯验证 + KB 回流（PG + Milvus 双写）
     ↓
⑦ FormatEngine 输出 → UTCF JSON + Gherkin/pytest/JUnit/XMind 转换
     ↓
⑧ 闭环 → 用例入库 + 向量嵌入更新 + 追溯矩阵持久化
```

---

## 4. 数据模型与存储层

### 4.1 PostgreSQL 核心表（16 张）

由 [db/models.py](file:///e:/git-down/ai-test-gen/backend/app/db/models.py) 定义：

| 表名 | 用途 | 关键字段 |
|------|------|---------|
| `requirements` | 需求元数据 | id, product_id, project_id, srr_json, status, version, go_live_date |
| `test_cases` | 测试用例 | id, req_id, case_json, type, status, confidence |
| `test_patterns` | 测试模式库 | name, category, pattern_json, usage_count |
| `error_cases` | 错误案例库 | error_id, original_case_id, error_type, corrected_json |
| `generation_batches` | 生成批次 | req_id, total_cases, model_used, total_tokens, total_cost |
| `req_dependencies` | 需求依赖关系 | source_req_id, target_req_id, relation_type, strength |
| `traceability` | 追溯矩阵 | test_case_id, requirement_id, requirement_field, trace_type |
| `llm_config` | LLM 配置（加密） | provider, api_key (Fernet), is_primary |
| `products` | 产品 | name (唯一), visible_to_user_ids |
| `projects` | 项目（属产品） | product_id, name, version |
| `modules` | 模块（属产品） | product_id, name, pos_x, pos_y（图谱坐标） |
| `module_dependencies` | 模块依赖（图谱边） | source_module_id, target_module_id, relation_type |
| `functional_points` | 功能点（v1.4） | requirement_id, module_id, srr_json, atomicity_check |
| `fp_dependencies` | FP 依赖关系 | source_fp_id, target_fp_id, relation_type (5 种) |
| `analysis_tasks` | 异步任务（v2.14） | task_type, status, phase, progress, result_json |
| `knowledge_qa` | 知识问答记录 | question, answer, conversation_history, archived |
| `kb_settings` | KB 专属设置 | key, value |
| `roles` / `users` | 角色 / 用户 | RBAC 权限模型 |
| `prompt_overrides` | 提示词覆盖 | module, name, value, default_value |
| `req_patterns` | 需求模式库 | modules, required_dimensions, common_boundary, risk_points |

### 4.2 Milvus 向量库（5 个 Collection）

由 [db/milvus_store.py](file:///e:/git-down/ai-test-gen/backend/app/db/milvus_store.py) 管理：

| Collection | 用途 | 嵌入源 |
|-----------|------|--------|
| `requirements` | 归档需求嵌入 | SRR JSON 的文本化 |
| `test_cases` | 用例嵌入 | UTCF case_json |
| `patterns` | 测试模式嵌入 | pattern_json |
| `req_pats` | 需求模式嵌入 | req_patterns.pattern_json |
| `errors` | 错误案例嵌入 | error_detail |

**嵌入模型**：`BAAI/bge-large-zh-v1.5`（本地 sentence-transformers）。

### 4.3 混合检索策略

由 [db/hybrid_search.py](file:///e:/git-down/ai-test-gen/backend/app/db/hybrid_search.py) 实现：

```
用户查询 → 向量检索 (bge-large-zh) ──┐
                                       ├─→ RRF 融合 → Cross-Encoder 重排 → Top-N
              BM25 关键词检索 (PG tsvector) ─┘
```

- **向量召回**：Top-K=20，相似度阈值 0.70
- **BM25 召回**：PostgreSQL `tsvector` + zhparser 中文分词
- **RRF 融合**：Reciprocal Rank Fusion
- **重排**：bge-reranker-base Cross-Encoder（v1.1.0 修复三大 bug 后启用）

---

## 5. 稳定性与安全设计

### 5.1 四层防护体系

| 层级 | 防护措施 | 实现位置 |
|------|---------|---------|
| L1 接入层 | Token Bucket 限速（API_RPM_LIMIT=60） + JWT 认证 + RBAC | `core/rate_limiter.py`、`core/auth.py` |
| L2 编排层 | 断路器 + 指数退避重试 + 多 LLM 故障转移 | `core/llm_client.py` |
| L3 引擎层 | UTCF Schema 校验 + LLM-as-Judge 5% 采样 | `models/utcf.py`、ReviewAgent |
| L4 数据层 | DLQ 死信队列 + 幂等守卫 + KB 静默跳过 | `core/dlq.py`（v2.2 新增） |

### 5.2 安全设计（v2.2 强化）

| 维度 | 措施 |
|------|------|
| **JWT 认证** | 除 `/api/health` 和 `/api/auth/login` 外全量保护；8h 过期 |
| **API Key 加密** | Fernet 对称加密存储；响应脱敏（前3后4位） |
| **密码哈希** | PBKDF2-SHA256 + 盐值 |
| **CORS 白名单** | 仅允许前端开发地址 |
| **生产环境关闭文档** | `docs_url=None`（非 DEBUG 模式） |
| **启动安全校验** | `settings.validate_security()` 检查 APP_SECRET_KEY / MASTER_KEY |
| **RBAC 权限** | Role.menus JSON 数组定义菜单权限；前端按权限渲染 |

### 5.3 中间件链（按执行顺序）

```
请求 → CORS → request_logging → auth → rate_limit → 路由处理 → 全局异常处理 → 响应
```

由 [main.py](file:///e:/git-down/ai-test-gen/backend/app/main.py#L147-L209) 注册。

---

## 6. 历次版本变更架构设计

### 6.1 版本总览

| 版本 | 日期 | 标签主题 | 架构影响等级 |
|------|------|---------|------------|
| v1.1.0 | 2026-07-10 | 双索引知识库 + Celery 异步双写 + 降级策略 | ⭐⭐⭐ |
| v1.2.0 | 2026-07-14 | go_live_date 类型修复 + KB 搜索筛选 | ⭐⭐ |
| v1.3.0 | 2026-07-14 | 上传体验优化 + 向量化状态 + Milvus 元数据修复 | ⭐⭐ |
| v1.4 | 2026-07-14 | 功能点系统 + zhparser 警告 | ⭐⭐⭐⭐ |
| v1.5.0 | 2026-07-14 | 功能点依赖图谱 + 产品管理 API + Docker zhparser | ⭐⭐⭐ |
| v1.5.1 | 2026-07-15 | FP 归档门控 + 删除级联清理 | ⭐⭐ |
| v1.5.2 | 2026-07-15 | FP 审核/重生成/依赖/影响分析 + 6 个 bug 修复 | ⭐⭐⭐ |
| v1.6.0 | 2026-07-15 | FP 多选合并 + SRR 逐字段编辑 | ⭐⭐⭐ |
| v1.7.0 | 2026-07-16 | SRR 全字段逐项编辑 FieldEditor 组件化重构 | ⭐⭐ |
| v1.7.1 / v1.7.2 | 2026-07-16/17 | 版本号维护 | ⭐ |
| v1.8.0 | 2026-07-17 | 需求确认流程 + 用例生成质量提升 | ⭐⭐⭐ |
| v1.9.0 | 2026-07-20 | XMind 在线编辑器 + 用例审核/编辑/删除 + 系统全面中文化 | ⭐⭐⭐⭐ |
| v2.0.0 | 2026-07-20 | SRR 架构重构（交互层分离）+ KB 基线补全精准化 + FP 去重跳过 LLM | ⭐⭐⭐⭐⭐ |
| v2.1.0 | 2026-07-20 | 项目/模块管理拆分 + 需求历史版本管理 + 需求 XMind 导出 | ⭐⭐⭐ |
| v2.1.1 | 2026-07-20 | 修复项目筛选只能查到部分需求的 bug | ⭐ |
| v2.2.0 | 2026-07-21 | 需求分析双 SRR 架构重构与用例生成质量提升 | ⭐⭐⭐⭐⭐ |

---

### 6.2 v1.1.0 — 双索引知识库 + Celery 异步双写（2026-07-10）

**架构变更**：
- 引入 **Celery + Redis** 异步任务队列，需求归档时触发 `embedding_tasks.py` 异步向量化
- **PG + Milvus 双写**：归档需求先写 PostgreSQL，再异步写入 Milvus，避免阻塞用户请求
- **降级策略**：Milvus 不可用时静默跳过，KB 检索失败时降级为人工澄清兜底
- 奠定 L4 数据层防护基础

**新增文件**：
- `backend/app/tasks/celery_app.py`
- `backend/app/tasks/embedding_tasks.py`

---

### 6.3 v1.2.0 / v1.3.0 — 数据修复与体验优化（2026-07-14）

**v1.2.0 架构变更**：
- `Requirement.go_live_date` 类型从 `Date` 改为 `DateTime`（支持精确到时分秒）
- KB 检索结果按 `go_live_date` 降序排序，同功能以靠后时间为准
- `answer_text` 增加 `（v2.1, 上线: 2026-07-11 14:30:00, 相似度 0.55）` 版本时间标签

**v1.3.0 架构变更**：
- `Requirement` 增加 `vectorization_status` 字段（null/processing/completed/failed）
- 前端 KB 管理页显示双状态徽章：绿色"已归档"（PG）+ 蓝色"已向量化"（Milvus）/ 琥珀"未向量化"
- 修复 Milvus 元数据超限问题（`go_live_time` 作为 metadata 字段名冲突）

---

### 6.4 v1.4 — 功能点系统引入（2026-07-14）

**架构变更（⭐⭐⭐⭐ 重大）**：
- **引入功能点（Functional Point, FP）三级粒度模型**：BP（业务流程）→ FP（功能点）→ Data（数据）
- 新增 2 张表：
  - `functional_points`：FP 主表，含 `srr_json`（子 SRR）、`atomicity_check`（原子性三要素自检）、`test_hints`（测试提示）
  - `fp_dependencies`：FP 依赖关系表，支持 5 种关系（depends_on / data_flows / invariants / lifecycle_scenarios / conflicts_with）
- **GenAgent 升级**：从 DB 加载 FP 列表，按 FP 定向生成用例，每个 FP ≥ 1 用例
- **追溯强化**：用例 `related_fp` 字段标注，`Traceability.requirement_field` 支持 `fp:{fp_id}` 格式
- **原子性三要素**：明确触发操作 + 可验证业务结果 + 有限元素边界
- PostgreSQL 安装 zhparser 中文分词扩展（含 xunsearch scws + Gitee zhparser 镜像）

**影响范围**：奠定了"需求 → FP → 测试用例"的核心价值链，是 v2.0 双 SRR 架构的前置基础。

---

### 6.5 v1.5.0 / v1.5.1 / v1.5.2 — 功能点深化（2026-07-14 ~ 15）

**v1.5.0 架构变更**：
- 功能点依赖图谱可视化（前端使用 `@xyflow/react`）
- 产品管理 API（CRUD + 可见性控制 `visible_to_user_ids`）
- Docker PostgreSQL zhparser 构建完善（Dockerfile 严格错误检测 `set -eux` + 编译验证 + scws 字典安装 + ld.so.conf 配置）

**v1.5.1 架构变更**：
- **FP 归档门控**：FP 必须审核通过（status=approved）才能生成用例
- **删除级联清理**：删除需求时级联清理 FP + FP 依赖 + Milvus 向量
- FP 解析后自动提取 + 依赖关系识别

**v1.5.2 架构变更**：
- FP 审核 / 重生成 / 依赖 / 影响分析完整流程
- 修复 6 个 bug（含 FP 与需求关联、依赖图谱渲染等）

---

### 6.6 v1.6.0 / v1.7.0 — 编辑能力强化（2026-07-15 ~ 16）

**v1.6.0 架构变更**：
- FP 多选合并（merge_fp API）
- SRR 逐字段编辑（前端 FieldEditor 组件雏形）
- 通道选择策略调整：全部走 FULL 通道确保质量（`channel_selector.py` 注释可见）

**v1.7.0 架构变更**：
- **FieldEditor 组件化重构**：SRR 全字段逐项编辑，支持嵌套路径（a.b.c）
- 泛化 SRR 字段保存逻辑（`saveSrrField` 函数）
- 模块名称映射（ID → 名称）用于 SRR 显示

---

### 6.7 v1.8.0 — 需求确认流程（2026-07-17）

**架构变更**：
- 引入需求**确认流程**：`pending_review` → `confirmed` 状态门控
- 只有 `confirmed` 状态的需求才能触发生成
- 用例生成质量提升（Prompt 优化 + Few-shot 示例）

---

### 6.8 v1.9.0 — XMind 在线编辑 + 全面中文化（2026-07-20）

**架构变更（⭐⭐⭐⭐ 重大）**：
- **XMind 在线编辑器**（`XMindEditor.tsx` 组件）：支持用例审核/编辑/删除
- 测试用例列表动态更新（编辑器变更后同步刷新）
- **系统全面中文化**：所有系统名称、按钮、状态标签使用中文
- 用户手册按钮固定在界面右上角

**新增组件**：`XMindEditor.tsx`

---

### 6.9 v2.0.0 — SRR 架构重构（2026-07-20）

**架构变更（⭐⭐⭐⭐⭐ 重大版本）**：

#### 双 SRR 分层架构

```
原架构: 单一 SRR（FP 内部规则 + 跨 FP 交互规则混合）
                ↓
新架构: interaction_srr（总 SRR = 交互层）
        + 每个 FP 的 sub_srr（内部层）
```

- **interaction_srr**：跨 FP 交互规则、共享约束、契约定义
- **FP sub_srr**：FP 专属内部流程、规则、约束
- **一条规则只在一处定义，消除重复**

#### KB 基线补全精准化

- 新增 `_detect_baseline_focus()` 函数：按问题维度精准返回答案
- 正常流程问题 → 返回 `happy_path`
- 异常问题 → 返回 `alternate_flows`
- 数据问题 → 返回 `data_constraints`
- 答案长度从 559 字符（全量 5 维度）降至 ~110 字符（精准 1 维度）

#### FP 去重跳过 LLM

- 同模块已有 FP 名 → 复制到新需求，不重新调用 LLM
- 节省 LLM 调用成本

#### ClarifyAgent 改进

- 移除固定冷启动问题（模块/影响/边界/依赖）
- 仅基于实际文档分析生成动态问题
- 相似度阈值从 0.85 降至 0.40（bge-large-zh 问题→文档相似度实际为 0.40-0.65）
- LLM 二次过滤作为后备

#### MatchAgent 移除

- 用例生成阶段不再从知识库检索历史用例
- 替代方案：Few-shot 示例注入 Prompt + FP 定向生成上下文
- 理由：SRR 已足够详细

---

### 6.10 v2.1.0 — 项目/模块管理拆分 + 历史版本（2026-07-20）

**架构变更**：
- **项目/模块管理拆分**：原 `ProjectsModules` 页面拆分为 `ProjectManagement` + `ModuleManagement`
- **需求历史版本管理**：新增 `VersionHistoryPanel` 组件
  - 每次重新分析前自动快照当前 SRR 入 `_version_history` 数组
  - 支持一键切换历史版本（restore API）
- **需求 XMind 导出**：`exporters/req_xmind.py` 实现需求树形导出
- `Requirement` 模型增加 `supersedes` 字段（版本链双向追溯）
- `upload_requirement_file` 端点增加 `project_id` 参数

---

### 6.11 v2.1.1 — 项目筛选 bug 修复（2026-07-20）

**架构变更（bug 修复）**：
- 根因：v2.1.0 之前 `upload_requirement_file` 缺少 `project_id` 参数，导致历史需求 `project_id=NULL`
- 三层修复：
  1. **代码修复**：后端端点添加 `project_id` 参数 + 支持 `unassigned` 筛选 NULL
  2. **前端修复**：`uploadFile` 传 `project_id` + 下拉增加"(未绑定项目)"选项 + 展示用名字
  3. **数据修复**：SQL 自动绑定单项目产品的 NULL `project_id` 需求

**影响文件**：
- [backend/app/api/requirements.py](file:///e:/git-down/ai-test-gen/backend/app/api/requirements.py)
- [backend/app/api/testcases.py](file:///e:/git-down/ai-test-gen/backend/app/api/testcases.py)
- [frontend/src/api/client.ts](file:///e:/git-down/ai-test-gen/frontend/src/api/client.ts)
- [frontend/src/pages/Requirements.tsx](file:///e:/git-down/ai-test-gen/frontend/src/pages/Requirements.tsx)
- [frontend/src/pages/TestCases.tsx](file:///e:/git-down/ai-test-gen/frontend/src/pages/TestCases.tsx)
- [frontend/src/pages/ProjectManagement.tsx](file:///e:/git-down/ai-test-gen/frontend/src/pages/ProjectManagement.tsx)

---

### 6.12 v2.2.0 — 双 SRR 架构深化 + 质量提升（2026-07-21）

**架构变更（⭐⭐⭐⭐⭐ 重大版本）**：

#### 死信队列（DLQ）引入

- 新增 [core/dlq.py](file:///e:/git-down/ai-test-gen/backend/app/core/dlq.py)：基于 Redis List 的轻量死信队列
- 用于持久化异步任务失败记录，支持事后审计和手动重试
- 填补 L4 数据层防护的最后一环

```python
class DeadLetterQueue:
    def push(self, task_type: str, payload: dict, error: str, max_retries: int = 3) -> bool
    def peek(self, limit: int = 20) -> list[dict]
    def pop(self, limit: int = 1) -> list[dict]
    def count(self) -> int
```

#### 启动安全校验

- `settings.validate_security()` 在启动时检查：
  - 生产环境必须设置 `APP_SECRET_KEY`（JWT 签名）
  - 生产环境必须设置 `MASTER_KEY`（API Key 加密）
  - 开发环境自动生成临时密钥
- 生产环境关闭 API 文档（`docs_url=None`）

#### 一致性检查

- GenAgent 生成后对比上次生成的用例数量与类型分布
- 数量差异 > 20% 触发告警
- 非阻塞，仅记录日志

#### FP 覆盖率矩阵

- 流水线完成后计算 FP 覆盖率
- 返回 `total_fps` / `covered_fps` / `coverage_rate` / `uncovered` 列表

#### 需求分析全链路文档

- 新增 [docs/09-requirement-analysis-pipeline.md](file:///e:/git-down/ai-test-gen/docs/09-requirement-analysis-pipeline.md)：需求分析全链路流程图
- 新增 [docs/10-v2.2-requirement-analysis-report.md](file:///e:/git-down/ai-test-gen/docs/10-v2.2-requirement-analysis-report.md)：v2.2 需求分析报告

#### ClarifyAgent 增强

- 异常处理改为日志记录（移除静默 `except Exception: pass`）
- KB LLM 提供商读取异常时记录日志而非吞掉
- 取消检测点：解析后 + KB 搜索前

#### 异步任务系统

- `AnalysisTask` 表记录长耗时任务（upload/clarify/regenerate/impact/generate/merge_fp）
- SSE 事件同步任务进度
- 进程重启时 stale 的 running 任务标记为 interrupted
- 任务取消端点 `POST /tasks/{id}/cancel`：running/pending → interrupted，关联需求 → draft

---

## 7. 架构演进路线图

### 7.1 已完成的演进主线

```
v1.1.0 异步化基础 (Celery + 双写)
   ↓
v1.4   功能点系统 (FP 三级粒度)
   ↓
v1.5.x FP 深化 (依赖图谱 + 审核 + 影响分析)
   ↓
v1.9.0 交互升级 (XMind 编辑 + 中文化)
   ↓
v2.0.0 架构重构 (双 SRR + KB 精准化 + MatchAgent 移除)
   ↓
v2.2.0 质量与稳定性 (DLQ + 安全校验 + 一致性检查)
```

### 7.2 关键架构决策记录

| 决策 | 版本 | 理由 |
|------|------|------|
| 移除 MatchAgent | v2.0 | SRR 已足够详细，无需 KB 检索历史用例 |
| 全部走 FULL 通道 | v1.6 | 简单需求也需 Clarify + Impact 确保质量 |
| 双 SRR 分层 | v2.0 | 消除规则重复，交互层与内部层职责分离 |
| 相似度阈值 0.40 | v2.0 | bge-large-zh 问题→文档实际相似度 0.40-0.65 |
| 引入 DLQ | v2.2 | L4 防护最后一环，异步任务失败可追溯 |
| zhparser 中文分词 | v1.4 | BM25 关键词检索需要中文分词支持 |

### 7.3 后续演进方向

| 方向 | 规划 |
|------|------|
| **ImpactAgent 完整启用** | 后期优化需求为主时，模块依赖图 BFS 遍历 + 回归测试推荐 |
| **多 LLM 对冲** | LiteLLM 多提供商并发调用，取首达响应 |
| **LangFuse 全链路** | Trace + 质量指标 + 稳定性指标可视化 |
| **MinIO 对象存储** | 原始需求文档、测试报告、日志归档 |
| **横向扩展** | FastAPI 多实例 + Nginx LB |

---

## 附录：文档索引

| 文档 | 主题 |
|------|------|
| [00-architecture-overview.md](file:///e:/git-down/ai-test-gen/docs/00-architecture-overview.md) | 系统架构总览（v2.1） |
| [01-requirement-parsing.md](file:///e:/git-down/ai-test-gen/docs/01-requirement-parsing.md) | 需求解析 |
| [02-knowledge-base.md](file:///e:/git-down/ai-test-gen/docs/02-knowledge-base.md) | 知识库架构 |
| [03-case-generation-flow.md](file:///e:/git-down/ai-test-gen/docs/03-case-generation-flow.md) | 用例生成流程 |
| [04-output-format.md](file:///e:/git-down/ai-test-gen/docs/04-output-format.md) | 输出格式规范 |
| [05-technical-selection.md](file:///e:/git-down/ai-test-gen/docs/05-technical-selection.md) | 技术选型 |
| [06-e2e-test-report.md](file:///e:/git-down/ai-test-gen/docs/06-e2e-test-report.md) | E2E 测试报告 |
| [07-internal-deployment.md](file:///e:/git-down/ai-test-gen/docs/07-internal-deployment.md) | 内部部署 |
| [08-functional-points.md](file:///e:/git-down/ai-test-gen/docs/08-functional-points.md) | 功能点子系统 |
| [09-requirement-analysis-pipeline.md](file:///e:/git-down/ai-test-gen/docs/09-requirement-analysis-pipeline.md) | 需求分析全链路（v2.2） |
| [10-v2.2-requirement-analysis-report.md](file:///e:/git-down/ai-test-gen/docs/10-v2.2-requirement-analysis-report.md) | v2.2 需求分析报告 |
| **11-architecture-design-and-changelog.md（本文档）** | 架构设计与历次变更总览 |
