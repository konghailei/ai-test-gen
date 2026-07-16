#!/bin/bash
# ============================================================================
# AI-Test-Gen 内网环境检测脚本
#
# 用法: bash scripts/check-env.sh
#
# 在你准备部署的内网服务器上运行此脚本，
# 检查 Docker / Compose / 磁盘 / 内存 是否符合要求。
# 无需任何离线包，只需要 shell。
# ============================================================================

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

PASS=0; FAIL=0; WARN=0
pass() { PASS=$((PASS+1)); info "  ✅ $1"; }
fail() { FAIL=$((FAIL+1)); error "  ❌ $1"; }
warnc() { WARN=$((WARN+1)); warn "  ⚠️  $1"; }

echo ""
echo "============================================"
echo " AI-Test-Gen 内网环境检测"
echo "============================================"
echo ""

# ============================
# 1. Docker Engine
# ============================
echo "--- 1. Docker Engine ---"
if ! command -v docker &>/dev/null; then
  fail "Docker 未安装"
else
  ver=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
  if [ "$ver" = "unknown" ]; then
    ver=$(docker --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+')
  fi
  info "    版本: $ver"
  if echo "$ver" | grep -qE '^[2-9][0-9]\.'; then
    pass "Docker Engine >= 20.10"
  elif echo "$ver" | grep -qE '^(1[8-9]\.|2[0-9]\.)'; then
    warnc "Docker Engine $ver，建议升级到 20.10+"
  else
    warnc "Docker Engine $ver，版本偏低"
  fi
fi

# ============================
# 2. Docker Compose
# ============================
echo ""
echo "--- 2. Docker Compose ---"
COMPOSE_OK=false
if docker compose version &>/dev/null; then
  cver=$(docker compose version 2>/dev/null)
  info "    版本: $cver"
  pass "docker compose (v2) 可用"
  COMPOSE_OK=true
elif command -v docker-compose &>/dev/null; then
  cver=$(docker-compose --version 2>/dev/null)
  warnc "检测到 docker-compose v1，本项目需要 V2: $cver"
else
  fail "Docker Compose 未安装"
fi

# ============================
# 3. BuildKit
# ============================
echo ""
echo "--- 3. BuildKit (多阶段构建) ---"
if docker buildx version &>/dev/null; then
  bver=$(docker buildx version 2>/dev/null | head -1 | grep -oP 'v?\S+\.\S+')
  pass "BuildKit $bver 可用"
else
  warnc "BuildKit 不可用，前端多阶段构建将失败"
fi

# ============================
# 4. OS / 架构
# ============================
echo ""
echo "--- 4. 操作系统 ---"
OS="$(uname -s)"
ARCH="$(uname -m)"
info "    $OS / $ARCH"
if [ "$OS" = "Linux" ]; then
  KERNEL=$(uname -r | grep -oP '^\d+\.\d+')
  info "    Kernel: $(uname -r)"
  if [ "$(echo "$KERNEL" | cut -d. -f1)" -ge 4 ]; then
    pass "Linux kernel >= 4.0"
  else
    warnc "Kernel $KERNEL < 4.0"
  fi
  # 检查 cgroup 版本
  if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    pass "cgroup v2 (推荐)"
  else
    info "    cgroup v1"
  fi
elif [ "$OS" = "Darwin" ]; then
  pass "macOS 支持 (Docker Desktop)"
else
  warnc "非 Linux/macOS 系统，请确认 Docker 可用"
fi

# ============================
# 5. 内存
# ============================
echo ""
echo "--- 5. 内存 ---"
if [ -r /proc/meminfo ]; then
  MEM_KB=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}')
  MEM_GB=$((MEM_KB / 1024 / 1024))
  info "    ${MEM_GB}GB"
  if [ "$MEM_GB" -ge 16 ]; then
    pass "内存 >= 16GB (推荐)"
  elif [ "$MEM_GB" -ge 8 ]; then
    warnc "内存 ${MEM_GB}GB, 满足最低要求 8GB, 推荐 16GB"
  else
    fail "内存 ${MEM_GB}GB < 8GB, Milvus 启动可能失败"
  fi
elif command -v free &>/dev/null; then
  MEM_GB=$(free -g 2>/dev/null | awk '/^Mem:/{print $2}')
  info "    ${MEM_GB}GB (估算)"
  if [ "$MEM_GB" -lt 8 ]; then
    fail "内存不足"
  fi
else
  warnc "无法检测内存"
fi

# ============================
# 6. 磁盘
# ============================
echo ""
echo "--- 6. 当前目录磁盘空间 ---"
CUR_DIR="$(pwd)"
AVAIL_KB=$(df "$CUR_DIR" 2>/dev/null | awk 'NR>1{print $4}')
if [ -n "$AVAIL_KB" ]; then
  AVAIL_GB=$((AVAIL_KB / 1024 / 1024))
  info "    ${AVAIL_GB}GB 可用 (挂载点: $(df "$CUR_DIR" 2>/dev/null | awk 'NR>1{print $1}'))"
  if [ "$AVAIL_GB" -ge 30 ]; then
    pass "磁盘剩余 >= 30GB (满足首次部署)"
  elif [ "$AVAIL_GB" -ge 10 ]; then
    warnc "磁盘剩余 ${AVAIL_GB}GB, 首次部署建议 30GB+, 增量更新勉强够用"
  else
    fail "磁盘剩余 ${AVAIL_GB}GB < 10GB, 空间不足"
  fi
else
  warnc "无法检测磁盘空间"
fi

# ============================
# 7. docker info 健康检查
# ============================
echo ""
echo "--- 7. Docker Daemon 状态 ---"
docker info --format 'Containers: {{.Containers}} / Running: {{.ContainersRunning}}' 2>/dev/null && pass "Docker Daemon 运行正常" || fail "Docker Daemon 未运行"

# ============================
# 汇总
# ============================
echo ""
echo "============================================"
echo " 检测结果汇总"
echo "============================================"
echo "  ✅ 通过: $PASS"
echo "  ⚠️  告警: $WARN"
echo "  ❌ 失败: $FAIL"
echo ""

if [ "$FAIL" -gt 0 ]; then
  error "$FAIL 项检查未通过，请修复后重试"
  exit 1
elif [ "$WARN" -gt 0 ]; then
  warn "$WARN 项告警，建议优化但通常可部署"
  exit 0
else
  info "全部通过！环境符合部署要求 ✅"
  exit 0
fi