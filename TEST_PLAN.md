# AI-Test-Gen 测试计划（v5.0 · 执行计划）

> **与旧版的根本区别**：测试计划不再是一堆"脚本断言内容"的用例清单，而是一个**执行计划**——
> 脚本负责调接口跑流程、采集证据、验证结构与机制；**系统生成的内容不再用脚本断言，
> 而是原样回传给 AI 逐条判定**——内容质量的判断 AI 比硬编码断言精准得多。
> 旧版计划（v4.x，12 阶段手工脚本清单）已归档：`docs/TEST_PLAN-v4-archived.md`。

## 1. 三层测试模型

| 层 | 验证什么 | 谁来判定 | 怎么执行 |
|---|---|---|---|
| **L1 机制层** | 机制不变量：并发互斥、出箱幂等、状态守卫、输入指纹、乐观锁、检查点落库 | **脚本**（客观、秒级、可重复——机器判比人准） | 六个探针 + 421 回归 + unit，`run_test_plan.py` 自动串跑 |
| **L2 流程层** | 流程结构：**从需求分析到生成全链路**——上传→解析→功能点→澄清→确认→生成→采集；接口可达、终态合法、字段齐全、无非法引用 | **脚本**（只做**结构断言**，零内容断言） | driver 驱动真实 API 全链路，逐项结构检查 |
| **L3 内容层** | 内容质量：用例是否对齐需求、场景是否唯一、数值口径是否正确、KB 回答是否可信 | **AI**（逐条判定，按统一标准给 ok/bad/doubt + 理由） | 产物打包回传（审阅台 + JSON）→ AI 判定 → `apply_verdicts.py` 落地 |

**判定权划分的依据**：结构/机制是客观不变量，脚本断言精确且无成本；
内容质量是语义判断（"这个场景是否真的来自需求"、"这两条是否算重复"），硬编码断言
只能覆盖皮毛（字面匹配/数量），AI 判定才能覆盖语义。**旧版计划最大的问题就是把内容
判断塞进了脚本**——断言越写越脆，漏判误判都多。

## 2. 执行入口

所有命令在容器内执行（`docker exec ai-test-gen-api python ...`）：

```bash
# ── 完整执行（L1 机制 + L2 流程 + L3 内容回传）──────────────────
# 真跑一次生成（约 25 分钟，消耗额度）
python /app/scripts/run_test_plan.py --req-id REQ-xxx

# 复用最近一次已有产物（不触发生成；重新判定历史 run 用这个）
python /app/scripts/run_test_plan.py --from-run /app/_review_runs/<时间戳>

# ── 只跑 L1 机制层（秒级，日常自检用）──────────────────────────
python /app/scripts/run_test_plan.py --only mechanism

# ── 只跑 L2/L3（已有机制层结论时）─────────────────────────────
python /app/scripts/run_test_plan.py --only flow --req-id REQ-xxx
```

**产出**（`/app/_review_runs/plan-<时间戳>/`）：

| 文件 | 内容 |
|---|---|
| `plan-results.json` | 三层结果汇总：机制层每步退出码、流程层结构断言逐项结果、内容层待判定清单 |
| `ai-review/cases.json` | 生成用例全文（AI 判定输入，含步骤/断言/覆盖标注/证据链） |
| `ai-review/run.json` | 运行证据（done 事件 / 发布决策 / 覆盖口径 / 降级标记） |
| `ai-review/AI-REVIEW-TASK.md` | AI 判定任务书（判什么 / 按什么标准 / 交付什么格式） |

## 3. L3 内容层：AI 判定闭环

```
run_test_plan.py 产出证据包
    → AI 逐条判定（标准：合同完整 / 需求对齐 / 场景唯一 / 数值口径一致）
    → 输出 verdicts JSON（ok / bad / doubt + 理由）
    → docker exec ... python /app/scripts/apply_verdicts.py --run-dir <run_dir> --execute
    → 符合 → approve（写向量库建回归基线）；重复/矛盾 → reject；存疑 → 保留等人工
```

判定标准四条（与系统口径一致，AI 判定任务书会随证据包生成）：

1. **可执行合同**：断言含 type/target/expected_value、步骤完整（L2 已结构校验，预期全过）；
2. **需求对齐**：场景是否真的来自 SRR 要点（对照 point_coverage 与需求原文）；
3. **场景唯一性**：与其他用例语义重复（同场景改写 = 重复；数字边界不同 = 不重复）；
4. **需求口径**：数值/规则是否与 SRR 原文一致（锁定次数、时长、提示文案等）。

## 4. L1 机制层清单（六个探针 + 回归）

| 探针 | 验证的机制不变量 | 断言数 |
|---|---|---|
| `_probe_concurrency.py` | 同 req_id 并发只占一个槽位；SSE 事件不串流 | 11 |
| `_probe_outbox.py` | 出箱三崩溃点（commit 前 / Milvus 后 / ack 前）均收敛且不重复 | 13 |
| `_probe_durable.py` | 断点续跑跳过已完成 FP；旧指纹被 409 拒绝 | 8 |
| `_probe_revision_lock.py` | 需求编辑乐观锁 409 / 放行 / 递增 | 7 |
| `_probe_checkpoint_saver.py` | 检查点落 PG（thread_id=task_id）；同线程重入 | 6 |
| `_probe_selfheal.py` | API 重启后自动续跑；指纹不一致跳过 | 6 |
| `_probe_evidence_claims.py` | claims 解析/校验/降级；证据链经 Pydantic 往返存活；failed 问答不得纠错归档；节点 span 归属 | 13 |
| `_probe_html_proto.py`（docs/17） | HTML 原型语义树机制不变量：安全剥离（script/on*/危险协议）、归一化压平、预算截断、降级标记四态、label 六级关联 | 由 unit 覆盖（见下） |

脚本式回归：phase2-5 共 421 项（机制不变量的历史回归）+ **`test_phase2_html_upload.py`（docs/17，4 组：HTML 上传→语义树/ui_rules、混传边界标记、恶意原型注入剥离、降级路径注入含 HTTP 全链路透出）**；
unit 层 125 + **40 项（`test_html_proto_extract.py`，docs/17：角色映射/label 关联/安全剥离/归一化/预算/降级/双入口对称性 3 项/内容校验）**。

> **分组运行（2026-09-28 新增）**：全量 4 组含 LLM 长流程需 20+ 分钟，单进程跑易被宿主回收（实测 12 分钟中断）。
> 用 `--case N` 拆开跑，如 `--case 4`（最快，纯注入）、`--case 1`、`--case 2,3`。
> **断言口径**：确定性行为（剥离/标记/结构/降级）强断言；LLM 自由生成项（`ui_rules` 条数、澄清问题文本）用 `soft=True` 只告警。

> **鉴权注记（2026-09-24）**：脚本式用例走 `POST /api/auth/login`（admin/888888）取 token，
> **不可用 `get_default_token()`**——`APP_SECRET_KEY` 为 API 进程级随机，脚本进程自签的 token
> 签名密钥与 API 进程不同 → 恒 401（对齐 `scripts/run_test_plan.py` 的既有做法）。

## 5. L2 流程层结构断言清单（driver 自动执行）

| 断言 | 说明 |
|---|---|
| done 事件存在 | 流水线正常收尾（而不是中途异常） |
| 终态合法 | completed / awaiting_human_review / failed 之一 |
| 发布决策存在 | 发布门禁产出了决策与原因 |
| 产出用例数 > 0 | 有内容产出（0 用例是否该 blocked → 内容层判定） |
| 无非法要点引用 | invalid_point_ids 为空（引用了不存在的要点 = 结构性错误） |
| 覆盖口径已产出 | mapped / unknown 之一（unknown 是否可接受 → 内容层判定） |
| 需求分析：解析完成 | 上传后异步任务 completed，SRR.requirement 存在、功能点 ≥ 1 |
| 需求分析：澄清闭环 | 有问题则回答并重解析完成；无问题则跳过（质量分达标） |
| 需求分析：确认成功 | 需求状态推进到 confirmed（生成的前置） |
| 需求分析：HTML 原型（docs/17） | 上传 .html 原型后任务 completed；`【原型：】`边界标记进 raw_text；语义树产出（textbox/button 行存在）；SRR ui_rules 抽取（条数属 LLM 依赖项，软断言）；恶意原型 script/on*/javascript: 均被剥离且用户可见产出未含注入文本；降级标记（proto_tree_truncated/proto_parse_timeout/proto_parse_failed/proto_no_semantics）经 `result_json.proto_degraded_flags` + `degraded`/`degraded_reason` 透出消费 |

## 6. 运行节奏建议

| 场景 | 执行 |
|---|---|
| 日常自检（改代码后） | `--only mechanism`（秒级） |
| 每周回归 | 完整执行（`--from-run` 复用 + 一次真跑生成） |
| 大版本发布前 | 完整执行 + AI 逐条判定 + 判定落地 + 人工抽查存疑项 |
| KB 问答验收 | `--req-id` 完整执行后，AI 额外判定 claims 的"结论→来源"是否成立 |

## 7. 与旧版计划的映射

旧版 12 个阶段的手工脚本清单已归档（`docs/TEST_PLAN-v4-archived.md`），映射关系：

- 旧"阶段 10/11/12"的生成全流程验证 → L2 流程层（结构断言）+ L3 内容层（AI 判定）；
- 旧阶段中的内容断言（"应有 5 个用例"、"应包含 XX 场景"）→ 全部移入 L3 由 AI 判定；
- 旧阶段中的机制验证（状态码、终态）→ L1/L2 保留；
- 附录 A（API 速查）/ 附录 B（数据保留清单）仍有效，见归档文件。
