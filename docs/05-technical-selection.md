# 05 — 技术选型分析

> 各模块技术方案对比、推荐选型及迁移路径

---

## 1. 选型总览

| 模块 | 推荐方案 | 核心决策因素 |
|------|---------|-------------|
| LLM 推理 | DeepSeek-V4 (主力) + GPT-4o (复杂场景) | 成本/效果平衡、中文优化 |
| LLM 多提供商路由 | LiteLLM | 统一接口、故障转移、100+提供商支持 |
| Agent 编排 | LangGraph | 状态管理、DAG 流程、可观测性 |
| 向量数据库 | Milvus | 统一向量库后端（ChromaDB 已弃用） |
| 关系数据库 | PostgreSQL 16 + pgvector | 成熟稳定、JSONB 支持、向量扩展 |
| 嵌入模型 | bge-large-zh-v1.5 | 中文语义、开源、1024维 |
| 后端框架 | FastAPI (Python) | AI 生态、异步支持、开发效率 |
| 前端框架 | React 18 + Tailwind CSS | 组件化、生态丰富 |
| 限速/断路器 | Redis + Python (Lua原子操作) | 分布式安全、Token Bucket |
| 重试框架 | asyncio + 自定义实现 (全抖动) | 重试预算、4xx不重试 |
| 任务队列 | Celery + Redis | 异步生成、批量任务调度、死信队列 |
| 可观测性 | LangFuse + Prometheus + Grafana | LLM 调用全链路追踪 |
| 对象存储 | MinIO | S3 兼容、私有部署 |

---

## 2. LLM 提供商标杆对比

### 2.1 候选方案

| 维度 | DeepSeek-V4 | GPT-4o | Claude 3.5 Sonnet | Qwen2.5-72B | 本地 vLLM |
|------|------------|--------|-------------------|-------------|-----------|
| **结构化输出** | ★★★★☆ | ★★★★★ | ★★★★★ | ★★★★☆ | ★★★☆☆ |
| **中文理解** | ★★★★★ | ★★★★☆ | ★★★★☆ | ★★★★★ | ★★★★☆ |
| **指令遵循** | ★★★★☆ | ★★★★★ | ★★★★★ | ★★★★☆ | ★★★☆☆ |
| **推理能力** | ★★★★☆ | ★★★★★ | ★★★★★ | ★★★★☆ | ★★★☆☆ |
| **成本 (1M tokens)** | ¥2 / ¥8 | $5 / $15 | $3 / $15 | 免费 (开源) | 硬件成本 |
| **延迟 (avg)** | ~3s | ~4s | ~3s | ~5s | 取决于 GPU |
| **数据隐私** | API 调用 | API 调用 | API 调用 | 可本地部署 | 完全本地 |
| **Function Calling** | 支持 | 原生 | 原生 | 支持 | 需额外配置 |

### 2.2 推荐策略：分层调用

```
需求复杂度
    │
    ├── 简单需求 (标准 CRUD、表单校验) ──► DeepSeek-V4 (成本优先)
    │
    ├── 中等需求 (状态机、复杂业务规则) ──► DeepSeek-V4 / GPT-4o (质量优先)
    │
    └── 高复杂度 (多系统集成、安全审计) ──► GPT-4o / Claude (效果优先)
```

**成本估算**（按日均 500 个需求处理）：

| 场景 | 日均 Token 消耗 | 日均成本 (DeepSeek) | 日均成本 (GPT-4o) |
|------|----------------|--------------------|-------------------|
| 需求解析 | ~2M tokens | ¥16 | ~$10 |
| 用例生成 | ~5M tokens | ¥40 | ~$25 |
| 质量审核 | ~1M tokens | ¥8 | ~$5 |
| **合计** | **~8M tokens** | **¥64/日** | **~$40/日** |

> DeepSeek 成本约为 GPT-4o 的 1/5，在中文场景效果相当，推荐作为主力。

---

## 3. Agent 框架对比

| 维度 | LangGraph | CrewAI | AutoGen | 自研 (DAG Engine) |
|------|-----------|--------|---------|-------------------|
| **编排模式** | 有向图 (StateGraph) | 角色协作 (Sequential) | 对话式 (Chat) | 自定义 DAG |
| **状态管理** | 内置 Checkpoint | 需自行实现 | 需自行实现 | 需自行实现 |
| **人机交互** | interrupt() 中断 | 需自行实现 | 需要人工代理 | 需自行实现 |
| **可观测性** | LangSmith/LangFuse | 有限 | 有限 | 需自行对接 |
| **并行执行** | Send() API | 原生支持 | 原生支持 | 需自行实现 |
| **学习曲线** | 中等 | 低 | 中等 | 高 |
| **社区活跃度** | ★★★★★ | ★★★★☆ | ★★★☆☆ | — |

### 3.1 推荐：LangGraph

**理由**：
1. **StateGraph 天然匹配**：需求解析 → 知识检索 → 用例生成 → 审核 → 输出的 DAG 流程
2. **Checkpoint 机制**：支持断点续跑，人工审核后可从中断点继续
3. **与 LangFuse 深度集成**：开箱即用的 LLM 调用追踪
4. **Send() 并行**：GenAgent 的多类型并行生成天然适合

### 3.2 LangGraph 流程定义示例

```python
from langgraph.graph import StateGraph, END

class GenState(TypedDict):
    raw_requirement: str
    srr: dict | None
    match_results: list
    generated_cases: list
    reviewed_cases: list
    final_output: dict

workflow = StateGraph(GenState)

# 添加节点
workflow.add_node("parse_requirement", req_agent.parse)
workflow.add_node("retrieve_knowledge", match_agent.retrieve)
workflow.add_node("generate_cases", gen_agent.generate)      # 并行出 4 个子任务
workflow.add_node("review_cases", review_agent.review)

# 定义边
workflow.add_edge("parse_requirement", "retrieve_knowledge")
workflow.add_edge("retrieve_knowledge", "generate_cases")
workflow.add_conditional_edges(
    "review_cases",
    decide_next,
    {
        "approved": END,
        "regenerate": "generate_cases",  # 低质量回退重新生成
        "human_review": "human_review_node"  # 中断等待人工
    }
)
```

---

## 4. 向量数据库选型

| 维度 | ChromaDB | Milvus | pgvector | Qdrant | Weaviate |
|------|----------|--------|----------|--------|----------|
| **部署复杂度** | 极低 (嵌入式) | 高 (K8s) | 低 (PG 扩展) | 中 (Docker) | 中 |
| **规模上限** | ~100K 向量 | 十亿级 | ~1M 向量 | 十亿级 | 十亿级 |
| **检索速度** | 中 | 高 (GPU) | 中 | 高 | 高 |
| **混合检索** | 有限 | 支持 | 支持 (SQL + 向量) | 支持 | 支持 (hybrid) |
| **中文嵌入** | 依赖模型 | 依赖模型 | 依赖模型 | 依赖模型 | 依赖模型 |
| **运维成本** | 零 | 高 | 低 (复用 PG) | 中 | 中 |
| **过滤能力** | metadata filter | scalar filter | SQL WHERE | payload filter | GraphQL filter |

### 4.1 推荐路径

```
MVP (验证概念): ChromaDB — 零配置启动，< 1 万向量
    │
    ▼
成长期 (<10 万向量): pgvector  — 复用 PostgreSQL，减少运维组件
    │
    ▼
成熟期 (>10 万向量): Milvus  — 分布式、高性能、混合检索
```

---

## 5. 嵌入模型选型

| 模型 | 维度 | 中文效果 | 开源 | 推理速度 | 适用场景 |
|------|------|---------|------|---------|---------|
| **bge-large-zh-v1.5** | 1024 | ★★★★★ | MIT | 快 | 通用中文语义检索 |
| m3e-large | 1024 | ★★★★☆ | Apache 2.0 | 快 | 中文文本匹配 |
| text-embedding-3-large | 3072 | ★★★★☆ | 闭源 (API) | 快 | 多语言 |
| stella-base-zh-v3-1792d | 1792 | ★★★★★ | MIT | 中 | 高精度中文 |
| bge-m3 | 1024 | ★★★★★ | MIT | 中 | 多语言 + 长文本 |

### 5.1 推荐：bge-large-zh-v1.5

**理由**：
- MTEB 中文榜单 Top 3，语义检索任务表现优异
- 1024 维，存储和检索效率高
- MIT 协议，可自由使用和微调
- 支持 max_length=512，覆盖绝大部分需求文本

**备选**：如果需求文档包含大量英文技术术语，推荐 **bge-m3** (多语言)。

---

## 6. 后端框架选型

| 维度 | FastAPI (Python) | Spring Boot (Java) |
|------|-----------------|-------------------|
| **AI/LLM 生态** | ★★★★★ (LangChain/LlamaIndex/LangGraph) | ★★★☆☆ (Spring AI 尚在早期) |
| **开发效率** | ★★★★★ | ★★★☆☆ |
| **异步支持** | 原生 async/await | WebFlux (复杂) |
| **类型安全** | Pydantic v2 (强) | Java 原生 (强) |
| **性能** | 中高 (Uvicorn) | 高 (Netty) |
| **团队匹配** | 适合 AI/数据团队 | 适合传统 Java 团队 |

> 推荐 **FastAPI**，因为 LLM 生态的核心库（LangChain, LlamaIndex, ChromaDB, Milvus SDK）均为 Python 优先。

---

## 7. 可观测性选型

```
LangFuse (LLM 链路追踪)
├── Token 用量统计
├── 延迟分布
├── Prompt 版本对比
└── 生成质量评分

Prometheus + Grafana (系统监控)
├── API QPS/延迟/错误率
├── Agent 各环节耗时
├── 知识库检索延迟
└── 数据库连接池

PostgreSQL (业务指标)
├── 用例接受率趋势
├── 人工修正率趋势
├── 覆盖率变化
└── 用户活跃度
```

---

## 8. 部署方案对比

| 方案 | 适用阶段 | 优势 | 劣势 |
|------|---------|------|------|
| **Docker Compose** | 开发/小团队 | 一键启动、配置简单 | 无高可用、单点故障 |
| **K8s + Helm** | 生产/中大型 | 弹性伸缩、滚动更新 | 运维复杂度高 |
| **Serverless (云函数)** | 轻量/突发流量 | 按量付费、免运维 | 冷启动延迟、状态管理难 |

### 8.1 推荐路径

```
开发阶段 → Docker Compose (单机全栈)
    │
    ▼
生产阶段 → K8s (核心服务) + 云服务 (PostgreSQL/Redis/MinIO)
    │           │
    │           ├── FastAPI (Deployment × 3)
    │           ├── Celery Worker (Deployment × 5)
    │           ├── ChromaDB/Milvus (StatefulSet × 3)
    │           └── Redis (云服务)
    │
    └── 监控: Prometheus Operator + Grafana + LangFuse Cloud
```

---

## 9. 稳定性组件选型

### 9.1 多提供商路由: LiteLLM

| 维度 | LiteLLM | 自研 Router |
|------|---------|-------------|
| 提供商支持 | 100+ (OpenAI/DeepSeek/Claude/本地) | 需逐一适配 |
| 故障转移 | 内置 (顺序/并行对冲) | 需自行实现 |
| 统一接口 | ✅ 所有提供商同一 API | 需自行抽象 |
| 成本追踪 | 内置 | 需自行实现 |
| 社区活跃 | ★★★★★ | - |

> 推荐 LiteLLM，一个库解决多提供商路由 + 故障转移 + 成本追踪。

### 9.2 限速/断路器: Redis + Python

```
限速: Token Bucket (Redis Lua 原子操作)
  ├── RPM bucket: 限制请求频率
  ├── TPM bucket: 限制 Token 消耗
  └── 分层配额: 全局 + 用户双约束

断路器: 三状态机 (Redis 后端)
  ├── CLOSED → 失败率>20% → OPEN
  ├── OPEN → 冷却30s → HALF_OPEN
  └── HALF_OPEN → 成功3次 → CLOSED
  LLM特有触发: 成本>$10 / 轮次>20 / 质量<0.7
```

### 9.3 重试: 自定义全抖动框架

```python
# 全抖动公式: sleep = random(0, min(cap, base * 2^attempt))
# 原则: 重试预算<10% / 只在最外层重试 / 429读Retry-After / 4xx不重试
# 备选: tenacity 库 (但需自定义抖动和预算逻辑)
```

### 9.4 死信队列: Redis List

```python
# 重试耗尽的任务写入 DLQ
# 记录: task, error, max_retries, timestamp, status
# 用途: 审计、根因分析、手动恢复、异常检测
```

---

## 10. 技术栈依赖清单

```txt
# 核心
langgraph>=0.2.0
langchain>=0.3.0
pymilvus>=2.4.0
fastapi>=0.115.0
pydantic>=2.0
uvicorn[standard]>=0.30.0

# LLM
openai>=1.50.0  # GPT-4o API + DeepSeek (兼容接口)
litellm>=1.50.0  # 多提供商路由 + 故障转移

# 嵌入 + 重排
sentence-transformers>=3.0.0  # bge-large-zh 嵌入 + CrossEncoder 重排（bge-reranker-base）

# 数据库
psycopg[binary]>=3.2.0
asyncpg>=0.29.0
sqlalchemy[asyncio]>=2.0

# 任务队列
celery[redis]>=5.4.0
redis>=5.0

# 可观测性
langfuse>=2.50.0
prometheus-fastapi-instrumentator>=7.0

# 测试框架适配
pytest>=8.0
gherkin>=26.0  # Gherkin 解析

# 工具
python-dotenv>=1.0
httpx>=0.27.0
tenacity>=8.0  # 重试
```

---

## 11. 容量规划与性能基准

### 11.1 单机并发能力评估

| 环节 | 单机并发能力 | 瓶颈分析 | 优化建议 |
|------|-------------|---------|---------|
| ReqAgent | ~50 并发 | LLM RPM 限制（DeepSeek 假设 500 RPM） | 多 API Key 轮换 |
| GenAgent | ~20 并发 | LLM TPM 限制（假设 100K TPM） | 批量生成减少调用次数 |
| Embedding | ~100 docs/s | CPU 推理速度（bge-large-zh） | GPU 加速或 API 嵌入 |
| PG 连接池 | ~100 并发 | 连接池大小（默认 10-20） | 增加到 50-100 |
| Vector DB 检索 | ~200 QPS | HNSW 索引性能 | 调优 M/ef 参数 |

### 11.2 限流配置

```python
# 基于 Redis Token Bucket 的双轴限速
RATE_LIMIT_CONFIG = {
    "llm_rpm": {
        "limit": 500,        # 每分钟最大请求数
        "window": 60,        # 窗口期（秒）
        "strategy": "token_bucket",
    },
    "llm_tpm": {
        "limit": 100000,     # 每分钟最大 Token 数
        "window": 60,
    },
    "embedding": {
        "limit": 100,        # 每秒最大嵌入计算数
        "window": 1,
    },
    "pg_pool": {
        "max_connections": 50,
        "timeout": 30,
    }
}

# 限流实现：Redis Lua 原子操作（分布式安全）
# 触发限流时返回 429 + Retry-After 头
```

### 11.3 扩容方案

| 场景 | 方案 | 效果 |
|------|------|------|
| LLM RPM 不足 | 多 API Key 轮换 / 升级配额 | 线性扩容 |
| GenAgent 并发不足 | FastAPI Worker × N + Redis 统一限速 | 线性扩容 |
| Embedding 瓶颈 | GPU 推理 / 嵌入 API 服务 | 10-50 倍加速 |
| PG 连接不足 | PgBouncer 连接池 + 读副本 | 2-5 倍扩容 |
| Vector DB 性能 | Milvus 集群 + 分区 + 分片 | 线性扩容 |

### 11.4 日均处理能力估算

> **单价基准**：与 [doc-00 §9.1](./00-architecture-overview.md#mvp-简化架构可行性优化) 保持一致，MVP 链路
> 单价 ¥8-16/需求（2-4 次 LLM 调用）；完整通道单价 ¥42-82/需求（20-30 次 LLM 调用）。
>
> **历史订正**：本表早期版本给出"年成本 ¥1.5-3 万 / ¥15-30 万"，与日成本 × 365 的结果相差约 100 倍，
> 系单位换算错误（误将日成本当年成本）。以下为重新核算后的数字。

| 配置 | 日均处理能力 | 单价/需求 | 日均成本 | 年成本（按 365 日计） |
|------|-------------|----------|---------|---------------------|
| MVP（单机，DeepSeek，2-4 次 LLM） | ~500 需求/日 | ¥8-16 | ¥4,000-8,000 | **¥146-292 万** |
| 标准（3 Worker，DeepSeek，2-4 次 LLM） | ~2,000 需求/日 | ¥8-16 | ¥16,000-32,000 | **¥584-1,168 万** |
| 生产（5 Worker + GPU 嵌入，完整通道 20-30 次 LLM） | ~5,000 需求/日 | ¥42-82 | ¥210,000-410,000 | **¥7,665-14,965 万** |

> **说明**：
> - 上述为 LLM 调用 + 嵌入计算的成本上限，未含人力、服务器、存储等基础设施成本。
> - **生产环境**通常不会 100% 走完整通道：[doc-00 §9.1 配置驱动通道选择](./00-architecture-overview.md#配置驱动的通道选择)
>   中复杂需求走完整通道、简单需求走 MVP 链路，实际年成本约为上表的 30-50%。
> - 若实际日均需求量低于 500/日，建议直接采用 MVP 链路（年成本 ¥29-58 万），无需进入完整通道。
> - 与 [doc-00 §9.1 MVP vs 完整架构对比表](./00-architecture-overview.md#mvp-vs-完整架构对比)
>   中的"年成本 100 需求/日 → MVP ¥20-40 万"数字一致（100 需求/日 × ¥8-16 × 365 = ¥29-58 万，区间下限对齐）。

---

## 12. 实施建议

1. **MVP 阶段只用 Docker Compose 全量部署**，避免过早引入 K8s
2. **LLM 优先使用 DeepSeek**，但保留 OpenAI 兼容接口便于切换
3. **嵌入模型本地部署**（FlagEmbedding），避免 API 调用成本和延迟
4. **LangFuse 必接**：LLM 应用的调试成本远高于传统应用，全链路追踪是必需品
5. **pgvector 作为长期方案**：在已有 PostgreSQL 的项目中，pgvector 是性价比最高的向量检索方案
