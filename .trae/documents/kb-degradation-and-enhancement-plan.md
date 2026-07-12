# 知识库降级策略简化与增强完善方案

> **范围**：在已完成 SQLite 移除（Phase 1）和 Milvus HNSW+分区+req_pats（Phase 2）的基础上，完成剩余的混合检索集成、重排器、缓存、异步双写、降级策略简化与文档同步更新。

---

## 一、摘要

用户核心诉求：
1. **降级策略简化** — "一旦知识库查询不到就无所谓直接生成，后面有人工澄清作为兜底"。KB 不可用 → 静默跳过 → 正常生成 → 人工澄清兜底。**不要**多级 PG/Redis/规则回退污染生成质量。
2. **SQLite 移除** — 已使用 PG，移除 SQLite 冗余（Phase 1 已完成，本方案仅做最终验证）。
3. **Milvus >10w 策略** — HNSW + 分区（Phase 2 已完成，本方案仅做最终验证）。
4. **其他方面最优策略** — 完成 BM25+RRF 混合检索集成、bge-reranker 重排、Redis 多级缓存、Celery 异步双写。
5. **同时修改架构设计文档和代码** — docs/02 §2.6/§3.1/§9 全面更新，代码落地。

---

## 二、当前状态分析

### ✅ 已完成（Phase 1：SQLite 移除）

经全量 grep 验证，`backend/` 下仅剩 2 处 SQLite 字样，均为**文档注释**（说明"已弃用"），无任何代码逻辑引用：
- `backend/.env.example:15` — 注释 "统一架构，无 SQLite"
- `backend/app/db/database.py:4` — 模块 docstring "SQLite 已弃用移除"

`prompt_store.py` 已使用 SQLAlchemy + psycopg2 同步引擎，`config.py` 无 `SQLITE_PATH`，`requirements.txt` 无 `aiosqlite`。

**结论**：Phase 1 完成，无需额外修改。

### ✅ 已完成（Phase 2：Milvus HNSW + 分区 + req_pats）

`backend/app/db/milvus_store.py` 已实现：
- HNSW 索引：`M=16, efConstruction=200, ef=64`（替代 IVF_FLAT）
- 5 个 Collection：`requirements / test_cases / patterns / req_pats / errors`
- 模块分区：`auth/payment/user/order/system/_default`，写入按 metadata.modules 路由
- IVF_FLAT → HNSW 自动迁移逻辑（`_migrate_index_if_needed`）

**结论**：Phase 2 完成，无需额外修改。

### 🟡 部分完成（Phase 3：BM25 + RRF）

**已完成**：
- ✅ `docker/postgres.Dockerfile` — 基于 `pgvector/pgvector:pg16`，安装 `postgresql-16-zhparser`
- ✅ `docker/init-zhparser.sql` — 创建 zhparser 扩展 + `chinese` 文本搜索配置 + 词性映射
- ✅ `docker-compose.yml` — postgres 服务改为 `build: docker/postgres.Dockerfile`
- ✅ `backend/app/db/database.py` Step 5 — `requirements` 和 `test_cases` 表添加 `search_vec` tsvector 列 + GIN 索引
- ✅ `backend/app/db/hybrid_search.py` — `HybridSearcher` 类，RRF 融合（k=60），BM25 SQL 查询，内部降级（向量失败→仅BM25，BM25失败→仅向量，两者失败→空列表），lazy import `kb_cache` 和 `reranker`

**未完成**：
- ❌ `clarify_agent.py` 仍直接调用 `vector_store.search_similar_requirements`（L792），未走 `hybrid_searcher`
- ❌ `knowledge_qa.py` 仍直接调用 `vector_store.search_similar_requirements`（L357），未走 `hybrid_searcher`
- ❌ `orchestrator.py` `_retrieve_node` 仍直接调用 `vector_store` 三个检索方法（L578/610/642），未走 `hybrid_searcher`
- ❌ `config.py` 缺少 `RERANKER_ENABLED` 配置项（`hybrid_search.py:92` 已引用）

### ❌ 未开始（Phase 4：bge-reranker 重排器）

`backend/app/db/reranker.py` 不存在。`hybrid_search.py:94` 通过 lazy import 引用 `reranker`，模块未创建时该代码路径会触发 `ImportError`（被 except 捕获，降级为 RRF 排序）。

### ❌ 未开始（Phase 5：Redis 多级缓存）

`backend/app/db/kb_cache.py` 不存在。`hybrid_search.py:56/102` 通过 lazy import 引用 `kb_cache`，模块未创建时缓存读写会触发 `ImportError`（被 except 捕获，跳过缓存）。

`docker-compose.yml` **无 Redis 服务**。`config.py` 有 `REDIS_HOST/PORT/DB/PASSWORD` 配置和 `has_redis` 属性，但无实际 Redis 实例。

### ❌ 未开始（Phase 6：Celery 异步双写）

`backend/app/tasks/` 目录不存在。`celery_app.py` 和 `embedding_tasks.py` 均未创建。

`backend/app/api/requirements.py:1471` 归档端点仍为**同步双写**：PG commit → `asyncio.to_thread(vector_store.add_requirement)` 阻塞等待 Milvus 写入完成。

`backend/app/api/knowledge_qa.py:589/629` 知识问答归档端点同样是同步 Milvus 写入。

### 🟡 部分完成（Phase 7：降级策略简化）

**生成流水线（已正确）**：
- ✅ `clarify_agent.py:798-800` — KB 查询失败 → `matched = []` → 继续生成澄清问题
- ✅ `orchestrator.py:_retrieve_node` — 三个 KB 查询各自 try/except，失败 → `few_shot_context` 对应字段为空列表 → GenAgent 无 few-shot 生成
- ✅ 人工澄清作为兜底（QualityGate + ClarifyLoopGuard 3 轮迭代）

**知识问答（需调整）**：
- ⚠️ `knowledge_qa.py:265-275` — KB 不可用时向 Prompt 注入诊断信息（"请在回答中告知用户知识库当前不可用"），这会**污染 LLM 生成**，让回答变成"知识库不可用"的告警而非真正回答用户问题

**文档（需重写）**：
- ⚠️ `docs/02-knowledge-base.md` §2.6 — 仍描述 5 级降级（L0 全能力 → L1 PG 备用 → L2 Redis 只读 → L3 静态规则 → L4 拒绝服务 503），与用户"简化降级"诉求冲突

### ❌ 未开始（Phase 8：架构文档更新）

`docs/02-knowledge-base.md` 需更新章节：
- §2.6 错误处理 — 5 级降级 → 简化降级
- §3.1 混合检索架构 — 补充实际实现细节（zhparser + RRF + hybrid_searcher）
- §9.7 异步双写 — 从"示例代码"更新为"Celery 实际实现"
- §9.4 多级缓存 — 从"设计"更新为"Redis + LRU 实际实现"

---

## 三、降级策略再分析（用户核心诉求）

### 3.1 用户原则

> "我不想影响内容生成结果，所以一旦知识库查询不到就无所谓直接生成后面有人工澄清作为兜底"

核心原则：**KB 是增强项，不是阻断项**。KB 不可用时，生成质量可能略降（无 few-shot 参考），但**绝不能因为 KB 降级逻辑本身污染生成结果**。

### 3.2 新降级策略（简化版）

```
KB 查询（向量 + BM25 混合检索）
    │
    ├─ 成功 → 返回检索结果 → 增强 Prompt → 生成
    │
    └─ 失败（向量+BM25 均失败）
         │
         └─ 静默跳过 → 返回空列表 → 生成无 KB 增强
                                        │
                                        └─ 人工澄清兜底（QualityGate + ClarifyLoopGuard）
```

**关键变化**（对比旧 5 级降级）：
- ❌ 删除 L1 PG 备用降级 — PG 全文检索已在 `hybrid_searcher` 内部作为并列检索源，不是"降级"
- ❌ 删除 L2 Redis 只读缓存降级 — Redis 缓存是性能优化，不是降级路径
- ❌ 删除 L3 静态规则降级 — 不存在规则引擎，删除虚构设计
- ❌ 删除 L4 拒绝服务 503 — KB 不可用不阻断生成，永远不返回 503
- ✅ 保留：KB 内部混合检索的优雅降级（向量失败→仅BM25，BM25失败→仅向量）— 这是检索质量优化，不是系统级降级

### 3.3 各模块降级行为

| 模块 | KB 失败时行为 | 是否影响生成质量 | 兜底机制 |
|------|-------------|----------------|---------|
| **ClarifyAgent** | `matched=[]` → 不补全答案 → 生成澄清问题 | ❌ 不影响（本就不依赖 KB 必须有结果） | 人工澄清 3 轮 |
| **retrieve_node** | `few_shot_context` 各字段为空 → GenAgent 无 few-shot 生成 | ⚠️ 略降（无历史参考，用例可能不够丰富） | 人工审核 + 迭代 |
| **GenAgent** | 收到空 `few_shot_context` → 正常生成（Prompt 中 few-shot 段为空） | ⚠️ 略降 | ReviewAgent + 人工审核 |
| **knowledge_qa.py** | **见 §3.4 决策** | — | — |

### 3.4 知识问答降级决策

知识问答（`knowledge_qa.py`）与生成流水线不同：它是**纯检索增强问答**，系统 Prompt 明确要求"只能基于提供的知识库上下文回答"。KB 不可用时：

**当前行为**（需修改）：向 Prompt 注入诊断信息 → LLM 生成"知识库不可用"的告警回答 → **污染了输出**

**新行为**（简化）：
- KB 检索成功但 0 结果 → 正常调用 LLM（空上下文，LLM 会回答"知识库中未找到相关信息"）
- KB 检索失败（search_ok=False）→ **不调用 LLM**，直接返回错误响应：
  ```json
  {"success": false, "message": "知识库检索暂时不可用，请稍后重试", "data": {"kb_status": {...}}}
  ```
  前端展示友好错误提示，不浪费 LLM 调用，不产生误导性回答。

---

## 四、待完成工作

### A. Phase 3 收尾：hybrid_searcher 集成

#### A1. 补充 config.py 配置项

**文件**：`backend/app/config.py`

在"检索配置"区块后新增"重排器配置"：
```python
# ========== 重排器配置 ==========
"""是否启用 Cross-Encoder 重排（bge-reranker-base）"""
RERANKER_ENABLED: bool = True

"""重排模型名称（本地路径或 HuggingFace ID）"""
RERANKER_MODEL_NAME: str = "BAAI/bge-reranker-base"

"""重排模型计算设备"""
RERANKER_DEVICE: str = "cpu"

"""重排 Top-N 数量（超过此数量才触发重排）"""
RERANKER_THRESHOLD: int = 5
```

在"Redis 配置"区块后新增"Celery 配置"：
```python
# ========== Celery 配置 ==========
"""Celery broker URL（Redis）"""
CELERY_BROKER_URL: str = ""

"""Celery result backend URL（Redis）"""
CELERY_RESULT_BACKEND: str = ""
```

#### A2. 集成 hybrid_searcher 到 ClarifyAgent

**文件**：`backend/app/agents/clarify_agent.py`

修改 `_query_kb_for_answers` 方法（L791-800），将直接调用 `vector_store.search_similar_requirements` 改为调用 `hybrid_searcher.search_requirements`：

```python
# 修改前（L792）:
matched = await asyncio.to_thread(
    vector_store.search_similar_requirements,
    query=q["question"], top_k=5,
    product_id=srr.get("requirement", {}).get("product_id", ""),
    status="archived",
)

# 修改后:
from app.db.hybrid_search import hybrid_searcher
# ClarifyAgent 无 PG session 入参，传 None → hybrid_searcher 仅走向量检索
# （BM25 需要 PG session，无 session 时跳过 BM25，不报错）
matched = await hybrid_searcher.search_requirements(
    query=q["question"], top_k=5,
    product_id=srr.get("requirement", {}).get("product_id", ""),
    status="archived",
    session=None,  # ClarifyAgent 无 PG session
)
```

**注意**：ClarifyAgent 的 `clarify()` 方法签名无 PG session 参数。为保持 BM25 能力，有两种方案：
- **方案 A（推荐）**：在 `clarify()` 方法中通过 `async_session()` 临时创建 PG session 传入
- **方案 B**：传 `None`，仅走向量检索（BM25 不生效）

采用**方案 A**：在 `_query_kb_for_answers` 内部用 `async with async_session() as session:` 创建临时会话供 BM25 使用。

#### A3. 集成 hybrid_searcher 到 knowledge_qa.py

**文件**：`backend/app/api/knowledge_qa.py`

修改 `kqa_ask` 端点（L357-378），将直接调用 `vector_store` 改为 `hybrid_searcher`：

```python
# 修改前:
req_results = await asyncio.to_thread(
    vector_store.search_similar_requirements,
    question, product_id=product_id, top_k=5, status="archived",
)

# 修改后:
from app.db.hybrid_search import hybrid_searcher
req_results = await hybrid_searcher.search_requirements(
    query=question, product_id=product_id, top_k=5,
    status="archived", session=session,  # 已有 session 入参
)
```

用例检索（`search_similar_cases`）暂不集成 BM25（test_cases 表的 BM25 tsvector 已建好，但 `hybrid_searcher` 当前只有 `search_requirements` 方法）。保持 `vector_store.search_similar_cases` 直接调用。

#### A4. retrieve_node 保持直接调用 vector_store

**文件**：`backend/app/core/orchestrator.py`

`_retrieve_node` 暂不集成 `hybrid_searcher`，原因：
- retrieve_node 检索 3 种 Collection（requirements / test_cases / patterns），`hybrid_searcher` 当前只支持 requirements
- retrieve_node 无 PG session 入参（LangGraph 节点签名限制）
- retrieve_node 已有完善的 try/except 降级

**决策**：retrieve_node 保持现状，后续如需可扩展 `hybrid_searcher` 支持 test_cases 和 patterns。

---

### B. Phase 4：创建 reranker.py

#### B1. 创建重排器模块

**新文件**：`backend/app/db/reranker.py`

```python
"""Cross-Encoder 重排器 — bge-reranker-base。

对 hybrid_searcher 的 RRF 融合结果进行二次排序，提升 Top-N 精度。

设计依据：
- docs/02-knowledge-base.md §9.5（Re-rank 优化）
- FlagEmbedding 库的 FlagModel 接口
"""
from __future__ import annotations
import threading
from typing import Any
from app.config import settings
from app.utils.logger import logger


class CrossEncoderReranker:
    """bge-reranker-base Cross-Encoder 重排器。"""

    def __init__(self):
        self._model = None
        self._lock = threading.Lock()

    def _ensure_model(self):
        """延迟加载模型（线程安全）。"""
        if self._model is None:
            with self._lock:
                if self._model is None:
                    import os
                    from FlagEmbedding import FlagModel
                    model_name = settings.RERANKER_MODEL_NAME
                    device = settings.RERANKER_DEVICE
                    # 优先加载本地路径（docker 挂载）
                    if os.path.isabs(model_name) and os.path.isfile(os.path.join(model_name, "config.json")):
                        logger.info("[Reranker] 加载本地模型: {}", model_name)
                    else:
                        logger.info("[Reranker] 加载模型: {} (device={})", model_name, device)
                    self._model = FlagModel(
                        model_name,
                        use_fp16=(device != "cpu"),
                        device=device,
                    )
        return self._model

    def rerank(self, query: str, results: list[dict], top_n: int = 5) -> list[dict]:
        """对检索结果进行 Cross-Encoder 重排。

        Args:
            query: 查询文本
            results: RRF 融合后的结果列表
            top_n: 返回 Top-N

        Returns:
            list[dict]: 重排后的 Top-N 结果
        """
        if not results:
            return []
        if len(results) <= top_n:
            return results

        model = self._ensure_model()
        # 构建 query-document 对
        pairs = [[query, r.get("document", "") or r.get("metadata", {}).get("title", "")] for r in results]
        # Cross-Encoder 打分
        scores = model.compute_score(pairs, normalize=True)
        if isinstance(scores, float):
            scores = [scores]
        # 按分数降序排列
        scored = list(zip(results, scores))
        scored.sort(key=lambda x: -x[1])
        # 返回 Top-N，附加 rerank_score
        return [{**r, "rerank_score": round(s, 4)} for r, s in scored[:top_n]]


reranker = CrossEncoderReranker()
```

#### B2. docker-compose.yml 挂载重排模型

**文件**：`docker-compose.yml`

在 backend 服务的 volumes 中新增：
```yaml
- ./models/bge-reranker-base:/models/bge-reranker-base:ro
```

在 environment 中新增：
```yaml
- RERANKER_ENABLED=true
- RERANKER_MODEL_NAME=/models/bge-reranker-base
```

---

### C. Phase 5：创建 kb_cache.py + Redis 服务

#### C1. 创建缓存模块

**新文件**：`backend/app/db/kb_cache.py`

```python
"""知识库检索缓存 — L1 Redis 语义缓存 + L2 本地 LRU 缓存。

设计依据：
- docs/02-knowledge-base.md §9.4（多级缓存）
- 查询是只读操作，缓存安全
- 缓存 key: (query, product_id, collection_type)
- L1 Redis: TTL 24h，最近 5000 次查询
- L2 LRU: 进程内，最近 1000 次查询
"""
from __future__ import annotations
import hashlib
import json
from collections import OrderedDict
from threading import Lock
from typing import Any

from app.config import settings
from app.utils.logger import logger


class KbCache:
    """二级缓存：L1 Redis + L2 LRU。"""

    LRU_MAX_SIZE = 1000
    CACHE_TTL = 86400  # 24 小时

    def __init__(self):
        self._lru: OrderedDict[str, str] = OrderedDict()
        self._lru_lock = Lock()
        self._redis = None
        self._redis_checked = False

    def _get_redis(self):
        """延迟获取 Redis 连接（仅在 has_redis 时初始化）。"""
        if not self._redis_checked:
            self._redis_checked = True
            if settings.has_redis:
                try:
                    import redis
                    self._redis = redis.Redis(
                        host=settings.REDIS_HOST,
                        port=settings.REDIS_PORT,
                        db=settings.REDIS_DB,
                        password=settings.REDIS_PASSWORD or None,
                        decode_responses=True,
                        socket_timeout=2,
                    )
                    self._redis.ping()
                    logger.info("[KbCache] Redis 连接成功: {}:{}", settings.REDIS_HOST, settings.REDIS_PORT)
                except Exception as e:
                    logger.warning("[KbCache] Redis 连接失败，仅用 LRU: {}", str(e)[:100])
                    self._redis = None
        return self._redis

    def _cache_key(self, query: str, product_id: str, coll_type: str) -> str:
        """生成缓存 key。"""
        raw = f"{query}|{product_id}|{coll_type}"
        return f"kb_cache:{hashlib.md5(raw.encode()).hexdigest()}"

    async def get(self, query: str, product_id: str, coll_type: str) -> list[dict] | None:
        """查询缓存。返回 None 表示未命中。"""
        key = self._cache_key(query, product_id, coll_type)

        # L2 LRU
        with self._lru_lock:
            if key in self._lru:
                self._lru.move_to_end(key)
                try:
                    return json.loads(self._lru[key])
                except Exception:
                    del self._lru[key]

        # L1 Redis
        redis = self._get_redis()
        if redis:
            try:
                cached = redis.get(key)
                if cached:
                    # 回填 L2
                    with self._lru_lock:
                        self._lru[key] = cached
                        if len(self._lru) > self.LRU_MAX_SIZE:
                            self._lru.popitem(last=False)
                    return json.loads(cached)
            except Exception as e:
                logger.debug("[KbCache] Redis 读取失败: {}", str(e)[:80])

        return None

    async def set(self, query: str, product_id: str, coll_type: str, results: list[dict]) -> None:
        """写入缓存。"""
        if not results:
            return
        key = self._cache_key(query, product_id, coll_type)
        try:
            value = json.dumps(results, ensure_ascii=False)
        except Exception:
            return

        # L2 LRU
        with self._lru_lock:
            self._lru[key] = value
            if len(self._lru) > self.LRU_MAX_SIZE:
                self._lru.popitem(last=False)

        # L1 Redis
        redis = self._get_redis()
        if redis:
            try:
                redis.setex(key, self.CACHE_TTL, value)
            except Exception as e:
                logger.debug("[KbCache] Redis 写入失败: {}", str(e)[:80])


kb_cache = KbCache()
```

#### C2. docker-compose.yml 新增 Redis 服务

**文件**：`docker-compose.yml`

在 postgres 服务后新增：
```yaml
# ==========================================================================
# Redis（L1 缓存 + Celery broker）
# ==========================================================================
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

在 backend 服务的 environment 中新增：
```yaml
- REDIS_HOST=redis
- REDIS_PORT=6379
- CELERY_BROKER_URL=redis://redis:6379/1
- CELERY_RESULT_BACKEND=redis://redis:6379/2
```

在 backend 服务的 depends_on 中新增：
```yaml
redis:
  condition: service_healthy
```

---

### D. Phase 6：Celery 异步双写

#### D1. 创建 Celery 应用

**新文件**：`backend/app/tasks/__init__.py`（空文件）

**新文件**：`backend/app/tasks/celery_app.py`

```python
"""Celery 应用实例 — 异步嵌入计算 + 知识库双写。

设计依据：
- docs/02-knowledge-base.md §9.7（异步双写一致性方案）
- PG 同步写入（事务保证）→ Celery 异步计算 Embedding → 写入 Milvus
"""
from __future__ import annotations
from app.config import settings
from app.utils.logger import logger

try:
    from celery import Celery

    broker = settings.CELERY_BROKER_URL or "redis://localhost:6379/1"
    backend = settings.CELERY_RESULT_BACKEND or "redis://localhost:6379/2"

    celery_app = Celery(
        "ai_test_gen",
        broker=broker,
        backend=backend,
        include=["app.tasks.embedding_tasks"],
    )

    celery_app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        task_default_queue="embedding",
        task_routes={
            "app.tasks.embedding_tasks.embed_and_store_requirement": {"queue": "embedding"},
        },
    )
    logger.info("[Celery] 应用已初始化: broker={}", broker)
except ImportError:
    logger.warning("[Celery] celery 未安装，异步双写降级为同步")
    celery_app = None
```

#### D2. 创建嵌入任务

**新文件**：`backend/app/tasks/embedding_tasks.py`

```python
"""嵌入计算 + Milvus 写入异步任务。

由归档端点触发：PG commit → celery_app.send_task → Milvus 写入
失败重试 3 次，最终失败记录到日志（不阻断归档）。
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from app.tasks.celery_app import celery_app
from app.utils.logger import logger


if celery_app is not None:

    @celery_app.task(
        name="app.tasks.embedding_tasks.embed_and_store_requirement",
        bind=True,
        max_retries=3,
        default_retry_delay=5,
    )
    def embed_and_store_requirement(self, req_id: str, srr_json: dict, metadata: dict):
        """异步计算嵌入并写入 Milvus。

        Args:
            req_id: 需求 ID
            srr_json: SRR JSON
            metadata: Milvus 元数据
        """
        try:
            from app.db.milvus_store import vector_store
            vector_store.add_requirement(req_id, srr_json, metadata=metadata)
            logger.info("[Celery] 嵌入+Milvus写入成功: req_id={}", req_id)
        except Exception as exc:
            logger.error("[Celery] 嵌入+Milvus写入失败: req_id={}, error={}", req_id, str(exc)[:200])
            raise self.retry(exc=exc)

    @celery_app.task(
        name="app.tasks.embedding_tasks.embed_and_store_test_case",
        bind=True,
        max_retries=3,
        default_retry_delay=5,
    )
    def embed_and_store_test_case(self, case_id: str, case_json: dict, metadata: dict):
        """异步计算嵌入并写入 Milvus（测试用例）。"""
        try:
            from app.db.milvus_store import vector_store
            vector_store.add_test_case(case_id, case_json, metadata=metadata)
            logger.info("[Celery] 嵌入+Milvus写入成功: case_id={}", case_id)
        except Exception as exc:
            logger.error("[Celery] 嵌入+Milvus写入失败: case_id={}, error={}", case_id, str(exc)[:200])
            raise self.retry(exc=exc)
```

#### D3. 归档端点改为异步双写

**文件**：`backend/app/api/requirements.py`

修改 `archive_requirement`（L1468-1482），将同步 `vector_store.add_requirement` 改为 Celery 异步任务：

```python
# 修改前（L1468-1482）:
try:
    from app.db.milvus_store import vector_store
    await asyncio.to_thread(
        vector_store.add_requirement,
        req_id=record.id,
        srr_json=record.srr_json or {},
        metadata=milvus_meta,
    )
    kb_written = True
except Exception as e:
    kb_error = str(e)[:300]
    logger.warning("...", ...)

# 修改后:
try:
    from app.tasks.celery_app import celery_app
    if celery_app is not None:
        # 异步双写：PG 已 commit，Celery 异步写 Milvus
        celery_app.send_task(
            "app.tasks.embedding_tasks.embed_and_store_requirement",
            args=[req_id, record.srr_json or {}, milvus_meta],
            queue="embedding",
        )
        kb_written = True  # 任务已派发
        logger.info("[API/requirements] 异步写入任务已派发: req_id={}", req_id)
    else:
        # Celery 不可用 → 降级为同步写入
        from app.db.milvus_store import vector_store
        await asyncio.to_thread(
            vector_store.add_requirement,
            req_id=record.id,
            srr_json=record.srr_json or {},
            metadata=milvus_meta,
        )
        kb_written = True
except Exception as e:
    kb_error = str(e)[:300]
    logger.warning("[API/requirements] 知识库写入失败（不阻断）: req_id={}, error={}", req_id, kb_error[:200])
```

同样修改 `knowledge_qa.py` 的 `kqa_archive` 端点（L588-601, L627-640）。

#### D4. docker-compose.yml 新增 Celery Worker

**文件**：`docker-compose.yml`

在 backend 服务后新增：
```yaml
# ==========================================================================
# Celery Worker（异步嵌入计算 + 知识库双写）
# ==========================================================================
celery_worker:
  container_name: ai-test-gen-celery
  build:
    context: .
    dockerfile: backend/Dockerfile
    args:
      BASE_IMAGE: ai-test-gen-base:latest
  volumes:
    - ./data:/app/data
    - ./logs:/app/logs
    - ./backend:/app
    - ./models/bge-large-zh-v1.5:/models/bge-large-zh-v1.5:ro
    - ./models/bge-reranker-base:/models/bge-reranker-base:ro
  env_file:
    - ./backend/.env
  environment:
    - DATABASE_URL=postgresql+asyncpg://aitestgen:aitestgen123@postgres:5432/ai_test_gen
    - VECTOR_STORE=milvus
    - MILVUS_HOST=milvus
    - MILVUS_PORT=19530
    - EMBEDDING_MODEL_NAME=/models/bge-large-zh-v1.5
    - REDIS_HOST=redis
    - REDIS_PORT=6379
    - CELERY_BROKER_URL=redis://redis:6379/1
    - CELERY_RESULT_BACKEND=redis://redis:6379/2
  command: celery -A app.tasks.celery_app.celery_app worker --loglevel=info --concurrency=2 --queues=embedding
  depends_on:
    redis:
      condition: service_healthy
    postgres:
      condition: service_healthy
  networks:
    - ai-test-gen-net
  restart: unless-stopped
```

---

### E. Phase 7：降级策略简化（代码+注释）

#### E1. knowledge_qa.py 简化降级

**文件**：`backend/app/api/knowledge_qa.py`

修改 `_stream_qa_answer`（L264-275），删除诊断信息注入逻辑：

```python
# 修改前（L264-275）:
if kb_status and not kb_status.get("search_ok", True):
    error_msg = kb_status.get("error", "未知错误")
    prompt += (
        f"\n\n⚠️ 诊断信息：知识库检索失败，原因：{error_msg}。"
        f"请在回答中告知用户知识库当前不可用，建议检查 Milvus 服务状态。"
    )
elif kb_status and kb_status.get("total", 0) == 0:
    prompt += (
        "\n\n⚠️ 诊断信息：知识库中暂无已归档的需求或测试用例。"
        "请在回答中告知用户当前知识库为空，建议先归档需求后再进行问答。"
    )

# 修改后: 删除上述诊断注入，改为在 kqa_ask 端点中提前拦截
```

修改 `kqa_ask` 端点（L354-392），KB 检索失败时提前返回错误：

```python
# 在构建 kb_status 后新增：
if kb_status["search_ok"] is False and kb_status["total"] == 0:
    # KB 完全不可用 → 不调用 LLM，直接返回错误
    logger.warning("[API/knowledge-qa] KB 不可用，跳过 LLM 调用: errors={}", search_errors)
    return ApiResponse(
        success=False,
        message="知识库检索暂时不可用，请稍后重试",
        data={"kb_status": kb_status},
    )
# KB 可用但 0 结果 → 正常调用 LLM（空上下文）
```

同时 `_stream_qa_answer` 签名中 `kb_status` 参数保留但不再用于 Prompt 注入，仅用于 done 事件返回。

#### E2. 更新代码注释

**文件**：`backend/app/db/hybrid_search.py`

更新模块 docstring 中的降级策略说明（L8-12），使其与文档一致：
```python
"""降级策略（docs/02 §2.6 简化版）：
- KB 是增强项，不是阻断项
- 向量检索失败 → 仅用 BM25（检索质量优化，非系统降级）
- BM25 检索失败 → 仅用向量（同上）
- 两者都失败 → 返回空列表（上层静默跳过 KB，正常生成，人工澄清兜底）
- 永远不因 KB 不可用而阻断生成流程或返回 503
"""
```

**文件**：`backend/app/agents/clarify_agent.py`

在 `_query_kb_for_answers` 的 KB 查询 except 块（L798-800）补充注释：
```python
except Exception as e:
    # 降级策略：KB 不可用 → 静默跳过 → 继续生成澄清问题 → 人工澄清兜底
    # 不影响生成流程，不注入诊断信息到 Prompt
    logger.warning("[ClarifyAgent] KB 查询失败: q={!r} error={}", q.get("question", "")[:80], str(e)[:200])
    matched = []
```

---

### F. Phase 8：架构文档更新

#### F1. docs/02-knowledge-base.md §2.6 重写

将"5 级降级"替换为"简化降级策略"：

```markdown
#### 降级策略（简化版）

**设计原则**：KB 是增强项，不是阻断项。KB 不可用时静默跳过，正常生成，人工澄清兜底。

\`\`\`
KB 查询（向量 + BM25 混合检索）
    │
    ├─ 成功 → 返回检索结果 → 增强 Prompt → 生成
    │
    └─ 失败（向量+BM25 均失败）
         │
         └─ 静默跳过 → 返回空列表 → 生成无 KB 增强
                                        │
                                        └─ 人工澄清兜底（QualityGate + ClarifyLoopGuard 3 轮）
\`\`\`

**各模块降级行为**：

| 模块 | KB 失败时行为 | 兜底机制 |
|------|-------------|---------|
| ClarifyAgent | `matched=[]` → 不补全答案 → 生成澄清问题 | 人工澄清 3 轮 |
| retrieve_node | `few_shot_context` 为空 → GenAgent 无 few-shot 生成 | ReviewAgent + 人工审核 |
| GenAgent | 空 few-shot → 正常生成 | ReviewAgent + 人工审核 |
| knowledge_qa | KB 完全不可用 → 不调用 LLM → 返回错误响应 | 用户稍后重试 |

**已废弃的多级降级**（旧版 L0-L4 方案已删除）：
- ❌ L1 PG 备用降级 — PG BM25 已在 hybrid_searcher 内部作为并列检索源
- ❌ L2 Redis 只读缓存降级 — Redis 是性能优化，非降级路径
- ❌ L3 静态规则降级 — 不存在规则引擎
- ❌ L4 拒绝服务 503 — KB 不可用永不阻断生成
```

#### F2. docs/02 §3.1 补充实现细节

在 §3.1 混合检索架构图的"Re-rank"框后补充实际实现说明：

```markdown
**实际实现**（`backend/app/db/hybrid_search.py`）：

1. **向量检索**：Milvus HNSW（ef=64），返回 Top-K×2 结果
2. **BM25 检索**：PostgreSQL tsvector + zhparser 中文分词 + GIN 索引
   - SQL: `WHERE search_vec @@ plainto_tsquery('chinese', :q)`
   - 排序: `ts_rank(search_vec, query) DESC`
3. **RRF 融合**：k=60，`score(d) = Σ 1/(k + rank_i(d))`
4. **Cross-Encoder 重排**：bge-reranker-base（FlagEmbedding），对 RRF Top-K×2 重排为 Top-N
5. **多级缓存**：L1 Redis（TTL 24h）+ L2 LRU（进程内 1000 条）

**内部降级**（检索质量优化，非系统降级）：
- 向量失败 → 仅用 BM25
- BM25 失败 → 仅用向量
- 两者都失败 → 返回空（上层静默跳过）
```

#### F3. docs/02 §9.4 更新为实际实现

将 §9.4 多级缓存的设计描述更新为实际实现（`backend/app/db/kb_cache.py`）。

#### F4. docs/02 §9.7 更新为 Celery 实际实现

将 §9.7 异步双写的示例代码更新为实际实现（`backend/app/tasks/celery_app.py` + `embedding_tasks.py`），包含：
- PG 同步写入 → `celery_app.send_task` 派发异步任务
- Celery Worker 异步计算 Embedding → 写入 Milvus
- 失败重试 3 次（`max_retries=3, default_retry_delay=5`）
- Celery 不可用时降级为同步写入（`celery_app is None` 分支）

#### F5. docs/02 §9.7 表格更新

```markdown
| 方案 | MVP | 生产环境 | 一致性保证 |
|------|-----|---------|-----------|
| **同步双写**（Phase 1-2 已实现） | ✅ 推荐 | ❌ 延迟高 | PG 失败回滚 Vector DB |
| **异步双写**（Phase 6 已实现） | ❌ 复杂 | ✅ 推荐 | PG 先成功 → Celery 异步 Embedding → 失败重试 3 次 → 降级同步 |
```

#### F6. docs/00 和 docs/05 同步更新

- `docs/00-architecture-overview.md`：技术栈更新为 "PostgreSQL + pgvector + zhparser + Milvus + Redis + Celery"
- `docs/05-technical-selection.md`：§4 向量数据库（Milvus HNSW 已实现）、§11 容量规划（Redis + Celery 已实现）

---

## 五、假设与决策

### 5.1 关键决策

| # | 决策点 | 选择 | 理由 |
|---|-------|------|------|
| D1 | ClarifyAgent BM25 集成方式 | 方案 A：临时创建 PG session | 保持 BM25 能力，ClarifyAgent 是 KB 查询主要消费方 |
| D2 | retrieve_node 是否集成 hybrid_searcher | 暂不集成 | hybrid_searcher 仅支持 requirements；retrieve_node 还检索 test_cases/patterns；已有 try/except 降级 |
| D3 | knowledge_qa KB 完全不可用时的行为 | 返回错误响应，不调用 LLM | 避免空上下文幻觉；节省 LLM 调用；用户体验更好（明确错误 vs 误导性回答） |
| D4 | knowledge_qa KB 可用但 0 结果时的行为 | 正常调用 LLM（空上下文） | LLM 会回答"未找到相关信息"，符合用户预期 |
| D5 | 异步双写范围 | 归档端点 + kqa_archive | 这两个是主要写入路径；删除操作保持同步（低频，需即时一致） |
| D6 | Celery 不可用时 | 降级为同步写入 | 保证系统可用性，celery_app=None 时走 asyncio.to_thread |
| D7 | test_cases BM25 集成 | 暂不集成 | hybrid_searcher 仅实现 search_requirements；test_cases 的 tsvector 已建好，后续扩展 |
| D8 | bge-reranker-base 模型挂载 | docker-compose 挂载 /models/bge-reranker-base | 与 bge-large-zh-v1.5 一致的挂载方式 |

### 5.2 假设

- 假设 `./models/bge-reranker-base/` 目录已存在或将在部署时下载
- 假设 Redis 7-alpine 镜像可用
- 假设 Celery 5.4.0 与 Redis 7 兼容（已在 requirements.txt 锁定）
- 假设 zhparser 扩展在 pgvector/pgvector:pg16 镜像中可通过 `postgresql-16-zhparser` 安装

---

## 六、验证步骤

### 6.1 代码验证

1. **Phase 3 集成验证**：
   - `grep -rn "hybrid_searcher" backend/app/` — 确认 ClarifyAgent 和 knowledge_qa.py 已引用
   - `grep -n "RERANKER_ENABLED" backend/app/config.py` — 确认配置项存在
   - 启动后端，触发 ClarifyAgent KB 查询，日志应显示 `[HybridSearch]` 前缀

2. **Phase 4 重排器验证**：
   - `python -c "from app.db.reranker import reranker; print(reranker)"` — 无 ImportError
   - 触发 KB 查询（结果 >5 条），日志应显示重排执行

3. **Phase 5 缓存验证**：
   - `python -c "from app.db.kb_cache import kb_cache; print(kb_cache)"` — 无 ImportError
   - 同一查询执行 2 次，第 2 次日志应显示 `缓存命中`

4. **Phase 6 异步双写验证**：
   - `python -c "from app.tasks.celery_app import celery_app; print(celery_app)"` — 无 ImportError
   - 归档需求后，日志应显示 `异步写入任务已派发`，Celery Worker 日志显示 `嵌入+Milvus写入成功`

5. **Phase 7 降级验证**：
   - 停止 Milvus 服务，触发知识问答 → 应返回错误响应（不调用 LLM）
   - 停止 Milvus 服务，触发生成流水线 → 应正常生成（无 few-shot），日志显示 KB 查询失败但不阻断

### 6.2 文档验证

- `docs/02-knowledge-base.md` §2.6 不再包含 L0-L4 五级降级
- `docs/02-knowledge-base.md` §3.1 包含 hybrid_searcher 实际实现说明
- `docs/02-knowledge-base.md` §9.4 包含 kb_cache.py 实际实现
- `docs/02-knowledge-base.md` §9.7 包含 Celery 实际实现

### 6.3 Docker 验证

- `docker compose up -d` 启动所有服务（含 redis + celery_worker）
- `docker compose ps` 确认 6 个服务健康：frontend / backend / postgres / redis / milvus / celery_worker

---

## 七、实施顺序

1. **A. Phase 3 收尾**（config.py + ClarifyAgent + knowledge_qa 集成）
2. **B. Phase 4 重排器**（reranker.py + docker-compose 模型挂载）
3. **C. Phase 5 缓存**（kb_cache.py + Redis 服务）
4. **D. Phase 6 异步双写**（celery_app.py + embedding_tasks.py + 归档端点改造 + celery_worker 服务）
5. **E. Phase 7 降级简化**（knowledge_qa 提前拦截 + 注释更新）
6. **F. Phase 8 文档更新**（docs/02 §2.6/§3.1/§9.4/§9.7 + docs/00 + docs/05）

每个 Phase 完成后执行对应的验证步骤。
