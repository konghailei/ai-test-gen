# 02 — 知识库架构设计

> 持久化历史需求与测试用例，为 AI 生成提供上下文与模式参考

---

## 1. 设计目标

知识库（Knowledge Base, KB）是 **闭环积累** 的核心载体。它存储两类关键资产：

| 资产类型 | 存储目的 | 消费方 |
|----------|---------|--------|
| **历史需求文档 (SRR)** | 为新需求提供相似功能参考 | ClarifyAgent(KB补全) → MatchAgent → GenAgent |
| **历史测试用例** | 提供用例模式、边界条件、断言模板 | MatchAgent → GenAgent |
| **测试模式库** | 高频测试模式（如 CRUD、状态机、权限校验） | GenAgent（Few-shot Prompt） |
| **需求模式库** | 同类需求的抽象维度集合 | ClarifyAgent（遗漏检测） |
| **错误案例库** | 历史生成失败/被驳回的用例及修正 | ReviewAgent（避免重复错误） |
| **追溯矩阵** | 用例↔需求双向追溯关系 | ReviewAgent + 变更影响分析 |

---

## 2. 双引擎存储架构

采用 **Vector DB + Relational DB** 的双引擎架构，各司其职：

```
┌──────────────────────────────────────────────────────┐
│                    知识库双引擎                         │
│                                                       │
│  ┌─────────────────────┐  ┌─────────────────────────┐│
│  │    Vector DB         │  │   Relational DB (PG)    ││
│  │   (语义检索引擎)       │  │   (结构化查询引擎)        ││
│  ├─────────────────────┤  ├─────────────────────────┤│
│  │ Collection: reqs     │  │ Table: requirements      ││
│  │  - 需求语义嵌入       │  │  - id, title, priority   ││
│  │  - 用例语义嵌入       │  │  - product_id, modules[] ││
│  │  - 测试模式嵌入       │  │  - tags, version         ││
│  │                      │  │  - created_at, updated_at││
│  │                      │  │  - srr_json (JSONB)      ││
│  ├─────────────────────┤  ├─────────────────────────┤│
│  │ Collection: cases    │  │ Table: test_cases         ││
│  │  - 用例语义嵌入       │  │  - id, req_id, type      ││
│  │  - 用例步骤嵌入       │  │  - status, priority      ││
│  │                      │  │  - author, reviewer      ││
│  │                      │  │  - created_at, updated_at││
│  │                      │  │  - case_json (JSONB)     ││
│  ├─────────────────────┤  ├─────────────────────────┤│
│  │ Collection: patterns │  │ Table: test_patterns      ││
│  │  - 测试模式嵌入       │  │  - id, name, category    ││
│  │  - 需求模式嵌入       │  │  - usage_count            ││
│  │                      │  │  - pattern_json (JSONB)  ││
│  ├─────────────────────┤  ├─────────────────────────┤│
│  │ Collection: req_pats │  │ Table: req_patterns       ││
│  │  - 需求模式嵌入       │  │  - id, name, modules     ││
│  │                      │  │  - required_dimensions    ││
│  ├─────────────────────┤  ├─────────────────────────┤│
│  │ Collection: errors   │  │ Table: error_cases         ││
│  │  - 错误案例嵌入       │  │  - id, original_case_id  ││
│  │                      │  │  - error_type, feedback  ││
│  │                      │  │  - corrected_case_json   ││
│  ├─────────────────────┤  ├─────────────────────────┤│
│  │                      │  │ Table: traceability       ││
│  │                      │  │  - case_id, req_id       ││
│  │                      │  │  - field, trace_type     ││
│  │                      │  │  - confidence            ││
│  └─────────────────────┘  └─────────────────────────┘│
└──────────────────────────────────────────────────────┘
```

### 2.1 向量数据库 (Vector DB)

**职责**：语义相似度检索——回答"历史上哪些需求和用例与当前需求最相似？"

**选型**：

| 方案 | 适用阶段 | 特点 |
|------|---------|------|
| **ChromaDB（已弃用）** | 开发/小规模 | 轻量、Python 原生、零配置、< 10w 向量 |
| **Milvus** | 生产/大规模 | 分布式、十亿级向量、混合检索、GPU 加速 |
| **pgvector** | 已有 PG 基础设施 | PostgreSQL 扩展、统一运维、中等规模 |
| **Qdrant** | 中等规模 | Rust 实现、高性能、过滤能力强 |

**推荐路径**：Milvus（统一向量库后端，ChromaDB 已弃用移除）

**向量存储结构**：

```python
# 需求嵌入
{
    "id": "emb-req-001",
    "embedding": [0.023, -0.451, ...],  # 1536/3072 维
    "metadata": {
        "req_id": "REQ-2024-001",
        "doc_type": "requirement",
        "module": "auth",
        "priority": "P0",
        "version": "v1.0",
        "status": "active",
        "created_at": "2024-06-24T10:00:00Z"
    },
    "text": "用户登录功能：用户通过账号密码登录系统..."  # 原文片段（用于展示）
}

# 测试用例嵌入
{
    "id": "emb-case-001",
    "embedding": [0.112, -0.389, ...],
    "metadata": {
        "case_id": "TC-2024-001",
        "req_id": "REQ-2024-001",
        "doc_type": "test_case",
        "case_type": "functional",
        "priority": "P0",
        "module": "auth",
        "status": "approved",
        "acceptance_rate": 0.92,
        "created_at": "2024-06-25T14:00:00Z"
    },
    "text": "TC-001: 正常登录流程验证 - 输入有效账号密码..."
}
```

### 2.1.1 Vector DB 完整操作示例

> ⚠️ **已废弃示例（历史保留）**：以下代码为早期 ChromaDB 用法示例，当前版本已移除 ChromaDB、统一使用 Milvus 作为唯一向量库后端，本节代码**不可直接运行**。当前接口与调用方式请以 `backend/app/db/milvus_store.py` 及各 Agent 中的 `vector_store` 单例为准。

以下以 ChromaDB 为例，展示知识库的完整操作链路（历史参考）。

#### 初始化与存入

```python
import chromadb
from sentence_transformers import SentenceTransformer

# 1. 初始化
client = chromadb.HttpClient(host="localhost", port=8000)
embedding_model = SentenceTransformer('BAAI/bge-large-zh-v1.5')

# 2. 创建 Collection（如不存在）
collection = client.get_or_create_collection(
    name="requirements",
    metadata={"hnsw:space": "cosine"}  # cosine 相似度
)

# 3. 嵌入并存入需求
def store_requirement(req_id: str, srr: dict):
    """将需求存入 Vector DB"""
    # 构建语义文本（用于嵌入）
    semantic_text = f"""
    需求标题: {srr['requirement']['title']}
    功能概述: {srr['requirement']['summary']}
    正常流程: {format_flows(srr['flows']['happy_path'])}
    异常流程: {format_flows(srr['flows'].get('alternate_flows', []))}
    业务规则: {format_rules(srr.get('business_rules', []))}
    """.strip()
    
    # 生成嵌入
    embedding = embedding_model.encode(semantic_text).tolist()
    
    # 存入 ChromaDB
    collection.add(
        ids=[req_id],
        embeddings=[embedding],
        metadatas=[{
            "req_id": srr['requirement']['id'],
            "title": srr['requirement']['title'],
            "product_id": srr['requirement'].get('product_id', ''),
            "modules": srr['requirement'].get('modules', []),
            "priority": srr['requirement']['priority'],
            "version": srr.get('version', 1),
            "status": srr.get('status', 'active'),
        }],
        documents=[semantic_text]
    )
    print(f"✅ 已存入需求 {req_id}")

# 4. 存入测试用例（类似流程）
def store_test_case(case_id: str, case_json: dict, req_id: str):
    semantic_text = f"""
    用例标题: {case_json['title']}
    测试步骤: {format_steps(case_json['steps'])}
    预期结果: {format_expects(case_json['steps'])}
    """.strip()
    embedding = embedding_model.encode(semantic_text).tolist()
    collection = client.get_collection("test_cases")
    collection.add(
        ids=[case_id],
        embeddings=[embedding],
        metadatas=[{
            "case_id": case_id,
            "req_id": req_id,
            "type": case_json.get('type', 'functional'),
            "priority": case_json.get('priority', 'P2'),
            "status": case_json.get('status', 'approved'),
        }],
        documents=[semantic_text]
    )
```

#### 检索（MatchAgent 使用）

```python
def search_similar_requirements(
    query_text: str,
    product_id: str = None,
    modules: list[str] = None,
    top_k: int = 10,
    pg_conn=None
) -> list[dict]:
    """
    语义检索相似需求。
    支持产品隔离 + 模块过滤。
    """
    # 1. 生成查询嵌入
    query_embedding = embedding_model.encode(query_text).tolist()
    
    # 2. 构建 where 过滤条件（Metadata 过滤）
    where_clause = {}
    if product_id:
        where_clause["product_id"] = product_id  # 产品隔离
    if modules:
        # ChromaDB $in 操作符：匹配任意模块
        where_clause["module"] = {"$in": modules}
    
    # 3. 执行向量检索
    collection = client.get_collection("requirements")
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        where=where_clause if where_clause else None,
        include=["metadatas", "documents", "distances"]
    )
    
    # 4. 转换结果为标准格式
    matches = []
    for i, (req_id, metadata, distance) in enumerate(zip(
        results["ids"][0],
        results["metadatas"][0],
        results["distances"][0]
    )):
        # ChromaDB 返回余弦距离 (0=完全相同, 2=完全不同)
        # 转换为相似度 (0.0~1.0)
        similarity = 1 - distance / 2
        matches.append({
            "req_id": req_id,
            "title": metadata["title"],
            "module": metadata["module"],
            "similarity": round(similarity, 4),
            "snippet": results["documents"][0][i][:200]
        })
    
    # 5. 如果提供了 PG 连接，补充 SRR JSON
    if pg_conn:
        req_ids = [m["req_id"] for m in matches]
        cur = pg_conn.cursor()
        cur.execute(
            "SELECT id, srr_json FROM requirements WHERE id = ANY(%s)",
            (req_ids,)
        )
        srr_map = {row[0]: row[1] for row in cur.fetchall()}
        for m in matches:
            m["srr_json"] = srr_map.get(m["req_id"])
    
    return matches

# 使用示例
similar_reqs = search_similar_requirements(
    query_text="用户登录功能，支持手机号登录",
    module="auth",
    top_k=5,
    pg_conn=pg_conn
)
for r in similar_reqs:
    print(f"  {r['req_id']} ({r['similarity']:.2f}): {r['title']}")
# 输出:
#  REQ-AUTH-001 (0.9234): 用户登录功能
#  REQ-AUTH-015 (0.8912): 用户注册功能（含手机号）
#  ...
```

#### 更新与删除

```python
def update_requirement_embedding(req_id: str, new_srr: dict):
    """需求变更时，更新向量嵌入"""
    semantic_text = build_semantic_text(new_srr)
    new_embedding = embedding_model.encode(semantic_text).tolist()
    
    collection = client.get_collection("requirements")
    collection.update(
        ids=[req_id],
        embeddings=[new_embedding],
        metadatas=[{
            "req_id": new_srr['requirement']['id'],
            "product_id": new_srr['requirement'].get('product_id', ''),
            "title": new_srr['requirement']['title'],
            "module": (new_srr['requirement'].get('modules', [''])[0]
                      if new_srr['requirement'].get('modules') else ''),
            "modules": new_srr['requirement'].get('modules', []),
            "version": new_srr.get('version', 1),
        }],
        documents=[semantic_text]
    )
    print(f"✅ 已更新需求 {req_id} 的向量嵌入")

def delete_requirement_embedding(req_id: str):
    """需求删除/归档时，从向量库移除"""
    collection = client.get_collection("requirements")
    collection.delete(ids=[req_id])
    print(f"✅ 已从向量库删除 {req_id}")
```

### 2.2 关系数据库 (PostgreSQL)

**职责**：结构化查询——回答"P0 的 auth 模块有哪些测试用例？最近修改了哪些需求？"

**核心表结构**：

```sql
-- 需求表（含版本管理 + 产品/模块隔离）
CREATE TABLE requirements (
    id              VARCHAR(32) PRIMARY KEY,
    product_id      VARCHAR(64) NOT NULL,         -- 产品隔离键
    title           VARCHAR(255) NOT NULL,
    priority        VARCHAR(4) CHECK (priority IN ('P0','P1','P2','P3')),
    modules         TEXT[] DEFAULT '{}',           -- PostgreSQL 数组：关联多个模块
    tags            TEXT[] DEFAULT '{}',
    version         INTEGER DEFAULT 1,              -- 版本号：v1, v2, v3...
    status          VARCHAR(16) DEFAULT 'active',  -- active / archived / deleted
    srr_json        JSONB NOT NULL,
    raw_text        TEXT,
    source_type     VARCHAR(32),  -- 'markdown','jira','prd','conversation'
    source_url      TEXT,
    created_by      VARCHAR(64),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    -- 版本追溯字段（核心：支持优化需求上下文补全）
    replaced_by     VARCHAR(32) REFERENCES requirements(id),  -- 新版本需求ID（本需求被哪个需求替代）
    supersedes      VARCHAR(32) REFERENCES requirements(id),  -- 旧版本需求ID（本需求替代了哪个需求）
    change_type     VARCHAR(16) DEFAULT 'new',  -- new / optimized / bugfix / deprecated
    change_summary  TEXT,  -- 本次变更说明（用于影响分析）
    diff_json       JSONB  -- 与上一版本的差异化描述（自动生成）
    
    -- 索引
    INDEX idx_req_product (product_id),
    INDEX idx_req_modules USING GIN (modules),
    INDEX idx_req_status (status),
    INDEX idx_req_version (version)
);

-- 需求依赖关系表（依赖图谱的结构化存储，与 Neo4j 图数据库同步）
CREATE TABLE req_dependencies (
    id              SERIAL PRIMARY KEY,
    source_req_id   VARCHAR(32) REFERENCES requirements(id) NOT NULL,  -- 源需求（依赖方）
    target_req_id   VARCHAR(32) REFERENCES requirements(id) NOT NULL,  -- 目标需求（被依赖方）
    relation_type   VARCHAR(32) NOT NULL,  -- replaces(版本替代) / depends_on(依赖) / related_to(关联) / conflicts(冲突)
    strength        VARCHAR(16) DEFAULT 'strong',  -- strong(强依赖，必须同步变更) / weak(弱关联，建议关注)
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(source_req_id, target_req_id, relation_type)
);

-- 需求-用例追溯表（增强版，支持影响分析）
CREATE TABLE req_case_traceability (
    id              SERIAL PRIMARY KEY,
    req_id          VARCHAR(32) REFERENCES requirements(id) NOT NULL,
    case_id         VARCHAR(32) REFERENCES test_cases(id) NOT NULL,
    trace_type      VARCHAR(32) DEFAULT 'direct',  -- direct(直接验证) / indirect(间接覆盖) / regression(回归)
    confidence      NUMERIC(3,2) DEFAULT 1.0,      -- AI 标注的追溯置信度
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(req_id, case_id, trace_type)
);

-- 测试用例表
CREATE TABLE test_cases (
    id              VARCHAR(32) PRIMARY KEY,
    req_id          VARCHAR(32) REFERENCES requirements(id),
    title           VARCHAR(255) NOT NULL,
    type            VARCHAR(32) NOT NULL,  -- 'functional','boundary','security','performance','integration'
    priority        VARCHAR(4),
    status          VARCHAR(16) DEFAULT 'draft',  -- 'draft','reviewed','approved','rejected'
    case_json       JSONB NOT NULL,
    source          VARCHAR(32) DEFAULT 'ai_generated',  -- 'ai_generated','manual','imported'
    gen_batch_id    VARCHAR(64),  -- 同一批次生成的用例共享此 ID
    confidence      NUMERIC(3,2),  -- AI 生成置信度 0.00-1.00
    reviewed_by     VARCHAR(64),
    reviewed_at     TIMESTAMPTZ,
    feedback        JSONB,  -- 人工修正记录
    execution_count INTEGER DEFAULT 0,
    last_pass_rate  NUMERIC(3,2),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 测试模式表
CREATE TABLE test_patterns (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(128) NOT NULL UNIQUE,
    category        VARCHAR(32) NOT NULL,  -- 'crud','state_machine','auth','validation','workflow'
    pattern_json    JSONB NOT NULL,
    usage_count     INTEGER DEFAULT 0,
    success_rate    NUMERIC(3,2),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 错误案例表
CREATE TABLE error_cases (
    id              SERIAL PRIMARY KEY,
    original_case_id VARCHAR(32) REFERENCES test_cases(id),
    error_type      VARCHAR(32) NOT NULL,  -- 'missing_precondition','wrong_boundary','redundant','ambiguous'
    error_detail    TEXT,
    feedback        TEXT,
    corrected_json  JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 需求模式表 (ClarifyAgent 遗漏检测数据源)
CREATE TABLE req_patterns (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(128) NOT NULL UNIQUE,
    modules         TEXT[] DEFAULT '{}',  -- 适用的模块列表
    required_dimensions JSONB NOT NULL,   -- 期望的维度列表
    common_boundary JSONB,                -- 常见边界条件
    risk_points     JSONB,                -- 典型风险点
    usage_count     INTEGER DEFAULT 0,
    success_rate    NUMERIC(3,2),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 追溯矩阵表 (用例↔需求双向追溯)
CREATE TABLE traceability (
    id              SERIAL PRIMARY KEY,
    test_case_id    VARCHAR(32) REFERENCES test_cases(id),
    requirement_id  VARCHAR(32) REFERENCES requirements(id),
    requirement_field VARCHAR(128),       -- 关联到SRR的哪个字段
    trace_type      VARCHAR(16) NOT NULL, -- 'direct','inferred','derived'
    confidence      NUMERIC(3,2),
    evidence        TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 索引
CREATE INDEX idx_req_module ON requirements(module);
CREATE INDEX idx_req_priority ON requirements(priority);
CREATE INDEX idx_req_status ON requirements(status);
CREATE INDEX idx_req_tags ON requirements USING GIN(tags);
CREATE INDEX idx_req_srr ON requirements USING GIN(srr_json jsonb_path_ops);

CREATE INDEX idx_case_req ON test_cases(req_id);
CREATE INDEX idx_case_type ON test_cases(type);
CREATE INDEX idx_case_status ON test_cases(status);
CREATE INDEX idx_case_confidence ON test_cases(confidence);
CREATE INDEX idx_case_gen_batch ON test_cases(gen_batch_id);
CREATE INDEX idx_trace_case ON traceability(test_case_id);
CREATE INDEX idx_trace_req ON traceability(requirement_id);

-- pgvector 扩展索引 (如使用 pgvector)
-- CREATE EXTENSION vector;
-- CREATE TABLE requirement_embeddings (
--     req_id VARCHAR(32) REFERENCES requirements(id),
--     embedding vector(1536)
-- );
-- CREATE INDEX ON requirement_embeddings USING ivfflat (embedding vector_cosine_ops);
```### 2.5 配置示例

#### 环境变量配置 (.env)

```bash
# ── Vector DB 配置 ──
VECTOR_DB_TYPE=milvus  # 统一使用 Milvus（ChromaDB 已弃用）
CHROMADB_HOST=localhost
CHROMADB_PORT=8000
MILVUS_HOST=localhost
MILVUS_PORT=19530

# ── Embedding 模型配置 ──
EMBEDDING_MODEL=bge-large-zh-v1.5
EMBEDDING_DEVICE=cpu  # cpu | cuda | mps
EMBEDDING_BATCH_SIZE=32

# ── PostgreSQL 配置 ──
DATABASE_URL=postgresql://user:pass@localhost:5432/ai_test_gen
PG_POOL_SIZE=10
PG_TIMEOUT=30

# ── 知识库维护配置 ──
KB_DECAY_RATE=0.005       # 时间衰减率（半衰期 ~180 天）
KB_BOOST_REUSE=1.5       # 复用加分系数
KB_MIN_WEIGHT=0.1         # 最低权重（防止完全遗忘）
KB_CLEANUP_DAYS=365     # 超过 365 天未使用的知识自动归档

# ── 检索配置 ──
MATCH_TOP_K=20             # 召回数量
MATCH_FINAL_N=5            # 最终返回数量
MATCH_SIMILARITY_THRESHOLD=0.70  # 最低相似度阈值
MATCH_ENABLE_RRF=true       # 是否启用 RRF 融合
MATCH_ENABLE_GRAPH=false    # 是否启用图谱扩展（需要 Neo4j/networkx）
```

#### Python 配置（config.py）

```python
from pydantic import BaseModel, Field
from typing import Optional

class VectorDBConfig(BaseModel):
    """Vector DB 配置"""
    type: str = Field(default="milvus", pattern="^(milvus|pgvector)$")
    host: str = "localhost"
    port: int = 8000
    collection_name: str = "requirements"
    embedding_dim: int = 1536
    distance_metric: str = Field(default="cosine", pattern="^(cosine|l2|ip)$")

class EmbeddingConfig(BaseModel):
    """Embedding 模型配置"""
    model_name: str = "BAAI/bge-large-zh-v1.5"
    device: str = "cpu"  # cpu | cuda | mps
    batch_size: int = Field(default=32, ge=1, le=128)
    normalize: bool = True
    cache_dir: Optional[str] = None

class KnowledgeBaseConfig(BaseModel):
    """知识库总配置"""
    # Vector DB
    vector_db: VectorDBConfig = Field(default_factory=VectorDBConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    
    # PostgreSQL
    database_url: str = "postgresql://localhost/ai_test_gen"
    pg_pool_size: int = Field(default=10, ge=1, le=50)
    pg_timeout: int = Field(default=30, ge=1, le=300)
    
    # 检索
    match_top_k: int = Field(default=20, ge=1, le=100)
    match_final_n: int = Field(default=5, ge=1, le=20)
    match_similarity_threshold: float = Field(default=0.70, ge=0.0, le=1.0)
    match_enable_rrf: bool = True
    match_enable_graph: bool = False
    
    # 维护
    decay_rate: float = Field(default=0.005, ge=0.0, le=0.1)
    boost_reuse: float = Field(default=1.5, ge=1.0, le=3.0)
    min_weight: float = Field(default=0.1, ge=0.0, le=1.0)
    cleanup_days: int = Field(default=365, ge=30, le=3650)

# 从环境变量加载
import os

def load_kb_config() -> KnowledgeBaseConfig:
    return KnowledgeBaseConfig(
        vector_db=VectorDBConfig(
            type=os.getenv("VECTOR_DB_TYPE", "milvus"),
            host=os.getenv("CHROMADB_HOST", "localhost"),
            port=int(os.getenv("CHROMADB_PORT", "8000")),
        ),
        embedding=EmbeddingConfig(
            model_name=os.getenv("EMBEDDING_MODEL", "BAAI/bge-large-zh-v1.5"),
            device=os.getenv("EMBEDDING_DEVICE", "cpu"),
            batch_size=int(os.getenv("EMBEDDING_BATCH_SIZE", "32")),
        ),
        database_url=os.getenv("DATABASE_URL", "postgresql://localhost/ai_test_gen"),
        match_top_k=int(os.getenv("MATCH_TOP_K", "20")),
        match_final_n=int(os.getenv("MATCH_FINAL_N", "5")),
        match_similarity_threshold=float(os.getenv("MATCH_SIMILARITY_THRESHOLD", "0.70")),
        decay_rate=float(os.getenv("KB_DECAY_RATE", "0.005")),
        cleanup_days=int(os.getenv("KB_CLEANUP_DAYS", "365")),
    )
```

---

## 2.3 需求版本管理（优化需求上下文补全的核心）

### 问题定义

当收到**优化需求**（如"优化登录流程"）时，需求文档通常只描述"改了什么"，不包含"改之前的完整状态"。这导致：
1. AI 无法理解优化前的完整上下文
2. 无法准确分析变更的影响范围
3. 回归测试范围不明确

### 解决方案：需求版本链

每个需求维护一个**版本链**，`replaced_by` / `supersedes` 字段形成双向链表：

```
REQ-AUTH-001-v1  ──replaced_by──►  REQ-AUTH-001-v2  ──replaced_by──►  REQ-AUTH-001-v3
     │                           │                             │
  (原始需求)                   (第一次优化)                   (第二次优化)
     │                           │                             │
  supersedes=NULL              supersedes=v1                 supersedes=v2
```

**版本链查询**（获取优化需求的完整上下文）：

```python
def get_requirement_version_chain(req_id: str, pg_conn) -> dict:
    """获取需求的完整版本链，用于优化需求上下文补全"""
    # 1. 找到最新版本
    cur = pg_conn.cursor()
    cur.execute("""
        WITH RECURSIVE version_chain AS (
            -- 锚点：找到当前需求
            SELECT id, title, version, supersedes, replaced_by, srr_json, change_summary, diff_json, 0 as depth
            FROM requirements WHERE id = %s
            UNION ALL
            -- 递归：向上找新版本
            SELECT r.id, r.title, r.version, r.supersedes, r.replaced_by, r.srr_json, r.change_summary, r.diff_json, vc.depth + 1
            FROM requirements r
            JOIN version_chain vc ON r.supersedes = vc.id
        )
        SELECT * FROM version_chain ORDER BY depth;
    """, (req_id,))
    chain = cur.fetchall()
    
    # 2. 找到原始版本（depth=0 或更早的版本）
    original = chain[0] if chain else None
    
    # 3. 找到所有后续版本
    optimized_versions = [r for r in chain if r['version'] > original['version']]
    
    return {
        "original": original,          # 原始需求（优化前的完整上下文）
        "current": chain[-1],          # 当前最新版本
        "history": chain,              # 完整版本链
        "change_summaries": [r['change_summary'] for r in chain if r['change_summary']]
    }

def get_original_requirement_for_optimization(new_req_id: str, pg_conn) -> dict:
    """给定优化需求ID，自动找到它替代的原始需求"""
    cur = pg_conn.cursor()
    cur.execute("""
        SELECT r_original.* 
        FROM requirements r_new
        JOIN requirements r_original ON r_new.supersedes = r_original.id
        WHERE r_new.id = %s
    """, (new_req_id,))
    return cur.fetchone()  # 返回原始需求的完整 SRR JSON
```

### 自动生成 diff_json（需求变更检测）

当新需求 `supersedes` 旧需求时，自动生成差异化描述，存入 `diff_json`：

```python
def generate_requirement_diff(old_srr: dict, new_srr: dict) -> dict:
    """
    对比新旧需求的 SRR JSON，生成结构化 diff
    用于影响分析和测试用例范围确定
    """
    diff = {
        "added": {},    # 新增的字段/内容
        "removed": {},  # 删除的字段/内容
        "modified": {}, # 修改的字段/内容
        "summary": ""   # 自然语言摘要（LLM 生成）
    }
    
    # 1. 字段级对比
    all_keys = set(list(old_srr.keys()) + list(new_srr.keys()))
    for key in all_keys:
        old_val = old_srr.get(key)
        new_val = new_srr.get(key)
        if old_val is None and new_val is not None:
            diff["added"][key] = new_val
        elif old_val is not None and new_val is None:
            diff["removed"][key] = old_val
        elif old_val != new_val:
            diff["modified"][key] = {"old": old_val, "new": new_val}
    
    # 2. 使用 LLM 生成自然语言摘要（用于 Prompt 注入）
    prompt = f"""
    请对比以下两个需求版本，用中文生成一段简洁的变更说明（200字以内），
    重点说明：改了什么功能、影响了哪些流程、是否需要新增/修改测试用例。

    旧版本：{json.dumps(old_srr, ensure_ascii=False, indent=2)}
    新版本：{json.dumps(new_srr, ensure_ascii=False, indent=2)}

    输出格式：
    {{
        "summary": "变更摘要",
        "impacted_flows": ["受影响的流程1", "受影响的流程2"],
        "test_impact": "需要新增/修改/删除哪些测试"
    }}
    """
    llm_response = call_llm(prompt, temperature=0.1, response_format="json")
    diff["summary"] = json.loads(llm_response)

    return diff
```

---

## 2.4 需求依赖图谱（影响范围分析的核心）

### 设计目标

| 目标 | 说明 |
|------|------|
| **上下文补全** | 优化需求 → 自动找到原始需求 + 关联需求 |
| **影响范围分析** | 需求变更 → 自动分析哪些用例需要更新/新增/回归 |
| **追溯性保证** | 每个用例 → 能追溯到原始需求 → 能追溯到变更历史 |
| **回归测试范围** | 需求变更 → 自动推荐需要执行的回归测试用例 |

### 图谱模型（基于 ATRI 成熟方案）

```
节点（Node）: 需求（Requirement）
边（Edge）:   依赖关系（Dependency）

关系类型：
  replaces(O, N)    — 新版本 N 替代旧版本 O（版本链）
  depends_on(A, B) — 需求 A 依赖需求 B（A 的功能需要 B 先实现）
  related_to(A, B) — 需求 A 与需求 B 相关（同一模块、相似功能）
  conflicts(A, B)  — 需求 A 与需求 B 冲突（不应同时生效）
```

**图谱存储方案**：

| 方案 | 适用场景 | 技术 |
|------|---------|------|
| **networkx（内存图）** | 开发/小规模（< 5000 节点） | Python 原生，零依赖 |
| **Neo4j（图数据库）** | 生产/大规模 | Cypher 查询，原生图遍历 |
| **PostgreSQL（边表）** | 已有 PG 基础设施 | `req_dependencies` 表，SQL 查询 |

**推荐路径**：开发期用 networkx → 生产期迁移到 Neo4j（或继续用 PG + 递归 CTE）

### Neo4j 图谱 Schema（生产方案）

```cypher
// 节点：需求
CREATE (r:Requirement {
    id: "REQ-AUTH-001",
    title: "用户登录功能",
    module: "auth",
    version: 1,
    status: "archived",           // 已被新版本替代
    srr_json: {...}               // 完整 SRR JSON
})

// 边：版本替代（新版本 v2 替代旧版本 v1）
CREATE (r_v2:Requirement {id: "REQ-AUTH-001-v2", version: 2, status: "active"})
CREATE (r_v2)-[:REPLACES {change_type: "optimized", change_summary: "优化登录流程，新增验证码"}]->(r_v1)

// 边：功能依赖（支付依赖登录，即登录是支付的前置条件）
CREATE (r_pay:Requirement {id: "REQ-PAY-001", title: "支付功能"})
CREATE (r_pay)-[:DEPENDS_ON {strength: "strong"}]->(r_v1)

// 边：模块关联（登录和个人中心共用账号体系）
CREATE (r_profile:Requirement {id: "REQ-PROFILE-001", title: "个人中心"})
CREATE (r_v1)-[:RELATED_TO {strength: "weak"}]->(r_profile)
```

### 影响分析算法（图谱遍历）

```python
def analyze_change_impact(req_id: str, graph, max_depth: int = 3) -> dict:
    """
    基于依赖图谱分析需求变更的影响范围
    返回：直接影响 + 间接受影响的需求和测试用例
    """
    impact = {
        "direct": {"requirements": [], "test_cases": []},   # 直接影响（1跳）
        "indirect": {"requirements": [], "test_cases": []}, # 间接受影响（2-3跳）
        "regression_scope": []  # 需要回归测试的用例
    }
    
    # 1. 找到变更需求的所有下游节点（DEPENDS_ON 关系的反向）
    #     如果 A depends_on B，那么 B 变更会影响 A
    downstream_nodes = breadth_first_search(
        graph=graph,
        start=req_id,
        relation_types=["DEPENDS_ON"],  # 找依赖本需求的其他需求
        direction="incoming",            # 反向遍历
        max_depth=max_depth
    )
    
    # 2. 找到变更需求的所有上游节点（了解变更上下文需要的信息）
    upstream_nodes = breadth_first_search(
        graph=graph,
        start=req_id,
        relation_types=["DEPENDS_ON", "RELATED_TO"],
        direction="outgoing",  # 正向遍历
        max_depth=2
    )
    
    # 3. 收集所有相关测试用例
    for node in downstream_nodes:
        impact["direct"]["test_cases"].extend(get_test_cases_for_requirement(node))
    for node in upstream_nodes:
        impact["indirect"]["test_cases"].extend(get_test_cases_for_requirement(node))
    
    # 4. 去重，按优先级排序
    impact["regression_scope"] = deduplicate_and_sort(
        impact["direct"]["test_cases"] + impact["indirect"]["test_cases"]
    )
    
    return impact

# 使用 networkx 实现（开发期）
import networkx as nx

def build_dependency_graph_from_pg(pg_conn) -> nx.DiGraph:
    """从 PostgreSQL 加载依赖关系，构建 networkx 有向图"""
    G = nx.DiGraph()
    
    # 1. 添加所有需求节点
    cur = pg_conn.cursor()
    cur.execute("SELECT id, title, module, version, status FROM requirements")
    for row in cur.fetchall():
        G.add_node(row['id'], **dict(row))
    
    # 2. 添加依赖边
    cur.execute("SELECT source_req_id, target_req_id, relation_type, strength FROM req_dependencies")
    for row in cur.fetchall():
        G.add_edge(
            row['source_req_id'],
            row['target_req_id'],
            relation_type=row['relation_type'],
            strength=row['strength']
        )
    
    return G

def get_impact_scope(req_id: str, G: nx.DiGraph, max_depth: int = 2) -> dict:
    """
    给定需求ID，返回变更影响范围
    图约定：边 A → B 表示「A depends_on B」
      - G.successors(A) = A 依赖的节点（A 的上游）
      - G.predecessors(A) = 依赖 A 的节点（A 的下游）
    """
    # 依赖本需求的其他需求（下游 dependents）
    dependents = set()
    for depth in range(1, max_depth + 1):
        for node in list(dependents):
            if node in G:
                dependents.update(G.predecessors(node))  # predecessors = 依赖本节点的
        dependents.update(nx.single_source_shortest_path_length(G, req_id, cutoff=depth).keys())
    
    # 本需求直接依赖的（上游 dependencies）
    direct_deps = list(G.successors(req_id))
    # 直接依赖本需求的（下游 dependents）
    direct_dependents = list(G.predecessors(req_id))
    
    # 2跳影响（间接依赖本需求的）
    indirect = []
    for node in direct_dependents:
        indirect.extend(list(G.predecessors(node)))  # 依赖直接下游的
    
    return {
        "direct_deps": direct_deps,              # 本需求直接依赖的（上游）
        "direct_dependents": direct_dependents,  # 直接依赖本需求的（下游，最核心的影响范围）
        "indirect_dependents": list(set(indirect) - set(direct_dependents)),
        "all_dependents": list(nx.ancestors(G, req_id))  # 所有依赖本需求的（下游递归）
    }
```### 2.4.1 影响分析完整示例

以下展示一个完整的影响分析过程：**修改「用户登录」需求后，系统自动分析影响范围**。

#### 场景：优化登录流程（增加验证码）

```
原始需求: REQ-AUTH-001-v1「用户登录功能」
优化需求: REQ-AUTH-001-v2「用户登录功能（增加验证码）」
```

#### 步骤1: 构建依赖图谱（networkx）

```python
import networkx as nx

# 构建图谱（边 A → B 表示「A depends_on B」）
G = nx.DiGraph()

# 添加需求节点
requirements = [
    ("REQ-AUTH-001-v1", {"title": "用户登录", "module": "auth"}),
    ("REQ-AUTH-001-v2", {"title": "用户登录(v2)", "module": "auth"}),
    ("REQ-PAY-001", {"title": "支付功能", "module": "payment"}),
    ("REQ-PROFILE-001", {"title": "个人中心", "module": "user"}),
]

for req_id, attrs in requirements:
    G.add_node(req_id, **attrs)

# 添加依赖边（A → B 表示 A 依赖 B）
edges = [
    ("REQ-AUTH-001-v2", "REQ-AUTH-001-v1", {"relation": "replaces"}),
    ("REQ-PAY-001", "REQ-AUTH-001-v1", {"relation": "depends_on", "strength": "strong"}),
    ("REQ-PROFILE-001", "REQ-AUTH-001-v1", {"relation": "depends_on", "strength": "weak"}),
]
for source, target, attrs in edges:
    G.add_edge(source, target, **attrs)

print("图谱节点:", list(G.nodes()))
print("图谱边:", list(G.edges()))
# 输出:
# 图谱节点: ['REQ-AUTH-001-v1', 'REQ-AUTH-001-v2', 'REQ-PAY-001', 'REQ-PROFILE-001']
# 图谱边: [('REQ-AUTH-001-v2', 'REQ-AUTH-001-v1'), ('REQ-PAY-001', 'REQ-AUTH-001-v1'), ('REQ-PROFILE-001', 'REQ-AUTH-001-v1')]
```

**图谱含义**：
- `REQ-AUTH-001-v2 → REQ-AUTH-001-v1`：v2 替代 v1（版本链）
- `REQ-PAY-001 → REQ-AUTH-001-v1`：支付依赖登录（强依赖）
- `REQ-PROFILE-001 → REQ-AUTH-001-v1`：个人中心依赖登录（弱依赖）

#### 步骤2: 影响范围分析

```python
def get_impact_scope(req_id: str, G: nx.DiGraph, max_depth: int = 2) -> dict:
    """
    给定需求ID，返回变更影响范围
    图约定：边 A → B 表示「A depends_on B」
    """
    # 依赖本需求的其他需求（下游 dependents）
    # nx.predecessors(G, n) = 指向 n 的节点 = 依赖 n 的节点
    direct_dependents = list(G.predecessors(req_id))
    
    # 本需求依赖的节点（上游 dependencies）
    direct_dependencies = list(G.successors(req_id))
    
    # 2跳影响：依赖直接下游的节点
    indirect = []
    for node in direct_dependents:
        indirect.extend(list(G.predecessors(node)))
    
    # 所有受影响的节点（递归）
    all_affected = list(nx.ancestors(G, req_id))
    
    return {
        "direct_dependents": direct_dependents,   # 直接依赖本需求的（下游）
        "direct_dependencies": direct_dependencies, # 本需求直接依赖的（上游）
        "indirect_dependents": list(set(indirect) - set(direct_dependents)),
        "all_affected": all_affected,  # 所有受影响的（递归）
    }

# 分析 REQ-AUTH-001-v1 的影响范围
impact = get_impact_scope("REQ-AUTH-001-v1", G)
print("直接影响（下游）:", impact["direct_dependents"])
print("间接影响:", impact["indirect_dependents"])
print("所有受影响:", impact["all_affected"])

# 输出:
# 直接影响（下游）: ['REQ-PAY-001', 'REQ-PROFILE-001']
# 间接影响: []
# 所有受影响: ['REQ-PAY-001', 'REQ-PROFILE-001']
```

**分析结果**：
- 修改「用户登录」会**直接影响**「支付功能」和「个人中心」
- 需要**回归测试**这两个模块的用例
- 「用户登录 v2」替代了 v1，所以 v2 的用例需要**更新**

#### 步骤3: 生成回归测试推荐

```python
def recommend_regression_tests(req_id: str, impact: dict, pg_conn) -> dict:
    """
    根据影响范围，推荐需要回归测试的用例
    """
    recommendations = {
        "must_update": [],   # 必须更新的用例
        "must_regression": [],  # 必须回归测试的用例
        "optional_check": [],  # 建议检查的用例
    }
    
    cur = pg_conn.cursor()
    
    # 1. 必须更新：本需求的直接用例
    cur.execute(
        "SELECT id, title FROM test_cases WHERE req_id = %s AND status = 'approved'",
        (req_id,)
    )
    recommendations["must_update"] = cur.fetchall()
    
    # 2. 必须回归：直接影响的需求的用例
    for affected_req in impact["direct_dependents"]:
        cur.execute(
            "SELECT id, title FROM test_cases WHERE req_id = %s AND status = 'approved'",
            (affected_req,)
        )
        recommendations["must_regression"].extend(cur.fetchall())
    
    # 3. 建议检查：间接影响的需求的用例
    for affected_req in impact["indirect_dependents"]:
        cur.execute(
            "SELECT id, title FROM test_cases WHERE req_id = %s AND status = 'approved'",
            (affected_req,)
        )
        recommendations["optional_check"].extend(cur.fetchall())
    
    return recommendations

# 生成回归测试推荐
recommendations = recommend_regression_tests("REQ-AUTH-001-v1", impact, pg_conn)
print(f"必须更新: {len(recommendations['must_update'])} 个用例")
print(f"必须回归: {len(recommendations['must_regression'])} 个用例")
print(f"建议检查: {len(recommendations['optional_check'])} 个用例")
```

#### 步骤4: 输出影响分析报告

```json
{
  "change_summary": "优化登录流程，增加验证码校验",
  "impact_scope": {
    "direct_dependents": ["REQ-PAY-001", "REQ-PROFILE-001"],
    "direct_dependencies": ["REQ-SMS-001"],
    "indirect_dependents": []
  },
  "test_recommendations": {
    "must_update": [
      {"case_id": "TC-AUTH-001", "title": "正常登录流程", "action": "update"},
      {"case_id": "TC-AUTH-002", "title": "验证码错误处理", "action": "add"}
    ],
    "must_regression": [
      {"case_id": "TC-PAY-001", "title": "支付前登录检查", "action": "regression"},
      {"case_id": "TC-PROFILE-001", "title": "个人中心登录状态", "action": "regression"}
    ],
    "optional_check": []
  },
  "risk_assessment": {
    "high_risk": ["验证码发送失败", "验证码超时处理"],
    "medium_risk": ["UI 布局变化", "错误提示文案"],
    "low_risk": ["性能影响（增加一次短信调用）"]
  }
}
```


### 依赖关系自动发现（LLM 辅助）

新需求入库时，LLM 自动分析其与其他需求的依赖关系：

```python
def auto_discover_dependencies(new_req_srr: dict, pg_conn, embedding_model) -> list:
    """
    自动发现新需求与其他需求的依赖关系
    返回：[(target_req_id, relation_type, confidence), ...]
    """
    # 1. 语义相似度检索候选（可能相关的需求）
    candidates = vector_search(
        query=new_req_srr['summary'],
        collection='requirements',
        top_k=20,
        pg_conn=pg_conn,
        embedding_model=embedding_model
    )
    
    # 2. LLM 分析依赖关系
    prompt = f"""
    分析新需求与以下历史需求的依赖关系。
    
    新需求：{json.dumps(new_req_srr, ensure_ascii=False)}
    
    候选历史需求：
    {json.dumps([{"id": c["req_id"], "title": c["title"], "summary": c["summary"]} for c in candidates], ensure_ascii=False)}
    
    请判断每个候选需求与新需求的关系（选择其一或"无关系"）：
    - replaces: 新需求是候选需求的优化/升级版本
    - depends_on: 新需求的功能依赖候选需求（候选是先置条件）
    - related_to: 新需求与候选需求属于同一模块/功能域
    - conflicts: 新需求与候选需求存在功能冲突
    - none: 无关系
    
    输出 JSON 数组：
    [{{"target_id": "REQ-XXX", "relation": "depends_on", "confidence": 0.95, "reason": "..."}}, ...]
    """
    llm_response = call_llm(prompt, temperature=0.0, response_format="json")
    return json.loads(llm_response)
```

---

## 3. 知识检索策略 (MatchAgent)

### 3.1 混合检索架构

```
当前需求 SRR
     │
     ├──────────────────┬──────────────────┐
     ▼                  ▼                  ▼
┌─────────┐     ┌─────────────┐    ┌──────────────┐
│ 语义检索 │     │ 关键词匹配   │    │ 图谱扩展检索  │
│ (Vector)│     │ (BM25/Full) │    │ (Graph RAG)  │
└────┬────┘     └──────┬──────┘    └──────┬───────┘
     │                  │                  │
     │     ┌────────────┴──────────┐       │
     └─────┤   Reciprocal Rank    ├───────┘
           │   Fusion (RRF) 融合   │
           └────────────┬──────────┘
                        ▼
                 ┌─────────────┐
                 │ Top-K 结果   │
                 │ (K=10~20)   │
                 └──────┬──────┘
                        ▼
                 ┌─────────────┐
                 │ Re-rank     │
                 │ (Cross-     │
                 │  Encoder)   │
                 └──────┬──────┘
                        ▼
                 ┌─────────────┐
                 │ 最终 Top-N   │
                 │ (N=3~5)    │
                 │ + 关联用例   │
                 └─────────────┘
```

**实际实现**（`backend/app/db/hybrid_search.py`）：

1. **向量检索**：Milvus HNSW（M=16, efConstruction=200, ef=64），返回 Top-K×2
2. **BM25 检索**：PostgreSQL tsvector + zhparser 中文分词 + GIN 索引
   - SQL: `WHERE search_vec @@ plainto_tsquery('chinese', :q)`
   - 排序: `ts_rank(search_vec, query) DESC`
3. **RRF 融合**：k=60，`score(d) = Σ 1/(k + rank_i(d))`
4. **Cross-Encoder 重排**：bge-reranker-base（FlagEmbedding），对 RRF Top-K×2 重排为 Top-N
5. **多级缓存**：L1 Redis（TTL 24h）+ L2 LRU（进程内 1000 条），key = MD5(query|product_id|coll_type)

**内部降级**（检索质量优化，非系统降级）：
- 向量检索失败 → 仅用 BM25
- BM25 检索失败 → 仅用向量
- 两者都失败 → 返回空（上层静默跳过，见 §2.6 降级策略）

### 3.2 检索维度

| 维度 | 检索目标 | 实现方式 |
|------|---------|---------|
| **语义相似** | 功能相似的历史需求 | Vector DB cosine similarity |
| **模块匹配** | 同模块的历史用例 | PG: `WHERE module = ?` |
| **模式匹配** | 同类测试模式 | PG + Vector 联合查询 |
| **依赖链路** | 关联需求链上的用例 | 图遍历 (Neo4j/pgRouting) |
| **时间衰减** | 优先近期/活跃用例 | 加权评分 `score * decay_factor` |

### 3.3 检索增强的 Few-shot 构建

检索到的 Top-N 历史用例通过以下模板注入 GenAgent 的 Prompt：

```
以下是与当前需求相似的历史测试用例，请参考其结构和覆盖策略：

=== 历史用例 1 (相似度: 0.92) ===
需求: {similar_req_title}
用例类型: {case_type}
用例内容:
{similar_case_json}

=== 历史用例 2 (相似度: 0.87) ===
...

请参考以上历史用例的格式和测试覆盖思路，为当前需求生成测试用例。
注意：不要直接复制历史用例的具体数据，而是借鉴其测试策略和用例结构。
```### 2.6 错误处理

知识库操作可能因多种原因失败，需要完善的错误处理策略。

#### 错误分类与处理

| 错误类型 | 来源 | 处理策略 | 用户感知 |
|---------|------|---------|---------|
| **Vector DB 连接失败** | 写入/检索时 | 重试 3 次 → 静默跳过 KB（生成流水线正常生成，人工澄清兜底） | 检索质量略降，功能正常 |
| **PG 连接失败** | 写入/查询时 | 连接池重试 → 报错（PG 是主库，无降级） | 操作失败，提示重试 |
| **Embedding 模型失败** | 生成嵌入时 | Celery 任务重试 3 次 → 记录日志（不阻断归档） | 归档成功，KB 写入延迟 |
| **磁盘空间不足** | 写入 Vector DB 时 | 立即告警 → 拒绝新写入 | "知识库已满，请联系管理员" |
| **数据不一致** | Vector DB 与 PG 数据不匹配 | 记录不一致 → 后台异步修复 | 无感知（后台修复中） |

#### 错误处理代码示例

```python
import logging
from typing import Optional, Callable, Any
from functools import wraps

logger = logging.getLogger(__name__)

class KnowledgeBaseError(Exception):
    """知识库操作基础异常"""
    def __init__(self, message: str, code: str, retryable: bool = True):
        self.message = message
        self.code = code
        self.retryable = retryable
        super().__init__(message)

class VectorDBError(KnowledgeBaseError):
    """Vector DB 操作异常"""
    pass

class RelationDBError(KnowledgeBaseError):
    """关系数据库操作异常"""
    pass

def with_retry(max_attempts: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """
    重试装饰器
    :param max_attempts: 最大重试次数
    :param delay: 初始延迟（秒）
    :param backoff: 退避倍数
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_error = None
            current_delay = delay
            
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except (VectorDBError, RelationDBError) as e:
                    last_error = e
                    if not e.retryable:
                        logger.error(f"不可重试的错误: {e.message} (code={e.code})")
                        raise
                    
                    if attempt < max_attempts:
                        logger.warning(
                            f"操作失败（尝试 {attempt}/{max_attempts}): {e.message}。"
                            f"{current_delay:.1f}s 后重试..."
                        )
                        time.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        logger.error(f"操作失败（已重试 {max_attempts} 次）: {e.message}")
            
            raise last_error
        return wrapper
    return decorator

# 使用示例
class KnowledgeBase:
    @with_retry(max_attempts=3, delay=1.0, backoff=2.0)
    def store_requirement(self, req_id: str, srr: dict) -> bool:
        """存储需求到知识库（双写）"""
        try:
            # 1. 写入 Vector DB
            self._store_to_vector_db(req_id, srr)
        except Exception as e:
            raise VectorDBError(
                message=f"Vector DB 写入失败: {e}",
                code="VECTOR_DB_WRITE_ERROR",
                retryable=True
            )
        
        try:
            # 2. 写入 PostgreSQL
            self._store_to_pg(req_id, srr)
        except Exception as e:
            # 回滚 Vector DB
            try:
                self._delete_from_vector_db(req_id)
            except Exception as rollback_error:
                logger.error(f"回滚失败: {rollback_error}")
            
            raise RelationDBError(
                message=f"PostgreSQL 写入失败: {e}",
                code="PG_WRITE_ERROR",
                retryable=True
            )
        
        return True

    def _store_to_vector_db(self, req_id: str, srr: dict):
        """写入 Vector DB（内部方法）"""
        try:
            embedding = self.embedding_model.encode(self._build_text(srr))
            self.collection.add(
                ids=[req_id],
                embeddings=[embedding.tolist()],
                metadatas=[{"req_id": req_id, **srr.get("requirement", {})}]
            )
        except Exception as e:
            raise VectorDBError(
                message=f"Vector DB 操作失败: {e}",
                code="VECTOR_DB_ERROR",
                retryable=True
            )

    def _store_to_pg(self, req_id: str, srr: dict):
        """写入 PostgreSQL（内部方法）"""
        try:
            with self.pg_conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO requirements (id, product_id, title, module, modules, srr_json, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (id) DO UPDATE SET
                        product_id = EXCLUDED.product_id,
                        module = EXCLUDED.module,
                        modules = EXCLUDED.modules,
                        srr_json = EXCLUDED.srr_json,
                        updated_at = NOW()
                """, (
                    req_id,
                    srr.get("requirement", {}).get("product_id", ""),
                    srr.get("requirement", {}).get("title", ""),
                    (srr.get("requirement", {}).get("modules", [""])[0]
                     if srr.get("requirement", {}).get("modules") else ""),
                    srr.get("requirement", {}).get("modules", []),
                    json.dumps(srr, ensure_ascii=False)
                ))
                self.pg_conn.commit()
        except Exception as e:
            raise RelationDBError(
                message=f"PostgreSQL 操作失败: {e}",
                code="PG_ERROR",
                retryable=True
            )
```

#### 降级策略（简化版）

**设计原则**：KB 是增强项，不是阻断项。KB 不可用时静默跳过，正常生成，人工澄清兜底。绝不因 KB 降级逻辑本身污染生成结果（如向 Prompt 注入诊断提示）。

```
KB 查询（向量 + BM25 混合检索）
    │
    ├─ 成功 → 返回检索结果 → 增强 Prompt → 生成
    │
    └─ 失败（向量+BM25 均失败）
         │
         └─ 静默跳过 → 返回空列表 → 生成无 KB 增强
                                        │
                                        └─ 人工澄清兜底（QualityGate + ClarifyLoopGuard 3 轮）
```

**各模块降级行为**：

| 模块 | KB 失败时行为 | 兜底机制 |
|------|-------------|---------|
| ClarifyAgent | `matched=[]` → 不补全答案 → 生成澄清问题 | 人工澄清 3 轮 |
| retrieve_node | `few_shot_context` 为空 → GenAgent 无 few-shot 生成 | ReviewAgent + 人工审核 |
| GenAgent | 空 few-shot → 正常生成 | ReviewAgent + 人工审核 |
| knowledge_qa | KB 完全不可用 → 不调用 LLM → 返回错误响应 | 用户稍后重试 |

**内部降级**（`hybrid_searcher` 内部，检索质量优化，非系统降级）：
- 向量检索失败 → 仅用 BM25
- BM25 检索失败 → 仅用向量
- 两者都失败 → 返回空（上层静默跳过）

**已废弃的多级降级**（旧版 L0-L4 方案已删除）：
- ❌ L1 PG 备用降级 — PG BM25 已在 `hybrid_searcher` 内部作为并列检索源，不是降级路径
- ❌ L2 Redis 只读缓存降级 — Redis 是性能优化，非降级路径
- ❌ L3 静态规则降级 — 不存在规则引擎，删除虚构设计
- ❌ L4 拒绝服务 503 — KB 不可用永不阻断生成流水线

#### 错误日志格式

```python
# 结构化错误日志（便于后续分析和告警）
{
    "timestamp": "2024-06-26T10:30:00Z",
    "level": "ERROR",
    "module": "knowledge_base",
    "operation": "store_requirement",
    "error_code": "VECTOR_DB_WRITE_ERROR",
    "error_message": "Vector DB 写入失败: Connection timeout",
    "retryable": true,
    "attempt": 3,
    "max_attempts": 3,
    "duration_ms": 5000,
    "req_id": "REQ-2024-001",
    "trace_id": "trace-xxx"
}
```

---

## 4. 知识库维护策略

### 4.1 闭环回流流程

```
GenAgent 生成用例
     │
     ▼
ReviewAgent 审核
     │
     ├── 高置信度 (≥0.85) ──► 自动入库 (标记 reviewed)
     │
     ├── 中置信度 (0.6~0.85) ──► 人工审核 → 修正后入库
     │
     └── 低置信度 (<0.6) ──► 人工审核 → 入库或丢弃
                                   │
                                   ▼
                            记录到 error_cases 表
                            (如被驳回，记录错误类型+修正)
```

### 4.2 知识衰减与刷新

```python
# 用例权重计算公式
weight = base_weight
       * (1 - decay_rate) ** age_days    # 时间衰减
       * acceptance_rate                  # 接受率加权
       * (1 + boost_for_reuse)            # 复用加分
       * module_relevance_factor           # 模块相关性

# 示例: 基础权重1.0，半衰期180天，接受率0.95，被复用5次的用例
# weight = 1.0 * 0.5 * 0.95 * 1.5 = 0.7125
```

### 4.3 知识库迁移

| 场景 | 迁移策略 |
|------|---------|
| **新项目冷启动** | 从通用测试模式库注入种子数据 + 允许人工导入历史用例 |
| **项目间迁移** | 按模块/Tag 过滤，只迁移通用模式（去除项目特定数据） |
| **知识库升级** | 版本化 Schema，支持向前兼容读取，必要时全量 re-embedding |

---

## 5. 测试模式库 (Pattern Library)

预定义高频测试模式，作为 Few-shot 示例的"种子数据"，解决冷启动问题：

| 模式 | 适用场景 | 模板特征 |
|------|---------|---------|
| **CRUD 模式** | 资源增删改查 | 创建/读取/更新/删除 + 幂等性 + 并发 |
| **状态机模式** | 订单、审批流 | 合法状态转移 × 非法转移拒绝 |
| **认证授权模式** | 登录、权限 | 正常登录 + 异常 + 过期 + 越权 |
| **输入校验模式** | 表单、API | 正常值 + 边界值 + 非法值 + 特殊字符 |
| **分页排序模式** | 列表接口 | 首页/末页/越界/排序/空列表 |
| **并发模式** | 库存、秒杀 | 乐观锁/悲观锁/幂等/最终一致性 |
| **异步回调模式** | 支付、消息 | 超时/重试/幂等/回调顺序 |
| **数据一致性模式** | 分布式事务 | 最终一致性/补偿/回滚 |

每个模式存储为结构化 JSON + 向量嵌入，在检索时与需求一起参与相似度计算。

---

## 6. 需求模式库 (Requirement Pattern Library)

需求模式库存储**同类需求的抽象维度集合**，是 ClarifyAgent 遗漏检测的数据源。

### 6.1 与测试模式库的区别

| 维度 | 测试模式库 | 需求模式库 |
|------|-----------|-----------|
| **存储内容** | 测试用例的结构模板 | 需求应包含的维度清单 |
| **消费方** | GenAgent (Few-shot) | ClarifyAgent (遗漏检测) |
| **示例** | "CRUD模式→创建/读取/更新/删除用例" | "认证类需求→应有密码重试锁定机制" |

### 6.2 预定义模式

```python
REQUIREMENT_PATTERNS = {
    "auth": {
        "name": "认证授权类",
        "modules": ["auth", "login", "register", "sso"],
        "required_dimensions": [
            "正常登录/注册流程", "异常处理（错误密码、账号不存在）",
            "密码重试锁定机制", "会话管理（Token/Session）", "密码加密存储"
        ],
        "common_boundary": ["用户名长度边界", "密码长度边界", "SQL注入防护"],
        "risk_points": ["暴力破解", "会话固定攻击", "密码泄露"]
    },
    "crud": {
        "name": "CRUD 操作类",
        "modules": ["user", "order", "product", "article"],
        "required_dimensions": [
            "创建/查询/更新/删除流程", "数据校验规则", "权限控制", "分页/排序"
        ],
        "risk_points": ["越权操作", "数据丢失", "并发冲突"]
    },
    "payment": {
        "name": "支付交易类",
        "modules": ["payment", "transaction", "refund"],
        "required_dimensions": [
            "支付/退款流程", "支付状态机", "金额计算规则",
            "交易安全（签名/加密）", "幂等性保证", "对账机制"
        ],
        "risk_points": ["重复支付", "金额篡改", "退款超时", "对账不一致"]
    }
}
```

### 6.3 持续积累

- 每次需求入库时，自动提取维度并更新模式统计
- 模式使用频率和成功率作为质量反馈
- 低频/低成功率模式自动降权

---

## 7. KB 辅助需求补全

知识库在 ClarifyAgent 中承担**自动补全**角色，减少人工负担：

### 7.1 补全流程

```
ClarifyAgent 检测到缺口
    │
    ▼
检索 KB 中同类需求的对应字段/维度
    │
    ├─ 相似度 > 0.85 → 自动补全，标记 [KB_INFERRED]
    │                  来源需求ID + 相似度记录
    │
    └─ 相似度 ≤ 0.85 → 不补全，生成问题交人工
```

### 7.2 补全策略

| 缺口类型 | 补全方式 | 是否自动填充 |
|----------|---------|-------------|
| 缺失字段 (preconditions 等) | 从相似需求的对应字段复制 | ✅ 自动 |
| 缺失维度 (密码锁定机制等) | 从相似需求提取参考内容 | ❌ 需确认 |
| 模糊描述 | 无法补全 | ❌ 需人工 |

---

## 8. 业界借鉴

| 实践来源 | 借鉴点 |
|----------|--------|
| **RAG 架构 (Meta/OpenAI)** | 混合检索 + Re-rank + 上下文窗口管理 |
| **LangChain/LlamaIndex** | Document Loader → Splitter → Embedding → VectorStore 标准链路 |
| **Notion/Linear 知识库** | 层级式文档组织 + 双向链接 |
| **TestGen-LLM (Meta)** | 从代码 diff 反推测试关注点，类比从需求 diff 反推 |
| **Microsoft IntelliTest/Pex** | 符号执行自动探索边界条件，可结合 LLM 推理 |

---

## 9. 大规模检索优化（>10 万条）

> 当知识库向量数超过 10 万条后，需对 Milvus 实施分区/分片与分层优化策略（Milvus 为当前唯一向量库后端，已统一替代 ChromaDB）。

### 9.1 迁移路径与触发条件

| 阶段 | 向量规模 | 推荐引擎 | 触发条件 |
|------|---------|---------|---------|
| MVP | < 1 万 | Milvus（单机/嵌入式） | 零配置启动 |
| 成长期 | 1 万 ~ 10 万 | Milvus（单机优化） | 检索 P95 > 200ms 时启用分区 |
| **成熟期** | **> 10 万** | **Milvus（分布式）** | **pgvector 检索 P95 > 500ms 或向量数 > 10 万** |
| 超大规模 | > 1000 万 | Milvus 集群 + 分片 | 单节点内存不足 |

### 9.2 分区与分片策略

```python
# Milvus 分区策略：按 module 分区，检索时只扫描目标分区
"""
Collection: requirements
  ├── Partition: auth       (认证模块的需求向量)
  ├── Partition: payment    (支付模块的需求向量)
  ├── Partition: user       (用户模块的需求向量)
  ├── Partition: order      (订单模块的需求向量)
  └── Partition: _default   (未分类的需求向量)

检索时指定分区:
  collection.search(
      data=[query_embedding],
      partition_names=["auth"],  # 只在 auth 分区检索
      n_results=20
  )

优势: 检索范围从全量 N 缩小到 N/module_count，延迟降低 5-10 倍
"""
```

**分片策略**（>1000 万向量时）：
- 按 `created_at` 月份分片：历史数据冷热分离
- 热数据（最近 6 个月）：保持在内存索引中
- 冷数据（6 个月以上）：降级到磁盘索引，按需加载

### 9.3 HNSW 索引参数调优

```python
# Milvus HNSW 索引参数
index_params = {
    "index_type": "HNSW",
    "metric_type": "COSINE",
    "params": {
        "M": 16,              # 每层最大连接数，越大召回率越高但内存越大
        "efConstruction": 200, # 构建时搜索宽度，越大索引质量越好但构建越慢
    }
}

# 检索参数
search_params = {
    "params": {
        "ef": 64   # 检索时搜索宽度，越大召回率越高但延迟越大
    }
}

# 参数选择建议:
# | 数据规模    | M  | efConstruction | ef  | 预期 P95 延迟 |
# |------------|-----|----------------|-----|--------------|
# | 10-50 万   | 16  | 200            | 64  | ~50ms        |
# | 50-100 万  | 32  | 256            | 128 | ~80ms        |
# | > 100 万   | 48  | 512            | 256 | ~120ms       |
```

### 9.4 多级缓存

**实际实现**：`backend/app/db/kb_cache.py`（`KbCache` 类）

```
请求 → L2 本地 LRU 缓存 (OrderedDict, 最近 1000 次)
         ├── 命中 → move_to_end → 返回 (延迟 <0.1ms)
         └── 未命中 → L1 Redis 缓存 (精确 key 匹配, TTL 24h)
                      ├── 命中 → 回填 L2 → 返回 (延迟 <1ms)
                      └── 未命中 → hybrid_searcher 检索 (向量 + BM25 + RRF + 重排)
                                   ├── 返回结果
                                   └── 写入 L2 + L1 缓存
```

| 缓存层 | 实现 | 容量 | TTL | 命中率预期 |
|--------|------|------|-----|-----------|
| L2 本地缓存 | Python OrderedDict + threading.Lock | 最近 1000 次查询 | 进程生命周期 | 10-15% |
| L1 Redis 缓存 | Redis GET/SETEX（`decode_responses=True`） | 最近 5000 次查询 | 24h（`SETEX key 86400 value`） | 15-25% |
| 检索层 | hybrid_searcher（Milvus + PG BM25） | 全量 | — | 60-75% |

**缓存 key 设计**：`kb_cache:{MD5(query|product_id|coll_type)}`，MD5 避免长查询字符串和特殊字符问题。

**降级**：Redis 不可用时自动降级为 L2 LRU-only（`_get_redis()` 返回 None，`get/set` 仅操作 L2）。

### 9.5 Re-rank 优化

当召回量大（Top-K=50~100）时，Cross-Encoder Re-rank 成为瓶颈：

| 策略 | 适用场景 | 延迟 | 效果 |
|------|---------|------|------|
| **不 Re-rank** | MVP / < 10 万条 | 0ms | 召回率依赖向量质量 |
| **轻量 Re-rank**（bge-reranker-base） | 10-50 万条 | ~20ms | 召回率 +5-10% |
| **重量 Re-rank**（bge-reranker-large） | > 50 万条 / 高精度场景 | ~50ms | 召回率 +10-15% |
| **LLM Re-rank**（DeepSeek 打分） | 极高精度场景 | ~3s | 召回率 +15-20% |

**推荐**：MVP 不 Re-rank，成长期用 bge-reranker-base，成熟期用 bge-reranker-large。

### 9.6 检索质量监控

```python
# 定期评估检索质量（每周自动执行）
METRICS = {
    "Recall@K": "前 K 个结果中包含正确答案的比例（需人工标注 Ground Truth）",
    "MRR": "Mean Reciprocal Rank，正确答案的平均排名倒数",
    "NDCG@K": "考虑排序位置的相关性得分",
    "P95_latency": "95 分位检索延迟",
    "cache_hit_rate": "缓存命中率"
}

# 告警阈值
ALERT_THRESHOLDS = {
    "Recall@10 < 0.80": "检索召回率下降，可能需要更新 Embedding 模型",
    "P95_latency > 500ms": "检索延迟过高，可能需要优化索引或扩容",
    "cache_hit_rate < 10%": "缓存命中率过低，可能查询模式变化"
}
```

### 9.7 异步双写一致性方案

> **问题**：Vector DB 和 PG 不支持分布式事务，可能出现数据不一致。

**实际实现**：
- `backend/app/tasks/celery_app.py` — Celery 应用，broker/backend 用 Redis，JSON 序列化，`task_acks_late=True`，queue routing
- `backend/app/tasks/embedding_tasks.py` — 两个异步任务
- 归档端点（`requirements.py` + `knowledge_qa.py`）：PG commit → `celery_app.send_task` 派发 → Celery Worker 异步 Embedding + Milvus 写入

```python
# 归档端点（backend/app/api/requirements.py）
# Step 1: PG 同步写入（FastAPI 路由内，事务保证）
record.status = "archived"
record.go_live_date = parsed_go_live
await session.commit()  # PG 已落库

# Step 2: 异步写入 Milvus（Celery 任务派发）
from app.tasks.celery_app import celery_app
if celery_app is not None:
    celery_app.send_task(
        "app.tasks.embedding_tasks.embed_and_store_requirement",
        args=[record.id, record.srr_json or {}, milvus_meta],
        queue="embedding",
    )
else:
    # Celery 不可用 → 降级为同步写入
    await asyncio.to_thread(vector_store.add_requirement, ...)


# Celery 任务（backend/app/tasks/embedding_tasks.py）
@celery_app.task(name="app.tasks.embedding_tasks.embed_and_store_requirement",
                 bind=True, max_retries=3, default_retry_delay=5)
def embed_and_store_requirement(self, req_id: str, srr_json: dict, metadata: dict):
    try:
        vector_store.add_requirement(req_id, srr_json, metadata=metadata)
    except Exception as exc:
        raise self.retry(exc=exc)  # 失败重试 3 次，间隔 5s
```

**注意**：刚写入的需求不能被立即检索（需等异步 Embedding 完成，约 1-5s）。

| 方案 | MVP | 生产环境 | 一致性保证 |
|------|-----|---------|-----------|
| **同步双写** | ✅ 推荐 | ❌ 延迟高 | PG 失败回滚 Vector DB |
| **异步双写**（已实现） | ❌ 复杂 | ✅ 推荐 | PG 先成功 → Celery 异步 Embedding → 失败重试 3 次 → Celery 不可用时降级同步 |

---

## 10. 已有项目知识库入库方案

> 新项目冷启动时，需要将已有的需求文档、测试用例、缺陷报告等批量导入知识库。

### 10.1 多源 ETL Pipeline

```
数据源               ETL 阶段                        存储
┌─────────────┐    ┌──────────────────┐    ┌──────────────┐
│ JIRA Tickets │───►│                  │    │              │
│ PRD 文档     │───►│  ① Extract       │───►│              │
│ 飞书文档     │───►│  (多格式解析)     │    │              │
│ Excel 用例   │───►│                  │    │              │
│ Gherkin 文件 │───►│                  │    │              │
└─────────────┘    └────────┬─────────┘    │              │
                            │              │              │
                   ┌────────▼─────────┐    │              │
                   │  ② Transform     │    │  PostgreSQL  │
                   │  (清洗 + 标准化   │───►│  + Vector DB │
                   │   + SRR 转换)     │    │              │
                   └────────┬─────────┘    │              │
                            │              │              │
                   ┌────────▼─────────┐    │              │
                   │  ③ Load          │───►│              │
                   │  (批量写入 +     │    │              │
                   │   Embedding 计算) │    │              │
                   └──────────────────┘    └──────────────┘
```

### 10.2 数据源适配器

| 数据源 | 格式 | 适配策略 | 输出 |
|--------|------|---------|------|
| **JIRA** | REST API / CSV | 字段映射 → SRR JSON | requirements 表 |
| **PRD 文档** | Markdown / Word | ReqAgent 解析 → SRR JSON | requirements 表 |
| **飞书文档** | API / 导出 | 文本提取 → ReqAgent 解析 | requirements 表 |
| **Excel 用例** | .xlsx | 行映射 → UTCF JSON | test_cases 表 |
| **Gherkin** | .feature 文件 | gherkin-parser → UTCF JSON | test_cases 表 |
| **缺陷报告** | JIRA / CSV | 结构化提取 → error_cases 表 | error_cases 表 |

### 10.3 批量入库流程

```python
class BatchIngestionPipeline:
    """批量入库 Pipeline，支持容错和断点续传"""

    def __init__(self, pg_conn, vector_client, embedding_model, celery_app):
        self.pg = pg_conn
        self.vector = vector_client
        self.embedder = embedding_model
        self.celery = celery_app

    async def ingest(self, source_type: str, source_path: str, batch_size: int = 50):
        """
        批量入库主流程
        :param source_type: jira / markdown / excel / gherkin
        :param source_path: 数据源路径或 URL
        :param batch_size: 每批处理数量
        """
        # 1. Extract: 提取原始数据
        raw_items = self._extract(source_type, source_path)
        total = len(raw_items)

        # 2. 分批处理
        results = {"success": 0, "failed": 0, "skipped": 0, "errors": []}
        for i in range(0, total, batch_size):
            batch = raw_items[i:i + batch_size]
            batch_result = await self._process_batch(batch, source_type)
            self._merge_results(results, batch_result)

            # 记录进度（支持断点续传）
            self._save_checkpoint(source_path, i + len(batch), total)

        # 3. 输出入库报告
        return {
            "total": total,
            "success": results["success"],
            "failed": results["failed"],
            "skipped": results["skipped"],
            "error_details": results["errors"][:20],  # 前 20 个错误
            "duration_seconds": elapsed
        }

    async def _process_batch(self, batch: list, source_type: str) -> dict:
        """处理单批数据"""
        result = {"success": 0, "failed": 0, "skipped": 0, "errors": []}

        for item in batch:
            try:
                # 2a. Transform: 转换为 SRR JSON 或 UTCF JSON
                if source_type in ("jira", "markdown", "feishu"):
                    srr = await self._transform_to_srr(item)
                    # 2b. Load: 写入 PG + Vector DB（异步双写）
                    await self._store_requirement(srr)
                elif source_type in ("excel", "gherkin"):
                    utcf = self._transform_to_utcf(item)
                    await self._store_test_case(utcf)
                result["success"] += 1
            except ValidationError as e:
                result["skipped"] += 1
                result["errors"].append({"item": item.get("id"), "error": str(e)})
            except Exception as e:
                result["failed"] += 1
                result["errors"].append({"item": item.get("id"), "error": str(e)})

        return result
```

### 10.4 数据清洗规则

| 规则 | 说明 | 处理方式 |
|------|------|---------|
| **去重** | 相同标题 + 相同内容的需求/用例 | 保留最新版本，旧版本标记 `archived` |
| **字段补全** | 缺失 `module` / `priority` 等字段 | 从内容推断或标记 `unknown` |
| **格式标准化** | 日期格式、编码、空白字符 | 统一为 ISO 8601 / UTF-8 |
| **无效数据过滤** | 空需求、测试步骤为空的用例 | 跳过并记录到错误日志 |
| **编码修复** | 乱码、HTML 实体 | 自动修复或标记需人工处理 |

### 10.5 增量同步策略

```python
# 定时增量同步（每日凌晨执行）
SYNC_CONFIG = {
    "schedule": "0 2 * * *",  # 每天凌晨 2 点
    "strategy": "incremental",  # 只同步上次同步后有变更的数据
    "jira_jql": "updated >= '{last_sync}' AND project = '{project}'",
    "dedup_key": "source_id",  # 按源系统 ID 去重
}

# 同步状态记录
# CREATE TABLE kb_sync_log (
#     id SERIAL PRIMARY KEY,
#     source_type VARCHAR(32),
#     source_path TEXT,
#     last_sync_at TIMESTAMPTZ,
#     total_count INTEGER,
#     success_count INTEGER,
#     failed_count INTEGER,
#     status VARCHAR(16),  -- running / completed / failed
#     error_details JSONB
# );
```

### 10.6 冷启动策略

知识库支持**从零开始**，不强制要求初始化种子数据。

#### 模式 A：从零开始（推荐，新产品默认）

| 阶段 | 行为 | 效果 |
|------|------|------|
| **创建产品** | 在系统中创建产品（Product），获得 product_id | 后续所有需求/用例按 product_id 隔离 |
| **第 1 个需求** | 知识库为空，ClarifyAgent 自动切换「冷启动模式」：生成影响范围/边界条件/依赖确认类问题 | 通过深度人工澄清，保证需求精确性 |
| **指定模块** | 冷启动时会问"涉及哪些模块？"，自动创建模块（Module） | 模块按产品隔离 |
| **第 1 个需求完成后** | 自动存入知识库（SRR + 生成的测试用例） | 成为知识库的第一个种子 |
| **第 2~N 个需求** | 知识库已有数据，正常走匹配 + 遗漏检测 | 逐步积累，越用越准 |

**冷启动模式**（ClarifyAgent 自动触发，当 `kb_empty=True`）：
- 影响范围确认："这个功能的上线，会影响哪些已有功能？"
- 边界条件确认："需求的边界条件是什么？（最大最小值、空值处理、并发上限等）"
- 依赖系统确认："这个功能依赖哪些外部系统或服务？"
- 用户角色确认："哪些用户角色会使用这个功能？不同角色的权限差异？"
- 异常场景确认："这个功能的异常场景有哪些？"

#### 模式 B：加速模式（可选，有历史项目时）

如果你有历史项目的需求文档 / 测试用例，可以批量导入，跳过冷启动阶段：

| 资产类型 | 推荐数量 | 来源 | 作用 |
|----------|---------|------|------|
| 需求 SRR | 50-100 个 | 历史项目导入 | MatchAgent 语义检索基线 |
| 测试用例 UTCF | 100-200 个 | 历史项目导入 | GenAgent Few-shot 参考 |
| 测试模式 | 5-10 个 | 预定义（CRUD/auth/payment 等） | 模式匹配基线 |
| 需求模式 | 3-5 个 | 预定义（auth/crud/payment） | ClarifyAgent 遗漏检测 |
| 错误案例 | 10-20 个 | 人工标注 | ReviewAgent 避免重复错误 |

> **注意**：导入历史数据**不是必须的**。系统设计为"从零开始也能正常工作"，导入只是加速知识积累。

#### 配置示例

```python
# config/knowledge_base.py
COLD_START_MODE = True   # 允许从零开始（默认 True）
AUTO_ADD_FIRST_REQ = True  # 第一个需求完成后自动入库（默认 True）
```

---

## 11. 实施建议

1. **统一使用 Milvus 跑通 MVP**（当前唯一向量库后端，ChromaDB 已移除），再按需扩展至分布式
2. **Embedding 模型要支持中文**：推荐 `bge-large-zh-v1.5` 或 `m3e-large`
3. **定期评估检索质量**：用 Recall@K、MRR 指标监控，人工标注 Ground Truth
4. **冷启动支持从零开始**：知识库为空时，ClarifyAgent 自动切换深度澄清模式（详见 §10.6）
5. **知识库版本化**：每次 Schema 变更记录迁移脚本，保留回滚能力
6. **大规模检索优化分阶段实施**：MVP 即用 Milvus 单机 → 10 万条启用分区/分片 → 1000 万条启用 Milvus 集群（详见 §9）
7. **入库 Pipeline 支持断点续传**：大批量入库时可能中断，需支持从断点恢复（详见 §10.3）
8. **异步双写 + 定期对账**：生产环境用异步双写降低延迟，每小时对账修复不一致（详见 §9.7）
