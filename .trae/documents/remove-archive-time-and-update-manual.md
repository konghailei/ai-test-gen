# 计划：去除归档时间 + 用户手册更新

## 摘要

去除需求归档时间（archive time）的用户可编辑功能和显示，只保留上线时间（go-live time）作为需求版本排序的唯一依据。同时全面更新用户手册，使其反映最新的系统逻辑（混合检索、KB 专属 LLM、搜索筛选、Celery 异步双写等）。

## 现状分析

### 归档时间（archived_at）的使用位置

| 层 | 文件 | 用途 |
|----|------|------|
| DB | `models.py:62` | `Requirement.archived_at` 列（自动设置，保留） |
| DB | `models.py:105` | `TestCase.archived_at` 列（自动设置，保留） |
| 后端 API | `knowledge.py:33-35` | `UpdateArchiveTimeRequest` 请求模型（删除） |
| 后端 API | `knowledge.py:431-506` | PATCH `/entries/{id}/archive-time` 端点（删除） |
| 后端 API | `knowledge.py:51` | `_build_requirement_kb_meta` 中 `archived_at` 字段（删除） |
| 后端 API | `knowledge.py:44-45,297-298,329-330` | 列表响应中返回 `archived_at`（保留，只读） |
| 后端 API | `requirements.py:1466` | 归档时自动设置 `record.archived_at`（保留） |
| 后端 API | `requirements.py:1479` | Milvus 元数据含 `archived_at`（删除） |
| 前端 API | `client.ts:313-316` | `updateArchiveTime` 方法（删除） |
| 前端类型 | `types/index.ts:287` | `KbEntry.archived_at` 字段（保留，后端仍返回） |
| 前端 UI | `KnowledgeBase.tsx:126-195` | `ArchiveTimeEditModal` 组件（删除） |
| 前端 UI | `KnowledgeBase.tsx:352-355` | `handleEditArchiveTime` / `onSavedArchiveTime`（删除） |
| 前端 UI | `KnowledgeBase.tsx:554-556` | "归档于 xxx" 显示（删除） |
| 前端 UI | `KnowledgeBase.tsx:578-580` | 编辑归档时间按钮（删除） |
| 前端 UI | `KnowledgeBase.tsx:606-612` | `<ArchiveTimeEditModal>` 渲染（删除） |

### 用户手册过时内容

| 位置 | 问题 |
|------|------|
| L166 | "4 个集合" → 实际 5 个（含 req_pats） |
| L188 | "上线日期" → 应为"上线时间"（已改为 datetime） |
| L188 | "需填写上线日期" → 应为"需填写上线时间" |
| L209 | `archived_at` 在 Milvus 元数据中 → 删除 |
| L209 | `go_live_date YYYY-MM-DD` → `YYYY-MM-DDTHH:MM` |
| L303 | "上线日期" → "上线时间" |
| L314 | `archived_at` 在元数据中 → 删除 |
| L322 | "上线日期" → "上线时间" |
| L331 | "共 4 个独立集合" → "共 5 个" |
| L415 | "上线日期优先" → "上线时间优先" |
| L437 | "上线日期" → "上线时间" |
| 缺失 | BM25 + CrossEncoder 混合检索（向量+BM25+RRF+重排） |
| 缺失 | KB 专属 LLM 选择器 |
| 缺失 | KB/需求管理页面搜索筛选功能 |
| 缺失 | 修改上线时间功能 |
| 缺失 | Celery 异步双写 |
| 缺失 | 简化降级策略（KB 不可用→跳过+正常生成） |

## 修改方案

### 第一步：后端 — 删除归档时间编辑功能

**文件：`backend/app/api/knowledge.py`**

1. 删除 `UpdateArchiveTimeRequest` 类（L33-35）
2. 删除 PATCH `/entries/{entry_id}/archive-time` 端点（L431-506）
3. 从 `_build_requirement_kb_meta()` 中删除 `archived_at` 字段（L51）
4. 保留列表响应中的 `archived_at`（只读，用于内部参考）

**文件：`backend/app/api/requirements.py`**

1. 从 Milvus 元数据中删除 `archived_at`（L1479）
2. 保留 `record.archived_at = datetime.now(timezone.utc)`（DB 列保留，内部用途）

### 第二步：前端 — 删除归档时间 UI

**文件：`frontend/src/pages/KnowledgeBase.tsx`**

1. 删除 `ArchiveTimeEditModal` 组件（L126-195）
2. 删除 `editingArchiveEntry` state
3. 删除 `handleEditArchiveTime` 函数
4. 删除 `onSavedArchiveTime` 回调
5. 删除 "归档于 {formatDate(entry.archived_at)}" 显示
6. 删除编辑归档时间的日历图标按钮
7. 删除 `<ArchiveTimeEditModal>` 渲染

**文件：`frontend/src/api/client.ts`**

1. 删除 `updateArchiveTime` 方法（L313-316）

### 第三步：用户手册全面更新

**文件：`frontend/src/pages/UserManual.tsx`**

#### 3.1 系统架构总览（architecture section）
- L166: "4 个集合" → "5 个集合：requirements / test_cases / patterns / req_pats / errors"

#### 3.2 需求管理（requirements section）
- L188: "需填写上线日期" → "需填写上线时间（精确到分钟）"
- L209: 删除 `archived_at`，`go_live_date YYYY-MM-DD` → `YYYY-MM-DDTHH:MM`
- 新增 feature: "修改上线时间" — 已归档需求可随时修改上线时间，同步更新 Milvus 元数据
- 新增 feature: "搜索筛选" — 需求列表支持按状态/优先级筛选 + 标题模糊搜索

#### 3.3 增量需求（incremental section）
- 无需修改（不涉及归档时间）

#### 3.4 测试用例生成（generate section）
- 无需修改

#### 3.5 知识库（knowledge section）
- L303: "上线日期" → "上线时间"
- L314: 删除 `archived_at`
- L331: "共 4 个独立集合" → "共 5 个"
- 新增 feature: "KB 专属 LLM" — 知识库页面可配置专属 LLM 供应商，与主 LLM 独立
- 新增 feature: "搜索筛选" — KB 条目列表支持标题搜索 + 向量化状态筛选
- 新增 feature: "修改上线时间" — 每条需求可点击日历图标修改上线时间
- 修改 logic: "归档入库流程（双写）" → "归档入库流程（Celery 异步双写）"

#### 3.6 项目与模块（projects section）
- 无需修改

#### 3.7 管理后台（admin section）
- 新增 feature: "KB 专属 LLM 配置" — 管理员可为知识库问答配置独立 LLM 供应商
- 修改 logic: "提示词管理" 5 个 Agent → 补充 knowledge_qa 模块的 QA_SYSTEM_PROMPT 和 CHAT_SYSTEM_PROMPT

#### 3.8 KB 检索规则（kb-rules section）
- L415: "上线日期优先" → "上线时间优先"
- L437: "上线日期" → "上线时间"
- 修改 logic: "Embedding 编码" → 改为"混合检索"描述（向量检索 + BM25 关键词检索 + RRF 融合 + CrossEncoder 重排）
- 新增 logic: "降级策略" — KB 不可用时静默跳过，正常生成；向量/BM25 互为降级
- 修改 config: 增加 BM25 / Reranker / KB LLM 相关配置项

### 第四步：Docker 重建 + 验证

1. `docker compose up -d --build frontend backend celery_worker`
2. 验证 KB 页面不再显示归档时间编辑按钮
3. 验证上线时间编辑功能正常
4. 验证用户手册页面内容更新

## 假设与决策

1. **DB 列保留**：`archived_at` 列保留在数据库中（自动设置），只是不再暴露给用户编辑和显示。这样避免 DB 迁移，且内部仍可使用。
2. **Milvus 元数据清理**：从新归档的需求中移除 `archived_at` 元数据。已有向量的 `archived_at` 元数据不清理（无害，检索不使用）。
3. **列表 API 响应保留**：`archived_at` 仍在列表 API 响应中返回（只读），前端不显示但不报错。
4. **类型定义保留**：`KbEntry.archived_at` 类型保留，避免 TS 编译错误。
5. **用户手册结构不变**：保持 8 个章节结构，只更新内容。

## 验证步骤

1. 前端 Docker 构建通过（`tsc -b && vite build` 无错误）
2. KB 页面：归档时间编辑按钮和显示已移除
3. KB 页面：上线时间编辑功能正常工作
4. KB 页面：搜索和筛选功能正常
5. 用户手册页面：所有章节内容已更新
6. 后端 API：PATCH `/entries/{id}/archive-time` 返回 404
7. 后端 API：归档需求时 Milvus 元数据不再包含 `archived_at`
8. 8 个容器全部健康
