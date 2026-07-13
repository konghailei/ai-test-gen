# AI-Test-Gen 内网部署指南

> 适用场景：无外网 / 受限网络环境下的服务器部署（离线镜像 + 本地模型）
> 部署形态：Docker Compose 单机全栈（8 容器）
> 版本基线：v1.2.0

---

## 1. 文档目的

本指南面向运维 / 实施人员，提供一套**完整的、可离线执行**的 AI-Test-Gen 部署流程，涵盖：

- 部署前的离线资源准备（基础镜像 + 本地模型）
- 首次全量部署步骤
- 初始化配置（LLM Key、管理员账号）
- 生产环境加固建议
- 数据备份、日常运维与升级

> ⚠️ **关键前提**：本系统依赖本地模型（`bge-large-zh-v1.5` 嵌入、`bge-reranker-base` 重排）与多个 Docker 镜像。**所有镜像与模型必须在有外网的机器上预先拉取 / 下载后，再导入内网服务器**。内网部署过程中全程不需要访问 HuggingFace 或 Docker Hub。

---

## 2. 系统架构与组件

```
                       ┌──────────────────────┐
   浏览器 ── http://IP ─┤  Nginx (frontend:80)  │  静态站点 + /api 反向代理
                       └──────────┬───────────┘
                                  │ /api/*
                       ┌──────────▼───────────┐
                       │  FastAPI (api:8000)   │
                       └──┬───────┬───────┬────┘
            ┌─────────────┘       │       └─────────────┐
     ┌──────▼──────┐  ┌──────────▼──────┐  ┌──────────▼──────┐
     │ PostgreSQL  │  │   Milvus 2.4    │  │    Redis 7      │
     │  16+pgvector│  │   Standalone    │  │  缓存+Celery MQ │
     └─────────────┘  └──┬──────────┬──┘  └─────────────────┘
                  ┌──────▼───┐  ┌────▼─────┐
                  │  etcd    │  │  MinIO   │   (Milvus 内部依赖)
                  └──────────┘  └──────────┘
```

### 容器清单（共 8 个）

| 容器名 | 镜像 / 构建 | 说明 | 对外端口 |
|--------|------------|------|---------|
| `ai-test-gen-web` | 前端镜像 | React + Nginx 反代 | **80** |
| `ai-test-gen-api` | 应用镜像 | FastAPI 后端 | 8000 |
| `ai-test-gen-celery` | 应用镜像 | Celery Worker（异步嵌入 + 向量双写） | — |
| `ai-test-gen-pg` | pgvector/pg16 | 业务库 + BM25 全文检索 | 5432 |
| `ai-test-gen-redis` | redis:7-alpine | 缓存 + 任务队列 | 6379 |
| `ai-test-gen-milvus` | milvusdb/milvus:v2.4.0 | 向量库 | 19530 / 9091 |
| `ai-test-gen-etcd` | quay.io/coreos/etcd:v3.5.5 | Milvus 元数据 | — |
| `ai-test-gen-minio` | minio/minio:RELEASE... | Milvus 对象存储 | 9001 |

> 除前端 80 端口外，其余端口默认仅在 Docker 网络内部互通。**对外只暴露 80 端口即可**（通过 Nginx 代理访问 API）。如需运维直接连数据库 / 向量库，再按需单独暴露。

---

## 3. 环境要求

| 项目 | 最低要求 | 推荐 |
|------|---------|------|
| 操作系统 | Linux x86_64（Ubuntu 22.04 / CentOS 7+） | Ubuntu 22.04 LTS |
| Docker Engine | 24.0+ | 26.x |
| Docker Compose | v2.20+ | v2.27+ |
| CPU | 4 核 | 8 核 |
| 内存 | 8 GB | 16 GB（嵌入 + 重排在 CPU 上运行，较吃内存） |
| 磁盘 | 20 GB 空闲 | 50 GB+（模型 ~1.5GB，镜像 ~10GB，数据增长） |
| 模型 | 必选：bge-large-zh-v1.5；可选：bge-reranker-base | 两者均本地 |

> ⚠️ **磁盘预留**：Milvus 向量数据、PostgreSQL 数据均持久化在宿主机目录，会随知识库增长。生产环境建议给数据盘预留 ≥ 50GB。

---

## 4. 部署前准备（离线资源）

### 4.1 在有外网的机器上准备

推荐在**一台能联网的临时机器**上完成镜像与模型的预拉取，再整体搬运到内网。

#### (a) 拉取并导出基础镜像

```bash
# 1. 克隆代码
git clone <你的内网 git 地址或解压离线包> ai-test-gen
cd ai-test-gen

# 2. 构建基础镜像（含全部 Python 依赖，约 15-25 分钟）
docker compose -f docker-compose.base.yml build base

# 3. 预热基础设施镜像（etcd/minio/milvus/redis/base）
python scripts/fetch-deps.py --no-models      # 仅预热镜像，不下载模型
# 或手动：
# docker pull quay.io/coreos/etcd:v3.5.5
# docker pull minio/minio:RELEASE.2023-03-20T20-16-18Z
# docker pull milvusdb/milvus:v2.4.0
# docker pull redis:7-alpine
# docker pull pgvector/pgvector:pg16
# docker pull node:20-alpine
# docker pull nginx:1.27-alpine
# docker pull python:3.12-slim

# 4. 导出全部镜像为 tar（便于离线搬运）
docker images | grep -E "ai-test-gen|milvus|etcd|minio|redis|pgvector|node:20|nginx:1.27|python:3.12" | awk '{print $1":"$2}' > images.txt
cat images.txt | xargs -I {} docker save -o {}.tar
# 合并为一个包（可选）
# cat *.tar > ai-test-gen-images.tar
```

#### (b) 下载本地模型（必需）

```bash
# 在能联网的机器执行（纯 Python，无需 Docker / WSL）
python scripts/fetch-deps.py --models-only

# 校验落盘
ls -la models/bge-large-zh-v1.5/ | head
ls -la models/bge-reranker-base/ | head
```

模型将落在 `./models/` 目录下，部署时通过 bind mount 挂载进容器（**镜像内不含模型**）。

> 若 HuggingFace 不可达，脚本会给出代理 / 手动放置指引。手动放置时目录结构应为：
> - `models/bge-large-zh-v1.5/`（含 config.json、pytorch_model.bin、tokenizer 等）
> - `models/bge-reranker-base/`（含 config.json、model.safetensors 等）

### 4.2 搬运到内网服务器

```bash
# 在临时机器打包
tar czf ai-test-gen-offline.tar.gz \
    ai-test-gen/   # 完整代码目录（含 models/、docker-compose*.yml、Dockerfile*、scripts/）
# 加上导出的镜像 tar

# 内网服务器解压后导入镜像
# docker load -i ai-test-gen-images.tar
# 或逐个：docker load -i milvusdb-milvus-v2.4.0.tar ...
```

### 4.3 内网服务器目录确认

解压后确保以下关键文件 / 目录存在：

```
ai-test-gen/
├── docker-compose.yml
├── docker-compose.base.yml
├── backend/Dockerfile
├── backend/Dockerfile.base
├── docker/postgres.Dockerfile
├── frontend/Dockerfile
├── frontend/nginx.conf
├── scripts/fetch-deps.py
├── models/bge-large-zh-v1.5/      ← 必须存在
├── models/bge-reranker-base/      ← 建议存在（重排）
└── .dockerignore
```

---

## 5. 首次部署步骤

### 步骤 1：加载基础镜像（若用离线 tar）

```bash
cd /path/to/ai-test-gen
# 若镜像已 docker load，跳过；否则构建：
docker compose -f docker-compose.base.yml build base
docker compose -f docker-compose.base.yml up -d   # 启动 etcd/minio/milvus 基础设施
```

### 步骤 2：（可选）生产环境调整

**强烈建议生产环境修改 `docker-compose.yml`：**

1. **关闭 DEBUG 热重载**：
   ```yaml
   environment:
     - DEBUG=false
   ```
   > 设为 `false` 后，系统会强制要求 `MASTER_KEY`（Fernet 主密钥）与 JWT 密钥，否则拒绝启动。详见第 7 节。

2. **取消代码 bind mount（生产应把代码烘焙进镜像）**：
   注释掉 `backend` 和 `celery_worker` 服务中的以下两行（共 4 处）：
   ```yaml
   # - ./backend:/app        # ← 生产环境注释掉，避免代码随宿主机变化
   ```
   保留模型与数据挂载：
   ```yaml
   - ./data:/app/data
   - ./logs:/app/logs
   - ./models/bge-large-zh-v1.5:/models/bge-large-zh-v1.5:ro
   - ./models/bge-reranker-base:/models/bge-reranker-base:ro
   ```

3. **修改默认密码与密钥**（见第 7 节）。

### 步骤 3：构建并启动全部服务

```bash
# 构建应用镜像并启动 8 个容器
docker compose up -d --build

# 观察后端启动日志，看到 "Application startup complete" 即就绪
docker compose logs -f backend
```

> 首次启动会自动执行数据库迁移、建表、写入种子数据（角色、默认管理员、测试模式库）。

### 步骤 4：验证部署

```bash
# 健康检查（应返回 success:true，含模型就绪状态）
curl -s http://localhost/api/health | python -m json.tool

# 模型加载较慢（CPU 加载 bge ~1.5GB，约 2-3 分钟），等待 model_ready:true：
# "vector_db": { "model_ready": true, ... }
```

浏览器访问：

| 入口 | 地址 | 说明 |
|------|------|------|
| 前端主界面 | `http://<服务器IP>` | 需求分析、用例生成 |
| 管理后台 | `http://<服务器IP>/admin` | 系统设置、产品、测试模式库、提示词 |
| API 文档 | `http://<服务器IP>/api/docs` | Swagger UI |
| 健康检查 | `http://<服务器IP>/api/health` | 状态 JSON |

---

## 6. 初始化配置

### 6.1 登录管理员账号

- 默认管理员：**`admin` / `888888`**
- 首次登录后**务必到「系统设置」修改默认密码**。

### 6.2 配置 LLM 提供商（必需）

系统使用 LLM 解析需求、生成用例。需配置至少一个可用的 API Key：

1. 进入「系统设置」页面
2. 选择提供商（如 **百炼 / DeepSeek / OpenAI**），填入 API Key、Base URL、模型名
3. 点击「保存」

> ✅ API Key **持久化到 PostgreSQL**（Fernet 加密），重启不丢失。
> ⚠️ 若 `.env` 中残留占位值（如 `sk-dummy`），以数据库配置为准——保存后会覆盖。
> 配置完成后，`/api/health` 返回的 `llm_configured` 应为 `true`。

### 6.3 知识库冷启动

- 首次部署知识库为空（`kb_empty: true`），属**正常冷启动模式**——需求澄清会降低通过阈值，直接生成用例。
- 归档需求后，向量自动写入 Milvus，后续需求即可检索历史知识。

---

## 7. 生产环境加固（推荐）

| 加固项 | 操作 | 原因 |
|--------|------|------|
| **关闭 DEBUG** | `docker-compose.yml` 设 `DEBUG=false` | 关闭 uvicorn 热重载，启用生产模式校验 |
| **设置 MASTER_KEY** | 在 `backend` 环境变量加 `MASTER_KEY=<Fernet 密钥>` | `DEBUG=false` 时强制要求，否则拒绝启动；用于解密 DB 中的 API Key |
| **设置 JWT 密钥** | 加 `APP_SECRET_KEY=<随机串>` | 签名 JWT，避免重启后 token 失效 |
| **移除代码挂载** | 注释 `./backend:/app`（见步骤 2） | 生产代码应固化进镜像，防止宿主机误改 |
| **修改默认管理员密码** | 系统设置中修改 `admin` 密码 | 防止默认口令被利用 |
| **修改数据库密码** | `docker-compose.yml` + `PG_PASSWORD` 环境变量 | 默认 `aitestgen123` 不安全 |
| **修改 MinIO 密钥** | `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | 默认 `minioadmin` |
| **防火墙只放行 80** | 安全组 / iptables 仅开放 80 | 数据库 / 向量库不暴露公网 |
| **HTTPS（可选）** | 在 Nginx 前加一层 LB 或改 `frontend/nginx.conf` 启用 TLS | 内网有合规要求时 |

生成密钥：

```bash
# Fernet 主密钥
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# JWT 随机串
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

> ⚠️ **MASTER_KEY 务必备份**：丢失它 = 永久无法解密已存储的 LLM API Key，必须重新配置。

---

## 8. 数据持久化与备份

### 8.1 持久化目录

| 数据 | 宿主机路径 | 说明 |
|------|-----------|------|
| PostgreSQL 数据 | `./data/pgdata/` | 业务数据 + 加密的 LLM 配置 |
| 加密主密钥 | `./data/.keys/master.key` | Fernet 密钥文件（与 MASTER_KEY 二选一） |
| Milvus 向量 | `./milvus/milvus/` | 需求 / 用例向量 |
| Milvus 元数据 | `./milvus/etcd/` | etcd |
| Milvus 对象存储 | `./milvus/minio/` | MinIO |
| 本地模型 | `./models/` | 嵌入 / 重排模型（**备份时无需包含，可重新下载**） |
| 日志 | `./logs/` | 按天轮转 |

### 8.2 备份建议

```bash
# 停止写入（或低峰期）后打包数据目录
tar czf ai-test-gen-backup-$(date +%F).tar.gz \
    data/pgdata data/.keys milvus etcd logs

# 仅备份 PostgreSQL（逻辑备份，更通用）
docker exec ai-test-gen-pg pg_dump -U aitestgen ai_test_gen > pg-backup.sql
```

> 恢复时：停止容器 → 解压覆盖对应目录 → `docker compose up -d`。
> **MASTER_KEY 与备份包必须一同保管**，否则无法解密 API Key。

---

## 9. 日常运维

### 9.1 启停

```bash
docker compose ps                         # 查看所有容器状态
docker compose stop                       # 暂停（保留数据）
docker compose start                      # 恢复
docker compose down                       # 停止并删容器（保留数据卷）
docker compose down -v                    # ⚠️ 删除所有数据（慎用）
```

### 9.2 日志

```bash
docker compose logs -f backend            # 后端实时日志
docker compose logs -f celery             # 异步任务日志
docker compose logs -f web                # 前端 / Nginx 日志
```

### 9.3 代码更新（修改业务代码后）

```bash
git pull                                  # 或用离线包覆盖
docker compose up -d --build              # 基于已有基础镜像，秒级重建应用层
```

### 9.4 依赖更新（requirements.txt 变更后）

```bash
docker compose -f docker-compose.base.yml build base --no-cache
docker compose up -d --build
```

### 9.5 完全重置（清空所有数据，重新来过）

```bash
docker compose down -v
docker compose -f docker-compose.base.yml down -v
# 重新 up 后数据为空，需重新配置 LLM、重新归档需求
```

---

## 10. 故障排查

| 现象 | 可能原因 | 解决 |
|------|---------|------|
| 后端容器 `unhealthy` | bge 模型未挂载 / 路径错误 | 检查 `./models/bge-large-zh-v1.5/config.json` 是否存在；确认 compose 中 bind mount 路径正确 |
| `/api/health` 中 `model_ready: false` | 首次启动模型仍在 CPU 加载（1-3 分钟） | 等待；若超时检查模型目录完整性 |
| 知识库为空 / 上传需求提示 KB 为空 | 需求尚未归档，向量未写入 | 归档一条需求后向量自动写入；归档后稍等 Celery 处理 |
| 上传需求报错「所有 LLM 均不可用 / 401」 | LLM Key 未配置或无效 | 系统设置中重新保存有效 Key；确认 `llm_configured: true` |
| 端口冲突（80 / 5432 / 19530 被占用） | 宿主已有服务占用 | 修改 `docker-compose.yml` 对应端口映射 |
| PostgreSQL 连接失败 | 密码 / 网络不匹配 | 确认 `DATABASE_URL` 与 PG 容器密码一致 |
| 前端白屏 / 旧版本 | 前端缓存或镜像未重建 | `docker compose build frontend && docker compose up -d frontend` |
| `DEBUG=false` 启动失败 | 未设 `MASTER_KEY` / JWT 密钥 | 配置第 7 节所述密钥后重启 |
| zhparser 不可用 | PG16 镜像无此包 | 自动降级为 `simple` 配置，不影响功能（中文分词精度略降，由重排补偿） |

### 常用诊断命令

```bash
# 进入数据库
docker exec -it ai-test-gen-pg psql -U aitestgen -d ai_test_gen

# 查看 Milvus 向量数
docker exec ai-test-gen-api python -c "
import sys; sys.path.insert(0,'/app')
from app.db.vector_store import vector_store
v = vector_store.get_all_requirements(limit=100)
print('向量数:', len(v))
"

# 查看容器资源占用
docker stats
```

---

## 11. 附录

### 11.1 端口速查

| 端口 | 服务 | 默认暴露 | 用途 |
|------|------|---------|------|
| 80 | Nginx / 前端 | ✅ 对外 | 用户访问入口 |
| 8000 | FastAPI | 内网 | 后端 API |
| 5432 | PostgreSQL | 内网 | 数据库（运维用） |
| 6379 | Redis | 内网 | 缓存 / MQ |
| 19530 | Milvus | 内网 | 向量服务 |
| 9091 | Milvus health | 内网 | 健康检查 |
| 9001 | MinIO console | 内网 | 对象存储控制台（minioadmin） |

### 11.2 关键环境变量（docker-compose.yml）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEBUG` | `true` | 生产设 `false` |
| `DATABASE_URL` | `postgresql+asyncpg://...` | PG 连接串 |
| `VECTOR_STORE` | `milvus` | 向量后端 |
| `EMBEDDING_MODEL_NAME` | `/models/bge-large-zh-v1.5` | 嵌入模型（宿主机挂载） |
| `RERANKER_MODEL_NAME` | `/models/bge-reranker-base` | 重排模型（宿主机挂载） |
| `MASTER_KEY` | 空 | `DEBUG=false` 时必填，Fernet 密钥 |
| `APP_SECRET_KEY` | 空 | JWT 签名密钥 |
| `PG_PASSWORD` | `aitestgen123` | PG 密码（生产修改） |
| `NGINX_PORT` | `80` | 前端端口 |
| `API_PORT` | `8000` | 后端端口 |

### 11.3 默认账号

| 账号 | 密码 | 说明 |
|------|------|------|
| `admin` | `888888` | 超级管理员，**首次登录后立即修改** |

---

> 文档版本：v1.2.0 对应部署 | 生成日期：2026-07-13
> 维护：高级开发工程师（Senior Developer）
