# 新产品「DevBlog 技术博客创作平台」全链路质量验证 - 规格说明（Spec）

## Overview
- **Summary**: 在 `ai-test-gen` 系统中新建独立产品「DevBlog 技术博客创作平台」，使用真实需求文档（V1.0 基线 + V1.1 迭代）端到端跑通：需求上传 → 需求分析（SRR/FP/澄清） → 确认归档 → 知识问答 → 依赖图谱 → 测试用例生成 → 质量验证（Q/R/M 系列）→ V1.1 版本变更后知识库与图谱增量迭代。所有产出（澄清问题、SRR、功能点、影响分析、归档向量、知识问答、测试用例、回归复用）必须进行质量核验，并根据实测结果**优化 TEST_PLAN.md** 第 12 阶段内容（新增"博客平台全新产品实测"章节与结果对照）。
- **Purpose**: 以完全独立的新业务域（技术博客平台，非现有工单系统）作为验证载体，覆盖 TEST_PLAN.md §3-11 全部阶段；同时在"干净产品"状态下验证阶段 6/7/8/9/10/11 的机制是否具备**产品级隔离与冷启动正确性**，并把本轮真实验证结果回填为测试计划的权威基线。
- **Target Users**: 系统测试工程师、产品质量负责人。

## Goals
1. 在与工单系统（PROD-53145-856）完全隔离的新产品下，成功完成 V1.0 上传→澄清→FP拆分→确认→归档→知识问答→图谱→用例生成全链路。
2. 完成 V1.1 迭代上传→重新生成→验证知识库按最新版本回答、图谱增量、用例版本基线收敛。
3. 对每个阶段的**产出质量**按 §6.6.1 通用清单逐项打分并留存证据。
4. 阶段 10/11（生成与质量验证/多版本迭代）在新产品下从零飞轮冷启动，验证阶段 11 Q/R/M 机制在首产品首生成场景下的正确性。
5. 基于上述实测结果，在 TEST_PLAN.md 新增「§20 阶段 12：全新产品（DevBlog）跨阶段基线实测」章节，补充通用核验项与冷启动飞轮基线。

## Non-Goals
- 不修改系统业务代码（除非出现按 P3 定义的**阻塞性问题**且必须最小修复；修复需在 TEST_REPORT.md 记录并回归）。
- 不使用已有工单系统的项目/需求/FP/向量/用例作为生成素材。
- 不使用脚本硬编码答案判定澄清收敛（严格按 §15.5 真实验收方式）。
- 不使用 mock 数据（除非用户明确授权），所有生成/检索/审核均基于容器真实环境 + 真实 LLM + 真实 Milvus/PG。

## Background & Context
### 载体文档
- V1.0 基线：`backend/tests/博客创作平台-产品需求文档PRD.md`（账户体系、内容创作管理、社区互动、搜索通知、数据统计 5 大功能域）
- V1.1 迭代：`backend/tests/博客创作平台-v1.1优化需求文档.md`（编辑器体验 8 项 OPT-A + 内容安全 3 项 OPT-B，含对 V1.0 规则的模糊引用「沿用现有上传规则」）
- 文档特点：**功能点名不与工单系统任何 FP 重名**（文章发布/Markdown 编辑/素材库/敏感词过滤 vs 工单受理/派单/SLA），可验证跨产品 FP 复用不污染（等价于 §17 M7 全新模块构造）。

### 环境基线（2026-09-01 实测）
- PG 现有产品仅 `PROD-53145-856 客户服务工单系统`（9 projects / 9 reqs / 22 FP / TestCases=352: approved=285, reviewed=39, draft=28）。
- 新建「DevBlog」产品后，两个产品的 FP/向量/用例**必须互不污染**。
- Milvus/后端容器 `ai-test-gen-api` 运行中、LLM 通道（日日新）可用。

### 约束来源
- TEST_PLAN.md §3.1：**所有测试一律在容器真实环境执行**（`docker exec -e PYTHONPATH=/app ai-test-gen-api python ...`）。
- TEST_PLAN.md §6.6.1：每轮必做产出质量 13 项通用核验。
- TEST_PLAN.md §10 阻塞性问题策略：阻断核心流程才最小修复，否则记录 Bug。
- 用户规则：提交最终结果前必须先测试是否符合要求；不使用模拟数据。

## Functional Requirements
- **FR-1 产品与项目创建**：通过标准 API 创建新产品「DevBlog 技术博客创作平台」与 V1.0 项目、V1.1 项目，版本号唯一。
- **FR-2 V1.0 上传与解析**：上传 PRD v1.0 后，异步解析至 `phase=done`，生成 raw_text/srr_json 草稿/澄清问题。
- **FR-3 澄清质量与一轮收敛**：按系统 options 或原文撰写回答（非脚本硬编码），提交后澄清一轮收敛（最多 3 轮兜底）。
- **FR-4 FP 拆分与审核/确认**：FP 拆分非 0、名称正确覆盖文档、模块分配后 approve，需求 `pending_review→confirmed`。
- **FR-5 影响分析正确性**：需求级 + FP 级影响分析正确显示依赖方向、被依赖/依赖者，与依赖图谱一致。
- **FR-6 归档与向量化完整**：项目归档后，Milvus 写入该需求的 requirement 向量 + 各 FP 向量，`kb_stats.total_requirements` 对应递增。
- **FR-7 知识问答正确性**：对 V1.0 归档 3 条固定问题，KB 命中并给出包含关键值的答案；V1.1 归档后同问题按**最新版本**回答。
- **FR-8 测试用例生成全链路（阶段10 T1-T9）**：`POST /api/generate` SSE 流式 9 节点 → 用例 → 审核 → 缺口补生成最多一次 → 二次审核 → 回归复用（V1.1 时）→ 固化 → 6 格式导出 → CRUD 反馈。
- **FR-9 用例质量与多版本迭代（阶段11 Q/R/M）**：三档置信度分流正确、source_pattern 100% 非空、draft↔ErrorCase 一一对应、回归用例不重复入库、飞轮 PG approved=Milvus、V1.0→V1.1 版本变更后用例覆盖新值、跨批次置信度向好。
- **FR-10 跨产品隔离**：DevBlog 产品的 FP/向量/用例查询结果绝不包含 PROD-53145-856 的数据，反之亦然。
- **FR-11 计划文档优化**：在 TEST_PLAN.md 新增 §20 阶段 12，包含本轮各阶段 AC 通过情况矩阵、冷启动飞轮基线（首批次置信度分布/入库分流）、与 §19.2 工单系统基线的横向对比表、计划 §15.5 澄清收敛方法学在新产品下的再验证结论。

## Non-Functional Requirements
- **NFR-1（质量门槛）**：每阶段按 §6.6.1 通用核验清单不达标项 ≤ 2 条（单项除外，按实际）；澄清问题重复率 < 10%、冗余率 < 15%。
- **NFR-2（性能）**：上传→解析完成 ≤ 15 分钟；澄清提交→重解析完成 ≤ 10 分钟；归档→向量化完成 ≤ 3 分钟；generate→done ≤ 20 分钟（LLM 限流除外）。
- **NFR-3（可追溯）**：所有中间产物（tokens、req_id、fp_id、task_id、gen_batch_id、向量 ID）持久化到 `backend/_e2e_tmp/artifacts/blog_*.json`，可复测。
- **NFR-4（稳定性）**：全链路无 5xx 阻断性异常；如有 P3 修复后必须最小回归通过。

## Constraints
- **Technical**: 仅在容器 `ai-test-gen-api` 真实环境执行；后端代码修改仅限 P3 最小修复；不使用 mock。
- **Business**: 新产品 ID/名称必须与 PROD-53145-856 隔离；所有 DevBlog 数据保留不删除；不使用 `build_answer` 硬编码答案判断澄清收敛。
- **Dependencies**: 依赖 `日日新` LLM 通道、Milvus 服务、Postgres、docker 运行状态；容器 `ai-test-gen-api` 处于 running 且带 `--reload`。

## Assumptions
- 容器 `ai-test-gen-api` 与 Milvus/Postgres 运行正常；LLM 配额在 30 RPM 以上（允许 429 自动重试）。
- 文档 `博客创作平台-产品需求文档PRD.md` / `博客创作平台-v1.1优化需求文档.md` 内容完整、可直接上传。
- 系统测试账号 `admin/888888` 可用；admin 权限足以执行全部操作。
- 若 DevBlog 同名产品已存在，改为追加后缀「_DevBlog_<timestamp>」并记录。

## Acceptance Criteria

### AC-1: 新产品与项目创建成功
- **Type**: `rule`
- **Given**: admin 已登录且 JWT 有效
- **When**: 调用 `POST /api/products` 创建 DevBlog 产品，其后创建 V1.0（版本 v1.0）与 V1.1（版本 v1.1）两个项目
- **Then**: 返回产品 id、两项目 id；产品名称不与现有工单系统重名；两项目版本号在该产品下唯一且创建成功（HTTP 200/201）
- **Pass Condition**: 产品 + 两项目均存在且可查询；产品 id ≠ PROD-53145-856
- **Evidence**: `artifacts/blog_state.json` 包含 product_id / v1_proj_id / v11_proj_id

### AC-2: V1.0 上传解析至 done，SRR/FP 非空且覆盖主功能域
- **Type**: `rule`
- **Given**: V1.0 项目存在
- **When**: 上传 PRD v1.0 并轮询 `GET /requirements/tasks/{task_id}` 至 phase=done
- **Then**: 需求存在，`GET /requirements/{req_id}/fp` 返回 FP 数量 ≥ 5，且至少覆盖「账户体系/内容创作/社区互动/搜索通知/数据统计」中的 4 个域；SRR JSON 含 requirement.title / flows / business_rules
- **Pass Condition**: fp_count ≥ 5 且 4/5 域被覆盖，SRR 关键字段非空
- **Evidence**: `artifacts/blog_v1_parse.json` 记录 req_id / fp_list / srr_summary

### AC-3: 澄清质量与真实一轮收敛
- **Type**: `rubric`
- **Dimension**: 澄清问题质量（清晰度 + 必要性 + 覆盖度 + 重复率 + 收敛方式）
- **Scale**: 0-5
- **Anchors**: 1 = 存在 3+ 项明显对已明确内容提问（冗余），或收敛仍依赖脚本硬编码答案；3 = 问题基本清晰、冗余率 10%-20%、一轮收敛由系统 options/原文驱动；5 = 问题均锚定具体页面/功能点、无冗余、重复率<10%、由 options/原文一轮直接收敛
- **Pass Threshold**: >= 4
- **Evidence**: 回答来源审查表（每条问题来源=system_options | written_by_original | script_driven，script_driven 比例=0）+ 重复率统计 + 冗余率统计，详见 `artifacts/blog_v1_clarify.json`

### AC-4: FP 分配模块 + approve 后，需求可 confirm 成功（状态机正确）
- **Type**: `rule`
- **Given**: 澄清收敛完成（状态 pending_review）
- **When**: 对全部 FP `PUT .../fp/{fp_id}` 分配 modules → 全量 `PATCH .../fp/{fp_id}/approve` → `POST .../confirm`
- **Then**: 全部 FP status=approved；confirm 200；需求 status=confirmed；无 409 前置失败
- **Pass Condition**: 所有 FP approved，需求 confirmed
- **Evidence**: `artifacts/blog_v1_confirm.json` = approve 批量结果 + confirm HTTP 200 响应

### AC-5: 影响分析与依赖图谱正确性（I1/I2/I3/I4/I6，I7 按需视实际跨需求边）
- **Type**: `rule`
- **Given**: 需求已 confirmed
- **When**: `POST /requirements/{req_id}/impact-analysis` + 典型 FP `GET /products/{pid}/fp/impact?fp_id=X`
- **Then**: 需求级影响分析符合跨需求语义（不列本需求 FP 或若 V1.0 作为首个需求则 total_affected=0 合理）；FP 级列出 affected_fps 并含 direction（outgoing/incoming）/ relation_cn / depth，与依赖边一致；前端可打开图谱查看方向徽标
- **Pass Condition**: 语义与方向字段符合，图谱不报错
- **Evidence**: `artifacts/blog_v1_impact.json` = impact_analysis 原始响应 + FP 级受影响清单

### AC-6: 归档→向量化完整，KB 递增
- **Type**: `rule`
- **Given**: 项目下所有需求 confirmed
- **When**: `POST /products/projects/{proj_id}/archive` → 等待向量化异步（最多 3 分钟） → `GET /knowledge/stats`
- **Then**: archive 返回 kb_failed=[]，kb_written ≥ 1；stats.total_requirements 较归档前递增（1+FP数）；Milvus 查该 req_id 向量 metadata 含 type=requirement/project_id/product_id/status
- **Pass Condition**: kb_failed=[] + 递增 + Milvus 向量存在
- **Evidence**: `artifacts/blog_v1_archive.json` = 归档响应 + 前后 stats diff + Milvus 查询结果

### AC-7: 知识问答命中 V1.0 并含关键值，V1.1 后按最新版本回答
- **Type**: `rule`
- **Given**: V1.0 已归档（步骤 A）、V1.1 也已归档（步骤 B）
- **When**: 两个时点分别调用 `POST /knowledge-qa/ask`（SSE），固定提问集：
  - Q1「平台图片上传支持哪些格式？单文件大小限制多少？素材库总配额？」
  - Q2「敏感词校验规则是什么？分几级？」
  - Q3「文章历史版本对比与回滚规则是什么？手动标记版本最多保留多少个？」
- **Then**: A 时点（仅 V1.0）命中 V1.0 关键值；B 时点 V1.1 已归档后，Q1/Q3 答案采纳 V1.1 的最新规则（素材库总配额 2GB、手动版本 ≤ 50 个），**不再返回 V1.0 旧值或「沿用现有」模糊表述**
- **Pass Condition**: A 命中≥2/3 含关键值；B 时 Q1/Q3 最新版本采纳正确
- **Evidence**: `artifacts/blog_qa_v1.json` 与 `blog_qa_v11.json` = SSE 解析出的 sources/answer 片段，以及关键值断言

### AC-8: 测试用例生成全链路通过（阶段 10 T1-T9）
- **Type**: `rule`
- **Given**: V1.0 需求 confirmed、impact_analysis 条目全部带 confirmed_at
- **When**: 发起 `POST /api/generate`（SSE 流式，Bearer），解析 9 节点 stage 至 done
- **Then**: 
  - T1：异常前置（404/两档 409）严格命中，confirmed 状态可正常生成
  - T2：Content-Type=text/event-stream，stage 顺序 parse→select_channel→clarify→gate→impact→test_design→generate→review→done
  - T3：test_design 含 ≥ 3 种测试技术标识
  - T4：cases 覆盖全部功能点（related_fp 完整）；case 结构完整（title/preconditions/steps/expected/priority）
  - T5：补生成 ≤ 1 次，最终回流为第二次审核结果
  - T6：（V1.0 首轮无历史用例 → 缺口补生成闭环；V1.1 时验证回归复用 RTS）
  - T7：归档后产品+FP 维度 latest approved 用例集可查询
  - T8：6 格式（json/markdown/gherkin/pytest/xmind/xmind8）导出全部成功且内容正确
  - T9：用例 CRUD + 单/批量 feedback 端点可用
- **Pass Condition**: T1-T9 全部按上述断言通过
- **Evidence**: `artifacts/blog_v1_generate.json` = SSE 全流日志、用例列表结构、导出文件样本、feedback 响应

### AC-9: 阶段 11 Q/R/M 机制在新产品冷启动下符合语义
- **Type**: `rule`
- **Given**: V1.0 与 V1.1 都完成 generate、归档、用户反馈闭环
- **When**: 逐条比对 done 事件 cases 分项分数 → SQL 按批次分组 → 查 Milvus test_cases 集合 → M2 版本变更用例预期值审查
- **Then**:
  - Q1：置信度公式逐例复算匹配（含 pattern_bonus 与 [0,1] 钳制）
  - Q2：三档分流无越档（≥0.70无blocker→approved；其余按规则→reviewed/draft）
  - Q3：FP coverage 归一化后 coverage_rate=1.0 或缺口被补生成闭环
  - Q4：source_pattern 非空率 100%，LLM 已填不被兜底覆盖
  - Q5：draft 数 == ErrorCase 数，reason=confidence_below_threshold 等语义对齐
  - Q6：feedback(reviewed→approve) 后下一轮 match_score 提升
  - R1：相似用例检索仅命中 DevBlog 产品（product_id 过滤）
  - R2：RTS 回归复用 summary 数值与 aggregate_latest_cases 一致（V1.1 时 reused_count>0）
  - R3：回归复用用例不重复入库（PG FP+case_type 唯一性）
  - R4：DevBlog 产品 PG approved 数 == Milvus test_cases 数（飞轮双写）
  - M1：每条用例带 gen_batch_id，可追溯批次
  - M2：V1.1 生成的「素材库 2GB/50 个手动标记」用例预期值=新值，V1.0 旧值未残留为最新基线
  - M3：未受影响 FP 回归用例取 V1.0 go_live_date 最新项目基线
  - M4：同 FP V1.0→V1.1 confidence 单调向好
  - M5：Milvus DevBlog 用例向量检索结果不包含工单系统用例
- **Pass Condition**: Q1-Q6/R1-R4/M1-M5 断言按实际全部通过（V1.0 首轮冷启动下 M4 可单版本跳过，V1.1 时必须满足）
- **Evidence**: `artifacts/blog_quality_*.json` = 置信度复算表、SQL 批次分布、Milvus 双写一致性核对、M2 新旧预期值对比

### AC-10: 跨产品数据隔离（M7 在 KB/TC 维度拓展验证）
- **Type**: `rule`
- **Given**: DevBlog 产品完成 V1.0 归档 + 用例生成归档
- **When**: ①对 DevBlog 产品检索 `/knowledge-qa/ask` 时，检查 sources 中的 req_id/project_id 是否均属于 DevBlog；②`GET /testcases?product_id=<DevBlog_id>` 响应 case 的 product_id 全部等于 DevBlog_id；③反向对工单系统的检索/查询不含 DevBlog 数据
- **Then**: 三类检索无跨产品污染（一条都不允许混入）
- **Pass Condition**: 0 条越界
- **Evidence**: `artifacts/blog_product_isolation.json` = 三类查询过滤校验明细

### AC-11: TEST_PLAN.md 新增 §20 阶段 12 并记录本轮真实基线
- **Type**: `rule`
- **Given**: 上述 AC-1~AC-10 所有证据文件存在
- **When**: 编辑 TEST_PLAN.md，于 §19 后新增 §20「阶段 12：全新产品（DevBlog）跨阶段基线实测」与附录 B 追加条目
- **Then**: 新增章节包含：
  - §20.1 背景与目标（为什么需要全新产品基线）
  - §20.2 产品与项目清单（ID/版本/文档）
  - §20.3 通用清单 §6.6.1 逐项结果矩阵（V1.0/V1.1 两版本）
  - §20.4 阶段 10/11 冷启动实测（首生成批次置信度分布、三档分流数、飞轮双写数、source_pattern 非空率）
  - §20.5 与 §19.2 工单系统基线横向对比（冷启动 vs 多轮累积）
  - §20.6 越用越好用指标（V1.0→V1.1 KB 命中率、澄清轮次、FP 置信度演进曲线）
  - §20.7 通过标准（与 AC-1~AC-10 对应项列表）
  - 附录 B 新增 DevBlog 产品/项目/需求/用例保留清单
- **Pass Condition**: TEST_PLAN.md 可在 diff 中看到上述 8 个小节已写入且引用的 ID/数值与证据一致
- **Evidence**: `git diff TEST_PLAN.md` 输出

### AC-12: 非阻塞问题与自动发现测试点记录
- **Type**: `rubric`
- **Dimension**: 测试记录完整性（Bug 清单 + 自动发现测试点 + 阻塞修复记录 + 每阶段质量矩阵）
- **Scale**: 0-5
- **Anchors**: 1 = 只说结论无证据；3 = 有证据但结构混乱；5 = 按 TEST_PLAN.md §8 报告结构完整记录，每条 Bug 附复现/预期/根因，每阶段质量矩阵附统计值
- **Pass Threshold**: >= 4
- **Evidence**: `TEST_REPORT.md` 相应章节更新（或在本规格 artifacts 目录生成结构化中间报告，最后再合并入根目录 TEST_REPORT.md）

## Open Questions
- [ ] 若创建产品时发现「DevBlog 技术博客创作平台」同名已存在（非预期），是否允许使用后缀 `_20260901` 命名？（计划默认：允许）
- [ ] 若首轮 generate 冷启动下 confidence 普遍低于 0.70（Q2 三档分流会大量 reviewed/draft），是否需要批量人工 feedback 启动飞轮？（计划默认：按 Q6 操作对 reviewed 批量 approve，再跑第二轮，以验证飞轮正向闭环）
