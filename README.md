# AI-Test-Gen — AI 驱动测试用例自动生成系统

> 基于需求驱动 + AI 自动生成测试用例的完整设计方案

---

## 项目概述

本项目是一套完整的设计文档体系，定义了如何利用大语言模型（LLM）+ 检索增强生成（RAG）+ 需求影响分析技术，将非结构化的软件需求文档自动转化为高质量、可执行、可追溯、可回流的测试用例。

**核心价值**：

- **减少 60%+ 手工测试用例编写工作**
- **需求澄清闭环（一次性问题列表）——确保需求明确后再生成**
- **影响分析引擎——优化需求场景自动识别影响范围，指导精准生成/回归**
- **全链路可追溯——用例↔需求双向追溯**
- **闭环积累的知识库——越用越准**
- **稳定性保障——断路器/限速/重试/故障转移/优雅降级**
- **多测试框架适配——生成即可执行**
- **大规模检索优化——>10 万条知识库的分区分片+多级缓存策略**
- **已有项目入库——多源 ETL Pipeline 支持批量导入历史资产**

---

## 🚀 一键部署（Docker，推荐）

> 适用于 Windows Docker Desktop / Linux / macOS

```bash
# 1. 启动（无需任何配置文件）
docker compose up -d

# 2. 等待 30-60 秒后打开浏览器
#    访问 http://localhost

# 3. 进入「系统设置」→ 填入 API Key → 点击「保存」
#    （配置自动持久化到数据库，重启不丢失）
```

**就是这么简单。不需要编辑任何配置文件。**

| 用途 | 命令 |
|------|------|
| 启动 | `docker compose up -d` |
| 查看日志 | `docker compose logs -f` |
| 更新代码后重建 | `docker compose up -d --build` |
| 停止 | `docker compose down` |
| 完全重置（清空数据） | `docker compose down -v` |

---

## 📖 文档导航

| 序号 | 文档 | 说明 |
|------|------|------|
| 00 | [架构总览](./docs/00-architecture-overview.md) | 五层架构、数据流、技术栈、MVP 架构、进度反馈、实施路线 |
| 01 | [需求解析与格式化](./docs/01-requirement-parsing.md) | SRR JSON、ReqAgent、ClarifyAgent（批量问题列表）、QualityGate、需求模式库、优化需求上下文补全 |
| 02 | [知识库架构设计](./docs/02-knowledge-base.md) | 双引擎存储、混合检索、版本链、依赖图谱、大规模检索优化（§8）、已有项目入库（§9） |
| 03 | [测试用例生成流程](./docs/03-case-generation-flow.md) | GenAgent、ImpactAnalysisAgent、ReviewAgent、追溯矩阵、一致性、稳定性保障 |
| 04 | [用例输出格式规范](./docs/04-output-format.md) | UTCF JSON + Markdown 输出、Gherkin/pytest 转换、覆盖率矩阵、追溯矩阵 |
| 05 | [技术选型分析](./docs/05-technical-selection.md) | 全栈技术对比、稳定性组件选型、容量规划、性能基准 |

> **注**：原 doc-06（检索优化）和 doc-07（入库方案）已整合进 doc-02 的 §8 和 §9；原 doc-11（可行性审查）的结论已整合进 doc-00 的 §9.1（MVP 架构）和各文档对应章节。

---

## 快速理解

```
需求文档 ──► [ReqAgent] ──► SRR JSON
                                 │
                                 ▼
                    [ClarifyAgent] 需求澄清
                    ├── 完整性检测 (必填字段)
                    ├── 歧义性检测 (模糊描述)
                    ├── 遗漏检测 (需求模式对比)
                    ├── KB 自动补全 (相似度>0.85)
                    └── 一次性问题列表 (最多5个，批量回答)
                                 │
                                 ▼
                    [QualityGate] 质量门禁
                    不达标 → 打回 ClarifyAgent
                                 │ ✅ 通过
                                 ▼
                    [ImpactAnalysisAgent] 影响分析 ⭐
                    ├── 新需求 → 跳过
                    └── 优化需求 → 版本链+依赖图谱
                        → 影响范围报告 + 回归推荐
                                 │
                                 ▼
                    [MatchAgent] 知识库检索
                    向量+关键词+模式匹配
                    🔒 断路器保护
                                 │
                                 ▼
                    [GenAgent] 多维并行生成
                    + 追溯标注 + 一致性检查
                    + 影响范围指导（优化需求只更新受影响用例）
                    🔒 断路器 + 重试 + 故障转移
                                 │
                                 ▼
                    [ReviewAgent] 质量审核
                    + 追溯验证 + 变异测试
                                 │
                                 ▼
                    [FormatEngine] 格式输出
                    UTCF JSON → Markdown / Gherkin / pytest
                    + 覆盖率矩阵 + 追溯矩阵
                                 │
                                 ▼
                    回流知识库 (闭环积累)
                    PG 同步写入 → 异步 Embedding → Vector DB
```

---

## 模板文件

| 文件 | 说明 |
|------|------|
| [FIELD_DEFINITIONS.md](./templates/FIELD_DEFINITIONS.md) | **模板字段详细说明** — 三个模板的所有字段定义、类型、约束、示例 |
| [requirement-template.json](./templates/requirement-template.json) | 标准化需求输入模板 |
| [testcase-output-template.json](./templates/testcase-output-template.json) | UTCF 测试用例输出模板 |
| [knowledge-base-schema.json](./templates/knowledge-base-schema.json) | 知识库数据模型 |

---

## 技术栈速览

| 层级 | 推荐方案 |
|------|---------|
| LLM | DeepSeek-V4 (主力) + GPT-4o (复杂场景) |
| 多提供商路由 | LiteLLM (故障转移) |
| Agent 框架 | LangGraph |
| 向量数据库 | ChromaDB → pgvector → Milvus（渐进式迁移） |
| 关系数据库 | PostgreSQL 16 |
| 嵌入模型 | bge-large-zh-v1.5 |
| 限速/断路器 | Redis + Python (Lua原子操作) |
| 后端 | FastAPI (Python) |
| 可观测性 | LangFuse + Prometheus + Grafana |

---

## 业界参考

| 来源 | 借鉴点 |
|------|--------|
| **ClarifyGPT** (ACL 2024) | 模糊需求检测 + 针对性澄清问题生成 |
| **SpecFix** (2025) | 一次性问题列表优于多轮对话 |
| **Auto-Validation** (2025) | 可追溯性、一致性、完整性三大验证指标 |
| **Meta TestGen-LLM** (2024) | 分类生成策略、人机协作模式 |
| **CodiumAI Cover-Agent** | 代码行为分析 → 测试生成 → 人工确认闭环 |
| **RAG 架构** (Meta/OpenAI) | 混合检索 + Re-rank + 上下文管理 |
| **微软测试套件优化** | 20 万测试套件，智能选取 12-15% → 94% 召回率 |

---

## 实施路径

```
Phase 1: MVP (4-6 周) — 3 个 Agent，2-4 次 LLM 调用，<30s，¥8-16/需求
├── ReqAgent: Markdown → SRR JSON
├── QualityGate: Schema 校验 + 必填检查（纯规则）
├── MatchAgent + GenAgent: 向量检索 + 批量生成
├── ReviewAgent: 采样 5% 审核
├── KB: ChromaDB + 种子数据 50-100 个
├── 稳定性: RPM 限速 + 指数退避重试 + Schema 校验
└── Output: UTCF JSON

Phase 2: 增强 (6-8 周) — 加上澄清 + 影响分析 + 追溯
├── ClarifyAgent: 一次性问题列表（1 轮）
├── ImpactAnalysisAgent: 优化需求影响分析（按需调用）
├── GenAgent: +边界值 +异常 +安全测试
├── TraceabilityMatrix: 用例↔需求追溯
├── KB: PostgreSQL + pgvector + 版本链 + 依赖图谱
├── 稳定性: 断路器 + 多提供商故障转移 + 优雅降级
├── Output: +Markdown + Gherkin + pytest + 覆盖率矩阵
├── 进度反馈: SSE 推送
└── UI: Web 管理面板

Phase 3: 成熟 (8-12 周) — 大规模 + 持续优化
├── KB: Milvus + 分区分片 + 多级缓存 + Re-rank
├── 入库 Pipeline: 多源 ETL + 增量同步
├── Batch/Incremental Mode
├── 稳定性: LLM-as-Judge + Embedding漂移 + 死锁预防
├── 变异测试: 用例完整性验证
├── CI/CD 集成
└── Metrics Dashboard: 接受率/覆盖率/可追溯率/一致性
```

---

## 文件结构

```
ai-test-gen/
├── README.md                              # 项目导航
├── docs/
│   ├── 00-architecture-overview.md        # 架构总览（五层架构+MVP架构+数据流+技术栈+实施路线）
│   ├── 01-requirement-parsing.md          # 需求解析（SRR+ClarifyAgent+QualityGate+模式库+优化需求补全）
│   ├── 02-knowledge-base.md               # 知识库（双引擎+版本链+依赖图谱+大规模优化+入库方案）
│   ├── 03-case-generation-flow.md         # 生成流程（GenAgent+ImpactAnalysis+Review+追溯+稳定性）
│   ├── 04-output-format.md                # 输出格式（UTCF+Markdown+Gherkin/pytest+覆盖率矩阵）
│   └── 05-technical-selection.md          # 技术选型（全栈+稳定性组件+容量规划）
├── templates/
│   ├── FIELD_DEFINITIONS.md                # 字段说明（三个模板的所有字段详细定义）
│   ├── requirement-template.json           # 需求输入模板
│   ├── testcase-output-template.json       # 用例输出模板
│   └── knowledge-base-schema.json          # 知识库 Schema
└── kb-ppt/
    ├── ai-test-gen知识库架构.pptx          # 知识库架构演示文稿（用于评审/培训）
    └── create-kb-ppt.js                    # 演示文稿生成脚本（PptxGenJS）
```
