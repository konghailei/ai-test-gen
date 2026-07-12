# 项目增强与修复计划

## 概述

本计划覆盖六个方面：Bug/隐患修复、README 整合、用户手册、归档上线日期 + KB 去重、项目/模块标签页 + 检索、项目版本号。

---

## 一、当前状态分析

### 1.1 Bug/隐患审计结果

经全项目扫描，发现以下关键问题：

| 严重度 | 文件 | 问题 |
|--------|------|------|
| HIGH | `knowledge_qa.py:L131-142, L173-182` | SQL 拼接存在注入风险 |
| HIGH | `KnowledgeQA.tsx:L45-49` | `dangerouslySetInnerHTML` 渲染未净化的 API 返回内容（XSS） |
| HIGH | `RequirementDetail.tsx:L52-57` | 同上，`dangerouslySetInnerHTML` XSS 风险 |
| HIGH | `config.py:L204-205` | `DEBUG=true` 默认开启，生产环境暴露内部错误 |
| MEDIUM | `knowledge.py:L63,181,282,302,350` | 多处 bare `except Exception` 静默吞错 |
| MEDIUM | `products.py:L177,247-254` | 删除产品/项目时向量库删除失败静默吞错 |
| MEDIUM | `auth.py:L50-55` | JWT secret 在 dev 模式使用静态值 |
| MEDIUM | `config.py:L202-203` | `FRONTEND_URL` 硬编码 localhost |
| LOW | `requirements.py:L970,1077` | 最终 SRR 生成失败降级时未充分提示用户 |

### 1.2 README 与 DEPLOY 现状

- `README.md`（193 行）：项目概述 + 一键部署 + 文档导航 + 架构流程图 + 技术栈 + 实施路径
- `DEPLOY.md`（268 行）：环境要求 + 首次部署步骤 + 日常开发 + 架构说明 + 数据持久化 + 运维 + 配置参考 + 故障排查
- 两者有部分重叠（一键部署），需合并为单一文档

### 1.3 归档与 KB 检索现状

- `Requirement` 模型（`models.py:L29-67`）：**无** `go_live_date` 列；有 `version`(int)、`supersedes`、`superseded_by`、`change_type`
- 归档端点（`requirements.py:L1375-1461`）：仅接受 `req_id`，无 body；写入 Milvus 的 metadata 含 `product_id/modules/priority/status/archived_at`，**无** `go_live_date`、**无** `project_version`
- `search_similar_requirements`（`milvus_store.py:L298-342`）：无去重逻辑，无版本优先级
- `_query_kb_for_answers`（`clarify_agent.py:L790-870`）：处理多个匹配结果，但**不按上线日期排序或去重**
- `knowledge_qa.py /ask`：同样无去重

### 1.4 项目/模块管理现状

- `ProjectsModules.tsx`（284 行）：左右并排网格布局，**非标签页**；**无检索**；`AddForm` 仅接受 name + description，**无版本号**
- 路由：`/projects-modules`（`App.tsx:L92`）
- 后端 `list_projects`（`products.py:L192-202`）：返回 id/product_id/name/description/created_at，**不返回 version**
- 后端 `create_project`（`products.py:L205-226`）：`CreateProjectRequest`（`products.py:L62-66`）仅 name/description/id，**无 version**
- `Project` 模型（`models.py`）：**已有** `version` 列（String(32), default="1.0"），但前后端均未使用
- 前端 `Project` 类型（`types/index.ts:L233-239`）：**无** `version` 字段
- 前端 API（`client.ts:L460-462`）：`createProject(pid, name, desc)` 不传 version

### 1.5 Layout 现状

- `Layout.tsx`：顶部栏右侧有用户头像下拉菜单（L155-216），**无用户手册入口**
- 用户菜单项：管理后台 / 系统设置 / 修改密码 / 退出登录

---

## 二、计划变更

### 任务 1：Bug/隐患修复（按严重度从高到低）

**文件：`backend/app/api/knowledge_qa.py`**
- 问题：L131-142、L173-182 存在 SQL 字符串拼接
- 修复：改用 SQLAlchemy 参数化查询或 `session.execute(text(sql), {"param": value})`，杜绝注入

**文件：`frontend/src/pages/KnowledgeQA.tsx`**
- 问题：L45-49 使用 `dangerouslySetInnerHTML` 渲染 API 返回的 markdown 内容
- 修复：替换为安全的 markdown 渲染。检查是否已有 markdown 解析库（如 `react-markdown`）；若有则使用，若无则将 `dangerouslySetInnerHTML` 改为先 `DOMPurify.sanitize()` 净化再渲染

**文件：`frontend/src/pages/RequirementDetail.tsx`**
- 问题：L52-57 同上 `dangerouslySetInnerHTML` XSS 风险
- 修复：同上，用 `DOMPurify.sanitize()` 净化或改用 `react-markdown`

**文件：`backend/app/config.py`**
- 问题：L204-205 `DEBUG=true` 默认开启
- 修复：默认值改为 `False`，仅通过环境变量 `DEBUG=true` 显式开启

**文件：`backend/app/api/knowledge.py` + `products.py`**
- 问题：多处 bare `except Exception` 静默吞错
- 修复：在 `except` 块中添加 `logger.warning(...)` 日志记录（部分已有，补全缺失的），确保错误不丢失但也不阻断主流程

### 任务 2：README 整合

**文件：`README.md`**
- 将 `DEPLOY.md` 的全部内容整合进 README，结构调整为：
  1. 项目概述（保留现有）
  2. 🚀 快速开始（简化版，保留现有一键部署）
  3. 📦 详细部署（整合 DEPLOY.md 的环境要求、首次部署、日常开发）
  4. 🏗️ 架构说明（整合 DEPLOY.md 的架构图、双 Compose 设计、数据持久化）
  5. 🔧 运维与配置（整合 DEPLOY.md 的日常运维、配置参考、故障排查）
  6. 📖 文档导航（保留现有）
  7. 技术栈 + 实施路径（保留现有）
  8. 文件结构（保留现有，更新）

**文件：`DEPLOY.md`**
- 整合完成后删除此文件，在 README 顶部注明"部署文档已整合到本文件"

### 任务 3：用户手册

**文件：`frontend/src/components/Layout.tsx`**
- 在顶部栏右侧、用户头像按钮**之前**，新增一个"用户手册"按钮（图标 `BookOpen` from lucide-react）
- 点击后打开一个全屏模态弹窗 `UserManualModal`

**新文件：`frontend/src/components/UserManualModal.tsx`**
- 内容结构（中文）：
  - **需求管理**：上传需求 → 自动解析 SRR → KB 检索补全 → 人工澄清 → 归档
  - **测试用例生成**：选择已澄清需求 → AI 生成用例 → 审核编辑
  - **知识库**：归档需求入库 → 知识问答 → 状态指示（已归档/已向量化）
  - **项目与模块**：项目（含版本号）管理、模块管理、依赖图谱
  - **管理后台**：产品管理、用户管理、角色管理、系统设置、提示词管理
  - **KB 检索规则说明**：上线日期靠后的需求优先；版本号标注来源
- 弹窗有关闭按钮，点击遮罩关闭，`Esc` 键关闭

### 任务 4：归档上线日期 + KB 去重

**4a. 数据库模型变更**

**文件：`backend/app/db/models.py`**
- `Requirement` 类新增列：`go_live_date: Mapped[date | None] = mapped_column(Date, nullable=True)`
- 导入 `from datetime import date` 和 `from sqlalchemy import Date`

**4b. 归档端点变更**

**文件：`backend/app/api/requirements.py`**
- `archive_requirement` 端点新增请求体参数 `go_live_date`（可选，ISO 日期字符串 `YYYY-MM-DD`）
- 新建 Pydantic 模型 `ArchiveRequest(BaseModel)`：`go_live_date: str | None = None`
- 将 `go_live_date` 写入 `record.go_live_date`（解析为 `date` 对象）
- 将 `go_live_date` 写入 Milvus metadata
- 查询项目的 `version`，写入 Milvus metadata 的 `project_version` 字段（通过 `record.project_id` 查 `Project` 表）

**4c. 前端归档 UI 变更**

**文件：`frontend/src/api/client.ts`**
- `archive` 方法签名改为：`archive: (id: string, goLiveDate?: string) => request(...)`
- body 传 `{ go_live_date: goLiveDate }`

**文件：`frontend/src/pages/RequirementDetail.tsx`**
- `handleArchive` 改为弹出日期选择模态弹窗（非 `confirm()`）
- 弹窗含：日期输入框（`<input type="date">`，必填）+ 说明文字"上线日期用于知识库检索时区分最新需求，同一功能以靠后日期为准" + 确认/取消按钮
- 确认后调用 `reqApi.archive(id, goLiveDate)`

**4d. KB 检索去重逻辑**

**文件：`backend/app/agents/clarify_agent.py` — `_query_kb_for_answers` 方法**
- 在处理 `matched` 结果时（L809 循环之前），按 `go_live_date` 降序排序
- 当同一模块出现多个匹配结果时，优先采纳 `go_live_date` 最新的结果
- 在 `answer_text` 中标注上线日期和项目版本：`"• REQ-xxx（v2.1, 上线: 2026-07-11, 相似度 0.55）"`
- 从 metadata 提取 `go_live_date` 和 `project_version` 字段

**文件：`backend/app/api/knowledge_qa.py` — `/ask` 端点**
- 搜索结果按 `go_live_date` 降序排序
- 在 LLM prompt 中注明每个结果的上线日期和版本，让 LLM 优先参考最新版本

**文件：`backend/app/db/milvus_store.py` — `search_similar_requirements` 方法**
- 在返回结果中包含 `go_live_date` 和 `project_version`（从 metadata 提取，已随 `_search` 返回）
- 无需改 Milvus schema，metadata 是 JSON 字段，新增字段自动包含

### 任务 5：项目/模块标签页 + 检索

**文件：`frontend/src/pages/ProjectsModules.tsx`**
- 将左右并排网格改为**标签页布局**：
  - Tab 1：「项目」— 项目列表 + 新增表单
  - Tab 2：「模块」— 模块列表 + 新增表单
- 两个 Tab 各自顶部添加**搜索框**（`<input type="text" placeholder="搜索名称或描述...">`）
- 搜索逻辑：前端过滤，按 `name` 和 `description` 模糊匹配（大小写不敏感）
- 标签页切换时保持各自搜索状态
- 列表展示为表格或卡片网格，每项显示名称、描述、创建时间（项目额外显示版本号）

### 任务 6：项目版本号

**6a. 后端变更**

**文件：`backend/app/api/products.py`**
- `CreateProjectRequest`（L62-66）新增字段：`version: str = Field(default="1.0", max_length=32, description="项目版本号")`
- `create_project`（L205-226）：创建 `Project` 记录时设置 `version=body.version`
- `list_projects`（L192-202）：响应中增加 `"version": r.version`

**6b. 前端变更**

**文件：`frontend/src/types/index.ts`**
- `Project` 接口新增 `version: string` 字段

**文件：`frontend/src/api/client.ts`**
- `createProject` 签名改为：`createProject: (pid: string, name: string, desc: string, version: string) => ...`
- body 传 `{ name, description: desc, version }`

**文件：`frontend/src/pages/ProjectsModules.tsx`**
- 项目新增表单（`AddForm`）增加版本号输入框（`<input type="text" placeholder="如 1.0, 2.1.0">`，默认值 "1.0"）
- 项目列表每项显示版本号标签（如 `v2.1.0`）
- `AddForm` 组件需区分 project 模式和 module 模式：project 模式显示版本号字段，module 模式不显示

**6c. KB 检索展示版本**

- 归档时将项目版本写入 Milvus metadata（已在任务 4b 中处理）
- KB 检索结果中展示版本信息（已在任务 4d 中处理）

---

## 三、假设与决策

1. **用户手册形式**：选择模态弹窗而非独立页面，避免新增路由，保持简洁。内容为静态中文说明，硬编码在组件中。
2. **上线日期去重策略**：按 `go_live_date` 降序排序检索结果，最新日期优先。不删除旧版本结果（保留为参考），但在 `answer_text` 中标注哪个是最新版本。LLM prompt 中注明优先参考最新版本。
3. **上线日期为必填**：归档时必须填写上线日期，确保 KB 去重逻辑有据可依。
4. **项目版本默认值**：新建项目时版本号默认 "1.0"，用户可修改。
5. **DEPLOY.md 整合后删除**：避免两份文档不同步。README 中保留"详细部署"章节引用原 DEPLOY.md 内容。
6. **XSS 修复**：优先使用 `DOMPurify`（轻量、成熟）。若项目已有 `react-markdown` 则直接使用。
7. **数据库迁移**：`go_live_date` 新增列为 nullable，无需 backfill。SQLAlchemy `create_all` 会自动创建新表，但已有表需手动 `ALTER TABLE requirements ADD COLUMN go_live_date DATE`。由于项目使用 Docker 且数据库可重建，通过重建容器 + `create_all` 即可。

---

## 四、验证步骤

### 4.1 Bug 修复验证
- [ ] `knowledge_qa.py` SQL 注入：构造含单引号的查询参数，确认不报错且无注入
- [ ] `KnowledgeQA.tsx` / `RequirementDetail.tsx` XSS：输入 `<script>alert(1)</script>` 内容，确认不执行
- [ ] `config.py` DEBUG 默认关闭：不设环境变量启动，确认 `DEBUG=False`
- [ ] 静默吞错：触发向量库删除失败，确认日志中有 warning

### 4.2 README 验证
- [ ] README 包含 DEPLOY.md 全部章节
- [ ] DEPLOY.md 已删除
- [ ] README 中命令和路径正确

### 4.3 用户手册验证
- [ ] 顶部栏右侧出现"用户手册"按钮
- [ ] 点击弹出模态，内容完整，Esc/遮罩可关闭
- [ ] 不影响现有用户菜单功能

### 4.4 上线日期 + KB 去重验证
- [ ] 归档弹窗含日期选择，必填
- [ ] 归档后 Milvus metadata 含 `go_live_date` 和 `project_version`
- [ ] 上传增量需求文档，KB 检索结果按上线日期排序，最新版本标注优先
- [ ] 知识问答结果中含版本和上线日期信息

### 4.5 标签页 + 检索验证
- [ ] 项目/模块分两个 Tab，切换正常
- [ ] 搜索框实时过滤，清空恢复全量
- [ ] 两个 Tab 的搜索状态独立

### 4.6 项目版本号验证
- [ ] 新建项目表单含版本号字段，默认 "1.0"
- [ ] 项目列表显示版本号
- [ ] 后端 `list_projects` 返回 version
- [ ] 归档需求时 Milvus metadata 含 `project_version`

### 4.7 整体回归
- [ ] `cd backend && python -m py_compile app/api/requirements.py app/api/products.py app/db/models.py app/agents/clarify_agent.py`
- [ ] `cd frontend && npx tsc --noEmit`
- [ ] Docker 重建后浏览器全功能冒烟测试
