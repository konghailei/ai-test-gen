#!/bin/bash
# ============================================================================
# pack-first.sh — 首次离线打包脚本
#
# 用法: bash scripts/pack-first.sh <版本号> [--no-compress]
#   例: bash scripts/pack-first.sh v1.7.0
#       bash scripts/pack-first.sh v1.7.0 --no-compress   # 不压缩，保留目录
#
# 前置条件:
#   1. 在项目根目录运行
#   2. 需要外网访问 Docker Hub
#   3. 已安装 Docker + docker compose
# ============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# ---------- 参数 ----------
VERSION="${1:-}"
[ -z "$VERSION" ] && { echo "用法: bash scripts/pack-first.sh <版本号>"; exit 1; }
COMPRESS="${2:-}"
OUTPUT_DIR="../ai-testgen-offline-pack-${VERSION}"
OUTPUT_DIR="$(cd "$(dirname "$OUTPUT_DIR")" && pwd)/$(basename "$OUTPUT_DIR")"

echo "============================================"
echo " AI-Test-Gen 离线包打包脚本 (首次)"
echo " 版本: $VERSION"
echo " 输出: $OUTPUT_DIR"
echo "============================================"

# ---------- 清理临时目录 ----------
rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR/images" "$OUTPUT_DIR/models" "$OUTPUT_DIR/code"

# 记录版本信息（与 pack-update.sh 对称；此前首次包只有目录名带版本，
# 部署时 deploy.sh 读不到 VERSION → 版本信息显示"未标注"，事后无法核对）
echo "$VERSION" > "$OUTPUT_DIR/VERSION"
echo "$(date '+%Y-%m-%d %H:%M:%S')" >> "$OUTPUT_DIR/VERSION"

# 版本一致性校验：包名版本必须与代码内 VERSION 一致，
# 否则内网部署出来的实际版本和包的标称版本不符（发版纪律问题）。
CODE_VER=$(head -1 backend/app/VERSION 2>/dev/null | tr -d '\r\n')
FRONT_VER=$(grep -m1 '"version"' frontend/package.json 2>/dev/null | sed 's/.*: *"\([^"]*\)".*/\1/')
echo ""
echo "=== 版本一致性校验 ==="
echo "  包版本    : $VERSION"
echo "  后端 VERSION: ${CODE_VER:-<缺失>}"
echo "  前端 version: ${FRONT_VER:-<缺失>}"
STRIP_VER="${VERSION#v}"
if [ "$STRIP_VER" != "$CODE_VER" ] || [ "$STRIP_VER" != "$FRONT_VER" ]; then
  echo "  ⚠️ 版本不一致：请先把 backend/app/VERSION 与 frontend/package.json 改成 $STRIP_VER"
  echo "     （继续打包也能用，但内网会看到版本号对不上）"
fi
echo ""

# ==================================================================
# 1. 构建/拉取所有镜像
# ==================================================================
echo ""
echo "=== [1/4] 构建基础镜像 ==="

# 1a. 基础 Python 依赖镜像
echo "--- 构建 ai-test-gen-base:latest ---"
docker compose -f docker-compose.base.yml build base

# 1b. PostgreSQL + zhparser
echo "--- 构建 ai-test-gen-postgres:latest ---"
docker compose build postgres

# 1c. 拉取外部官方镜像
echo "--- 拉取外部镜像 ---"
for img in \
  python:3.12-slim \
  node:20-alpine \
  nginx:1.27-alpine \
  redis:7-alpine \
  quay.io/coreos/etcd:v3.5.5 \
  minio/minio:RELEASE.2023-03-20T20-16-18Z \
  milvusdb/milvus:v2.4.0; do
  echo "  docker pull $img"
  docker pull "$img"
done

# 1d. 构建应用镜像
echo "--- 构建前端镜像 ---"
docker build -t ai-test-gen-web:latest -f frontend/Dockerfile .

echo "--- 构建后端应用镜像 ---"
docker build \
  --build-arg BASE_IMAGE=ai-test-gen-base:latest \
  -t ai-test-gen-api:latest \
  -f backend/Dockerfile .

# ==================================================================
# 2. 导出镜像为 .tar
# ==================================================================
echo ""
echo "=== [2/4] 导出镜像为 tar 文件 ==="

save_image() {
  local name="$1" tag="$2" filename="$3"
  echo "  保存 $name:$tag → images/$filename"
  docker save "$name:$tag" -o "$OUTPUT_DIR/images/$filename"
}

save_image "python"              "3.12-slim"    "01-python-3.12-slim.tar"
save_image "ai-test-gen-base"    "latest"       "02-ai-test-gen-base.tar"
save_image "ai-test-gen-postgres" "latest"      "03-ai-test-gen-pg.tar"
save_image "redis"               "7-alpine"     "04-redis-7-alpine.tar"
save_image "quay.io/coreos/etcd" "v3.5.5"       "05-etcd-v3.5.5.tar"
save_image "minio/minio"         "RELEASE.2023-03-20T20-16-18Z" "06-minio.tar"
save_image "milvusdb/milvus"     "v2.4.0"       "07-milvus-v2.4.0.tar"
save_image "node"                "20-alpine"    "08-node-20-alpine.tar"
save_image "nginx"               "1.27-alpine"  "09-nginx-1.27-alpine.tar"
save_image "ai-test-gen-web"     "latest"       "10-ai-test-gen-web.tar"
# 注意：后端镜像 (ai-test-gen-api) 基于 ai-test-gen-base 只加了一层 COPY，
# 内网直接用 base 镜像 + 源码 rebuild 即可，无需单独保存

# ==================================================================
# 3. 复制模型文件
# ==================================================================
echo ""
echo "=== [3/4] 复制模型文件 ==="
if [ -d models/bge-large-zh-v1.5 ] && [ -d models/bge-reranker-base ]; then
  echo "  复制 models/ → $OUTPUT_DIR/models/"
  # 排除 .gitattributes 等非模型文件
  rsync -a --exclude='.git*' --exclude='README.md' models/ "$OUTPUT_DIR/models/"
  echo "  模型大小: $(du -sh "$OUTPUT_DIR/models" | cut -f1)"
else
  echo "  ⚠️ 未找到 models/ 目录，请手动复制模型文件到 $OUTPUT_DIR/models/"
  echo "    需要: models/bge-large-zh-v1.5/ 和 models/bge-reranker-base/"
fi

# ==================================================================
# 4. 复制源码 + 部署脚本
# ==================================================================
echo ""
echo "=== [4/4] 复制源码和部署脚本 ==="

echo "  复制 backend/"
rsync -a --delete \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
  --exclude='*.egg-info' --exclude='.keys' \
  backend/ "$OUTPUT_DIR/code/backend/"

echo "  复制 frontend/"
rsync -a --delete \
  --exclude='node_modules' --exclude='dist' \
  frontend/ "$OUTPUT_DIR/code/frontend/"

echo "  复制 docker/"
rsync -a --delete docker/ "$OUTPUT_DIR/code/docker/"

echo "  复制根目录配置"
cp .dockerignore "$OUTPUT_DIR/code/" 2>/dev/null || true

echo "  复制部署文件"
cp docker-compose.offline.yml "$OUTPUT_DIR/"
cp scripts/deploy.sh "$OUTPUT_DIR/"
mkdir -p "$OUTPUT_DIR/scripts"
# apply-resource-limits.sh：deploy.sh 阶段 4-2 调用，按宿主资源生成
# docker-compose.resources.override.yml（容器内存/CPU 配额）。
# 漏打包的后果：部署时提示"未找到脚本，跳过" → 容器无上限 → OOM 保护失效（静默）。
cp scripts/apply-resource-limits.sh "$OUTPUT_DIR/scripts/" 2>/dev/null || {
  echo "  ❌ scripts/apply-resource-limits.sh 缺失（OOM 保护依赖它）"
  exit 1
}
chmod +x "$OUTPUT_DIR/deploy.sh" "$OUTPUT_DIR/scripts/apply-resource-limits.sh"

# 生成 install.env
cat > "$OUTPUT_DIR/install.env" << 'ENVEOF'
# AI-Test-Gen 离线部署配置
# 复制为 .env 或直接修改此文件
NGINX_PORT=80
API_PORT=8000
DEBUG=false
PG_PASSWORD=aitestgen123
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin

# ---------------------------------------------------------------------------
# 容器资源配额（v4.1.5，v4.1.7 补大内存档位）
# ---------------------------------------------------------------------------
# 留空/不写 = deploy.sh 按宿主内存与核数自动计算（推荐）：
#   8GB  宿主 → api 6g / api cpus 6     16GB  → api 10g / cpus 6
#   32GB       → api 20g / cpus 12      64GB  → api 32g / cpus 24
#   128GB      → api 64g / cpus 48      256GB → api 96g / cpus 72
#   v4.1.9 起 celery 已退役（向量化走出箱 relay），api 独占其余核数，
#   不再有 "api+celery 合计不得超过宿主核数" 的 3:1 重分逻辑。
# 设了上限，应用的 OOM 保护（内存紧张自动降并发）与并发预算才有依据；
# 不设 = 应用读到的是宿主内存，保护形同虚设（严重时被内核 OOM Killer 直接杀容器）。
#
# 只在自动计算不合预期时手工指定，格式：内存用 6g / 10240m，CPU 用整数或小数。
# API_MEM_LIMIT=6g
# API_CPU_LIMIT=6
# MILVUS_MEM_LIMIT=3g

# ---------------------------------------------------------------------------
# 生成并发与去重并行（v4.1.7，大机器必看）
# ---------------------------------------------------------------------------
# 【GEN_TASK_CONCURRENCY_MAX】全局同时进行的生成任务上限（硬顶）。
#   默认 4。注意它是**硬顶**：即使 96 核机器推导出 23 个并发，也会被它卡在 4。
#   建议值 ≈ 同时使用人数上浮 1~2：
#     10 人左右 → 8~10；20 人 → 16；单人 → 4（默认即可）
#   不要盲目设很大：并发生成任务都会去占本地 bge 推理，超过物理核后互相拖慢。
# GEN_TASK_CONCURRENCY_MAX=8
#
# 【DEDUP_EMBED_WORKERS】语义去重时的 Embedding 分片并行进程数。
#   ""/1/0/off = 串行（默认）；auto = max(1, 物理核-1)；整数 N = N 个进程。
#   每 worker 独立加载一份 bge 模型（约 1.6GB 内存）、4 线程。
#   推荐 N ≈ api cpus / 4（每进程 4 线程正好吃满配额）：
#     api 6 核 → N=1~2      api 12 核 → N=3      api 24 核 → N=6
#     api 72 核 → N=16~18   （16×4=64 线程，留余量给事件循环）
#   ⚠️ 两个硬约束：
#     ① 内存：N × 1.6GB 必须 < api 的 mem_limit，否则被 OOM Killer 杀
#     ② 分片门槛：单次去重的文本数 < N×2 时**自动回退串行**
#        （代码里 `len(texts) >= workers * 2`），所以 N 开太大对小批次无收益
#   实测参考：526 条用例串行约 35 分钟（单条 ~4 秒，见 DEDUP_SINGLE_CASE_SEC）。
# DEDUP_EMBED_WORKERS=auto
#
# 【DEDUP_EMBEDDING_MODEL_NAME】去重专用嵌入模型（可换轻量模型加速）。
#   留空 = 用全局 bge-large（1024 维，质量最好，最慢）。
#   改成 bge-base（768 维）/ bge-small（512 维）能显著提速，但**必须重新定标阈值**
#   （DEDUP_SEMANTIC_THRESHOLD 默认 0.92 是 bge-large 的定标值），
#   且已有向量缓存全部失效需重算。实测 small 在部分场景召回质量下降，谨慎使用。
# DEDUP_EMBEDDING_MODEL_NAME=/models/bge-base-zh-v1.5
ENVEOF

# ==================================================================
# 校验：检查关键文件是否完整
# ==================================================================
echo ""
echo "=== 校验离线包完整性 ==="

CHECK_FAILED=false
check_file() {
  if [ -f "$1" ]; then
    echo "  ✅ $1"
  else
    echo "  ❌ $1 缺失！"
    CHECK_FAILED=true
  fi
}
check_dir() {
  if [ -d "$1" ]; then
    echo "  ✅ $1/"
  else
    echo "  ❌ $1/ 缺失！"
    CHECK_FAILED=true
  fi
}

check_dir "$OUTPUT_DIR/code/frontend/src"
check_file "$OUTPUT_DIR/code/frontend/package.json"
check_file "$OUTPUT_DIR/code/frontend/package-lock.json"
check_file "$OUTPUT_DIR/code/frontend/index.html"
check_file "$OUTPUT_DIR/code/frontend/vite.config.ts"
check_file "$OUTPUT_DIR/code/frontend/tsconfig.json"
check_file "$OUTPUT_DIR/code/frontend/nginx.conf"
check_dir "$OUTPUT_DIR/code/backend/app"
check_file "$OUTPUT_DIR/code/backend/requirements.txt"
check_file "$OUTPUT_DIR/code/backend/Dockerfile"
check_file "$OUTPUT_DIR/code/docker/postgres.Dockerfile"
check_file "$OUTPUT_DIR/code/docker/init-zhparser.sql"
check_file "$OUTPUT_DIR/docker-compose.offline.yml"
check_file "$OUTPUT_DIR/deploy.sh"
check_file "$OUTPUT_DIR/scripts/apply-resource-limits.sh"
check_dir "$OUTPUT_DIR/images"
check_dir "$OUTPUT_DIR/models/bge-large-zh-v1.5"
check_file "$OUTPUT_DIR/models/bge-large-zh-v1.5/pytorch_model.bin"
check_file "$OUTPUT_DIR/install.env"

if [ "$CHECK_FAILED" = true ]; then
  echo ""
  echo "⚠️  ⚠️  ⚠️  离线包完整性检查未通过，请修复以上缺失文件！"
  exit 1
else
  echo ""
  echo "✅ 离线包完整性检查通过"
fi

# ==================================================================
# 打包
# ==================================================================
echo ""
echo "=== 打包完成 ==="
echo "输出目录: $OUTPUT_DIR"
echo ""
echo "目录结构:"
du -sh "$OUTPUT_DIR"/*
echo ""

if [ "$COMPRESS" != "--no-compress" ]; then
  echo "--- 压缩为 tar.gz (耗时较长) ---"
  cd "$(dirname "$OUTPUT_DIR")"
  ARCHIVE="ai-testgen-offline-pack-${VERSION}.tar.gz"
  tar czf "$ARCHIVE" "$(basename "$OUTPUT_DIR")"
  echo "  压缩包: $(cd "$(dirname "$OUTPUT_DIR")" && pwd)/$ARCHIVE"
  echo "  大小: $(du -h "$ARCHIVE" | cut -f1)"
fi

echo ""
echo "============================================"
echo " 下一步:"
echo "  将 $OUTPUT_DIR 拷贝到内网服务器"
echo "  执行: bash deploy.sh"
echo "============================================"