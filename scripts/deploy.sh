#!/bin/bash
# ============================================================================
# deploy.sh — 内网一键部署/更新脚本
#
# 用法:
#   首次部署:  bash deploy.sh
#   增量更新:  bash deploy.sh
#   指定目录:  INSTALL_DIR=/opt/ai-testgen bash deploy.sh
#   指定配置:  bash deploy.sh --env /path/to/install.env
#
# 功能:
#   - 加载 images/*.tar（首次部署）
#   - 复制 models/ 文件（首次部署）
#   - 覆盖源码并重建应用镜像（增量更新，利用已有 base 缓存）
#   - 创建数据目录并启动所有服务
#
# 兼容性:
#   支持首次完整包 和 增量更新包，自动检测并执行对应逻辑
# ============================================================================
set -euo pipefail

# ---------- 颜色输出 ----------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ---------- 解析参数 ----------
ENV_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --env) ENV_FILE="$2"; shift 2 ;;
    --help) echo "用法: bash deploy.sh [--env <env_file>]"; exit 0 ;;
    *) break ;;
  esac
done

# ---------- 确定安装目录 ----------
# 脚本所在目录即为离线包目录
PACK_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PACK_DIR"

# 如果指定了 INSTALL_DIR，则复制到目标目录
INSTALL_DIR="${INSTALL_DIR:-$PACK_DIR}"
if [ "$INSTALL_DIR" != "$PACK_DIR" ]; then
  info "安装到: $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  # 复制除 images/ models/ 之外的所有内容
  rsync -a --exclude='images/' --exclude='models/' "$PACK_DIR/" "$INSTALL_DIR/"
  cd "$INSTALL_DIR"
else
  info "安装目录: $INSTALL_DIR"
fi

# ---------- 加载配置 ----------
if [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ]; then
  info "加载配置: $ENV_FILE"
  set -a; source "$ENV_FILE"; set +a
elif [ -f install.env ]; then
  set -a; source install.env; set +a
elif [ -f .env ]; then
  set -a; source .env; set +a
fi

# ==================================================================
# 阶段 1: 加载基础镜像（仅首次部署有 images/ 目录）
# ==================================================================
if [ -d images ] && [ "$(ls -A images/ 2>/dev/null)" ]; then
  info "========================================"
  info "检测到 images/ 目录，执行首次镜像加载"
  info "========================================"
  
  IMAGE_COUNT=$(ls images/*.tar 2>/dev/null | wc -l)
  info "加载 $IMAGE_COUNT 个镜像..."
  
  for tar in images/*.tar; do
    name=$(basename "$tar")
    echo -n "  → $name ... "
    load_output=$(docker load -i "$tar" 2>&1)
    echo "done"
  done
  
  info "镜像加载完成"
  echo ""
else
  info "无 images/ 目录，跳过镜像加载（增量更新场景）"
  echo ""
fi

# ==================================================================
# 阶段 2: 部署模型文件（仅首次部署有 models/ 目录）
# ==================================================================
if [ -d models ] && [ "$(ls -A models/ 2>/dev/null)" ]; then
  info "========================================"
  info "检测到 models/ 目录，部署模型文件"
  info "========================================"
  
  # 检查目标路径是否存在
  TARGET_MODELS="models"
  mkdir -p "$TARGET_MODELS"
  
  if [ -f "$TARGET_MODELS/bge-large-zh-v1.5/pytorch_model.bin" ]; then
    warn "模型已存在，跳过复制（如需覆盖请手动删除）"
  else
    info "复制模型文件..."
    rsync -a --progress models/ "$TARGET_MODELS/"
    info "模型大小: $(du -sh "$TARGET_MODELS" | cut -f1)"
  fi
  echo ""
fi

# ==================================================================
# 阶段 3: 部署/更新源码并重建应用镜像
# ==================================================================
if [ -d code ] && [ "$(ls -A code/ 2>/dev/null)" ]; then
  info "========================================"
  info "检测到 code/ 目录，更新源码并重建应用镜像"
  info "========================================"
  
  # 3a. 复制后端源码
  if [ -d code/backend ]; then
    info "更新 backend/ 源码..."
    mkdir -p backend
    rsync -a --delete \
      --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
      code/backend/ backend/
  fi
  
  # 3b. 复制前端源码
  if [ -d code/frontend ]; then
    info "更新 frontend/ 源码..."
    mkdir -p frontend
    rsync -a --delete \
      --exclude='node_modules' --exclude='dist' \
      code/frontend/ frontend/
  fi
  
  # 3c. 复制 docker/ 配置
  if [ -d code/docker ]; then
    info "更新 docker/ 配置..."
    mkdir -p docker
    rsync -a --delete code/docker/ docker/
  fi
  
  # 3d. 复制根目录配置
  [ -f code/.dockerignore ] && cp code/.dockerignore . 2>/dev/null || true
  
  # 3e. 重建应用镜像（利用已有 base 镜像缓存，只产生新 CODE 层）
  if command -v docker compose &>/dev/null; then
    COMPOSE_CMD="docker compose"
  else
    COMPOSE_CMD="docker-compose"
  fi
  
  info "重建后端镜像 (基于 ai-test-gen-base:latest 缓存)..."
  $COMPOSE_CMD -f docker-compose.offline.yml build backend
  
  info "重建前端镜像..."
  $COMPOSE_CMD -f docker-compose.offline.yml build frontend
  
  info "应用镜像重建完成"
  echo ""
fi

# ==================================================================
# 阶段 4: 准备数据目录
# ==================================================================
info "========================================"
info "准备持久化数据目录"
info "========================================"

mkdir -p \
  data/pgdata \
  data/redis \
  milvus/etcd \
  milvus/minio \
  milvus/milvus \
  logs

info "数据目录已就绪"

# ==================================================================
# 阶段 5: 启动服务
# ==================================================================
echo ""
info "========================================"
info "启动所有服务"
info "========================================"

if command -v docker compose &>/dev/null; then
  COMPOSE_CMD="docker compose"
else
  COMPOSE_CMD="docker-compose"
fi

info "执行: $COMPOSE_CMD -f docker-compose.offline.yml up -d"
$COMPOSE_CMD -f docker-compose.offline.yml up -d

# ==================================================================
# 阶段 6: 检查启动状态
# ==================================================================
echo ""
info "========================================"
info "检查服务健康状态"
info "========================================"

sleep 5

# 检查各服务
services=("ai-test-gen-pg" "ai-test-gen-redis" "ai-test-gen-etcd" "ai-test-gen-minio" "ai-test-gen-milvus" "ai-test-gen-api" "ai-test-gen-web" "ai-test-gen-celery")
all_healthy=true

for svc in "${services[@]}"; do
  status=$(docker inspect --format='{{.State.Health.Status}}' "$svc" 2>/dev/null || echo "not_found")
  if [ "$status" = "healthy" ]; then
    info "  ✅ $svc: healthy"
  elif [ "$status" = "not_found" ]; then
    warn "  ⚠️  $svc: 未找到（可能服务名不同）"
    all_healthy=false
  else
    warn "  ⏳ $svc: $status（启动中，稍后可用 docker compose ps 查看）"
    all_healthy=false
  fi
done

# ==================================================================
# 完成
# ==================================================================
echo ""
info "========================================"
info "部署完成！"
info "========================================"
echo ""
echo "  前端地址:  http://localhost:${NGINX_PORT:-80}"
echo "  API 地址:  http://localhost:${API_PORT:-8000}"
echo ""
echo "  常用命令:"
echo "    查看日志:  $COMPOSE_CMD -f docker-compose.offline.yml logs -f"
echo "    重启服务:  $COMPOSE_CMD -f docker-compose.offline.yml restart"
echo "    停止服务:  $COMPOSE_CMD -f docker-compose.offline.yml down"
echo ""

if [ "$all_healthy" = false ]; then
  echo -e "${YELLOW}  提示: 部分服务尚未就绪，等待 1-2 分钟后运行以下命令检查:${NC}"
  echo "    $COMPOSE_CMD -f docker-compose.offline.yml ps"
fi