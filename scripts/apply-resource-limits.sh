#!/bin/bash
# ============================================================================
# apply-resource-limits.sh — 生成容器资源配额 override（内存上限 + CPU 配额）
#
# 为什么需要独立脚本 / 独立 override 文件：
#   应用的 OOM 保护（MEM_LOW_RATIO → 自动降并发）与并发预算（task_ceiling）
#   都读 **容器自己的 cgroup 上限**。容器没设上限时 cgroup 是 "max"，
#   守卫只能读宿主机内存 → 判定与实际风险不一致 → 保护等于没有。
#
#   而 docker-compose 的 mem_limit / cpus **不接受空串**（invalid size: '' /
#   ParseFloat 失败），无法用 `${API_MEM_LIMIT:-}` 这种"留空即不限"的写法；
#   用注释掉的行又会让"到底有没有生效"变成需要人肉确认的事。
#   → 折中：模板 compose 保持"不限"的旧行为，本脚本在**算出具体值**时
#     才生成 docker-compose.resources.override.yml，用它叠加启动。
#
# 3.10 / 4.x 兼容：
#   - 只写 mem_limit / cpus 这类 Docker 侧字段，与内核版本无关；
#     Docker 自己会落到 cgroup v1（3.10）或 v2（4.x ≈ 4.0+ 可选、5.8+ 默认）。
#   - 应用侧两条分支都已支持（resource_guard 的 cgroup_v1/v2 探测）。
#
# 用法:
#   bash scripts/apply-resource-limits.sh                # 自动按宿主资源计算
#   API_MEM_LIMIT=6g API_CPU_LIMIT=4 bash scripts/apply-resource-limits.sh  # 手工指定
#   bash scripts/apply-resource-limits.sh --dry-run      # 只看会生成什么，不落盘
#   bash scripts/apply-resource-limits.sh --remove       # 删除 override（回到不限）
#
# 可覆盖的环境变量:
#   API_MEM_LIMIT / API_CPU_LIMIT / MILVUS_MEM_LIMIT / CELERY_MEM_LIMIT / CELERY_CPU_LIMIT
#   （CELERY_MEM_LIMIT 不设时按 API_MEM_LIMIT 的档位自动折半；
#     CELERY_CPU_LIMIT 不设时取 API_CPU_LIMIT 的一半，下限 1）
#
# 生成后启动方式（deploy.sh 已内置，手工部署时用）:
#   docker compose -f docker-compose.offline.yml -f docker-compose.resources.override.yml up -d
# ============================================================================
set -euo pipefail

DRY_RUN=false
REMOVE=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --remove)  REMOVE=true ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $arg（支持 --dry-run / --remove / --help）" >&2; exit 2 ;;
  esac
done

# 定位脚本所在目录（兼容被 deploy.sh source 或从别处调用）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_FILE="$PACK_DIR/docker-compose.resources.override.yml"

info() { echo "[resources] $*"; }
warn() { echo "[resources][WARN] $*" >&2; }

if [ "$REMOVE" = true ]; then
  if [ -f "$OUT_FILE" ]; then
    rm -f "$OUT_FILE"
    info "已删除 $OUT_FILE（容器配额回到「不限」）"
  else
    info "override 文件不存在，无需删除"
  fi
  exit 0
fi

# ---------------------------------------------------------------------------
# 1. 探测宿主内存（MB）
# ---------------------------------------------------------------------------
HOST_MEM_MB=0
if [ -r /proc/meminfo ]; then
  HOST_MEM_MB=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
fi
if [ "${HOST_MEM_MB:-0}" -le 0 ] && command -v sysctl &>/dev/null; then
  # macOS 兜底
  HOST_MEM_MB=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1024 / 1024 ))
fi

# ---------------------------------------------------------------------------
# 2. 探测宿主逻辑核
# ---------------------------------------------------------------------------
HOST_CPU=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 0)

# ---------------------------------------------------------------------------
# 3. 自动计算（未显式指定时）
# ---------------------------------------------------------------------------
# 内存：宿主 60%~65%，并按档位取整，给 Milvus（4G 起）+ bge 模型（~2G）留余量。
#   为什么不按"宿主 - 固定预留"：预留值随数据量变化，固定值会在小内存机器上
#   算成负数、在大内存机器上算得过松。分档更可预测。
if [ -z "${API_MEM_LIMIT:-}" ] && [ "${HOST_MEM_MB:-0}" -gt 0 ]; then
  if   [ "$HOST_MEM_MB" -lt 7000 ];  then API_MEM_LIMIT="4g";  warn "宿主内存 ${HOST_MEM_MB}MB < 7GB，api 限 4g 可能偏紧"
  elif [ "$HOST_MEM_MB" -lt 12000 ]; then API_MEM_LIMIT="6g"
  elif [ "$HOST_MEM_MB" -lt 24000 ]; then API_MEM_LIMIT="10g"
  elif [ "$HOST_MEM_MB" -lt 48000 ]; then API_MEM_LIMIT="20g"
  else                                    API_MEM_LIMIT="32g"
  fi
fi

if [ -z "${MILVUS_MEM_LIMIT:-}" ] && [ "${HOST_MEM_MB:-0}" -gt 0 ]; then
  if   [ "$HOST_MEM_MB" -lt 7000 ];  then MILVUS_MEM_LIMIT="3g"
  elif [ "$HOST_MEM_MB" -lt 12000 ]; then MILVUS_MEM_LIMIT="4g"
  elif [ "$HOST_MEM_MB" -lt 24000 ]; then MILVUS_MEM_LIMIT="6g"
  else                                    MILVUS_MEM_LIMIT="8g"
  fi
fi

# CPU：api 最多给 宿主核数-2（留 2 核给 Milvus/PG/系统），下限 2。
#   不设 cpus 时应用看得到全部核 → task_ceiling 按宿主核数放大，
#   32 核机器会一次放 4 个生成任务并发（GEN_TASK_CONCURRENCY_MAX=4），把 bge 推理挤爆。
if [ -z "${API_CPU_LIMIT:-}" ] && [ "${HOST_CPU:-0}" -gt 0 ]; then
  API_CPU_LIMIT=$(( HOST_CPU > 4 ? HOST_CPU - 2 : 2 ))
fi

# celery_worker 的 CPU：与 api 同理，必须受限。
#   它跑 embedding + 知识库双写，每 worker 进程各有一份 torch 线程池（4 线程）。
#   不设 cpus 时它与 api **争同一批物理核**，且应用侧的并发预算完全看不到它
#   （它不在 api 进程里，task_ceiling 只管 api 自己的生成任务）。
#   实测形态：api 被限 6 核、celery 看到 8 核 → 两边合计最多可开 6+（2×4）线程池，
#   彻底超过宿主 8 逻辑核的容量。
#   取 api 的一半（下限 1）：celery 是异步旁路，慢一点可接受，抢核会直接拖垮生成。
if [ -z "${CELERY_CPU_LIMIT:-}" ] && [ -n "${API_CPU_LIMIT:-}" ]; then
  CELERY_CPU_LIMIT=$(( API_CPU_LIMIT / 2 ))
  [ "$CELERY_CPU_LIMIT" -lt 1 ] && CELERY_CPU_LIMIT=1
fi

# ---------------------------------------------------------------------------
# 4. 生成 override
# ---------------------------------------------------------------------------
if [ -z "${API_MEM_LIMIT:-}" ]; then
  warn "无法探测宿主内存，且未显式指定 API_MEM_LIMIT → 不生成 override（保持不限）"
  exit 0
fi

API_CPU_BLOCK=""
if [ -n "${API_CPU_LIMIT:-}" ]; then
  API_CPU_BLOCK="    cpus: \"${API_CPU_LIMIT}\""
else
  API_CPU_BLOCK="    # cpus: 未设置（应用将看到宿主全部核）"
fi

MILVUS_BLOCK=""
if [ -n "${MILVUS_MEM_LIMIT:-}" ]; then
  MILVUS_BLOCK="  milvus:
    mem_limit: ${MILVUS_MEM_LIMIT}"
else
  MILVUS_BLOCK="  # milvus: 未设置内存上限"
fi

# celery_worker：与 backend 共用同一镜像、各自加载一份 bge（~1.6GB）+ reranker，
# 但它是**独立进程**，没有 backend 那种 MEM_LOW_RATIO 降并发守卫。
# 不限额时它和 backend 抢内存，且内核 OOM Killer 挑哪个全看运气
# （3.10 的 memcg 策略偏好杀 PID 最大者，往往正好是它）。
# 取 backend 的一半：它只跑嵌入/双写，不跑生成链路。
CELERY_MEM_LIMIT="${CELERY_MEM_LIMIT:-}"
if [ -z "$CELERY_MEM_LIMIT" ] && [ -n "${API_MEM_LIMIT:-}" ]; then
  case "$API_MEM_LIMIT" in
    6g)  CELERY_MEM_LIMIT="3g" ;;
    10g) CELERY_MEM_LIMIT="4g" ;;
    20g) CELERY_MEM_LIMIT="6g" ;;
    32g) CELERY_MEM_LIMIT="8g" ;;
    4g)  CELERY_MEM_LIMIT="2g" ;;
    *)   CELERY_MEM_LIMIT="$API_MEM_LIMIT" ;;  # 自定义值时保守取同值
  esac
fi

CELERY_BLOCK=""
if [ -n "$CELERY_MEM_LIMIT" ]; then
  CELERY_BLOCK="  celery_worker:
    mem_limit: ${CELERY_MEM_LIMIT}"
else
  CELERY_BLOCK="  # celery_worker: 未设置内存上限"
fi
# CPU 配额接在同一个 celery 块里（块已存在就追加，不存在则新建块）
if [ -n "${CELERY_CPU_LIMIT:-}" ]; then
  if printf '%s' "$CELERY_BLOCK" | grep -q "^  celery_worker:"; then
    CELERY_BLOCK="${CELERY_BLOCK}
    cpus: \"${CELERY_CPU_LIMIT}\""
  else
    CELERY_BLOCK="  celery_worker:
    # mem_limit 未设置，但 CPU 必须限（否则与 api 争物理核）
    cpus: \"${CELERY_CPU_LIMIT}\""
  fi
fi

CONTENT="# ============================================================================
# docker-compose.resources.override.yml — 【自动生成，勿手工编辑】
#
# 生成者: scripts/apply-resource-limits.sh
# 宿主:   ${HOST_MEM_MB}MB 内存 / ${HOST_CPU} 逻辑核
# 作用:   给 backend / milvus 声明 cgroup 上限，让应用的 OOM 保护与并发预算
#         有真实依据（否则读到的是宿主内存 → 保护失效）。
#
# 生效方式（必须叠加在主 compose 之后，顺序不能反）:
#   docker compose -f docker-compose.offline.yml \\
#                  -f docker-compose.resources.override.yml up -d
#
# 删除本文件 = 回到「不限内存/CPU」的旧行为。
# 重新计算: bash scripts/apply-resource-limits.sh
# ============================================================================
services:
  backend:
    mem_limit: ${API_MEM_LIMIT}
${API_CPU_BLOCK}
${CELERY_BLOCK}
${MILVUS_BLOCK}
"

if [ "$DRY_RUN" = true ]; then
  echo "----- $OUT_FILE (dry-run，未写入) -----"
  echo "$CONTENT"
  exit 0
fi

printf '%s' "$CONTENT" > "$OUT_FILE"
info "已生成 $OUT_FILE"
info "  backend.mem_limit      = ${API_MEM_LIMIT}   backend.cpus = ${API_CPU_LIMIT:-未设}"
info "  celery_worker.mem_limit = ${CELERY_MEM_LIMIT:-未设}   celery_worker.cpus = ${CELERY_CPU_LIMIT:-未设}"
info "  milvus.mem_limit        = ${MILVUS_MEM_LIMIT:-未设}"

# 语法自检：叠加后必须能被 compose 解析（失败就别让部署继续）
if command -v docker &>/dev/null && docker compose version &>/dev/null; then
  if docker compose -f "$PACK_DIR/docker-compose.offline.yml" -f "$OUT_FILE" config --quiet 2>/dev/null; then
    info "  ✅ 叠加后 compose 配置校验通过"
  else
    warn "  ❌ 叠加后 compose 配置校验失败，请检查 $OUT_FILE"
    exit 1
  fi
fi
