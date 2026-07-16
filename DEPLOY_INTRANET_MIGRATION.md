# AI-Test-Gen 内网部署迁移方案

## 一、现有架构分析

### 1.1 服务组成

| 服务 | 镜像来源 | 构建方式 | 大小估算 |
|------|----------|----------|----------|
| **backend (API)** | `ai-test-gen-base:latest` + `COPY backend/` | 双层构建，依赖层预烘焙 | ~1.8GB(基础) + ~2MB(代码) |
| **celery_worker** | 同上 `ai-test-gen-base:latest` | 与 backend 共用同一 Dockerfile | 同上 |
| **frontend (Nginx)** | `node:20-alpine`(build) + `nginx:1.27-alpine`(run) | 多阶段构建，产物仅 ~20MB | ~40MB |
| **postgres** | `pgvector/pgvector:pg16` + zhparser 编译 | 自建 Dockerfile | ~600MB |
| **redis** | `redis:7-alpine` | 官方镜像 | ~30MB |
| **etcd** | `quay.io/coreos/etcd:v3.5.5` | 官方镜像 | ~100MB |
| **minio** | `minio/minio:RELEASE.2023-03-20T20-16-18Z` | 官方镜像 | ~250MB |
| **milvus** | `milvusdb/milvus:v2.4.0` | 官方镜像 | ~1.2GB |
| **BGE 模型** | 本地文件，volume 挂载 | 不烘焙进镜像 | ~2.4GB |

### 1.2 现有分层设计的优势

当前项目已经做了很好的分层：

```
Dockerfile.base  →  ai-test-gen-base:latest   (Python 3.12 + 全部 pip 依赖)
                          │
                    Dockerfile  (只 COPY backend/ .)
                          │
                    ai-test-gen-api:latest       (纯代码层，< 2MB)
```

这意味着：
- **基础依赖不变时**，只需重建薄的应用层（秒级）
- **模型文件不进入镜像**，通过 bind mount 挂载，避免每次上传

---

## 二、推荐方案：私有 Docker Registry + 分层推送

### 2.1 架构图

```
┌─────────────────────────────────┐       ┌──────────────────────────────────┐
│        外网开发环境              │       │        内网部署环境              │
│                                 │       │                                  │
│  docker-compose.yml             │       │  docker-compose.intranet.yml     │
│  docker-compose.base.yml        │       │  (镜像地址指向私有 Registry)      │
│         │                       │ push  │         │                        │
│         ▼                       │──────→│         ▼                        │
│  构建 → 打标签 → 推送镜像        │       │  pull 镜像 → docker compose up   │
│         │                       │       │         │                        │
│         ▼                       │       │         ▼                        │
│  私有 Docker Registry           │       │  内网服务器                      │
│  (registry:2 或 Harbor)         │       │  - 只拉取变更的层                │
└─────────────────────────────────┘       └──────────────────────────────────┘
```

### 2.2 增量传输原理

Docker 镜像按层（layer）传输。分层架构的收益：

| 更新场景 | 需传输的内容 | 大小 | 频率 |
|----------|-------------|------|------|
| **首次部署** | 所有镜像层 + 模型文件 | ~6.5GB | 一次 |
| **代码变更（后端）** | 仅 `COPY backend/` 新层 | ~2MB | 频繁 |
| **代码变更（前端）** | 多阶段构建产物（dist + nginx conf） | ~20MB | 频繁 |
| **Python 依赖变更** | 重推 `ai-test-gen-base` 的应用依赖层 | ~200-500MB | 偶尔 |
| **模型升级** | 模型文件（通过 SCP/rsync 非 Docker） | ~2.4GB | 极少 |
| **基础 OS 升级** | 重推整个 `ai-test-gen-base` | ~1.8GB | 极少 |

### 2.3 传输量对比

| 方案 | 首次传输 | 每次代码更新 | 每次依赖更新 |
|------|---------|-------------|-------------|
| ❌ 整机打包（OVA/VM） | ~15GB | 每次 ~15GB | 每次 ~15GB |
| ❌ 单镜像烘焙所有内容 | ~5GB | 每次 ~5GB | 每次 ~5GB |
| ❌ 每次重新构建所有镜像 | ~6.5GB | 每次 ~6.5GB | 每次 ~6.5GB |
| **✅ 本方案（分层推送）** | **~6.5GB** | **~2-20MB** | **~200-500MB** |

---

## 三、实施步骤

### 3.1 前期准备：搭建私有 Docker Registry

**在内网服务器上执行（一次）：**

```bash
# 方案 A：轻量级 registry:2
docker run -d \
  --name registry \
  --restart=always \
  -p 5000:5000 \
  -v /data/registry:/var/lib/registry \
  registry:2

# 方案 B（推荐）：Harbor（企业级，带 Web UI + 权限管理）
# 略，参考 https://goharbor.io
```

**在外网开发机上配置信任：**

```bash
# 假设内网 Registry 地址为 192.168.1.100:5000
echo '{"insecure-registries": ["192.168.1.100:5000"]}' \
  > /etc/docker/daemon.json
systemctl restart docker

# 或修改 Docker Desktop (Windows) Settings → Docker Engine → 添加 insecure-registries
```

### 3.2 第一阶段：迁移基础镜像和模型（一次，大传输）

```bash
# ===== 1. 构建所有外网镜像 =====
# 构建基础镜像（Python 依赖层）
docker compose -f docker-compose.base.yml build base

# 构建 PostgreSQL（含 zhparser 编译）
docker compose build postgres

# 拉取外部官方镜像
docker pull node:20-alpine
docker pull nginx:1.27-alpine
docker pull redis:7-alpine
docker pull quay.io/coreos/etcd:v3.5.5
docker pull minio/minio:RELEASE.2023-03-20T20-16-18Z
docker pull milvusdb/milvus:v2.4.0
docker pull registry:2   # 供内网使用

# ===== 2. 打标签 =====
REGISTRY=192.168.1.100:5000

docker tag ai-test-gen-base:latest       $REGISTRY/ai-test-gen-base:latest
docker tag ai-test-gen-postgres:latest   $REGISTRY/ai-test-gen-postgres:latest
docker tag node:20-alpine                $REGISTRY/node:20-alpine
docker tag nginx:1.27-alpine             $REGISTRY/nginx:1.27-alpine
docker tag redis:7-alpine                $REGISTRY/redis:7-alpine
docker tag quay.io/coreos/etcd:v3.5.5    $REGISTRY/etcd:v3.5.5
docker tag minio/minio:RELEASE.2023-03-20T20-16-18Z $REGISTRY/minio:RELEASE.2023-03-20T20-16-18Z
docker tag milvusdb/milvus:v2.4.0        $REGISTRY/milvus:v2.4.0

# ===== 3. 推送所有镜像 =====
docker push $REGISTRY/ai-test-gen-base:latest
docker push $REGISTRY/ai-test-gen-postgres:latest
docker push $REGISTRY/node:20-alpine
docker push $REGISTRY/nginx:1.27-alpine
docker push $REGISTRY/redis:7-alpine
docker push $REGISTRY/etcd:v3.5.5
docker push $REGISTRY/minio:RELEASE.2023-03-20T20-16-18Z
docker push $REGISTRY/milvus:v2.4.0

# ===== 4. 迁移模型文件（SCP 或 U 盘拷贝） =====
# 模型文件 ~2.4GB，不经过 Docker registry，直接文件传输
rsync -avP ./models/ user@192.168.1.100:/data/models/
```

### 3.3 第二阶段：日常代码更新（小传输）

**在外网开发机上执行：**

```bash
# 一键构建并推送应用镜像
REGISTRY=192.168.1.100:5000

# 构建后端应用镜像（基于已存在的 base，只 COPY 代码）
docker build \
  --build-arg BASE_IMAGE=$REGISTRY/ai-test-gen-base:latest \
  -t $REGISTRY/ai-test-gen-api:latest \
  -f backend/Dockerfile .

# 构建前端应用镜像
docker build \
  -t $REGISTRY/ai-test-gen-web:latest \
  -f frontend/Dockerfile .

# 推送（Docker 自动增量传输，只发送变更的层）
docker push $REGISTRY/ai-test-gen-api:latest
docker push $REGISTRY/ai-test-gen-web:latest
```

**在内网服务器上执行：**

```bash
# 拉取并重启（只拉取变更的层）
docker compose -f docker-compose.intranet.yml pull
docker compose -f docker-compose.intranet.yml up -d
```

---

## 四、内网用 docker-compose.intranet.yml

此文件将所有镜像源指向内网私有 Registry，并移除对外网的依赖。

```yaml
# ============================================================================
# docker-compose.intranet.yml — 内网部署版
# 所有镜像从私有 Registry 拉取，无需外网访问
# ============================================================================

services:
  # ==========================================================================
  # 前端 + Nginx 反向代理
  # ==========================================================================
  frontend:
    image: ${REGISTRY:-192.168.1.100:5000}/ai-test-gen-web:latest
    container_name: ai-test-gen-web
    ports:
      - "${NGINX_PORT:-80}:80"
    depends_on:
      backend:
        condition: service_healthy
    networks:
      - ai-test-gen-net
    restart: unless-stopped

  # ==========================================================================
  # 后端 API 服务
  # ==========================================================================
  backend:
    image: ${REGISTRY:-192.168.1.100:5000}/ai-test-gen-api:latest
    container_name: ai-test-gen-api
    ports:
      - "${API_PORT:-8000}:8000"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
      - ./models/bge-large-zh-v1.5:/models/bge-large-zh-v1.5:ro
      - ./models/bge-reranker-base:/models/bge-reranker-base:ro
    environment:
      <<: &backend-env
        APP_PORT: 8000
        DEBUG: ${DEBUG:-false}
        DATABASE_URL: postgresql+asyncpg://aitestgen:aitestgen123@postgres:5432/ai_test_gen
        VECTOR_STORE: milvus
        MILVUS_HOST: milvus
        MILVUS_PORT: 19530
        EMBEDDING_MODEL_TYPE: local
        EMBEDDING_MODEL_NAME: /models/bge-large-zh-v1.5
        RERANKER_ENABLED: "true"
        RERANKER_MODEL_NAME: /models/bge-reranker-base
        REDIS_HOST: redis
        REDIS_PORT: 6379
        CELERY_BROKER_URL: redis://redis:6379/1
        CELERY_RESULT_BACKEND: redis://redis:6379/2
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"]
      interval: 30s
      timeout: 10s
      retries: 10
      start_period: 60s
    depends_on:
      redis:
        condition: service_healthy
      postgres:
        condition: service_healthy
      milvus:
        condition: service_healthy
    networks:
      - ai-test-gen-net
    restart: unless-stopped

  # ==========================================================================
  # Celery Worker
  # ==========================================================================
  celery_worker:
    image: ${REGISTRY:-192.168.1.100:5000}/ai-test-gen-api:latest
    container_name: ai-test-gen-celery
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
      - ./models/bge-large-zh-v1.5:/models/bge-large-zh-v1.5:ro
      - ./models/bge-reranker-base:/models/bge-reranker-base:ro
    environment:
      <<: *backend-env
    command: celery -A app.tasks.celery_app.celery_app worker --loglevel=info --concurrency=2 --queues=embedding
    healthcheck:
      test: ["CMD-SHELL", "celery -A app.tasks.celery_app.celery_app inspect ping -d celery@$$HOSTNAME --timeout=10 || exit 1"]
      interval: 30s
      timeout: 15s
      retries: 3
      start_period: 60s
    depends_on:
      redis:
        condition: service_healthy
      postgres:
        condition: service_healthy
      milvus:
        condition: service_healthy
    networks:
      - ai-test-gen-net
    restart: unless-stopped

  # ==========================================================================
  # PostgreSQL 16 + pgvector + zhparser
  # ==========================================================================
  postgres:
    image: ${REGISTRY:-192.168.1.100:5000}/ai-test-gen-postgres:latest
    container_name: ai-test-gen-pg
    environment:
      POSTGRES_DB: ai_test_gen
      POSTGRES_USER: aitestgen
      POSTGRES_PASSWORD: ${PG_PASSWORD:-aitestgen123}
    ports:
      - "5432:5432"
    volumes:
      - ./data/pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U aitestgen -d ai_test_gen"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - ai-test-gen-net
    restart: unless-stopped

  # ==========================================================================
  # Redis
  # ==========================================================================
  redis:
    image: ${REGISTRY:-192.168.1.100:5000}/redis:7-alpine
    container_name: ai-test-gen-redis
    ports:
      - "6379:6379"
    volumes:
      - ./data/redis:/data
    command: redis-server --appendonly yes --maxmemory 256mb --maxmemory-policy allkeys-lru
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - ai-test-gen-net
    restart: unless-stopped

  # ==========================================================================
  # Milvus 向量数据库（依赖 etcd + minio）
  # ==========================================================================
  etcd:
    image: ${REGISTRY:-192.168.1.100:5000}/etcd:v3.5.5
    container_name: ai-test-gen-etcd
    environment:
      - ETCD_AUTO_COMPACTION_MODE=revision
      - ETCD_AUTO_COMPACTION_RETENTION=1000
      - ETCD_QUOTA_BACKEND_BYTES=4294967296
      - ETCD_SNAPSHOT_COUNT=50000
    volumes:
      - ./milvus/etcd:/etcd
    command: etcd -advertise-client-urls=http://127.0.0.1:2379 -listen-client-urls http://0.0.0.0:2379 --data-dir /etcd
    healthcheck:
      test: ["CMD", "etcdctl", "endpoint", "health"]
      interval: 30s
      timeout: 20s
      retries: 3
    networks:
      - ai-test-gen-net
    restart: unless-stopped

  minio:
    image: ${REGISTRY:-192.168.1.100:5000}/minio:RELEASE.2023-03-20T20-16-18Z
    container_name: ai-test-gen-minio
    environment:
      MINIO_ACCESS_KEY: ${MINIO_ACCESS_KEY:-minioadmin}
      MINIO_SECRET_KEY: ${MINIO_SECRET_KEY:-minioadmin}
    ports:
      - "9001:9001"
    volumes:
      - ./milvus/minio:/minio_data
    command: minio server /minio_data --console-address ":9001"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 30s
      timeout: 20s
      retries: 3
    networks:
      - ai-test-gen-net
    restart: unless-stopped

  milvus:
    image: ${REGISTRY:-192.168.1.100:5000}/milvus:v2.4.0
    container_name: ai-test-gen-milvus
    command: ["milvus", "run", "standalone"]
    security_opt:
      - seccomp:unconfined
    environment:
      ETCD_ENDPOINTS: etcd:2379
      MINIO_ADDRESS: minio:9000
      MINIO_ACCESS_KEY_ID: ${MINIO_ACCESS_KEY:-minioadmin}
      MINIO_SECRET_ACCESS_KEY: ${MINIO_SECRET_KEY:-minioadmin}
    volumes:
      - ./milvus/milvus:/var/lib/milvus
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9091/healthz"]
      interval: 30s
      timeout: 20s
      retries: 5
      start_period: 60s
    ports:
      - "19530:19530"
      - "9091:9091"
    depends_on:
      etcd:
        condition: service_healthy
      minio:
        condition: service_healthy
    networks:
      - ai-test-gen-net
    restart: unless-stopped

networks:
  ai-test-gen-net:
    driver: bridge
```

---

## 五、自动化脚本

### 5.1 外网一键推送脚本 (push_to_intranet.sh)

```bash
#!/bin/bash
# ============================================================================
# push_to_intranet.sh — 构建并推送镜像到内网 Registry
# 用法: ./push_to_intranet.sh [registry_url]
# 默认: 192.168.1.100:5000
# ============================================================================
set -euo pipefail

REGISTRY="${1:-192.168.1.100:5000}"
TAG="${2:-latest}"

echo "=== 1. 构建后端应用镜像 ==="
docker build \
  --build-arg BASE_IMAGE=${REGISTRY}/ai-test-gen-base:${TAG} \
  -t ${REGISTRY}/ai-test-gen-api:${TAG} \
  -f backend/Dockerfile .

echo "=== 2. 构建前端应用镜像 ==="
docker build \
  -t ${REGISTRY}/ai-test-gen-web:${TAG} \
  -f frontend/Dockerfile .

echo "=== 3. 推送镜像（增量传输） ==="
docker push ${REGISTRY}/ai-test-gen-api:${TAG}
docker push ${REGISTRY}/ai-test-gen-web:${TAG}

echo "=== 完成！==="
echo "后端镜像: ${REGISTRY}/ai-test-gen-api:${TAG}"
echo "前端镜像: ${REGISTRY}/ai-test-gen-web:${TAG}"
```

### 5.2 内网一键更新脚本 (update_from_intranet.sh)

```bash
#!/bin/bash
# ============================================================================
# update_from_intranet.sh — 从内网 Registry 拉取并重启
# 用法: ./update_from_intranet.sh [registry_url]
# 默认: 192.168.1.100:5000
# ============================================================================
set -euo pipefail

REGISTRY="${1:-192.168.1.100:5000}"

echo "=== 拉取最新镜像（增量传输） ==="
REGISTRY=${REGISTRY} docker compose -f docker-compose.intranet.yml pull

echo "=== 重启更新服务 ==="
REGISTRY=${REGISTRY} docker compose -f docker-compose.intranet.yml up -d

echo "=== 清理旧镜像 ==="
docker image prune -f

echo "=== 更新完成！ ==="
```

---

## 六、备选方案

### 方案 B：docker save/load + 离线包传输

适合内网 **没有** Docker Registry，但有文件传输通道（U盘/光盘/网闸）。

```bash
# ===== 外网：打包镜像 =====
# 首次：打包所有基础镜像
docker save \
  ai-test-gen-base:latest \
  ai-test-gen-postgres:latest \
  node:20-alpine \
  nginx:1.27-alpine \
  redis:7-alpine \
  quay.io/coreos/etcd:v3.5.5 \
  minio/minio:RELEASE.2023-03-20T20-16-18Z \
  milvusdb/milvus:v2.4.0 \
  -o ./offline-pack/base-images.tar

# 代码更新后：只打包应用镜像
docker save \
  ai-test-gen-api:latest \
  ai-test-gen-web:latest \
  -o ./offline-pack/app-images.tar

# ===== 内网：加载镜像 =====
docker load -i ./offline-pack/app-images.tar

# 然后 docker compose up -d
```

**缺点**：每次需要完整传输镜像文件（即使只有小改动，`docker save` 保存的是完整镜像）。

### 方案 C：使用 docker buildx bake + 远程缓存

利用 BuildKit 的远程缓存功能，将构建缓存放在内网可访问的位置。

**不推荐**：配置复杂，且对内网环境要求高。

---

## 七、模型文件处理

BGE 模型文件（~2.4GB）是两个目录：

```
models/
├── bge-large-zh-v1.5/     # 嵌入模型，1024维
│   ├── pytorch_model.bin
│   ├── config.json
│   └── ...
└── bge-reranker-base/     # 重排模型
    ├── pytorch_model.bin
    ├── config.json
    └── ...
```

**推荐处理方式**：

1. **首次**：物理拷贝或 rsync 整个 `models/` 到内网服务器
2. **后续**：模型基本不变，无需再次传输
3. 在 docker-compose 中通过 `volumes` 直接挂载宿主机路径，不进入镜像

---

## 八、部署清单总结

### 第一次部署（大传输 ~6.5GB）

| 序号 | 内容 | 方式 | 大小 |
|------|------|------|------|
| 1 | 安装 Docker + docker compose | 官方离线包 | ~200MB |
| 2 | 推送 `ai-test-gen-base` 等 8 个基础镜像 | `docker push` | ~3.5GB |
| 3 | 传输 `models/` 目录 | rsync/SCP | ~2.4GB |
| 4 | 首次构建并推送前后端应用镜像 | `docker build && push` | ~200MB |
| 5 | 内网拉取并启动 | `docker compose pull && up` | - |

### 日常更新（小传输 ~2-20MB）

| 序号 | 内容 | 方式 | 大小 |
|------|------|------|------|
| 1 | 构建并推送后端应用镜像 | `docker build && push` | ~2MB |
| 2 | 构建并推送前端应用镜像 | `docker build && push` | ~20MB |
| 3 | 内网拉取并重启 | `docker compose pull && up` | 增量 |

### 依赖更新（中传输 ~200-500MB）

| 序号 | 内容 | 方式 | 大小 |
|------|------|------|------|
| 1 | 重新构建 `ai-test-gen-base` | `docker compose build base` | - |
| 2 | 推送新基础镜像 | `docker push` | ~200-500MB |
| 3 | 重新构建并推送应用镜像 | `docker build && push` | 增量 |
| 4 | 内网拉取并重启 | `docker compose pull && up` | 增量 |

---

## 九、注意事项

1. **REGISTRY 地址配置**：内网 IP 可能变化，建议使用域名或通过 `docker-compose.intranet.yml` 中的 `REGISTRY` 环境变量控制
2. **Windows 路径**：外网如果是 Windows，注意 `docker compose` 的路径格式
3. **镜像版本管理**：建议用 Git commit SHA 或版本号做 tag（如 `ai-test-gen-api:v1.7.0`），而非总是 `latest`，方便回滚
4. **数据持久化**：PostgreSQL 数据、Milvus 数据、Redis 数据都在 `./data/` 和 `./milvus/` 下，这些目录 **不要** 随镜像更新，需独立备份
5. **首次启动顺序**：`etcd` → `minio` → `milvus` → `postgres` → `redis` → `backend` → `celery_worker` → `frontend`（docker-compose 的 depends_on 已处理）