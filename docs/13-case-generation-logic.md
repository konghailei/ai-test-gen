# 13 — 用例生成整体逻辑（代码级梳理）

> 梳理时间：2026-09-11 ｜ 对应代码版本：`backend/app/VERSION` = 3.2.14
> 依据文件：`api/generate.py`、`core/orchestrator.py`、`agents/gen_agent.py`、`agents/review_agent.py`、`agents/test_design_agent.py`、`core/quality_gate.py`、`core/channel_selector.py`、`core/fp_cases.py`

本文是**代码实际行为的快照**，不是设计意图。凡与 `docs/00-03` 不一致处，以本文为准并已标注。

---

## 0. 一句话总览

```
已确认 SRR ──► [8 节点 LangGraph 流水线] ──► 用例落库(PG) + 向量回流(Milvus)
                  │
                  └─ FP 分片生成是核心：按功能点拆小 prompt 逐个生成，
                     跨 FP 用例单独走"集成通道"，每个 FP 完成即写 checkpoint
```

**分层职责**

| 层 | 文件 | 职责 |
|---|---|---|
| 入口层 | `api/generate.py` | 守卫校验、SSE 封装、任务状态追踪、报告落库、断点续跑入口 |
| 编排层 | `core/orchestrator.py` | LangGraph DAG、状态传递、条件路由、一致性检查、回归装配 |
| Agent 层 | `agents/*.py` | 6 个专职 Agent（解析 / 澄清 / 门禁 / 影响 / 设计 / 生成 / 审核） |
| 能力层 | `core/channel_selector.py`、`core/quality_gate.py`、`core/fp_cases.py`、`utils/coverage.py`、`core/prompt_manager.py`、`core/llm_client.py` | 通道判定、门禁、FP 用例聚合、要点覆盖、Prompt、模型调用 |
| 存储层 | PG + Milvus | `test_cases`、`analysis_tasks`、`error_cases`、`traceability`；用例向量 + 测试模式库 |

---

## 1. 入口层：`POST /api/generate`

唯一生成入口（SSE）。请求参数：`req_id`、`channel`（仅记录日志，不生效）、`resume`、`resume_from_task`。

### 1.1 六道前置守卫（顺序执行，任一失败即中断）

| # | 校验 | 失败码 | 说明 |
|---|---|---|---|
| 1 | 需求存在 | 404 | — |
| 2 | 产品可见性 `assert_product_visible` | 403 | v3.1.3 P1-1 产品隔离 |
| 3 | `status == "confirmed"` | 409 | 必须先走完 需求分析→澄清→确认 |
| 4 | 无未确认的影响分析条目 | 409 | v3.1.3 新增。`impact_analysis` 中 `confirmed_at` 为空即拦截，防止回归覆盖无声缩水 |
| 5 | SRR 非空（`srr_json.requirement`） | 400 | **明确禁止回退到 raw_text**，防止 SRR 简化导致通道误判与质量下降 |
| 6 | 并发互斥 | 409 | **v3.4 原子占用**（`core/task_guard.py`）：同一事务内 `pg_advisory_xact_lock` → 僵尸回收（updated_at>6h → interrupted）→ 活动检查 → 插入；DB 层另有 partial unique index（`database.py` init_db Step 3b）；**`resume=True` 同受互斥**。插入失败区分"唯一索引冲突（409）"与"其它 DB 错误（500）" |

### 1.2 任务与流式封装

- 建 `AnalysisTask(task_type="generate_cases", status="running")`。
- `EventSourceResponse(event_generator())`，事件格式 `{"event":"message","data":"<json>"}`。
- **断连处理（H6 修复）**：`request.is_disconnected()` 轮询 + `response.aclose` 补丁 + `finally` 三重兜底，任一命中即把任务写为 `aborted`，杜绝永久 `running` 幽灵任务。
- **完成时（stage=done）**：写 `result_json`（`case_count` / `type_dist` / `case_titles` / `point_coverage` / `reused_count` / `regression_count` / `kb_flowback` / 审核摘要），并异步落库评审报告（`build_srr_report` + `build_case_report`），失败不阻断主流程。

### 1.3 断点续跑（v3.1.3）

`_find_resume_checkpoint()` 从最近一次 `interrupted / aborted / cancelled / failed` 的 `generate_cases` 任务读取 `result_json.checkpoint`（必须含 `fp_groups`）。指定 `resume_from_task` 时按其 ID 精确取用。

**v3.4（docs/15 §4.2）输入指纹校验**：checkpoint 必须携带输入指纹
（req_id / product_id / revision / srr_sha256 / fp_set_sha256 / prompt_version / model / schema_version，
`core/pipeline_fingerprint.py`）。续跑前用当前输入重算并逐字段比对，**任一不一致 → 409**
（文案逐字段说明"checkpoint=旧 → 当前=新"）；只有显式 `fallback_to_restart=true` 才放弃旧断点
从头生成。老 checkpoint 无指纹 → 视为不可验证，同样拒绝。
⚠️ 校验放在原子占用槽位**之前**（纯检查无副作用），否则被拒时会把 running 任务留在库里。
另：`PipelineState` 必须显式声明 `resume` / `resume_checkpoint`——未声明字段会被 LangGraph
静默过滤（v3.4 之前因此**断点续跑从未生效**）。

---

## 2. 编排层：LangGraph DAG

### 2.1 图结构

```
START → parse → select_channel ─┬─(enable_clarify)→ clarify → gate ─┐
                                └─(else)─────────────┘             │
                                        gate ─(未通过且可重试)──────┘ 回 clarify（≤3 轮）
                                        gate ─(通过/强制)─► impact → test_design → generate → review → END
```

固定边：`parse→select_channel`、`clarify→gate`、`impact→test_design`、`test_design→generate`、`generate→review`、`review→END`
条件边：`select_channel → {clarify|gate}`；`gate → {impact|clarify}`

**SSE 进度映射**：parse 20 / select_channel 25 / clarify 30 / gate 40 / impact 50 / test_design 62 / generate 70 / review 90 / done 100

### 2.2 各节点行为

**① `_parse_node`** — 不调用 LLM。直接复用已确认 SRR；`existing_req_id` 会覆盖 SRR 内的 `requirement.id`，避免重新解析产生新 ID 导致 `test_cases` 外键冲突。SRR 为空直接 `ValueError`。

**② `_select_channel_node`**（v3.4.4 起内部实现更名为 `_prepare_node`，节点 key 保持 `select_channel` 以稳定 SSE 契约）— 调 `build_generation_config()` 构造环节开关。**FAST/FULL 通道分级已删除**（v1.6 起恒 FULL，v3.4.4 清理掉 channel 概念）；`enable_impact_analysis` 仅当存在 `optimization_context`。

> ⚠️ 关键分支：只要传入 `existing_req_id`（即从 API 生成的已确认需求），**强制 `enable_clarify=False`**，其余能力保留。原因有三：① 已确认 SRR 产出的 `needs_clarification` 无人回答；② 回环纯烧 LLM；③ 网络/配额故障会让 clarify 卡死，表面看像"没有 LLM 调用"。

**③ `_clarify_node`** — 首轮先把交互式澄清答案应用到 SRR（`_apply_clarify_answers`，支持 `a.b.c` 点号路径，内部 deepcopy，不污染原对象）。然后 `clarify_agent.clarify(srr)`；**优先写回 `augmented_srr`**（KB 自动补全结果），否则下游 GenAgent 拿到的是补全前草稿。轮次 `clarify_iteration += 1`。

**④ `_gate_node`** — `quality_gate.check(srr, clarify_result)`，详见 §2.3。

**⑤ `_impact_node`** — 通道关闭影响分析时直接返回空结果。否则 `_fetch_original_cases()`（沿 `optimization_context.original_requirement.id` 取历史用例）交给 `impact_agent.analyze()`，产出 `generation_guidance` 供下游 prompt 注入。

**⑥ `_test_design_node`（v2.3，**无 LLM 调用**）** — 三件事：
1. `load_functional_points(req_id)` 统一加载 FP（v3.1.3 起形状唯一、含 `atomicity_check`），失败 → `degraded_flags += ["fp_load_failed"]`；
2. `test_design_agent.design()` 产出蓝图（8 种技术适用性 + 边界/决策表/状态/错误推测/场景目标 + `guidance_text`）；
3. `_assemble_regression_cases()` 装配回归基线（见 §2.4）。

**⑦ `_gen_node`** — 见 §3。

**⑧ `_review_node`** — 见 §4。

### 2.3 质量门禁（`QualityGate`）

**动态阈值**：冷启动（`kb_status.kb_empty` 或 `product_first_requirement`）→ **0.60**；正常 → **0.75**。

| 类型 | 检查项 |
|---|---|
| Blocker | ① SRR Schema 校验（Pydantic `SRR.model_validate`）② 关键字段非空（`requirement.title` / `requirement.summary` / `flows.happy_path`）③ `quality_report.overall_score >= pass_score` ④ 无未解决 Critical 问题 |
| Warning | ⑤ `happy_path.steps < 2` ⑥ 冷启动模式提示 |

**回环控制（`ClarifyLoopGuard`）**：
- `passed` 或 `forced_pass` → 走 impact
- 未通过且 `iteration < MAX_CLARIFY_ITERATIONS(3)` → 回 clarify
- 达上限 → `forced_pass=true` + warning "达到最大澄清轮次（3），强制通过"
- **clarify 被禁用时**（FAST / 已确认需求）未通过也直接强制放行，绝不回环

`forced_pass` 会透传到 done 事件（`manual_action_required=true`），前端提示"门禁未完全通过，请人工修改需求后再确认"。

### 2.4 回归用例装配 RTS（v3.4）

来源：`srr.impact_analysis` 中 `is_affected == True` **且** `confirmed_at` 非空的条目。

对每个受影响 FP 调 `aggregate_latest_cases(session, product_id, fp_name, fp_id)`：
- 口径：`TestCase → Requirement → Project` 三表 join，筛 `status='approved'` 且 `related_fp` 匹配，**按项目 `go_live_date` 最大者**认定为"最新基线"；
- 命中 → 逐条复制并改写：`type="regression"`、`title="[回归]…"`、`related_fp=fp_name`、`source="reused"`、`source_case_id`、`gen_metadata.regression_source="reused"`；
- 未命中 → 记入 `coverage_gaps`（含 `test_focus` + `reason`），交 GenAgent 定向补生成。

> ⚠️ 该口径意味着：**改项目 `go_live_date` 会让已固化的 `functional_point_cases` 归属失效**，必须对受影响的 FP 名重跑 `consolidate_fp_cases()`。这是 v3.2.14 的已知坑。

结果写回 state：`regression_reuse_summary = {reused_count, gap_count}`，并在 done 事件透出。

### 2.5 测试设计技术的选择机制（8 种 → 实际生效 6 种）

`TestDesignAgent` 声明了 8 种技术（等价类 / 边界值 / 决策表 / 状态迁移 / 场景 / 正交 / 错误推测 / 因果图），但**选择动作分三层，且只有 6 种具备确定性触发**：

| 层 | 实现位置 | 作用 | 技术数 |
|---|---|---|---|
| ① 静态定义 | `prompts/gen_agent.py` SYSTEM_PROMPT 技术 1~8 | 给 LLM 的技术手册，由 LLM 自行选用 | 8（仅有描述） |
| ② 定向触发 | `_build_test_design_guidance()` | 扫 SRR 字段，命中即写死"测哪些点"注入 prompt | **6** |
| ③ 事后统计 | `GenAgent._analyze_technique_coverage()` | 生成后回查用例集的技术覆盖迹象 | **6** |

#### ① 触发条件（② 层，全为规则 / 关键词匹配，非 LLM 决策）

| 技术 | 触发条件 | 扫描字段 |
|---|---|---|
| 等价类划分 + 边界值分析 | constraints 值含 `-` `~` `至` `范围` `min` `max` `≤` `≥` `<` `>` 任一，且 `field` 非空 | `data_constraints` |
| 决策表法 | `type ∈ {validation, business, workflow}` 的规则数 ≥ 2 | `business_rules` |
| 状态迁移法 | 备选流 `name` / `trigger` 含 `状态` `锁定` `禁用` `过期` `取消` `撤销` `回退` `超时` `失败` | `flows.alternate_flows` |
| 场景法 | `happy_path.steps` 非空且 `alternate_flows` 非空；或 `shared_business_rules` 非空且 FP 数 ≥ 2 | `flows` / `cross_fp_interactions` |
| 错误推测法 | 字段名+约束含 `必填/required/非空/not null` → 空值测试；含 `长度/length/字符/max/min/-` 且有数字 → 超长+特殊字符；含 `金额/数量/price/amount/count` → 负数/零值/精度溢出；或规则文本含必填语义 | `data_constraints` + `business_rules` |
| 正交实验法 | ❌ 无触发器 | — |
| 因果图法 | ❌ 无触发器 | — |

**扫描范围**：`[总SRR] + [每个 FP 的子 SRR]`；总 SRR 以 `id="total"` 的伪条目参与扫描（`_extract_targets` 因此要过滤 `[total]` 前缀）。

**命中后的输出**（不只给"用什么技术"，还写死生成要求）：
- 边界值 → 有效等价类代表值 + 无效等价类两值 + `min-1, min, min+1, max-1, max, max+1`
- 决策表 → 识别条件/动作 → 列 `2^n` 组合 → 合并相似规则 → 每组合一个用例
- 状态迁移 → 正向覆盖全部合法转换 + 反向尝试非法转换
- 场景法 → 基本流 + 各备选流的不同组合
- 错误推测 → 仅补"常规用例之外"的易错场景，条目**截断至 15 条**（`error_guess_hints[:15]`）

7 条路径全部未命中 → 返回空串 → 不注入任何指导，LLM 只依赖 ① 层静态定义。

#### ② 两个已确认缺陷

1. **正交实验法、因果图法没有实现。** 它们仅存在于 SYSTEM_PROMPT 的静态描述里，既无定向触发器，也不参与覆盖统计。`TestDesignAgent.design()` 的状态位判定为：

   ```python
   "orthogonal_array": "正交" in guidance_text,   # 恒 False
   "cause_effect":     "因果图" in guidance_text,  # 恒 False
   ```

   因此 `techniques_applicable` 中这两个键**永远为 False**——前端"8 种技术适用性"里这两项是死位，不代表需求不匹配。

2. **③ 层统计口径远宽于 ② 层触发口径**，不能当作"技术已定向应用"的证据：
   - 场景法：`functional > 0` 即算覆盖（几乎必然命中）
   - 错误推测法：`exception` 或 `security` 用例 > 0 即算覆盖
   - 等价类划分：需"存在范围型约束"**且**"`boundary_value` 已覆盖"同时成立

   要验证真实的定向触发结果，应读 `test_design_blueprint.techniques_applicable`（② 层口径）。

#### ③ 触发粒度示例

`data_constraints: [{field: "password", constraints: "6-20"}]`：
- 命中边界值：`"-" in "6-20"` 且 `field` 非空 → 写入 `boundary_targets`
- 同时命中错误推测：`combined = "password 6-20"` 含 `-` 且有数字 → 追加"超长字符串 + 特殊字符测试"

同一字段同时触发两种技术（设计上互补）。注意 `has_range` 判定包含裸 `"-"`，**任何带连字符的约束都会命中边界值分析**。

---

## 3. 生成层：`_gen_node` + `GenAgent`（v3.1.3 FP 分片）

### 3.1 编排器侧的三条通道

| 通道 | 触发条件 | 行为 |
|---|---|---|
| **逐 FP 分片**（主） | `functional_points` 非空 | 每个 FP 单独调 `gen_agent.generate(fp_scope=fp, functional_points=[fp])`，用小 prompt 定向生成；用例缺 `related_fp` 时补为 `fp.id` |
| **全量兜底** | `functional_points` 为空 | 单次全量 SRR 生成（v3.1.7 补）。否则 FP 拆分失败/被跳过的需求会静默产出 0 条用例 |
| **跨 FP 集成** | SRR 存在 `cross_fp_flows` / `shared_business_rules` / `data_constraints` | 组装 `global_srr` 片段独立生成，`type` 归一化为 `integration`，保证按 FP 拆分后组合用例不丢失 |

**容错与续跑**：
- 单个 FP 生成异常 → `logger.warning` + 继续下一个（**不阻断**），进度卡片标 `failed`，续跑会重试；
- 每个 FP 成功后立即 `_save_gen_checkpoint(task_id, checkpoint_groups, fingerprint=…)` 写入 `analysis_tasks.result_json.checkpoint`（v3.4 起附带**输入指纹**，续跑前逐字段比对）；
- 续跑时跳过 `completed_fps`，已恢复的 FP 标 `status="resumed"`；
- 细粒度进度经 `_gen_event_queue` 与 LangGraph 节点事件合并为同一队列按序透出（`fp_subtask` / `subtasks` / `fp_progress`）。

汇总后做一次**标题级去重**（`title or id` 为 key，保留首次出现）。

### 3.2 `GenAgent.generate()` 六阶段

1. **KB 轻量检索（v3.2）** — 同模块下 `status='approved'` 的用例取 Top-3 作 Few-shot（步骤截 3 条、断言截 2 条，按标题去重）。失败仅 debug 日志。
2. **测试模式库检索（v3.4）** — `vector_store.search_patterns(query, top_k=3)`，用 `模块名 + summary/title` 组 query（截 200 字），`asyncio.to_thread` 避免阻塞事件循环。
3. **Prompt 构建** — `calc_min_cases()` 按要点规模推导动态数量下限并作为硬约束注入；同时注入 FP 子 SRR、覆盖缺口（`coverage_gaps`）、回归锚点（`regression_anchors`）、影响指导。
   - `fp_scope` 传入时 `_srr_effective = fp_scope.srr_json`，并 `pop("points")` 强制限定范围；
   - `global_srr` 传入时 `_fp_effective = []`，只注入跨 FP/共享规则/顶层约束。
4. **LLM 调用** — `llm_client.call_json(temperature=0.4, max_tokens=32768, allow_array=True, agent_name="GenAgent")`。失败抛 `RuntimeError`。
5. **解析与后处理** — `_extract_cases()`（兼容数组或含 `test_suite` 的对象）→ `_post_process_cases()`（生成 ID `TC-{MODULE}-{批次4}-{序号4}`、补全字段、`gen_confidence` 放入 `gen_metadata`）→ **要点标注兜底**（v3.1.3：`backfill_cases_point_ids()` 按关键词命中为未标注用例回填覆盖的要点 ID）。
6. **四级去重（v2.0）** — 精确 → 语义（embedding）→ 子集 → LLM 等价判定。**最后 `merge_regression_cases()` 并入 reused 基线**，并为 reused 用例补 `gen_batch_id`，再算覆盖率。

> 注意顺序：覆盖率统计基于**合并回归基线之后**的最终用例集（v3.3 修正）。

### 3.3 一致性检查（`_gen_node` 尾部，v2.0 三维度）

基线：`load_latest_completed_task(req_id)`（只取已完成任务，排除 running/failed）。

| 维度 | 算法 | 不一致判据 |
|---|---|---|
| 用例数方差 | `abs(cur-prev)/max(prev,1)` | `> 20%` |
| 类型分布 | 各类型计数 Jaccard | `< 0.7` |
| 语义相似度 | 标题 embedding，每条当前用例取与历史的最佳匹配后求均值 | `< 0.7` |

综合判定：`is_inconsistent = 数量不一致 or (类型不一致 and 语义不一致)`

- DB 查询失败 → `degraded_flags += ["consistency_db_failed"]`，`consistency_check = {"error": "db_unavailable"}`
- 语义相似度用 `None` 表示**计算失败**，与合法的 `0.0` 区分（v3.1.3 消除二义性）

---

## 4. 审核层：`_review_node` + `ReviewAgent`

### 4.1 审核流程

1. **多因子置信度（非 LLM）** — `final_confidence = match_score×0.4 + completeness×0.3 + consistency×0.2 + gen_self×0.1 + pattern_bonus`；`match_score` 来自 `vector_store.search_similar_cases(title, top_k=3, product_id=...)`（v2.2 按产品过滤，防跨产品泄漏）。写入 `case["confidence"]`。
2. **规则预检** — 追溯性 + 可执行性，产出 `severity=blocker` 的 issue。
3. **LLM 6 维审核** — 完整性 / 去重 / 一致性 / 覆盖率等。
4. **合并 + 要点级覆盖** — `calc_point_coverage()`，优先按要点映射统计，无法映射时回退数量比；输出 `total_points / covered_points / coverage_rate / high_priority_covered / uncovered`。

### 4.2 补生成闭环（最多 1 次）

`should_supplement=True` 且 `supplement_iteration < 1` 时：
- 把 `coverage_gap` 类型 hint 的 `uncovered_points` **按 FP 分组**，转成结构化 `supplement_gaps`（`fp_id` / `fp_name` / `test_focus` / `reason`），`test_focus` 每 FP 最多取 15 个未覆盖要点；
- 非 `coverage_gap` 的旧式字符串 hint 走原映射；
- 调 `gen_agent.generate(coverage_gaps=supplement_gaps)` → `merge_regression_cases()` 合并 → **二次 review**；
- 二次仍建议补生成 → 标 `supplement_exhausted=True`，避免前端误解"还会再补"。

### 4.3 KB 回流 `persist_to_kb()`

**入库前标题级严格去重**（R3 修复）：同标题只保留 `confidence` 最高的一条。原因：FP 分片 + 补生成各自子轮内部去重，无法覆盖**跨子轮的标题重复**。

逐条写入（每条用 `session.begin_nested()` savepoint 隔离，单条失败不影响其余）：

| 条件 | 处理 |
|---|---|
| `confidence >= 0.70` 且非 blocker | `approved`（自动入库） |
| `0.60 ~ 0.70` | `reviewed`（待人工确认） |
| `< 0.60` 或 blocker | 记录到 `error_cases` |
| `id` 已存在（v3.1.3 修复） | 跳过，计入 `reused_skip` —— 回归复用用例带历史 ID，重复 insert 会 UniqueViolation 导致整批丢失 |

- **v3.4 起 PG→Milvus 走事务性出箱**（`core/vector_outbox.py`）：approved 分支在 savepoint 内把投递意图与用例同事务入箱，由 relay 至少一次幂等投递；`stats.vector_pending` 记入箱条数。**Milvus 抖动不再判 `kb_publication_incomplete`**（出箱自愈），只有入箱失败才计 `vector_write_failed`；积压见 `pending_summary()` 与 `reconcile_cases()`。
- **向量写入只对 approved 用例做**（v2.0 决策），避免未审核用例污染 Few-shot 检索。

> 阈值 0.70 是 v3.5 从 0.85 下调的：冷启动时 `match_score=0`、`pattern_bonus=0`，理论上限仅 0.6~0.7，原阈值永远不可达，会导致 approved 恒为空、复用机制死循环。

### 4.4 覆盖率口径与完整性盲区

`utils/coverage.py:collect_points()` 是**覆盖率分母的唯一来源**，同时也是注入 LLM 的「必覆盖清单」来源（v3.1.3 起强制两者同源，避免清单与统计口径不一致）。

#### ① 参与度量的 5 类要点

| 要点类型 | 来源 |
|---|---|
| `happy_path` | 总 SRR 与各 FP 的 `flows.happy_path.steps` |
| `alternate_flow` / `alt_flow` | 总 SRR 与各 FP 的 `flows.alternate_flows` |
| `rule` | **仅各 FP 的** `business_rules` |
| `data` | **仅各 FP 的** `data_constraints` |
| `shared_rule` | 总 SRR 的 `cross_fp_interactions.shared_business_rules` |
| （`precondition`） | 可抽取，但 `include_preconditions=False` 时不计入分母 |

> ⚠️ **总 SRR 只抽 4 类**（happy_path / alternate_flows / precondition / shared_rule）。`business_rules` 与 `data_constraints` 只在 FP 子 SRR 中抽取，因为 v2.48 起这两个字段已从总 SRR 移除（下沉到 FP）。
> 后果：**FP 拆分失败或为空（走全量兜底生成通道）时，分母里会缺失"业务规则"和"数据约束"两个维度**，覆盖度量维度缩水。`degraded_flags=fp_load_failed` 只覆盖"加载失败"，不覆盖"本来就没有 FP"。

#### ② 不参与度量的字段（10 个）

`non_functional`（性能/安全/兼容性/可访问性）、`api_definitions`、`user_roles`、`dependencies`、`ui_rules`、`data_model`、`assumptions`、`relationships`、`field_conflicts`、`legacy_data_impact`。

- v2.48 已把 `boundary_conditions` / `acceptance_criteria` 合并进 `business_rules`、`test_hints` 移除 —— 这三个**属设计取舍，不是遗漏**。
- 但上列 10 个字段**仍会随完整 SRR 注入 prompt**（`prompts/gen_agent.py:786` 的 `json.dumps(srr)`；只有补生成场景才瘦身为需求骨架）。SYSTEM_PROMPT 里也确实有"性能测试（仅当 SRR 含 `non_functional.performance` 时生成）""安全测试 ≥1 个"这类要求。
- 结果是**不对称**：LLM 为其生成了用例→不计分；完全没生成→不告警、不触发补生成、`degraded_flags` 不亮。

#### ③ 三个已确认风险

**R1｜回退口径把「用例数」当「覆盖率」（最严重）**

```python
annotate_rate = cases_with_ids / case_count
mapping_based = total > 0 and cases_with_ids > 0 and annotate_rate >= 0.3
if mapping_based:
    rate = len(covered_ids) / total          # 真实映射口径
else:
    rate = min(case_count, total) / total    # 退化为数量比
```

- 标注率 < 30% 时，覆盖率退化成"用例条数 ÷ 要点数"。极端例：12 个要点 / 15 条用例 / **0 条标注** → `min(15,12)/12 = 100%`。
- 该分支下 `uncovered = []`（第 371 行）且 `high_priority_covered = min(len(hp_points), case_count)` → **补生成机制永远不会被触发**，系统认为已全覆盖。
- 30% 标注率门槛为硬编码经验值，无配置项。

**R2｜覆盖维度盲区**

见 ②。非功能、接口、权限、依赖、UI 规则类需求点若未覆盖，覆盖率与告警系统均无感知。

**R3｜FP 为空时维度缩水**

见 ①。此时分母只剩主流程步骤 + 异常分支 + 共享规则。

**R4（附带）｜`backfill_cases_point_ids()` 可能误标**

兜底回填用 3-gram 命中 + `min_ratio=0.35` 阈值（要求至少命中 1 个 3 字词）。它能把标注率推过 30% 门槛从而启用映射口径（这是好事），但关键词匹配会跨语义误配 —— 例如要点「订单金额超过阈值需审批」与用例「订单金额为负值时提示错误」共享 `订单金` `单金额` 等 3-gram，可能被判为覆盖。**标注率达标 ≠ 标注正确。**

#### ④ 其他量化细节

- `calc_min_cases()`：`ceil(happy_path 数 / 3)` + `alternate_flows 数` + `rules 数` + **数据字段数**（非边界点数）；P0/P1 取 `max(min_cases, total × 0.6)`；最终 **封顶 40**（`min(min_cases, 40)`）。
- 数据约束按"字段数"计 1 例，而一个范围字段理论上需 6 个边界点（`min-1 … max+1`）→ **数量下限偏低**，且 40 的封顶对大需求偏低。
- 要点描述统一截断至 160 字符（`desc[:160]`）。

#### ⑤ 改进建议（按性价比排序）

| # | 建议 | 说明 |
|---|---|---|
| 1 | 修回退口径 | 标注率不足时返回 `rate: None` + `unreliable: true`，且该模式下不返回空 `uncovered`（至少把全部要点列为"覆盖未知"）。**纯计算口径修正，不改生成行为，风险最低** |
| 2 | 扩展 `collect_points` | 纳入 `non_functional` / `api_definitions` / `user_roles` / `dependencies` / `ui_rules`。会让分母变大、覆盖率数值下降，但那才是真实情况 |
| 3 | 修正 `min_cases` | `data` 计数改为按边界点数；放宽 40 封顶 |
| 4 | FP 为空时告警 | 区分"FP 加载失败"与"本来就没有 FP"，后者也应置 degraded（覆盖维度少了两个） |

---

## 5. done 事件与降级可观测

`done` 事件携带：`srr`、`cases`、`review_report`、`clarify_result`、`gate_result`、`impact_result`、`kb_flowback`、`fp_coverage`、`point_coverage`、`consistency_check`、`test_design`、`regression_reuse_summary`、`degraded_flags`、`manual_action_required`、`supplement_iteration`、`duration_ms`。

**`degraded_flags`（v3.1.3）** 是跨节点累积的降级信号，目前有：`fp_load_failed`、`consistency_db_failed`。前端据此提示"功能点定向生成/一致性对比不可用"。

**异常路径**：`run_pipeline` 捕获异常 → `final_state["error"]` 写入 → `dlq.push(task_type="pipeline", payload={req_id, channel, stage})` → yield `error` 事件。
> v3.1.3 修复过一个隐蔽缺陷：`_producer` 曾静默吞掉 `astream` 异常，导致用户看到"流水线完成"但 0 用例且无 error 事件、DLQ 无记录。现在异常对象会经队列传给消费循环统一处理。

---

## 6. 状态流转

```
draft ──分析──► pending_review ──confirm──► confirmed ──POST /generate──► (AnalysisTask: running)
                    ▲                                                        │
                    └──────── regenerate / 重新澄清 ◄────────────────────────┘
```

- 仅 `confirmed` 可生成；重新分析/澄清会把状态重置回 `draft` / `pending_review`。
- `confirm_requirement` 还有两项守卫：`_finalize_degraded` / `_clarify_degraded` / `_recheck_skipped` 降级标记存在时拒绝确认；所有 FP 必须已审核通过。

---

## 7. 关键设计决策与已知取舍

1. **SRR 是唯一真源**：入口、parse、GenAgent 三层都不允许回退 raw_text。代价是需求分析阶段的错误会一路传导到底。
2. **✅ 已完成（v3.4.4）**：`select_channel` 的"名义决策"已清理——FAST 相关死代码全部删除，`channel` 概念不复存在；模块职责收敛为 `build_generation_config`（环节开关构造器），`existing_req_id` 关闭 clarify 的行为保持不变。
3. **四层去重防线**（GenAgent 四级 + persist 前标题级）说明**跨子轮重复**是真实痛点；FP 分片越细，重复概率越高。
4. **回归复用优先于重新生成**：成本可控、结果稳定；只有缺口才交给 LLM。代价是强依赖 `related_fp` 标注质量与 `go_live_date` 归属正确性。
5. **版本标注体系（2026-09-11 已治理）**：项目里有两套编号并行——`VERSION` 文件是对外发布版本（当前 `3.2.14`），代码注释里的 `vX.Y` 是**内部变更批次号**。本轮把 `v4.x` / `v5.x` 共 21 个编号形式（95 处代码 + 19 处文档）经 git 溯源后统一归并为 `v3.1.3`（共同引入提交 `fd349cf`，2026-09-07）。`v1.x` / `v2.x` / `v3.x` 经溯源确认是**多条并行编号流**（`v2.3` 早于 `v2.0`、`v3.1` 早于 `v2.34`），与发布版本不存在单调映射，保留原样。

---

## 8. 变更时间线（按代码注释批次号归纳）

> **编号说明**：`v1.x` / `v2.x` / `v3.x` 是**并行批次号**，不构成时间线。下表的"引入时发布版本"由 `git log -S <编号>` 溯源得到，可据此定位历史提交。
> `v3.5` 与 `v4.x` / `v5.x` 世代同批（同属 `fd349cf`），已统一归并为 `v3.1.3`。

| 批次号 | 引入时发布版本 | 内容 |
|---|---|---|
| v1.4 | 1.3.0 | 引入 FP 定向生成；生成任务在任务管理页可见 |
| v1.6 | 1.7.0 | 通道选择改为恒 FULL；仅 confirmed 可生成 |
| v2.0 | 1.3.0 | 四级去重、三维一致性检查、KB 向量写入延后至审核后 |
| v2.3 | — | 新增 `test_design` 节点（8 种测试设计技术） |
| v3.1 | 1.9.0 | 移除 KB 检索节点 `retrieve` |
| v3.2 | — | GenAgent 轻量 KB 检索（Few-shot Top-3） |
| v3.4 | 2.8.0 | 测试模式库检索 + 回归用例装配 RTS |
| **v3.5 + v4.x / v5.x** | **3.1.3** | approved 阈值 0.85→0.70；未确认影响条目拦截生成；FP 加载源统一到 `test_design`；ORM 收敛 `db/repositories` + `degraded_flags` 可观测；要点标注兜底回填；审核 prompt 降载与权重重构；**FP 分片生成 + checkpoint 断点续跑 + 细粒度进度**；维度级置信度；评审报告中心（门禁报告 + 审核报告落库）；提示词进化闭环 |
