#!/bin/bash
# ============================================================================
# pack-update.sh — 增量更新打包脚本
#
# 用法: bash scripts/pack-update.sh <版本号> [--no-compress]
#   例: bash scripts/pack-update.sh v1.7.1
#
# 说明:
#   只打包变更的源码 + 部署脚本，不含基础镜像和模型文件。
#   内网收到后，deploy.sh 会用已有 base 镜像缓存重建应用层。
#   包大小通常 ~5MB，远小于首次的 ~6.5GB。
# ============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# ---------- 参数 ----------
VERSION="${1:-}"
[ -z "$VERSION" ] && { echo "用法: bash scripts/pack-update.sh <版本号>"; exit 1; }
COMPRESS="${2:-}"
OUTPUT_DIR="../ai-testgen-offline-pack-update-${VERSION}"
OUTPUT_DIR="$(cd "$(dirname "$OUTPUT_DIR")" && pwd)/$(basename "$OUTPUT_DIR")"

echo "============================================"
echo " AI-Test-Gen 离线包打包脚本 (增量更新)"
echo " 版本: $VERSION"
echo " 输出: $OUTPUT_DIR"
echo "============================================"

# ---------- 清理 ----------
rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR/code"

# ==================================================================
# 1. 复制源码（只含变更，不含 node_modules / __pycache__）
# ==================================================================
echo ""
echo "=== [1/2] 复制源码 ==="

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

# ==================================================================
# 2. 构建并导出前端镜像（内网无 npm，必须预构建）
# ==================================================================
echo ""
echo "=== [2/3] 构建前端镜像 ==="
echo "  构建前端镜像 (ai-test-gen-web:latest)..."
if docker build -t ai-test-gen-web:latest -f frontend/Dockerfile . 2>/dev/null; then
  echo "  保存前端镜像 → 10-ai-test-gen-web.tar"
  mkdir -p "$OUTPUT_DIR/images"
  docker save ai-test-gen-web:latest -o "$OUTPUT_DIR/images/10-ai-test-gen-web.tar"
  echo "  镜像大小: $(du -h "$OUTPUT_DIR/images/10-ai-test-gen-web.tar" | cut -f1)"
else
  echo "  ⚠️ 前端构建失败（可能是 npm 源不可达），跳过镜像导出"
  echo "    内网部署时将使用已有的前端镜像"
fi

# ==================================================================
# 2. 复制部署文件
# ==================================================================
echo ""
echo "=== [2/2] 复制部署文件 ==="

cp docker-compose.offline.yml "$OUTPUT_DIR/"
cp scripts/deploy.sh "$OUTPUT_DIR/"
mkdir -p "$OUTPUT_DIR/scripts"
# 增量包同样必须带 apply-resource-limits.sh：
# deploy.sh 每次部署（含增量）都会重算配额 override，漏了它容器会退回无上限（OOM 保护失效）。
cp scripts/apply-resource-limits.sh "$OUTPUT_DIR/scripts/" 2>/dev/null || {
  echo "  ❌ scripts/apply-resource-limits.sh 缺失（OOM 保护依赖它）"
  exit 1
}
chmod +x "$OUTPUT_DIR/deploy.sh" "$OUTPUT_DIR/scripts/apply-resource-limits.sh"

# 复制 install.env 如果存在
if [ -f "$OUTPUT_DIR/../install.env" ]; then
  cp "$OUTPUT_DIR/../install.env" "$OUTPUT_DIR/" 2>/dev/null || true
fi

# 记录版本信息
echo "$VERSION" > "$OUTPUT_DIR/VERSION"
echo "$(date '+%Y-%m-%d %H:%M:%S')" >> "$OUTPUT_DIR/VERSION"

# 版本一致性校验：包名版本必须与代码内 VERSION 一致，
# 否则内网部署出来的实际版本和包的标称版本不符（deploy.sh 会报"包版本与代码版本不一致"）。
CODE_VER=$(head -1 backend/app/VERSION 2>/dev/null | tr -d '\r\n')
FRONT_VER=$(grep -m1 '"version"' frontend/package.json 2>/dev/null | sed 's/.*: *"\([^"]*\)".*/\1/')
echo ""
echo "=== 版本一致性校验 ==="
echo "  包版本      : $VERSION"
echo "  后端 VERSION: ${CODE_VER:-<缺失>}"
echo "  前端 version: ${FRONT_VER:-<缺失>}"
STRIP_VER="${VERSION#v}"
if [ "$STRIP_VER" != "$CODE_VER" ] || [ "$STRIP_VER" != "$FRONT_VER" ]; then
  echo "  ⚠️ 版本不一致：请先把 backend/app/VERSION 与 frontend/package.json 改成 $STRIP_VER"
  echo "     （继续打包也能用，但内网会看到版本号对不上）"
fi
echo ""

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
  echo "--- 压缩为 tar.gz ---"
  cd "$(dirname "$OUTPUT_DIR")"
  ARCHIVE="ai-testgen-offline-pack-update-${VERSION}.tar.gz"
  tar czf "$ARCHIVE" "$(basename "$OUTPUT_DIR")"
  echo "  压缩包: $(cd "$(dirname "$OUTPUT_DIR")" && pwd)/$ARCHIVE"
  echo "  大小: $(du -h "$ARCHIVE" | cut -f1)"
fi

echo ""
echo "============================================"
echo " 下一步:"
echo "  将 $OUTPUT_DIR 拷贝到内网服务器"
echo "  在已有安装目录执行: bash deploy.sh"
echo "============================================"