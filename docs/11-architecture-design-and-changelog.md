# 11 — 架构设计文档与历次变更总览

> AI 测试用例生成系统整体架构设计逻辑、系统结构说明与版本演进  
> 版本: v3.1.1 | 文档日期: 2026-08-17
>
> 本文档是系统的**架构主索引**，整合整体架构设计逻辑、系统结构说明、以及自 v1.1.0 -> v3.1.1 的历次架构变更要点。其他分册文档（00–10）按主题展开细节，本文档提供横向串联与纵向演进视图。

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
│  生成准备: 构造环节开关（已确认需求跳过生成期澄清；v3.4.4 前为 FAST/FULL 通道分级） │
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
| channel_selector.py | 生成配置构造器（环节开关；原通道选择器，v3.4.4 删除 FAST/FULL 分级） |
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
        启用生成期澄清      未启用（生产实际路径）
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
| `_prepare_node`（节点 key 保持 select_channel） | select_channel | 25% | 生成准备（构造环节开关；已确认需求跳过生成期澄清） |
| `_clarify_node` | clarify | 30% | ClarifyAgent 澄清 + KB 补全 |
| `_gate_node` | gate | 40% | QualityGate 门禁检查 |
| `_impact_node` | impact | 50% | ImpactAgent 影响分析（仅优化需求） |
| `_gen_node` | generate | 70% | GenAgent 生成用例 + FP 定向 + 一致性检查 |
| `_review_node` | review | 90% | ReviewAgent 审核 + KB 回流 |

### 3.3 生成准备策略（v3.4.4：原「通道选择策略」）

由 [core/channel_selector.py](file:///e:/git-down/ai-test-gen/backend/app/core/channel_selector.py) 的 `build_generation_config()` 决定：

| 环节开关 | 取值 | 说明 |
|------|---------|---------|
| `enable_clarify` | 已确认需求恒 False | 澄清已在需求分析链路完成；DAG 条件路由仍以它为准 |
| `enable_impact_analysis` | 仅优化需求 | 由 SRR 的 `optimization_context` 决定 |
| `enable_multi_type_generation` | 恒 True | 4 类型分类生成（功能/边界/异常/安全），质量保证核心 |
| `enable_full_review` | 恒 True | 完整审核，质量保证核心 |

> **历史**：v1.6 前曾有 FAST/FULL 通道分级，因误判 FAST 导致只生成 1 个用例而废止
> （恒走完整流程）；v3.4.4 清理掉全部 FAST 死代码，channel 字段不复存在。

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
| v2.8.5x ~ v2.8.56 | 2026-08-05 | 知识问答纠正审核 + KB 检索优化 + 已删除内容过滤修复 + 归档静默失败修复 + 澄清冗余问题优化 | ⭐⭐⭐⭐ |
| v2.8.57 | 2026-08-06 | 进度条优化：ClarifyAgent LLM 调用级进度上报 + 阶段时间线+进度条组件 | ⭐⭐⭐⭐ |
| v2.8.58 | 2026-08-06 | 归档向量化改为 Celery 异步派发（修复归档按钮一直转圈） | ⭐⭐⭐⭐ |
| v2.8.59 | 2026-08-06 | 彻底清理 v2.8.55 已移除功能的残留代码（kb_access_log 占位条目、phase_breakdown 字段、死代码方法） | ⭐⭐ |
| v2.8.60 | 2026-08-06 | 修复 legacy_data_impact 字段被 LLM 推断预填充 + 移除"字段为空就提问"的补全式澄清 + 全面代码质量治理 | ⭐⭐⭐⭐⭐ |
| v2.8.61 | 2026-08-06 | 修复 ClarifyAgent LLM 语义检测失灵（_generate_questions 从未被调用） + 跨节矛盾检测 + 引用未定义检测 | ⭐⭐⭐⭐⭐ |
| v2.8.62 | 2026-08-06 | LLM 语义检测前移至 KB 查询之前 + issues_detected 转 questions 后处理 | ⭐⭐⭐⭐⭐ |
| v2.8.63 | 2026-08-06 | 引用表述预扫描注入 hint，强制 LLM 逐条判断引用未定义 | ⭐⭐⭐⭐⭐ |
| v2.8.64 | 2026-08-06 | PRD 新建功能文档专项检测（上限处理/降级触发/风险-能力一致性/立场不一致） + 多文件上传跨文档矛盾检测（【文档：XXX】标记边界） | ⭐⭐⭐⭐⭐ |
| v2.8.65 | 2026-08-07 | LLM 语义检测问题分流：completeness/ambiguity 类先经 KB 查询，未答的才转人工澄清；conflict 类直接人工 | ⭐⭐⭐⭐⭐ |
| v2.8.66 | 2026-08-08 | 澄清问题「具体到功能点」归属注入 + 引用未定义主题兜底收集（防不收敛） | ⭐⭐⭐⭐ |
| v2.8.7x | 2026-08-09 | KB 查询语义遗漏检测（字段存在≠内容充分）+ KB 查询日志中文化 + 澄清不兜底 + 删除需求列表页 | ⭐⭐⭐⭐ |
| v2.9.0 | 2026-08-11 | 需求检索去重按 go_live_date 取最新 + RRF 时间衰减因子 + 字段级片段提取 | ⭐⭐⭐⭐ |
| v3.0.0 | 2026-08-12 | 提示词质量优化 + 影响分析修复（双向 BFS 覆盖被依赖功能点）+ 同名 FP 合并 | ⭐⭐⭐⭐⭐ |
| v3.0.1 | 2026-08-14 | 多轮澄清收敛重构（禁止发散）+ 依赖边随确认自动审核 + 同名 FP 状态节点折叠 | ⭐⭐⭐⭐⭐ |
| v3.1.0 | 2026-08-17 | 架构设计文档 + 前端使用手册整理（文档化发布） | ⭐⭐ |
| v3.1.1 | 2026-08-17 | 修复切换产品时「加载项目列表失败」（PBKDF2 线程池化 + 连接池防御 + 前端超时重试） | ⭐⭐⭐⭐ |

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

### 6.13 v2.8.5x — v2.8.56 — 知识问答纠正审核 + KB 检索优化 + 已删除内容过滤修复

**架构变更（⭐⭐⭐⭐ 重要版本群）**：

#### v2.8.51 — Milvus 向量删除一致性修复
- `delete_requirement` / `delete_test_case` 未调用 `flush()` 导致 BOUNDED 一致性下查询仍返回已删数据
- 修复：所有 delete 操作后立即 flush，并在批量删除产品/项目时降级为单条清理 + flush

#### v2.8.52 — 需求分析事务一致性 + 会话管理
- ReqAgent 改为返回 ParseResult 元组替代实例属性，消除单例并发污染
- 澄清提交、重生成、影响分析统一在单事务中持久化 Requirement + FP，失败时整体回滚
- 后台任务异常回调防止协程静默消亡；advisory lock 避免并发分析任务冲突
- 分析失败时回滚 Requirement status 到 original_status，不再无条件降级为 draft

#### v2.8.53 — XMind 导出字段修复 + 跨 FP 交互中文化
- req_xmind.py 修复跨 FP 交互、假设类型、共享业务规则、旧版 SRR 顶层字段丢失问题（修复 K/L/M）
- 项目管理页对齐设计稿配色与布局
- database.py 触发器自动维护 search_vec（Bug C-1）

#### v2.8.54 — 优化需求 LLM 语义判断
- 替代关键词匹配：LLM 判断"改已有的"vs"建全新的"，结果存入 srr["_llm_is_optimization"]
- 强/弱优化关键词拆分，避免"调整/完善/增强"误判
- 关键词仅作为 LLM 未返回时的最终兜底

#### v2.8.55 — KB 检索优化（Plan C）
- 移除冗余检索：删除全局 _search_similar_requirements 调用和 _enrich_with_pg 的 6 个子查询（节省 ~800ms Milvus 时间 + 6 个 PG 查询）
- RRF 时间衰减因子：final_score = rrf_score * (0.5 + 0.5 * 0.5^(age_days/180))，替代应用层 go_live_date 覆盖排序
- LLM 校验版本感知：verification_items 携带 go_live_date + project_version，优先采纳最新版本
- 字段级片段精度：extracted_answer 提取针对问题的具体片段而非整字段值
- 版本感知去重：优先保留 go_live_date 最新版本而非相似度最高版本

#### v2.8.56 — 知识问答纠正审核流程 + KB 已删除内容过滤修复 + 归档静默失败修复 + 澄清冗余问题优化

**归档静默失败修复**（Bug 修复）：
- 项目归档接口 `POST /api/products/projects/{proj_id}/archive` 向量写入失败时静默吞掉异常，未回写 `vectorization_status`，导致用户以为归档成功但 KB 实际无数据，检索不出来
- 修复：归档时回写 `vectorization_status` 字段（completed/failed），记录失败列表 `kb_failed`，返回值中携带失败列表供前端展示并提示用户
- KB 查询接口增加 `status='archived'` 过滤，避免跨产品数据泄漏

**澄清冗余问题优化**（功能优化）：
- ClarifyAgent 语义遗漏检测模式中 LLM 未看到功能点详情，导致生成"答案已在 FP summary 中"的冗余澄清问题
- 修复：在 `_generate_kb_queries` 语义遗漏检测模式中注入 `fp_summary` 功能点详情（最多 15 个 FP 的 name+summary），让 LLM 能看到已有内容

**提示词管理死代码清理**（可维护性）：
- `prompt_manager.py` 注册表中 `clarify_agent.SYSTEM_PROMPT` 是死代码——`_generate_questions` 方法从未被主流程调用（主流程实际走 `_generate_kb_queries` + `_refine_unresolved_to_questions`）
- 清理：从 `_PROMPT_REGISTRY` 移除该注册项，避免用户在提示词管理界面误修改无效提示词
- 常量定义保留在 `app/prompts/clarify_agent.py`，仅注册表移除

**纠正审核流程**（新增完整审核工作流）：
- 知识问答页提交纠正后状态为 `pending_review`，不再直接归档到向量库
- 新增纠正审核页（`/corrections` 路由），支持按状态（待审核/已通过/已拒绝/全部）+ 模块筛选
- 审核通过：根据颗粒度执行不同归档策略
  - `requirement`（整篇需求）：归档为新需求并嵌入向量库
  - `functional_point`（单个功能点）：删除原 FP 向量并重新嵌入纠正内容（部分纠正）
- 审核拒绝：不进入向量库，状态标记为 `rejected`
- KnowledgeQA 表新增 7 个字段：correction_status / correction_module / correction_target_type / correction_target_id / reviewed_by / reviewed_at / review_notes
- DEFAULT_MENUS 和角色菜单加入 `correction_review`

**KB 已删除内容过滤修复**（Bug 修复）：
- `list_kb_entries_by_project` 查询需求时未按 `status='archived'` 过滤，导致从知识库删除（恢复为 active）的需求仍展示在项目详情页
- `list_kb_projects` 的 requirement_count / test_case_count 统计未按 `status='archived'` 过滤，数字虚高
- 修复：两个接口均加上 `Requirement.status == "archived"` 过滤条件

**新增文件**：
- `frontend/src/pages/CorrectionReview.tsx`：纠正审核页面
- `backend/tests/test_v2_56_kqa_review.py`：19 个单元测试覆盖 schema/迁移/API/前端
- `backend/migrations/versions/v2_56_add_kqa_review_fields.py`：数据库迁移脚本

---

### 6.14 v2.8.57 — 进度条优化（ClarifyAgent LLM 调用级进度 + 阶段时间线组件）

**问题背景**：
ClarifyAgent 内部有 6-7 次 LLM 调用（优化检测、KB 查询生成、KB 检索、答案校验、字段冲突裁决、未答精炼），但全部压缩在 `phase=kb_search, progress=40` 一条记录中。前端 RequirementClarify.tsx 丢弃了 `progress/phase/phase_timings` 字段，用户只能看到旋转图标和一行文字。重新生成场景同样简陋。ProjectManagement.tsx 中 analyzing 状态只显示静态标签。

**后端优化**：
- `ClarifyAgent.clarify()` 新增 `progress_callback` 参数，在每个 LLM 调用节点上报进度
- 进度区间细化：40→41（KB状态检查）→44（优化检测）→48（KB查询生成）→52（KB检索+校验）→60（KB应用+冲突裁决）→66（未答精炼）→70（澄清完成）
- `_run_requirement_analysis` 传入回调，更新 `phase=kb_search` 的 progress 和 message
- `_run_regenerate` 传入回调，将 40-70 区间线性映射到 55-80（重新生成已有前置进度）
- 回调异常降级为日志，不中断主流程

**前端优化**：
- `RequirementClarify.tsx` 增加阶段时间线+进度条组件（5 阶段圆点+连接线+百分比进度条+阶段耗时）
  - 复用于提交澄清和重新生成两个场景
  - 展示已完成阶段耗时和当前阶段进行中耗时
  - 失败/完成状态有独立样式
- `ProjectManagement.tsx` analyzing 状态需求展示迷你进度条
  - 轮询 `getTaskByReq` 获取关联任务进度（3s 间隔）
  - 状态徽章旁边展示 60px 宽迷你进度条+百分比+阶段标签
  - 无 analyzing 需求时自动停止轮询

**修改文件**：
- `backend/app/agents/clarify_agent.py`：增加 `progress_callback` 参数和 `_report` 封装
- `backend/app/api/requirements.py`：`_run_requirement_analysis` 和 `_run_regenerate` 传入回调
- `frontend/src/pages/RequirementClarify.tsx`：阶段时间线+进度条组件
- `frontend/src/pages/ProjectManagement.tsx`：analyzing 迷你进度条+轮询

---

### 6.15 v2.8.58 — 归档向量化改为 Celery 异步派发（修复归档按钮一直转圈）

**问题根因**：
架构设计文档（§6.1 v1.1.0）明确要求"PG 同步 + Celery 异步 Milvus 双写"，但项目归档接口 `POST /api/products/projects/{proj_id}/archive` 实际实现是**同步**调用 `vector_store.add_requirement`，在 HTTP 请求中循环执行所有需求和 FP 的 embedding 计算 + Milvus 写入。当需求/FP 数量较多或 embedding 模型加载慢时，HTTP 请求长时间阻塞，前端归档按钮一直转圈。

对比发现 `knowledge.py` 和 `knowledge_qa.py` 都正确使用了 `celery_app.send_task` 异步派发，唯独 `products.py` 归档接口遗漏。

**修复方案**：
- 归档接口 PG commit 后，改为 `celery_app.send_task` 异步派发向量化任务
- 需求初始 `vectorization_status = "pending"`，Celery 任务完成后回写 `completed/failed`
- Celery 不可用时（未安装）降级为同步写入（保持兼容）
- 返回值新增 `vectorization_mode` 字段（async/sync），前端可感知处理模式

**修改文件**：
- `backend/app/api/products.py`：归档接口向量写入改为 Celery 异步派发
- `frontend/src/pages/ProjectManagement.tsx`：ArchiveModal 适配异步响应

**验证**：
- 后端 41/41 测试通过（v2.55/v2.56 套件）
- 前端 TypeScript 无错误，构建成功
- 归档接口现在秒级返回（PG 落库 + send_task 派发），向量化在 Celery Worker 后台异步进行

---

### 6.16 v2.8.60 — 修复 legacy_data_impact 字段被 LLM 推断预填充 + 全面代码质量治理

本版本包含两部分改进：(A) legacy_data_impact 字段预填充修复 + (B) 全面代码质量治理。

#### A. 修复 legacy_data_impact 字段被 LLM 推断预填充

**问题根因**：
需求 REQ-1785984203941-1648（DevBlog 博客平台 v1.1 迭代优化）解析后，`legacy_data_impact.impact_scope`、`migration_plan`、`rollback_and_validation`、`risks` 字段被 LLM 基于功能点描述自行推断填充。但原始文档中并未明确描述历史数据影响分析相关内容，且 ClarifyAgent 生成的两个澄清问题（是否影响历史数据、具体影响范围）尚未被用户回答。按照"原文未提及则留空，由后续澄清环节补充"原则，这些字段不应被 LLM 预填充。

**修复方案**：
1. **Prompt 修订**（`backend/app/prompts/req_agent.py`）：
   - 优化需求识别段落明确：识别到优化关键词仅设置 `is_optimization = true`
   - `legacy_data_impact` 其他字段（`impact_scope`/`migration_plan`/`rollback_and_validation`/`risks`/`user_clarification`）严格遵循"原文明确写出才提取"原则
   - 新增三条约束：禁止基于功能点描述推断、禁止使用占位符（如"待技术评审确定"）、历史数据影响分析内容必须能在原文找到对应描述
   - Schema 示例注释更新为"仅从原文档明确写出处提取，未提及留空"
2. **存量数据修复**：清理需求 REQ-1785984203941-1648 中被错误预填充的字段，仅保留 `is_optimization = true`

#### B. 全面代码质量治理

基于对后端（33 项）、前端（33 项）、配置/测试/文档（31 项）共 70+ 项问题的分析，按优先级分三批处理：

**第一批 — 安全/正确性修复**：
1. `backend/app/config.py`:367 — 修复 MASTER_KEY 读取使用 `os.environ.get` 而非 `self.MASTER_KEY` 的 bug（.env 文件加载的值在 python -m app.main 启动时不可见，导致生产环境误报 RuntimeError）
2. `backend/app/models/utcf.py`:139 — 修复 generator 字段硬编码过时版本号 `ai-test-gen v1.2.0`（当前版本 2.8.60）
3. `docker-compose.intranet.yml`:63-65 — 补齐生产环境必需密钥 `APP_SECRET_KEY` 和 `MASTER_KEY`（原缺失导致 DEBUG=false 时启动失败）
4. `backend/migrations/versions/v2_56_add_kqa_review_fields.py` — 删除断裂的 Alembic 迁移文件（down_revision 指向不存在的 v2_55_kb_optimization，且项目实际用 SCHEMA_MIGRATIONS 字典做迁移）
5. `backend/app/api/requirements.py`:5862 — 静默吞异常 `except Exception: pass` 改为日志记录

**第二批 — 垃圾代码清理与结构优化**：
6. 删除 `backend/test_e2e_payload.json` 临时测试残留文件
7. 删除 `backend/app/exporters/xmind.py:211` 的 `export_xmind` 兼容别名（全仓无调用方）
8. 删除 `backend/app/agents/gen_agent.py:670` 和 `backend/app/core/llm_client.py:985` 的冗余局部 `settings` 导入
9. 移动 `scripts/test_*.py` 5 个一次性脚本到 `scripts/legacy/`（项目硬约束要求根目录不含一次性脚本）
10. 合并 `backend/app/api/export.py` 与 `testcases.py` 中重复的 `_extract_fp`/`_extract_fp_id` 函数（逻辑完全一致，改为从 testcases 导入）
11. 清理 `backend/requirements.txt` 中未使用的依赖 `tenacity` 和 `markdown`
12. 更新 `backend/app/api/__init__.py` 文档字符串（原只列 6 个路由，实际 13 个）
13. 修复 `backend/app/db/milvus_store.py:982,1011` 的 `v2.8.5` 笔误（应为 `v2.8.50`）

**第三批 — 前端 bug 修复与文档同步**：
14. `frontend/src/pages/TestCases.tsx`:355 — 修复 AbortController 超时定时器未清理的内存泄漏（5 分钟 setTimeout 未保存返回值，组件卸载时无法 clearTimeout）
15. `frontend/src/auth.tsx`:9 — 删除未使用的 `useEffect` 导入
16. `frontend/src/pages/RequirementDetail.tsx`:3 — 删除未使用的 `Eye`、`Clock`、`brTypeLabel`、`acTypeLabel` 导入
17. `frontend/src/components/FPSRRViewer.tsx`:10 — 删除未使用的 `acTypeLabel` 导入
18. `README.md`:3 — 版本号 `v1.9.0` 更新为 `v2.8.60`
19. `README.md`:398 — `DeepSeek-V4` 修正为 `deepseek-chat (DeepSeek-V3)`
20. `docs/11-architecture-design-and-changelog.md`:4 — 版本号 `v2.2` 更新为 `v2.8.60`
21. `.gitignore`:54-57 — 新增 `backend/.keys/`、`*.key`、`*.master.key` 忽略规则（防止主密钥误提交）
22. `.gitignore`:119-123 — 新增根目录临时报告文件忽略规则（BUG_REPORT_*.md 等）

**修改文件清单**（共 22 个文件）：
- 后端：config.py、utcf.py、requirements.py、export.py、gen_agent.py、llm_client.py、xmind.py、milvus_store.py、api/__init__.py、requirements.txt、prompts/req_agent.py
- 前端：auth.tsx、RequirementDetail.tsx、FPSRRViewer.tsx、TestCases.tsx、package.json
- 配置：docker-compose.intranet.yml、.gitignore、backend/app/VERSION
- 文档：README.md、docs/11-architecture-design-and-changelog.md
- 删除：backend/migrations/、backend/test_e2e_payload.json
- 移动：scripts/test_*.py → scripts/legacy/

**验证**：
- 后端 10 个修改文件 Python 语法检查全部通过
- 前端 TypeScript 检查无错误，构建成功（10.60s）
- config.py 加载验证：MASTER_KEY/APP_SECRET_KEY 字段正确存在
- utcf.py generator 默认值验证：`'ai-test-gen'`（无过时版本号）
- export.py → testcases.py 导入链验证：无循环依赖（仅 torch DLL 环境问题阻断，与代码无关）
- 7 项修改专项漏洞扫描全部通过（无循环导入、无类型不兼容、无残留引用、无下游依赖破坏）

**未处理的已知问题**（需独立任务专项处理）：
- God 模块拆分（requirements.py 6049 行、clarify_agent.py 3087 行、milvus_store.py 1081 行等）
- db/models.py 按业务域拆分（27 个 ORM 类集中一处）
- 测试体系重构（改用 pytest 规范、补全 core/db 模块测试覆盖）
- 前端超大文件拆分（RequirementDetail.tsx 2370 行、DependencyGraph.tsx 2289 行）
- 前端 FP 模块选择器抽取公共组件（FPSRRViewer 与 FunctionalPoints 约 200 行重复）

#### C. 移除"字段为空就提问"的补全式澄清问题

**问题根因**：
需求 REQ-1785994445742-9919 分析时产生了3个不靠谱的"字段补全式"澄清问题：
- `kb_legacy_data_impact_impact_scope`：「本次迭代对存量历史数据的影响范围包括哪些？」
- `kb_legacy_data_impact_migration_plan`：「针对历史图片和版本等存量数据有何迁移方案？」
- `kb_legacy_data_impact_rollback_and_validation`：「系统上线发布时的数据验证与回滚方案是什么？」

这些问题是因为 `legacy_data_impact.impact_scope`、`migration_plan`、`rollback_and_validation` 字段为空而触发的。但这些字段为空是允许的——`_detect_optimization_impact` 方法已生成2个固定提问（`kb_optimization_impact` 多选 + `kb_optimization_impact_scope` 文本输入）由用户回答后再生成完整内容。字段补全式提问不靠谱：某些内容本来就可能为空，问题应基于原始文本分析产生（逻辑冲突、描述不清、缺少详细信息等），参考 Claude Code 的 ask 模式。

**修复方案**：
从 `COMPLETENESS_CHECKLIST` 中移除 `optimization` 级别的三个字段检查（`legacy_data_impact.impact_scope`、`migration_plan`、`rollback_and_validation`）。修改后：
- 字段为空不再触发"字段补全式"问题
- 保留 `_detect_optimization_impact` 方法生成的2个固定优化提问
- LLM 基于原始文本分析产生的问题（逻辑冲突、描述不清、缺少详细信息等）不受影响
- `_check_completeness` 中的 `optimization` 级别检查逻辑自动失效（`COMPLETENESS_CHECKLIST` 不再有该键）
- 所有引用 `optimization_missing` 的代码得到空数组，不会报错

**修改文件**：
- `backend/app/prompts/clarify_agent.py`：注释移除 `COMPLETENESS_CHECKLIST["optimization"]`

---

### 6.29 v3.4 — 端到端可靠性收口：并发互斥 / 出箱 / 输入指纹 / 发布门禁 / HITL（2026-09-15）

对应 `docs/15` 的 P0 全部收口 + P1 的 4.3/4.4 落地（逐项证据与验收见 docs/15 §6 实施进度表）。

**A. 并发互斥原子化（§4.3，`core/task_guard.py`）**
- 问题：`/api/generate` "session A 查活动任务 → session B 插任务"存在 TOCTOU；
  `resume=True` 可绕过互斥。
- 修复：`reserve_generation_slot()` 同一事务内 advisory lock → 僵尸回收 → 活动检查 → 插入；
  `init_db` Step 3b 增加 partial unique index 作 DB 层最终防线；resume 不再绕过。
- 验收：20 并发同 req_id 恰好 1 成功（探针 11/11）；SSE 隔离由 ContextVar 承载并实测不串流。

**B. PG→Milvus 事务性出箱（§4.4，`core/vector_outbox.py`）**
- 问题：双写不在同一事务，Milvus 写早于 PG commit；TestCase 无向量化状态可查。
- 修复：业务事务内 enqueue 意图（不 commit），relay 至少一次幂等投递（退避重试）；
  metadata 带 revision/content_hash；`reconcile_cases()` 双向对账（删除判定只在一份实现里）。
- 验收：三个崩溃点（commit 前 / Milvus 成功后 / ack 前）注入崩溃均收敛且不重复（探针 13/13）。

**C. durable execution（§4.2，`core/pipeline_fingerprint.py`，部分完成）**
- 实测缺陷：`PipelineState` 未声明 `resume/resume_checkpoint` → **断点续跑从未生效**（同源第 4 例）。
- 修复：补声明 + checkpoint 输入指纹（8 字段）+ 续跑前比对（不一致 409，`fallback_to_restart` 放行）。
- 验收：完成 2 个 FP 后续跑不再调用已完成 FP；旧 revision checkpoint 被拒（探针 8/8）。
- 剩余：PostgresSaver 与任务迁出 API 进程（互为前提，专项处理）。

**D. 发布门禁与 HITL（§4.1 / §4.5）**
- 发布决策 fail-closed 并三处持久化；`review_reports.passed` 与发布决策对齐；
- prompt 激活改为服务器端评估证据（`evaluation_runs` + `eval_prompt_candidate.py`）；
- 前端 `PublicationPanel` / KB 回流跳过态；审阅台（`review_generation_run.py`）+ 判定执行（`apply_verdicts.py`）。

**E. 测试体系**
- `tests/conftest.py` 分层：脚本式套件默认不收集，unit 层无服务可跑；CI 去掉 `continue-on-error`。

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
   ↓
v2.8.5x~v2.8.56 知识问答纠正审核 + KB 检索优化 + 已删除内容过滤修复 + 归档静默失败修复 + 澄清冗余问题优化
   ↓
v2.8.57 进度条优化（ClarifyAgent LLM 调用级进度 + 阶段时间线组件）
   ↓
v2.8.58 归档向量化改为 Celery 异步派发（修复归档按钮一直转圈）
   ↓
v2.8.60 修复 legacy_data_impact 字段被 LLM 推断预填充（澄清未回答前应留空）
   ↓
v2.8.61 修复 ClarifyAgent LLM 语义检测失灵 + 跨节矛盾 + 引用未定义检测
   ↓
v2.8.62 LLM 语义检测前移至 KB 查询之前 + issues_detected 转 questions
   ↓
v2.8.63 引用表述预扫描注入 hint，强制 LLM 逐条判断引用未定义
   ↓
v2.8.65 LLM 语义检测问题先经 KB 查询（分流：KB 可答自动补全，KB 未答转人工）
   ↓
v2.8.66 澄清问题具体到功能点归属注入（防不收敛）
   ↓
v3.0.0 提示词质量优化 + 影响分析双向 BFS + 同名 FP 合并
   ↓
v3.0.1 多轮澄清收敛重构（eval_answer_coverage，禁止发散，一轮收敛）
   ↓
v3.1.0 架构文档 + 使用手册整理（文档化发布）
   ↓
v3.1.1 修复切换产品报错（PBKDF2 线程池化 + 连接池防御 + 前端超时重试）
```

### 6.17 v2.8.61 — 修复 ClarifyAgent LLM 语义检测失灵 + 跨节矛盾 + 引用未定义检测

本版本修复了三个相关问题：(A) `_generate_questions` 从未被调用导致 LLM 语义检测失灵；(B) 跨节矛盾检测能力增强；(C) 引用未定义检测能力增强。

#### A. 修复 `_generate_questions` 从未被调用（核心根因）

**问题根因**：
用户上传需求 REQ-1785997647705-3535 后，指出两个明显问题未被 ClarifyAgent 识别：
1. **矛盾点（OPT-A-02 历史版本）**：优化方案节写"手动版最多保留 50 个，超出提示清理"，但同一节的验收标准节写"手动标记版本长期保留，不设数量上限"——上限既存在又不存在
2. **明显模糊点（OPT-A-01 图片素材库）**：新增验收条目"素材库上传支持的图片格式、单文件大小限制及超限提示，与现有图片上传规则完全一致"——但本文档通篇没有写这些规则具体是什么

调查发现：`ClarifyAgent._generate_questions` 方法（本应是 LLM 基于 SRR 全文进行 6 类语义遗漏检测的入口）**从未被主流程调用**。该方法在 prompts.py 中有完整的 SYSTEM_PROMPT，指示 LLM 检测：
- 输入校验规则缺失
- 权限要求缺失
- 边界条件缺失
- 异常处理缺失
- 数据约束缺失
- 模糊表述（如"适当""必要时"等）

但主流程只调用了：
1. `_check_completeness`（规则层字段存在性检查，只能发现字段缺失）
2. `_generate_kb_queries`（LLM 生成 KB 查询）
3. `_detect_logical_contradictions`（正则模式规则检测，只能发现同一句中的直接对立表述）
4. `_detect_optimization_impact`（优化需求场景固定问题）

这些方法都无法发现跨节矛盾和引用未定义的模糊表述——即使用了大力气写的"基于原始文本分析的 ask 模式"完全失灵。

**修复方案**：
在主流程步骤⑧处理完 KB 未答问题后，新增步骤⑧c 调用 `_generate_questions`：
- 将 augmented_srr（已 KB 补全的 SRR）传入，让 LLM 基于 SRR 全文检测语义遗漏
- LLM 生成的问题 id 加 `llm_` 前缀避免与 KB 问题 id 冲突
- 与 KB 未答问题合并后通过 `_dedup_questions` 去重
- LLM 失败不阻断流水线（KB 未答问题仍可用）

#### B. 跨节矛盾检测增强

**原问题**：
`_detect_logical_contradictions` 使用正则模式只能检测同一句中的对立表述（如"存在 X 且不存在 X"），无法检测跨节矛盾。

**增强方案**：
在 SYSTEM_PROMPT 中新增"跨节矛盾"检测维度，明确指示 LLM：
- 对每个数量约束、上限、保留策略、时间窗口等关键约束，扫描全文所有章节
- 检查"优化方案"节与"验收标准"节对同一约束的描述是否一致
- 检查"功能描述"与"验收标准"对同一字段的取值范围、上限、规则是否一致
- 检查"业务规则"与"数据约束"对同一字段的定义是否一致
- 检测到则作为 critical 级别 conflict 提问

#### C. 引用未定义检测增强

**原问题**：
原 SYSTEM_PROMPT 只检测模糊量词（"适当""必要时"），未识别"引用了某规则但文档未定义"的模糊表述。

**增强方案**：
在 SYSTEM_PROMPT 中新增"引用未定义检测"维度，明确指示 LLM 识别以下模式：
- 「与现有 XX 规则一致」「按现有 XX 执行」「沿用现有 XX」——但本文档通篇未定义"现有 XX 规则"具体内容
- 「参照行业标准 XX」「遵循 XX 规范」——但未指明具体哪个标准/规范
- 「按既有逻辑处理」「沿用原系统 XX」——但未描述既有逻辑/原系统的具体内容
- 检测到则作为 important 级别 ambiguity 提问

### 6.18 v2.8.62 — LLM 语义检测前移 + issues_detected 转 questions

本版本完成两项优化：(A) 将 LLM 语义检测从流程末尾前移至 KB 查询之前；(B) 修复 LLM 返回 issues_detected 但未生成 questions 时的问题丢失。

#### A. LLM 语义检测前移至 KB 查询之前（步骤 ②c）

**原问题**：
v2.8.61 虽然修复了 `_generate_questions` 未被调用的问题，但调用位置在步骤⑧c（KB 查询之后）。用户期望的流程是"LLM 分析原始文本找问题→KB 澄清→人工澄清"，即 LLM 应先于 KB 发现问题，让 KB 有机会解决 LLM 发现的问题。

**修复方案**：
将 `_generate_questions` 调用从步骤⑧c 前移到步骤②c（规则检测之后、KB 查询生成之前）：
- LLM 基于 `_raw_text` 检测跨节矛盾/引用未定义/信息缺失/模糊表述
- LLM 生成的问题 id 加 `llm_` 前缀避免冲突
- 在步骤⑧c 与 KB 未答问题合并后去重
- LLM 失败不阻断流水线（KB 仍可用）

**效果**：LLM 发现的问题先于 KB 查询，KB 有机会自动解决部分问题，减少人工澄清负担。

#### B. issues_detected 转 questions 后处理

**原问题**：
部分 LLM 模型（如 deepseek-v4-flash）只返回 `issues_detected` 结构化检测结果，而不生成 `questions` 字段，导致检测到的跨节矛盾、引用未定义等问题被完全丢弃。

**修复方案**：
在 `_generate_questions` 方法中新增后处理逻辑：当 `questions` 为空但 `issues_detected` 非空时，自动将 issues 转换为 questions：
- conflict 类型 → input_type=select，提供候选选项
- 其他类型 → input_type=text
- optional 级别跳过

### 6.19 v2.8.63 — 引用表述预扫描注入 hint

本版本解决了 LLM 处理超长文本时漏检"引用未定义"问题的能力瓶颈。

**原问题**：
v2.8.62 验证发现，同一 LLM（deepseek-v4-flash）能识别 OPT-A-02 跨节矛盾，但漏检 OPT-A-01 引用未定义（"素材库上传支持的图片格式、单文件大小限制及超限提示，与现有图片上传规则完全一致"）。根因是 LLM 在处理 12 万字符的超长 user_prompt 时，容易忽略 system_prompt 中的"引用未定义检测"维度。

**修复方案**：
在 `_generate_questions` 调用 LLM 之前，预先用正则扫描 `_raw_text` 中的所有引用表述（"与现有XX一致""沿用现有XX""参照XX规范"等），将扫描结果作为 hint 注入 user_prompt，强制 LLM 逐条判断每个引用是否定义了具体内容：

1. 新增 `_scan_reference_expressions` 方法，用 10 个正则模式扫描引用表述
2. 新增 `_REFERENCE_PATTERNS` 类属性，定义引用表述匹配规则
3. `build_user_prompt` 新增 `reference_hints` 参数，注入"引用表述预扫描清单"
4. 清单中明确指示："必须对清单中的每一项判断文档是否定义了具体内容，未定义则生成 ambiguity 问题"

**验证结果**（需求 REQ-1785997647705-3535）：

| 检测项 | 修复前（v2.8.62） | 修复后（v2.8.63） |
|--------|-------------------|-------------------|
| 问题总数 | 2 个 | 8 个 |
| OPT-A-02 跨节矛盾 | ✓ 识别 | ✓ 识别（Q004, conflict） |
| OPT-A-01 引用未定义 | ✗ 漏检 | ✓ **已识别**（Q005, ambiguity） |
| 额外引用未定义 | ✗ | ✓ 识别（Q006, 自动保存保留策略） |
| ambiguity_score | 0.0 | 0.15 |

LLM 基于预扫描的 3 条引用表述清单，逐条判断后准确生成了 OPT-A-01 的 ambiguity 问题。

### 6.20 v2.8.64 — PRD 新建功能专项检测 + 跨文档矛盾检测

本版本解决了测试中发现的两个结构性能力缺口：**PRD 类新建功能文档检测过宽** + **多文件上传无跨文档矛盾检测**。

**原问题**：
对 DevBlog PRD v1.0（7,500 字）的测试发现，ClarifyAgent 仅返回 1 个通用问题，但实际文档中存在至少 4 处可检测的细节缺口：
1. 范围声明「不做管理后台/内容审核」vs 风险缓解「建议提供最小运营处理入口」的立场不一致
2. 历史版本上限 20 版未定义达到上限后的处理规则
3. 微信资质降级方案未说明触发时机和恢复条件
4. 风险评估要求运营处理能力，但权限模型不含管理员角色

同时发现：多文件上传时，虽然文件已用 `---` 合并，但**没有文档边界标记**，LLM 无法识别「v1.0 PRD 说不做 vs v1.1 优化需求说要做」这类跨文档矛盾。

**修复方案**：

#### 1. 多文件合并增加【文档：XXX】边界标记
- `_parse_uploaded_files` 函数在合并时增加 `【文档：filename】` 前缀标记
- 此前后台多文件上传协程路径（`_run_upload_pipeline`）已添加标记，regenerate 等端点复用的 `_parse_uploaded_files` 未添加，现已统一

#### 2. ClarifyAgent SYSTEM_PROMPT 全面强化
- **步骤 0.5 检测清单**：扩展为 5 项，新增「跨文档矛盾扫描」（多【文档：XXX】标记比对）和「PRD 深度扫描」（5 项细目）
- **逻辑矛盾检测**：新增 2 个子类
  - 跨文档矛盾：检测不同【文档：X】vs【文档：Y】对同一功能的范围/约束不一致
  - 同文档立场不一致：PRD 中「范围外 vs 风险缓解建议」的隐含冲突（非直接对反但预期不确定）
- **新增「检测项 6：新建功能/PRD 专项检测」**，对应已有的「优化需求专项检测」：
  - **a) 上限落地规则**：所有「保留最近 N 版/最多 N 条」需定义达到上限后的处理行为
  - **b) 降级触发与恢复**：所有「若无法则降级」需定义触发判定、用户通知、恢复机制三件事
  - **c) 风险-能力一致性**：风险缓解要求的操作能力需在权限/功能模型中有对应设计
  - **d) 范围声明 vs 建议的立场不一致**：功能范围 vs 风险缓解对同一问题的态度冲突
  - **e) 模糊条件判定主体**：所有「必要时/适当/视情况」需明确谁判定、怎么判定

**验证结果**：见 v2.8.64 回归测试报告。

### 6.21 v2.8.65 - LLM 语义检测问题先经 KB 查询（修复人工澄清绕过知识库）

本版本解决了「人工澄清问题没有先使用 KB 查询」的核心流程缺陷。

**原问题**：
对 REQ-1786061938371-5197 的排查发现，11 个澄清问题中有 6 个是 `llm_` 前缀（LLM 语义检测生成），**完全绕过了 KB 查询**。根因是 v2.8.62 把 LLM 语义检测前移到 KB 查询之前（步骤②c），但在步骤⑧c 直接 `questions.extend(llm_semantic_questions)` 把 LLM 问题全部合并进最终列表，没有让 KB 有机会回答这些问题。

实际 KB 查询日志只查了 3 个问题（规则层缺口生成），而 LLM 语义检测发现的 6 个问题（如「现有图片上传规则是什么」「自动保存保留策略是什么」这类引用未定义问题，KB 中很可能已有定义）从未经过 KB 查询。

**修复方案**：LLM 语义问题按 category 分流

```
LLM 语义检测问题（步骤②c）
├── completeness/ambiguity 类 → 转为额外 KB 查询（步骤③，field 加 llm_semantic. 前缀）
│   ├── KB 能答 → 自动补全 SRR（步骤⑤），不生成人工问题
│   └── KB 未答 → 步骤⑧ 转为 kb_ 前缀人工问题（保留原始 category/context/options）
└── conflict 类（文档内部矛盾，KB 无法解决）→ 步骤⑧c 直接合并为人工问题
```

**关键改动**：
1. **步骤②c 后**：新增分流逻辑，`llm_semantic_questions` 拆为 `llm_kb_eligible_questions`（completeness/ambiguity）和 `llm_direct_questions`（conflict）
2. **步骤③**：把 `llm_kb_eligible_questions` 转为 KB 查询格式（`_from_llm_semantic=True` 标记），与规则层缺口生成的 KB 查询合并；MAX_KB_QUERIES 上限从 5 提升到 8 容纳额外查询
3. **`_query_kb_for_answers`**：透传 `_from_llm_semantic` 标记到 kb_answers
4. **步骤⑧**：KB 未答问题生成人工问题时，若来自 LLM 语义检测，保留原始 category/context/options（不被模板覆盖）
5. **步骤⑧c**：只合并 `llm_direct_questions`（conflict 类），不再 extend 所有 LLM 问题

**效果**：符合用户预期「先 KB 查询，没有结果的才让人工澄清」。LLM 发现的「引用未定义」类问题会先查 KB 历史需求，KB 已有定义的自动补全，KB 没有的才转人工澄清。

### 6.22 v2.8.66 — 澄清问题具体到功能点归属注入（2026-08-08）

**核心问题**：引用未定义 / 沿用规则类澄清问题未指明其归属的功能点，用户在澄清时无法定位是哪条规则需要补全，且同一主题可能反复提问导致不收敛。

**修复方案**：在 ClarifyAgent 最终问题去重后，统一调用 `ClarifyAgent._attach_fp_scope` 为问题注入功能点归属——引用未定义类问题（question 含"引用未定义"或"规则+未给出/未定义/沿用"）把功能点归属写入 question 文本，并补充 `_ref_theme_label` 兜底收集主题，确保同一主题回答后不再重复追问。

### 6.23 v2.8.7x — KB 查询质量与交互优化（2026-08-09）

本系列聚焦知识库查询的**语义质量**与**可读性**：

- **语义遗漏检测**：字段存在 ≠ 内容充分。规律层缺口为零时，引入 LLM 基于 SRR 全文检测 6 类语义遗漏（输入校验规则、权限要求、边界条件、异常处理、数据约束、模糊表述），避免"字段非空但内容空泛"逃过质量门禁。
- **KB 查询日志中文化**：将 `_kb_query_logs` 中的英文/内部字段名映射为中文可读描述（通过 STATUS_LABELS / 字段映射表统一翻译），提升前端知识问答与澄清来源展示的可读性。
- **澄清不兜底**：LLM 判定问题相关但未返回精炼答案（信息不充分）时，直接转人工澄清并提示「需人工澄清」，不使用降低回答质量的兜底内容。
- **删除需求列表页**：`/requirements` 路由重定向到 `/projects`，上传需求功能内嵌到项目管理页弹窗。

### 6.24 v2.9.0 — KB 检索纯度与版本时效增强（2026-08-11）

- **需求检索去重优先保留最新版本**：去重逻辑从"相似度最高"改为"优先保留 go_live_date 最新版本"，避免旧版本覆盖新版本检索结果。
- **RRF 时间衰减因子**：融合阶段引入时间衰减（`final_score = rrf_score * (0.5 + 0.5 * 0.5^(age_days/180))`），抑制应用层覆盖排序对相关性的破坏。
- **字段级片段提取**：从 `document_full` 中提取针对问题的具体片段，而非返回整字段值，减少知识库噪音。
- **LLM 校验注入版本时效**：prompt 中注入文档 go_live_date 与 project_version，优先采纳最新版本内容。

### 6.25 v3.0.0 — 提示词质量优化 + 影响分析修复 + 同名 FP 合并（2026-08-12）

**三大主线**：

1. **提示词全面泛化**：审查所有 Agent 提示词，移除针对特定需求语料的死板模式，强化领域无关总原则，泛化示例（避免分页误报、敏感词编造、行业惯例编造），提升跨需求场景的准确性与一致性。
2. **影响分析修复**：FP 级影响分析原来只遍历被依赖功能点（单向），修复 `_bfs_fp_reverse` 为**双向 BFS**，同时覆盖被依赖功能点（target/上游）与依赖者（source/下游），并标注 direction；需求确认时自动将该需求作为 source 的 pending 依赖边置为 approved。
3. **同名 FP 合并**：功能点按名称去重合并，支持版本状态展开（同一节点给出不同需求版本的对应状态）。

**伤害修复**：影响分析显示依赖关系的 I1–I6 端到端验证全部通过。

### 6.26 v3.0.1 — 多轮澄清收敛重构（禁止发散）（2026-08-14）

这是澄清机制的一次**核心流程改造**，直接决定澄清是否"一轮收敛"。

**原问题**：提交澄清答案后系统从零做完整缺口检测，重新生成一整轮新问题，导致已答问题被重复提问、问题发散、无法收敛（其他问题详情见历史验证）。

**重构方案**：不再从零生成新问题，而是**评估上一轮问题答案的覆盖情况**（`eval_answer_coverage`，@ClarifyAgent）：

```
提交上一轮答案后（POST /requirements/{id}/clarify）
  └─ eval_answer_coverage(final_srr, clarify_history)
      ├─ 答案为空（用户跳过）→ 视为不关注，不再追问
      ├─ 答案充分覆盖问题核心（covered）→ 收敛
      └─ 答案模糊/不完整/答非所问（uncovered/partial）→ 仅对该问题追问（保留原问题 + _followup_reason）
```

- **禁止发散**：评估 prompt 明确只判断"该问题答案是否充分"，不因答案引出新话题而生成新问题。
- **正常 1 轮正确回答即完全收敛**；仅当回答模糊或不完整才产生第二轮追问；最多 3 轮。
- 配套修复：重解析后将 `_raw_text` 恢复为原始需求文本（避免用户澄清文本中的"引用未定义"关键词触发新一轮误检），清除导致不收敛的历史污染。

**效果**：经新产品端到端验证，第一轮 7 个问题收到完整理想答案后，`eval_answer_coverage` 判定所有答案 covered，followup=0，需求直接进入 pending_review，**实现一轮收敛**。

### 6.27 v3.1.0 — 架构文档与使用手册整理（2026-08-17）

文档化发布版本，无新代码功能：

- 更新本文档版本总览、演进主线至 v3.1.0。
- 完善前端在线使用手册（`frontend/src/pages/UserManual.tsx`），补充多轮澄清收敛机制说明。
- 版本号统一同步（后端 `app/VERSION`、前端 `package.json` / `package-lock.json`、`README.md`、`TEST_REPORT.md`）。

### 6.28 v3.1.1 — 彻底修复切换产品「加载项目列表失败」（2026-08-17）

**现象**：用户切换左上角产品时偶发弹「加载项目列表失败」，nginx 日志对应请求返回 **499**（客户端放弃），API 侧相同查询正常仅需几 ms 却无响应记录。

**根因**（双层）：
1. **触发器——登录阻塞事件循环**：`verify_password`/`hash_password` 使用 PBKDF2（60 万次迭代），是同步 CPU 密集调用。直接在 async 路由中执行会**阻塞单 worker 事件循环 0.3~15s**，期间所有请求（含切换产品时的 `/projects` 列表）排队挂死。用户频繁登录（含输错密码）都会触发。
2. **放大器——连接池无自愈**：引擎缺 `pool_pre_ping`/`pool_recycle`/语句超时，池内连接僵死后查询永远无响应，宁挂死不报错。

**修复**：
- **后端（治本）**：
  - `auth.py`/`users.py`：登录、改密、建用户的 PBKDF2 校验与哈希全部移入 `asyncio.to_thread`，不再阻塞事件循环（登录接口瞬时 + 全系统不被卡）。
  - `database.py` 连接池：加 `pool_pre_ping`（取连接前探活剔除死连接）、`pool_recycle=1800`、语句超时 60s（`statement_timeout`）、事务 idle 超时 120s、TCP 连接超时 10s——**任何异常宁报错不挂死**。
  - `config.py`：全量 SQL echo 与 `DEBUG` 解耦，新增 `SQL_ECHO`（默认关闭），减少日志拖累事件循环。
- **前端（兜底）**：
  - `client.ts`：所有请求自带 30s 超时（永不无限挂起）+ 超时/瞬断自动重试 GET + 502 HTML 响应的友好报错。
  - `ProjectManagement.tsx`：登录过期跳转时不再误弹「加载项目列表失败」。

**验证**：
- 8 产品 × 2 接口（projects/kb-projects）API 回归全 200。
- 池参数会话级实测生效（`statement_timeout=1min`、`pre_ping=True`、`recycle=1800`）。
- 登录 PBKDF2 进行中并发 `/projects` 48ms 返回（修复前被卡 8~15s）。
- 浏览器实测 10 次切换（项目管理 8 + 知识库 2）：0 报错，网络全 2xx。

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
| **原型链接解析（未来要做）** | 上传时识别需求文档（docx 超链接/URL）中的**原型链接**（Axure/墨刀/蓝湖/Figma 等），直接扫描原型内容（全平台无统一 API、结果受授权与平台多样性限制），用于**丰富需求分析内容**（补充正文缺失的页面/交互/字段信息，供澄清、总 SRR、FP 识别消费）。架构拟采用**平台适配器 + 三通道（优先开放 API 直扫结构化 → 无 API 走 headless DOM 文本提取 → 截图多模态描述兜底，复用现有 vision 链路）**；扫描内容需经 LLM 判重去噪，冲突走现有澄清机制。当前**仅作规划，暂不实现**。 |

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
