# 知识库架构重构计划 — 双引擎增强 + 降级简化 + SQLite 清理

## 摘要

本次重构覆盖三个核心目标：
1. **SQLite 彻底清除** — 完成已开始的 Phase 1（config/prompt_store/database/crypto/main 已改），清理剩余的 .env.example、requirements.txt 及全项目注释中的 SQLite 引用
2. **Milvus 大规模优化（>10w 条策略）** — HNSW 索引 + 模块分区 + req_pats Collection + BM25/RRF 混合检索 + bge-reranker 重排 + Redis 多级缓存 + Celery 异步双写
3. **降级策略简化** — KB 不可用时跳过 KB 增强 → 直接生成 → 人工澄清兜底（不再做 PG fallback 等多级降级，避免污染生成质量）

---

## 当前状态分析

### Phase 1 已完成（6 个文件）
| 文件 | 状态 | 关键变更 |
|------|------|---------|
| `backend/app/config.py` | ✅ | 移除 SQLITE_PATH/is_pg，新增 DATA_DIR + pg_sync_url |
| `backend/app/db/models.py` | ✅ | 新增 PromptOverride + ReqPattern ORM |
| `backend/app/db/prompt_store.py` | ✅ | 全量重写为 SQLAlchemy 同步引擎 + PromptOverride |
| `backend/app/utils/crypto.py` | ✅ | _KEY_DIR 改用 DATA_DIR |
| `backend/app/db/database.py` | ✅ | 移除 SQLite 分支/锁重试，PG-only 引擎 |
| `backend/app/main.py` | ✅ | db_type 硬编码 postgresql |

### Phase 1 待完成
| 文件 | 待办 |
|------|------|
| `backend/.env.example` | 移除 SQLITE_PATH，DATABASE_URL 改必填，新增 DATA_DIR，更新注释 |
| `backend/requirements.txt` | 移除 aiosqlite==0.20.0 |
| 10 个文件的注释清理 | clarify_agent / review_agent / seed / exceptions / knowledge / knowledge_qa / products / orchestrator / settings 中的 "SQLite" 注释 |

### 未开始（Phase 2-8）
| Phase | 目标 | 涉及文件 |
|-------|------|---------|
| 2 | HNSW + 分区 + req_pats | milvus_store.py |
| 3 | BM25 + RRF（zhparser） | database.py, 新建 hybrid_search.py, docker-compose.yml |
| 4 | bge-reranker 重排 | 新建 reranker.py, config.py, docker-compose.yml |
| 5 | Redis + LRU 多级缓存 | 新建 kb_cache.py, docker-compose.yml, config.py |
| 6 | Celery 异步双写 | 新建 tasks/, docker-compose.yml, requirements.txt, requirements.py |
| 7 | 降级策略简化 | clarify_agent.py, knowledge_qa.py, milvus_store.py |
| 8 | 架构文档更新 | docs/02, docs/00, docs/05, README |

---

## 实施计划

### Phase 1 — SQLite 清理（收尾）

#### 1.1 `backend/.env.example`
- 删除 `SQLITE_PATH=./data/aitestgen.db` 行
- 取消注释 `DATABASE_URL=postgresql://...`，改为必填
- 新增 `DATA_DIR=./data`
- 更新头部注释：删除 "开发阶段 → SQLite" 路径描述
- 更新 "MVP 数据库配置" 段标题为 "数据库配置（PostgreSQL + pgvector）"
- 删除 "取消注释后生效，需同时将 SQLITE_PATH 注释掉" 说明

#### 1.2 `backend/requirements.txt`
- 删除第 51 行 `aiosqlite==0.20.0`
- 更新注释 "SQLite 异步驱动（开发/回退用）" → 删除
- 确认 asyncpg==0.30.0 和 psycopg2-binary==2.9.10 已在位

#### 1.3 注释清理（10 个文件）
全局将 "SQLite" 注释更新为 "PostgreSQL" / "PG"，具体：

| 文件 | 行号 | 当前 | 改为 |
|------|------|------|------|
| `agents/clarify_agent.py` | 87, 186, 190, 192, 217, 220, 748, 754, 759, 763, 1194, 1197, 1200, 1239, 1360, 1373 | "SQLite 异步会话" / "SQLite 关系增强" / "_enrich_with_sqlite" | "PG 异步会话" / "PG 关系增强" / "_enrich_with_pg" |
| `agents/review_agent.py` | 435 | "SQLite + Vector DB 双写" | "PG + Vector DB 双写" |
| `db/seed.py` | 3, 229, 241 | "SQLite 与 Milvus" / "插入 SQLite" | "PostgreSQL 与 Milvus" / "插入 PG" |
| `core/exceptions.py` | 46 | "数据库锁定（SQLite 并发写入冲突）" | "数据库锁定（并发写入冲突）" |
| `api/knowledge.py` | 162, 183, 210, 256, 286 | "从 SQLite 中读取" | "从 PostgreSQL 中读取" |
| `api/knowledge_qa.py` | 68, 75, 138, 180, 201 | "SQLite 过滤" / "SQLite 查询" | "PG 过滤" / "PG 查询" |
| `api/products.py` | 39, 235, 302 | "SQLite/SQLAlchemy" / "SQLite 默认未启用外键" | "PostgreSQL" / 删除 SQLite 外键说明（PG 原生支持） |
| `core/orchestrator.py` | 747 | "SQLite + Vector DB 双写" | "PG + Vector DB 双写" |
| `api/settings.py` | 4 | "存入 SQLite" | "存入 PostgreSQL" |

**注意**：`clarify_agent.py` 中的 `_enrich_with_sqlite` 方法名需重命名为 `_enrich_with_pg`，同步更新所有调用方（第 192、220 行）。

---

### Phase 2 — Milvus HNSW + 模块分区 + req_pats Collection

#### 2.1 `backend/app/db/milvus_store.py` — 索引升级

**COLLECTION_CONFIGS** 新增 `req_pats`：
```python
COLLECTION_CONFIGS = {
    "requirements": {"description": "需求向量", "dim": 1024},
    "test_cases": {"description": "用例向量", "dim": 1024},
    "patterns": {"description": "测试模式向量", "dim": 1024},
    "req_pats": {"description": "需求模式向量", "dim": 1024},  # 新增
    "errors": {"description": "错误案例向量", "dim": 1024},
}
```

**索引升级**（`_create_collection` 方法）：
```python
# IVF_FLAT → HNSW（适合 >10w 向量）
index_params = {
    "metric_type": "COSINE",
    "index_type": "HNSW",
    "params": {"M": 16, "efConstruction": 200},
}
```

**检索参数升级**（`_search` 方法）：
```python
search_params = {"metric_type": "COSINE", "params": {"ef": 64}}
```

#### 2.2 模块分区策略

**新增分区常量**：
```python
# 按模块分区，检索时只扫描目标分区，延迟降低 5-10 倍
MODULE_PARTITIONS = ["auth", "payment", "user", "order", "system", "_default"]
```

**`_ensure_collections` 方法增强**：创建 Collection 后立即创建分区：
```python
def _ensure_collections(self) -> None:
    for coll_key, cfg in COLLECTION_CONFIGS.items():
        coll_name = self._collection_name(coll_key)
        if not utility.has_collection(coll_name):
            self._create_collection(coll_name, cfg["dim"])
            self._create_partitions(coll_name)  # 新增
            logger.info("[Milvus] 创建 Collection + 分区: {}", coll_name)
```

**新增 `_create_partitions` 方法**：
```python
def _create_partitions(self, coll_name: str) -> None:
    """为 Collection 创建模块分区。"""
    collection = Collection(coll_name)
    existing_partitions = [p.name for p in collection.partitions]
    for partition_name in MODULE_PARTITIONS:
        if partition_name not in existing_partitions:
            collection.create_partition(partition_name)
```

**新增 `_get_partition_name` 方法**：根据 metadata 中的 module 选择分区：
```python
def _get_partition_name(self, metadata: dict) -> str:
    """根据 metadata 中的模块信息选择分区名。"""
    modules = metadata.get("modules", "")
    if isinstance(modules, str) and modules:
        first_module = modules.split(",")[0].strip().lower()
        if first_module in MODULE_PARTITIONS:
            return first_module
    return "_default"
```

**`_upsert` 方法增强**：写入时指定分区：
```python
collection.insert([...], partition_name=partition_name)
```

**`_search` 方法增强**：检索时可指定分区（可选，默认搜全部分区以保召回率）：
- 新增 `partition_names` 参数，调用方可传入模块列表缩小搜索范围
- 默认不指定（搜全部分区），保证召回率

#### 2.3 req_pats Collection CRUD

新增方法：
```python
def add_req_pattern(self, pattern_id: str, name: str, pattern_json: dict,
                    modules: list[str] = None) -> None: ...
def search_req_patterns(self, query: str, top_k: int = 3,
                        module: str | None = None) -> list[dict]: ...
def delete_req_pattern(self, pattern_id: str) -> None: ...
```

#### 2.4 索引迁移兼容

`_ensure_collections` 中对已存在的 Collection 检查索引类型，若为 IVF_FLAT 则重建为 HNSW：
```python
def _ensure_collections(self) -> None:
    for coll_key, cfg in COLLECTION_CONFIGS.items():
        coll_name = self._collection_name(coll_key)
        if not utility.has_collection(coll_name):
            self._create_collection(coll_name, cfg["dim"])
            self._create_partitions(coll_name)
        else:
            self._migrate_index_if_needed(coll_name)  # 新增
```

---

### Phase 3 — BM25 混合检索 + RRF 融合（zhparser PG 原生扩展）

#### 3.1 自定义 PostgreSQL Dockerfile

新建 `docker/postgres.Dockerfile`：
```dockerfile
FROM pgvector/pgvector:pg16

# 安装 zhparser 中文分词扩展
RUN apt-get update && apt-get install -y \
    postgresql-16-zhparser \
    && rm -rf /var/lib/apt/lists/*

# 初始化脚本：创建扩展和配置
COPY docker/init-zhparser.sql /docker-entrypoint-initdb.d/
```

新建 `docker/init-zhparser.sql`：
```sql
CREATE EXTENSION IF NOT EXISTS zhparser;
CREATE TEXT SEARCH CONFIGURATION chinese (PARSER = zhparser);
ALTER TEXT SEARCH CONFIGURATION chinese ADD MAPPING FOR n,v,a,i,e,l WITH simple;
```

#### 3.2 `docker-compose.yml` — PG 服务改用自定义镜像

```yaml
postgres:
  build:
    context: .
    dockerfile: docker/postgres.Dockerfile
  # 替换原来的 image: pgvector/pgvector:pg16
```

#### 3.3 `backend/app/db/database.py` — init_db 增强

在 `init_db()` 的 `_migrate` 函数中新增：

```python
# Step 5: BM25 全文检索 — 为 requirements 和 test_cases 表添加 tsvector 列 + GIN 索引
BM25_SETUPS = [
    ("requirements", "search_vec", "title || ' ' || COALESCE(raw_text, '')"),
    ("test_cases", "search_vec", "title || ' ' || COALESCE(case_json::text, '')"),
]
for table, vec_col, ts_expr in BM25_SETUPS:
    if not inspector.has_table(table):
        continue
    existing_cols = {c["name"] for c in inspector.get_columns(table)}
    if vec_col not in existing_cols:
        conn_sync.execute(text(
            f"ALTER TABLE {table} ADD COLUMN {vec_col} tsvector"
        ))
        conn_sync.execute(text(
            f"UPDATE {table} SET {vec_col} = to_tsvector('chinese', {ts_expr})"
        ))
        conn_sync.execute(text(
            f"CREATE INDEX idx_{table}_{vec_col} ON {table} USING GIN({vec_col})"
        ))
        logger.info("DB 迁移: {}.{} tsvector 列 + GIN 索引已创建", table, vec_col)
```

#### 3.4 新建 `backend/app/db/hybrid_search.py`

```python
"""混合检索模块 — Milvus 向量检索 + PostgreSQL BM25 关键词检索 + RRF 融合。

设计依据：docs/02-knowledge-base.md §3.1（混合检索架构）
"""

class HybridSearcher:
    """混合检索器，融合向量语义检索和 BM25 关键词检索。"""

    RRF_K = 60  # RRF 融合参数

    async def search_requirements(
        self,
        query: str,
        product_id: str | None = None,
        modules: list[str] | None = None,
        top_k: int = 20,
        status: str | None = None,
        session: AsyncSession = None,
    ) -> list[dict]:
        """混合检索需求：向量 + BM25 → RRF 融合 → Top-K。"""
        # 1. Milvus 向量检索（异步 to_thread）
        vector_results = await asyncio.to_thread(
            vector_store.search_similar_requirements,
            query=query, product_id=product_id, modules=modules,
            top_k=top_k * 2, status=status,
        )

        # 2. PG BM25 关键词检索
        bm25_results = []
        if session:
            bm25_results = await self._bm25_search(
                session, query, product_id, modules, top_k * 2, status
            )

        # 3. RRF 融合
        fused = self._rrf_fuse(vector_results, bm25_results)

        return fused[:top_k]

    async def _bm25_search(self, session, query, product_id, modules, top_k, status) -> list[dict]:
        """PostgreSQL BM25 全文检索（使用 zhparser 中文分词）。"""
        sql = text("""
            SELECT id, ts_rank(search_vec, plainto_tsquery('chinese', :q)) AS rank,
                   title, srr_json
            FROM requirements
            WHERE search_vec @@ plainto_tsquery('chinese', :q)
              AND (:product_id IS NULL OR product_id = :product_id)
              AND (:status IS NULL OR status = :status)
            ORDER BY rank DESC
            LIMIT :top_k
        """)
        result = await session.execute(sql, {
            "q": query, "product_id": product_id,
            "status": status, "top_k": top_k,
        })
        return [
            {"id": r.id, "similarity": float(r.rank), "document": r.title,
             "metadata": {"req_id": r.id}}
            for r in result
        ]

    def _rrf_fuse(self, vector_results: list[dict], bm25_results: list[dict]) -> list[dict]:
        """Reciprocal Rank Fusion (k=60) 融合两路检索结果。"""
        scores: dict[str, float] = {}
        items: dict[str, dict] = {}

        for rank, item in enumerate(vector_results):
            item_id = item["id"]
            scores[item_id] = scores.get(item_id, 0) + 1.0 / (self.RRF_K + rank + 1)
            items[item_id] = item

        for rank, item in enumerate(bm25_results):
            item_id = item["id"]
            scores[item_id] = scores.get(item_id, 0) + 1.0 / (self.RRF_K + rank + 1)
            if item_id not in items:
                items[item_id] = item

        fused = [
            {**items[id], "rrf_score": score}
            for id, score in sorted(scores.items(), key=lambda x: -x[1])
        ]
        return fused


hybrid_searcher = HybridSearcher()
```

#### 3.5 集成到 ClarifyAgent

`clarify_agent.py` 中的 KB 查询方法改用 `hybrid_searcher` 替代直接调用 `vector_store.search_similar_requirements`，在有 PG session 可用时走混合检索。

---

### Phase 4 — bge-reranker-base 重排

#### 4.1 新建 `backend/app/db/reranker.py`

```python
"""Cross-Encoder 重排模块 — 使用 bge-reranker-base 对检索结果二次排序。

设计依据：docs/02-knowledge-base.md §9.5（Re-rank 优化）
"""

from FlagEmbedding import FlagReranker


class KBReRanker:
    """基于 bge-reranker-base 的 Cross-Encoder 重排器。"""

    def __init__(self):
        self._model: FlagReranker | None = None
        self._lock = threading.Lock()

    def _ensure_model(self) -> FlagReranker:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    model_path = settings.RERANKER_MODEL_NAME
                    use_fp16 = settings.RERANKER_USE_FP16
                    self._model = FlagReranker(model_path, use_fp16=use_fp16)
                    logger.info("[ReRanker] 模型已加载: {}", model_path)
        return self._model

    def rerank(self, query: str, documents: list[dict], top_n: int = 5) -> list[dict]:
        """对检索结果重排，返回 Top-N。

        Args:
            query: 查询文本
            documents: 检索结果列表（每个 dict 含 document 字段）
            top_n: 返回数量

        Returns:
            重排后的 Top-N 结果
        """
        if not documents:
            return []

        try:
            model = self._ensure_model()
            pairs = [[query, d.get("document", "")[:512]] for d in documents]
            scores = model.compute_score(pairs, normalize=True)

            if isinstance(scores, float):
                scores = [scores]

            for doc, score in zip(documents, scores):
                doc["rerank_score"] = float(score)

            documents.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
            return documents[:top_n]
        except Exception as e:
            logger.warning("[ReRanker] 重排失败，返回原始排序: {}", str(e)[:200])
            return documents[:top_n]


reranker = KBReRanker()
```

#### 4.2 `backend/app/config.py` — 新增重排配置

```python
# ========== 重排配置 ==========
"""是否启用 Cross-Encoder 重排"""
RERANKER_ENABLED: bool = True

"""重排模型名称（bge-reranker-base，轻量级，~20ms/50 条）"""
RERANKER_MODEL_NAME: str = "BAAI/bge-reranker-base"

"""重排模型计算设备"""
RERANKER_DEVICE: str = "cpu"

"""是否使用 FP16 加速（CPU 下建议 False）"""
RERANKER_USE_FP16: bool = False
```

#### 4.3 `docker-compose.yml` — 挂载重排模型

```yaml
backend:
  volumes:
    - ./models/bge-reranker-base:/models/bge-reranker-base:ro  # 新增
```

#### 4.4 `backend/requirements.txt` — 新增依赖

```
FlagEmbedding==1.2.10              # bge-reranker-base 重排模型
```

#### 4.5 集成到 hybrid_search.py

在 `HybridSearcher.search_requirements` 末尾加入重排：
```python
# 4. Cross-Encoder 重排
if settings.RERANKER_ENABLED and len(fused) > settings.MATCH_FINAL_N:
    fused = await asyncio.to_thread(
        reranker.rerank, query, fused, top_n=settings.MATCH_FINAL_N
    )
```

---

### Phase 5 — Redis + LRU 多级缓存

#### 5.1 新建 `backend/app/db/kb_cache.py`

```python
"""知识库多级缓存 — L1 Redis 语义缓存 + L2 本地 LRU 缓存。

设计依据：docs/02-knowledge-base.md §9.4（多级缓存）
"""

import functools
from collections import OrderedDict


class LRUCache:
    """简单的线程安全 LRU 缓存。"""
    def __init__(self, capacity: int = 1000):
        self._capacity = capacity
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            return None

    def set(self, key: str, value):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = value
            if len(self._cache) > self._capacity:
                self._cache.popitem(last=False)


class KBCache:
    """知识库多级缓存管理器。"""

    def __init__(self):
        self._l2 = LRUCache(capacity=1000)  # 本地 LRU
        self._redis = None  # Redis 客户端（延迟初始化）

    def _get_redis(self):
        if self._redis is None and settings.has_redis:
            import redis
            self._redis = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                password=settings.REDIS_PASSWORD or None,
                decode_responses=True,
            )
        return self._redis

    def _cache_key(self, query: str, product_id: str, collection: str) -> str:
        """生成缓存键（基于查询文本 + 产品 + Collection）。"""
        import hashlib
        raw = f"{collection}:{product_id}:{query}"
        return f"kb:{hashlib.sha256(raw.encode()).hexdigest()}"

    async def get(self, query: str, product_id: str, collection: str) -> list[dict] | None:
        """查询缓存：L2 本地 → L1 Redis。"""
        key = self._cache_key(query, product_id, collection)

        # L2 本地缓存
        cached = self._l2.get(key)
        if cached is not None:
            return cached

        # L1 Redis 缓存
        redis = self._get_redis()
        if redis:
            try:
                import json
                raw = redis.get(key)
                if raw:
                    result = json.loads(raw)
                    self._l2.set(key, result)  # 回填 L2
                    return result
            except Exception as e:
                logger.warning("[KBCache] Redis 读取失败: {}", str(e)[:100])

        return None

    async def set(self, query: str, product_id: str, collection: str, results: list[dict], ttl: int = 86400):
        """写入缓存：L2 本地 + L1 Redis。"""
        key = self._cache_key(query, product_id, collection)

        # L2 本地缓存
        self._l2.set(key, results)

        # L1 Redis 缓存
        redis = self._get_redis()
        if redis:
            try:
                import json
                redis.setex(key, ttl, json.dumps(results, ensure_ascii=False))
            except Exception as e:
                logger.warning("[KBCache] Redis 写入失败: {}", str(e)[:100])


kb_cache = KBCache()
```

#### 5.2 `docker-compose.yml` — 新增 Redis 服务

```yaml
redis:
  container_name: ai-test-gen-redis
  image: redis:7-alpine
  ports:
    - "6379:6379"
  volumes:
    - ./data/redis:/data
  command: redis-server --appendonly yes --maxmemory 256mb --maxmemory-policy allkeys-lru
  healthcheck:
    test: ["CMD", "redis-cli", "ping"]
    interval: 10s
    timeout: 5s
    retries: 5
  networks:
    - ai-test-gen-net
  restart: unless-stopped
```

#### 5.3 `backend/app/config.py` — Redis 配置已有，确保环境变量传入

在 docker-compose.yml 的 backend.environment 新增：
```yaml
- REDIS_HOST=redis
- REDIS_PORT=6379
```

#### 5.4 集成到 hybrid_search.py

在 `search_requirements` 开头加入缓存查询：
```python
# 0. 缓存查询
cached = await kb_cache.get(query, product_id or "", "requirements")
if cached is not None:
    return cached[:top_k]

# ... 检索逻辑 ...

# 5. 写入缓存
await kb_cache.set(query, product_id or "", "requirements", fused)
return fused[:top_k]
```

#### 5.5 `backend/requirements.txt` — 新增依赖

```
redis==5.0.1                        # L1 语义缓存
```

---

### Phase 6 — Celery 异步双写

#### 6.1 新建 `backend/app/tasks/__init__.py`
空文件。

#### 6.2 新建 `backend/app/tasks/celery_app.py`

```python
"""Celery 应用配置 — 异步任务队列（嵌入向量计算 + 知识库双写）。

设计依据：docs/02-knowledge-base.md §9.7（异步双写一致性方案）
"""

from celery import Celery
from app.config import settings

celery_app = Celery(
    "ai_test_gen",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_routes={
        "tasks.embedding_tasks.embed_and_store_requirement": {"queue": "embedding"},
    },
    task_default_max_retries=3,
    task_retry_backoff=True,
    task_retry_backoff_max=300,
)
```

#### 6.3 新建 `backend/app/tasks/embedding_tasks.py`

```python
"""嵌入计算异步任务 — PG 写入成功后异步计算 Embedding 并写入 Milvus。"""

from app.tasks.celery_app import celery_app
from app.db.milvus_store import vector_store
from app.utils.logger import logger


@celery_app.task(
    name="tasks.embedding_tasks.embed_and_store_requirement",
    bind=True,
    max_retries=3,
    default_retry_delay=5,
)
def embed_and_store_requirement(self, req_id: str, srr_json: dict, metadata: dict):
    """异步计算嵌入并写入 Milvus（从 PG 归档触发）。

    失败重试 3 次，最终失败写入 DLQ（Celery 自动处理）。
    """
    try:
        vector_store.add_requirement(
            req_id=req_id,
            srr_json=srr_json,
            metadata=metadata,
        )
        logger.info("[Celery] 嵌入写入完成: req_id={}", req_id)
    except Exception as exc:
        logger.error("[Celery] 嵌入写入失败: req_id={}, error={}, retry={}",
                     req_id, str(exc)[:200], self.request.retries)
        raise self.retry(exc=exc)


@celery_app.task(name="tasks.embedding_tasks.sync_check")
def sync_check():
    """对账任务：PG 有但 Milvus 没有的记录 → 补写（每小时执行）。"""
    # 实现 PG ↔ Milvus 对账逻辑
    ...
```

#### 6.4 `backend/app/api/requirements.py` — 归档端点改异步双写

将 archive_requirement 中的同步 Milvus 写入改为 Celery 异步任务：

```python
# 旧代码（同步双写）：
# await asyncio.to_thread(vector_store.add_requirement, ...)

# 新代码（异步双写）：
try:
    from app.tasks.embedding_tasks import embed_and_store_requirement
    embed_and_store_requirement.delay(
        req_id=record.id,
        srr_json=record.srr_json or {},
        metadata=milvus_meta,
    )
    kb_written = True  # 表示已提交异步任务
    logger.info("[API/requirements] 异步写入任务已提交: req_id={}", req_id)
except Exception as e:
    kb_error = str(e)[:300]
    logger.warning("[API/requirements] 异步任务提交失败: req_id={}, error={}", req_id, kb_error[:200])
```

#### 6.5 `docker-compose.yml` — 新增 Celery Worker

```yaml
celery_worker:
  build:
    context: .
    dockerfile: backend/Dockerfile
    args:
      BASE_IMAGE: ai-test-gen-base:latest
  container_name: ai-test-gen-celery
  volumes:
    - ./backend:/app
    - ./models/bge-large-zh-v1.5:/models/bge-large-zh-v1.5:ro
  env_file:
    - ./backend/.env
  environment:
    - DATABASE_URL=postgresql+asyncpg://aitestgen:aitestgen123@postgres:5432/ai_test_gen
    - CELERY_BROKER_URL=redis://redis:6379/1
    - CELERY_RESULT_BACKEND=redis://redis:6379/2
    - MILVUS_HOST=milvus
    - MILVUS_PORT=19530
    - EMBEDDING_MODEL_NAME=/models/bge-large-zh-v1.5
  command: celery -A app.tasks.celery_app worker --loglevel=info -Q embedding
  depends_on:
    - redis
    - postgres
    - milvus
  networks:
    - ai-test-gen-net
  restart: unless-stopped
```

#### 6.6 `backend/.env.example` — 新增 Celery 配置

```bash
# Celery（异步双写）
CELERY_BROKER_URL=redis://localhost:6379/1
CELERY_RESULT_BACKEND=redis://localhost:6379/2
```

#### 6.7 `backend/requirements.txt` — 新增依赖

```
celery[redis]==5.4.0               # 异步任务队列（嵌入计算）
```

#### 6.8 `backend/app/config.py` — 新增 Celery 配置

```python
# ========== Celery 配置 ==========
CELERY_BROKER_URL: str = ""
CELERY_RESULT_BACKEND: str = ""
```

---

### Phase 7 — 降级策略简化

#### 7.1 核心原则

**用户要求**：KB 查询不到 → 跳过 → 直接生成 → 人工澄清兜底

**不做的事**（删除/不实现）：
- ❌ Milvus 失败时 fallback 到 PG 全文检索（Level 1 降级）
- ❌ PG 失败时 fallback 到 Redis 缓存（Level 2 降级）
- ❌ Redis 失败时 fallback 到静态规则（Level 3 降级）
- ❌ 规则匹配失败时返回 503（Level 4 降级）

**做的事**：
- ✅ KB 查询失败 → log warning → matched = [] → 跳过 KB 增强 → 正常生成
- ✅ ClarifyAgent 后续自动检测缺口 → 生成人工澄清问题
- ✅ BM25/RRF/ReRank/Cache 都是 KB 可用时的增强，不可用时整体跳过

#### 7.2 `backend/app/agents/clarify_agent.py` — 确认现有行为

当前代码（第 798-800 行）已实现正确的降级：
```python
except Exception as e:
    logger.warning("[ClarifyAgent] KB 查询失败: q={!r} error={}", ...)
    matched = []
```

**需修改**：将 `_enrich_with_sqlite` 方法重命名为 `_enrich_with_pg`，更新注释。

#### 7.3 `backend/app/api/knowledge_qa.py` — 简化降级

KB QA 端点在向量检索失败时，返回友好提示而非 503：
```python
try:
    results = await asyncio.to_thread(vector_store.search_similar_requirements, ...)
except Exception as e:
    logger.warning("[KBQA] 向量检索失败，返回空结果: {}", str(e)[:200])
    results = []  # 跳过，不阻断
```

#### 7.4 `backend/app/db/hybrid_search.py` — 降级逻辑

在混合检索中，任一路失败不阻断另一路：
```python
# 向量检索失败 → 只用 BM25
try:
    vector_results = await asyncio.to_thread(...)
except Exception as e:
    logger.warning("[HybridSearch] 向量检索失败，仅用 BM25: {}", str(e)[:200])
    vector_results = []

# BM25 检索失败 → 只用向量
try:
    bm25_results = await self._bm25_search(...)
except Exception as e:
    logger.warning("[HybridSearch] BM25 检索失败，仅用向量: {}", str(e)[:200])
    bm25_results = []

# 两者都失败 → 返回空（上层走人工澄清）
if not vector_results and not bm25_results:
    return []
```

---

### Phase 8 — 架构文档更新

#### 8.1 `docs/02-knowledge-base.md`

**§2.6 降级策略** — 重写为简化版：
```
Level 0 (KB 可用): Milvus + PG 混合检索 + ReRank + Cache
    ↓ KB 不可用
Level 1 (跳过 KB): 不检索 → 直接生成 → ClarifyAgent 人工澄清兜底

注意：不再做多级 PG/Redis/规则 fallback，避免降级路径污染生成质量。
```

**§2.1 选型表** — 更新：
- ChromaDB 行标注 "已弃用移除"
- pgvector 行标注 "仅用于 BM25 全文检索，不作为向量库"

**§3.1 混合检索架构** — 标注 "已实现"：
- 向量检索（Milvus HNSW）
- BM25 关键词检索（PG zhparser）
- RRF 融合（k=60）
- Cross-Encoder 重排（bge-reranker-base）
- 多级缓存（Redis + LRU）

**§9 大规模检索优化** — 标注 "已实现"：
- §9.2 分区与分片：按模块分区（auth/payment/user/order/system/_default）
- §9.3 HNSW 索引：M=16, efConstruction=200, ef=64
- §9.4 多级缓存：L1 Redis + L2 LRU
- §9.5 Re-rank：bge-reranker-base
- §9.7 异步双写：Celery + 每小时对账

**§2.5 配置示例** — 更新环境变量：
- 移除 SQLITE_PATH
- 新增 DATA_DIR
- 新增 RERANKER_* 配置
- 新增 CELERY_* 配置
- DATABASE_URL 改为必填

#### 8.2 `docs/00-architecture-overview.md`

- 技术栈表格：移除 SQLite，PostgreSQL 标注为唯一关系数据库
- 新增 Redis、Celery 到技术栈
- 新增 bge-reranker-base 到模型列表

#### 8.3 `docs/05-technical-selection.md`

- §8.1 迁移路径：更新为 "PostgreSQL + Milvus（统一）"
- §10 依赖清单：新增 FlagEmbedding、redis、celery[redis]；移除 aiosqlite
- §11 容量规划：更新 Redis 和 Celery 的资源规划

#### 8.4 `README.md`（如存在）

- 快速开始：更新 docker-compose 启动命令（含 Redis + Celery）
- 技术栈：更新

---

## 假设与决策

### 用户已确认的决策
1. **BM25 中文分词**：使用 zhparser PG 原生扩展（非 jieba 预处理）
2. **增强范围**：全套增强（HNSW + 分区 + BM25/RRF + bge-reranker + Redis 缓存 + Celery 异步）
3. **降级策略**：KB 不可用 → 跳过 → 直接生成 → 人工澄清兜底（不做多级 fallback）
4. **SQLite**：彻底移除，PG 作为唯一关系数据库
5. **Milvus**：使用 >10w 条的优化策略（HNSW + 分区）

### 技术假设
- bge-reranker-base 模型需预下载到 `./models/bge-reranker-base/`
- zhparser 在 pgvector/pgvector:pg16 镜像上可通过 apt 安装（postgresql-16-zhparser 包）
- Celery Worker 与 Backend 共享代码卷，使用相同基础镜像
- Redis 使用 7-alpine 镜像，256MB 内存上限，LRU 淘汰策略
- 现有 IVF_FLAT 索引的 Collection 需要在首次启动时迁移为 HNSW（_migrate_index_if_needed）

### 依赖项
- `FlagEmbedding==1.2.10` — bge-reranker-base 推理
- `redis==5.0.1` — L1 缓存
- `celery[redis]==5.4.0` — 异步任务队列
- 移除 `aiosqlite==0.20.0`

---

## 验证步骤

### Phase 1 验证
1. `grep -ri "sqlite" backend/` 应返回 0 结果（代码层面）
2. `grep -ri "SQLITE_PATH" backend/` 应返回 0 结果
3. 应用正常启动，PG 连接成功
4. prompt_store CRUD 正常（通过 /api/admin/prompts 端点测试）

### Phase 2 验证
1. Milvus Collection 列表包含 `aitestgen_req_pats`
2. 各 Collection 的索引类型为 HNSW（通过 Milvus CLI 检查）
3. 各 Collection 包含 6 个分区（auth/payment/user/order/system/_default）
4. 需求向量写入时进入正确的模块分区
5. 检索结果正确返回

### Phase 3 验证
1. PostgreSQL 中 `SELECT * FROM pg_extension WHERE extname = 'zhparser'` 返回结果
2. `requirements` 表包含 `search_vec` tsvector 列
3. `plainto_tsquery('chinese', '用户登录')` 能正确分词
4. 混合检索返回融合后的结果（向量 + BM25）
5. RRF 融合排序合理

### Phase 4 验证
1. bge-reranker-base 模型加载成功（启动日志可见）
2. 检索结果包含 `rerank_score` 字段
3. 重排后 Top-5 结果质量优于重排前

### Phase 5 验证
1. Redis 连接成功（健康检查）
2. 重复查询第二次返回缓存结果（延迟显著降低）
3. L2 本地缓存命中率 10-15%

### Phase 6 验证
1. 归档需求后，Celery Worker 日志显示嵌入任务执行
2. Milvus 中出现新向量（异步写入完成）
3. Celery 对账任务每小时执行（crontab 配置）

### Phase 7 验证
1. Milvus 停止后，归档需求仍成功（PG 写入正常，KB 写入任务进入重试队列）
2. ClarifyAgent 在 KB 不可用时正常生成澄清问题
3. 无 503 错误返回

### Phase 8 验证
1. doc-02 降级策略章节与代码实现一致
2. doc-02 §9 各小节标注 "已实现"
3. docker-compose up -d 启动所有服务（PG + Milvus + Redis + Celery + Backend + Frontend）
