# AI-Test-Gen - AI 驱动测试用例自动生成系统

> v2.8.60 | 基于需求驱动 + AI 自动生成测试用例的完整设计方案（含 XMind 在线编辑器）

---

## 项目概述

本项目是一套完整的设计文档体系，定义了如何利用大语言模型（LLM）+ 检索增强生成（RAG）+ 需求影响分析技术，将非结构化的软件需求文档自动转化为高质量、可执行、可追溯、可回流的测试用例。

**核心价值**：

- **减少 60%+ 手工测试用例编写工作**
- **需求澄清闭环（一次性问题列表）--确保需求明确后再生成**
- **影响分析引擎--优化需求场景自动识别影响范围，指导精准生成/回归**
- **全链路可追溯--用例↔需求双向追溯**
- **闭环积累的知识库--越用越准**
- **稳定性保障--断路器/限速/重试/故障转移/优雅降级**
- **多测试框架适配--生成即可执行**
- **大规模检索优化-->10 万条知识库的分区分片+多级缓存策略**
- **已有项目入库--多源 ETL Pipeline 支持批量导入历史资产**

---

## 🚀 快速开始（Docker，推荐）

> 适用于 Windows Docker Desktop / Linux / macOS

### 前置条件

1. **下载本地模型**（嵌入 + 重排，必需）：
   ```bash
   python scripts/fetch-deps.py --models-only
   ```
   模型落盘到 `./models/bge-large-zh-v1.5` 和 `./models/bge-reranker-base`，由 docker-compose 以 bind mount 挂载进容器。

2. **构建基础镜像**（首次或 `requirements.txt` 变更时，约 15-25 分钟）：
   ```bash
   docker compose -f docker-compose.base.yml build base
   ```

### 一键启动

```bash
# 构建并启动全部 8 个容器（约 30 秒）
docker compose up -d --build

# 等待 30-60 秒后打开浏览器
#    访问 http://localhost

# 进入「系统设置」-> 填入 API Key -> 点击「保存」
#    （配置自动持久化到数据库，重启不丢失）
```

| 用途 | 命令 |
|------|------|
| 启动 | `docker compose up -d --build` |
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
| **磁盘空间** | 至少 **20GB** 空闲（基础镜像约 10GB） |
| **网络** | 首次需外网（拉取镜像 + 安装 Python 依赖 + 下载本地模型） |
| **Python** | 3.10+（仅用于运行 `fetch-deps.py` 下载模型，容器内不需要） |

### 首次部署（约 20-30 分钟）

项目采用 **双 Compose + 双 Dockerfile** 架构--基础镜像含所有重量级依赖（构建一次），应用镜像只复制业务代码（日常秒级重建）。

```
Dockerfile.base  ->  ai-test-gen-base:latest    (pip install 全部 Python 依赖，构建一次)
       │
       └──-> Dockerfile  ->  ai-test-gen-backend  (只 COPY 代码，< 10 秒)
```

### 下载本地模型（必需，需先完成）

运行时模型以**宿主机 bind mount** 方式挂载进容器（`docker-compose.yml` 中
`./models/bge-large-zh-v1.5` 与 `./models/bge-reranker-base` 以 `volume:ro` 挂载），
**镜像内不含模型**，因此构建前必须先把模型落到本地 `./models/`：

```bash
# 跨平台统一入口（Windows / Linux / macOS 相同，只要 python 在 PATH）：
python scripts/fetch-deps.py --models-only

# 跳过镜像预热、仅下载模型；完整版（含预热基础设施镜像）用：
#   python scripts/fetch-deps.py
```

- **模型落盘目录**：`./models/bge-large-zh-v1.5`（嵌入，必选）、`./models/bge-reranker-base`（重排，`RERANKER_ENABLED=true` 时启用）。
- 模型下载为纯 Python（`huggingface_hub`），无需 Docker / WSL；若 HuggingFace 不可达，
  脚本会给出代理/手动放置指引，放置好后重跑会幂等跳过。
- **Windows 用户**：直接 `python scripts/fetch-deps.py` 即可（或双击 `scripts\fetch-deps.bat`）；用 `bash scripts/fetch-deps.sh` 请走 **Git Bash**，不要用已损坏的 WSL 发行版。

> Python 依赖（torch / sentence-transformers / pymilvus 等）由镜像构建时
> 直接 `pip install -r requirements.txt` **在线安装**，无需预先下载轮子缓存；
> Docker 层缓存已能加速重复构建（仅当 `requirements.txt` 变更时才重新下载）。

**第 1 步：构建基础镜像**

```bash
docker compose -f docker-compose.base.yml build base
```

首次在线安装全部 Python 依赖（torch、sentence-transformers、pymilvus 等；受网络与镜像源影响，
通常 15-25 分钟；`requirements.txt` 不变时 Docker 层缓存可跳过此步）。

> **仅首次或 requirements.txt 变更时需要**。后续日常开发跳过此步。

**第 2 步：构建并启动全部服务**

```bash
docker compose up -d --build
```

启动以下全部 8 个容器（约 30 秒）：

| 容器 | 说明 | 端口 |
|------|------|------|
| `ai-test-gen-web` | React + Nginx 前端 | 80 |
| `ai-test-gen-api` | FastAPI 后端 | 8000 |
| `ai-test-gen-celery` | Celery Worker（异步嵌入+知识库双写） | - |
| `ai-test-gen-pg` | PostgreSQL 16 + pgvector | 5432 |
| `ai-test-gen-redis` | Redis（缓存 + Celery broker） | 6379 |
| `ai-test-gen-milvus` | Milvus Standalone（向量库） | 19530 |
| `ai-test-gen-etcd` | Milvus 元数据 | - |
| `ai-test-gen-minio` | Milvus 对象存储 | 9001 |

**第 3 步：确认启动**

```bash
docker compose logs -f backend
```

看到 `Application startup complete` 表示就绪。

**第 4 步：打开浏览器**

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

### 系统架构

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
    ┌────────────┼──────────────┬───────────┐
    │            │              │           │
┌───▼───┐  ┌─────▼─────┐  ┌────▼────┐  ┌───▼───┐
│  PG   │  │  Milvus   │  │  Redis  │  │ Celery│
│  16   │  │Standalone │  │ 缓存+MQ  │  │ Worker│
└───────┘  └─────┬─────┘  └─────────┘  └───────┘
         ┌───────┴───────┐
    ┌────▼────┐    ┌─────▼─────┐
    │  etcd   │    │  MinIO    │
    │ 元数据   │    │ 对象存储   │
    └─────────┘    └───────────┘
```

### 双 Compose 文件设计

| 文件 | 用途 | 构建频率 | 耗时 |
|------|------|---------|------|
| `docker-compose.base.yml` | 基础设施 + 基础镜像构建 | 首次/依赖变更 | 15-25 min |
| `docker-compose.yml` | 应用层（全部 8 个容器） | **每次改代码** | < 10 秒 |

### 知识库双引擎架构

| 引擎 | 用途 | 技术 |
|------|------|------|
| **Milvus** | 向量语义检索 | HNSW 索引（M=16, efConstruction=200） |
| **PostgreSQL** | BM25 关键词检索 | tsvector + 全文搜索（zhparser 或 simple） |
| **RRF 融合** | 向量 + BM25 倒排融合 | Reciprocal Rank Fusion (k=60) |
| **Cross-Encoder** | 二阶段重排 | bge-reranker-base (sentence-transformers) |
| **Redis + LRU** | 多级缓存 | 查询结果缓存 + 嵌入缓存 |
| **Celery** | 异步双写 | PG 同步落库 -> Celery 异步嵌入 + Milvus 写入 |

> **zhparser 说明**：`postgresql-16-zhparser` 在部分 Debian/PG 发行版中不可用，系统自动降级为 `simple` 文本搜索配置。中文分词精度略低，但 RRF 融合 + Cross-Encoder 重排可补偿。

### 数据持久化

| 数据 | 存储位置 | 说明 |
|------|---------|------|
| 业务数据 | `./data/pgdata/` | PostgreSQL 16（绑定挂载） |
| LLM 配置 | `./data/pgdata/` | PostgreSQL 加密存储（Fernet） |
| 向量数据 | `./milvus/milvus/` | Milvus（绑定挂载） |
| 元数据 | `./milvus/etcd/` | etcd（绑定挂载） |
| 对象存储 | `./milvus/minio/` | MinIO（绑定挂载） |
| bge 模型 | `./models/` | 宿主机 bind mount（镜像内不含模型） |
| 日志 | `./logs/` | loguru 按天轮转 |
| 加密密钥 | `./data/.keys/master.key` | Fernet 主密钥（持久化） |

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
| `EMBEDDING_MODEL_NAME` | `/models/bge-large-zh-v1.5` | 嵌入模型（宿主机挂载的本地路径） |
| `RERANKER_MODEL_NAME` | `/models/bge-reranker-base` | 重排模型（宿主机挂载的本地路径） |
| `DEBUG` | `true` | 热重载模式 |
| `API_PORT` | `8000` | 后端端口 |
| `NGINX_PORT` | `80` | 前端端口 |

> **无需创建 `.env` 文件**。所有关键环境变量已在 `docker-compose.yml` 的 `environment:` 中预配。LLM API Key 通过前端「系统设置」页面配置，持久化到数据库（Fernet 加密）。

### 故障排查

| 现象 | 原因 | 解决 |
|------|------|------|
| 基础镜像构建报错 `failed to fetch anonymous token` | Docker 通过 IPv6 连接 Docker Hub 失败 | Docker Desktop -> Settings -> Docker Engine -> 添加 `"fixed-cidr-v6": "", "ip6tables": false` -> Apply & Restart |
| 基础镜像构建慢 | pip install torch 2GB+ | 正常 15-25 min，用 `--no-cache` 确保干净 |
| 应用容器 unhealthy | bge 模型未预载 | 运行 `python scripts/fetch-deps.py --models-only` 下载模型 |
| PG 连接失败 | asyncpg 缺失 | 基础镜像内已含，确认重建过 |
| 知识库为空 | 向量数据未迁移 | 需手动归档需求触发 Celery 异步写入 |
| 前端白屏 | 缓存旧版本 | `docker compose build frontend` 重建 |
| 端口冲突 | 80/5432/19530 被占用 | 修改 `docker-compose.yml` 中对应端口 |
| zhparser 不可用 | PG16 镜像无此包 | 自动降级为 `simple` 配置，不影响功能 |
| 重排不生效 | 模型缺 `config.json` | 运行 `python scripts/fetch-deps.py --models-only` 重新下载 |

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
                    不达标 -> 打回 ClarifyAgent
                                 │ ✅ 通过
                                 ▼
                    [ImpactAnalysisAgent] 影响分析 ⭐
                    ├── 新需求 -> 跳过
                    └── 优化需求 -> 版本链+依赖图谱
                        -> 影响范围报告 + 回归推荐
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
                    UTCF JSON -> Markdown / Gherkin / pytest
                    + 覆盖率矩阵 + 追溯矩阵
                                 │
                                 ▼
                    回流知识库 (闭环积累)
                    PG 同步写入 -> Celery 异步 Embedding -> Milvus
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
| 06 | [端到端测试报告](./docs/06-e2e-test-report.md) | 49 项测试全部通过，含架构组件验证、问题修复记录 |

---

## 模板文件

| 文件 | 说明 |
|------|------|
| [FIELD_DEFINITIONS.md](./templates/FIELD_DEFINITIONS.md) | **模板字段详细说明** - 三个模板的所有字段定义、类型、约束、示例 |
| [requirement-template.json](./templates/requirement-template.json) | 标准化需求输入模板 |
| [testcase-output-template.json](./templates/testcase-output-template.json) | UTCF 测试用例输出模板 |
| [knowledge-base-schema.json](./templates/knowledge-base-schema.json) | 知识库数据模型 |

---

## 技术栈速览

| 层级 | 推荐方案 |
|------|---------|
| LLM | deepseek-chat (DeepSeek-V3, 主力) + GPT-4o (复杂场景) |
| 多提供商路由 | LiteLLM (故障转移) |
| Agent 框架 | LangGraph |
| 向量数据库 | Milvus（HNSW 索引） |
| 关系数据库 | PostgreSQL 16 + pgvector |
| 嵌入模型 | bge-large-zh-v1.5 |
| 重排模型 | bge-reranker-base (Cross-Encoder) |
| 缓存 | Redis + LRU 多级缓存 |
| 异步任务 | Celery (embedding + 知识库双写) |
| 后端 | FastAPI (Python) |
| 前端 | React + TypeScript + Tailwind CSS |

---

## 文件结构

```
ai-test-gen/
├── README.md                              # 项目导航 + 部署文档
├── docker-compose.base.yml                # 基础设施 + 基础镜像构建
├── docker-compose.yml                     # 应用层（全部 8 个容器）
├── .dockerignore                          # 构建忽略清单
├── docs/                                  # 架构文档（00-06）
├── templates/                             # 输入输出模板
├── docker/
│   ├── postgres.Dockerfile                # PG 镜像（zhparser 可选）
│   └── init-zhparser.sql                  # zhparser 初始化（条件执行）
├── backend/
│   ├── Dockerfile.base                    # 基础镜像：pip install 全部依赖
│   ├── Dockerfile                         # 应用镜像：FROM base + COPY 代码
│   ├── requirements.txt                   # Python 依赖
│   └── app/                               # FastAPI 后端
├── frontend/
│   ├── Dockerfile                         # 前端构建（Node + Nginx）
│   └── src/                               # React 源码
├── scripts/
│   ├── fetch-deps.py                      # 下载本地模型（跨平台）
│   ├── download_model.py                  # HuggingFace 模型下载
│   └── test_*.py                          # E2E 测试脚本
├── models/                                # 本地模型权重（gitignore，bind mount）
├── milvus/                                # Milvus 运行时数据
├── data/                                  # PG 数据 + 加密密钥
└── logs/                                  # 应用日志
```
