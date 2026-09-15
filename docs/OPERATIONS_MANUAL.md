# 人工参与环节操作手册（v3.4）

> v3.4 新增：§8 生成结果审核流（发布决策解读 + 审阅台 + 判定落地）、§9 断点续跑（输入指纹）、
> §10 运维巡检（向量出箱积压 / 评估运行记录）、§11 测试与验收；§6 提示词激活门槛改为服务器端评估证据。
> 架构层面的变更见 `docs/00` §2.1 与 `docs/15`（实施进度表）。

> 依据《深入理解 AI Agent》对照体检落地的 8 项机制中，需要人工投入的环节及标准操作流程。
> 原则：**工具已自动化，人工只做"标注、判断、决策"三件事。**

---

## 1. Judge 金标校准（P2-4）——唯一必须人工标注的环节

**目的**：证明 ReviewAgent 的 6 维评审和人类判断一致（kappa>0.7），否则评审分数只是"另一个模型的意见"。

**标注流程**（预计投入：100-200 条 × 每条 1-2 分钟 ≈ 3-5 小时，可分多次）：

1. 选一份评审报告（建议用真实批量的报告，如 RR-1788851002563-8821）
2. 从报告 `issues` 中抽 60-100 个被标记的用例，再混入 30-50 个未被标记的用例
3. 逐条人工判断：**这个用例是否真的存在质量问题？**（对照需求原文，只判"是/否"）
4. 写入金标文件 `backend/eval/golden/review_golden.jsonl`，每行一条：

```json
{"case_id": "TC-工单管理-8465-7952", "human_flagged": true, "dimension": "deduplication"}
{"case_id": "TC-工单管理-8465-7960", "human_flagged": false}
```

5. 运行校准（容器内）：

```bash
python /app/scripts/calibrate_judge.py \
  --report-id RR-1788851002563-8821 \
  --golden /app/eval/golden/review_golden.jsonl
```

**判定**：退出码 0（kappa>0.7）→ 评审可用于决策；1 → 禁止（先调 prompt 或换 judge 供应商再校）。**换 judge 供应商或改评审提示词后必须重校**。

**何时做**：启用"评审异构路由"之前做一次基线；之后每次切换 judge 供应商重做。

---

## 2. 双配置对比评估（P2-3）——用统计口径代替"感觉变好了"

**场景**：想验证某个改动（如评审异构路由、提示词进化）是否真的更好。

**流程**：

1. 同一批需求分别在配置 A / 配置 B 下生成，各自保存逐用例结果 JSONL：

```json
{"case_id": "TC-xxx", "passed": true}
```

2. 配对比较（只看两配置结果不同的用例，扣除题目难度噪声）：

```bash
python /app/scripts/eval_stats.py pair /app/eval/run_a.jsonl /app/eval/run_b.jsonl
```

3. **决策纪律**：McNemar p<0.05 才允许切换；区间重叠（Wilson CI）一律不下结论。
4. 单配置稳定性检查：`ptrials --n 10 --c 6 --k 5`，回归验收看 **Pass^k**（全成功口径），探索看 Pass@k。

---

## 3. 检索质量评估集维护（P3-7）

**已有种子**：5 个真实问题（recall@20=1.0）。**维护节奏**：每新增一个产品/知识领域补 3-5 问。

1. 在 `backend/eval/retrieval_evalset.jsonl` 追加：

```json
{"query": "新领域的问题？", "product_id": "PROD-xxx", "expected_req_ids": ["REQ-理应命中的需求"]}
```

2. 跑评估：`python /app/scripts/eval_retrieval.py`
3. **管理指标**：检索失败率（1−recall@20）> 0.2 时优先调分块/检索参数，而不是调提示词。

---

## 4. 失败轨迹回流（P3-6）——每周跑一次

```bash
python /app/scripts/reflux_failures.py --limit 20 --days 7
```

产物 `backend/eval/failure_cases.jsonl`（幂等，重复跑不重复）。**每周 review 一次**：
- 失败原因归类（LLM 超时 / 格式错 / 逻辑 bug）
- 可复现的失败 → 加进回归测试；环境类失败 → 记录到运维清单
- 配合 trace 树定位：`GET /api/observability/trace/{task_id}` 看每个 Agent 的 LLM 调用明细

---

## 5. 评审异构路由启用（P1-1）——机制已备，选型你定

**启用**：`PUT /api/settings/review-llm {"provider": "<供应商名>"}`
**关闭**：`PUT /api/settings/review-llm {"provider": ""}`（评审跟随主力，同源模式）

**启用前检查清单**：
- [ ] 完成 §1 金标校准（校准的就是这个 judge）
- [ ] 确认评审供应商与生成主力**不同模型家族**（如生成=deepseek 系 → 评审=qwen/sensenova 系）
- [ ] 评审供应商强度与生成相近（太弱的模型"审不动"，见书 Ch10）

**验证效果**：启用前后各跑一次同批需求生成，按 §2 做配对比较——**评审 issues 的 precision 提升**（用金标子集人工复核）才是成功标准。

---

## 6. 提示词进化流程（P2-5）——从采纳到激活

1. 评审报告页采纳意见（adoption_status=accepted）
2. `POST /api/review-reports/prompt-evolution`（**不传 activate**）→ 产物进候选区（status=pending）
3. 查看候选：`GET /api/review-reports/prompt-evolution/candidates?status=pending`
4. 人工审阅候选内容（GET 详情核对沉淀规则是否合理、无冲突）
5. 跑**服务器端评估**（v3.4 起，激活不再认客户端声明）：

```bash
docker exec ai-test-gen-api python /app/scripts/eval_prompt_candidate.py --integration-id PI-xxx
# 在冻结评估集上同时评估「候选」与「当前线上版本」（基线对比），输出：
#   status=passed/failed + evaluation_run_id = EV-xxxx
# 常用参数：--limit 2（省额度冒烟）/ --threshold 0.75 / --offline（只验管道，不可用于激活）
```

6. 用评估运行 ID 激活（服务器校验：status=passed、目标一致、有数据集 hash、口径版本一致、
   **prompt_hash 与候选逐字一致**、有基线对比记录——任一不满足直接 400）：

```bash
POST /api/review-reports/prompt-evolution/{record_id}/activate
{"evaluation_run_id": "EV-xxxx", "eval_notes": "回归通过：prompt_candidate_v1，候选 1.0 / 基线 0.75"}
```

7. 出问题回退：`POST /api/admin/prompts/{module}/{name}/restore/{prompt_history_id}`（激活响应里带 history_id）

**纪律**：`confirmed_eval` 字段已废弃，传了也不算数——评估必须在服务器端跑出来并落
`evaluation_runs`。候选文本在评估之后改过一个字，激活也会被拒（prompt_hash 逐字比对），
必须重新评估。

---

## 7. Prompt Cache 命中观测（P1-2）——零人工，定期看

```bash
docker exec ai-test-gen-pg psql -U aitestgen -d ai_test_gen -c \
"SELECT agent_name, SUM((token_usage->>'cache_hit_tokens')::bigint) AS cached, SUM((token_usage->>'prompt_tokens')::bigint) AS total FROM llm_call_logs WHERE created_at > now() - interval '7 days' GROUP BY agent_name;"
```

cached/total 比例持续为 0 → 检查 system prompt 是否被动态内容污染（时间戳、随机 ID 等进了前缀）。

---

---

## 8. 生成结果的审核流（v3.4 HITL）——"为什么没自动入库"与判定落地

### 8.1 先看懂「发布决策」

生成完成 ≠ 已入库。done 事件与生成页顶部的**发布决策面板**会给出：

| 终态 | 含义 | 你要做什么 |
|---|---|---|
| `completed`（auto_approve） | 高置信用例已自动入库并写向量库 | 无需操作 |
| `awaiting_human_review`（human_review） | 用例已入库为「待审核」，**未写向量库**，暂不参与复用 | 逐条 approve / reject |
| `failed`（blocked） | 内容不合格或为空，**PG 与向量库均未写入** | 修正后重新生成，历史数据未被污染 |

未自动核准的原因（reason 码）都带中文说明，常见：
`coverage_unknown`（用例没标注 SRR 要点，无法证明覆盖）、`review_blockers`（审核有 blocker）、
`invalid_point_references`（引用了不存在的要点 ID）、`review_gaps_unresolved`（覆盖缺口未补齐）。

### 8.2 审阅台：内容回传 + 逐条判定

```bash
# 跑一次真实生成并产出审阅台（单文件 HTML，产物在容器 /tmp，不会触发热重载）
docker exec ai-test-gen-api python /app/scripts/review_generation_run.py --req-id REQ-xxx
docker cp ai-test-gen-api:/app/_review_runs/<时间戳> ./_review_runs/
```

审阅台里每条用例有步骤表/断言表/覆盖要点/入库状态，可打「符合 / 不符 / 存疑 + 备注」
（浏览器本地保存，可导出 JSON）；也可用 `--task-id`（从已有任务重建）或 `--from-run`（只重渲染）。

### 8.3 判定落地（approve / reject 批量执行）

```bash
# 默认干跑：先看影响面（各类条数、不在库中的条目、执行前后状态分布）
docker exec ai-test-gen-api python /app/scripts/apply_verdicts.py --run-dir /app/_review_runs/<ts>
# 确认后执行（自动快照执行前状态到 status-before-apply.json，可回滚核对）
docker exec ai-test-gen-api python /app/scripts/apply_verdicts.py --run-dir /app/_review_runs/<ts> --execute
```

- 符合 → 批量 approve（PG `approved` + 出箱投递写向量库，建立回归基线）
- 不符 → 批量 reject（备注写明与谁重复）
- 存疑 → **不动**，保持 reviewed 等你人工确认

⚠️ **"approve 了" ≠ "KB 能用上"**：审核只置 `approved`，`archived_at`（固化）由**产品/项目归档
流程（第 11b 步）**写入。按 KB 问答口径，未固化的用例暂不作为问答依据；但 few-shot 复用不受影响。

---

## 9. 断点续跑（v3.4）——续跑前有"输入指纹"校验

```bash
POST /api/generate
{"req_id": "REQ-xxx", "resume": true}                        # 自动找最近可续任务
{"req_id": "REQ-xxx", "resume": true, "resume_from_task": "TASK-xxx"}   # 指定来源
```

续跑前系统会重算**输入指纹**（SRR 内容 / 功能点集合 / 需求修订号 / prompt 版本 / 模型 / 合同版本）
并与断点比对：

- **一致** → 从断点继续，已完成的功能点不再重新生成（进度卡显示"已续跑"）；
- **不一致 → 409**，文案逐字段说明哪里变了（如「需求修订号（999 → 6）」）——
  这是防止"改完需求把旧用例拼上来"的保护，不要绕；
- 确实要放弃旧断点重跑：加 `"fallback_to_restart": true`。

注意：断点续跑同样受并发互斥约束（同需求有任务在跑时 409）。

---

## 10. 运维巡检（v3.4 新增两件事）

### 10.1 向量出箱积压（PG→Milvus）

审核通过 / 需求入库的向量写**不再同步直写**，走 `vector_outbox` + relay（5s 一轮，失败退避重试）。
定期看积压：

```bash
docker exec ai-test-gen-api python -c "import asyncio; from app.core.vector_outbox import pending_summary; print(asyncio.run(pending_summary()))"
```

- `by_status` 里 pending 长期为 0 → 正常；
- pending 持续增长 → 看 api 容器日志里的 `[VectorOutbox] 投递失败将重试`；
- `failed` > 0 → 先看 `error` 字段（多为 Milvus 不可用），恢复后跑一次对账补写：

```bash
docker exec ai-test-gen-api python -c "import asyncio; from app.core.vector_outbox import reconcile_cases; print(asyncio.run(reconcile_cases(requeue_missing=True)))"
```

（`reconcile_cases` 默认**只补缺失、不删孤儿**；孤儿/陈旧向量清单会打印出来，确认后再传
`apply_deletes=True`。）

### 10.2 评估运行记录（prompt 激活证据）

`evaluation_runs` 表记录每次候选评估（数据集/prompt hash、指标、逐样本结果）。
"这条提示词当时凭什么上线"→ 查这张表；激活被 400 拒绝时，错误信息会指明缺哪项证据。

---

## 11. 测试与验收

```bash
# unit 层（CI 同款，无需 PG/Redis/Milvus，失败即阻断）
python -m pytest backend/tests -q

# 集成层（脚本式套件，需要真实服务）
AITG_RUN_INTEGRATION=1 python -m pytest backend/tests

# 容器内跑全套脚本式回归（421 项基线）
docker exec ai-test-gen-api python /app/tests/test_phase2_core.py   # …phase3/4/5 同理

# 专项验收探针（不调 LLM，秒级）
docker exec ai-test-gen-api python /app/_probe_concurrency.py   # 并发互斥 + SSE 隔离（11 项）
docker exec ai-test-gen-api python /app/_probe_outbox.py        # 出箱三崩溃点收敛（13 项）
docker exec ai-test-gen-api python /app/_probe_durable.py       # 断点续跑 + 指纹（8 项）
```

---

## 12. 优先级建议

| 顺序 | 事项 | 投入 | 收益 |
|---|---|---|---|
| 1 | §1 金标标注 100 条基线 | 3-5h | 评审可信度的地基 |
| 2 | §5 评审异构路由启用 + §2 对比验证 | 0.5h | 消除同源偏见 |
| 3 | §4 每周失败回流 | 15min/周 | 评估集活资产 |
| 4 | §3 检索评估集随产品扩充 | 随用随加 | KB 质量可管理 |
| 5 | §6 提示词进化 | 随需 | 有门槛（服务器端评估）的持续进化 |
| 6 | §8 每次生成后走审核流（审阅台 → 判定 → apply_verdicts） | 10-20min/次 | 用例库只进合格且不重复的内容 |
| 7 | §10.1 每周看出箱积压与对账 | 5min/周 | PG↔Milvus 不一致自愈可观测 |
