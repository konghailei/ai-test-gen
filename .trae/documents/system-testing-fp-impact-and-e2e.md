# 系统全方位测试计划 — FP 影响功能说明 + 两轮 E2E

## 背景与目标

上一阶段已完成全部代码实现（步骤 1-8：漏洞修复、SRR schema、ImpactAnalysisAgent 扩展、后端端点、GenAgent 提示词、前端 API、DependencyGraph 弹窗）。LLM 之前因 qwen/kimi-k2.6 免费额度耗尽而阻塞测试。

**用户确认**：保持相同 API Key，将模型切换为 `qwen3.7-plus`（同 dashscope 平台）。

**本计划目标**：完成两轮系统全方位测试，验证已实现的功能并发现潜在问题。

---

## 当前状态分析

### 已实现（步骤 1-8，代码审查确认）
- `backend/app/db/milvus_store.py` — 3 处 `except Exception: pass` → `logger.warning`
- `backend/app/api/knowledge.py` L559 — zhparser 诊断异常日志化
- `backend/app/api/requirements.py` L797, L2093 — 异常日志化
- `backend/app/api/requirements.py` L2724-2852 — `POST /{req_id}/fp/{fp_id}/impact-description` + `PUT /{req_id}/impact-description/{fp_id}`
- `backend/app/prompts/req_agent.py` — SRR_SCHEMA_GUIDE 新增 `impact_analysis` 字段
- `backend/app/agents/impact_agent.py` L325-585 — `_analyze_fp_impact` + `_bfs_fp_reverse` + `_build_fp_impact_description`（含 LLM 降级机制）
- `backend/app/prompts/impact_agent.py` L208-255 — `FP_IMPACT_DESCRIPTION_SYSTEM_PROMPT` + `build_fp_impact_description_prompt`
- `backend/app/prompts/gen_agent.py` L196-232 — `build_user_prompt` 注入 `impact_analysis_section`
- `frontend/src/api/client.ts` L267-275 — `generateImpactDescription` + `saveImpactDescription`
- `frontend/src/types/index.ts` L436-468 — `FPImpactDescription` 接口
- `frontend/src/pages/DependencyGraph.tsx` L268-469 — `ImpactDescriptionModal` 组件 + FP 模式双击触发

### LLM 配置更新方式
- 端点：`PUT /api/settings/llm`（全量替换，第一项为主供应商）
- 请求体：`{providers: [{provider, provider_label, api_key, base_url, model, is_primary}]}`
- API Key 含 `***` 时保留原值（脱敏格式），新 Key 会 Fernet 加密存储
- 更新后即时生效（写入 `settings._all_provider_configs`）

### 测试数据
- `test_data/requirements/01_login_full.md` — 第一轮：完整登录需求
- `test_data/requirements/02_login_optimize.md` — 第二轮：优化需求（增量）
- 默认账号：`admin / 888888`

### 关键降级机制
- `_build_fp_impact_description`：LLM 失败时用规则拼接文本（不阻塞功能）
- LLM 客户端：多供应商故障转移 + 断路器 + 重试

---

## 测试步骤

### 步骤 1：环境准备与 LLM 配置切换

**目的**：确认所有服务健康，切换 LLM 模型到 `qwen3.7-plus`。

**操作**：
1. 检查 8 个容器状态：`docker ps --format "table {{.Names}}\t{{.Status}}"`
   - ai-test-gen-web, ai-test-gen-api, ai-test-gen-celery, ai-test-gen-pg, ai-test-gen-redis, ai-test-gen-etcd, ai-test-gen-minio, ai-test-gen-milvus
2. 登录获取 token：`POST /api/auth/login` {username: "admin", password: "888888"}
3. 获取当前 LLM 配置：`GET /api/settings/llm`
4. 更新 LLM 配置：`PUT /api/settings/llm`
   - provider: "qwen"
   - provider_label: "通义千问"
   - api_key: "sk-***f31c"（脱敏格式，保留原值）— **注意**：由于前端传脱敏格式会保留原 Key，但 PUT 是全量替换，需要先 GET 拿到完整 api_key 后再传入完整 Key
   - base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
   - model: "qwen3.7-plus"（**新模型名**）
   - is_primary: true
5. 测试 LLM 连通性：`POST /api/settings/llm/test?provider_name=qwen`
6. 验证返回 `success: true`，延迟合理（<5000ms）

**预期结果**：LLM 连通正常，可进行后续测试。

**失败处理**：如果 `qwen3.7-plus` 模型不存在，尝试 `qwen-plus` / `qwen-max` / `qwen2.5-72b-instruct` 等标准模型名，并报告给用户。

---

### 步骤 2：第一轮 E2E 测试 — 上传到归档全链路

**目的**：验证需求分析完整流程（上传 → 解析 → 澄清 → FP 拆分 → FP 依赖 → 生成用例 → 归档 → KB 查询）。

#### 2.1 上传需求文档
- 端点：`POST /api/requirements/upload`
- Body：`{raw_text: <01_login_full.md 内容>, product_id: <已有产品ID>, source_type: "markdown"}`
- 验证：
  - `success: true`
  - 返回 `req_id` 和 `srr` 对象
  - SRR 包含核心字段：requirement, preconditions, flows, business_rules 等
  - 需求状态为 `analyzing` 或 `active`

#### 2.2 获取澄清问题
- 端点：`GET /api/requirements/{req_id}/clarify/questions`
- 验证：
  - 返回 `questions` 数组（可能为空或含若干问题）
  - 返回 `quality_report`（含 overall_score）
  - 返回 `kb_status`（kb_empty 应为 false 如果有归档数据）

#### 2.3 提交澄清答案
- 端点：`POST /api/requirements/{req_id}/clarify`
- Body：`{answers: {...}, extra_notes: ""}`
- 验证：
  - `success: true`
  - 需求状态更新为 `active`

#### 2.4 查询功能点列表
- 端点：`GET /api/requirements/{req_id}/fp`
- 验证：
  - 返回 FP 数组（登录场景应有 3-6 个 FP）
  - 每个 FP 含 id, name, fp_type, module_id, priority, test_hints

#### 2.5 添加 FP 依赖关系（为第二轮测试准备）
- 端点：`POST /api/requirements/{req_id}/fp/dependencies`
- Body：`{source_fp_id: <fp1_id>, target_fp_id: <fp2_id>, relation_type: "depends_on", description: "..."}`
- 至少添加 2-3 条依赖关系（depends_on / data_flows / conflicts_with）
- 验证：`success: true`

#### 2.6 生成测试用例
- 端点：`POST /api/generate`（SSE 流）
- Body：`{req_id: <req_id>}`
- 验证：
  - SSE 事件流：parse → retrieve → generate → review
  - 最终返回 `cases` 数组（至少 5 个用例）
  - 每个用例含 title, type, priority, case_json
  - 返回 `review_report`（含 coverage_summary）

#### 2.7 归档需求
- 端点：`POST /api/requirements/{req_id}/archive`
- Body：`{go_live_date: "2026-07-15 12:00:00"}`
- 验证：
  - `success: true`
  - 状态变为 `archived`
  - 返回 `kb_task_queued: true`（异步向量化）

#### 2.8 验证 KB 查询
- 等待 5-10 秒让 Celery 完成向量化
- 端点：`GET /api/knowledge?q=登录密码错误&limit=5`
- 验证：
  - 返回结果包含刚归档的需求
  - 相似度分数合理（>0.3）

**预期结果**：第一轮全链路通过，需求已归档，KB 可检索。

---

### 步骤 3：第二轮测试 — 优化需求 + FP 影响分析

**目的**：验证增量需求上传、KB 补全、以及本次新增的 FP 影响功能说明端到端流程。

#### 3.1 上传优化需求文档
- 端点：`POST /api/requirements/upload`
- Body：`{raw_text: <02_login_optimize.md 内容>, product_id: <同一产品ID>, source_type: "markdown"}`
- 验证：
  - `success: true`
  - SRR 中 `_is_incremental: true`（KB 补全标记）
  - SRR 中 `_baseline_gaps` 可能有内容（与归档需求对比的差距）

#### 3.2 验证 KB 补全
- 端点：`GET /api/requirements/{new_req_id}/clarify/questions`
- 验证：
  - `auto_augmentations` 数组非空（从 KB 自动补全的字段）
  - `kb_status.kb_empty: false`

#### 3.3 查询新需求的 FP 列表
- 端点：`GET /api/requirements/{new_req_id}/fp`
- 验证：返回 FP 数组

#### 3.4 测试 FP 影响功能说明 — 自动生成（核心新功能）
- 前置条件：需要有一个 FP 存在于 FP 依赖图中（步骤 2.5 添加的依赖，或新需求中也添加 FP 依赖）
- 端点：`POST /api/requirements/{new_req_id}/fp/{fp_id}/impact-description`
- 验证：
  - `success: true`
  - 返回 `data` 含完整影响分析结果：
    - `fp_id`, `fp_name`, `fp_type`, `module_id`, `req_id`
    - `affected_fps` 数组（反向 BFS 找到的依赖此 FP 的功能点）
    - `affected_modules` 数组（模块依赖图谱 BFS 结果）
    - `affected_requirements` 数组（受影响的其他需求）
    - `description` 字符串（LLM 合成或降级规则文本）
    - `test_focus` 字符串（测试关注点）
    - `generated_at` 时间戳
    - `is_auto_generated: true`
  - SRR 的 `impact_analysis` 数组新增一条记录

#### 3.5 验证 SRR 中 impact_analysis 已写入
- 端点：`GET /api/requirements/{new_req_id}`
- 验证：
  - `srr_json.impact_analysis` 是数组
  - 数组包含刚生成的 fp_id 对应条目
  - 条目结构与步骤 3.4 返回一致

#### 3.6 测试 FP 影响功能说明 — 编辑保存（核心新功能）
- 端点：`PUT /api/requirements/{new_req_id}/impact-description/{fp_id}`
- Body：`{description: "手动编辑的影响说明内容", test_focus: "手动编辑的测试关注点"}`
- 验证：
  - `success: true`
  - 再次 GET 需求详情，确认 `impact_analysis` 中该 fp_id 条目的：
    - `description` 已更新为手动编辑内容
    - `test_focus` 已更新
    - `is_auto_generated: false`（标记为手动编辑）

#### 3.7 生成测试用例（验证 impact_analysis 注入提示词）
- 端点：`POST /api/generate`（SSE 流）
- Body：`{req_id: <new_req_id>}`
- 验证：
  - SSE 流正常完成
  - 生成的用例中应体现影响分析的关注点（如果 description/test_focus 有具体内容）
  - 用例 `requirement_field` 包含 `fp:{fp_id}` 建立追溯关系

#### 3.8 验证 FP 依赖图谱端点
- 端点：`GET /api/products/{product_id}/fp/dependency-graph`
- 验证：
  - 返回所有 FP 节点（跨需求）
  - 返回 FPDependency 边
  - 节点含 fp_type, priority, req_id, req_title 等元数据

**预期结果**：第二轮测试通过，FP 影响功能说明端到端工作正常（自动生成 + 编辑保存 + SRR 写入 + 用例生成注入）。

---

### 步骤 4：前端 UI 验证（可选，如果后端测试全部通过）

**目的**：验证 DependencyGraph.tsx 的 ImpactDescriptionModal 在浏览器中正常工作。

**操作**：
1. 打开浏览器访问 `http://localhost`
2. 登录 admin/888888
3. 选择产品 → 进入"依赖图谱"页面
4. 切换到"功能点"模式
5. 双击一个 FP 节点 → 验证弹出 ImpactDescriptionModal
6. 点击"生成影响说明"按钮 → 验证加载动画 + 结果展示
7. 编辑 description/test_focus 文本框 → 点击"保存"
8. 验证保存成功提示

---

### 步骤 5：测试报告与问题记录

**操作**：
- 汇总所有测试步骤的通过/失败状态
- 记录任何异常、错误消息、不符合预期的行为
- 对失败项分析根因（代码 bug / 配置问题 / 环境问题）
- 输出最终测试报告

---

## 假设与决策

1. **模型名假设**：用户提供的 `qwen3.7-plus` 模型名直接使用。如果 dashscope 返回 "model not found"，回退尝试 `qwen-plus` → `qwen-max` → `qwen2.5-72b-instruct`，并立即报告给用户。
2. **产品复用**：使用数据库中已有的产品（如 `PROD-30428-917 "测试"`），不新建产品。
3. **FP 依赖数据**：如果第一轮上传的需求没有自动拆分出足够多的 FP，手动通过 API 添加 FP 依赖关系以支持第二轮的影响分析测试。
4. **测试方式**：优先使用 PowerShell + `Invoke-RestMethod` 进行 API 测试（快速、可脚本化）。前端 UI 验证作为可选步骤。
5. **SSE 测试**：`POST /api/generate` 是 SSE 流，使用 `Invoke-RestMethod` 可能无法正确处理流式响应。如果遇到问题，改用 `httpx` Python 脚本或直接通过前端 UI 触发生成。
6. **清理策略**：测试产生的需求不主动删除（保留作为后续测试的 KB 数据）。如果用户要求清理，使用 `DELETE /api/requirements/{id}`。

---

## 验证清单

| 步骤 | 验证项 | 预期状态 |
|------|--------|----------|
| 1 | 8 容器全部 healthy | ✅ |
| 1 | LLM 连通性测试 | ✅ success=true |
| 2.1 | 需求上传 + SRR 解析 | ✅ |
| 2.2 | 澄清问题获取 | ✅ |
| 2.3 | 澄清答案提交 | ✅ |
| 2.4 | FP 列表查询 | ✅ FP 数量 ≥ 1 |
| 2.5 | FP 依赖添加 | ✅ |
| 2.6 | 测试用例生成 | ✅ cases ≥ 5 |
| 2.7 | 需求归档 | ✅ status=archived |
| 2.8 | KB 查询 | ✅ 命中归档需求 |
| 3.1 | 优化需求上传 | ✅ _is_incremental=true |
| 3.2 | KB 补全验证 | ✅ auto_augmentations 非空 |
| 3.3 | 新需求 FP 列表 | ✅ |
| 3.4 | FP 影响说明自动生成 | ✅ **核心新功能** |
| 3.5 | SRR impact_analysis 写入 | ✅ **核心新功能** |
| 3.6 | FP 影响说明编辑保存 | ✅ is_auto_generated=false |
| 3.7 | 用例生成含 impact_analysis | ✅ |
| 3.8 | FP 依赖图谱端点 | ✅ |
| 4 | 前端 UI 弹窗验证 | ✅（可选）|
