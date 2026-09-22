# 测试计划 v5.0 执行报告 — 2026-09-21

> 执行依据：`TEST_PLAN.md` v5.0（L1 机制层脚本断言 / L2 流程层结构断言 / L3 内容层 AI 判定）
> 执行窗口：2026-09-21 03:37 – 04:23（容器内 `run_test_plan.py`）
> 判定口径：PASS / FAIL / **N/A**（样本为 0 不记为通过；用别处证据顶替记为「范围外替代」）

---

## 0. 结论先行

| 层 | 结论 | 一句话 |
|---|---|---|
| **L1 机制层** | **FAIL** | 6/7 探针 + 4 个回归套件 + unit 全通过；`_probe_checkpoint_saver.py` 失败，**根因是缺依赖 `langgraph.checkpoint.postgres`，不是环境噪声** |
| **L2 流程层** | **PASS**（修复后） | 缺陷 B 修复前：两次生成都在「已产出 11~27 条」后被补生成异常整条击穿、0 落库。修复并重跑后：**6/6 结构断言全过**，流水线正常收尾，10 条保住、落库 9 条 |
| **L3 内容层** | **ok 7 / bad 2 / doubt 1** | 本轮真实产物 10 条逐条判定（第 3 节）。单条质量达标且优于历史批次，但覆盖率仅 **24.2%**，且 `review_llm_failed` 表示本轮 LLM 审核缺席 |

> **后续追加**：本轮共定位 5 个系统自身缺陷，其中三个已修复并推送——
> **E3**（主供应商保存失效，`08a0358`）、**E2**（僵尸供应商残留，`60972d1`）、
> **B**（补生成击穿流水线，`7db82c9`）。
> 09-22 补修：**C**（finalize UnboundLocalError，`f978a06`）、**D**（覆盖维度缩水，`3414830`）。
> **A**（检查点缺依赖，P0）经历「修复→复发→根治」：`88d4378` 止血后容器重建即复发，
> 09-22 已补装并验证探针 6/6，base 镜像重建根治中（详见第 4 节缺陷 A）。
> 仍待修：**E**（跨子轮重复）。详见第 4 节。

**本轮最重要的两个发现**（都与 LLM 供应商无关，是系统自身问题）：

1. **检查点持久化是假的**：`PIPELINE_CHECKPOINT_BACKEND=postgres`，但模块缺失导致静默降级成 `InMemorySaver`，PG 里 0 行检查点。docs/15 的 P0-2「durable execution」实际未闭环。
2. **补生成失败会击穿整条流水线**：主用例集已经生成完毕（9~13 条），仅因为「补充生成」这一**增强步骤**的 LLM 调用超时，整条流水线抛异常，已生成用例全部丢弃、0 落库。用户拿到的结果是零产出 + 已消耗的额度。

---

## 1. L1 机制层（脚本断言）

`run_test_plan.py` 全量执行，`plan-results.json`：`mechanism_pass = False`

| 步骤 | 结果 |
|---|---|
| `probe:_probe_concurrency.py` | ✅ pass |
| `probe:_probe_outbox.py` | ✅ pass |
| `probe:_probe_durable.py` | ✅ pass |
| `probe:_probe_revision_lock.py` | ✅ pass |
| **`probe:_probe_checkpoint_saver.py`** | ❌ **fail（2 通过 / 4 失败）** |
| `probe:_probe_selfheal.py` | ✅ pass |
| `probe:_probe_evidence_claims.py` | ✅ pass |
| `regression:test_phase2_core.py` | ✅ pass |
| `regression:test_phase3_agents.py` | ✅ pass |
| `regression:test_phase4_orchestrator.py` | ✅ pass |
| `regression:test_phase5_api.py` | ✅ pass |
| `pytest:unit` | ✅ pass |

### 1.1 唯一失败项的证据链（已定位到根因）

探针输出：

```
[1] 首次运行（postgres 检查点后端）
  ✅ done 事件存在
  ❌ checkpoints 已落 PG（thread_id=task_id）  ← rows=0
[2] 同 thread_id 第二次运行
  ❌ 重跑后检查点仍在累积
[3] 后端未降级
  ❌ backend未降级
  ❌ 后端类型为 AsyncPostgresSaver  ← InMemorySaver
探针汇总: 2 项通过, 4 项失败
```

根因（容器内实跑抓到的 ERROR 行）：

```
[Orchestrator] ⚠️ PostgresSaver 初始化失败，检查点降级为 memory
（本次部署的 done 事件会带 checkpoint_backend_degraded）:
No module named 'langgraph.checkpoint.postgres'
```

代码位置：`backend/app/core/orchestrator.py:361-401`（`_ensure_checkpoint_backend`）
—— `PIPELINE_CHECKPOINT_BACKEND` 默认是 `postgres`（`config.py:104`），import 失败 → `except` → 降级 `MemorySaver`。

**这不是环境抖动，是部署缺依赖。** 后果：断点续跑、durable execution、进程重启后状态审计全部不成立——而 `_probe_durable.py` 之所以还能 pass，是因为它验证的是「应用层的 FP checkpoint 与输入指纹」，走的是 `analysis_tasks.result_json`，不依赖 LangGraph checkpointer。

> 补充观察：降级本身有 ERROR 日志 + `degraded_flags`，**可观测性做到了**，但「能力降级后没有 fail-closed」——这正是 docs/15 P0-1 的同型问题：降级可见 ≠ 高风险副作用被阻断。

---

## 2. L2 流程层（结构断言）

### 2.1 完整链路轮（03:37:36 – 03:50:59，含需求分析）

| 步骤 | 结果 | 证据 |
|---|---|---|
| `analysis:upload` | ✅ | req_id=`REQ-1789962162638-8915` |
| `analysis:parse` | ❌ | 任务 `failed`：所有 LLM 提供商均不可用（2 家，241s） |
| `analysis:srr` | ❌ | `SRR.requirement=缺` |
| `analysis:clarify` | ⏭ skipped | 无澄清问题 |
| `analysis:finalize` | ❌ | **500**，且日志有 `UnboundLocalError` |
| `analysis:fp_review` | ✅（空洞） | FP 0 个，审核通过 0 个 |
| `analysis:functional_points` | ❌ | 功能点 0 个 |
| `analysis:confirm` | ❌ | 409「当前状态 draft 不可确认」 |

**附带发现（真实健壮性缺陷）**：`finalize_requirement` 在 LLM 失败路径下未初始化 `finalize_result`，
抛 `cannot access local variable 'finalize_result' where it is not associated with a value` → 全局 500。
失败路径返回了 500 而非结构化错误，前端无从区分「需求有问题」和「供应商挂了」。

### 2.2 生成轮（跳过分析，直接对已确认需求 `REQ-1787723370912-1404`）

目标需求：登录安全策略，P0，confirmed，1 个 FP，SRR 口径明确（5 次锁定 / 1 分钟 / 提示统一 / 计数清零 / 剩余时间提示）。

| 轮次 | 生成结果 | 最终落库 |
|---|---|---|
| 第 1 轮（03:56–04:07） | FP 分片生成失败 → **兜底通道成功产出 13 条，去重后 11 条** | **0** |
| 第 2 轮（04:13–04:23） | 兜底通道成功产出 **9 条** | **0** |

两次都是同一个死法（API 日志实证）：

```
04:03:17  节点 generate（FP分片）完成 ▶ cases=11, fp_tagged=11
04:03:17  节点 review ▶ cases=11
04:07:02  [LLM] 首响应超时: openrouter:poolside/laguna-s-2.1:free, 180s 内无任何响应
04:07:57  [LLM] 首响应超时: 火山:glm-5.3-flash, 54s 内无任何响应
04:07:57  [GenAgent] LLM 调用失败: 所有 LLM 提供商均不可用
04:07:57  [Orchestrator] ╳ 流水线异常: 用例生成失败
```

PG 实证：`select count(*) from test_cases where created_at > '2026-09-21 03:50'` → **0**。
证据包 `/app/_review_runs/20260921-035659/` 只有 `AI-REVIEW-TASK.md`，没有 `cases.json` / `run.json`。

### 2.2.1 补充取证：供应商配置与调用顺序（2026-09-21 追加）

起因：用户反馈「主供应商应该是火山」，与本报告第 2.3 节口径不一致，故回查运行时配置。

**启动日志实证（`app.api.settings:load_db_config_to_settings`）**：

```
2026-09-21 00:21:02  已从 DB 加载 LLM 配置: 主供应商=qwen2, model=deepseek-v4-flash-0731, 共 6 个供应商
2026-09-21 03:42:35  已从 DB 加载 LLM 配置: 主供应商=日日新, model=sensenova-6.8-flash-lite, 共 3 个供应商
```

**三件事**：

1. **火山从未是主供应商。** 00:21 时主供应商是 `qwen2`（deepseek-v4-flash-0731），03:42 之后是 `日日新`
   （`llm_config.is_primary=true`）。火山在候选列表里，但 `is_primary=false`。
2. **本轮没有「先用火山」。** 实际调用顺序（API 日志）：
   第 1 轮 `qwen:qwen3.8-max` → `日日新`；补生成阶段 `openrouter` → `火山`。
   **火山每次都是最后一家**（前面全超时之后），且自身也 54s 首响应超时失败。
3. **发现新缺陷：`_all_provider_configs` 只增不删。**
   DB 从 6 个供应商缩到 3 个后，运行时 `available_providers` 仍是
   `["qwen", "日日新", "qwen1", "openrouter", "火山", "qwen2"]` ——
   `qwen` / `qwen1` / `qwen2` 三家**残留在内存里继续参与故障转移**。
   实证：04:00 那次故障转移打到了 `qwen:qwen3.8-max`，而此时 DB 的 `llm_config` 只有 3 行、已无 qwen。
   后果：故障转移会去调**已被删除的供应商**，且该供应商的 api_key 是旧值，失败不可解释、排查方向被误导。

**配置被重载的时间点**：03:42:35，紧邻 03:42:36 的 `AI测试用例生成系统 正在关闭...`
—— 疑似机制层探针（自愈探针）创建独立 app 实例、其 lifespan 触发了全局 `settings` 单例重载。
`operation_logs` 中**没有**任何 LLM 配置修改记录，排除人工改配置。

> 这条对本轮结论的影响：本轮 L2/L3 失败的直接原因仍是供应商长请求超时；
> 但「故障转移到已删除的僵尸供应商」是本轮新发现的独立缺陷，需单独修。

### 2.3 环境归因（必须与被判定对象区分）

- 短请求正常：连通性测试 `日日新` 1.1s / 2.35s 均成功。
- 长请求（SRR 解析、GenAgent 生成、补生成）持续首响应超时：09-20 与 09-21 连续两天、跨 4 个供应商（日日新 / qwen / openrouter / 火山）全部命中 180s 首响应超时，另有 1 次 `deepseek` 报 `Error code: 40`（额度/鉴权侧）。
- **结论：这是外部供应商问题，不是系统缺陷。但系统把它的损失放大了**——见第 4 节缺陷 B。

### 2.4 生成期的可观测质量信号（本轮唯一拿到的真实数据）

- `动态数量下限: min_cases=5，要点总数=8`
- 兜底通道下 `fp_count=0` → 覆盖分母只来自总 SRR 的 4 类要点，**缺 `business_rules` / `data_constraints` 两个维度**（docs/13 §4.4 的 R3 风险在本轮实测复现）
- `few_shot=3`（Top-3 相似度 0.809~0.822）、`patterns=3` — 知识注入正常
- 类型分布（第 1 轮）：functional 6 / exception 2 / security 2 / boundary 1

---

## 3. L3 内容层：**本轮真实产物判定（ok 7 / bad 2 / doubt 1）**

> **本节已更新**：缺陷 B 修复后重跑（run `20260921-064549`），流水线正常收尾、10 条用例保住、落库 9 条，
> 证据包 `cases.json` 完整产出。L3 由 N/A 转为**有本轮真实内容可判**。
> 判定结果已落盘：`/app/_review_runs/20260921-064549/verdicts-auto.json`。
> 第 3.2 节保留 2026-09-16 历史批次判定作为**对比基线**（不再作为本轮结论的替代品）。

### 3.0 本轮运行证据（`run.json`）

| 项 | 值 | 说明 |
|---|---|---|
| 终态 | `awaiting_human_review` | 合法终态 |
| 发布决策 | `human_review` | 9 条全部进入待人工，0 条 auto_approve |
| 用例数 | 10（落库 9） | 结构断言 `cases=10` 通过 |
| 平均置信度 | 0.94 | — |
| 覆盖率 | **8 / 33 = 24.2%**（`mapped`，标注率 0.9） | 真实映射口径 |
| 非法要点引用 | `[]` | 无 |
| **降级标记** | **`fp_generate_failed`、`review_llm_failed`** | 两个都亮 |
| review passed / blocked | 0 / 0 | ⚠️ 见下方说明 |

> ⚠️ **`passed=0 / blocked=0` 不代表质量合格**：`review_llm_failed` 说明 6 维 LLM 审核没跑成，
> 只剩规则预检与多因子置信度。规则预检没抓到 blocker ≠ 内容合格。
> 同理 `avg_conf=0.94` 是在 `match_score` 来自向量检索、无 LLM 审核修正下得出的。

### 3.1 本轮 10 条逐条判定

需求口径（REQ-1787723370912-1404）：连续失败 5 次→锁定；锁定时长**固定 1 分钟**；
锁定期间禁止登录并**实时提示剩余锁定时间**；到期自动解锁且失败计数清零；
失败计数按账号维度、成功登录清零；密码错误提示统一返回「账号或密码错误」，不区分账号不存在。

| # | ID | 类型 | 标题 | 判定 | 理由 |
|---|---|---|---|---|---|
| 1 | TC-登录安全-3059-5717 | regression/P2 | [回归]并发登录同一账号失败计数准确性 | ⚠️ **doubt** | ① `covered_point_ids` 为 **None**，无需求追溯，违反「每个用例必须能从 SRR 找到依据」；② 并发场景需求原文未定义，属超出需求范围的推断；③ 断言仅 2 条且全在 DB 层，无接口层可观测 |
| 2 | TC-登录安全-9737-4363 | integration/P0 | 正确账号密码登录成功并清零失败计数 | ✅ ok | 4 条断言齐全。注：前置「失败计数为 0」时 `fail_count=0` 断言平凡成立，实质清零验证依赖 #10 |
| 3 | TC-登录安全-9737-4364 | integration/P0 | 连续失败5次触发账号锁定并提示锁定消息 | ✅ ok | 5 次 / 1 分钟 / 文案与 SRR 原文完全一致 |
| 4 | TC-登录安全-9737-4365 | integration/P0 | 锁定期间尝试登录被拒绝并显示剩余锁定时间 | ✅ ok | 三要素完整（拒绝 + 文案 + 剩余时间非空），本场景代表 |
| 5 | TC-登录安全-9737-4366 | integration/P0 | 锁定到期后账号自动解锁失败计数清零 | ✅ ok | 与「到期自动解锁且计数清零」一致 |
| 6 | TC-登录安全-9737-4367 | exception/P1 | 用户名不存在时登录失败提示统一返回 | ✅ ok | 防枚举，需求明确要求，本主题代表 |
| 7 | TC-登录安全-9737-4369 | exception/P1 | 锁定后再次登录被拒绝验证非法状态转换 | ❌ **bad** | 与 #4 场景重复，覆盖要点完全相同（happy_path.4 + alternate_flow.1）；#4 含剩余时间提示更完整，保留 #4 |
| 8 | TC-登录安全-9737-4370 | security/P1 | 密码错误提示统一返回不区分用户存在与否 | ❌ **bad** | 与 #6 断言完全相同（message + distinguish_user），仅类型由 exception 改 security，无安全维度增量 |
| 9 | TC-登录安全-9737-4371 | boundary/P2 | 锁定期间显示剩余锁定时间准确性验证 | ✅ ok | 真边界：`lt 61 / gt 59` 与 30s 后 `lt 31 / gt 29`，与「固定 1 分钟」一致；与 #4（仅 not_null）不重复，判定标准清晰 |
| 10 | TC-登录安全-9737-4372 | integration/P1 | 成功登录后失败计数清零验证 | ✅ ok | 前置失败计数=3 时清零——非平凡验证，是「成功登录清零」的实质用例；与 #2 边界不同，不算重复 |

**汇总：ok 7 / bad 2 / doubt 1**

### 3.2 本轮生成质量 vs 系统要求（对照）

| 系统要求 | 本轮实测 | 结论 |
|---|---|---|
| 断言含 `type` / `target` / `expected_value` | 10/10 均有可判定期望值（含 `lt/gt` 组合表达范围） | ✅ 达标 |
| 每个用例可独立执行、步骤含 action + expected | 9/10 达标；#7 仅 1 步 | ✅ 基本达标 |
| `covered_point_ids` 需求追溯 | 9/10 有；**#1 为 None** | ⚠️ 1 条不达标 |
| 场景唯一（跨子轮去重） | 重复 2/10 = **20%** | ✅ 较历史批次（47.6%）明显改善 |
| 需求口径一致（5次/1分钟/提示统一/计数清零） | 10/10 无口径冲突 | ✅ 达标（历史批次曾出现「30 秒」冲突） |
| 覆盖率应如实反映 | 24.2%（8/33），`invalid_point_ids=[]`，`degraded_flags` 如实亮起 | ✅ 口径诚实 |

**一句话结论**：本轮生成的**单条用例质量达标且优于历史批次**（无口径冲突、断言可执行、重复率降到 20%），
但**覆盖率只有 24.2%**，且带了 `fp_generate_failed`、`review_llm_failed` 两个降级——
即「产出的每一条质量还行，但远没覆盖完需求，而且这一轮的质量把关（LLM 审核）本身是缺席的」。

---

### 3.3 附：2026-09-16 历史批次判定（对比基线，非本轮结论）

> 以下为缺陷 B 修复前、本轮无产物时判定的历史批次，**仅作对比基线保留**，不计入本轮 PASS。

#### 21 条逐条判定

来源：`/app/_review_runs/20260916-070031/cases.json`（2026-09-16 07:08 落库的 21 条，非本轮）
该轮运行证据：`outcome.status=completed`、`publication.decision=auto_approve`、
`coverage={status: mapped, annotation_rate: 1.0, covered: 7, total: 7, uncovered: 0}`、
`invalid_point_ids=[]`、`degraded_flags=[]`

判定标准（与测试计划一致）：① 可执行合同 ② 需求对齐 ③ 场景唯一性 ④ 需求口径

| # | ID | 类型 | 标题 | 判定 | 理由 |
|---|---|---|---|---|---|
| 1 | TC-AUTH-5422-6461 | functional/P0 | 正确账号密码登录成功并失败计数清零 | ✅ ok | 3 步 + 3 断言（type/target/expected_value 齐全），对齐 happy_path，口径「成功清零」正确 |
| 2 | TC-AUTH-5422-6462 | functional/P0 | 连续5次密码错误触发账号锁定并提示锁定消息 | ✅ ok | 口径 5 次 + 1 分钟 + 文案「账号已锁定，请1分钟后再试」与 SRR 完全一致 |
| 3 | TC-AUTH-5422-6463 | functional/P0 | 锁定期间正确凭证被拒+剩余时间+计数不变 | ✅ ok | 三要素完整，本场景代表（保留） |
| 4 | TC-AUTH-5422-6464 | functional/P0 | 锁定到期自动解锁并计数清零且可登录 | ✅ ok | 解锁场景代表 |
| 5 | TC-AUTH-5422-6465 | security/P1 | 用户名不存在与密码错误提示一致 | ✅ ok | 防枚举，需求明确要求，本主题代表 |
| 6 | TC-AUTH-5422-6466 | boundary/P1 | 连续失败4次不锁定、第5次锁定 | ✅ ok | 阈值边界 4/5 定义清晰 |
| 7 | TC-AUTH-5422-6467 | boundary/P1 | 锁定期间第6次计数保持5不增加 | ✅ ok | 与「锁定期间不增加计数」一致 |
| 8 | TC-AUTH-5422-6468 | boundary/P1 | 1 分钟到期前后登录行为边界 | ✅ ok | 到期前/后双侧，与「固定 1 分钟」一致 |
| 9 | TC-AUTH-5422-6469 | exception/P1 | 锁定期间正确凭证仍被拒（锁定优先级） | ❌ **bad** | 与 #3 场景重复；#3 更完整（含剩余时间 + 计数不变），保留 #3 |
| 10 | TC-AUTH-5422-6470 | exception/P1 | 失败计数跨独立会话保持（账号维度） | ✅ ok | 需求明确「按账号维度统计」 |
| 11 | TC-AUTH-5424-6472 | integration/P0 | 连续登录失败5次触发锁定并提示 | ❌ **bad** | 与 #2 重复（同一场景的 integration 通道改写），保留 #2 |
| 12 | TC-AUTH-5424-6473 | integration/P1 | 锁定期间被拒并提示剩余锁定时间 | ❌ **bad** | ① 与 #3 重复；② **口径冲突**：断言写死「请30秒后再试」，需求是固定 1 分钟 |
| 13 | TC-AUTH-5424-6474 | integration/P1 | 1分钟到期后自动解锁且计数清零 | ❌ **bad** | 与 #4 重复 |
| 14 | TC-AUTH-5424-6475 | boundary/P2 | 连续失败4次不触发锁定 | ❌ **bad** | 与 #6 重复（#6 含 4 次不锁定 + 第 5 次锁定，更完整） |
| 15 | TC-AUTH-5424-6476 | boundary/P2 | 到期前最后一次尝试仍被拒 | ❌ **bad** | 与 #8 重复（#8 含到期前后双侧） |
| 16 | TC-AUTH-5424-6477 | exception/P1 | 锁定期间输入错误密码计数不增加 | ❌ **bad** | 与 #7 重复 |
| 17 | TC-AUTH-5424-6478 | exception/P1 | 不存在账号与密码错误提示统一 | ❌ **bad** | 与 #5 重复 |
| 18 | TC-AUTH-5424-6479 | security/P1 | 统一错误提示防止用户名枚举 | ❌ **bad** | ① 与 #5 重复；② 断言仅 `custom:response.messages=None`，**无可判定期望值**，不可执行 |
| 19 | TC-AUTH-5424-6480 | security/P1 | 连续失败5次锁定策略阻止暴力破解 | ❌ **bad** | 与 #2 重复（断言实质相同，无安全维度增量） |
| 20 | TC-AUTH-5424-6481 | integration/P0 | 账号状态迁移 正常→锁定→解锁 全流程 | ✅ ok | 真正跨状态迁移，覆盖 3 个要点，本批唯一的全流程用例 |
| 21 | TC-AUTH-5424-6482 | integration/P2 | 多条件组合登录验证（决策表） | ⚠️ **doubt** | 断言仅 `custom:combined_results=None`，判定标准不清、无法执行，需人工确认组合矩阵 |

**汇总：ok 10 / bad 10 / doubt 1**

#### 与系统要求的一致性对照（历史批次）

| 系统要求（docs/13 / gen_agent 契约） | 实测结论 |
|---|---|
| 断言须含 `type` / `target` / `expected_value` | 21 条中 19 条满足；**#18、#21 用 `custom` + `expected_value=None`，判定标准缺失，不可执行** |
| 每个用例必须可独立执行、步骤含 action + expected | 全部满足（步数 2~4，expected 齐全） |
| `related_requirement` / `covered_point_ids` 追溯 | 全部带 `covered_point_ids`，无 `invalid_point_ids` |
| 场景唯一（四级去重应当消灭跨子轮重复） | **未达标**：21 条里 10 条是重复（47.6%）。根因是 **FP 分片 + 补生成各自子轮内部去重，覆盖不了跨子轮重复**（docs/13 §4.3 已记载该风险，本批数据实测复现） |
| 需求口径一致（5 次 / 1 分钟 / 提示统一 / 计数清零） | 20/21 一致；**#12 断言「请30秒后再试」与「固定 1 分钟」冲突** |
| 覆盖率口径 | `annotation_rate=1.0`、`covered 7 / total 7 = 100%`。**注意**：分母只有 7 个要点（总 SRR 的 4 类），`non_functional` / `api_definitions` / `user_roles` 等 10 个字段完全不在分母里（docs/13 §4.4 R2），这个 100% 是**维度缩水后的 100%**，不等于需求被全覆盖 |

---

## 4. 缺陷清单（按优先级）

### A. 检查点后端缺依赖 → 静默降级内存（P0，本轮实证 → 修复复发 → **运行时已恢复，根治中**）

> **状态：止血 09-22 已完成并实测（探针 6/6，AsyncPostgresSaver，检查点已落 PG）；base 镜像重建根治进行中。**

**修复史（完整链条，含一次复发）**：

1. **09-21 `88d4378`（止血 + 防复发半成品）**：容器内 pip install 补装 + Dockerfile.base 加构建期校验
   （`python -c "import langgraph.checkpoint.postgres, psycopg_pool"`，缺包即构建失败）。
   当时验证探针 6/6——但验证的是**运行时状态**，不是「镜像重建后仍 6/6」。
2. **09-22 08:22 容器重建 → 复发**：`docker compose up -d --build` 只重建应用层镜像（秒级，FROM 老基础镜像），
   运行时补装被抹掉。实测：`langgraph.checkpoint.postgres` MISSING、探针回落 2/4 失败。
   **两个真根因**：
   - **基础镜像从未重建**：`ai-test-gen-base` 构建于 **2026-07-13**，而依赖是 **09-16**（`839248a`）才加入
     requirements.txt 的——不是 88d4378 猜的「构建缓存命中旧层」，是 base 镜像整体落后两个月的依赖。
   - **日常部署命令不覆盖依赖层**：`docker-compose.yml` 顶部注释推荐的日常命令 `up -d --build` 只走
     `backend/Dockerfile`（只 COPY 代码），依赖装在 `docker-compose.base.yml` 的 base 服务里，**日常链路永远不装新依赖**。
3. **09-22 止血（本次）**：容器内补装 `langgraph-checkpoint-postgres==2.0.16` + `psycopg[binary,pool]==3.3.5`
   （版本按 requirements.txt 锁定），重跑探针 **6/6 全过**，后端类型确认为 AsyncPostgresSaver。
   依赖漂移全量扫描：requirements.txt 25 项声明中仅缺这 2 项，无其他漂移。
4. **根治（进行中）**：重建 base 镜像（`docker-compose -f docker-compose.base.yml build base`，15-25 分钟），
   88d4378 加的构建期校验会首次真正执行——缺包即构建失败，保证新 base 镜像自带完整依赖。
   **遗留动作（base 构建完成后）**：
   - 重建应用层镜像并重启（`docker-compose up -d --build`）——让容器用新 base；
   - 验证：`python -c "import langgraph.checkpoint.postgres, psycopg_pool"` + 探针 6/6。

**遗留风险（修复后仍存在，需单独立项）**：
- **能力降级未 fail-closed**：`_ensure_checkpoint_backend` 的降级只「可见」（ERROR 日志 + degraded_flag），
  高风险场景应 fail-closed——至少让生成链路在 `checkpoint_backend_degraded=true` 时不再声称支持断点续跑。
  **代价**：PG 不可用时生成会直接拒绝启动，而不是带病运行。

**部署纪律（新增，防止第三次复发）**：
- **改 requirements.txt ≠ 完成**。必须配套执行：`docker-compose -f docker-compose.base.yml build base` →
  `docker-compose up -d --build`。日常 `up -d --build` **不会**把新依赖装进镜像。
- **镜像依赖漂移应纳入机制层探针**：把「requirements.txt 声明 vs 容器实装」全量比对做成探针
  （本次手扫 25 项仅缺 2 项——扫描脚本已验证可行，可直接固化）。

### B. 补生成异常击穿整条流水线（P0，本轮实证 3/3 → **已修复**）

> **状态：已修复并推送**，commit `7db82c9`。修复后重跑：流水线由「异常」变「正常完成」，
> 10 条用例保住、落库 9 条、结构断言 6/6 PASS。

- 现象：主用例集已生成（11 条 / 9 条），补生成 LLM 调用超时 → `run_pipeline` 抛异常 → **全部丢弃、0 落库**
- 影响：LLM 抖动时 100% 零产出，已消耗的额度与等待全部浪费；用户侧表现为「跑了 10 分钟什么都没有」
- **修复与代价**：把补生成本身做成 best-effort——`try/except` 捕获后置 `supplement_failed=True`（与已有 `supplement_exhausted` 同型），保留主用例集继续走 review → persist。
  **代价**：补生成失败时覆盖率缺口不再被填补，done 事件必须如实带 `supplement_failed` 让前端提示「覆盖不全，可重试补生成」。
  **不建议**的做法：把超时预算调大——本轮 180s 首响应超时是供应商侧问题，加大预算只是把 10 分钟变 20 分钟

### C. `finalize_requirement` LLM 失败路径 UnboundLocalError → 500（P1）

- 现象：`cannot access local variable 'finalize_result' where it is not associated with a value`
- 修复：失败分支先给 `finalize_result` 赋默认值再进异常处理，返回结构化 4xx/5xx
- 代价：无副作用，纯健壮性修复

### D. 覆盖口径维度缩水（P1，本轮实测复现 → **已修复 2026-09-22**）

> **状态：已修复并推送**，commit `3414830`。
> 回归：unit 117/1 + phase2 74 + phase3 91 + phase4 158 + phase5 112 + phase6 141 全绿；
> 桩测 5/5（提取白名单 / 标注后可判已覆盖 / 总 SRR 清单透出 / FP 子 SRR 清单透出 / 空扩展字段无回归）。

- 现象：兜底通道下 `要点总数=8`、历史批次 `total=7`，`non_functional` / `api_definitions` / `user_roles` / `dependencies` / `ui_rules` 等 10 个字段既不计分也不告警，而 prompt 里又完整注入了这些字段
- 结果：**LLM 为其生成了用例不计分；完全没生成也不告警、不触发补生成、`degraded_flags` 不亮**（docs/13 §4.4 R2 的不对称性）
- **修复内容（三处联动，缺一不可——"打标记必须到达消费方"）**：
  1. `utils/coverage.py::_add_extended_points`：5 类扩展字段纳入 `collect_points` 分母（总 SRR + FP 子 SRR 两级；空串/空列表不掺分母，`non_functional` dict 型按维度拆分）；
  2. **标注提取白名单同步扩展**：`_POINT_ID_RE`/`_BARE_POINT_ID_RE` 加入 5 类新 ID（含 `non_functional.{key}` 词尾形态兼容）——实测发现若只改 ①，用例标注了也提取不出来 → 恒判"未覆盖" → 假触发补生成，比不修更糟；
  3. `prompts/gen_agent.py::_build_coverage_checklist`：总 SRR 与 FP 段落透出 5 类扩展要点（LLM 看不到要点 ID 就无从标注，分母只会凭空变大）。
- **代价**：覆盖率数值会明显下降（分母变大），历史口径不可比，需要一次性重新标定基线（`stats` 已带 5 类新键可观测分母构成）。

### E2. 供应商配置热重载只增不删 → 僵尸供应商参与故障转移（P1，本轮实证）

- 现象：DB `llm_config` 3 行，运行时 `available_providers` 却有 6 家（`qwen`/`qwen1`/`qwen2` 残留）；
  04:00 的故障转移实际调用了 DB 里已不存在的 `qwen:qwen3.8-max`
- 根因：`_apply_config_to_settings()` 只做 `settings._all_provider_configs[provider] = {...}`，
  重载前不清空旧 dict
- **修复与代价**：`load_db_config_to_settings` 在应用前先 `settings._all_provider_configs.clear()`。
  **代价**：极低，纯清理。但要注意——若某个 provider 只在内存里（从未落库）被依赖，清理后会立即失效；
  本项目的 provider 全部来自 DB，无此风险
- 附带：建议在 `load_db_config_to_settings` 里对比「DB 集合 vs 内存集合」，把被移除的 provider 打 INFO 日志，
  否则这类残留永远不会有人发现

### E3. 「设为主供应商」保存后不生效（P0，用户反馈 → **已修复**）

> **状态：已修复并推送**，commit `08a0358`（方案 A：后端以请求体 `is_primary` 为准）。
> 回归：`pytest tests` 117 passed / 1 skipped；`test_phase5_api.py` 107 项全过；
> 机制层无新增失败（唯一失败仍是既有缺陷 A `_probe_checkpoint_saver.py`）。

**现象**：在设置页对某供应商点「设为主供应商」→ 保存 → 重新打开，主供应商还是原来那一家。

**根因：前后端的主供应商判定口径不一致。**

后端 `backend/app/api/settings.py:421`（`update_llm_config`）：

```python
is_primary=(idx == 0),  # 列表第一项为主供应商
```

—— **完全忽略**请求体里提交的 `is_primary` 字段（`LLMConfigItem.is_primary` 存在，前端也确实提交），
只用「数组下标是否为 0」重新判定。

前端 `frontend/src/pages/Settings.tsx:121-123` 只改布尔值、不改数组顺序：

```ts
const setPrimary = (idx: number) => {
  setProviders(prev => prev.map((p, i) => ({ ...p, is_primary: i === idx })))
}
```

`handleSave`（:138-150）提交时也未对 `providers` 排序，顺序 = 页面列表原顺序。

**完整闭环**：

1. 用户对第 N 项点「设为主供应商」→ 前端只置 `is_primary=true`，数组顺序不变；
2. 保存 → 提交数组的第 0 项仍是**原来的**主供应商；
3. 后端 `is_primary=(idx==0)` → 第 0 项得 `true`，用户选的那项得 `false`；
4. 重新打开 → GET `/api/settings/llm` 按 `order_by(is_primary.desc(), id.desc())` 排序，
   **primary 永远排在第一位**；
5. 用户看到「还是原来的」。

**且 UI 上无法绕过**：页面没有拖拽排序，用户没有任何手段改变提交顺序；
而 GET 又总是把当前 primary 排在第一位 —— 形成死锁，除非目标项恰好已是第一项，
或删除排在它前面的所有供应商。

**修复方案（两个层面，建议都做）**

| 方案 | 改动 | 代价 |
|---|---|---|
| **A（推荐，改后端）** | 以请求体的 `is_primary` 为准：`primary_idx = next((i for i, it in enumerate(req.providers) if it.is_primary), None)`；无 true 或超过一个时归一化（取第一个 true，否则取 0）并把其余置 false | 需加「有且仅有一个 primary」的校验，否则 `llm_client` 主配置不稳；API 单独调用方若依赖「首项即主」的旧语义会变（本项目无此调用方） |
| **B（改前端）** | `setPrimary` 时把选中项移到数组首位：`[target, ...rest].map((p,i)=>({...p, is_primary: i===0}))` | 只改前端，后端 `idx==0` 的隐晦语义仍在；接口层单独调用时依然反直觉 |

推荐 **A**（后端是权威口径），B 作为一致性补充。

**临时解法（待用户授权后执行）**：直接改库一条 SQL 即可，比走 API 全量 `delete+insert` 安全得多——

```sql
UPDATE llm_config SET is_primary = (provider = '<目标供应商>');
```

改完需重启 API 容器让 `settings.LLM_PROVIDER` 重新加载（注意 E2 的僵尸残留问题，重启可一并清掉）。

> 说明：根因定位阶段未做写库实测（`update_llm_config` 是 `delete` 全表再 `insert`，
> 会重写用户配置行的 id）。**修复后已通过防回归用例 + 变异验证坐实**。

**修复内容**（`backend/app/api/settings.py`）

```python
marked = [i for i, it in enumerate(req.providers) if it.is_primary]
if len(marked) > 1:   # 多个 true → 取第一个并告警
    logger.warning("[API/settings] 提交了 {} 个 is_primary=true，归一化取第一个（{}）", ...)
elif not marked:      # 无 true → 回退第一项
    logger.info("[API/settings] 未提交任何 is_primary=true，回退第一项为主供应商（{}）", ...)
primary_idx = marked[0] if marked else 0
...
is_primary=(idx == primary_idx),   # 原为 (idx == 0)
```

并在保存日志里补上主供应商名（故障转移排查的第一个抓手）。

**防回归用例**（`backend/tests/test_phase5_api.py` 5.4b / 5.4c）

- 5.4b：提交 3 项、`is_primary=true` 放在**第 2 项** → 断言有且仅有一个 primary，且是 `provider_b`
- 5.4c：全不提交 `is_primary` → 断言回退第一项为主

**变异验证（防空洞通过）**：把判定临时改回 `idx == 0` 后重跑 ——

```
✗ 主供应商是请求体标记的那一项（provider_b）
✗ 非标记项未被置为主供应商
✗ 2/107 项测试失败
日志实证: [API/settings] LLM 配置已更新: 共 3 个供应商, 主供应商=provider_a   ← 被错判成第一项
```

恢复修复版后：`主供应商=provider_b`，107 项全过。
→ 两条新用例**确实能捕获该缺陷**，不是摆设。

**线上配置未受影响**：测试前后 `llm_config` 均为 3 行（id 202/203/204，key 长度 140/140/184，primary=日日新），
与备份一致。

### E. 跨子轮重复率 47.6%（P1，历史批次实测）

- 现象：21 条中 10 条语义重复，集中在 integration 通道与 FP 分片通道的同名场景
- 根因：四级去重只在子轮内生效；`persist_to_kb` 前的标题级去重只比标题，改写标题就绕过
- **修复与代价**：`persist_to_kb` 前增加一次**跨子轮语义去重**（embedding 相似度阈值，建议 0.88——本批 Few-shot 命中相似度落在 0.809~0.822，0.88 不会误伤正常用例，但会误伤同场景的不同边界值用例，需要人工抽查校准）。
  **代价**：阈值定错会误删合法边界用例；根治还差「去重决策留痕」——被删的用例要能追溯到是谁的同义

---

## 5. 本轮未做的事（明确列出）

- 未执行 `apply_verdicts.py --execute`：那会改库里用例的 approved/rejected 状态，且本次判定对象是历史产物，落地意义存疑。**等你拍板**。
- 未修改任何代码、未提交 commit。
- 未重新标定覆盖率基线（缺陷 D 的修复前置）。

## 6. 下一步建议（按性价比）

1. **先修 A + B**：A 是缺依赖，改动最小、收益最大；B 是本轮零产出的直接原因，改完 LLM 抖动时至少有产出。两项可合成一个 commit 或分两个。
2. **换/加一个可用的 LLM 供应商**：当前 4 家对长请求全部 180s 首响应超时，短请求正常。在供应商修好之前，任何跑生成的测试轮都会不稳定——**这是本轮 L3 判 N/A 的根本原因，不是测试计划本身的问题**。
3. 供应商恢复后重跑一次完整轮，拿本轮真实产物补上 L3 判定。
