# 人工参与环节操作方案（v3.2.1）

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
5. 在评估集回归通过后激活：

```bash
POST /api/review-reports/prompt-evolution/{record_id}/activate
{"confirmed_eval": true, "eval_notes": "回归通过：xx 用例集，Pass^k 无退化"}
```

6. 出问题回退：`POST /api/admin/prompts/{module}/{name}/restore/{prompt_history_id}`（激活响应里带 history_id）

**纪律**：confirmed_eval=false 会直接 400——这是故意的门槛，不要为图省事先确认后补回归。

---

## 7. Prompt Cache 命中观测（P1-2）——零人工，定期看

```bash
docker exec ai-test-gen-pg psql -U aitestgen -d ai_test_gen -c \
"SELECT agent_name, SUM((token_usage->>'cache_hit_tokens')::bigint) AS cached, SUM((token_usage->>'prompt_tokens')::bigint) AS total FROM llm_call_logs WHERE created_at > now() - interval '7 days' GROUP BY agent_name;"
```

cached/total 比例持续为 0 → 检查 system prompt 是否被动态内容污染（时间戳、随机 ID 等进了前缀）。

---

## 8. 优先级建议

| 顺序 | 事项 | 投入 | 收益 |
|---|---|---|---|
| 1 | §1 金标标注 100 条基线 | 3-5h | 评审可信度的地基 |
| 2 | §5 评审异构路由启用 + §2 对比验证 | 0.5h | 消除同源偏见 |
| 3 | §4 每周失败回流 | 15min/周 | 评估集活资产 |
| 4 | §3 检索评估集随产品扩充 | 随用随加 | KB 质量可管理 |
| 5 | §6 提示词进化 | 随需 | 有门槛的持续进化 |
