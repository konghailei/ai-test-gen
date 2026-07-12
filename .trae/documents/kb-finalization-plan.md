# 知识库降级策略再分析与最终完善方案

> **范围**：在已完成 hybrid_searcher 集成、reranker、kb_cache、Celery 基础设施的基础上，完成剩余的归档端点异步双写改造、降级策略简化收尾、SQLite 残留清理、以及架构文档同步更新。

---

## 一、摘要

用户核心诉求（再分析）：
1. **降级策略简化** — "我不想影响内容生成结果，所以一旦知识库查询不到就无所谓直接生成后面有人工澄清作为兜底"。KB 是增强项不是阻断项：KB 不可用 → 静默跳过 → 正常生成 → 人工澄清兜底。**删除**一切多级 PG/Redis/规则回退和 503 拒绝服务设计。
2. **SQLite 彻底清除** — 已使用 PG，清理残留的 SQLite 引用（pyproject.toml 依赖、README、前端标签、Dockerfile/.gitignore 注释、过期测试脚本）。
3. **Milvus >10w 策略** — 已完成（HNSW M=16/efConstruction=200 + 模块分区 + 5 Collection），本方案仅做文档同步。
4. **其他最优策略** — 完成 Celery 异步双写（基础设施已就绪，归档端点待改造）。
5. **同时修改架构文档和代码** — docs/02 §2.6/§3.1/§9 全面更新。

---

## 二、当前状态分析（已验证）

### ✅ 已完成

| 模块 | 状态 | 验证依据 |
|------|------|---------|
| SQLite 代码逻辑移除 | ✅ | `config.py` 无 `SQLITE_PATH`，`database.py` PG-only，`prompt_store.py` 用 psycopg2 |
| Milvus HNSW + 分区 + 5 Collection | ✅ | `milvus_store.py:187-195` HNSW M=16/efConstruction=200；`MODULE_PARTITIONS` 6 分区；`COLLECTION_CONFIGS` 5 集合 |
| IVF_FLAT → HNSW 自动迁移 | ✅ | `milvus_store.py:_migrate_index_if_needed` |
| hybrid_searcher（RRF k=60 + BM25 zhparser + 内部降级） | ✅ | `hybrid_search.py` 完整实现，docstring 已为简化版 |
| ClarifyAgent 集成 hybrid_searcher | ✅ | `clarify_agent.py:790-812` 走 hybrid_searcher，session=None 时临时创建 PG session 供 BM25 |
| ClarifyAgent 降级注释 | ✅ | `clarify_agent.py:808-812` 注释"KB 不可用 → 静默跳过 → 人工澄清兜底" |
| knowledge_qa 需求检索走 hybrid_searcher | ✅ | `knowledge_qa.py:358-370` |
| config.py RERANKER_* + CELERY_* 配置 | ✅ | 已添加 |
| reranker.py（bge-reranker-base Cross-Encoder） | ✅ | `backend/app/db/reranker.py` 存在 |
| kb_cache.py（L1 Redis + L2 LRU 1000） | ✅ | `backend/app/db/kb_cache.py` 存在 |
| Celery 基础设施（celery_app.py + embedding_tasks.py） | ✅ | `backend/app/tasks/` 三个文件存在 |
| docker-compose Redis 服务 + 模型挂载 + 环境变量 | ✅ | `docker-compose.yml` redis 服务、bge-reranker-base 挂载、REDIS_*/CELERY_* 环境变量 |
| PG zhparser + tsvector + GIN 索引 | ✅ | `docker/postgres.Dockerfile`、`docker/init-zhparser.sql`、`database.py` Step 5 |

### ❌ 待完成

| # | 模块 | 问题 | 影响 |
|---|------|------|------|
| 1 | SQLite 残留 | `pyproject.toml:46` 仍有 `aiosqlite>=0.20.0` | 依赖冗余 |
| 2 | SQLite 残留 | `README.md:261,448` 提及 SQLite 可切换 | 文档误导 |
| 3 | SQLite 残留 | `frontend/src/pages/AdminKnowledgeBase.tsx:36` SQLite 标签兜底 | 前端显示错误 |
| 4 | SQLite 残留 | `frontend/src/pages/RequirementDetail.tsx:599` `sqlite_enrich` 图标 | 前端残留 |
| 5 | SQLite 残留 | `Dockerfile:43`、`.gitignore:38` 注释提及 SQLite | 注释过时 |
| 6 | SQLite 残留 | `verify_chroma_remove.py`、`test_data/debug_vector_db.py` 过期脚本 | 代码冗余 |
| 7 | Celery 异步双写 | `requirements.py:1468-1482` 归档端点仍同步 `vector_store.add_requirement` | 归档延迟高 |
| 8 | Celery 异步双写 | `knowledge_qa.py:591-604,630-643` kqa_archive 仍同步双写 | 归档延迟高 |
| 9 | Celery 异步双写 | `docker-compose.yml` 无 celery_worker 服务 | Celery 任务无法执行 |
| 10 | 降级简化 | `knowledge_qa.py:264-275` KB 不可用时向 Prompt 注入诊断信息 | 污染 LLM 输出 |
| 11 | 文档 | `docs/02-knowledge-base.md §2.6` 仍为 L0-L4 五级降级（含 503） | 与实际架构冲突 |
| 12 | 文档 | `docs/02 §3.1/§9.4/§9.7` 未更新为实际实现 | 文档滞后 |
| 13 | 文档 | `docs/00`、`docs/05` 技术栈未含 Redis/Celery | 文档滞后 |

---

## 三、降级策略再分析（用户核心诉求）

### 3.1 用户原则

> "我不想影响内容生成结果，所以一旦知识库查询不到就无所谓直接生成后面有人工澄清作为兜底"

核心：**KB 是增强项，不是阻断项**。KB 不可用时生成质量可能略降（无 few-shot 参考），但**绝不能因为 KB 降级逻辑本身污染生成结果**（如向 Prompt 注入"知识库不可用"的诊断提示）。

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
                                        └─ 人工澄清兜底（QualityGate + ClarifyLoopGuard 3 轮）
```

### 3.3 删除的旧降级机制（L0-L4 五级）

| 旧等级 | 旧描述 | 删除理由 |
|--------|--------|---------|
| L0 全能力 | Milvus + PG + Redis 全开 | 正常运行态，非降级 |
| L1 PG 备用 | Milvus 宕机 → PG 全文检索替代 | PG BM25 已在 `hybrid_searcher` 内部作为**并列检索源**，不是"降级路径" |
| L2 Redis 只读 | 缓存命中返回旧结果 | Redis 是**性能优化**，非降级路径；缓存未命中走正常检索 |
| L3 静态规则 | 关键词匹配简化检索 | **不存在规则引擎**，删除虚构设计 |
| L4 拒绝服务 503 | 返回"知识库不可用" | KB 不可用**永不阻断生成**；knowledge_qa 返回错误响应而非 503 |

### 3.4 保留的内部降级（检索质量优化，非系统降级）

`hybrid_search.py` 内部的双路降级是**检索质量优化**，保留：
- 向量检索失败 → 仅用 BM25（`hybrid_search.py:70-71`）
- BM25 检索失败 → 仅用向量（`hybrid_search.py:80-81`）
- 两者都失败 → 返回空列表（`hybrid_search.py:84-86`）→ 上层静默跳过

### 3.5 各模块降级行为

| 模块 | KB 失败时行为 | 影响生成质量 | 兜底机制 |
|------|-------------|-------------|---------|
| **ClarifyAgent** | `matched=[]` → 不补全答案 → 生成澄清问题 | ❌ 不影响 | 人工澄清 3 轮 |
| **retrieve_node** | `few_shot_context` 各字段为空 → GenAgent 无 few-shot 生成 | ⚠️ 略降（无历史参考） | ReviewAgent + 人工审核 |
| **GenAgent** | 空 few-shot → 正常生成（Prompt few-shot 段为空） | ⚠️ 略降 | ReviewAgent + 人工审核 |
| **knowledge_qa** | 见 §3.6 决策 | — | — |

### 3.6 知识问答降级决策

知识问答（`knowledge_qa.py`）与生成流水线不同：它是**纯检索增强问答**，无人工澄清兜底。系统 Prompt 要求"只能基于知识库上下文回答"。

**当前行为（需修改）**：KB 不可用时向 Prompt 注入"请在回答中告知用户知识库当前不可用"→ LLM 生成告警式回答 → **污染输出**

**新行为**：
- KB 检索成功但 0 结果 → 正常调用 LLM（空上下文，LLM 回答"知识库中未找到相关信息"）
- KB 检索失败（search_ok=False 且 total=0）→ **不调用 LLM**，直接返回错误响应：
  ```json
  {"success": false, "message": "知识库检索暂时不可用，请稍后重试", "data": {"kb_status": {...}}}
  ```
  理由：避免空上下文幻觉；节省 LLM 调用；用户体验更好（明确错误 vs 误导性回答）

---

## 四、待完成工作

### A. SQLite 残留清理

#### A1. 移除 pyproject.toml 中的 aiosqlite 依赖

**文件**：`pyproject.toml`

删除第 46 行 `"aiosqlite>=0.20.0",`（dependencies 数组中）。

#### A2. 更新 README.md

**文件**：`README.md`

- 第 261 行：将"关系库可在 PostgreSQL 与 SQLite 之间切换（`DATABASE_URL` 留空即 SQLite）"改为"关系库使用 PostgreSQL 16（`DATABASE_URL` 必填）"
- 第 448 行：将"数据库引擎（PG/SQLite 自适应）"改为"数据库引擎（PostgreSQL）"

#### A3. 更新前端 SQLite 标签

**文件**：`frontend/src/pages/AdminKnowledgeBase.tsx`

第 36 行：将 `const dbLabel = healthStatus?.db_type === 'postgresql' ? 'PostgreSQL 16' : 'SQLite'` 改为 `const dbLabel = 'PostgreSQL 16'`（或直接使用 `healthStatus?.db_type` 显示，移除 SQLite 兜底）。

**文件**：`frontend/src/pages/RequirementDetail.tsx`

第 599 行：将 `sqlite_enrich: Database,` 改为 `pg_enrich: Database,`（或移除该图标别名，统一用 `Database`）。需同步更新该图标在文件中的所有引用。

#### A4. 更新 Dockerfile 和 .gitignore 注释

**文件**：`Dockerfile`

第 43 行：将"创建数据目录（SQLite / 向量库元数据）"改为"创建数据目录（日志 / 向量库元数据）"。

**文件**：`.gitignore`

第 38 行：将"SQLite 数据库 + ChromaDB 向量库 + Fernet 加密密钥"改为"PostgreSQL 数据 + Milvus 向量库 + Fernet 加密密钥"。

#### A5. 删除过期测试脚本

**文件**：删除 `verify_chroma_remove.py`（ChromaDB 已移除，脚本过期）
**文件**：删除 `test_data/debug_vector_db.py`（引用 SQLite，过期调试脚本）

**注意**：`backend/.env.example:15` 和 `backend/app/db/database.py:4` 的注释已说明"无 SQLite"/"SQLite 已弃用移除"，保留作为历史说明，不修改。根目录 `BUG_REPORT_*.md`、`REQ_FLOW_ALIGNMENT_REPORT.md` 等历史快照文档不修改（历史记录）。

---

### B. Celery 异步双写完成

#### B1. requirements.py 归档端点改为异步

**文件**：`backend/app/api/requirements.py`

修改 `archive_requirement` 端点 L1468-1482，将同步 `vector_store.add_requirement` 改为 Celery `send_task` + 同步兜底：

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
    logger.info("[API/requirements] 已写入知识库: ...", ...)
except Exception as e:
    kb_error = str(e)[:300]
    logger.warning("[API/requirements] 知识库写入失败（不阻断）: ...", ...)

# 修改后:
try:
    from app.tasks.celery_app import celery_app
    if celery_app is not None:
        celery_app.send_task(
            "app.tasks.embedding_tasks.embed_and_store_requirement",
            args=[record.id, record.srr_json or {}, milvus_meta],
            queue="embedding",
        )
        kb_written = True
        logger.info("[API/requirements] 异步写入任务已派发: req_id={}", record.id)
    else:
        # Celery 未安装/未配置 → 降级为同步写入
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
    logger.warning("[API/requirements] 知识库写入失败（不阻断）: req_id={}, error={}", record.id, kb_error[:200])
```

#### B2. knowledge_qa.py 归档端点改为异步

**文件**：`backend/app/api/knowledge_qa.py`

修改 `kqa_archive` 端点两处同步双写：

**第一处（L591-604，需求归档）**：
```python
# 修改前:
try:
    await asyncio.to_thread(
        vector_store.add_requirement,
        archived_id, new_req.srr_json,
        metadata={...},
    )
except Exception as e:
    logger.warning("[API/knowledge-qa] 向量库归档失败: {}", str(e)[:100])

# 修改后:
try:
    from app.tasks.celery_app import celery_app
    archive_meta = {"title": title, "type": "knowledge_qa", "qa_id": req.id, "product_id": "PROD-DEFAULT"}
    if celery_app is not None:
        celery_app.send_task(
            "app.tasks.embedding_tasks.embed_and_store_requirement",
            args=[archived_id, new_req.srr_json, archive_meta],
            queue="embedding",
        )
    else:
        await asyncio.to_thread(vector_store.add_requirement, archived_id, new_req.srr_json, metadata=archive_meta)
except Exception as e:
    logger.warning("[API/knowledge-qa] 向量库归档失败: {}", str(e)[:100])
```

**第二处（L630-643，用例归档）**：同样改为 `celery_app.send_task("app.tasks.embedding_tasks.embed_and_store_test_case", ...)` + 同步兜底。

#### B3. docker-compose.yml 新增 celery_worker 服务

**文件**：`docker-compose.yml`

在 backend 服务后、postgres 服务前新增：

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
      - EMBEDDING_MODEL_TYPE=local
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

### C. 降级策略简化收尾

#### C1. knowledge_qa.py 删除诊断信息注入

**文件**：`backend/app/api/knowledge_qa.py`

**删除 L264-275** 的诊断信息注入逻辑：

```python
# 删除以下代码（L264-275）:
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
```

`_stream_qa_answer` 的 `kb_status` 参数保留，仅用于 done 事件返回诊断信息（前端展示），不再注入 Prompt。

#### C2. knowledge_qa.py KB 完全不可用时提前返回

**文件**：`backend/app/api/knowledge_qa.py`

在 `kqa_ask` 端点构建 `kb_status` 后（L388 之后）、`return EventSourceResponse` 之前，新增提前拦截：

```python
# KB 完全不可用（两路检索都失败且无结果）→ 不调用 LLM，直接返回错误
if not kb_status["search_ok"] and kb_status["total"] == 0:
    logger.warning("[API/knowledge-qa] KB 完全不可用，跳过 LLM 调用: errors={}", search_errors)
    return ApiResponse(
        success=False,
        message="知识库检索暂时不可用，请稍后重试",
        data={"kb_status": kb_status},
    )
# KB 可用但 0 结果 → 正常调用 LLM（空上下文，LLM 会回答"未找到相关信息"）
```

**注意**：`ApiResponse` 需要从 `app.models.api_schemas` 导入（若未导入）。该端点返回类型为 `EventSourceResponse`，提前返回 `ApiResponse` 需确认 FastAPI 路由是否支持 Union 返回。若不支持，改为 `raise HTTPException(status_code=503, detail="知识库检索暂时不可用，请稍后重试")` 或返回 `JSONResponse`。

**实现时需验证**：`kqa_ask` 的返回类型注解。若为 `EventSourceResponse`，提前返回需用 `JSONResponse`（从 `fastapi.responses` 导入）。

#### C3. 确认 hybrid_search.py 和 clarify_agent.py 注释（已完成，无需修改）

- `hybrid_search.py:8-12` docstring 已为简化版 ✅
- `clarify_agent.py:808-812` 注释已为简化版 ✅

---

### D. 架构文档更新

#### D1. docs/02-knowledge-base.md §2.6 重写

将"降级策略详细"（L1294 附近，含 L0-L4 五级）替换为 §3.2 的简化版降级策略内容：

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

**内部降级**（`hybrid_searcher` 内部，检索质量优化，非系统降级）：
- 向量检索失败 → 仅用 BM25
- BM25 检索失败 → 仅用向量
- 两者都失败 → 返回空（上层静默跳过）

**已废弃的多级降级**（旧版 L0-L4 方案已删除）：
- ❌ L1 PG 备用降级 — PG BM25 已在 hybrid_searcher 内部作为并列检索源
- ❌ L2 Redis 只读缓存降级 — Redis 是性能优化，非降级路径
- ❌ L3 静态规则降级 — 不存在规则引擎
- ❌ L4 拒绝服务 503 — KB 不可用永不阻断生成
```

#### D2. docs/02 §3.1 补充实现细节

在 §3.1 混合检索架构图后补充实际实现说明：

```markdown
**实际实现**（`backend/app/db/hybrid_search.py`）：

1. **向量检索**：Milvus HNSW（M=16, efConstruction=200, ef=64），返回 Top-K×2
2. **BM25 检索**：PostgreSQL tsvector + zhparser 中文分词 + GIN 索引
   - SQL: `WHERE search_vec @@ plainto_tsquery('chinese', :q)`
   - 排序: `ts_rank(search_vec, query) DESC`
3. **RRF 融合**：k=60，`score(d) = Σ 1/(k + rank_i(d))`
4. **Cross-Encoder 重排**：bge-reranker-base（FlagEmbedding），对 RRF Top-K×2 重排为 Top-N
5. **多级缓存**：L1 Redis（TTL 24h）+ L2 LRU（进程内 1000 条），key = MD5(query|product_id|coll_type)
```

#### D3. docs/02 §9.4 更新为 kb_cache.py 实际实现

将 §9.4 多级缓存的设计描述更新为 `backend/app/db/kb_cache.py` 的实际实现：L1 Redis（TTL 24h）+ L2 LRU（OrderedDict, 1000 条），缓存 key 设计，读写流程，Redis 不可用时降级为 LRU-only。

#### D4. docs/02 §9.7 更新为 Celery 实际实现

将 §9.7 异步双写的示例代码更新为实际实现：
- `backend/app/tasks/celery_app.py` — Celery 应用，broker/backend 用 Redis，JSON 序列化，`task_acks_late=True`，queue routing
- `backend/app/tasks/embedding_tasks.py` — 两个任务 `embed_and_store_requirement` / `embed_and_store_test_case`，`max_retries=3, default_retry_delay=5`
- 归档端点：PG commit → `celery_app.send_task` 派发 → Celery Worker 异步 Embedding + Milvus 写入
- Celery 不可用（`celery_app is None`）→ 降级为同步 `asyncio.to_thread`

§9.7 表格更新：

| 方案 | MVP | 生产环境 | 一致性保证 |
|------|-----|---------|-----------|
| **同步双写** | ✅ 推荐 | ❌ 延迟高 | PG 失败回滚 Vector DB |
| **异步双写**（已实现） | ❌ 复杂 | ✅ 推荐 | PG 先成功 → Celery 异步 Embedding → 失败重试 3 次 → Celery 不可用时降级同步 |

#### D5. docs/00-architecture-overview.md 技术栈更新

更新技术栈描述为："PostgreSQL 16 + pgvector + zhparser + Milvus（HNSW + 分区）+ Redis（缓存 + Celery broker）+ Celery（异步双写）"。移除任何 SQLite 提及。

#### D6. docs/05-technical-selection.md 更新

- §4 向量数据库：标注 Milvus HNSW（M=16, efConstruction=200）+ 模块分区已实现
- §关系数据库：标注 PostgreSQL 16 为唯一关系数据库（SQLite 已移除）
- 依赖清单：新增 FlagEmbedding、redis、celery[redis]；移除 aiosqlite

---

## 五、假设与决策

### 关键决策

| # | 决策点 | 选择 | 理由 |
|---|-------|------|------|
| D1 | knowledge_qa KB 完全不可用时的行为 | 返回错误响应，不调用 LLM | 避免空上下文幻觉；节省 LLM 调用；无人工澄清兜底；用户体验更好（明确错误 vs 误导性回答） |
| D2 | knowledge_qa KB 可用但 0 结果时的行为 | 正常调用 LLM（空上下文） | LLM 会回答"未找到相关信息"，符合用户预期；KB 正常但无数据不是错误 |
| D3 | 异步双写范围 | 归档端点（requirements.py + knowledge_qa.py） | 这两个是主要写入路径；删除操作保持同步（低频，需即时一致） |
| D4 | Celery 不可用时 | 降级为同步写入 | `celery_app is None` 时走 `asyncio.to_thread`，保证系统可用性 |
| D5 | retrieve_node 是否集成 hybrid_searcher | 暂不集成 | hybrid_searcher 仅支持 requirements；retrieve_node 还检索 test_cases/patterns；已有 try/except 降级 |
| D6 | test_cases BM25 集成 | 暂不集成 | hybrid_searcher 仅实现 search_requirements；test_cases 的 tsvector 已建好，后续扩展 |
| D7 | 过期测试脚本处理 | 删除 verify_chroma_remove.py 和 test_data/debug_vector_db.py | ChromaDB/SQLite 已移除，脚本无引用价值 |
| D8 | 历史快照文档（BUG_REPORT.md 等） | 不修改 | 历史记录，修改会失真 |

### 假设

- 假设 `./models/bge-reranker-base/` 目录已存在或将在部署时下载
- 假设 Celery 5.4.0 与 Redis 7 兼容（requirements.txt 已锁定）
- 假设 `kqa_ask` 端点可用 `JSONResponse` 提前返回错误（FastAPI 支持）

---

## 六、验证步骤

### 代码验证

1. **SQLite 清理验证**：
   - `grep -ri "sqlite" backend/` 仅返回 `database.py:4` 和 `.env.example:15` 的注释（说明"已弃用"）
   - `grep -ri "aiosqlite" pyproject.toml` 返回 0 结果
   - `grep -ri "sqlite" frontend/src/` 返回 0 结果

2. **Celery 异步双写验证**：
   - `grep -n "celery_app" backend/app/api/requirements.py` 确认 send_task 调用
   - `grep -n "celery_app" backend/app/api/knowledge_qa.py` 确认 send_task 调用
   - `docker compose up -d` 启动后 `docker compose ps` 确认 celery_worker 服务健康
   - 归档需求后，backend 日志显示"异步写入任务已派发"，celery_worker 日志显示"嵌入+Milvus写入成功"

3. **降级简化验证**：
   - `grep -n "诊断信息" backend/app/api/knowledge_qa.py` 返回 0 结果（诊断注入已删除）
   - 停止 Milvus 服务，触发知识问答 → 返回错误响应（不调用 LLM）
   - 停止 Milvus 服务，触发生成流水线 → 正常生成（无 few-shot），日志显示 KB 查询失败但不阻断

### 文档验证

- `docs/02-knowledge-base.md` §2.6 不再包含 L0-L4 五级降级和 503
- `docs/02` §3.1 包含 hybrid_searcher 实际实现说明
- `docs/02` §9.4 包含 kb_cache.py 实际实现
- `docs/02` §9.7 包含 Celery 实际实现
- `docs/00` 和 `docs/05` 技术栈含 Redis/Celery，无 SQLite

### Docker 验证

- `docker compose up -d` 启动所有服务（含 redis + celery_worker）
- `docker compose ps` 确认 7 个服务健康：frontend / backend / celery_worker / postgres / redis / milvus / etcd / minio

---

## 七、实施顺序

1. **A. SQLite 残留清理**（pyproject.toml + README + 前端 + Dockerfile + .gitignore + 删除过期脚本）
2. **B. Celery 异步双写**（requirements.py 归档端点 + knowledge_qa.py 归档端点 + docker-compose celery_worker）
3. **C. 降级策略简化收尾**（knowledge_qa.py 删除诊断注入 + KB 不可用提前返回）
4. **D. 架构文档更新**（docs/02 §2.6/§3.1/§9.4/§9.7 + docs/00 + docs/05）

每个步骤完成后执行对应的验证步骤。
