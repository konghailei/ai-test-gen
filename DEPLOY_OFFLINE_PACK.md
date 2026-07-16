# AI-Test-Gen 离线包部署方案

## 概述

将整个项目打包成一个 **离线部署包**，通过 U 盘 / 光盘 / 网闸拷贝到内网服务器上，
内网机器**完全不需要访问外网**即可完成部署和后续更新。

---

## 设计思路：分层传输

项目已有良好的 Docker 分层结构，离线包据此拆为两层：

```
┌─────────────────────────────────────────────────────┐
│  第一层：基础层（首次部署，~5GB，几乎不更新）        │
│  ┌───────────┬──────────┬──────────────────────────┐ │
│  │ 基础镜像   │ redis    │ 从 Docker Hub 拉取        │ │
│  │ (8个)      │ etcd     │ 保存为 .tar              │ │
│  │           │ minio    │ 内网 docker load 加载     │ │
│  │           │ milvus   │                          │ │
│  │           │ postgres │ 本地 Dockerfile 构建后保存│ │
│  │           │ base     │                          │ │
│  ├───────────┼──────────┼──────────────────────────┤ │
│  │ 模型文件   │ bge系   │ 直接拷贝 ~2.4GB           │ │
│  └───────────┴──────────┴──────────────────────────┘ │
├─────────────────────────────────────────────────────┤
│  第二层：应用层（代码更新，~5MB，频繁更新）          │
│  ┌───────────┬──────────┬──────────────────────────┐ │
│  │ 源码      │ backend  │ 只传变更代码，内网         │ │
│  │           │ frontend │ 用已有 base 镜像重建       │ │
│  │           │ docker   │ docker build 走缓存       │ │
│  └───────────┴──────────┴──────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

### 每次传输量

| 场景 | 传输内容 | 大小 | 频率 |
|------|---------|------|------|
| **首次部署** | 所有基础镜像(.tar) + 模型文件 + 源码 | **~6.5GB** | 一次 |
| **代码更新** | 仅变更的 `backend/` `frontend/` 源码 | **~5MB** | 频繁 |
| **依赖更新** | 重新打包 base 镜像 + 源码 | **~500MB** | 偶尔 |

**关键收益**：利用 Docker 构建缓存，内网重建应用层时只花几秒。

---

## 包目录结构

### 首次部署包（由 `pack-first.sh` 生成）

```
ai-testgen-offline-pack-v{VERSION}/
├── deploy.sh                    # ← 内网部署：加载镜像 → 构建应用 → 启动
├── docker-compose.offline.yml   # ← 离线 compose（内网无网络也工作）
├── install.env                  # ← 可选的部署参数（端口、密码等）
│
├── images/                      # ← 第1层：基础镜像（docker load）
│   ├── 01-python-3.12-slim.tar
│   ├── 02-ai-test-gen-base.tar   # Python 依赖烘焙层
│   ├── 03-ai-test-gen-pg.tar     # PostgreSQL + zhparser
│   ├── 04-redis-7-alpine.tar
│   ├── 05-etcd-v3.5.5.tar
│   ├── 06-minio.tar
│   ├── 07-milvus-v2.4.0.tar
│   ├── 08-node-20-alpine.tar     # 前端构建用
│   └── 09-nginx-1.27-alpine.tar  # 前端运行用
│
├── models/                      # ← BGE 模型（volume 挂载）
│   ├── bge-large-zh-v1.5/
│   └── bge-reranker-base/
│
└── code/                        # ← 第2层：源码（用于内网构建应用镜像）
    ├── backend/
    ├── frontend/
    ├── docker/
    ├── .dockerignore
    └── docker-compose.offline.yml
```

### 增量更新包（由 `pack-update.sh` 生成）

```
ai-testgen-offline-pack-update-v{VERSION}/
├── deploy.sh                    # ← 内网更新：覆盖源码 → 重建 → 重启
├── docker-compose.offline.yml
├── install.env
└── code/
    ├── backend/
    ├── frontend/
    └── docker/
```

> 增量包不含 images/ 和 models/，因为基础镜像已在首次部署时加载到内网机器上。

---

## 使用流程

### 外网开发机上执行

```bash
# 1. 首次打包（需要外网访问 Docker Hub）
bash scripts/pack-first.sh v1.7.0

# 输出：
# ai-testgen-offline-pack-v1.7.0/
# 约 6.5GB，可压缩为 .zip / .7z

# 2. 拷贝到 U 盘，带到内网

# --------------------------------------------------

# 3. 代码变更后，打包增量更新
bash scripts/pack-update.sh v1.7.1

# 输出：
# ai-testgen-offline-pack-update-v1.7.1/
# 约 5MB，压缩后更小
```

### 内网服务器上执行

```bash
# 1. 将离线包上传到内网服务器
#    scp / U盘挂载 / 光盘 均可

# 2. 解压并部署
cd /opt/
tar xzf ai-testgen-offline-pack-v1.7.0.tar.gz
cd ai-testgen-offline-pack-v1.7.0/
bash deploy.sh

# 等待启动完成，访问 http://<内网IP>/

# --------------------------------------------------

# 3. 后续更新
tar xzf ai-testgen-offline-pack-update-v1.7.1.tar.gz
cd ai-testgen-offline-pack-update-v1.7.1/
bash deploy.sh

# 只会覆盖代码 → 重建应用层 → 重启，基础层不动
```

---

## deploy.sh 执行流程

```
deploy.sh
│
├─ [有 images/] → docker load 所有 .tar（首次部署）
│
├─ [有 models/] → 复制模型文件到正确路径
│
├─ [有 code/]   → rsync 覆盖源码
│               → docker compose build backend    ← 走缓存，几秒
│               → docker compose build frontend   ← 走缓存，几秒
│
├─ 创建 data/ milvus/ 等持久化目录
│
└─ docker compose -f docker-compose.offline.yml up -d
```

---

## 补充说明

### 为什么更新包只传源码，不传 docker save 的镜像？

- `docker save ai-test-gen-api:latest` 包含**所有层**（含 1.8GB 的 Python 依赖层）
- 内网已经加载了 `ai-test-gen-base:latest`，`docker build` 会使用缓存
- 重建只产生新的 COPY 层（~2MB），所以源码传输是最小化的

### 如果需要更新基础镜像（升级 Python 依赖）

```bash
# 重新打包包含 base 的完整包
bash scripts/pack-first.sh v1.8.0
# 或只更新 base 镜像的增量包
bash scripts/pack-first.sh --base-only v1.8.0
```

### 内网服务器前置条件

- 已安装 Docker (≥20.10) 和 docker compose
- 磁盘剩余空间 ≥ 30GB（含数据和镜像）
- 建议 16GB 以上内存（Milvus + 模型加载）
