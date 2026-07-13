# 知识库产品隔离 / 归档时间管理 / 模块过滤与多选问答 — 实施计划

> 本计划基于 Phase 1 代码探索，针对用户提出的四项诉求：
> 1. 验证知识库查询已按产品隔离（不同产品之间不互相影响）
> 2. 知识库管理页面可查询所有已归档需求与用例，支持删除与修改归档时间，删除时双库（PG + Milvus）同步清理
> 3. 支持按所属模块查询与问答（更精准定位内容）
> 4. 知识库问答支持多选模块，针对专门模块进行问答

---

## 一、当前状态分析（Phase 1 探索结论）

### 1.1 产品隔离 — 部分实现，有 2 个漏洞

| 位置 | 文件:行号 | 状态 |
|---|---|---|
| `/knowledge/entries` 列表 | [knowledge.py](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge.py) L252-253, L286-288 | ✅ 已按 product_id 过滤 |
| `/knowledge-qa/ask` 需求检索 | [knowledge_qa.py](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge_qa.py) L352-354 | ✅ 已传 product_id 给 hybrid_searcher |
| `/knowledge-qa/ask` 用例检索 | [knowledge_qa.py](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge_qa.py) L365 | ❌ `search_similar_cases(question, top_k=5)` **未传 product_id** → 跨产品用例泄漏 |
| `/knowledge-qa/archive` | [knowledge_qa.py](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge_qa.py) L598, L636 | ❌ 硬编码 `"product_id": "PROD-DEFAULT"` → 归档内容关联到错误产品 |
| `milvus_store.search_similar_cases` | [milvus_store.py](file:///d:/git_down/ai-test-gen/backend/app/db/milvus_store.py) L556-578 | ✅ 已支持 product_id 参数（仅未在 /ask 中传入） |

### 1.2 KB 管理页删除 — 双库清理已实现

[delete_kb_entry](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge.py#L321) 已正确实现：
- requirement: 恢复 `active` 状态 + `delete_requirement_from_kb` 清向量库（按用户选择，"移出知识库"语义）
- test_case: PG 硬删除 + `delete_test_case_from_kb` 清向量库
- 返回 `db_deleted` / `kb_deleted` 分离状态，前端可精确展示

### 1.3 归档时间 — 无真实字段，需新增

| 位置 | 现状 |
|---|---|
| [Requirement 模型](file:///d:/git_down/ai-test-gen/backend/app/db/models.py#L25) | ❌ 无 `archived_at` 真实列 |
| [TestCase 模型](file:///d:/git_down/ai-test-gen/backend/app/db/models.py#L73) | ❌ 无 `archived_at` 真实列 |
| [knowledge.py L277](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge.py#L277) | 从 `updated_at` 派生（修改任何字段都会污染归档时间） |
| [knowledge.py L43](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge.py#L43) | `_build_requirement_kb_meta` 也从 `updated_at` 派生 |
| [requirements.py L1473-1478](file:///d:/git_down/ai-test-gen/backend/app/api/requirements.py#L1473) | archive_requirement 写入 Milvus 时 archived_at = now()，**未持久化到 PG** |

**问题**：当前显示的归档时间会随任何字段修改（如改 go_live_date）而变化，不真实。

### 1.4 模块过滤 — 检索层已支持，接口层未使用

| 位置 | 状态 |
|---|---|
| [hybrid_searcher.search_requirements](file:///d:/git_down/ai-test-gen/backend/app/db/hybrid_search.py#L36) | ✅ 已支持 modules 参数（L40） |
| `/knowledge-qa/ask` | ❌ 未传 modules |
| `/knowledge/entries` | ❌ 无 modules 筛选参数 |
| [milvus_store.search_similar_cases](file:///d:/git_down/ai-test-gen/backend/app/db/milvus_store.py#L556) | ❌ 不支持 modules 参数（需内存过滤） |

### 1.5 多选模块问答 — 完全缺失

| 位置 | 现状 |
|---|---|
| [KbQAAskRequest](file:///d:/git_down/ai-test-gen/backend/app/models/api_schemas.py#L236) | ❌ 仅 question + product_id，无 modules |
| [KnowledgeQA.tsx L104](file:///d:/git_down/ai-test-gen/frontend/src/pages/KnowledgeQA.tsx#L104) | ❌ body 仅 `{ question, product_id }`，无 modules |
| [client.ts askStream](file:///d:/git_down/ai-test-gen/frontend/src/api/client.ts#L325) | ❌ 不传 modules |

---

## 二、拟议变更

### 变更组 A：新增 `archived_at` 真实字段（后端）

**文件 1**: [backend/app/db/models.py](file:///d:/git_down/ai-test-gen/backend/app/db/models.py)

**变更内容**：
- `Requirement` 模型（L60 `go_live_date` 之后）新增：
  ```python
  # 归档时间：归档时设置，用户可手动修改。区别于 updated_at（任何字段修改都会刷新）。
  archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
  ```
- `TestCase` 模型（L101 `updated_at` 之后）同样新增 `archived_at` 字段（支持用例归档时间编辑）

**Why**: 当前 archived_at 从 updated_at 派生，任何字段修改都会污染归档时间，导致用户编辑 go_live_date 后归档时间显示错误。新增真实字段可独立维护。

**文件 2**: [backend/app/db/database.py](file:///d:/git_down/ai-test-gen/backend/app/db/database.py)（或 alembic 迁移路径，依据现有约定）

**变更内容**：在 `init_db` 中检测列是否存在，不存在则 `ALTER TABLE` 添加：
```sql
ALTER TABLE requirements ADD COLUMN archived_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE test_cases ADD COLUMN archived_at TIMESTAMP WITH TIME ZONE;
```

**How**: 沿用项目已有的"启动时迁移"模式（参考 `archived_at` 现有派生逻辑说明）。若已使用 Alembic，则生成迁移脚本 `add_archived_at_columns.py`。需在探索 database.py 后确认具体路径。

---

### 变更组 B：归档端点持久化真实 archived_at（后端）

**文件**: [backend/app/api/requirements.py](file:///d:/git_down/ai-test-gen/backend/app/api/requirements.py#L1462)

**变更内容**：在 `archive_requirement` 中（L1462-1466 之间），将：
```python
record.status = "archived"
record.go_live_date = parsed_go_live
```
改为：
```python
record.status = "archived"
record.go_live_date = parsed_go_live
record.archived_at = datetime.now(timezone.utc)  # 新增：持久化真实归档时间
```

L1473-1478 的 `milvus_meta["archived_at"]` 改为 `record.archived_at.isoformat()`，保证 PG 与 Milvus 一致。

**Why**: 当前归档时间只写入 Milvus 元数据一次，未持久化到 PG，导致后续无法修改。

---

### 变更组 C：新增"修改归档时间"端点（后端）

**文件**: [backend/app/api/knowledge.py](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge.py)

**变更内容**：在 `delete_kb_entry` 之后新增 `PATCH /entries/{entry_id}/archive-time`，参考 [update_go_live_date](file:///d:/git_down/ai-test-gen/backend/app/api/requirements.py#L1538) 模式：

```python
@router.patch("/entries/{entry_id}/archive-time", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def update_archive_time(
    entry_id: str,
    body: UpdateArchiveTimeRequest,
    entry_type: str = Query(..., description="条目类型 requirement / test_case"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """修改已归档条目的归档时间。

    支持需求与测试用例两种类型。需求归档时间同时同步到 Milvus 向量库元数据。
    """
    model = Requirement if entry_type == "requirement" else TestCase
    stmt = select(model).where(model.id == entry_id)
    record = (await session.execute(stmt)).scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail=f"{entry_type} {entry_id} 不存在")

    # 解析时间（沿用 update_go_live_date 的两级解析模式）
    try:
        parsed = datetime.fromisoformat(body.archive_time)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(body.archive_time + "T00:00:00")
        except ValueError:
            raise HTTPException(status_code=400, detail="时间格式不正确")

    old = record.archived_at
    record.archived_at = parsed
    await session.commit()

    # 同步 Milvus 元数据（仅需求有向量库元数据）
    milvus_updated = False
    milvus_error = ""
    if entry_type == "requirement":
        try:
            await asyncio.to_thread(
                vector_store.update_requirement_metadata,
                entry_id,
                {"archived_at": parsed.isoformat()},
            )
            milvus_updated = True
        except Exception as e:
            milvus_error = str(e)[:300]

    return ApiResponse(success=True, message="归档时间已更新", data={
        "entry_id": entry_id, "archive_time": parsed.isoformat(),
        "milvus_updated": milvus_updated, "milvus_error": milvus_error,
    })
```

**文件**: [backend/app/models/api_schemas.py](file:///d:/git_down/ai-test-gen/backend/app/models/api_schemas.py)

**变更内容**：新增请求 schema：
```python
class UpdateArchiveTimeRequest(BaseModel):
    """修改归档时间请求。"""
    archive_time: str = Field(..., description="归档时间，格式 YYYY-MM-DDTHH:MM 或 YYYY-MM-DD")
```

**Why**: 用户明确要求"修改归档时间"，需求与用例都需要支持。需求修改时同步 Milvus 元数据保证检索排序一致。

---

### 变更组 D：修复 /ask 用例检索的产品隔离漏洞（后端）

**文件**: [backend/app/api/knowledge_qa.py](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge_qa.py#L365)

**变更内容**：将：
```python
case_results = await asyncio.to_thread(vector_store.search_similar_cases, question, top_k=5)
```
改为：
```python
case_results = await asyncio.to_thread(
    vector_store.search_similar_cases,
    question, top_k=5, product_id=product_id, modules=modules,
)
```
其中 `modules` 来自变更组 F 中的请求参数。

**Why**: 当前用例检索跨产品，违反产品隔离原则。`search_similar_cases` 已支持 product_id 参数（仅未传入）。

---

### 变更组 E：修复 /archive 硬编码 product_id（后端）

**文件**: [backend/app/api/knowledge_qa.py](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge_qa.py#L598-L636)

**变更内容**：将 L598、L636 中：
```python
archive_meta = {"title": title, "type": "knowledge_qa", "qa_id": req.id, "product_id": "PROD-DEFAULT"}
```
改为（从 KnowledgeQA 记录中获取真实 product_id）：
```python
# 从问答记录获取真实 product_id（由提问时传入）
real_product_id = record.product_id or "PROD-DEFAULT"
archive_meta = {"title": title, "type": "knowledge_qa", "qa_id": req.id, "product_id": real_product_id}
```

**Why**: 硬编码 PROD-DEFAULT 导致所有问答归档内容关联到不存在的默认产品，破坏产品隔离。需要：

**前置变更**: `KnowledgeQA` 模型需存储 product_id（如果当前没有）。需在探索 models.py 中 KnowledgeQA 定义后确认。

**文件**: [backend/app/db/models.py](file:///d:/git_down/ai-test-gen/backend/app/db/models.py)（KnowledgeQA 模型）

如果 KnowledgeQA 已有 product_id 字段则直接用；若无，需新增字段：
```python
product_id: Mapped[str] = mapped_column(String(64), default="PROD-DEFAULT", index=True)
```

**文件**: [backend/app/api/knowledge_qa.py](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge_qa.py) `/ask` 端点（创建 KnowledgeQA 记录处）

需在创建 KnowledgeQA 记录时保存 `product_id=req.product_id`。

---

### 变更组 F：/ask 与 /entries 支持 modules 参数（后端）

**文件 1**: [backend/app/models/api_schemas.py](file:///d:/git_down/ai-test-gen/backend/app/models/api_schemas.py#L236)

**变更内容**：`KbQAAskRequest` 新增 modules 字段：
```python
class KbQAAskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=10000)
    product_id: str | None = Field(default=None, max_length=64)
    modules: list[str] | None = Field(default=None, description="按模块过滤（多选，可选）")
```

**文件 2**: [backend/app/api/knowledge_qa.py](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge_qa.py#L352)

**变更内容**：在 `/ask` 端点（kqa_ask_stream）中：
```python
req_results = await hybrid_searcher.search_requirements(
    query=question, product_id=product_id, modules=req.modules, top_k=5,
    status="archived", session=session,
)
```

**文件 3**: [backend/app/api/knowledge.py](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge.py#L205)

**变更内容**：`list_kb_entries` 新增 `modules` 查询参数：
```python
modules: str | None = Query(default=None, description="按模块筛选（逗号分隔，如 'auth,login'）"),
```
查询逻辑（针对 requirement 和 test_case 都过滤）：
```python
if modules:
    module_list = [m.strip() for m in modules.split(",") if m.strip()]
    # PostgreSQL JSON 数组包含查询（使用 cast + @>）
    from sqlalchemy import cast, Text
    # 或采用 Python 端过滤（参考 milvus_store search_similar_requirements 的内存过滤模式）
    # 优先使用 PG JSON @> 操作符
```

实际实现时建议使用 `Requirement.modules.op('@>')(cast(module_list, JSON))` 或回退到内存过滤（与 hybrid_search 一致）。

**Why**: 用户要求按模块查询和问答，提升精准度。

---

### 变更组 G：milvus_store.search_similar_cases 支持 modules（后端）

**文件**: [backend/app/db/milvus_store.py](file:///d:/git_down/ai-test-gen/backend/app/db/milvus_store.py#L556)

**变更内容**：参考 [search_similar_requirements](file:///d:/git_down/ai-test-gen/backend/app/db/milvus_store.py) 的 modules 内存过滤模式（fetch_k 放大后按集合交集筛选），新增 `modules: list[str] | None = None` 参数：
```python
def search_similar_cases(self, query: str, top_k: int = 5,
                         case_type: str | None = None,
                         req_id: str | None = None,
                         product_id: str | None = None,
                         modules: list[str] | None = None) -> list[dict]:
    self._ensure_connected()
    fetch_k = top_k * 3 if modules else top_k  # 放大以便过滤后仍有足够结果
    conditions = []
    if case_type: ...
    if req_id: ...
    if product_id: ...
    where_filter = " && ".join(conditions) if conditions else None
    results = self._search("test_cases", query, top_k=fetch_k, where_filter=where_filter)

    if modules:
        module_set = set(modules)
        filtered = []
        for r in results:
            meta = r.get("metadata", {})
            if isinstance(meta, str):
                import json
                meta = json.loads(meta)
            case_modules = meta.get("modules", "")
            case_module_set = {m.strip() for m in case_modules.split(",") if m.strip()} if case_modules else set()
            if case_module_set & module_set:
                filtered.append(r)
        return filtered[:top_k]
    return results
```

**Why**: 让用例检索也支持模块过滤，与需求检索对齐。

---

### 变更组 H：前端 — KnowledgeBase.tsx 增加模块筛选与归档时间编辑

**文件**: [frontend/src/api/client.ts](file:///d:/git_down/ai-test-gen/frontend/src/api/client.ts#L300)

**变更内容**：
1. `listEntries` 新增 modules 参数：
```typescript
listEntries: (productId?: string, type?: string, filters?: {
  search?: string; vectorized?: string; modules?: string[]
}) => {
  const params = new URLSearchParams()
  if (productId) params.set('product_id', productId)
  if (type) params.set('type', type)
  if (filters?.search) params.set('search', filters.search)
  if (filters?.vectorized) params.set('vectorized', filters.vectorized)
  if (filters?.modules?.length) params.set('modules', filters.modules.join(','))
  return request<{ items: KbEntry[]; total: number }>(`/knowledge/entries?${params}`)
},
```

2. 新增 `updateArchiveTime` 方法：
```typescript
updateArchiveTime: (entryId: string, entryType: string, archiveTime: string) =>
  request<{ entry_id: string; archive_time: string; milvus_updated?: boolean; milvus_error?: string }>(
    `/knowledge/entries/${entryId}/archive-time?entry_type=${entryType}`,
    { method: 'PATCH', body: JSON.stringify({ archive_time: archiveTime }) },
  ),
```

**文件**: [frontend/src/pages/KnowledgeBase.tsx](file:///d:/git_down/ai-test-gen/frontend/src/pages/KnowledgeBase.tsx)

**变更内容**：
1. 新增模块筛选下拉（多选）：
   - 通过 `products.listModules(selectedProductId)` 获取模块列表
   - 新增 `filterModules: string[]` state
   - `loadEntries` 时传入 `modules: filterModules`
   - UI：在筛选区域增加多选 chips（参考 KnowledgeQA.tsx 的多选模式）

2. 新增归档时间编辑入口：
   - 在每行操作列增加"归档时间"按钮（icon Clock）
   - 复用 `editingEntry` 模式，新增 `editArchiveTime` state
   - 复用现有 datetime-local input 与 `isoToDatetimeLocal` / `datetimeLocalToISO` 工具函数
   - 保存时调用 `knowledge.updateArchiveTime(entry.id, entry.type, isoDate)`
   - 更新本地列表 `archived_at` 字段

3. KbEntry 类型保持 `archived_at?: string`（[types/index.ts](file:///d:/git_down/ai-test-gen/frontend/src/types/index.ts#L278) L286 已存在）

**Why**: 让用户在 KB 管理页可以按模块筛选、修改归档时间。

---

### 变更组 I：前端 — KnowledgeQA.tsx 增加多选模块

**文件**: [frontend/src/pages/KnowledgeQA.tsx](file:///d:/git_down/ai-test-gen/frontend/src/pages/KnowledgeQA.tsx)

**变更内容**：
1. 新增 `selectedModules: string[]` state
2. 加载模块列表（与 KnowledgeBase.tsx 一致，通过 `products.listModules(selectedProductId)`）
3. 在头部下方增加模块多选 chips UI：
   - 多选 chips（点击切换选中/未选中状态）
   - "全部"按钮一键清空
4. `handleSubmit` 时将 `selectedModules` 加入请求体：
```typescript
body: JSON.stringify({
  question: q,
  product_id: selectedProductId || undefined,
  modules: selectedModules.length > 0 ? selectedModules : undefined,
}),
```
5. 追问（`knowledgeQA.chat`）端点不修改（追问基于已有 record_id，不重新检索 KB）

**文件**: [frontend/src/api/client.ts](file:///d:/git_down/ai-test-gen/frontend/src/api/client.ts#L325)

**变更内容**：`askStream` 方法签名增加 modules 参数：
```typescript
askStream: (
  question: string,
  onChunk: ..., onSources: ..., onDone: ..., onError: ...,
  options?: { product_id?: string; modules?: string[] }
) => {
  // body 改为:
  body: JSON.stringify({
    question,
    product_id: options?.product_id,
    modules: options?.modules?.length ? options.modules : undefined,
  }),
}
```

**Why**: 用户明确要求"知识库问答可以多选模块来对专门的模块进行问答"。

---

## 三、假设与决策

### 假设
1. **数据库迁移路径**：项目当前未使用 Alembic（待 database.py 探索确认），采用"启动时检测列并 ALTER TABLE"模式。如已使用 Alembic，则改为生成迁移脚本。
2. **KnowledgeQA 模型已有 product_id 字段**：探索时需确认；若无则按变更组 E 新增。
3. **PG JSON 数组过滤**：优先使用 `@>` 操作符；若 ORM 兼容性有问题则回退到内存过滤。
4. **modules 字段格式**：在 PG 中为 JSON 数组（如 `["auth", "login"]`）；在 Milvus 元数据中为逗号分隔字符串（如 `"auth,login"`）。

### 决策（用户已确认）
- **删除语义**：保持现状 — 需求从知识库移出（恢复 `active`），不硬删除 PG 记录；仅清向量库。测试用例仍硬删除。**无需变更**，[delete_kb_entry](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge.py#L321) 已正确实现。
- **多选模块交互**：chips 多选，不限定数量上限。

---

## 四、实施顺序与依赖

按以下顺序执行（依赖关系从上到下）：

1. **变更组 A**（models.py + database.py）→ 新增 `archived_at` 列
2. **变更组 B**（requirements.py）→ archive 端点持久化
3. **变更组 C**（knowledge.py + api_schemas.py）→ 新增修改归档时间端点
4. **变更组 G**（milvus_store.py）→ search_similar_cases 支持 modules
5. **变更组 E**（models.py KnowledgeQA + knowledge_qa.py /ask 创建处 + /archive）→ 修复 product_id
6. **变更组 F**（api_schemas.py + knowledge_qa.py /ask + knowledge.py /entries）→ modules 参数
7. **变更组 D**（knowledge_qa.py L365）→ 用例检索传 product_id + modules
8. **变更组 H**（client.ts + KnowledgeBase.tsx）→ 前端 KB 管理
9. **变更组 I**（KnowledgeQA.tsx + client.ts）→ 前端问答多选模块

---

## 五、验证步骤

### 5.1 后端单元验证（Python 脚本）

1. **数据库迁移**：启动后端，检查 `requirements.archived_at` 和 `test_cases.archived_at` 列存在
2. **归档持久化**：归档一个需求，检查 PG `archived_at` 字段被填充，且不等于 `updated_at`
3. **修改归档时间**：调用 `PATCH /knowledge/entries/{id}/archive-time`，检查 PG + Milvus 同步更新
4. **产品隔离**：
   - 创建两个产品 P1、P2，各归档一个需求
   - 在 P1 下调用 `/ask`，检查 sources 不含 P2 的需求
   - 在 P1 下调用 `/ask`，检查 case_results 不含 P2 的用例
5. **模块筛选**：归档需求时设置 modules=["auth"]，调用 `/entries?modules=auth` 应能查到，`/entries?modules=login` 查不到
6. **多选模块问答**：调用 `/ask` 时传 `modules=["auth","login"]`，验证返回的 sources 都属于这两个模块之一

### 5.2 前端验证（手动）

1. **KB 管理页**：
   - 模块筛选下拉能正确显示当前产品的模块列表
   - 多选模块后列表立即刷新
   - 修改归档时间弹窗能正确显示当前时间，保存后列表更新
   - 删除按钮仍正常工作（双库清理）
2. **知识问答页**：
   - 模块多选 chips 能正确显示和切换
   - 提问时附带 modules，sources 仅包含所选模块的条目
   - 追问（chat）不传 modules（基于已有记录）

### 5.3 边界情况

- modules 为空数组时 → 等同于不筛选（返回所有）
- modules 含不存在的模块名时 → 返回空结果（不报错）
- 修改归档时间为未来时间 → 允许（不做时间合法性校验，与 go_live_date 一致）
- product_id 为空时（未选择产品）→ 仍按现有行为（不筛选产品），但前端应强制要求选择产品

---

## 六、风险与回滚

### 风险
1. **数据库 ALTER TABLE 失败**：需在 init_db 中包裹 try/except，失败时仅记录日志不阻断启动（与现有迁移模式一致）
2. **PG JSON @> 操作符兼容性**：若 SQLite 测试环境不支持，需回退到内存过滤
3. **前端 modules 多选 UI 在产品未选择时的行为**：应禁用模块筛选（无产品则无模块列表）

### 回滚
- 每个变更组独立提交，可单独 revert
- 新增字段为 nullable，回滚后不影响现有数据
- 新增端点独立，回滚不影响现有功能

---

## 七、待 Phase 1 探索时确认的细节

执行阶段需在以下文件中确认：
1. [database.py](file:///d:/git_down/ai-test-gen/backend/app/db/database.py) 的迁移模式（启动时 ALTER 还是 Alembic）
2. [models.py KnowledgeQA](file:///d:/git_down/ai-test-gen/backend/app/db/models.py#L356) 是否已有 product_id 字段
3. [products.py listModules](file:///d:/git_down/ai-test-gen/backend/app/api/products.py) 端点返回的 Module 结构（用于前端 chips 显示）

确认后按上述计划执行，不再追加额外变更。
