# 端到端系统测试计划：产品创建 → 需求上传分析 → 归档 → 澄清问题 → KB回答合并 → 多次上传影响分析

> Status: DRAFT（Planner v1 输出）
> Source: user request
> Mode: (default)
> Iterations: 1 / 3
> Author: user request
> Last updated: 2026-08-07

---

## Requirements summary

用户需要对 ai-test-gen 系统执行一次**完整的端到端（E2E）功能测试**：
1. 新建一个独立产品（隔离现有数据）
2. 自己构造并上传 3~5 个需求文档（docx/md），需求之间设计好版本演进关系（v1 新建→v2 优化→v3 修复/小改动等），
   并设计若干场景：
   - 需求包含引用"现有 XX 规则"（引用未定义，KB 中 v1 会归档）
   - 需求包含数字矛盾（如 50 个 vs 不设上限）
   - 需求是优化/改造类（历史数据影响问题触发）
   - 需求有 happy_path 等字段，避免过度宽泛追问
3. 验证：归档是否正确（项目/需求/FP 三级状态流转、Milvus 向量入库、PG status、hybrid_search 按 archived 过滤召回）
4. 验证：需求分析是否完善（SRR 结构、_raw_text、功能点数量/粒度符合整体逻辑，不破坏既有分析框架）
5. 验证：产生的澄清问题是否精准且不多余（猜想类/过宽类问题被正确过滤，优化影响类仅一套并排末尾，引用未定义文本不冗长）
6. 验证：对澄清问题，若知识库中确实存在归档版本定义，则 KB 查询路径是否能命中并自动补全到 SRR，**合并后的 SRR 字段正确性**（不能覆盖原字段、必须 merge unique、cross_fp_flows 等字段符合 project_memory 中 hard constraints）
7. 验证：**多次上传后的影响范围分析**（v2、v3 上传后，对 v1 已归档 FP 的影响分析、依赖变更、回归用例分类是否正确）
8. 若发现问题，必须在**不破坏需求分析整体逻辑**的前提下修复，任何修改必须通过已有测试 + 新增回归用例验证。

核心精神：**闭环系统验收测试 + 问题零容忍修复**，但修复遵守"外科手术式改动，不改变整体架构/整体流程"原则。

---

## Acceptance criteria

### AC-1：产品/项目/模块创建成功且互相隔离
- 新建产品 Product A 成功返回 id，PG `products` 表有记录；创建 Project A1 / A2（其中 A1 走"活动→归档"，A2 走"活动→pending_archive→取消→活动→归档"两个状态路径）；创建 Module M1（用户）、M2（内容）、M3（版本）
- 用 A1/A2 上传需求后，A2 的 hybrid_search 查询结果不包含 A1 项目下的需求（按 product_id + modules 双重隔离验证）
- 项目 A1 归档后，`projects.status = archived`，其下 100% 需求和 FP 状态也同步为 `archived`，且 Milvus 内按 product_id 隔离查询不泄漏到其它产品。

### AC-2：需求文档设计合理，覆盖全部测试场景
- 至少 4 个需求文档：
  1. `ReqA_v1_PRD.md` — 新建平台 PRD（图片上传规则在 PRD 正文中**明确给了格式+大小+超限文案**；版本数量"最多保留 50 个"；用户管理/内容管理/版本管理三大模块；happy_path 至少 4 步）
  2. `ReqA_v2_Optimize.md` — 优化需求（明确包含"沿用现有图片上传规则"；"不设版本数量上限"；多处升级改造关键词 → 触发优化类专项）
  3. `ReqA_v3_SmallFix.md` — 小修订（"正文改动<5%的小修订免审核"→5% 判定标准未定义；引用 v1 的敏感词策略但未展开；happy_path 存在但字段略少）
  4. `ReqB_PRD.md` — 同产品 A 下另一个完全不相关产品（简单审批流 PRD），用于验证跨需求污染
- 文档内标题、模块、验收标准严格按 Markdown 列表写，确保 `_raw_text` 解析后结构可被 `_detect_logical_contradictions` 正常识别。
- 文档**不使用模拟数据以外的任何真实业务内容**（用户隐私/版权零风险）。

### AC-3：归档正确性验证（7 项指标）
1. `archive_project` 后，`products/{pid}/projects` 列表接口返回状态 `archived`
2. `projects/{proj_id}/kb/entries` 接口返回 **数量正确** 的需求和 FP，且状态全部 = archived
3. Milvus 内用 `product_id=A1` 查询向量存在（数量 ≈ 需求数 × 版本片段 + FP 数），用其它 product_id 查询零命中
4. `hybrid_searcher.search_requirements` 对 A1 的图片上传规则短 query 返回 ReqA_v1 文档（已归档，RRF 时间衰减排序最新版本优先）
5. `cancel-pending-archive` 正确回滚状态不残留 archived 标记
6. `reactivate` 恢复项目后，需求从 archived→active，且再次分析需求时 hybrid_search 不再把该需求作为 KB 返回
7. `archive_requirement` 单需求归档正确，不影响同级其它需求状态。

### AC-4：需求分析完善且不破坏整体逻辑
- 每个需求的 SRR JSON 结构符合：
  - `requirement.product_id / requirement.modules[]`（**复数**，不是 module；对齐 project_memory）
  - `flows.happy_path.steps` 与原始文档步骤一致（不能缺、不能凭空加 2 倍步骤）
  - `acceptance_criteria` 结构化字段 + `_raw_text` 全文（Markdown 列表完整保留）
  - `legacy_data_impact / total_srr / fp_srr` 符合文档分类（PRD 有，优化需求更完整，小修订最小）
- `FP 拆分后必须通过 FPGate 三项检查`（fp 非空、字段完整、唯一性），失败就中断标记 failed，不能"将就通过"。
- 需求状态流转完全走既有流水线：`analyzing → draft(needs_clarification) → pending_review → confirmed → archived`，不新增或删除任何状态值。

### AC-5：澄清问题精准、无多余
- 对于 `ReqA_v2_Optimize.md`：
  1. "沿用现有图片上传规则引用未定义"问题必须出现，且**question 文本精简在 120 字内，不含 raw_text#N 来源位置/原文引用噪声**
  2. "版本数量 50 vs 不设上限"的矛盾问题必须出现，category = conflict，
  3. 优化影响配套问题**恰好 2 个**（kb_optimization_impact + kb_optimization_impact_scope），严格排在人工澄清的最末尾，KB 路径不再生成第三套同语义问题
- 对于 `ReqA_v3_SmallFix.md`：5% 改动比例判定标准问题必须出现，但"逐实体字段定义类过宽追问"必须被 `_filter_over_broad_llm_questions` 过滤掉
- 猜想类问题（如"超过 X 怎么办"等 speculative upper_bound）必须 0 条出现在最终人工澄清列表。

### AC-6：KB 存在的问题能正确回答 + 合并 SRR 正确
- 流程：先上传 ReqA_v1_PRD.md → 分析通过 → 归档（Milvus + PG 双写） → 再上传 ReqA_v2_Optimize.md
- 对 v2 产生的"图片上传规则引用未定义"问题，KB 查询阶段必须**使用短 query 命中 v1**，返回 answered=true
- 合并后的 v2 augmented_srr 验证：
  - 图片上传规则字段从空 → 填充 v1 中的**格式/大小/超限文案三项具体值**
  - SRR 原有字段（如 requirement.title / requirement.id / flows.happy_path 等）**一点也不能被 KB 覆盖**
  - cross_fp_flows / shared_business_rules 等属于 total SRR merge 的字段只能 append unique，**不能 overwrite**
  - `gen_confidence` 必须放在 `gen_metadata.gen_confidence`（顶层）

### AC-7：多次上传影响范围分析正确
- 在 ReqA_v1 归档后上传 ReqA_v2_Optimize：`/{req_id}/impact-analysis` 返回受影响 FP 列表 = ReqA_v1 中所有与"版本数量 / 图片上传 / 用户权限"有关的 FP
- 影响分析的依赖变更方向：双向无箭头约束、连线不遮挡、节点显示 FP 名称（不显示 Req 名称），与 project_memory 偏好一致
- 确认影响分析后，`/generate` 生成的回归测试用例中，受影响 FP 的用例被标记为 type='regression'，并归类到"回归测试"目录下
- 再上传 ReqA_v3（小改动）：影响分析仅列出改动范围直接关联的 FP，**不产生过度扩散**（比如小改动不应标记全部 50 个 FP 为受影响）。

### AC-8：问题修复的零侵入性
- 若发现任何 bug，修复改动满足：
  - 不得修改路由文件（对齐 project_memory：middleware refactors 也不动路由）
  - 不得改动 SRR 顶层 schema，只能加辅助字段（下划线前缀 _xxx），辅助字段必须在最终持久化前清理
  - 修复后必须执行后端已有测试套件 `pytest backend/tests -x -q` 全通过，再加一条针对本次 bug 的最小回归用例。

---

## RALPLAN-DR

### Principles
1. **最小改动 + 可回滚**：任何代码修改以"局部补丁"为主，不重构；所有改动通过单一函数入口，能 1 行 revert 就回退。
2. **隔离第一**：全程使用独立产品（A-E2ETEST-YYYYMMDD-HHMM 命名）做测试，绝不碰现有项目；所有辅助文档存 `backend/_e2e_tmp/`，测试完成可选保留或删除。
3. **诊断先行、修复其次**：每一步异常**先留证据再修复**：save SRR JSON、save clarify questions、save kb_access_log、save impact 结果，归档后才能改代码。
4. **整体逻辑神圣不可侵犯**：ClarifyAgent 主流程 9 大步骤（①→⑨）顺序不变、QualityGate / FPGate / 错误可见性原则不动；仅在单步骤内加前置/后置过滤器。
5. **可验证性优先**：每个 AC 对应可执行的命令，不能用"看起来对了"验收。

### Decision drivers（前 3 高优先）
1. **数据隔离与可重现**：用户要求"新建一个产品"，这天然是避免污染现有数据的最佳驱动 → 所有测试资源 UUID 化，运行前后 snapshot 比较。
2. **场景覆盖完整度**：必须覆盖引用未定义/矛盾检测/优化影响/小修订 4 类场景，不然测试结果无法证明澄清问题的精准性。
3. **修复零侵入性**：用户明确说"修复不能破坏需求分析整体逻辑" → 任何进入代码修改阶段的步骤必须走「诊断 → 最小补丁 → 双重验证（旧测试+新回归）」流程。

### Viable options

#### Option A：真实 E2E（Favored）
- 实现思路：**直接调用真实 REST API**（登录 → 建产品 → 建项目 → 上传 docx/md → 轮询 tasks 直到完成 → 检查数据库状态 + Milvus 向量 → 归档 → 下一次上传 → 检查影响分析）；脚本用 Python httpx 调用 FastAPI；API 不通时降级为直接调用 `asyncio.run` 后端函数。
- 改动文件：
  - `scripts/e2e_system_test/_runner.py`（新建，主执行脚本，测试不通过退出码非 0）
  - `scripts/e2e_system_test/_artifacts/*.{md,json}`（新建，测试文档，**绝不进 git**：放在 .gitignore）
  - `backend/app/agents/clarify_agent.py`（**仅在 bug 确诊后修改，修改量 < 80 行/问题**，保留备份）
  - `backend/tests/test_e2e_system_regression.py`（新建，1 条 pytest 用例，跑最小核心场景）
- Pros：
  - 最接近真实用户行为，发现"集成层"bug（session/权限/SSE/前后端状态同步）概率最高
  - 测试脚本可复用，未来回归可以重跑
  - 测试 artifacts 可保存，出问题便于诊断
- Cons：
  - 依赖 Docker 容器（postgres/milvus/api/celery）健康运行，环境异常时需要先恢复
  - 执行时间较长（1 个需求 clarify 分析约 30s~120s，4 个需求 + 归档/影响分析总预计 15 分钟）

#### Option B：后端集成测试（降级备选）
- 实现思路：**不启动前端和 API 容器，仅用 pytest + TestClient 测后端**，直接调用 `upload_requirement` 视图函数，用 `AsyncSession` 直接检查 PG，Milvus 侧走 `milvus_store` mock。
- 改动文件：
  - `backend/tests/test_e2e_integration.py`（新建，fixture 建 Product/Project，pytest-asyncio）
  - `backend/app/db/milvus_store.py` 仅在 mock 时单测文件做 monkey patch（不动主代码）
- Pros：
  - 不依赖 Docker 网络，纯单测可跑，时间更短（预计 5 分钟）
  - 能精准控制输入输出，易定位问题
- Cons：
  - **无法测 SSE 进度、任务状态、前后端状态同步**（比如用户反馈的"暂不处理跳转详情页"问题），这是 Option B 最大的盲区
  - 对 hybrid_search 依赖 Milvus 原生能力的部分（RRF 融合、时间衰减）只能 mock，真实命中率难以验证

#### Option C：手动操作 + 脚本诊断（Rejected）
- 理由：可重现性差（每次手动点击顺序不一致）、诊断证据不完整（缺少原始 HTTP 请求）、无法回归复用。不符合 Principle 5。

### 为什么选 Option A + Option B 作 fallback（综合路径）
- Option A 满足全部 AC 覆盖度最高；**如果容器已在运行，直接 A**；
- 如果容器异常 3 次重试无法恢复，立即降级 Option B，但要把"AC-1 / AC-3 中 SSE/前端跳转相关部分"标记为"未覆盖，需手工验收"，在最终报告中显式列出。

---

## Implementation steps（基于 Favored Option A）

### Step 0：环境预检 & 工作区检查（不改代码）
- 命令：`docker-compose ps`（观察 api / celery / postgres / milvus 是否 Up healthy）
- 命令：`git status --short && git branch --show-current`（输出写入 `_e2e_tmp/git_before.log`，确保测试开始前工作区状态）
- 命令：`cd backend && python -c "from app.agents.clarify_agent import ClarifyAgent; print('agent import OK')"`（如果失败，先执行 Option B 降级）
- 文件引用：`docker-compose.yml`、`backend/app/agents/clarify_agent.py:1-100`（import 区块）

### Step 1：构造 4 份测试需求文档
- 新建目录 `backend/_e2e_tmp/docs/`
- 生成 4 份 Markdown 文件（按 AC-2 要求逐段编写，用中文，避免任何真实业务内容）：
  1. `A_E2ETEST_ReqA_v1_PRD.md`
  2. `A_E2ETEST_ReqA_v2_Optimize.md`
  3. `A_E2ETEST_ReqA_v3_SmallFix.md`
  4. `A_E2ETEST_ReqB_PRD.md`
- 文档长度：每个 500~1200 字，确保 Markdown 列表验收标准至少 6 条、`_raw_text` 解析出至少 3 个不同章节。
- 如果系统仅支持 docx，则用 `pandoc -i xx.md -o xx.docx`（如未安装则降级 md 直接上传，因为 `_run_requirement_analysis` 内部已支持两种）。
- 代码引用：`backend/app/api/requirements.py:1018-1050 upload_requirement 接收 File Upload 的格式判断逻辑`

### Step 2：登录获取鉴权 Token → 建产品/项目/模块
- 脚本中执行：POST `/auth/login` → 保存 token；如果 admin 默认账号配置不存在，走直接从 DB 取 API key 或创建测试账号（仅测试产品使用，测试结束删除）
- 创建产品 `E2E-产品隔离测试 20260807`：POST `/products` body=CreateProductRequest
- 创建项目 A1：POST `/products/{pid}/projects`，A2 同样
- 创建 M1/M2/M3 模块：POST `/products/{pid}/modules`
- 保存返回的所有 ID 到 `_e2e_tmp/ids.json`（pid/proj1_id/proj2_id/m1_id/m2_id/m3_id）
- 代码引用：
  - `backend/app/api/products.py:122-145 create_product`
  - `backend/app/api/products.py:308-343 create_project`
  - `backend/app/api/products.py:1504-1523 create_module`

### Step 3：上传 ReqA_v1_PRD → 分析 → 检查 SRR 结构 → 手动补全用户澄清（若有）→ 归档
- `POST /requirements/upload`（form-data：file + project_id + modules）
- `GET /tasks/{task_id}` 轮询直到 status=success / failed（超时 240s 即失败）
- 成功后 `GET /requirements/{req_id}` → 保存 SRR JSON 到 `_e2e_tmp/reqA_v1_srr.json`
- **SRR 完整性检查**（Python 断言）：`product_id 正确`、`modules == [M1, M2, M3]`、`_raw_text 非空且长度 > 500`、`flows.happy_path.steps >= 3`
- 如果产生澄清问题 → `POST /requirements/{req_id}/clarify` 提交一份合理答案（模拟用户回答）→ 等待 `_run_clarify_reparse` 完成
- `GET /requirements/{req_id}` 确认 status = draft → `POST /{req_id}/finalize` → `POST /{req_id}/confirm` → `POST /{req_id}/archive`（加 go_live_date=今天）
- 断言 AC-3：
  - PG 内 `requirements.status='archived'`
  - Milvus 用 `product_id=` 查询，向量数 > 0（调用 `milvus_store.search` 或直接 count）
  - `hybrid_searcher.search_requirements(query="图片上传规则 格式 大小 限制", product_id=A)` 返回的 top3 中至少有 1 条命中 ReqA_v1
- 代码引用：
  - `backend/app/api/requirements.py:1018 / upload_requirement`
  - `backend/app/api/requirements.py:4182-4223 archive_requirement`
  - `backend/app/db/hybrid_search.py: hybrid_searcher.search_requirements`（验证 RRF 融合 + 时间衰减）

### Step 4：上传 ReqA_v2_Optimize → 重点验证 4 条澄清质量 + KB 自动回答
- 同 Step3 上传流程，保存：
  - `_e2e_tmp/reqA_v2_clarify_questions.json`（`GET /{req_id}/clarify/questions` 返回）
  - `_e2e_tmp/reqA_v2_kb_access_log.json`（GET /requirements/{req_id} 返回 srr._kb_access_log 或 clarify 页日志）
- 断言 AC-5：
  - 矛盾问题：存在 1 条 category=conflict，question 含"50"和"上限"→ 如果缺失，记录为 BUG-矛盾漏检
  - 引用未定义：存在 1 条，`len(question) <= 120`，且不含 "raw_text#" → 如果不满足，记录为 BUG-问题过长
  - 优化影响：恰好 2 条，且在 questions 列表最后 2 个位置，id ∈ {kb_optimization_impact, kb_optimization_impact_scope} → 如果出现第 3 条语义重复，BUG-重复优化问题
  - 猜想类问题：0 条（扫描所有 question，正则匹配 "超过.*怎么办|若.*大于|大于.*上限|假设.*超限" 等 → 命中任意一条即 BUG-猜想类未删）
- 断言 AC-6：从 clarify_questions 中取出"图片上传规则引用未定义"问题 → 看它的 `resolved/answered` 状态是否为 True（由 KB 路径回答），并且 `augmented_srr` 中的图片上传规则字段已被填充具体值；同时对比 `_e2e_tmp/reqA_v1_srr.json` 和 v2 augmented_srr 同字段完全一致
- 若 AC-6 不成立（KB 能手动查到但 clarify 阶段未命中）→ 先分析是 query 质量 / product_id 过滤 / Milvus 未入库哪一种，Step 6 最小补丁修复
- 代码引用：
  - `backend/app/agents/clarify_agent.py:2147 hybrid_searcher.search_requirements query 优先级`（已改为优先 _kb_short_query）
  - `backend/app/agents/clarify_agent.py: L826-L845 统一分流池 与 L1263-L1290 末位追加`

### Step 5：确认 v2 澄清 → 归档 v2 → 上传 v3 → 影响分析正确性
- 对 v2 剩余的人工澄清问题（如矛盾项，KB 无法回答）提交合理答案 → `_run_clarify_reparse` → finalize → confirm → archive
- 上传 `ReqA_v3_SmallFix.md`，保存澄清问题
- 断言 AC-5 对 v3：存在"5%改动比例判定标准"问题；"实体字段定义类过宽追问"0 条（通过 `_filter_over_broad_llm_questions`）
- 在 v3 澄清页或 API 触发 `POST /{req_id}/impact-analysis`（不选/智能选择受影响 FP）→ 保存结果 `_e2e_tmp/impact_v3.json`
- **断言 AC-7 核心**：
  - 受影响 FP 数量 > 0 且 < 「ReqA_v1 FP 总数 × 60%」（防止过度扩散）
  - 至少 1 个 FP 的影响原因 = "改动触及图片上传规则 / 版本数量上限调整"（依赖变更检测正确）
  - `POST /{req_id}/impact-analysis/confirm` 后，`/generate` 生成的用例包含回归类：type=regression 且目录=回归测试
- 代码引用：
  - `backend/app/api/requirements.py:5217-5237 impact-analysis 入口`
  - `backend/app/agents/impact_agent.py`（如有）或对应 impact 检测逻辑文件
  - `backend/app/api/generate.py` 生成用例时 regression 分类逻辑

### Step 6：问题诊断与零侵入修复（仅在 AC 失败时进入）
**规则**：任何修复必须按下面顺序执行，**禁止跳步**。
1. **留证**：把失败 AC 的所有请求/响应/SRR/questions/SQL/Milvus 查询结果保存到 `_e2e_tmp/bug_{NAME}/`
2. **最小补丁方案**：写一份 5 行以内的修复提案 → 检查是否违反 8 条 hard constraints（project_memory.md），若违反就换方案；
   - 路由文件**绝对不改**
   - SRR 顶层绝对不改
   - 主流程顺序 ①~⑨ 绝对不改
3. **实施补丁**：修改对应文件 + 写 1 条 pytest 回归单测到 `backend/tests/test_e2e_regression.py`
4. **双验证**：
   - 先跑 pytest 后端全量测试：`pytest backend/tests -x -q`（必须全过）
   - 再回到 Step X（对应场景的那一步）**单独重跑该场景**，失败 AC 现在必须通过
5. **留证**：修改 diff 保存到 `_e2e_tmp/diff_{NAME}.patch`

### Step 7：上传 ReqB_PRD（跨需求污染验证）+ 归档 A2 完整状态流转
- 上传 ReqB，归档
- `hybrid_searcher.search_requirements(query="审批流", product_id=A)` 命中 ReqB 但不命中 ReqA（因为按 product_id 过滤）
- A2 项目走 "active → pending-archive → cancel → active → archive" 完整 5 步，检查每步 status + 需求数量/FP 数量是否与 A1 相同
- 代码引用：`backend/app/api/products.py:917 pending-archive / 969 cancel / 1004 reactivate`

### Step 8：最终报告与清理（可选）
- 生成最终报告 `_e2e_tmp/final_report.md`：通过的 AC、失败的 AC、修复的补丁清单、总执行时间、证据位置
- **测试数据清理（默认保留 7 天，如需立即清理用户决定）**：DELETE `/products/{pid}/delete-check` 确认无 FK 残留 → DELETE `/products/{pid}`；若删除受限则保留 + 文档注明"该产品为测试产品，可安全删除"
- 检查 `git diff` 确认仅修改了 ClarifyAgent/测试等合法位置，**没写乱其它代码**

---

## Workspace setup
- 实施前运行：
  ```
  cd d:\git_down\ai-test-gen
  git status --short
  git branch --show-current
  docker-compose ps 2>&1 | tee backend/_e2e_tmp/docker_ps_before.log
  ```
- 如果 working tree 干净（git status 空），且计划会修改代码，先询问用户是否创建 worktree：
  `git worktree add -b codex/e2e-system-test ../ai-test-gen--e2e-system-test`
  （因当前用户在 Plan 模式中，**本步骤在用户批准 plan 后、进入实施阶段时再执行询问**，此处仅声明）
- 如果 working tree dirty → 必须先 stash / commit 用户已有改动，防止测试代码混进去。

---

## Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| 容器状态不健康（celery worker 不消费任务，Milvus 超时） | 中 | 高 | Step 0 预检 + 3 次 `docker-compose restart api celery` 重试；仍失败则降级 Option B（后端集成测试）并在最终报告中明确未覆盖的 AC |
| LLM 调用失败或返回 JSON 损坏（关键路径） | 中 | 高 | 测试文档设计尽量贴近 `_detect_logical_contradictions` 规则层兜底能力（矛盾 50 vs 不设上限、引用未定义 图片上传 2 处均为规则层能独立产出），即使 LLM 不可用也能产出问题；LLM 失败时在 Step 6 中作为 "降级路径正确" 单独一个 AC 子项记录 |
| 测试产品删除外键约束失败（requirements→FP→testcases 多对多） | 低 | 中 | 先单独建测试产品，不与已有 product 混用；如果 delete 失败就保留并在报告中明确该产品 ID 可手动清理 |
| 修复补丁改了主流程，破坏了已有 60+ 单测通过性 | 中 | 极高 | Step 6 强制"双验证"：后端单测全通过 + 原场景重跑；任何导致已有单测失败的补丁立即 revert，改用单步过滤器加在 clarify_agent.py 的单一函数内 |
| 多次上传版本后，Milvus 向量版本去重失败，RRF 时间衰减让 v1 被顶到前面（应该取最新）| 低 | 中 | 在 Step 3 归档时**手动给 ReqA_v1 写一个 3 天前的 go_live_date**，v2 写今天，v3 写今天 + 1 小时 → 查询时排序验证 v2/v3 在 v1 前；同时单测断言时间衰减逻辑存在（`hybrid_search._apply_time_decay` 被调用）|
| 用户澄清问题在 "暂不处理 → 跳详情页" 时导航失败（属于前端路径） | 低 | 中 | 如果 Option B 降级则无法覆盖，必须在报告中显式列出"前端跳转未自动化验证，推荐在实施阶段手工点击一次 UploadRequirementModal 的暂不处理按钮验证" |

---

## Verification steps（每条 AC 对应的可执行命令 / 断言）

1. **AC-1 隔离**：
   ```
   pytest backend/tests/test_e2e_system_regression.py -v -k product_isolation
   ```
2. **AC-2 文档构造**：
   ```
   ls backend/_e2e_tmp/docs/ && python -c "import markdown; [markdown.markdown(open(f).read()) for f in glob('backend/_e2e_tmp/docs/*.md')]"
   ```
3. **AC-3 归档**：脚本执行 `archive_project` 后，
   - `SELECT status, count(*) FROM requirements WHERE project_id=A1 GROUP BY status;` 仅 `archived` 非 0
   - `hybrid_searcher.search_requirements(..., product_id=A1)` 结果 >= 需求数
   - `cancel-pending-archive` 后状态回到 active，Milvus 查询不返回它
4. **AC-4 需求分析**：脚本里 Python 断言 SRR JSON 结构 + `FPGate(fp_list).run_checks() == True`
5. **AC-5 澄清质量**：脚本遍历 clarify questions，长度/位置/关键词/猜想正则 4 项断言
6. **AC-6 KB 合并正确**：
   - 脚本 Python：`v2_aug.图片上传规则 == v1_orig.图片上传规则`
   - `assert set(v2_aug.keys()) - set(v2_before.keys())  # 只有引用未定义相关字段允许新增`
7. **AC-7 影响分析**：
   - `受影响 FP 数 < 总 FP 数 * 60%`
   - `生成用例中 regression 数量 == 受影响 FP 关联的用例数量`
8. **AC-8 修复零侵入**：
   - `pytest backend/tests -x -q` 退出码 0
   - `git diff --stat` 中被修改文件 ∈ 白名单（clarify_agent.py / tests/ 等 5 个内）

---

## Pre-mortem（不适用，默认模式）
- 本 plan 默认模式，不强制 pre-mortem

## Expanded test plan（不适用，默认模式）
- 本 plan 默认模式，不强制 expanded test plan

---

## ADR

- **Decision**：新建隔离产品 E2E 走真实 REST API（Option A），容器失败时降级后端集成测试（Option B）。修复严格在 Step 6 执行，且遵循外科手术改动。
- **Drivers**：
  1. 数据隔离优先（用户明确"新建一个产品"）
  2. 修复零侵入（用户明确"不能破坏需求分析整体逻辑"）
  3. 场景覆盖完整度（4 份需求文档覆盖所有澄清场景 + 影响分析）
- **Alternatives considered**：
  - **Option A：真实 E2E** → Chosen，因为最贴近用户行为、能发现集成层 Bug、脚本可复用
  - **Option B：后端集成测试** → Rejected 作为主选项，降级备选；原因：无法验证 SSE / 前端跳转（如 UploadRequirementModal 暂不处理跳转需求详情）
  - **Option C：手工点击** → Rejected；原因：不可重现、无自动化回归
- **Why chosen**：用户给出的检查项（上传弹窗跳转、澄清问题列表、知识库查询页、影响范围分析）天然涉及交互层 / 状态流转 / 前后端状态同步，真实 API 路径才能覆盖。
- **Consequences**：
  - 正：测试覆盖度高，脚本可作为后续 CI 的冒烟测试
  - 负：执行时间较长，依赖容器健康
- **Follow-ups**：
  - 测试脚本整理后可放入 `scripts/e2e/` 作为回归固定流程（本次是否放入由用户后续决定，不在本 plan scope）
  - Milvus 时间衰减排序在本次测试中会做断言，若存在 bug 会在 Step 6 中作为单独补丁最小修复

---

## Review trail
- Planner draft v1：完整 8 步骤 + AC 细粒度 8 项 + 风险 6 条 + 双 Option + 零侵入修复策略
- （等待 Architect challenge / Critic verdict 在下一轮迭代）
