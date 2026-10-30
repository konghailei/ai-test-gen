# 11 — 系统设计逻辑 / 架构文档 与业界优秀案例对比分析

> **分析日期**: 2026-07-29  
> **系统版本**: v2.2.0（已推送 Gitee）  
> **范围**: 代码设计逻辑 ↔ 架构文档一致性核查；与业界优秀案例对比；特点 / 优势 / 劣势；需求模块与知识库专项深挖

---

## 目录

1. [现状梳理：代码能力地图与文档偏差](#1-现状梳理代码能力地图与文档偏差)
2. [业界优秀案例（网络调研）](#2-业界优秀案例网络调研)
3. [系统特点 / 优势 / 劣势（对比结论）](#3-系统特点--优势--劣势对比结论)
4. [需求模块深入分析](#4-需求模块深入分析)
5. [知识库深入分析](#5-知识库深入分析)
6. [改进路线图（基于对比）](#6-改进路线图基于对比)
7. [文档同步建议（修复本次发现的偏差）](#7-文档同步建议修复本次发现的偏差)

---

## 1. 现状梳理：代码能力地图与文档偏差

### 1.1 实际代码能力地图（已逐文件验证）

| 能力 | 代码位置 | 实际状态 | 说明 |
|------|---------|---------|------|
| 双 SRR 分层（interaction_srr + FP sub_srr） | `fp_agent.py` / `req_agent.py` / `requirements.py` | ✅ 已实现 | 一条规则只在一处定义 |
| FP 去重跳过 LLM | `req_agent.py` `_fp_extract_from_raw` | ✅ 已实现 | 同模块已有 FP 名 → 复制到新需求 |
| XMind 8(XML) + 2020+(JSON) 双格式导出 | `exporters/xmind.py` | ✅ 已实现 | 需求 + 用例均支持 |
| Few-shot 示例注入 + related_fp 追溯 | `gen_agent.py` / `prompts/gen_agent.py` | ✅ 已实现 | 2 份 UTCF 模板 |
| 一致性检查（count_diff / type Jaccard / 语义相似度） | `orchestrator.py` `_gen_node` | ✅ 已实现 | 基线存于 `AnalysisTask.result_json`（`api/generate.py` 写入） |
| FP 覆盖率矩阵 | `orchestrator.py` `run_pipeline` done 事件 | ✅ 已实现 | total/covered/rate/uncovered |
| DLQ 死信队列 | `core/dlq.py`（新）+ `embedding_tasks.py` | ✅ 已实现 | Redis List，静默降级 |
| 需求 KM 基线补全精准化 | `clarify_agent.py` `_detect_baseline_focus` | ✅ 已实现 | 按维度返回 |
| 历史版本快照 / 切换 / Object 修复 | `requirements.py` / `VersionHistoryPanel.tsx` | ✅ 已实现 | `restoreVersion` 端点 |
| 轻量 KB 检索（生成期） | `gen_agent.py` `generate()` | ⚠️ 已实现但**弱** | 见 §5.3 |
| 需求侧 KB 混合检索 | `clarify_agent.py` + `db/hybrid_search.py` | ✅ 强 | 向量+BM25+RRF+重排+模块过滤 |
| 用例向量入库 | `review_agent.py` `persist_to_kb` → `milvus_store.add_test_case` | ✅ 已实现 | 5 个 Milvus 集合 |

### 1.2 ⚠️ 重要发现：架构文档与代码严重脱节

本次核查发现 **架构文档落后于代码约一个版本**，且存在内部矛盾：

| 文档 | 位置 | 声称 | 代码实际（v2.2.0） |
|------|------|------|-------------------|
| `docs/00-architecture-overview.md` | §11.2「弱于架构设计」 | DLQ 未实现、一致性检查未实现、覆盖率矩阵未实现 | **三者均已实现**（见 §1.1） |
| `docs/10-v2.2-requirement-analysis-report.md` | §5.3「仍弱于架构设计」 | 一致性检查、覆盖率矩阵列为"未实现"（劣势 #2 #3） | **两者均已实现**（orchestrator） |
| `docs/10` | §6.3「稳定性增强」 | #10 一致性检查"已修复"、DLQ ✅ | 与 §5.3 自相矛盾 |

> **结论**：`docs/00` 停留在 v2.1 视角，未同步 v2.2 的修复；`docs/10` 在同一篇报告里既说"已修复"又说"未实现"。**需求分析相关能力已达标，但文档未如实反映**，会误导后续维护与对外汇报。§7 给出具体修复清单。
>
> **（注：上述文档偏差已于 2026-07-29 按 §7 清单完成修正——docs/00 升 v2.2、docs/10 §5.3 重写、docs/09 §6 补已知差距。）**

### 1.3 五层架构实际落地情况

- **接入层 / 编排层 / 引擎层 / 数据层** 四层防护（限速、断路器、Schema 校验、DLQ）基本落地；`L2 编排层显式断路器`仍偏弱（仅 `llm_client` 指数退避，无状态机）——与 `docs/00 §11.2` 描述一致。
- Agent 流水线：`ReqAgent → ClarifyAgent → QualityGate → ImpactAnalysis → GenAgent → ReviewAgent`，**MatchAgent 确已移除（v3.1）**，替代为 FP 定向生成 + Few-shot + 轻量 PG 检索。

---

## 2. 业界优秀案例（网络调研）

### 2.1 学术界（可直接对标）

| 案例 | 来源 | 核心做法 | 对我们的启示 |
|------|------|---------|------------|
| **ClarifyGPT** | FSE 2024（中科院）被引 133+ | 用「代码一致性检查」探测歧义 → LLM 生成针对性澄清问题 → 用户回答 → 精炼需求 → 重新生成代码。GPT-4 Pass@1 由 70.96% → 80.80% | 我们的 `ClarifyAgent`（完整性/歧义/遗漏检测 + KB 自动补全 + 人工问答）与该范式同源；差异在于我们用**需求结构化**（SRR）而非「代码一致性」做歧义探测 |
| **LLM4Fin** | ISSTA 2024（FinTech 验收测试） | 三步法 + 领域知识注入；结论：**领域知识是覆盖率的决定性因素**，通用 LLM 显著弱于专家；LLM+传统算法协同最优 | 印证我们"知识库闭环积累"方向正确；也解释了为何 GenAgent 需要检索历史高质量用例 |
| **Meta TestGen-LLM** | FSE 2024 | 观察雕刻（observation carving）生成单测；质量闸门：**能编译 → 能运行 → 通过 → 提升覆盖率**。73% 人工采纳率，约 10–11.5% 的类被实际改进 | 这是**白盒 / 覆盖率驱动**模型（与我们黑盒 / 需求驱动互补）；其"必须提升覆盖率才接受"的硬闸门值得借鉴 |

### 2.2 工业界 / 国内厂商

| 厂商 / 产品 | 关键能力 | 可比点 |
|------------|---------|--------|
| **腾讯优测 Utest** | 混元 + DeepSeek 双模型；「AI + 专家知识库」RAG 模式；自然语言 / 接口文档 → 用例；金融领域**用例采纳率 60%**；流量录制回放构造测试数据 | 与我们的 KB+RAG 思路高度一致；**采纳率 60%** 是行业基准指标，我们尚未量化 |
| **Testin 云测 XAgent** | AI + OCR + NLP 融合，需求自动生成，采纳率 ~60%，全链路智能测试闭环 | 同样以"采纳率"为核心 KPI；强调全链路闭环（我们亦有闭环，但生成期召回弱，见 §5.3） |
| **华为** | 老特性 SFT 调优 → 新特性 RAG 驱动 → 无样例脚本自动化，覆盖近 3000 用户场景 | 给出 KB 演进三阶段路线，可作为我们知识库从"冷启动"到"自学习"的参考 |
| **Quest Global（RAG 框架）** | 从 SRS / OpenAPI 文档用 RAG 生成功能 + 非功能（性能/安全/稳定性）用例；LangChain + 向量库 | 证明 RAG from SRS 在功能 + 非功能场景均可行；我们已覆盖功能/边界/异常/安全，但未做性能/稳定性用例 |
| **Pcloudy Qpilot** | 多智能体（创建 / 视觉 / 自愈 / 编排 / 可观测）；self-healing 测试维护 | 我们缺**自愈（self-healing）**与**多模态视觉测试**，是 Phase 3 方向 |
| **Diffblue / Codium Cover** | 代码级单测生成 + 覆盖率闸门（开源实现 Meta TestGen-LLM） | 白盒路线，与我们互补而非竞争 |

---

## 3. 系统特点 / 优势 / 劣势（对比结论）

### 3.1 我们的定位（特点）

> **「需求驱动 + 黑盒功能测试 + 中文场景 + 可溯源闭环」平台**。  
> 与 Meta / Diffblue（白盒代码级）、与国外 SaaS（英文为主）形成差异：我们扎根**需求文档 → 结构化 SRR → 功能点 → 可追溯用例**的链路，且全程中文 + 国内向量库/模型可私有化部署。

### 3.2 优势（对比业界）

| # | 优势 | 证据 | 业界对比 |
|---|------|------|---------|
| 1 | **双 SRR 分层、零重复** | `fp_agent.py` 一次产出 interaction_srr + FP sub_srr | 多数开源/商用工具无此分层，规则散落重复 |
| 2 | **需求侧 KB 检索业界一流** | `hybrid_search.py`：向量 + BM25 + RRF + Cross-Encoder 重排 + 模块过滤 + 基线精准 + 冷启动降级 | 不弱于腾讯优测「AI+专家知识库」；模块过滤避免跨模块误匹配是亮点 |
| 3 | **全链路可追溯** | `related_fp` → Traceability → XMind 分组 → FP 覆盖率矩阵 | 多数工具只做到"用例↔需求"，FP 级追溯是差异化 |
| 4 | **FP 去重跳过 LLM** | 同模块已有 FP 直接复用 | 显著降本，业界少有此优化 |
| 5 | **中文私有化部署友好** | Milvus + bge-large-zh + DeepSeek，可离线 | 对比国外 SaaS（GPT-4o 依赖）更适合信创/合规场景 |
| 6 | **XMind 双格式 + 历史版本管理** | 需求/用例双格式导出 + 快照/切换 | 工程易用性强于多数学术原型 |

### 3.3 劣势 / 差距（对比业界）

| # | 劣势 | 影响 | 业界基准 |
|---|------|------|---------|
| 1 | **生成期 KB 召回是"最新 N 条"而非语义相似** | 跨需求复用历史用例能力弱，违背"越用越准"承诺（见 §5.3） | 腾讯优测 / 华为均用 RAG 语义召回，采纳率 60% |
| 2 | **缺覆盖率硬闸门** | 生成的用例不验证"是否真的覆盖需求要点"即入库 | Meta TestGen-LLM 以"提升覆盖率"为接受门槛 |
| 3 | **无 self-healing / 多模态** | UI 变更后用例维护成本高 | Qpilot / Testin 已具备自愈 |
| 4 | **未生成非功能用例** | 性能/安全/稳定性仅边界覆盖，无专项 | Quest Global 已做功能+非功能 |
| 5 | **采纳率/覆盖率等质量指标未量化对外** | 无法像厂商用"60% 采纳率"证明价值 | 厂商普遍以采纳率为核心 KPI |
| 6 | **编排层无显式断路器** | LLM 故障转移靠 `llm_client` 退避，无状态机 | 架构文档已标注，规模小可接受 |
| 7 | **架构文档与代码脱节** | 误导维护与汇报（见 §1.2） | — |

---

## 4. 需求模块深入分析

### 4.1 双 SRR 分层架构（核心资产）

```
LLM 一次产出 →
  ├ interaction_srr (写入 req.srr_json)   ← 交互层：跨 FP 调用/共享规则/契约/全局约束/跨 FP 验收
  └ functional_points[].sub_srr           ← 内部层：FP 专属流程/规则/数据约束/验收/test_hints
原则：单 FP → sub_srr；跨 FP → interaction_srr；禁止重复
```

**优势**：
- 消除"总 SRR = 所有 FP 子 SRR 并集"导致的规则重复（旧架构每条规则出现 2 次，现 0 次）。
- 与 ClarifyGPT「先结构化再精炼」理念一致，且比单纯"澄清问题"更进一步做了**层级职责分离**。

**短板**：
- `interaction_srr` 的质量高度依赖 LLM 一次产出是否准确划分"跨 FP vs 单 FP"，无二次校验（如跨 FP 契约遗漏时，下游 FP 定向生成会缺上下文）。
- 粒度问题（用户已确认"功能点划分本身不细"）：当 FP 较粗时，`sub_srr` 与 `interaction_srr` 边界偶发模糊，目前靠 LLM 自觉，缺规则兜底。

### 4.2 FP 子系统

| 能力 | 实现 | 评价 |
|------|------|------|
| 去重跳过 LLM | 查同模块已有名 → 硬约束 LLM → 复制 | 🟢 降本 + 跨需求一致 |
| 模块多选 | `modules` JSON 数组（兼容旧 `module_id`） | 🟢 灵活 |
| 重生成 / 合并 / 依赖图谱 / BFS 影响分析 | `fp_regenerate` / `fp_merge` / `fp_dependency_graph` / `fp_impact` | 🟢 完整 |
| 重新生成后自动 fp_split | `requirements.py` `_run_regenerate_reparse`（已修 session bug） | 🟢 一致性保障 |
| 审核流（pending → approved） | `fp_approve` | 🟢 门禁 |

**短板**：FP `atomicity_check`（原子性检查）已传入 GenAgent 但未在 UI/逻辑中强制使用；依赖图谱数据依赖人工维护的 FP 依赖，冷启动期图谱稀疏。

### 4.3 历史版本 / 状态机 / 确认流

- 状态机：`draft → pending_review → confirmed → archived`，任意非归档态可"补充重新生成"，restore 先快照再覆盖。
- **优势**：版本可追溯、可回滚，优于多数只保留"当前版"的工具。
- **短板**：版本差异对比（diff）未做，用户切换时只能看整版覆盖，无法直观看到"这一版改了哪条规则"。

### 4.4 需求模块小结

> 需求模块是**系统最强的一环**：双 SRR、FP 去重、版本管理均达到或超过国内商用水平。主要改进空间在 **(a) interaction_srr 跨 FP 契约的二次校验**、**(b) 版本 diff 视图**、**(c) atomicity_check 真正落地**。

---

## 5. 知识库深入分析（重点）

### 5.1 KB 架构实际构成

```
写入（回流闭环）:
  需求归档  → Milvus[requirements] + PG(requirements)
  用例审核通过 → Milvus[test_cases] + PG(test_cases)   ← persist_to_kb
  模式抽取  → Milvus[patterns] / [req_pats] / [errors]

检索:
  需求侧: ClarifyAgent → hybrid_searcher(向量+BM25+RRF+CrossEncoder重排, 模块过滤)
  用例侧:
    · ReviewAgent 去重/置信 → Milvus.search_similar_cases   ✅ 用向量
    · GenAgent 生成期召回  → PG "最新10条approved, 取前3"    ⚠️ 非语义
```

Milvus 实际配置了 **5 个集合**（`requirements` / `test_cases` / `patterns` / `req_pats` / `errors`），HNSW 索引 + 模块分区。

### 5.2 需求侧 KB —— 强项（建议作为对外标杆）

`hybrid_search.py` 实现的混合检索在中小团队场景属**生产级**：
- 向量（bge-large-zh 1024 维）+ PostgreSQL BM25（zhparser 中文分词，回退 simple）双路；
- **RRF（k=60）融合** + **Cross-Encoder 重排**（可开关）；
- **模块过滤**（`modules` 数组交集）防止跨模块误匹配；
- 结果缓存（避免重复Embedding）；
- 优雅降级：向量失败→仅BM25，BM25失败→仅向量，都失败→空（人工澄清兜底）。

ClarifyAgent 再叠加：`_detect_baseline_focus` 按问题维度精准提取（非全量返回）+ `_verify_kb_answers` LLM 二次校验 + 冷启动模式。**这套组合不弱于腾讯优测的「AI+专家知识库」**。

### 5.3 用例侧 KB —— 断点（最大短板）

**现象**：`gen_agent.py` 的"轻量 KB 检索"（v3.2）实际逻辑是：

```python
# 查同 product 已 approved 用例，取最新 10 条 → 按 title 去重 → 取前 3 条
cases_in_module = SELECT case_json FROM test_cases
    WHERE product_id == product_id AND status == "approved"
    ORDER BY updated_at DESC LIMIT 10
# 注意：注释写"同模块"，但查询只按 product_id，未用 modules 过滤！
```

即：**生成期参考的是"同产品最新审批过的用例"，而非"与当前需求语义相似的用例"**。而 Milvus 里 `test_cases` 集合明明已存了用例向量，`search_similar_cases` 也存在却**未被 GenAgent 调用**。

**后果**：
1. 新需求生成时，注入的 few-shot 可能是毫不相干的旧用例（只是"新"），对生成质量提升有限；
2. 系统承诺的"越用越准 / 闭环积累"在**生成阶段未兑现**——向量化的历史用例只被 ReviewAgent 用于去重/置信，未被生成消费；
3. 与 LLM4Fin 结论（领域/相似知识决定覆盖率）和腾讯优测 RAG 实践相悖。

**修复方向（低成本、高收益）**：将 `gen_agent.py` 的轻量检索改为优先调用 `vector_store.search_similar_cases(query=SRR摘要, top_k=3, modules=...)`，命中不足时回退到现有"最新 N 条"。这样闭环才真正闭合，且直接对齐华为"RAG 驱动新特性"路线。

### 5.4 闭环积累断点总结

```
需求 → [强: 混合检索补全] → 结构化 → 生成 → [弱: 非语义召回] → 审核 → [强: 向量去重] → 入库
                                                  ↑___________________ 断点：生成期未消费相似用例向量
```

### 5.5 与业界 KB 实践对比

| 维度 | 我们（现状） | 腾讯优测 / 华为（业界） | 差距 |
|------|------------|----------------------|------|
| 需求侧检索 | 混合检索 + 重排 + 模块过滤 | AI+专家知识库 RAG | 持平/略优 |
| 生成期用例召回 | 最新 N 条（非语义） | RAG 语义召回 | **明显落后** |
| 采纳率量化 | 无 | 60% | 缺失度量 |
| KB 自学习 | 仅人工审核入库 | 高频澄清→自动优化（华为路线） | 缺自学习 |
| 私有化/信创 | Milvus+DeepSeek 可离线 | 多数云 SaaS | **我们占优** |

---

## 6. 改进路线图（基于对比）

| 优先级 | 事项 | 依据 | 预估 |
|--------|------|------|------|
| **P0** | GenAgent 生成期改用语义召回（`search_similar_cases`），回退"最新 N 条" | §5.3 断点，对齐 LLM4Fin/腾讯/华为 | 0.5–1d |
| **P0** | 补覆盖率硬闸门：用例生成后校验 SRR 要点覆盖，未达标告警/补生成 | Meta TestGen-LLM 质量闸门 | 1–2d |
| **P1** | 量化质量指标：采纳率、覆盖率、可追溯率、一致性方差，对外dashboard | 厂商以采纳率为 KPI | 2d |
| **P1** | interaction_srr 跨 FP 契约二次校验（规则/LLM 双检） | §4.1 短板 | 1d |
| **P1** | 版本 diff 视图（哪条规则变更） | §4.3 短板 | 1d |
| **P2** | KB 自学习：高频澄清问题自动沉淀为模式 | 华为路线 | 2–3d |
| **P2** | 非功能用例（性能/稳定性）生成 | Quest Global 已验证 | 3d |
| **P3** | self-healing / 多模态视觉测试 | Qpilot / Testin | 长周期 |
| **P3** | 编排层显式断路器状态机 | 架构文档标注 | 1d |

---

## 7. 文档同步建议（修复本次发现的偏差）

> 以下偏差应在下一轮提交前修正，否则会持续误导维护与对外汇报。

| 文件 | 修正 |
|------|------|
| `docs/00-architecture-overview.md` §11.2 | DLQ / 一致性检查 / 覆盖率矩阵 由"未实现"改为"v2.2 已实现"；版本号 v2.1 → v2.2 |
| `docs/10-v2.2-requirement-analysis-report.md` §5.3 | 删除将"一致性检查/覆盖率矩阵"列为劣势的条目（与 §6.3 矛盾）；改为"仅编排层断路器仍为简化版" |
| `docs/09-requirement-analysis-pipeline.md` | 补充"生成期 KB 语义召回待增强"（§6 变更汇总） |
| 新增 | 本文件 `docs/11` 作为"现状 vs 业界"对标基线，后续每次大版本更新同步 |

---

> **附：核心结论一句话**  
> 需求模块（双 SRR / FP / 版本）已达国内商用水平，需求侧 KB 检索属生产级；**唯一关键断点是生成期未用语义召回历史用例**，补上后即形成真正闭环，整体可达到腾讯优测/华为同代水平。当前最该先做的是 §6 的 P0 两项。
