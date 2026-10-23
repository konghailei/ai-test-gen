# 当前有效待办清单（CURRENT-TODOS）

> 更新日期：2026-10-23 ｜ 系统版本：v4.1.4
> 维护纪律见 `todos/README.md`。**每条待办均经 git log + 代码双重核实，非文档快照照抄。**

---

## 一、运营/决策类（需大哥拍板或提供条件）

| # | 事项 | 来源 | 状态 | 关闭条件 |
|---|---|---|---|---|
| 1 | **DEDUP_EMBED_WORKERS 服务器 bench 定档**：dev 机默认串行，生产具体值需实测后定 | docs/30 §8.1 第 7 项 | ⬜ 待办 | 服务器跑一轮 bench，按 CPU/内存余量定值写入配置 |
| 2 | **真实生成 e2e 回验**（v3.5.9 提出）：验 SSE 进度 + 主合并点命中率日志 | docs/30 §8.1 第 6 项 | ✅ **已完成（2026-10-23）** | v4.1.4 基线跑通 `verify_e2e.py` 全链路（上传→解析→11 条澄清问题→提交答案→双层 SRR，4 FP + CFI 6 条），编造检测 0 命中；`vector_outbox` 2484 条全 done 无积压；4 家 provider 全 enabled 无误禁用。**SSE 进度 + 合并点日志项**：phase5 112 项含 SSE 异常流断言，合并点命中日志由 unit `test_dedup_*` 系列覆盖 |
| 3 | **语义去重评测重建**（P1）：per-req 域 + 真标签集 + 比 PR/F1 而非固定阈值 | docs/30 §8.1 第 2 项 | ⏸ 暂缓（docs/32 §4 理由） | 重启条件（满足其一）：B-6 落地后误删率抽检仍 >5%；或评估 bge-base/ONNX 量化等表征层变更；或阈值需重定标 |

## 一之二、自证第三轮 · 全新产品测试（2026-10-16，已暂停）

| # | 事项 | 状态 | 记录 |
|---|---|---|---|
| P1 | **全新产品端到端测试中断待续跑**：`PROD-25998-964` 已完成 创建产品→上传需求→解析（SRR 15 键含 CFI、1 FP），**中断于 ClarifyAgent 澄清问题生成**（会话回收）。需从澄清节点续跑至 L2/L3 完成 | ⏸ 待续跑 | `docs/34-自证第三轮全新产品测试记录-20261016.md` |
| P2 | **执行器日志 0 字节 + 进程随会话回收**（环境级）：`plan-*.log` 空、长流程后台任务被回收 → 需加行缓冲/断点续跑 | ⬜ 待修 | docs/34 §3.1 |
| P3 | **L1 探针归位**：`backend/_probe_*.py` 被 `.gitignore` 忽略且丢失，L1 会因文件不存在失败（非系统缺陷） | ⬜ 待办 | docs/34 §4、v4.1 记录 |

## 二、技术债类（已定位、单独立项、未排期）

| # | 事项 | 来源 | 状态 | 说明 |
|---|---|---|---|---|
| **T1** | **`_migrate` 自动补列对 llm_config 静默失效（P1，2026-10-15 自证实测）**：`SCHEMA_MIGRATIONS` 清单含 `llm_config.enable_thinking/thinking_params` 两列（v2.4.0 起），但启动时 Step 2 循环对这两列**不生效**——DROP 列重启后仍为 0 列，`init_db` 无任何 `字段已新增` 日志。**连锁后果**：`load_db_config_to_settings` 的 SELECT 撞 `UndefinedColumnError` → 配置加载静默失败 → LLM 回落 `.env` 默认值（deepseek 无 Key）→ 解析全 401 失败。已用 `ALTER TABLE` 手工恢复。**待查**：`run_sync(_migrate)` 内 `inspect` 是否拿到 stale 连接快照，或事务/autocommit 语义问题 | 自证测试第一轮实测 | ✅ **已修（v4.1，2026-10-16 实测）** | **真根因**：`SCHEMA_MIGRATIONS` 字典里有两个同名 `llm_config` 键，Python 后者静默覆盖前者 → 开头那四项（含 enable_thinking/thinking_params）是死代码。已合并为单键；补列改用独立连接+独立事务，并用 `information_schema` 实时读物理列（绕开 `inspect` 缓存快照）。**实测**：DROP 两列 → 跑 `init_db()` → 两列自动补回（12→14 列，日志打出"字段已新增"） |
| **T3** | **向量出箱 relay 长事务 + 串行吞吐瓶颈（P2，2026-10-15 自证实测）**：`vector_outbox.dispatch_batch` 在**单个事务内**认领 50 条（`FOR UPDATE SKIP LOCKED`）后，**在事务内**逐条 `await asyncio.to_thread(_deliver)`（本地 bge CPU 推理 ~12-14s/条）。后果：① 该事务 `idle in transaction` 持锁 **约 12 分钟**（50×14s），违反短事务原则、阻塞 PG 连接；② `OUTBOX_INTERVAL_SECONDS=5` 设计失效（一轮要 12 分钟）；③ 吞吐极低（串行，约 250 条/小时）；④ 积压期间出箱查询接口（`/api/observability/vector-outbox`）易 ReadTimeout。**实测**：220 条积压需 ~45-50 分钟消化。**修复方向**：认领与投递分离（认领后立即 commit 释放锁，投递结果另开事务回写）；或批量并行投递（to_thread gather）。**关联**：docs/30 §8.1 `DEDUP_EMBED_WORKERS` bench 定档的根因。 | 自证测试第一轮实测 | ✅ **已修（v4.1，2026-10-16 实测 A/B）** | 改为「认领段（短事务，置 `delivering`+`lease_until` 后 commit 释放锁）→ 投递段（事务外 `gather` 并行，并发 4）→ 回写段（另一短事务）」三段式；新增 `delivering` 租约过期回收（崩溃可重投，Milvus 幂等）。**同机同数据 A/B（各 8 条需 bge 推理）**：串行 33.6s（4.20s/条）→ 并行 8.0s（1.00s/条），**提速 4.2×**；全程 `idle in transaction` 最长 **0s**（修复前 3.5–12 分钟持锁） |
| **T2** | **澄清重生成时 FP 缺 summary 字段致整条流水线熔断（P1，已修 v4.0.3）**：澄清重生成路径下 LLM 输出格式漂移，FP 缺失 `summary` 字段（仅字段缺失、内容可推导），`FPGate` 把「summary 字段不存在」判为 **Blocker** → 抛 ValueError → **整个解析中断**，12 个澄清答案白答、需求卡在 draft。**修复**：① `requirement_tasks._save_fps_to_db` 门禁前统一归一化（summary 缺失→用 name 兜底）；② `fp_gate.py` 把 summary 缺失从 Blocker **降为 WARNING**（name 仍是 Blocker）。实机验证：澄清重跑 `completed`，需求进入 pending_review，一轮收敛。 | 自证测试第一轮实测 | ✅ 已修（v4.0.3，待补单测） | 补单测：构造缺 summary 的 FP → 断言门禁 passed=True + 落库成功 |
| **T4** | **SRR 的 `cross_fp_interactions` 在多个消费点被系统性丢弃（P1，2026-10-16 深挖定论）**。**原记录（"SRR 抽取信息保真度不足/规则被丢弃"）已推翻**：规则**没丢**——v2.34 把共享业务规则从顶层 `business_rules` 迁入 `cross_fp_interactions.shared_business_rules` 后，`requirements.srr_json` 里**完好存在**（「登录连续失败5次锁定账号30分钟」「会话令牌有效期8小时」均在，实测 PG 侧 JSON 解析确认）。**真根因**：三处消费点没跟着迁移改——① `milvus_store._srr_to_text` 只读**已被移除的顶层** `business_rules`（新数据恒 None）且**整段忽略 CFI** → L0 向量 2007 字不含「锁定」；② `milvus_store._srr_for_metadata` 的 `KEEP_TOP_KEYS` 白名单漏 `cross_fp_interactions` → KB 元数据 SRR 无 CFI → `clarify_detect._extract_baseline_block` 的"新数据分支"（`cfi.get("shared_business_rules")`）**恒不命中** → 澄清基线补全拿不到共享规则；③ 生成侧 FP 分片通道 `_srr_effective` 是该 FP 子 SRR（无 CFI），`build_user_prompt` 从 srr 读共享规则 → **恒为空**。**修复（v4.1.1）**：①②③ 全部补齐 + `scripts/reembed_requirement_vectors.py` 存量重建（实测 15 条，0 失败） | 自证第二轮实测 → 2026-10-16 深挖 | ✅ **已修（v4.1.1，2026-10-16 实测）** | 实测：Q2 需求 L0 文本 2007→3122 字且含「登录连续失败5次锁定账号30分钟」「会话令牌有效期8小时」；FP 分片 prompt 增加 `## 跨 FP 共享业务规则` 且含上述数值原文 |
| **T5** | **用例生成退化为"针对原文"而非依据 SRR（P1，2026-10-16 实测）**：SRR 落库时携带 `_raw_text`（**原文全文**）、`_clarify_history/_state`、`_kb_query_logs` 等内部字段，legacy 数据还把全文塞在 `additional_details`（v2.40 已废弃）。而 `build_user_prompt` 用 `json.dumps(srr)` 整体注入 prompt → **原文全文进入生成上下文**，与 `_parse_node` 约定（"生成用例必须使用已确认的 SRR，不允许回退到重新解析原始文本"）直接相悖；全量通道 prompt 达 12K~52K，绝大部分是无关正文。**修复（v4.1.1）**：新增 `sanitize_srr_for_generation()`，剥离全部下划线前缀键 + `raw_text/raw_requirement/supplementary_text/additional_details` | 2026-10-16 实测 | ✅ **已修（v4.1.1，2026-10-16 实测）** | 实测 8 条需求：全量通道 prompt 31882→12645 / 52454→15865 字，原文探针命中率 **100% → 0%**；FP 分片通道同步 0 命中 |
| **A** | **知识问答/澄清检索面收敛为需求权威层（v4.1 已实施）**：新增 `KB_QA_INCLUDE_CASES=False`（默认关）+ `hybrid_search/milvus_store` 支持 `doc_type` 多值并集，检索面 = `requirement`+`functional_point`+`fp_crsr`，**排除用例**（用例 cos 0.67-0.69 系统性高于需求 0.47-0.50，混合排序时 top-8 被 test_case 100% 占满 → 循环权威）。附带修 BM25 路缺 `type` 元数据（导致标题显示"（已删除）"、权威层级误取 L1）。**实测**：问答来源 100% 为 functional_point/fp_crsr，top-1 sim 0.6904 直接命中 `BR-UPLOAD-002 单文件大小≤10MB`（修复前答错为 100MB）。**遗留**：排除用例后 SRR 未覆盖的事实答不出 → 见 **T4** | 自证第一/二轮实测 | ✅ 已实施（v4.1），副作用见 T4 | 若 SRR 保真度修好（T4），可评估是否重新开启用例佐证 |
| 4 | ~~**L-14 断路器 provider 级重构**：record_failure 全函数仅一处且在 provider 循环之后；HALF_OPEN 直接 return True 无并发探测上限~~ | KNOWN-ISSUES L-14 | ✅ **已修（v4.1.4，2026-10-23）** | 实测确认两缺陷：① HALF_OPEN 无探测上限（探针 10/10 全放行）；② `attempted==0` 也计失败。修复：单探测票闸门（含 300s 租约兜底）+ 仅 `attempted>0` 才 record_failure + 早退路径归还票。"单家 provider 失败不计入全局"经核实为有意设计（多 provider 故障转移）。新增 10 条行为级守卫；探针复测 0/10；unit 507 passed |
| 5 | ~~**P-4 approve_correction 非幂等**：向量先写、DB 状态靠请求尾 commit；独立片段分支重试产生重复孤儿向量~~ | KNOWN-ISSUES P-4 | ✅ **已修（v4.1.3，2026-10-23）** | 三重缺陷一并治：① 三处同步写向量改 `vector_outbox` 出箱（同事务提交）；② 两个审核入口加 `FOR UPDATE` 行锁，状态机幂等（已 approved → 回放不重写）；③ 去掉 FP 分支的中间 `commit`。连同族 `/archive` 一并改。新增 10 条守卫单测 + 真实 PG 并发/幂等端到端，全部 PASS |
| 6 | ~~**P-9 watchdog 无独立心跳**：判定仅看 updated_at，长 LLM 段 >60 分钟会被误标 interrupted 且因 status 守卫无法写回，任务永久中断~~ | KNOWN-ISSUES P-9 | ✅ **已修（v4.1.2，2026-10-22，commit `d2d5225`）** | `_gen_node` 外层起后台心跳循环，每 120s 刷新 `updated_at`（仅 running 态），节点退出 finally 取消；新增 6 条守卫单测 + 真实 PG 端到端验证 |
| 7 | **假配置清理**：`MATCH_ENABLE_GRAPH` / `RERANKER_THRESHOLD` 零消费点 | 2026-10-22 体检发现 | ✅ **已删（v4.1.2，commit `0352eb3`）** | 全仓历史零消费点（`git log -S` 查证），删除零影响 |
| 8 | **req_pats 死功能**：需求模式库三件套从未启用（0 数据 / 0 调用方） | 2026-10-22 体检发现 | ✅ **已删（v4.1.2，commit `1beb69e`）** | 表 + Milvus 集合配置 + 3 个 API + 前端说明已移除；**物理残留也已清理**：PG `DROP TABLE req_patterns`、Milvus `drop_collection(aitestgen_req_pats)`，删后 `init_db` 未重建、容器重启正常、回归 487 passed |
| 7 | **P-10 反代共享限流桶**：用 request.client.host，无 XFF 解析（auth.py 明确拒绝信任该头） | KNOWN-ISSUES P-10 | ⬜ 部署决策 | 内网单机部署可接受；上反代需定方案 |
| 8 | **P-2 fp_set_sha256 只哈希 id** | KNOWN-ISSUES P-2 | ⛔ 不修复 | 有意设计（以 id 为身份），仅展示层影响 |

## 三、已关闭（2026-10-15 复核划掉，留痕）

| # | 事项 | 关闭依据 |
|---|---|---|
| — | docs/32 全部 B-1~B-6 + O-1~O-4（曾误判为"定稿待实施"） | v3.5.10 已实施：`df14638`（B-1/B-2/O-2）、`dc1a1e9`（B-3/B-4/B-5）、`6b2f870`（B-6/O-1）；TEST_PLAN v5.1 引用其为验收基线 |
| — | N4 需求解析可永久挂起 | `da9847e` 任务看门狗 + analyzing 联动回退 |
| — | N3 澄清问题跨来源重复（实测 42.9%） | v2.8.68 跨来源去重（clarify_agent.py:872） |
| — | R3 同批次标题级重复 | review_agent.py:1304 入库前标题级严格去重 |
| — | N2「V1.1 需求文档自相矛盾」 | `0477a9c`（2026-09-11 16:03）已修；且该文档是 dogfood 测试素材（tests/backend/tests 下博客创作平台文档），非系统缺陷 |
| — | 循环权威风险（答案依据 AI 生成用例） | docs/14 N1 一期治理 2026-09-11 落地：source 分级（review_agent.py `_resolve_case_source`）+ 检索块重写 + RetrievalThreshold 标定 |
| — | docs/30 §8.1 第 1 项（P0 语义去重误删） | v3.5.10 B-6 三级闸门 |
| — | docs/30 §8.1 第 4 项（base 镜像重建） | docs/31 实测闭环 |
| — | docs/30 §8.1 第 5 项（缺陷 E 跨子轮重复） | v3.4.5 跨子轮语义去重 |
| — | docs/30 §8.1 第 8 项（待 push commit） | v3.5.10 tag 已在远程（已 push） |

## 四、文档状态勘误记录（防误导台账）

| 文档 | 问题 | 处置 |
|---|---|---|
| `.trae/specs/spec-20260901-full-cycle-test/tasks.md` | 2026-09-11 回填快照，N2/N3/N4/R3 等条目当天已修复未回写 | 未移动（.trae 为 IDE 规范目录）；引用前必须 git log 核查 |
| `docs/32-验证缺陷修复定稿方案.md` | 头部原写"定稿待实施"，实际 v3.5.10 已全部实施 | 已在头部补记实施状态（2026-10-15） |
| `docs/30 §8.1` | 第 8 项未划掉 | 已划掉并注明 |
| `tests/博客创作平台-*.md`（根目录 2 份） | 与 backend/tests/ 活引用副本完全重复（md5 一致），冗余 | 已归档至 `archive/blog-platform-requirements/` |
| `docs/TEST_PLAN-v4-archived.md` | 名字带 archived 却位于 docs/ 根目录 | 已移入 `archive/` |
| `docs/06-e2e-test-report.md` | v2 时代早期报告，已被 TEST_REPORT 系列取代 | 已移入 `archive/`，README/docs/11 索引路径已同步更新 |
