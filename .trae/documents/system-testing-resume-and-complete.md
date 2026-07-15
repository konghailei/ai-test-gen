# 系统全方位测试计划（续）— 代码审查 + 两轮 E2E + FP 影响分析

## 背景与目标

上一阶段已完成全部代码实现（步骤 1-8：漏洞修复、SRR schema、ImpactAnalysisAgent 扩展、后端端点、GenAgent 提示词、前端 API、DependencyGraph 弹窗）。上一轮测试因会话上下文丢失而中断，第一轮 E2E 数据文件未保存（`aitest_e2e_data.json` 不存在），需要从头执行。

**本次计划目标**：
1. 审查当前已修改的 10 个文件，确认无遗留漏洞
2. 验证 LLM 配置（qwen3.7-plus）连通性
3. 执行第一轮 E2E 全链路测试（上传→解析→澄清→FP→生成→归档→KB 查询）
4. 执行第二轮测试（优化需求→KB 补全→FP 影响说明自动生成+编辑保存→用例生成）
5. 输出最终测试报告

---

## 当前状态分析

### 已修改文件（git status 确认）
| 文件 | 改动内容 | 状态 |
|------|----------|------|
| `backend/app/agents/impact_agent.py` | 新增 `_analyze_fp_impact` / `_bfs_fp_reverse` / `_build_fp_impact_description` | 已实现 |
| `backend/app/api/knowledge.py` L559 | zhparser 诊断异常日志化 | 已实现 |
| `backend/app/api/requirements.py` L797,L2093 | 异常日志化 | 已实现 |
| `backend/app/api/requirements.py` L2742-2861 | FP 影响说明 POST/PUT 端点 | 已实现 |
| `backend/app/api/requirements.py` L279-301 | P0 FK 修复（先 flush Requirement 再存 FP） | 已实现 |
| `backend/app/db/milvus_store.py` | 3 处 `except: pass` → `logger.warning` | 已实现 |
| `backend/app/prompts/gen_agent.py` L196-232 | `impact_analysis_section` 注入生成提示词 | 已实现 |
| `backend/app/prompts/impact_agent.py` L208-255 | FP 影响说明系统提示词 | 已实现 |
| `backend/app/prompts/req_agent.py` | SRR_SCHEMA_GUIDE 新增 `impact_analysis` 字段 | 已实现 |
| `frontend/src/api/client.ts` L267-275 | `generateImpactDescription` / `saveImpactDescription` | 已实现 |
| `frontend/src/pages/DependencyGraph.tsx` L268-469 | `ImpactDescriptionModal` 组件 | 已实现 |
| `frontend/src/types/index.ts` L436-468 | `FPImpactDescription` 接口 | 已实现 |

### 环境状态
- **Docker**：8 容器全部 healthy（运行 5 小时）
- **LLM**：上次切换为 `qwen3.7-plus`，需重新验证连通性
- **测试数据**：`test_data/requirements/01_login_full.md` + `02_login_optimize.md` 已就绪
- **默认账号**：`admin / 888888`
- **前次测试遗留**：无（`aitest_e2e_data.json` 不存在，需从头执行）

### 关键降级机制
- `_build_fp_impact_description`：LLM 失败时用规则拼接文本（不阻塞功能）
- LLM 客户端：多供应商故障转移 + 断路器 + 重试
- 归档后 KB 向量化：Celery 异步，失败不阻断归档

---

## 测试步骤

### 步骤 1：代码审查与漏洞复查（快速）

**目的**：确认 10 个修改文件无遗留漏洞，重点检查 P0 FK 修复和异常处理。

**操作**：
1. 用 `git diff` 查看以下关键文件的改动：
   - `backend/app/api/requirements.py` — 确认 `upload_requirement()` 和 `upload_requirement_file()` 中 `session.flush()` 位置正确（Requirement 创建后、`_save_fps_to_db` 调用前）
   - `backend/app/db/milvus_store.py` — 确认 3 处异常日志化无遗漏
   - `backend/app/agents/impact_agent.py` — 确认 `_bfs_fp_reverse` 有最大跳数限制（≤3），防止无限循环
2. 检查 `requirements.py` L2742-2861 两个端点的权限校验（`require_write` 依赖）
3. 检查 `impact_agent.py` 的 `_bfs_fp_reverse` 是否对环形依赖有防护（visited set）

**预期结果**：无新发现的漏洞，代码逻辑正确。

**失败处理**：如发现漏洞，记录到测试报告，但不阻塞测试流程（除非是 P0 阻塞性问题）。

---

### 步骤 2：环境验证与 LLM 配置

**目的**：确认服务健康 + LLM 连通。

**操作**：
1. 验证 8 容器状态：`docker ps --format "table {{.Names}}\t{{.Status}}"`
2. 登录获取 token：`POST /api/auth/login` {username: "admin", password: "888888"}
3. 获取当前 LLM 配置：`GET /api/settings/llm`
4. 若 model 不是 `qwen3.7-plus`，更新：`PUT /api/settings/llm`
   - Body：`{providers: [{provider: "qwen", provider_label: "通义千问", api_key: "sk-***f31c", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen3.7-plus", is_primary: true}]}`
   - 注意：`api_key` 含 `***` 时保留原加密值
5. 测试 LLM 连通性：`POST /api/settings/llm/test?provider_name=qwen`
6. 验证返回 `success: true`

**预期结果**：LLM 连通正常。

**失败处理**：如果 `qwen3.7-plus` 模型不存在，回退尝试 `qwen-plus` → `qwen-max` → `qwen2.5-72b-instruct`，并报告给用户。

---

### 步骤 3：第一轮 E2E 测试 — 上传到归档全链路

**目的**：验证需求分析完整流程。使用 Python httpx 脚本（避免 PowerShell 中文编码问题）。

**执行方式**：扩展现有 `test_data/test_full_e2e.py`，将超时从 180s 提升到 600s，覆盖完整 10 步。

#### 3.1 登录 + 获取产品
- `POST /api/auth/login` → token
- `GET /api/products/products` → 取第一个产品 ID（若无则创建）

#### 3.2 上传需求文档（01_login_full.md）
- `POST /api/requirements/upload` {raw_text, product_id, source_type: "markdown"}
- 验证：`success: true`，返回 `req_id` 和 `srr`
- 保存 `req_id` 到 `$TEMP/aitest_req1.txt`
- **超时**：600s（qwen3.7-plus 解析 + ClarifyAgent + 最终 SRR 生成约 3-5 分钟）

#### 3.3 获取澄清问题
- `GET /api/requirements/{req_id}/clarify/questions`
- 验证：返回 `questions` / `quality_report` / `kb_status`

#### 3.4 提交澄清答案
- `POST /api/requirements/{req_id}/clarify` {answers: {...}, extra_notes: ""}
- 验证：`success: true`，状态更新为 `active`

#### 3.5 查询功能点列表
- `GET /api/requirements/{req_id}/fp`
- 验证：返回 FP 数组
- **关键**：若 FP 数量 < 2，记录警告（影响 FP 依赖测试），但继续后续步骤

#### 3.6 添加 FP 依赖关系
- `POST /api/requirements/{req_id}/fp/dependencies`
- 若 FP ≥ 2，添加 depends_on / data_flows / conflicts_with 各一条
- 若 FP < 2，跳过并记录（第二轮测试时补充）

#### 3.7 生成测试用例（SSE 流）
- `POST /api/generate` {req_id}（SSE 流，600s 超时）
- 验证：SSE 事件流 parse → retrieve → generate → review
- 验证：返回 `cases` 数组（≥3 个用例）+ `review_report`

#### 3.8 归档需求
- `POST /api/requirements/{req_id}/archive` {go_live_date: "当前时间"}
- 验证：`success: true`，状态变为 `archived`，`kb_task_queued: true`

#### 3.9 验证 KB 查询
- 等待 10 秒让 Celery 完成向量化
- `GET /api/knowledge?q=登录密码错误锁定&limit=5`
- 验证：返回结果包含刚归档的需求

**预期结果**：第一轮全链路通过，需求已归档，KB 可检索。

**数据保存**：将 `{req_id, product_id, fp_ids, cases_count, deps_added}` 写入 `$TEMP/aitest_e2e_data.json`，供第二轮使用。

---

### 步骤 4：第二轮测试 — 优化需求 + FP 影响分析（核心新功能）

**目的**：验证增量需求上传、KB 补全、FP 影响功能说明端到端流程。

#### 4.1 上传优化需求文档（02_login_optimize.md）
- `POST /api/requirements/upload` {raw_text, product_id, source_type: "markdown"}
- 验证：`success: true`，返回新 `req_id2`
- 验证：SRR 中 `_is_incremental: true` 或 KB 补全标记

#### 4.2 验证 KB 补全
- `GET /api/requirements/{req_id2}/clarify/questions`
- 验证：`kb_status.kb_empty: false`（已有第一轮归档数据）
- 验证：`auto_augmentations` 可能有内容（从 KB 自动补全）

#### 4.3 查询新需求的 FP 列表
- `GET /api/requirements/{req_id2}/fp`
- 验证：返回 FP 数组
- 若 FP ≥ 2，添加 FP 依赖关系（为影响分析准备）

#### 4.4 测试 FP 影响功能说明 — 自动生成（核心新功能）
- 前置条件：需有 FP 依赖关系。若新需求 FP < 2，复用第一轮已归档需求的 FP（跨需求 FP 依赖）
- `POST /api/requirements/{req_id2}/fp/{fp_id}/impact-description`
- 验证：
  - `success: true`
  - 返回 `data` 含：
    - `fp_id`, `fp_name`, `fp_type`, `module_id`, `req_id`
    - `affected_fps` 数组（反向 BFS 结果，可能为空）
    - `affected_modules` 数组（模块 BFS 结果）
    - `affected_requirements` 数组
    - `description` 字符串（LLM 合成或降级规则文本，非空）
    - `test_focus` 字符串
    - `generated_at` 时间戳
    - `is_auto_generated: true`

#### 4.5 验证 SRR 中 impact_analysis 已写入
- `GET /api/requirements/{req_id2}`
- 验证：`srr_json.impact_analysis` 是数组
- 验证：数组包含刚生成的 fp_id 对应条目

#### 4.6 测试 FP 影响功能说明 — 编辑保存（核心新功能）
- `PUT /api/requirements/{req_id2}/impact-description/{fp_id}`
- Body：`{description: "手动编辑的影响说明：登录验证码功能变更会影响会话管理和账号锁定逻辑，回归测试需覆盖验证码错误不增加锁定计数的场景。", test_focus: "回归测试关注点：1. 验证码错误不触发账号锁定；2. 验证码过期后会话状态保持；3. 并发场景下验证码一致性"}`
- 验证：`success: true`
- 再次 `GET /api/requirements/{req_id2}`，确认：
  - `impact_analysis` 中该 fp_id 条目的 `description` 已更新
  - `test_focus` 已更新
  - `is_auto_generated: false`（标记为手动编辑）

#### 4.7 生成测试用例（验证 impact_analysis 注入提示词）
- `POST /api/generate` {req_id: req_id2}（SSE 流，600s 超时）
- 验证：SSE 流正常完成
- 验证：生成的用例数 ≥ 3
- 验证：用例 `requirement_field` 包含 `fp:{fp_id}` 建立追溯关系（若可查询）

#### 4.8 验证 FP 依赖图谱端点
- `GET /api/products/{product_id}/fp/dependency-graph`
- 验证：返回所有 FP 节点（跨需求）
- 验证：返回 FPDependency 边
- 验证：节点含 fp_type, priority, req_id, req_title 等元数据

**预期结果**：第二轮测试通过，FP 影响功能说明端到端工作正常。

---

### 步骤 5：测试报告

**操作**：
- 汇总所有测试步骤的通过/失败状态
- 记录任何异常、错误消息、不符合预期的行为
- 对失败项分析根因（代码 bug / 配置问题 / 环境问题）
- 输出最终测试报告（表格形式，包含步骤、验证项、实际结果、状态）

---

## 假设与决策

1. **模型名**：`qwen3.7-plus` 直接使用。若 dashscope 返回 "model not found"，回退 `qwen-plus` → `qwen-max` → `qwen2.5-72b-instruct`，并报告用户。
2. **产品复用**：使用数据库中已有产品，不新建。
3. **FP 依赖数据**：若单个需求 FP < 2，在第二轮测试时跨需求添加 FP 依赖（第一轮归档需求的 FP ↔ 第二轮新需求的 FP），确保影响分析有数据。
4. **测试方式**：Python httpx 脚本（UTF-8 编码正确，支持 SSE 流）。前端 UI 验证作为可选步骤，不在本计划核心范围。
5. **SSE 超时**：600s（qwen3.7-plus 多次 LLM 调用累计约 3-5 分钟）。
6. **清理策略**：测试产生的需求保留作为 KB 数据，不主动删除。
7. **跨需求 FP 依赖**：FP 依赖关系表 `fp_dependencies` 不限制 source 和 target 必须属于同一需求，可跨需求建立依赖（用于影响分析的反向 BFS）。

---

## 验证清单

| 步骤 | 验证项 | 预期状态 |
|------|--------|----------|
| 1 | 代码审查无 P0 漏洞 | ✅ |
| 2 | 8 容器全部 healthy | ✅ |
| 2 | LLM 连通性测试 (qwen3.7-plus) | ✅ success=true |
| 3.2 | 需求上传 + SRR 解析 | ✅ |
| 3.3 | 澄清问题获取 | ✅ |
| 3.4 | 澄清答案提交 + 状态 active | ✅ |
| 3.5 | FP 列表查询 | ✅ FP ≥ 1 |
| 3.6 | FP 依赖添加（或跨需求补充） | ✅ |
| 3.7 | 测试用例生成 (SSE) | ✅ cases ≥ 3 |
| 3.8 | 需求归档 | ✅ status=archived |
| 3.9 | KB 查询命中归档需求 | ✅ |
| 4.1 | 优化需求上传 | ✅ |
| 4.2 | KB 补全验证 (kb_empty=false) | ✅ |
| 4.3 | 新需求 FP 列表 | ✅ |
| 4.4 | **FP 影响说明自动生成** | ✅ **核心** |
| 4.5 | **SRR impact_analysis 写入** | ✅ **核心** |
| 4.6 | **FP 影响说明编辑保存** (is_auto_generated=false) | ✅ **核心** |
| 4.7 | 用例生成含 impact_analysis | ✅ |
| 4.8 | FP 依赖图谱端点 | ✅ |
| 5 | 测试报告输出 | ✅ |

---

## 执行方式

1. 创建 TodoWrite 跟踪 5 个步骤
2. 步骤 1-2 快速验证（< 2 分钟）
3. 步骤 3 第一轮 E2E：扩展现有 `test_full_e2e.py`（提升超时到 600s），运行并等待完成
4. 步骤 4 第二轮测试：新建 `test_data/test_round2_fp_impact.py` 脚本，读取 `$TEMP/aitest_e2e_data.json` 获取第一轮数据，执行 4.1-4.8
5. 步骤 5 汇总报告
