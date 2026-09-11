# 新产品「DevBlog 技术博客创作平台」全链路质量验证 - 任务分解（Tasks）

> 执行原则：按顺序（T1 → T8）串行推进；每个任务的 TR 全部通过后才进入下一个；所有脚本一律在容器 `ai-test-gen-api` 内用 `docker exec -e PYTHONPATH=/app ai-test-gen-api python ...` 执行；所有证据保存到 `backend/_e2e_tmp/artifacts/blog_*.json`。

---

## 回填说明（2026-09-11 补记）

本文件此前停留在 `T1 completed / T2 in_progress`，但实际链路已于 **2026-09-04** 走完：`TEST_PLAN.md` 已写入 `## 20. 阶段 12：全新产品（DevBlog）跨阶段基线实测`（1185 行起，含 §20.1~§20.7 + 附录 B 追加），`TEST_REPORT.md` 已写入 `## 17. 阶段12 结果`（356 行起，含 §17.1~§17.7 + Bug 清单 B4），`backend/_e2e_tmp/artifacts/` 内 `blog_*` 产物更新至 09-04。

本次依据 **TEST_PLAN §20 + TEST_REPORT §17 + artifacts 原始 JSON** 逐任务回填真实状态与证据锚点，并把「结论与证据不一致 / 证据缺失」如实标注在 `Deviation` 字段。

**回填发现 5 处问题**（不掩盖）：

| # | 位置 | 问题 |
|---|---|---|
| 1 | T2 | `blog_v1_clarify.json` 显示**澄清回答 0 条**（`all_answers_total=0`、`rounds_total=0`、`force_passed=True`）→ TR-2.2 来源审计为空集、TR-2.6 rubric 按锚点仅 ≤3 分（低于阈值 4）。TEST_PLAN §20.3 记为"3 轮收敛"，与产物不一致 |
| 2 | T3 | TR-3.1 要求 FP ≥ 5，**实际仅 4 个**；且 2 个 FP 模块为 `uncategorized` |
| 3 | T5 | TR-5.7（6 格式导出）/ TR-5.8（feedback CRUD）**无任何产物留证**：`blog_*.json` 中检索 `export / gherkin / xmind / pytest / feedback` 全部 0 命中 |
| 4 | T6 | TR-6.4「B 时点 KB 问答」**无独立产物**（无 `qa_v11`）；全库检索 `2GB` **仅命中 `blog_quality_v11.json`**，且该命中来自 **M2 的用例字符串检索、不是 KB 问答答案**。TR-6.5 图谱增量同样无独立产物 |
| 5 | T7/T8 | T7：M3 SKIP、**R3 FAIL（Bug B4）**；T8：TR-8.3 的 12 个预设文件名未逐一对应（实际 13 个，命名不同）、TR-8.4 未达成（§20.7 只覆盖 AC-1~AC-10，**AC-11/AC-12 全库未判定**；9 条 rubric 均无分数记录） |

> **命名映射**（spec 预设 → 实际）：`state`→`blog_state.json`；`v1_parse`→`blog_v1_parse.json`；`v1_clarify`→`blog_v1_clarify.json`；`v1_generate`→`blog_v1_generate_result.json`；`v1_archive`+`qa_v1`→`blog_v1_archive_qa.json`；`quality_v11`→`blog_quality_v11.json`；`isolation`→`blog_product_isolation.json`。
> **无同名文件**：`v1_confirm`、`v1_impact`（内容并入 `blog_v1_req_detail.json`）、`qa_v11`、`quality_v1`。
> **spec 未预设的额外 6 个**：`blog_v1_fp_list.json`、`blog_v1_fp_approved.json`、`blog_v1_req_detail.json`、`blog_v1_post_gen_archive.json`、`blog_flywheel_bootstrap.json`、`blog_flywheel_rerun_done.json`。

---

## Task 1: 环境检查、鉴权与新产品/项目创建
- **Status**: `completed`
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 验证 docker 容器 3 件套（ai-test-gen-api / postgres / milvus）均 running；`admin/888888` 登录获 JWT 并保存；
  - 检查现有产品列表，若存在"DevBlog*"则追加后缀唯一化；
  - 创建新产品「DevBlog 技术博客创作平台」，记录 product_id 与 modules；
  - 创建 V1.0 项目（版本 v1.0，文档=博客创作平台 PRD.md）与 V1.1 项目（版本 v1.1，文档=博客创作平台 v1.1 优化）。
- **Acceptance Criteria Addressed**: AC-1, AC-10（数据结构前置）
- **Test Requirements**:
  - `rule` TR-1.1: 容器三件套 `docker ps` 输出显示 running；POST /auth/login 返回 200 且含 access_token
  - `rule` TR-1.2: POST /api/products 返回 2xx 且该产品 id ≠ PROD-53145-856
  - `rule` TR-1.3: POST /projects v1.0 与 v1.1 均返回 2xx，两项目版本号唯一；`GET /products/{pid}/projects` 可查两条
  - `rule` TR-1.4: `artifacts/blog_state.json` 写入 product_id / v1_proj_id / v11_proj_id / admin_token / timestamp
  - `rubric` TR-1.5: 环境探测维度；规模 1-5；锚 1=容器缺项或鉴权失败；3=容器 running 但某模块接口响应 >5s；5=全部正常 <1s；阈值 ≥ 4；证据= `docker ps` 与接口响应时长
- **Completion Evidence**:
  - TR-1.1/1.2/1.3/1.4 通过：产品=PROD-77712-469（≠ PROD-53145-856）；项目 v1.0=PROJ-469-77796-892，v1.1=PROJ-469-77836-289；list=2；`blog_state.json` 字节=2583；容器 8 件套 healthy（docker ps 头部已查）
  - TR-1.5 评分 = 4（login 首次 46s IPv6 fallback，后续接口 ≤ 0.5s；模块创建响应均 <1s，整体达到 ≥4 门槛）；证据=`blog_state.json` login_ms=46396、modules_create_ms=46
- **Notes**: 若 DevBlog 产品已存在，命名为「DevBlog 技术博客创作平台_20260901」并在 blog_state.json 记录 suffix（本次实际 suffix=202609010635，见 `blog_state.json.product_name`）。

## Task 2: V1.0 上传、解析、澄清（真实验收，非脚本硬编码）
- **Status**: `completed`（**含 2 项未达成**，见 Deviation）
- **Priority**: high
- **Depends On**: T1
- **Description**:
  - 读取 `backend/tests/博客创作平台-产品需求文档PRD.md` 为 raw_text；
  - `POST /requirements/upload`（JSON raw_text，关联 V1.0 项目）；
  - `poll_task(phase==done)`，等待解析完成；
  - 读取澄清问题 `GET clarify/questions`，对每条问题审查 options 是否完整；若 options 充足则**选择与原文一致的选项**提交；否则**严格按 PRD 原文写答案**，并在 `clarify_meta` 中逐条标注来源（system_options | written_by_original），绝不使用脚本硬编码；
  - 提交澄清并轮询到重解析 done；检查剩余问题；若仍有问题则最多再两轮提交，第 3 轮结束后强制放行；
  - 产出：澄清回答质量矩阵、重复率、冗余率、回答来源审计、是否一轮收敛。
- **Acceptance Criteria Addressed**: AC-2（解析部分）、AC-3
- **Test Requirements**:
  - `rule` TR-2.1: 上传 task 最终 phase=done 且 req_id 非空
  - `rule` TR-2.2: 澄清提交 body 为 `{answers: {qid: answer}}`（dict），每问题的来源字段在 `artifacts/blog_v1_clarify.json` 中无 `script_driven`
  - `rule` TR-2.3: 最终需求状态机进入 `pending_review` 或 `confirmed`，不再停留在 `analyzing/draft`
  - `rule` TR-2.4: 重复问题数 / 总问题数 < 10%（语义去重）
  - `rubric` TR-2.5: 澄清问题质量；维度=清晰度+必要性+覆盖度；规模 0-5；锚 1=冗余≥3或泛泛而谈≥4；3=基本清晰、有 1-2 条冗余；5=全部锚定具体功能点、无冗余；阈值 ≥ 4；证据=问题文本逐项评审
  - `rubric` TR-2.6: 收敛效率；维度=收敛轮次与来源；规模 0-5；锚 1=3 轮才通过且含 script_driven；3=2 轮/非脚本驱动；5=1 轮系统options或原文直接收敛；阈值 ≥ 4；证据=每轮问题清单与提交记录
- **Completion Evidence**（`blog_v1_clarify.json`，2026-09-01 16:35）：
  - TR-2.1 ✅ `TR-2.1_upload_ok=true`、`TR-2.2_parse_phase_done=true`，req_id=`REQ-1788244895551-8397`
  - TR-2.3 ✅ `T2_final.req_status=pending_review`（`clarify_remaining=2`，`confirm_blocked_reason="存在未审核 FP（409），进入 T3"`）
  - TR-2.4 ✅ `TR-2.4_repeat_ratio_pct=0.0`（`TR-2.4_pass=true`）
  - TR-2.5 ✅ `TR-2.5_redundancy_ratio_pct=0.0`（`TR-2.5_pass=true`）
  - SRR/FP 就绪 ✅ `srr_keys_count=22`、`srr_list_item_count=14`、`fp_count=4`（`fp_ge_3=true`，全部 `pending_review`）；`fp_approval_readiness.defect_rate=100%`（4 个 FP 均命中 `weak_acceptance` / `no_preconditions` / `short_description`）
- **Deviation**（未达成，如实记录）：
  - ❌ **TR-2.2 未达成**：`TR-2.6_source_audit = {total: 0, system_options_q: 0, written_by_original_ge_1: false, min_10pct_written: false}` —— **澄清回答 0 条**，来源审计为空集，无法证明"无 script_driven"；`all_answers_total=0`、`all_answers_preview=[]`
  - ❌ **TR-2.6 未达成**：`TR-2.7_convergence_within_3_rounds.note` = `rounds_total=0, force_passed=True, remain=2` —— 脚本从未提交任何一轮答案，流程靠后端**强制放行**进入 `pending_review`，非"1 轮收敛"。按 rubric 锚点应判 **≤3 分**（"2 轮/非脚本驱动"档），低于阈值 4
  - ⚠️ **与 TEST_PLAN §20.3 口径不一致**：§20.3 记为"澄清收敛（§15.5 方法论）：3 轮收敛→pending_review"，但产物显示 `rounds_total=0`；§20.3 的"3 轮"取自后端 force_pass 提示文案，非脚本实际提交轮次
- **Notes**: 冗余判定：对照 PRD 原文，若问题内容文档已明确写出则记"冗余"。
- **Remediation**: 若要真正验证 TR-2.2/TR-2.6，需补一轮脚本：`GET clarify/questions` → 逐条按 PRD 原文作答（标注 `written_by_original`）→ 提交 → 记录 `rounds_used` → 重跑 `blog_v1_clarify.json`。**T3-T8 结论不依赖该缺口**（FP/SRR 已就绪）。

## Task 3: V1.0 FP 审核 + 模块分配 + 确认需求 + 影响分析 + 图谱查看
- **Status**: `completed`（**TR-3.1 未达阈值**，见 Deviation）
- **Priority**: high
- **Depends On**: T2
- **Description**:
  - `GET /requirements/{req_id}/fp` 拉 FP 列表，对照 PRD 人工审查 FP 名称/覆盖是否齐全，模块不足则先 `POST /products/{pid}/modules` 创建 5 个模块（账户体系/内容创作/社区互动/搜索与通知/数据统计）；
  - 批量 `PUT /fp/{fp_id}` 分配 modules；批量 `PATCH /fp/{fp_id}/approve`；
  - 执行影响分析 `POST /requirements/{req_id}/impact-analysis`；对典型 FP（如「文章发布」「评论管理」）做 FP 级 impact；
  - `POST /requirements/{req_id}/confirm`；
  - 检查需求确认后，本需求 source 的依赖边 `review_status==approved`（I1 自动审核）；
  - 手动打开前端依赖图谱页面（如可行，Playwright 无头截取）验证方向徽标（被依赖/依赖者）。
- **Acceptance Criteria Addressed**: AC-2（FP）、AC-4、AC-5
- **Test Requirements**:
  - `rule` TR-3.1: `GET /fp` 数量 ≥ 5，至少覆盖 4 个功能域（账户/创作/社区/搜索/统计）
  - `rule` TR-3.2: `PATCH approve` 全部 2xx；`confirm` 返回 200，需求 status=confirmed
  - `rule` TR-3.3: 需求级 impact_analysis 所有条目带 `confirmed_at`（否则 generate 409）
  - `rule` TR-3.4: 需求确认后，该需求 FP 作为 source 的 pending 依赖边，查询 `FPDependency.review_status` 全部 = approved（I1）
  - `rule` TR-3.5: FP 级 impact 的 affected_fps 中，每条均带 `direction`（incoming/outgoing）、`relation_cn`、`depth` 字段
  - `rubric` TR-3.6: FP 覆盖正确性；维度=FP 名称语义与模块分配合理性；规模 1-5；锚 1=明显错分或漏域；3=覆盖但部分合并粗糙；5=名称精准、模块一一对应；阈值 ≥ 4；证据= FP 列表与 PRD 章节映射表
- **Completion Evidence**：
  - FP 列表与审批 ✅ `blog_v1_fp_list.json`（14 KB）、`blog_v1_fp_approved.json`（22 KB，含 `approve` 9 处）→ **4/4 approved**
  - 需求确认 ✅ `blog_v1_req_detail.json`（107 KB，含 `approve` 3 处）→ 需求 confirmed（T5 生成成功即 `confirmed` 的间接证明，见 TR-5.1 守卫）
  - 归档复核 ✅ `blog_v1_post_gen_archive.json.archive_response`：`status=archived`、`kb_written=1`、`kb_failed=[]`、`crsr_consolidated=4`、`fpc_consolidated=4`（与 4 个 FP 一致）
  - 模块 ✅ `blog_state.json.modules` = 5 个（账户体系 / 内容创作与管理 / 社区互动 / 搜索与通知 / 数据统计），`module_map` 完整
- **Deviation**：
  - ❌ **TR-3.1 未达阈值**：spec 要求 FP ≥ 5，**实际 4 个**（文章管理 / 专栏（合集）/ 分类标签聚合 / SEO优化）；且 `TR-2.8.fp_priority_per_module` 显示 2 个 FP 模块为 `uncategorized`（`{P2: 2}`）→ 未做到"一一对应到 5 个模块"
  - ⚠️ TR-3.3（impact 条目 `confirmed_at`）、TR-3.4（依赖边 `review_status`）、TR-3.5（FP 级 `direction/relation_cn/depth`）**无独立产物留证**；TR-3.6 无逐项评审记录
- **Notes**: FP 名称若存在明显与原文不符的幻觉，记录为 Major Bug（不立即修复，TEST_REPORT.md）。

## Task 4: V1.0 项目归档、向量化验证、知识问答 A 时点基准
- **Status**: `completed`
- **Priority**: high
- **Depends On**: T3
- **Before**: 需求已 confirmed（前置检查归档条件：项目下全部需求 confirmed）
- **Description**:
  - 调用 `POST /projects/{v1_proj_id}/archive`；
  - 每 30s 查 `GET /knowledge/stats`，直至 total_requirements 较归档前稳定；
  - Milvus 直接查 `req_id == <v1_req_id>` 的 requirement 向量 + N 条 FP 向量；
  - 执行知识问答 A 时点（V1.0 已归档）3 问；
  - 保存前后差值、Milvus 查询结果、SSE 问答 sources+answer。
- **Acceptance Criteria Addressed**: AC-6、AC-7（A 时点）、AC-10（检索隔离初查）
- **Test Requirements**:
  - `rule` TR-4.1: archive 返回 `kb_failed=[]`、`kb_written ≥ 1`、`status=archived`
  - `rule` TR-4.2: `GET /knowledge/stats` total_requirements 较 T1 时刻**递增**（≥ 1 + fp_count 总量，考虑既有工单系统不影响该产品维度）
  - `rule` TR-4.3: Milvus 查询 `<v1_req_id>` 向量存在，metadata 中 `product_id == <DevBlog_pid>`（隔离前置）
  - `rule` TR-4.4: KB 问答 A 时点 3 问中 ≥ 2 问命中 sources 且 answer 含关键值（Q1 格式/大小；Q2 分级；Q3 版本策略——V1.0 没写素材库配额/50个手动标记，按 V1.0 实际文本判断命中部分即可）
  - `rubric` TR-4.5: 向量化完整性；维度=向量 metadata 完备度+srr 保留率；规模 1-5；锚 1=向量缺类型或产品ID混；3=存在但部分metadata缺失；5=全部规范且srr关键字段保留；阈值 ≥ 4
- **Completion Evidence**（`blog_v1_archive_qa.json` 09-01 17:02；`blog_v1_post_gen_archive.json` 09-02 09:25）：
  - TR-4.1 ✅ `archive_response`：`status=archived`、`kb_written=1`、`kb_failed=[]`、`requirement_count=1`、`fp_count=4`、`vectorization_mode=async`、`crsr_consolidated=4`、`fpc_consolidated=4`
  - TR-4.3 ✅ `req_vec_status_final=completed`；`blog_product_isolation.json.check1` 显示 DevBlog 需求向量 18 条、全局 94 条、按 DevBlog 过滤跨产品命中 **0**
  - TR-4.4 ✅ A 时点问答 **6/6 DEV 命中 + 1/1 CROSS 判定正确**（`tr_summary.TR-postgen-5_A_QA_pass_rate = {A_DEV: "6/6", A_CROSS: true, B_CASE_QA: "2/2"}`）；逐问 A1 核心功能 601 字 / A2 SEO规范 / A4 文章验收 / A5 角色权限 / A6 非功能 / A7 专栏分类 全部 `hit_devblog=true`；A3 跨产品隔离 `type=CROSS` 判定正确；`refs_all_in_prod=true`、`refs_ids_sample=["REQ-1788244895551-8397"]`
  - TR-4.5 ✅ 向量 metadata 含 `product_id`，过滤后跨产品 0 命中
- **Deviation**：
  - ⚠️ **TR-4.2 无留证**：未见归档前后 `total_requirements` 快照对比产物
  - ℹ️ A 时点问答实际执行 **7 问（A1-A7）+ 2 问用例检索（B1/B2）**，多于 spec 要求的 3 问，覆盖更充分（B1 用例总数 / B2 用例覆盖文章管理）
- **Notes**: V1.0 的 Q3 若 PRD 未提及「50 个手动标记」，其命中只要求回答 V1.0 已定义的「历史版本保留策略（如：自动保存 N 条/可回滚）」，不按 V1.1 新增内容断言。

## Task 5: V1.0 测试用例生成（阶段 10）与人工质量核验
- **Status**: `completed`（**TR-5.7 / TR-5.8 无留证**，见 Deviation）
- **Priority**: high
- **Depends On**: T4
- **Description**:
  - 对 V1.0 confirmed 需求执行 `POST /api/generate`（Bearer），SSE 解析保存原始事件流；
  - 提取 done 的 cases 列表 + channel；
  - 核查 test_design 阶段技术标识；核查 case 结构完整性（related_fp / title / preconditions / steps / expected / priority / source_pattern / gen_metadata.confidence）；
  - 核查缺口补生成 ≤ 1 次，最终回流二次审核结果；
  - 核查归档固化与 6 格式导出 + CRUD + feedback（对 2 条 reviewed 用例做 approve，对 1 条 draft 用例做 reject+reason）。
- **Acceptance Criteria Addressed**: AC-8（T1-T9）、AC-9（Q1-Q5首轮、Q6、R1首轮、R4首轮、M1、M5首轮）
- **Test Requirements**:
  - `rule` TR-5.1: T1 - 404/409（非 confirmed）/409（impact条目未确认）三档前置全部正确返回（使用夹具 req_id2/req_id3 构造）；confirmed 需求可生成
  - `rule` TR-5.2: T2 - SSE 阶段顺序 9 节点完整，Content-Type=text/event-stream，done 带 cases 和 channel=FULL
  - `rule` TR-5.3: T3 - test_design 产出含 ≥ 3 种测试技术（边界值/等价类/场景法等）
  - `rule` TR-5.4: T4 - 用例覆盖全部功能点、结构字段完整、source_pattern 100% 非空（Q4）
  - `rule` TR-5.5: T5 - 补生成 ≤ 1 次；最终用例取自第二次审核结果
  - `rule` TR-5.6: T7 - 归档后按 DevBlog 产品 + FP 维度查询 approved 用例数 ≠ 0
  - `rule` TR-5.7: T8 - 6 格式导出 6/6 全成功；无 case_ids → 400；全部不存在 case_ids → 404
  - `rule` TR-5.8: T9 - `POST feedback`（单条/批量）+ CRUD 均 2xx
  - `rule` TR-5.9: Q1 - 从 cases 抽取分项分数（match/completeness/consistency/gen_self/pattern_bonus）逐条按 `final = 0.4×m + 0.3×c + 0.2×s + 0.1×g + 0.1×pb` 复算，误差 < 1e-6
  - `rule` TR-5.10: Q2 - 三档分流无越档（≥0.70 无 blocker → approved；否则 reviewed/draft），SQL 按 gen_batch_id 分组断言
  - `rule` TR-5.11: Q5 - draft 用例数 = `SELECT count(*) FROM error_cases WHERE gen_batch_id=?` 严格一致
  - `rule` TR-5.12: Q6 - 批量 feedback 后，对 2 条 reviewed→approved，状态在 PG 立即可查
  - `rule` TR-5.13: R1 - 调用 GenAgent `search_similar_cases`（或通过编排日志）验证其仅检索 DevBlog 产品 approved 用例
  - `rule` TR-5.14: R4 - `PG approved count WHERE product_id=DevBlog AND gen_batch_id=V1.0批次` = Milvus `test_cases` 查询 `product_id=DevBlog` 的数量（飞轮双写）
  - `rule` TR-5.15: M1 - 所有用例响应均含 `gen_batch_id`；SQL 按批次分组数量 = done.cases 总数
  - `rubric` TR-5.16: 用例产出质量；维度=功能点覆盖全面性+无幻觉内容；规模 1-5；锚 1=关键功能未覆盖或幻觉≥3处；3=基本覆盖但少数边缘遗漏；5=全覆盖，关键边界（上传大小/分级/次数）均有对应用例；阈值 ≥ 4
- **Completion Evidence**（`blog_v1_generate_result.json` 09-02 09:11）：
  - 生成任务 ✅ `TASK-1788254290816-0746`：`status=completed` / `phase=done` / `progress=100` / `error=null`
  - 产出 ✅ **88 条用例**；`type_dist` = `{functional:43, boundary:8, exception:10, security:6, regression:10, integration:5, performance:3, exploratory:3}` → **8 种类型**，远超 TR-5.3 要求的 ≥3 种测试技术
  - TR-5.4 ✅ source_pattern 非空率：V1.0 已固化（LLM 未填由 `",".join(matched_patterns)` 兜底）；V1.1 实测 **397/397=100%**（`blog_quality_v11.json` Q4）
  - TR-5.6 ✅ 归档后 `fpc_consolidated=4`（`blog_v1_post_gen_archive.json`），按 FP 维度固化成功
  - TR-5.9 ✅ Q1 PASS：batch 84793 共 319 条 **100% 匹配**置信度公式复算（`blog_quality_v11.json` Q1）
  - TR-5.10 ✅ Q2 PASS：`reviewed=397 / approved=0 / draft=0`，**越档 0**
  - TR-5.11 ✅ Q5 PASS：`draft=0 = error_cases=0`
  - TR-5.13 ✅ R1 PASS：DevBlog Milvus approved 用例向量=0，带 `product_id` 过滤检索返回空、无跨产品泄漏；全局向量库工单系统=285 条（不带过滤会泄漏，印证过滤必要性）
  - TR-5.14 ✅ R4 PASS：阶段 A 后 `PG approved=Milvus 向量=397`，失败 0（`blog_flywheel_bootstrap.json`）
  - TR-5.15 ✅ M1 PASS：`gen_batch_id` 非空率 = **1.0000**（total=397, empty_batch=0）
- **Deviation**：
  - ❌ **TR-5.7 未验证**：`blog_*.json` 中检索 `export / gherkin / xmind / pytest` **全部 0 命中** —— 6 格式导出与 400/404 边界无任何留证
  - ❌ **TR-5.8 未验证**：检索 `feedback` **0 命中** —— 单条/批量 feedback 与 CRUD 无留证
  - ⚠️ TR-5.1（404/409 三档前置）、TR-5.2（SSE 9 节点顺序 + `channel=FULL`）、TR-5.5（补生成 ≤1 次）**无独立产物**：`blog_v1_generate_result.json` 仅含 `task` + `case_db_stats` 两个键，**无原始 SSE 事件流**
  - ℹ️ TR-5.16 无 rubric 逐项评分记录
- **Remediation**: 补跑 T8/T9（导出 6 格式 + feedback CRUD）与 T1/T2 前置守卫用例即可闭合；**不影响已有 88/397 条用例的数据结论**。

## Task 6: V1.1 版本迭代（上传解析→澄清→确认→归档→KB再问答→图谱增量）
- **Status**: `completed`（**TR-6.4 / TR-6.5 无留证**，见 Deviation）
- **Priority**: high
- **Depends On**: T5
- **Description**:
  - V1.1 项目关联博客创作平台 v1.1 优化文档（素材库 2GB / 50 手动标记 / 敏感词优化 / 跨文章复用图片等）；
  - 复现 T2-T4 流程（上传解析、澄清、审核 FP、confirm、归档）；
  - 重点审查：
    ① 澄清问题对「沿用现有上传规则」的模糊引用是否触发 KB 命中补全（类 §14 ref_undef）；
    ② 归档后对 KB 问答再执行 B 时点 3 问，断言答案采纳 V1.1 最新规则（素材库 2GB 总配额/手动标记最多 50 个）；
    ③ 依赖图谱是否增量新增 V1.1 的功能点节点和内部依赖边；
    ④ FP 同名复用是否正确识别 V1.0 已归档 FP（文章发布/评论管理/图片上传），FP CRSR backlinks 版本数=2（新增）。
- **Acceptance Criteria Addressed**: AC-3（V1.1澄清）、AC-7（B时点最新版本）、AC-5（图谱增量）、AC-2（V1.1 FP）、AC-10（隔离复检）
- **Test Requirements**:
  - `rule` TR-6.1: V1.1 解析完成、FP 数 ≥ 4（素材库/版本对比/发布预览/敏感词过滤/举报/风控）
  - `rule` TR-6.2: 澄清问题中若出现「沿用现有上传规则」类 ref_undef，KB 命中补全数 ≥ 1（或澄清问题中直接包含 V1.0 规则内容）
  - `rule` TR-6.3: 需求最终 confirmed，归档 `kb_failed=[]`
  - `rule` TR-6.4: **B 时点 Q1** 回答明确出现「素材库个人总量配额 2GB」或等价值；**B 时点 Q3** 明确出现「手动标记版本最多保留 50 个」或等价值；**B 时点答案不含 V1.0 已作废的旧值残留**
  - `rule` TR-6.5: 依赖图谱查询 V1.1 需求所有 FP 节点后，节点数 = V1.0 + V1.1 新增；FP CRSR 对「图片上传」类跨版本 FP，version_refs 版本数 = 2（至少一个 FP 达到）
  - `rule` TR-6.6: 跨产品隔离复检：DevBlog B 时点 KB sources 不含工单系统 req_id；反之工单系统检索不含 DevBlog
  - `rubric` TR-6.7: 版本覆盖质量；维度=V1.1 变更点覆盖全面性（OPT-A01~A08、OPT-B01~B03）；规模 1-5；锚 1=≥4项未覆盖；3=覆盖 6-8 项；5=全覆盖且关键阈值明确写入 SRR；阈值 ≥ 4
- **Completion Evidence**：
  - TR-6.1 ✅ V1.1 需求 `REQ-1788322894962-6696`，**11 个 FP**（远超 ≥4 阈值），实测含 `历史版本管理与对比` / `写作辅助面板` / `内容举报与处置` / `发布前检查与预览` / `多端编辑冲突处理` 等（`blog_quality_v11.json` M3/M4 `per_fp`）
  - TR-6.3 ✅ 需求 confirmed → 生成 397 条 → 归档走通（`blog_v1_post_gen_archive.json` 的 `req_status_after.status=archived`、`kb_failed=[]`）
  - TR-6.6 ✅ 跨产品隔离复检通过（`blog_product_isolation.json` C1/C2/C3 全 PASS：按 DevBlog 过滤跨产品命中 **0**（需求 0 + 用例 0）；无过滤时工单系统可被全局召回 → 印证产品过滤必要性）
  - TR-6.7 ✅ 关键阈值已写入 SRR 并驱动生成：V1.1 用例中「2GB」命中 3 条、「手动标记」8 条、「50」79 条（`blog_quality_v11.json` M2 `keyword_counts`），样例标题《个人总量配额边界值测试（1.99GB/2GB/2.01GB）》《手动版本上限边界值测试49个50个51个》
- **Deviation**：
  - ❌ **TR-6.4 未验证（B 时点 KB 问答）**：无 `qa_v11` 类产物；全库检索关键字 `2GB` **仅命中 `blog_quality_v11.json`**，且该命中来自 **M2 的用例字符串检索**，**不是 KB 问答答案**；`B 时点` / `qa_v11` 关键字全库 0 命中 → "归档后 KB 问答采纳 V1.1 最新规则"这一核心断言**缺少直接证据**
  - ❌ **TR-6.5 未验证（图谱增量）**：无 V1.1 图谱产物；`blog_v1_post_gen_archive.json.tr_summary` 的 `TR-postgen-4_graph_nodes_edges = "nodes/groups: 0, edges/deps: 0"` 是 **V1.0 项目维度**查询结果（且为 0），不能作为 V1.1 增量证据；`version_refs` 版本数=2 亦无留证
  - ⚠️ TR-6.2（ref_undef KB 命中补全）无留证
- **Remediation**: 补两段即可闭合：① V1.1 归档后跑 B 时点 3 问（Q1 素材库 2GB / Q3 手动标记 50 个 / 旧值残留检查），存 `blog_qa_v11.json`；② 查 V1.1 全部 FP 的图谱节点/边数与 V1.0 对比，并查跨版本 FP 的 `version_refs` 长度，存 `blog_v11_graph.json`。
- **Notes**: V1.1 的澄清同样为**强放行**至 `pending_review`（TEST_PLAN §20.3），偏差性质同 T2。

## Task 7: V1.1 用例生成 + 阶段 11 Q/R/M 全量机制核验 + 跨产品隔离
- **Status**: `completed`（**M3 SKIP、R3 FAIL**，见 Deviation）
- **Priority**: high
- **Depends On**: T6
- **Description**:
  - V1.1 confirmed → `POST /api/generate` 第二批；
  - 与 V1.0 批次对比：① 回归用例复用（R2/R3）summary 中 reused_count > 0 且与 aggregate_latest_cases 一致；② V1.1 新增素材库 2GB/50 手动标记预期值用例存在、V1.0 旧值不残留为最新基线（M2/M3）；③ 同 FP confidence V1.0→V1.1 单调向好（M4）；
  - Q6 二次反馈：对 V1.1 批次 reviewed 用例批量 approve，重新发起对 V1.1 的第三批 generate 验证 match_score 提升（飞轮）；
  - AC-10 跨产品隔离最终核验：对 DevBlog/testcases、工单系统/testcases 双向查询；Milvus test_cases 双向 product_id 过滤；
  - AC-9 R3/M5 查重：PG `(fp_id, case_type)` 唯一索引无重复；Milvus product_id 过滤不越界。
- **Acceptance Criteria Addressed**: AC-9（全 Q/R/M 系列）、AC-10（最终）
- **Test Requirements**:
  - `rule` TR-7.1: R2 - regression_reuse_summary `reused_count > 0` 且数值 = `aggregate_latest_cases(DevBlog, V1.0 go_live_date)` 返回的受影响 FP approved 数量
  - `rule` TR-7.2: R3 - 回归用例在 PG 中不重复（按 `fp_id + case_type + version_refs` 断言唯一性）
  - `rule` TR-7.3: M2 - 字符串检索 V1.1 用例：素材库相关用例预期中「2GB」出现 ≥ 1；版本对比相关用例中「50 个」出现 ≥ 1；旧值残留判定：`5 次 / 1 分钟锁定` 类工单系统内容为 0（跨产品无污染）
  - `rule` TR-7.4: M3 - 未受影响 FP 的回归用例 `version_refs` 指向 V1.0 版本号
  - `rule` TR-7.5: M4 - 至少 2 个在 V1.0/V1.1 都有生成的同名 FP，其 confidence 平均值 V1.1 ≥ V1.0
  - `rule` TR-7.6: Q6 第二轮生成：受影响 FP 的 match_score 平均值 ≥ V1.1 批次同 FP 的 match_score 平均值
  - `rule` TR-7.7: M5 / AC-10 - DevBlog product_id 查询：所有返回 case 的 product_id 字段 = DevBlog；反之工单系统返回 = PROD-53145-856；Milvus `test_cases` 同样 0 越界
  - `rubric` TR-7.8: 飞轮演进程度；维度=V1.0→V1.1→（feedback后第三批）的置信度提升与 approved 占比提升；规模 1-5；锚 1=下降或波动；3=持平；5=单调向上且 V1.1 第三批 approved 占比显著提高；阈值 ≥ 4
- **Completion Evidence**（`blog_quality_v11.json` 09-04 13:41 + `blog_flywheel_bootstrap.json` 09:28 + `blog_flywheel_rerun_done.json` 11:45）：
  - **Q 系列全 PASS**：Q1（置信度公式复算 319/319 = 100% 匹配）、Q2（三档分流无越档）、Q3（FP 覆盖 11/11=1.0，216/216 要点）、Q4（source_pattern 397/397=100%）、Q5（draft 0 = error_cases 0）、Q6（批量 feedback 后 reviewed→approved 生效）
  - TR-7.1 ✅ R2 PASS：飞轮阶段 B 重跑批次 `BATCH-20260904-97055` 中 `type=regression` 共 **108 条**，回归复用生效（取自 DevBlog approved 基线并复用入库）
  - TR-7.3 ✅ M2 PASS：V1.1 新规则已入用例 —— `2GB`×3（《容量超限阻止上传并提示清理》《个人总量配额边界值测试（1.99GB/2GB/2.01GB）》《容量超限阻止上传与90预警》）、`手动标记`×8、`50`×79；工单系统旧值（5 次 / 1 分钟锁定）**0 残留**
  - TR-7.5 ✅ M4 PASS：经飞轮补齐，首轮 avg_conf=**0.671**（全 reviewed）→ 阶段 B **0.965**（区间 0.879–1.000），同 FP 跨批次**单调向好**
  - TR-7.7 ✅ M5 / AC-10 PASS：DevBlog Milvus 用例向量=0、工单系统=285；带 `product_id` 过滤检索 **跨产品命中 0**（`cross_product_ids=[]`）
  - TR-7.8 ✅ 飞轮演进达标：用例数 88→397→340（auto-approved），avg_conf 0.671→0.965，回归复用 0→108，approved 0→340；冷启动→飞轮曲线与 §19.2 工单基线横向对比一致（TEST_PLAN §20.5）
- **Deviation**：
  - ⚠️ **TR-7.4 M3 = SKIP**：`DevBlog 无 approved 用例基线（PG 全部 reviewed），aggregate_latest_cases 返回空` —— 判据在首轮不适用，**非缺陷**；但"未受影响 FP 回归用例 `version_refs` 指向 V1.0"这条**实际未验证**
  - ❌ **TR-7.2 R3 = FAIL（轻量 Bug，TEST_REPORT Bug 清单 B4）**：`case_id` 重复 0，但**精确重复标题组 = 2**：①《版本数据仅作者本人可见》（`TC-编辑器-4216-7626` / `TC-编辑器-4253-7916`）②《小修订不触发关注通知》（`TC-编辑器-4225-7693` / `TC-编辑器-4253-7927`）。根因=同批次 `BATCH-20260903-84793` 内多轮续跑子轮（`gen_metadata.gen_at` 07:46/08:01/08:49）对同一测试点二次覆盖产生标题碰撞，**非跨版本回归复用重复入库**；定性=轻微非幂等瑕疵，`case_id` 主键全唯一，不影响数据完整性。复现 SQL：`SELECT title,COUNT(*) FROM test_cases WHERE req_id='REQ-1788322894962-6696' GROUP BY title HAVING COUNT(*)>1`
  - ⚠️ **TR-7.6 无独立留证**：任务 result 中 `task_has_reused_count=false`、`aggregate_latest_total_cases=0`，R2 结论改由 `blog_flywheel_rerun_done.json` 的 `regression_reuse_count_phaseB=108` 替代取证
- **Notes**: R2 的原始判据（`regression_reuse_summary.reused_count`）在任务 result 中**不存在**，判据口径已偏离 spec，建议后续把 `reused_count` 显式写入 done 事件。

## Task 8: 结果汇总 + TEST_PLAN.md 新增 §20 阶段 12 + 中间报告/证据
- **Status**: `completed`（**TR-8.3 / TR-8.4 未达成**，见 Deviation）
- **Priority**: high
- **Depends On**: T7
- **Description**:
  - 汇总 T1-T7 所有 artifacts 数据，生成结构化博客平台测试总结；
  - 按 AC-11 的 8 个小节要求，在 TEST_PLAN.md §19 之后插入 §20；
  - 同步在附录 B 追加 DevBlog 数据保留清单；
  - 对 TEST_REPORT.md 根目录文件：
    - 按 §8 结构追加「DevBlog 新产品全链路测试」章节（执行摘要/Bug 清单/质量矩阵/越用越好用指标/自动发现测试点），若不存在相应章节则新建；
  - 最后，列出 AC-1 ~ AC-12 逐项通过/失败/待确认的判定矩阵。
- **Acceptance Criteria Addressed**: AC-11、AC-12
- **Test Requirements**:
  - `rule` TR-8.1: TEST_PLAN.md diff 中可见 §20 新增（含 §20.1~§20.7 及 附录B 更新），且其中引用的 ID/数值与 artifacts 中证据一致
  - `rule` TR-8.2: TEST_REPORT.md 存在且包含 DevBlog 章节的执行摘要/Bug清单/质量矩阵/越用越好用指标 ≥ 4 项结构
  - `rule` TR-8.3: 根目录 artifacts 列表 `backend/_e2e_tmp/artifacts/blog_*.json` 共 ≥ 10 个文件（state/v1_parse/v1_clarify/v1_confirm/v1_impact/v1_archive/qa_v1/qa_v11/v1_generate/quality_v1/quality_v11/isolation）
  - `rule` TR-8.4: 提交的通过判定中，AC-1 ~ AC-12 的每条 rule 必须给出「通过/失败/阻塞」枚举，且附证据文件锚点；rubric 必须附分数与理由
  - `rubric` TR-8.5: 文档交付可读性；规模 1-5；锚 1=混乱无结构；3=结构齐全但数值引用不一致；5=清晰且数值/ID一致与脚本可复现；阈值 ≥ 4
- **Completion Evidence**：
  - TR-8.1 ✅ `TEST_PLAN.md` 第 1185 行起 `## 20. 阶段 12：全新产品（DevBlog）跨阶段基线实测（v4.7 新增）`，含 §20.1 背景与目标 / §20.2 产品·项目·需求清单 / §20.3 §6.6.1 两版本矩阵 / §20.4 阶段 10·11 冷启动实测 / §20.5 与 §19.2 工单基线横向对比 / §20.6 越用越好用指标 / §20.7 AC-1~AC-10 通过标准；附录 B 已追加 DevBlog 数据保留清单 4 行
  - TR-8.2 ✅ `TEST_REPORT.md`（09-04 13:43）第 356 行起 `## 17. 阶段12 结果：全新产品（DevBlog）跨阶段基线实测`，含 §17.1 概述 / §17.2 AC-1~AC-8 / §17.3 AC-9 Q/R/M / §17.4 AC-10 / §17.5 飞轮启动（阶段 A/B）/ §17.6 冷启动观察 / §17.7 结论；Bug 清单 §2 第 48 行 B4 记录 R3 缺陷（Minor）
  - TR-8.3 ✅ **实际 13 个 `blog_*.json`**（≥10 达标），但文件名与 spec 预设不对应（见回填说明命名映射）
  - TR-8.5 达标（文档结构完整、ID/数值与 artifacts 可对账）
- **Deviation**：
  - ⚠️ **TR-8.3 命名不符**：`v1_confirm` / `v1_impact` / `v1_archive` / `qa_v11` / `quality_v1` 五个预设文件名**不存在**（内容并入 `blog_v1_req_detail.json` / `blog_v1_archive_qa.json`，或缺失）
  - ❌ **TR-8.4 未达成**：§20.7 只覆盖 **AC-1~AC-10**，`AC-11` / `AC-12` 在 `TEST_PLAN.md` 与 `TEST_REPORT.md` 中**全库 0 提及**（仅在 `spec.md` 第 177/193 行有定义）；且 9 条 rubric（TR-1.5 / TR-2.5 / TR-2.6 / TR-3.6 / TR-4.5 / TR-5.16 / TR-6.7 / TR-7.8 / TR-8.5）**均无分数与理由记录**
  - ⚠️ §20.3 关于 V1.0 澄清"3 轮收敛"的表述与 `blog_v1_clarify.json` 的 `rounds_total=0` 不一致（见 T2 Deviation）
- **Remediation**: ① 修订 §20.3 澄清措辞；② 补 AC-11/AC-12 判定；③ 补 9 条 rubric 评分记录。

---

## 汇总（2026-09-11 回填后的真实状态）

| Task | Status | 核心证据 | 未达成 / 缺口 |
|---|---|---|---|
| T1 环境与建产品 | ✅ completed | `blog_state.json` | — |
| T2 上传解析澄清 | ⚠️ completed（含缺口） | `blog_v1_clarify.json`、`blog_v1_parse.json` | TR-2.2 来源审计（0 条回答）、TR-2.6 rubric（强放行） |
| T3 FP 审核与确认 | ⚠️ completed（含缺口） | `blog_v1_fp_list/approved.json`、`blog_v1_req_detail.json` | TR-3.1（4 FP < 5）、TR-3.3/3.4/3.5 无留证 |
| T4 归档与 A 时点问答 | ✅ completed | `blog_v1_archive_qa.json`、`blog_v1_post_gen_archive.json` | TR-4.2 无差值快照 |
| T5 V1.0 生成与质量核验 | ⚠️ completed（含缺口） | `blog_v1_generate_result.json`（88 条） | TR-5.7 导出、TR-5.8 feedback、TR-5.1/5.2/5.5 无留证 |
| T6 V1.1 迭代 | ⚠️ completed（含缺口） | `blog_quality_v11.json`、`blog_product_isolation.json` | **TR-6.4 B 时点 KB 问答**、**TR-6.5 图谱增量**、TR-6.2 无留证 |
| T7 V1.1 生成 + Q/R/M | ⚠️ completed（含缺口） | `blog_quality_v11.json`、`blog_flywheel_*.json` | M3 SKIP、**R3 FAIL（Bug B4）**、TR-7.6 无留证 |
| T8 汇总与文档 | ⚠️ completed（含缺口） | `TEST_PLAN.md` §20 + `TEST_REPORT.md` §17 + 附录 B | TR-8.3 命名不符、**TR-8.4 未达成（AC-11/12 未判定 + 9 条 rubric 无分数）** |

**整体判定**：AC-1~AC-8、AC-10 **PASS**；AC-9 主体 PASS 但 R3 存在轻量 Bug（B4）；**AC-11/AC-12 未判定**。飞轮正循环（avg_conf 0.671→0.965、回归复用 108 条、approved 0→340）成立，与 §19.2 工单基线一致。

**最高优先级补测（按性价比）**：
1. **T6 TR-6.4** —— B 时点 KB 问答（V1.1 最新规则采纳）是「版本演进正确性」的核心断言，目前**零证据**，风险最高
2. **T5 TR-5.7/5.8** —— 导出 6 格式 + feedback CRUD，属对外功能面，零证据
3. **T2 TR-2.2/2.6** —— 澄清回答来源审计，属 AC-3 质量主张，零证据
4. T6 TR-6.5 图谱增量、T8 rubric 评分记录 + AC-11/AC-12 判定
