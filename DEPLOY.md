# AI-Test-Gen Docker 部署说明

> 适用：Windows Docker Desktop / Linux / macOS  
> 架构版本：Phase 2 — PostgreSQL + Milvus + bge-large-zh-v1.5

---

## 一、环境要求

| 组件 | 要求 |
|------|------|
| **Docker Desktop** (Windows/macOS) | 4.x 及以上 |
| **Docker Engine** (Linux) | 24+ |
| **系统内存** | 推荐 **8GB** 以上 |
| **磁盘空间** | 至少 **20GB** 空闲（基础镜像约 14GB） |
| **网络** | 需外网（拉取镜像 + 首次构建下载依赖） |

---

## 二、首次部署（约 20-30 分钟）

项目采用 **双 Compose + 双 Dockerfile** 架构——基础镜像含所有重量级依赖（构建一次），应用镜像只复制业务代码（日常秒级重建）。

```
Dockerfile.base  →  ai-test-gen-base:latest    (pip install + COPY 本地 bge 模型，约 14GB，构建一次)
       │
       └──→ Dockerfile  →  ai-test-gen-backend  (只 COPY 代码，< 10 秒)
```

### 第 1 步：启动基础设施

```bash
cd D:\git_down\ai-test-gen
docker compose -f docker-compose.base.yml up -d etcd minio milvus
```

启动 etcd、MinIO、Milvus（约 1 分钟）。

### 第 2 步：构建基础镜像

```bash
docker compose -f docker-compose.base.yml build base --no-cache
```

耗时约 15-25 分钟，包含：
- pip install 全部 Python 依赖（torch、sentence-transformers、pymilvus 等）
- COPY 本地 models/bge-large-zh-v1.5（约 1.3GB，构建前需先下载到项目 models/ 目录，不再联网下载）

> **仅首次或 requirements.txt 变更时需要**。后续日常开发跳过此步。

### 第 3 步：构建并启动应用

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

### 第 4 步：确认启动

```bash
docker compose logs -f backend
```

看到 `Application startup complete` 表示就绪。

### 第 5 步：打开浏览器

访问 [http://localhost](http://localhost)

| 入口 | 地址 | 说明 |
|------|------|------|
| 前端界面 | `http://localhost` | 主页面 |
| 管理后台 | `http://localhost/admin` | 系统配置、产品管理、测试模式库 |
| API 文档 | `http://localhost/api/docs` | Swagger UI |
| 健康检查 | `http://localhost/api/health` | JSON 状态 |
| MinIO | `http://localhost:9001` | minioadmin/minioadmin |

---

## 三、日常开发（修改代码后）

```bash
docker compose up -d --build
```

基于已构建的 `ai-test-gen-base:latest`，只 COPY 业务代码，**< 10 秒**完成。

---

## 四、架构说明

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
| bge 模型 | 基础镜像内 `/app/models` | `Dockerfile.base` COPY 本地模型，无需挂载 |
| 日志 | `./logs/` | loguru 按天轮转 |

---

## 五、日常运维

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
from app.db.vector_store import vector_store
v = vector_store.get_all_requirements(limit=100)
print(f'向量数: {len(v)}')
"
```

---

## 六、配置参考

### docker-compose.yml 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_URL` | `postgresql+asyncpg://...` | PostgreSQL 连接（已预配） |
| `VECTOR_STORE` | `milvus` | 向量后端 |
| `EMBEDDING_MODEL_NAME` | `/app/models/bge-large-zh-v1.5` | 嵌入模型（基础镜像内置本地路径） |
| `DEBUG` | `true` | 热重载模式 |
| `API_PORT` | `8000` | 后端端口 |
| `NGINX_PORT` | `80` | 前端端口 |

### 切换回 SQLite + ChromaDB（临时方案）

```yaml
# 在 docker-compose.yml 中：
environment:
  DATABASE_URL: ""              # 空 = 使用 SQLite
  VECTOR_STORE: chromadb
  EMBEDDING_MODEL_TYPE: chroma_default
# 不需要 postgres / milvus / etcd / minio 服务
```

---

## 七、故障排查

| 现象 | 原因 | 解决 |
|------|------|------|
| 基础镜像构建慢 | pip install torch 2GB+ | 正常 15-25 min，用 `--no-cache` 确保干净 |
| 应用容器 unhealthy | bge 模型未预载 | 确认基础镜像已重建（`docker images ai-test-gen-base` 看日期） |
| PG 连接失败 | asyncpg 缺失 | 基础镜像内已含，确认重建过 |
| 知识库为空 | 向量数据未迁移 | 需手动迁移已归档需求到 Milvus |
| 前端白屏 | 缓存旧版本 | `docker compose build frontend` 重建 |
| 端口冲突 | 80/5432/19530 被占用 | 修改 `.env` 中对应端口 |

---

## 八、附录：文件清单

```
ai-test-gen/
├── docker-compose.base.yml       ← 基础设施 + 基础镜像构建
├── docker-compose.yml            ← 应用层（日常秒级构建）
├── .env                          ← Compose 环境变量
├── .dockerignore                 ← 构建忽略清单
├── DEPLOY.md                     ← 本文件
├── backend/
│   ├── Dockerfile.base           ← 基础镜像：pip install + COPY 本地 bge 模型
│   ├── Dockerfile                ← 应用镜像：FROM base + COPY 代码
│   ├── requirements.txt          ← Python 依赖
│   ├── .env                      ← 后端环境变量
│   └── app/
│       ├── main.py               ← FastAPI 入口
│       ├── config.py             ← 配置管理
│       ├── db/
│       │   ├── database.py       ← 数据库引擎（PG/SQLite 自适应）
│       │   ├── vector_store.py   ← 向量库路由
│       │   └── milvus_store.py   ← Milvus 适配器
│       └── api/                  ← API 路由
├── frontend/
│   ├── Dockerfile                ← 前端构建
│   └── src/                      ← React 源码
├── milvus/                       ← Milvus 运行时数据（etcd + MinIO + 向量）
├── data/pgdata/                  ← PostgreSQL 数据
└── logs/                         ← 应用日志
```
