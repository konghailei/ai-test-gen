# AI-Test-Gen 内网离线部署迁移手册

> 适用于 **Linux x86_64** 架构的内网服务器，所有步骤无需外网访问。
> 将整个系统打包为离线包，通过 U 盘 / 光盘 / 网闸拷贝到内网部署。

---

## 目录

1. [架构概览](#1-架构概览)
2. [版本要求](#2-版本要求)
3. [迁移流程（总览）](#3-迁移流程总览)
4. [外网：环境预检 + 前置下载](#4-外网环境预检--前置下载)
5. [外网：首次打包](#5-外网首次打包)
6. [外网：增量更新打包](#6-外网增量更新打包)
7. [内网：升级 Docker（如需要）](#7-内网升级-docker如需要)
8. [内网：一键部署](#8-内网一键部署)
9. [日常更新流程](#9-日常更新流程)
10. [常见问题](#10-常见问题)

---

## 1. 架构概览

### 1.1 服务组成

| 服务 | 容器名 | 镜像来源 | 类型 |
|------|--------|---------|------|
| **前端 Nginx** | `ai-test-gen-web` | 自构建（multi-stage: node + nginx） | 应用层 |
| **后端 API** | `ai-test-gen-api` | 自构建（FROM ai-test-gen-base + COPY backend） | 应用层 |
| **Celery Worker** | `ai-test-gen-celery` | 与后端共用同一镜像 | 应用层 |
| **PostgreSQL** | `ai-test-gen-pg` | 自构建（pgvector/pgvector:pg16 + zhparser） | 基础设施 |
| **Redis** | `ai-test-gen-redis` | `redis:7-alpine` | 基础设施 |
| **etcd** | `ai-test-gen-etcd` | `quay.io/coreos/etcd:v3.5.5` | 基础设施 |
| **MinIO** | `ai-test-gen-minio` | `minio/minio:RELEASE.2023-03-20T20-16-18Z` | 基础设施 |
| **Milvus** | `ai-test-gen-milvus` | `milvusdb/milvus:v2.4.0` | 基础设施 |

### 1.2 分层传输原理

```
第一层：基础设施 + 依赖层（仅首次传输，~5GB，几乎不更新）
  ┌─ 9 个基础镜像（redis/etcd/minio/milvus/postgres/python/node/nginx/base）
  └─ BGE 模型文件（~2.4GB，bind mount 挂载）

第二层：应用代码层（每次更新传输，~5MB，频繁更新）
  ┌─ backend/ 源码
  └─ frontend/ 源码
      ↓ 内网用已有 base 镜像缓存重建，只产生新 COPY 层
```

---

## 2. 版本要求

### 2.1 Docker Engine

| 项目使用的 Docker 特性 | 最低版本 | Docker 1.13.1 |
|-----------------------|---------|--------------|
| 多阶段构建 (`FROM nginx AS builder`) | **17.05+** | ❌ |
| 构建参数 (`ARG BASE_IMAGE`) | **17.05+** | ❌ |
| `docker compose` (V2 插件) | **20.10+** | ❌ |
| Compose YAML 锚点 (`<<: &backend-env`) | Compose **v2.0+** | ❌ |
| `depends_on` + `condition: service_healthy` | Compose **v2.0+** | ❌ |
| BuildKit (`docker buildx`) | **18.09+** | ❌ |
| `docker load` 加载 OCI 格式 tar | **17.05+** | ❌ |
| HEALTHCHECK 指令 | 1.12+ | ✅ |

> ⚠️ **结论：Docker 1.13.1 完全不可用，必须升级到 20.10+（推荐 24+）。**

### 2.2 硬件最低要求

| 资源 | 最低要求 | 推荐 |
|------|---------|------|
| **CPU** | 2 核 | 4 核 |
| **内存** | **8GB** | **16GB** |
| **磁盘** | **30GB** 空闲 | **50GB**（SSD 更佳） |
| **存储驱动** | overlay2 或 overlayfs | overlay2 |
| **内核** | Linux 4.0+ | Linux 5.x+ |

> Milvus 独占约 4GB 内存，BGE 模型加载约 2GB，PostgreSQL + Redis 约 1GB。

### 2.3 各服务的镜像大小参考

| 镜像 | 大小（估算） | 变更频率 |
|------|------------|---------|
| `ai-test-gen-base`（Python 3.12 + pip 依赖） | ~1.8GB | 极低（仅依赖变更） |
| `ai-test-gen-postgres`（pg16 + zhparser） | ~600MB | 几乎不变 |
| `milvusdb/milvus:v2.4.0` | ~1.2GB | 几乎不变 |
| `redis:7-alpine` | ~30MB | 几乎不变 |
| `minio/minio` | ~250MB | 几乎不变 |
| `quay.io/coreos/etcd:v3.5.5` | ~100MB | 几乎不变 |
| `node:20-alpine`（构建用） | ~150MB | 几乎不变 |
| `nginx:1.27-alpine` | ~45MB | 几乎不变 |
| `python:3.12-slim`（base 的上层镜像） | ~120MB | 几乎不变 |
| BGE 模型文件（两个模型） | ~2.4GB | 几乎不变 |
| **应用镜像（后端 + 前端）** | **~2MB + ~20MB** | **每次代码更新** |

---

## 3. 迁移流程（总览）

### 首次迁移

```
外网开发机                                          内网服务器
┌─────────────────────────────┐                  ┌──────────────────┐
│ 1. check-env.sh（可选验环境） │                  │ 1. 安装/升级 Docker│
│ 2. download-docker-offline  │  U盘/网闸拷贝     │ 2. 运行 deploy.sh │
│ 3. pack-first.sh            │ ──────────────→ │    - load 镜像    │
│    → 镜像 .tar              │  (首次约 6.5GB)  │    - 复制模型      │
│    → 模型文件               │                  │    - 构建应用      │
│    → 源码                   │                  │    - 启动服务      │
│    → Docker 安装包          │                  │                   │
└─────────────────────────────┘                  └──────────────────┘
```

### 日常更新

```
外网开发机                                          内网服务器
┌─────────────────────────────┐                  ┌──────────────────┐
│ pack-update.sh              │  U盘/网闸拷贝     │ cd 更新包目录     │
│   → 只打包变更源码          │ ──────────────→ │ bash deploy.sh   │
│   → 更新包约 5MB           │  (每次约 5MB)    │   - rsync 源码    │
│                             │                  │   - compose build │
│                             │                  │   - compose up -d │
└─────────────────────────────┘                  └──────────────────┘
```

---

## 4. 外网：环境预检 + 前置下载

### 4.1 检测内网服务器环境

在内网服务器上运行（仅需 shell，无需 Docker）：

```bash
# 把 scripts/check-env.sh 先拷到内网服务器
bash check-env.sh
```

输出示例：

```
============================================
 AI-Test-Gen 内网环境检测
============================================

--- 1. Docker Engine ---
    版本: 1.13.1
  ❌ Docker Engine < 20.10，需要升级

--- 2. Docker Compose ---
  ❌ Docker Compose 未安装
  ...
```

如果检测出 Docker 版本过低，参考第 7 节升级。

### 4.2 下载 Docker 离线安装包（在内网不通外网时需要）

在外网开发机执行：

```bash
# 下载 Docker 静态二进制 + Compose V2 + BuildX
bash scripts/download-docker-offline.sh

# 输出目录: ./docker-offline/
# ├── docker-28.1.1.tgz                   (~75MB)
# ├── docker-compose-linux-x86_64         (~30MB)
# ├── buildx-v0.23.0.linux-amd64          (~20MB)
# └── install-docker.sh
```

> 如果外网开发机是 Windows，也可以用浏览器手动下载：
> - Docker 二进制: https://download.docker.com/linux/static/stable/x86_64/
> - Compose V2: https://github.com/docker/compose/releases
> - BuildX: https://github.com/docker/buildx/releases

---

## 5. 外网：首次打包

在外网开发机（项目根目录）执行：

```bash
# 先下载模型（如尚未下载）
python scripts/fetch-deps.py --models-only

# 首次打包（需要外网访问 Docker Hub）
bash scripts/pack-first.sh v1.7.0
```

`pack-first.sh` 会自动完成：

```
步骤                                   耗时              网络需求
─────────────────────────────────────────────────────────────
1. docker compose build base           ~15-25 min        需要（pip install）
2. docker compose build postgres       ~5-10 min         需要（apt + git clone）
3. docker pull 7 个外部镜像             ~5 min            需要
4. docker build 前端 + 后端             ~30 秒            不需要（已下载好）
5. docker save → 9 个 .tar 文件         ~2 min            —
6. 复制 models/ 模型文件                 ~2 min            —
7. 复制源码 + 部署脚本                   ~5 秒             —
─────────────────────────────────────────────────────────────
总计                                ~30-40 min
输出目录: ./ai-testgen-offline-pack-v1.7.0/  (~6.5GB)
```

打包完成后目录结构：

```
ai-testgen-offline-pack-v1.7.0/
├── deploy.sh                       # 内网一键部署脚本
├── docker-compose.offline.yml      # 离线 compose
├── install.env                     # 部署配置
│
├── images/                         # 9 个基础镜像 tar
│   ├── 01-python-3.12-slim.tar
│   ├── 02-ai-test-gen-base.tar
│   ├── 03-ai-test-gen-pg.tar
│   ├── 04-redis-7-alpine.tar
│   ├── 05-etcd-v3.5.5.tar
│   ├── 06-minio.tar
│   ├── 07-milvus-v2.4.0.tar
│   ├── 08-node-20-alpine.tar
│   └── 09-nginx-1.27-alpine.tar
│
├── models/                         # BGE 模型文件
│   ├── bge-large-zh-v1.5/
│   └── bge-reranker-base/
│
└── code/                           # 源码（内网构建用）
    ├── backend/
    ├── frontend/
    ├── docker/
    └── .dockerignore
```

---

## 6. 外网：增量更新打包

代码修改后，在外网开发机执行：

```bash
bash scripts/pack-update.sh v1.7.1
```

输出：

```
ai-testgen-offline-pack-update-v1.7.1/
├── deploy.sh
├── docker-compose.offline.yml
├── install.env
├── VERSION                        # 版本 + 时间戳
└── code/
    ├── backend/
    ├── frontend/
    └── docker/
```

> **大小：约 5MB**（仅源码，不含镜像和模型）。
> 内网收到后，deploy.sh 会用已存在的 base 镜像缓存重建应用镜像，只产生新的 COPY 层。

### ⚠️ v3.3.0 依赖变更特别说明（docs/17 HTML 原型支持）

本次更新在 `backend/requirements.txt` 新增了 `beautifulsoup4==4.12.2` 与 `lxml==5.*`
（用于上传 HTML 原型的语义树解析）。**这次增量更新包不能只发源码**：

- 依赖装在 **base 镜像**（`ai-test-gen-base`），源码包重建应用镜像只 COPY 代码、
  不会装新依赖——内网若只更新源码，上传 .html 原型将直接运行时 `ImportError`。
- 正确做法（二选一）：
  1. **重导 base 镜像**（推荐）：外网 `docker compose -f docker-compose.base.yml build base`
     → `docker save ai-test-gen-base -o 02-ai-test-gen-base.tar` → 随包传输 → 内网 `docker load`；
  2. 内网 base 容器内手动 `pip install beautifulsoup4==4.12.2 "lxml>=5,<6"`（应急，不推荐——
     镜像重建后会丢失，且绕过 Dockerfile 构建期硬校验）。
- 构建期硬校验已加探针（`import bs4, lxml`），漏装时 base 镜像构建直接失败，不会带病上线。
- Dockerfile.base 本次有变更，**首次迁移/重导镜像的离线包必须包含最新 `backend/Dockerfile.base`**。

---

## 7. 内网：升级 Docker（如需要）

如果内网服务器的 Docker 版本过低（如 1.13.1），用下载好的离线包升：

### 7.1 拷贝离线包

将外网下载的 `docker-offline/` 目录拷贝到内网服务器。

### 7.2 停止旧 Docker 并安装

```bash
# 停止旧版本
systemctl stop docker
systemctl disable docker

# 卸载旧版本（CentOS）
yum remove -y docker docker-common docker-selinux docker-engine

# 安装新版本
sudo bash docker-offline/install-docker.sh

# 启动
systemctl enable docker
systemctl start docker

# 验证
docker --version          # 应显示 28.1.1
docker compose version    # 应显示 v2.35.1
docker buildx version     # 应显示 v0.23.0
```

### 7.3 install-docker.sh 做了什么

```
install-docker.sh
├── 解压 docker-28.1.1.tgz → /usr/local/bin/
│   ├── docker
│   ├── dockerd
│   ├── containerd
│   ├── containerd-shim-runc-v2
│   └── runc
├── 复制 docker-compose-linux-x86_64 → /usr/local/lib/docker/cli-plugins/docker-compose
├── 复制 buildx-v*.linux-amd64       → /usr/local/lib/docker/cli-plugins/docker-buildx
└── 创建 /etc/systemd/system/docker.service
```

---

## 8. 内网：一键部署

### 8.1 解压离线包

```bash
# 放到目标目录
mkdir -p /opt/ai-testgen
cd /opt/ai-testgen

# 解压（如压缩了）
tar xzf /path/to/ai-testgen-offline-pack-v1.7.0.tar.gz
cd ai-testgen-offline-pack-v1.7.0/
```

### 8.2 运行部署脚本

```bash
bash deploy.sh
```

`deploy.sh` 会自动完成以下步骤：

```
deploy.sh
│
├─ [阶段 0] 环境预检
│   ├─ Docker Engine 版本检查 (≥20.10)
│   ├─ Docker Compose 检查 (V2)
│   ├─ OS / 内核检查
│   ├─ 内存检查 (≥8GB)
│   ├─ 磁盘检查 (首次 ≥30GB，更新 ≥5GB)
│   └─ BuildKit 检查
│
├─ [阶段 1] 加载基础镜像（仅首次）
│   └─ docker load -i images/*.tar   ← 9个镜像，约3-5分钟
│
├─ [阶段 2] 复制模型文件（仅首次）
│   └─ rsync models/ → ./models/
│
├─ [阶段 3] 更新源码并重建（首次和更新都会执行）
│   ├─ rsync code/backend/ → backend/
│   ├─ rsync code/frontend/ → frontend/
│   ├─ docker compose build backend   ← 走缓存，几秒
│   └─ docker compose build frontend  ← 走缓存，几秒
│
├─ [阶段 4] 创建持久化数据目录
│   ├─ data/pgdata/
│   ├─ data/redis/
│   ├─ milvus/etcd/
│   ├─ milvus/minio/
│   ├─ milvus/milvus/
│   └─ logs/
│
├─ [阶段 5] 启动所有服务
│   └─ docker compose -f docker-compose.offline.yml up -d
│
└─ [阶段 6] 检查服务健康状态
    ├─ ai-test-gen-pg        ✅ healthy
    ├─ ai-test-gen-redis     ✅ healthy
    ├─ ai-test-gen-etcd      ✅ healthy
    ├─ ai-test-gen-minio     ✅ healthy
    ├─ ai-test-gen-milvus    ✅ healthy
    ├─ ai-test-gen-api       ✅ healthy
    ├─ ai-test-gen-web       ✅ healthy
    └─ ai-test-gen-celery    ✅ healthy
```

### 8.3 部署完成

```
============================================
 部署完成！
============================================

  前端地址:  http://localhost:80
  API 地址:  http://localhost:8000

  常用命令:
    查看日志:  docker compose -f docker-compose.offline.yml logs -f
    重启服务:  docker compose -f docker-compose.offline.yml restart
    停止服务:  docker compose -f docker-compose.offline.yml down
```

> 首次启动后，Milvus 初始化约需 1-2 分钟，API 健康检查在这之后才能通过。\
> 如果看到部分服务状态为 `starting`，等待 2 分钟后再次运行 `docker compose ps`。

### 8.4 可选配置

编辑 `install.env` 调整部署参数：

```bash
# 端口配置
NGINX_PORT=80
API_PORT=8000

# 关闭调试模式（生产环境建议）
DEBUG=false

# 数据库密码
PG_PASSWORD=aitestgen123

# MinIO（Milvus 底层存储）
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
```

使用方式：

```bash
# 方法一：修改 install.env 后直接部署
bash deploy.sh

# 方法二：在环境变量中覆盖
NGINX_PORT=8080 API_PORT=8443 bash deploy.sh
```

---

## 9. 日常更新流程

### 9.1 外网打包更新

```bash
cd /path/to/ai-test-gen   # 项目根目录

# 打包增量更新
bash scripts/pack-update.sh v1.7.1

# 输出到: ../ai-testgen-offline-pack-update-v1.7.1/
```

### 9.2 内网部署更新

```bash
cd /opt/ai-testgen

# 解压更新包
tar xzf /path/to/ai-testgen-offline-pack-update-v1.7.1.tar.gz
cd ai-testgen-offline-pack-update-v1.7.1/

# 一键更新
bash deploy.sh
# 或指定到现有安装目录
INSTALL_DIR=/opt/ai-testgen bash deploy.sh
```

`deploy.sh` 会自动：

1. 检测到无 `images/` 目录 → 跳过镜像加载
2. 检测到无 `models/` 目录 → 跳过模型复制
3. 检测到 `code/` 目录 → rsync 覆盖源码 → `docker compose build`（利用缓存，秒级）→ `docker compose up -d`

### 9.3 各场景传输量

| 场景 | 需要传输的内容 | 大小 | deploy.sh 会自动… |
|------|--------------|------|-------------------|
| **首次部署** | 完整离线包（镜像+模型+源码） | **~6.5GB** | load 镜像 + 复制模型 + 构建 + 启动 |
| **代码更新** | 增量更新包（仅源码） | **~5MB** | rsync 源码 + 重建应用镜像 + 重启 |
| **依赖升级** | 需要重新打首次包 | **~500MB-1GB** | 同上 |

---

## 10. 常见问题

### Q1: `deploy.sh` 报错 "Docker 未安装" 怎么处理？

```bash
# 方法一：先安装 Docker 离线包
sudo bash docker-offline/install-docker.sh
systemctl start docker

# 方法二：跳过检查直接部署（仅用于已确认环境可用）
SKIP_CHECK=true bash deploy.sh
```

### Q2: 可以在 Windows 内网服务器上部署吗？

不支持。本方案目标是 **Linux x86_64** 服务器。Windows 内网建议使用 Windows Docker Desktop，但由于本项目使用了 `docker compose` V2 语法和 bind mount 路径格式，需要在 Windows 上另行调整。

### Q3: 部署后如何查看日志？

```bash
# 查看所有服务日志
docker compose -f docker-compose.offline.yml logs -f

# 查看单个服务
docker compose -f docker-compose.offline.yml logs -f backend
docker compose -f docker-compose.offline.yml logs -f frontend
```

### Q4: 如何备份数据？

```bash
# 数据在以下目录，定期备份即可
./data/pgdata/          # PostgreSQL 数据
./data/redis/           # Redis 持久化
./milvus/milvus/        # Milvus 向量数据
./milvus/etcd/          # Milvus 元数据
./milvus/minio/         # Milvus 对象存储
```

### Q5: Python 依赖变更了如何更新？

```bash
# 在外网重新构建 base 镜像
docker compose -f docker-compose.base.yml build base

# 然后重新运行 pack-first.sh 打完整包
bash scripts/pack-first.sh v1.8.0
# （或手动导出 base 镜像更新）
docker save ai-test-gen-base:latest -o ai-test-gen-base.tar
```

### Q6: 是否可以将整个离线包压缩？

```bash
# pack-first.sh 默认会自动压缩为 tar.gz
# 也可以手动：
tar czf ai-testgen-offline-pack-v1.7.0.tar.gz ai-testgen-offline-pack-v1.7.0/
# 压缩率有限（镜像 tar 已是压缩格式），大约 6.5GB → 5.5GB
```