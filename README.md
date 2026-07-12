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

## 🚀 快速开始（Docker，推荐）

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

## 📦 详细部署

### 环境要求

| 组件 | 要求 |
|------|------|
| **Docker Desktop** (Windows/macOS) | 4.x 及以上 |
| **Docker Engine** (Linux) | 24+ |
| **系统内存** | 推荐 **8GB** 以上 |
| **磁盘空间** | 至少 **20GB** 空闲（基础镜像约 14GB） |
| **网络** | 需外网（拉取镜像 + 首次构建下载依赖） |

### 首次部署（约 20-30 分钟）

项目采用 **双 Compose + 双 Dockerfile** 架构——基础镜像含所有重量级依赖（构建一次），应用镜像只复制业务代码（日常秒级重建）。

```
Dockerfile.base  →  ai-test-gen-base:latest    (pip install + COPY 本地 bge 模型，约 14GB，构建一次)
       │
       └──→ Dockerfile  →  ai-test-gen-backend  (只 COPY 代码，< 10 秒)
```

**第 1 步：启动基础设施**

```bash
docker compose -f docker-compose.base.yml up -d etcd minio milvus
```

启动 etcd、MinIO、Milvus（约 1 分钟）。

**第 2 步：构建基础镜像**

```bash
docker compose -f docker-compose.base.yml build base --no-cache
```

耗时约 15-25 分钟，包含：
- pip install 全部 Python 依赖（torch、sentence-transformers、pymilvus 等）
- COPY 本地 models/bge-large-zh-v1.5（约 1.3GB，构建前需先下载到项目 models/ 目录，不再联网下载）

> **仅首次或 requirements.txt 变更时需要**。后续日常开发跳过此步。

**第 3 步：构建并启动应用**

```bash
docker compose up -d --build
```

启动以下全部服务（约 30 秒）：

| 容器 | 说明 | 端口 |
|------|------|------|
| `ai-test-gen-web` | React + Nginx 前端 | 80 |
| `ai-test-gen-api` | FastAPI 后端 | 8000 |
| `ai-test-gen-pg` | **PostgreSQL 16** | 5432 |
| `ai-test-gen-milvus` | **Milvus Standalone** | 19530 |
| `ai-test-gen-etcd` | Milvus 元数据 | — |
| `ai-test-gen-minio` | Milvus 对象存储 | 9001 |

**第 4 步：确认启动**

```bash
docker compose logs -f backend
```

看到 `Application startup complete` 表示就绪。

**第 5 步：打开浏览器**

访问 [http://localhost](http://localhost)

| 入口 | 地址 | 说明 |
|------|------|------|
| 前端界面 | `http://localhost` | 主页面 |
| 管理后台 | `http://localhost/admin` | 系统配置、产品管理、测试模式库 |
| API 文档 | `http://localhost/api/docs` | Swagger UI |
| 健康检查 | `http://localhost/api/health` | JSON 状态 |
| MinIO | `http://localhost:9001` | minioadmin/minioadmin |

### 日常开发（修改代码后）

```bash
docker compose up -d --build
```

基于已构建的 `ai-test-gen-base:latest`，只 COPY 业务代码，**< 10 秒**完成。

---

## 🏗️ 架构说明

### 正式方案（Phase 2）

```
                  ┌───────────────────┐
                  │     浏览器 :80      │
                  └─────────┬─────────┘
                            │
                  ┌─────────▼─────────┐
                  │  Nginx (frontend) │
                  │  静态文件 + /api 代理│
                  └────┬───────────┬──┘
               /api/*  │           │
          ┌────────────▼──┐  ┌─────▼──────┐
          │  FastAPI       │  │ React SPA  │
          │  uvicorn 热重载 │  └────────────┘
          └──────┬─────────┘
                 │
    ┌────────────┼──────────────┐
    │            │              │
┌───▼───┐  ┌─────▼─────┐  ┌────▼──────┐
│  PG   │  │  Milvus   │  │  MinIO    │
│  16   │  │Standalone │  │ 对象存储   │
└───────┘  └─────┬─────┘  └───────────┘
         ┌───────┴───────┐
    ┌────▼────┐    ┌─────▼─────┐
    │  etcd   │    │bge-large  │
    │ 元数据   │    │ 嵌入模型   │
    └─────────┘    │(基础镜像内置)│
                   └───────────┘
```

### 双 Compose 文件设计

| 文件 | 用途 | 构建频率 | 耗时 |
|------|------|---------|------|
| `docker-compose.base.yml` | 基础设施 + 基础镜像构建 | 首次/依赖变更 | 15-25 min |
| `docker-compose.yml` | 应用层（前端+后端+PG） | **每次改代码** | < 10 秒 |

### 数据持久化

| 数据 | 存储位置 | 说明 |
|------|---------|------|
| 业务数据 | `./data/pgdata/` | PostgreSQL 16（绑定挂载） |
| LLM 配置 | `./data/pgdata/` | PostgreSQL 加密存储 |
| 向量数据 | `./milvus/milvus/` | Milvus（绑定挂载） |
| 元数据 | `./milvus/etcd/` | etcd（绑定挂载） |
| 对象存储 | `./milvus/minio/` | MinIO（绑定挂载） |
| bge 模型 | 基础镜像内 `/models` 或宿主机挂载 | `Dockerfile.base` COPY + `docker-compose.yml` volume:ro |
| 日志 | `./logs/` | loguru 按天轮转 |

---

## 🔧 运维与配置

### 状态查看

```bash
docker compose ps                              # 所有容器
docker compose -f docker-compose.base.yml ps   # 基础设施
docker compose logs -f backend                 # 后端日志
```

### 启停

```bash
docker compose stop                            # 暂停（保留数据）
docker compose start                           # 恢复
docker compose down                            # 停止并删除容器（保留数据）
docker compose down -v                         # ⚠️ 删除所有数据
```

### 重建

```bash
# 日常改代码（秒级）
docker compose up -d --build

# 改 requirements.txt 后重建基础镜像
docker compose -f docker-compose.base.yml build base --no-cache
docker compose up -d --build

# 完全重置
docker compose down -v
docker compose -f docker-compose.base.yml down -v
```

### 数据库操作

```bash
# 进入 PG
docker exec -it ai-test-gen-pg psql -U aitestgen -d ai_test_gen

# 查看 Milvus 向量数
docker exec ai-test-gen-api python -c "
import sys; sys.path.insert(0,'/app')
from app.db.milvus_store import vector_store
v = vector_store.get_all_requirements(limit=100)
print(f'向量数: {len(v)}')
"
```

### 配置参考

#### docker-compose.yml 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_URL` | `postgresql+asyncpg://...` | PostgreSQL 连接（已预配） |
| `VECTOR_STORE` | `milvus` | 向量后端 |
| `EMBEDDING_MODEL_NAME` | `/models/bge-large-zh-v1.5` | 嵌入模型（基础镜像内置或宿主机挂载的本地路径） |
| `DEBUG` | `true` | 热重载模式 |
| `API_PORT` | `8000` | 后端端口 |
| `NGINX_PORT` | `80` | 前端端口 |

#### 向量库说明

> **Milvus 是系统唯一的向量库后端**（ChromaDB 已弃用并移除）。部署由 `docker-compose.yml`
> 注入 `VECTOR_STORE=milvus` 并启动 `milvus` / `etcd` / `minio` 服务，**不再支持**回退到 ChromaDB。
> 关系库使用 PostgreSQL 16（`DATABASE_URL` 必填），向量库固定为 Milvus。

### 故障排查

| 现象 | 原因 | 解决 |
|------|------|------|
| 基础镜像构建报错 `failed to fetch anonymous token` / `dial tcp [IPv6]:443 connectex` | Docker 守护进程通过 IPv6 连接 Docker Hub 失败（Windows 常见问题，无公网 IPv6 路由） | ① Docker Desktop → Settings → Docker Engine → 添加 `"fixed-cidr-v6": "", "ip6tables": false` → Apply & Restart；② 或编辑 `%USERPROFILE%\.docker\daemon.json` 写入上述配置；③ 重启后重新 `docker compose -f docker-compose.base.yml build base --no-cache` |
| 基础镜像构建慢 | pip install torch 2GB+ | 正常 15-25 min，用 `--no-cache` 确保干净 |
| 应用容器 unhealthy | bge 模型未预载 | 确认基础镜像已重建（`docker images ai-test-gen-base` 看日期） |
| PG 连接失败 | asyncpg 缺失 | 基础镜像内已含，确认重建过 |
| 知识库为空 | 向量数据未迁移 | 需手动迁移已归档需求到 Milvus |
| 前端白屏 | 缓存旧版本 | `docker compose build frontend` 重建 |
| 端口冲突 | 80/5432/19530 被占用 | 修改 `.env` 中对应端口 |

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
                    ├── KB 自动补全 (相似度>0.40)
                    └── 一次性问题列表 (动态生成)
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
| 向量数据库 | Milvus（统一向量库后端，ChromaDB 已弃用） |
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
├── KB: Milvus 向量库 + 种子数据 50-100 个
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
├── README.md                              # 项目导航 + 部署文档
├── docker-compose.base.yml                # 基础设施 + 基础镜像构建
├── docker-compose.yml                     # 应用层（日常秒级构建）
├── .env                                   # Compose 环境变量
├── .dockerignore                          # 构建忽略清单
├── docs/
│   ├── 00-architecture-overview.md        # 架构总览
│   ├── 01-requirement-parsing.md          # 需求解析
│   ├── 02-knowledge-base.md               # 知识库
│   ├── 03-case-generation-flow.md         # 生成流程
│   ├── 04-output-format.md                # 输出格式
│   └── 05-technical-selection.md          # 技术选型
├── templates/
│   ├── FIELD_DEFINITIONS.md               # 字段说明
│   ├── requirement-template.json          # 需求输入模板
│   ├── testcase-output-template.json      # 用例输出模板
│   └── knowledge-base-schema.json         # 知识库 Schema
├── backend/
│   ├── Dockerfile.base                    # 基础镜像：pip install + COPY 本地 bge 模型
│   ├── Dockerfile                         # 应用镜像：FROM base + COPY 代码
│   ├── requirements.txt                   # Python 依赖
│   ├── .env                               # 后端环境变量
│   └── app/
│       ├── main.py                        # FastAPI 入口
│       ├── config.py                      # 配置管理
│       ├── db/
│       │   ├── database.py                # 数据库引擎（PostgreSQL）
│       │   └── milvus_store.py            # Milvus 适配器
│       └── api/                           # API 路由
├── frontend/
│   ├── Dockerfile                         # 前端构建
│   └── src/                               # React 源码
├── milvus/                                # Milvus 运行时数据（etcd + MinIO + 向量）
├── data/pgdata/                           # PostgreSQL 数据
├── logs/                                  # 应用日志
└── kb-ppt/
    ├── ai-test-gen知识库架构.pptx          # 知识库架构演示文稿
    └── create-kb-ppt.js                   # 演示文稿生成脚本
```
