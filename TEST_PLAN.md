# AI-Test-Gen 测试计划（v5.1 · 执行计划）

> **与旧版的根本区别**：测试计划不再是一堆"脚本断言内容"的用例清单，而是一个**执行计划**——
> 脚本负责调接口跑流程、采集证据、验证结构与机制；**系统生成的内容不再用脚本断言，
> 而是原样回传给 AI 逐条判定**——内容质量的判断 AI 比硬编码断言精准得多。
> 旧版计划（v4.x，12 阶段手工脚本清单）已归档：`docs/TEST_PLAN-v4-archived.md`。
>
> **v5.1（2026-10-14）**：根据 docs/31（v3.5.9 三版本全链路）暴露的计划盲区 + docs/32（修复定稿方案）的方案变更，补充 8 项 L1/L2/L3 测试项与 3 条执行纪律，变更明细见 §8。

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

1. **可执行合同**：断言含 type/target/expected_value、步骤完整（L2 已结构校验，预期全过）；**顶层 `expected`（整体预期结果）非空**（v5.1 新增——v3.5.9 实测 133/320 空缺，AI 判定需对空缺条打 `bad` 并标注 "expected_top_missing"，区别于 step 级缺失）；
2. **需求对齐**：场景是否真的来自 SRR 要点（对照 point_coverage 与需求原文）；
3. **场景唯一性**：与其他用例语义重复（同场景改写 = 重复；数字边界不同 = 不重复；**角色变体 = 不重复**——v5.1 新增：同功能不同角色（管理员/普通用户/访客…）是合法变体，不得判重，与 docs/32 B-6 闸门口径一致）；
4. **需求口径**：数值/规则是否与 SRR 原文一致（锁定次数、时长、提示文案等）。

**角色变体专项判定（v5.1 新增，源自 docs/32 探针实证）**：证据包中如存在同骨架不同角色的用例对（标题去掉角色词后高度相似），AI 判定任务书必须显式提示"角色变体 = 合法独立场景"，并输出变体对清单与各自的覆盖要点（`role.N`）核对——角色变体被误判或 `role.N` 要点覆盖为空 = FAIL。这是 L3 层对 L1 探针（`_probe_role_dedup.py`）的语义层印证，两层共同构成角色变体的防误删闭环。

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
| `_probe_role_dedup.py`（docs/32 B-6，v5.1 新增） | **去重角色变体护栏**：同功能不同角色（管理员/普通用户/访客/只读成员…）不得被判重——批内 3-gram 层、跨分片语义层（0.92）、GenAgent L2 标题层（0.95）三处全部不删；对照组（真重复判删、不同场景保留）行为不变。5 组探针对（3 变体 + 2 对照），容器内真实 bge-large 实测 | 10 |

脚本式回归：phase2-5 共 421 项（机制不变量的历史回归）+ **`test_phase2_html_upload.py`（docs/17，4 组：HTML 上传→语义树/ui_rules、混传边界标记、恶意原型注入剥离、降级路径注入含 HTTP 全链路透出）**；
unit 层 125 + **40 项（`test_html_proto_extract.py`，docs/17：角色映射/label 关联/安全剥离/归一化/预算/降级/双入口对称性 3 项/内容校验）**。

> **分组运行（2026-09-28 新增）**：全量 4 组含 LLM 长流程需 20+ 分钟，单进程跑易被宿主回收（实测 12 分钟中断）。
> 用 `--case N` 拆开跑，如 `--case 4`（最快，纯注入）、`--case 1`、`--case 2,3`。
> **断言口径**：确定性行为（剥离/标记/结构/降级）强断言；LLM 自由生成项（`ui_rules` 条数、澄清问题文本）用 `soft=True` 只告警。

> **鉴权注记（2026-09-24）**：脚本式用例走 `POST /api/auth/login`（admin/888888）取 token，
> **不可用 `get_default_token()`**——`APP_SECRET_KEY` 为 API 进程级随机，脚本进程自签的 token
> 签名密钥与 API 进程不同 → 恒 401（对齐 `scripts/run_test_plan.py` 的既有做法）。

> **降级标记透出断言（v5.1 新增，源自 docs/31 F1——本轮盲区 #1）**：L2 driver 必须对 done 事件做
> **"降级标记可达性"检查**：`review_report` 里的降级信号（`review_shard_partial_failure` / `shard_review`
> / `review_degraded` / `degraded_flags` 任一非空）必须能从 done 事件与 `outcome` 里**原样 grep 到**。
> 背景：v3.5.9 实测分片审核失败 1/5，13 条用例未审全部 approved，且失败标记被 `_merge_review_results`
> 重建报告时丢弃——**终态三处（done 事件 / result_json / degraded_flags）全部不可见**。该断言在
> docs/32 B-2 实施前**预期 FAIL（暴露缺陷）**，实施后必须 PASS（探针注入法：mock 一个分片异常，
> 断言三处标记齐全）。这是「打标记补丁必须验证标记到达消费方」纪律的机制化落点。

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
| **顶层 expected 非空率**（v5.1 新增，docs/31 F4——盲区 #2） | done 事件采集 `case_json.expected` 为空比例；v3.5.9 实测 133/320（v3.0 高达 74.5%）。**docs/32 B-4 实施前**：仅采集报告（结构事实，不强断言阈值——历史批次基线波动大）；**实施后**：新增生成批次空置率应显著下降（LLM 依赖项，软断言 + 采样人工复核），历史存量不回填不追断 |
| **安全断言方向抽检**（v5.1 新增，docs/31 F3——盲区 #3） | 采集所有 `type ∈ {contains, equals}` 且 `expected_value` 命中载荷模式（`<script`、`javascript:`、`DROP TABLE`、`onerror`、`<iframe`、`<style`、`<img`、`<!--`）的断言 → **硬断言 count == 0**（负向断言必须 `not_contains`/`not_in`，载荷本体不得进 `expected_value`）。v3.5.9 实测 13 条反向（安全防线失效时反而通过）。**docs/32 B-3 实施前预期 FAIL**（暴露缺陷），实施后必须 PASS；存量 13 条修复后一并清零 |
| **落库状态分布与发布决策一致性**（v5.1 新增，docs/31 F6——盲区 #4） | 采集 `approved/reviewed/draft/error_cases` 四态分布，与 `publication.decision` 对照：partial_block 批次的好条（置信度 ≥0.70、非 blocker）应正常分级入库。v3.5.9 实测 3 坏条连坐 193 好条全降 reviewed。**docs/32 B-1 实施前**：采集+对照报告；**实施后**：partial_block + 高置信好条 → approved（硬断言，注入法构造 3 blocker + N 好条场景） |
| **补生成收敛性透出**（v5.1 新增，docs/31 §2.3——盲区 #5） | done 事件采集 `supplement_effect`（补前/补后 uncovered 数）。v3.5.9 三轮补后 `should_supplement` 全 True（补了没效果不可见）。**docs/32 O-1 实施前**：字段缺失即记 WARN（字段尚不存在）；**实施后**：补生成轮必须含该字段，`after ≥ before` 时标 FAIL（补生成反向劣化） |
| **invalid_point_ids 归零**（v5.1 强化既有断言，docs/31 F2——盲区 #6） | 既有"无非法要点引用"断言保留，但 v3.5.9 实测 `invalid_point_ids=["api"]`（URL 路径被正则误提）——断言本身没跑错，是**正则错了而断言没覆盖到污染源**。v5.1 增加：采集 invalid 明细并分类（URL 提取型 / 标题裸词型），**docs/32 B-5 实施后**两类归零（硬断言）；实施前仅分类报告 |

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

## 8. v5.1 变更记录（2026-10-14）

> 来源：docs/31（v3.5.9 三版本全链路报告，320 条用例 L3 判定）暴露的 6 处计划盲区 + docs/32（修复定稿方案）的方案变更。**原则：本轮逃逸的缺陷，每一处都要落成测试计划里的新断言/新探针——同样的坑不允许第二次踩。**

### 8.1 本轮测试暴露的盲区（docs/31 实证）

| # | 盲区 | 本轮实证 | 落点 |
|---|---|---|---|
| 1 | **降级标记不可达没有断言**：L2 只断言"终态合法/决策存在"，没检查降级信号是否真的透出到消费方 | 分片审核失败 1/5，13 条未审用例全 approved，`review_shard_partial_failure` 在 done/result_json/degraded_flags 三处全部 grep 不到 | §4 鉴权注记下方新增"降级标记透出断言" |
| 2 | **顶层 expected 没有采集**：结构校验只覆盖 step 级（schema 必填），顶层 optional 字段无人看 | 133/320 空缺（v3.0 74.5%），completeness 分看不出缺口在哪层 | §5 新增采集项；L3 判定标准 1 补"expected_top_missing" |
| 3 | **断言方向无抽检**：schema 只验证字段存在，不验证 contains/equals 的语义方向 | 13 条安全断言方向反（载荷进 expected_value，防线失效反而通过） | §5 新增"安全断言方向抽检"硬断言 |
| 4 | **落库分布与发布决策无对照**：approved/reviewed 分布没和 publication decision 比对 | 3 坏条连坐 193 好条全降 reviewed | §5 新增"落库状态分布与发布决策一致性" |
| 5 | **补生成效果不可见**：只看 should_supplement 布尔值，没看补前补后变化 | 三轮补后仍全 True，"补了没效果"无从发现 | §5 新增"补生成收敛性透出"（O-1 配套） |
| 6 | **invalid_point_ids 断言被正则缺陷绕过**：断言存在且正确，但污染源（URL 提取/标题裸词）不在断言视野 | `["api"]`（URL 误提）、`["happy_path"]`（标题裸词）假阳性 | §5 强化既有断言：分类采集 + B-5 实施后归零硬断言 |

**通用教训（docs/31 §3.6 同款）**：v3.5.9 报告初版"孤儿数字幻觉扫描"命中 216 条，逐项追源后全部误报——**检测器本身必须先验证**。v5.1 新增断言全部先在 v3.5.9 既有产物（`_dogfood_out/` 三版本 JSON）上回放校准，确认能区分"真缺陷"与"检测器误报"后才进计划。

### 8.2 docs/32 方案变更带来的新测试项

| 方案项 | 测试项 | 层 | 生效时点 |
|---|---|---|---|
| B-6 角色变体护栏（分层闸门） | `_probe_role_dedup.py` 5 组探针（3 变体保留 + 2 对照正确删），三处去重点全过 | L1 | **已生效**（探针脚本已在仓库根，v3.5.9 产物可回放） |
| B-6 留痕落库 + 一键复活 | 去重 removed 记录落库可查；审阅台恢复操作后用例回到可检索状态（向量同步走出箱） | L2 | B-6 实施后 |
| B-6 LLM 裁决窄带（闸门2） | mock 单测三态：判非重复→保留留痕 llm_distinct / 判重复→合并 / LLM 异常→fail-open 保留 | unit | B-6 实施后 |
| B-1 连坐终版 | 注入法：3 blocker + N 高置信好条 → 坏条进 error_cases、好条 approved、出箱新增 N 条 | L2 | B-1 实施后（实施前仅采集对照） |
| B-2 fail-closed | 注入法：mock 1 分片审核异常 → 失败分片用例落 reviewed 而非 approved；`shard_review`/`review_shard_partial_failure`/`degraded_flags` 三处齐全 | L2 | B-2 实施后（实施前该断言预期 FAIL = 暴露缺陷，不算回归破损） |
| B-3 安全断言校验 | 生成端注入恶意载荷场景 → validation_issues 产出 `cases[N].assertions` 内容错误 → partial_block 剔除；正反例各 3（负向词∧载荷模式双条件，防误伤合法正向断言） | unit + L2 | B-3 实施后 |
| B-4 顶层 expected | 新生成批次空置率下降（软断言）；completeness 口径拆 `completeness_step`/`completeness_top` 两键透出 | L2 采集 + L3 判定 | B-4 实施后 |
| B-5 正则修正 | 三版本回放：URL 提取型与标题裸词型 invalid 归零；covered 计数变化记录前后对照 | L1（回放脚本） | B-5 实施后 |
| O-1 补生成收敛 | done 事件含 `supplement_effect{before,after}`；after ≥ before 标 FAIL | L2 | O-1 实施后 |
| O-3 driver 加固 | 空 answers 跳过该轮并显式标记（不再 400）；driver 状态落宿主卷，容器重启可续跑 | 测试工具自身 | O-3 实施后 |

### 8.3 执行纪律补充（docs/31 教训）

1. **验证任务在跑时绝不碰容器 `/app`**（docs/31 §1.1 实证：容器 00:15 重启一次，靠 restart 而非 recreate 才没丢数据——探针/脚本一律放 `/tmp` 或宿主，防 reload 杀在飞任务）；
2. **长流程一律后台任务 + 日志落盘**（`-d` 模式 stderr 曾静默丢失，driver bug 一开始看不到 AttributeError）；
3. **每个"实施前预期 FAIL"的断言都要在对应方案实施当日重跑并记录翻转**——这是"缺陷→断言→修复→断言变绿"闭环的验收证据，避免断言加了没人跑、修完没人确认。
