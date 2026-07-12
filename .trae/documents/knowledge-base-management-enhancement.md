# 知识库管理增强计划

## 摘要

用户在知识库页面删除需求时遇到报错，并希望丰富知识库的查询和操作能力：显示每个需求归档后在**向量知识库（Milvus）**和**关系数据库（PostgreSQL）**中的状态（如"已向量化"、"已归档"），并增强知识库管理 UI。同时解答 MinIO 的角色（它是 Milvus 的存储后端，并非独立的向量知识库管理器）。

本计划通过以下改动实现：
1. 后端新增向量存在性检查方法、增强条目列表 API 返回双库状态、新增诊断端点
2. 前端在知识库页面显示双库状态徽标、改进删除错误处理、在管理页面显示向量库健康诊断

---

## 当前状态分析

### 后端

**`backend/app/db/milvus_store.py`**：
- 已有方法：`count_requirements(product_id)`、`count_test_cases()`、`is_empty()`、`is_model_ready()`、`get_all_requirements()`、`delete_requirement(req_id)`
- **缺失**：检查特定 `req_id` 是否存在于向量库的方法（`requirement_exists`）
- **缺失**：获取所有 Collection 统计信息的统一方法

**`backend/app/api/knowledge.py`**：
- `GET /entries`：从 PostgreSQL 读取已归档需求 + 测试用例，返回基本字段（id/title/type/product_id/modules/priority/summary）
- **缺失**：不返回该需求是否已在 Milvus 向量化
- **缺失**：不返回 `archived_at` 时间戳
- `DELETE /entries/{entry_id}`：需求删除 = status 改为 active + Milvus 删除；用例删除 = SQLite 删除 + Milvus 删除
- 删除时 Milvus 失败被捕获为 warning（不阻断），但返回值不包含 `kb_deleted` 状态

**`backend/app/main.py`** `/api/health`：
- 已返回 `vector_db: { connected, model_ready, collections: {requirements: N, test_cases: N, ...}, error }`
- 但 `AdminKnowledgeBase.tsx` 未展示这些向量库诊断信息

**`backend/app/api/requirements.py`** 归档端点（`POST /{req_id}/archive`）：
- 设置 status="archived"，写入 Milvus，返回 `kb_written` / `kb_error`
- 但 `archived_at` 时间戳只写入 Milvus metadata，未写入 PostgreSQL `Requirement.archived_at` 列

### 前端

**`frontend/src/pages/KnowledgeBase.tsx`**（111 行）：
- 简单列表 + 删除按钮
- 删除错误处理简陋：`alert('删除失败: ' + (e.message || ''))`
- **无状态徽标**（不显示是否已向量化/已归档）

**`frontend/src/pages/AdminKnowledgeBase.tsx`**（118 行）：
- 显示统计数字（需求/用例/模式/批次数）
- 显示系统配置（数据库引擎、向量库引擎、嵌入模型）
- **未显示**向量库连接状态、Collection 计数、健康诊断
- **未解释** MinIO 角色

**`frontend/src/types/index.ts`**：
- `KbEntry`：仅 id/title/type/product_id/modules/priority/summary
- `HealthStatus`：缺少 `vector_db` 字段

### 基础设施

Docker Compose 定义服务：
- **PostgreSQL**（端口 5432）：关系数据库，存储需求/用例/模式元数据
- **Milvus**（端口 19530/9091）：向量数据库引擎，依赖 etcd + minio
- **MinIO**（端口 9001）：S3 兼容对象存储，**是 Milvus 的存储后端**（存储向量数据文件），不是独立的 KB 管理器
- **etcd**：Milvus 的协调服务

### 删除报错原因分析

经 API 测试确认删除端点返回 200 成功。用户看到的报错可能来自：
1. **权限问题**：`DELETE /entries/{entry_id}` 要求 `require_admin` 权限，普通用户调用返回 403
2. **前端错误提取**：`request()` 函数从 HTTP 响应提取错误信息的方式可能与 403/500 响应体格式不匹配
3. **Milvus 删除失败**：被后端捕获为 warning 不阻断，但前端可能因其他原因报错

---

## 拟议改动

### 改动 1：后端 — 新增 `requirement_exists` 方法

**文件**：`backend/app/db/milvus_store.py`

**位置**：在 `delete_requirement_from_kb` 方法后（约 L367）

**新增方法**：
```python
def requirement_exists(self, req_id: str) -> bool:
    """检查指定需求是否已存在于向量库。"""
    self._ensure_connected()
    collection = self._get_collection("requirements")
    results = collection.query(
        expr=f'id == "{_safe_id(req_id, "req_id")}"',
        output_fields=["id"],
        limit=1,
    )
    return len(results) > 0

def get_collection_stats(self) -> dict:
    """获取所有 Collection 的统计信息。"""
    self._ensure_connected()
    stats = {}
    for coll_key in COLLECTION_CONFIGS:
        try:
            collection = self._get_collection(coll_key)
            stats[coll_key] = {
                "num_entities": collection.num_entities,
                "description": COLLECTION_CONFIGS[coll_key]["description"],
            }
        except Exception as e:
            stats[coll_key] = {"num_entities": -1, "error": str(e)[:100]}
    return stats
```

**原因**：支持在条目列表中显示每个需求是否已向量化；提供统一的 Collection 统计方法供诊断端点使用。

---

### 改动 2：后端 — 增强 `GET /knowledge/entries` 端点

**文件**：`backend/app/api/knowledge.py`

**位置**：`list_kb_entries` 函数内（L153-230）

**改动内容**：
1. 需求条目增加 `archived_at` 字段（从 PostgreSQL `Requirement.archived_at` 读取）
2. 需求条目增加 `vectorized` 字段（批量查询 Milvus 检查是否存在）
3. 为避免 N+1 查询性能问题，先收集所有 req_id，一次性调用 `get_all_requirements` 获取已向量化的 ID 集合，然后匹配

**实现思路**：
```python
# 在返回需求条目前，批量获取已向量化的 req_id 集合
vectorized_ids: set[str] = set()
try:
    all_reqs = await asyncio.to_thread(vector_store.get_all_requirements, None, 1000)
    vectorized_ids = {r["id"] for r in all_reqs}
except Exception as e:
    logger.warning("[API/knowledge] 获取向量库需求列表失败: {}", str(e)[:100])

# 在构建需求条目时
items.append({
    "id": r.id,
    "title": title,
    "type": "requirement",
    "product_id": r.product_id,
    "modules": ",".join(r.modules or []),
    "priority": r.priority,
    "summary": summary,
    "archived_at": r.archived_at.isoformat() if r.archived_at else "",
    "vectorized": r.id in vectorized_ids,  # 新增
    "created_at": r.created_at.isoformat() if r.created_at else "",
})
```

**原因**：让前端能显示每个需求在向量库和关系库中的状态，满足用户"丰富知识查询"的核心诉求。

---

### 改动 3：后端 — 新增 `GET /knowledge/diagnostics` 端点

**文件**：`backend/app/api/knowledge.py`

**位置**：在 `delete_kb_entry` 函数后

**新增端点**（管理员权限）：
```python
@router.get("/diagnostics", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def get_kb_diagnostics() -> ApiResponse:
    """知识库诊断信息（管理员）。
    
    返回向量库连接状态、各 Collection 计数、嵌入模型状态、架构说明。
    """
    diag = {
        "vector_db": {"connected": False, "model_ready": False, "collections": {}, "error": ""},
        "architecture": {
            "relational_db": "PostgreSQL — 存储需求/用例/模式元数据与 SRR JSON",
            "vector_db": "Milvus — 向量检索引擎，存储 1024 维语义向量",
            "object_storage": "MinIO — Milvus 的存储后端，存储向量数据文件（非独立 KB 管理器）",
            "coordination": "etcd — Milvus 的协调服务",
        },
    }
    try:
        diag["vector_db"]["connected"] = True
        diag["vector_db"]["model_ready"] = await asyncio.to_thread(vector_store.is_model_ready)
        diag["vector_db"]["collections"] = await asyncio.to_thread(vector_store.get_collection_stats)
    except Exception as e:
        diag["vector_db"]["error"] = str(e)[:200]
    return ApiResponse(success=True, message="ok", data=diag)
```

**原因**：为管理页面提供详细的 KB 健康诊断，并解释 MinIO 角色。

---

### 改动 4：后端 — 改进删除端点的错误处理

**文件**：`backend/app/api/knowledge.py`

**位置**：`delete_kb_entry` 函数（L237-290）

**改动内容**：
1. 返回值增加 `db_deleted`（关系库）和 `kb_deleted`（向量库）状态
2. 捕获并返回 Milvus 删除错误详情

```python
# 需求删除部分
db_deleted = True
kb_deleted = False
kb_error = ""
try:
    await asyncio.to_thread(vector_store.delete_requirement_from_kb, entry_id)
    kb_deleted = True
except Exception as e:
    kb_error = str(e)[:300]
    logger.warning("[API/knowledge] 向量库删除失败: req_id={}, error={}", entry_id, kb_error[:200])

return ApiResponse(
    success=True,
    message=f"已从知识库删除 {entry_type} {entry_id}" + ("" if kb_deleted else "（向量库删除失败，请检查 Milvus 状态）"),
    data={
        "entry_id": entry_id,
        "deleted": True,
        "db_deleted": db_deleted,
        "kb_deleted": kb_deleted,
        "kb_error": kb_error,
    },
)
```

**原因**：让前端能显示精确的删除结果（关系库已删除但向量库失败时提示用户）。

---

### 改动 5：前端 — 更新类型定义

**文件**：`frontend/src/types/index.ts`

**改动 1 — `KbEntry` 接口（L270-278）**：
```typescript
export interface KbEntry {
  id: string
  title: string
  type: 'requirement' | 'test_case'
  product_id: string
  modules?: string
  priority?: string
  summary?: string
  archived_at?: string      // 新增：归档时间
  vectorized?: boolean      // 新增：是否已在 Milvus 向量化
  created_at?: string
}
```

**改动 2 — `HealthStatus` 接口（L202-210）**：
```typescript
export interface HealthStatus {
  version: string
  llm_provider: string
  llm_configured: boolean
  kb_empty: boolean
  cold_start_mode: boolean
  db_type?: string
  vector_store?: string
  vector_db?: {              // 新增
    connected: boolean
    model_ready: boolean
    collections: Record<string, number>
    error: string
  }
}
```

**改动 3 — 新增 `KbDiagnostics` 接口**：
```typescript
export interface KbDiagnostics {
  vector_db: {
    connected: boolean
    model_ready: boolean
    collections: Record<string, { num_entities: number; description?: string; error?: string }>
    error: string
  }
  architecture: {
    relational_db: string
    vector_db: string
    object_storage: string
    coordination: string
  }
}
```

---

### 改动 6：前端 — 新增 `knowledge.diagnostics` API 方法

**文件**：`frontend/src/api/client.ts`

**位置**：`knowledge` 对象内（L280-300）

**新增**：
```typescript
diagnostics: () => request<KbDiagnostics>('/knowledge/diagnostics'),
```

---

### 改动 7：前端 — 增强 `KnowledgeBase.tsx`

**文件**：`frontend/src/pages/KnowledgeBase.tsx`

**改动内容**：

1. **状态徽标**：在每个需求条目上显示双库状态
   - 已归档 + 已向量化：绿色"已归档" + 蓝色"已向量化"
   - 已归档 + 未向量化：绿色"已归档" + 橙色"未向量化"（警告）
   - 测试用例：显示"用例"徽标（无需双库状态，因为用例不归档）

2. **改进删除错误处理**：
   - 替换 `alert` 为更详细的错误提示
   - 显示 `kb_deleted` / `db_deleted` 状态
   - 删除部分成功时提示"关系库已删除，向量库删除失败"

3. **显示 `archived_at` 时间**：在条目摘要中显示归档时间

**UI 草图**：
```
┌─────────────────────────────────────────────────────────┐
│ 📄 登录功能优化          [已归档] [已向量化]     🗑️    │
│    PROD-xxx · 2026-07-11 归档 · 正常流程...            │
├─────────────────────────────────────────────────────────┤
│ 📄 需求A                 [已归档] [⚠️未向量化]   🗑️    │
│    PROD-xxx · 2026-07-10 归档 · 点击重新向量化          │
└─────────────────────────────────────────────────────────┘
```

---

### 改动 8：前端 — 增强 `AdminKnowledgeBase.tsx`

**文件**：`frontend/src/pages/AdminKnowledgeBase.tsx`

**改动内容**：

1. **显示向量库连接状态**：从 `/api/health` 读取 `vector_db` 字段
   - 连接状态：绿色"已连接" / 红色"未连接"
   - 嵌入模型：绿色"就绪" / 红色"未就绪"
   - 错误信息（如有）

2. **显示 Collection 计数**：
   - requirements: N 条向量
   - test_cases: N 条向量
   - patterns: N 条向量
   - errors: N 条向量

3. **新增架构说明面板**：解释 MinIO/Milvus/etcd/PostgreSQL 各自角色
   ```
   ┌─────────────────────────────────────────────────────┐
   │ 🏗️ 知识库架构                                       │
   │                                                      │
   │ PostgreSQL  — 关系数据库，存储需求/用例元数据与 SRR  │
   │ Milvus      — 向量数据库，1024 维语义向量检索        │
   │ MinIO       — 对象存储，Milvus 的存储后端            │
   │ etcd        — 协调服务，Milvus 的元数据管理          │
   │                                                      │
   │ ℹ️ MinIO 不是独立的向量知识库管理器，它是 Milvus 的  │
   │    存储后端，负责持久化向量数据文件。                 │
   └─────────────────────────────────────────────────────┘
   ```

4. **调用 `knowledge.diagnostics()`**：获取并展示详细诊断信息

---

## 假设与决策

### 假设
1. `Requirement.archived_at` 列已存在（从 models.py 探索确认存在该列）
2. `get_all_requirements(limit=1000)` 足以覆盖当前系统的需求量（冷启动阶段，需求量 <1000）
3. 用户希望状态徽标显示在普通知识库页面（非仅管理页面）
4. 删除报错主要来自权限或 Milvus 连接问题，改进错误处理能让用户看到真实原因

### 决策
1. **批量查询而非逐条查询**：为避免 N+1 性能问题，一次性获取所有已向量化的 req_id 集合，在内存中匹配
2. **诊断端点设为管理员权限**：普通用户只需看到条目状态徽标，详细诊断信息仅管理员可见
3. **不新增"重新向量化"按钮**：当前范围内不实现，保持改动聚焦；用户可通过"归档"端点重新归档（如果状态为 active）。已归档但未向量化的情况可通过先删除再重新归档处理
4. **MinIO 角色通过架构说明面板解释**：不直接管理 MinIO，仅展示其角色

---

## 验证步骤

### 后端验证
1. 启动后端服务，调用 `GET /api/knowledge/entries`，确认每个需求条目包含 `vectorized` 和 `archived_at` 字段
2. 调用 `GET /api/knowledge/diagnostics`（管理员 token），确认返回 `vector_db.collections` 含 4 个 Collection 的计数
3. 调用 `DELETE /api/knowledge/entries/{req_id}?entry_type=requirement`，确认返回 `db_deleted` 和 `kb_deleted` 字段
4. 在 Milvus 中手动删除一条需求向量，再次调用 `GET /entries`，确认该需求显示 `vectorized: false`
5. 语法检查：`python -m py_compile backend/app/db/milvus_store.py backend/app/api/knowledge.py`

### 前端验证
1. 访问知识库页面，确认每个需求条目显示"已归档"+"已向量化"徽标
2. 删除一个需求，确认显示详细的删除结果（关系库/向量库状态）
3. 模拟权限不足（普通用户删除），确认错误提示清晰
4. 访问管理知识库页面，确认显示向量库连接状态、Collection 计数、架构说明面板
5. 类型检查：`cd frontend && npx tsc --noEmit`

### 端到端验证
1. 归档一个需求 → 在知识库页面确认显示"已归档"+"已向量化"
2. 删除该需求 → 确认从列表消失，且返回 `db_deleted: true, kb_deleted: true`
3. 在管理页面确认 Collection 计数相应减少

---

## 实施顺序

1. 后端改动 1（`requirement_exists` + `get_collection_stats` 方法）
2. 后端改动 2（增强 `GET /entries` 端点）
3. 后端改动 3（新增 `GET /diagnostics` 端点）
4. 后端改动 4（改进删除错误处理）
5. 后端语法检查
6. 前端改动 5（更新类型定义）
7. 前端改动 6（新增 `diagnostics` API 方法）
8. 前端改动 7（增强 `KnowledgeBase.tsx`）
9. 前端改动 8（增强 `AdminKnowledgeBase.tsx`）
10. 前端类型检查
11. 端到端验证
