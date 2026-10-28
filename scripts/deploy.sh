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

# ---------- 辅助函数 ----------
# 判断是否是首次部署（有 images/ 目录）
# 两个位置都看：默认模式在包目录；INSTALL_DIR 分离模式下首次包的 images/ 留在包目录。
is_first_deploy() {
  [ -n "$(ls -A "$PACK_DIR/images" 2>/dev/null)" ] \
    || { [ "$WORK_DIR" != "$PACK_DIR" ] && [ -n "$(ls -A "$WORK_DIR/images" 2>/dev/null)" ]; }
}

# 智能合并 .env 配置文件：新增变量不覆盖已有值
# 用法: merge_env_config <模板文件> <目标文件>
#   - 模板中已有的 KEY=VALUE 但目标已存在 → 保留目标值（不覆盖）
#   - 模板中有而目标中没有的变量 → 追加到目标末尾
#   - 注释行和空行保持原样
merge_env_config() {
  local template="$1"
  local target="$2"
  local tmpfile

  [ ! -f "$template" ] && return 0
  [ ! -f "$target" ] && { cp "$template" "$target"; return 0; }

  tmpfile=$(mktemp)
  # 读取目标文件已有 KEY 集合
  local -A existing_keys
  while IFS='=' read -r key rest; do
    # 跳过注释和空行
    [[ "$key" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$key" ]] && continue
    key="${key%%[[:space:]]*}"
    existing_keys["$key"]=1
  done < "$target"

  # 写入目标文件副本
  cp "$target" "$tmpfile"

  # 遍历模板，追加新变量
  local added=0
  while IFS= read -r line; do
    # 提取 KEY
    local key="${line%%=*}"
    key="${key%%[[:space:]]*}"
    # 跳过注释、空行、已存在变量
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "$line" ]] && continue
    if [[ -z "${existing_keys[$key]:-}" ]]; then
      echo "$line" >> "$tmpfile"
      existing_keys["$key"]=1
      added=$((added + 1))
    fi
  done < "$template"

  if [ "$added" -gt 0 ]; then
    cp "$tmpfile" "$target"
    info "配置合并: ${target} 新增了 ${added} 个变量（已有值保持不变）"
  else
    info "配置合并: ${target} 无新增变量"
  fi
  rm -f "$tmpfile"
}

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
# 脚本所在目录即为**离线包目录**。但有两种部署布局必须都支持：
#   ① 包布局（pack-first.sh / pack-update.sh 生成）：deploy.sh 在包**根目录**，
#      与 docker-compose.offline.yml 同级；
#   ② 源码仓库布局：deploy.sh 在 `scripts/` 子目录，compose 在**上一级**。
# 老实现只认 ①，于是从仓库里跑 `bash scripts/deploy.sh` 时 PACK_DIR 会变成
# `.../scripts`，接着 `find scripts/apply-resource-limits.sh` 找不到、
# `images/` 找不到、compose 也找不到——全部静默跳过，最后 up 一个空配置。
# 判据用"同级有没有 compose 文件"，比猜目录名可靠（也兼容被改名/软链的部署）。
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/docker-compose.offline.yml" ]; then
  PACK_DIR="$SCRIPT_DIR"
elif [ -f "$SCRIPT_DIR/../docker-compose.offline.yml" ]; then
  PACK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
else
  # 两种都没有：按包布局处理，后续会在阶段 5 的 compose 校验里明确报错
  PACK_DIR="$SCRIPT_DIR"
  warn "未在 $(basename "$SCRIPT_DIR") 或其上级找到 docker-compose.offline.yml"
  warn "  将按包布局继续，启动阶段会明确失败（不会静默启动空配置）"
fi
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

# 统一路径基准（重要）：
#   cd 之后"当前目录"才是部署目标（INSTALL_DIR），而 PACK_DIR 可能只是解包目录。
#   下面的阶段 3/4-2 之前混用了两者的路径：
#     · 阶段 4-2 用 `$PACK_DIR/scripts/...` 找脚本 → 脚本没复制进 INSTALL_DIR 时直接跳过
#     · 阶段 3 用相对路径 `images/...`，而上面 rsync **明确排除了 images/**
#   结果：只要用 INSTALL_DIR 分离部署，配额生成与前端镜像更新就静默失效。
#   统一改用 WORK_DIR（= cd 后的当前目录）做所有运行期路径基准。
WORK_DIR="$(pwd)"

# ---------- 加载配置 ----------
if [ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ]; then
  info "加载配置: $ENV_FILE"
  set -a; source "$ENV_FILE"; set +a
elif [ -f install.env ]; then
  set -a; source install.env; set +a
elif [ -f .env ]; then
  set -a; source .env; set +a
fi

# ---------- 版本识别与降级保护 ----------
# 为什么需要：内网迁移是"人工拷包 + 手工执行"，最怕三件事——
#   ① 包和安装目录不是同一版本（拷错包/解压到旧目录）
#   ② 用旧包覆盖新版本（回退却不知道）
#   ③ 事后无法确认"这台机器现在跑的是什么版本"
# 这里把三方版本号打出来并落盘，事后可查：
#   · 包版本   = 包内 VERSION 文件（pack-*.sh 写入）
#   · 已装版本 = 安装目录 .deployed-version（本脚本上次部署写入）
#   · 代码版本 = backend/app/VERSION（镜像里烘焙的真实版本）
PKG_VERSION=""
[ -f "$WORK_DIR/VERSION" ] && PKG_VERSION=$(head -1 "$WORK_DIR/VERSION" 2>/dev/null | tr -d '\r\n')
CODE_VERSION=""
[ -f "$WORK_DIR/backend/app/VERSION" ] && CODE_VERSION=$(head -1 "$WORK_DIR/backend/app/VERSION" 2>/dev/null | tr -d '\r\n')
DEPLOYED_VERSION=""
[ -f "$WORK_DIR/.deployed-version" ] && DEPLOYED_VERSION=$(head -1 "$WORK_DIR/.deployed-version" 2>/dev/null | tr -d '\r\n')

echo ""
info "版本信息:"
info "  离线包版本 : ${PKG_VERSION:-<未标注，可能是源码目录直跑>}"
info "  代码版本   : ${CODE_VERSION:-<未找到 backend/app/VERSION>}"
info "  已装版本   : ${DEPLOYED_VERSION:-<首次部署>}"

# 降级保护：包版本 < 已装版本 时**不阻断**（回滚是合法操作），但必须让人看见。
# 版本号比较复用 version_gte（下方定义在 SKIP_CHECK 块内，这里做等价判断）。
if [ -n "$PKG_VERSION" ] && [ -n "$DEPLOYED_VERSION" ] && [ "$PKG_VERSION" != "$DEPLOYED_VERSION" ]; then
  _pk=$(echo "$PKG_VERSION" | sed 's/^v//')
  _dp=$(echo "$DEPLOYED_VERSION" | sed 's/^v//')
  _cmp=$(printf '%s\n%s\n' "$_pk" "$_dp" | sort -V | tail -1)
  if [ "$_cmp" = "$_dp" ]; then
    warn "  ⚠️  这是**降级部署**：包版本 $PKG_VERSION 低于已装版本 $DEPLOYED_VERSION"
    warn "     若这是有意的回滚，请继续；若是拷错了包，请立刻 Ctrl-C"
  else
    info "  → 升级部署：$DEPLOYED_VERSION → $PKG_VERSION"
  fi
fi
# 包版本与代码版本不一致 → 包被改过或 VERSION 未同步（发版纪律问题，必须暴露）
if [ -n "$PKG_VERSION" ] && [ -n "$CODE_VERSION" ] && [ "$PKG_VERSION" != "$CODE_VERSION" ]; then
  warn "  ⚠️  包版本($PKG_VERSION) 与代码版本($CODE_VERSION) 不一致"
  warn "     通常是打成包后又改了 backend/app/VERSION。前端镜像会是旧版本，请确认。"
fi
echo ""

# ---------- 统一的 compose 命令探测 ----------
# 为什么单独做一个函数：老代码在 3 处各自判断，且判据是错的：
#     `if command -v docker compose &>/dev/null`   ← **错**
#   `command -v` 只接受**一个**命令名。传两个参数时它只检查第一个（`docker`），
#   于是"docker 存在"被当成"docker compose 子命令可用" → 选中的是 `docker compose`，
#   而真实调用时 `docker` 会把它当未知子命令（实测报
#   `unknown shorthand flag: 'f' in -f`，因为 `$COMPOSE_CMD $COMPOSE_FILES` 展开成
#   `docker compose -f ...` 但 `docker` 先吃到了 `compose` 之后的参数）。
#   正确判据 = **真的执行一次** `docker compose version`（本机则是 docker-compose）。
# 返回值：把 "docker compose" / "docker-compose" 写进 COMPOSE_CMD；都不可用则留空。
COMPOSE_CMD=""   # 必须在函数首次调用前存在（set -u）
detect_compose_cmd() {
  COMPOSE_CMD=""
  if command -v docker &>/dev/null && docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
  elif command -v docker-compose &>/dev/null; then
    COMPOSE_CMD="docker-compose"
  fi
  [ -n "$COMPOSE_CMD" ]
}

# ---------- 跨阶段共享变量的初值（必须先于 SKIP_CHECK 分支！）----------
# ⚠️ 这些变量原本只在「环境预检」的 else 分支里赋值，但阶段 1/2/6/7 也会引用。
#    脚本开了 `set -u`，`SKIP_CHECK=true` 跳过预检块时引用就是 "unbound variable"
#    → 部署在中途（甚至最后一步写记录时）崩掉，前面全部白做。
#    实测连踩两道：阶段 2 的模型完整性校验引用 CHECK_FAILED、阶段 5 引用 COMPOSE_CMD。
#    教训：**初始化必须放在分支之外**，放在分支里的"兜底默认值"等于没有。
CHECK_FAILED=false
CODE_REBUILT=false
NEED_RECREATE=false
KERNEL_MAJOR_MINOR=""
CGROUP_VERSION="unknown"
HOST_ARCH_TAG=""
HOST_PHYS=""
HOST_LOGICAL=""
SE_MODE="不适用"
DOCKER_ROOT=""
UP_ARGS="-d"
MEM_SOURCE="unknown"
MEM_RATIO=""
CEILING=""
CPU_SOURCE="unknown"
CPU_CORES="?"
PEER_EFFECTIVE=""
PEER_CPU=""
API_CPU_DECL=""

# ---------- 跳过检查（环境变量 SKIP_CHECK=true） ----------
if [ "${SKIP_CHECK:-false}" = "true" ]; then
  warn "环境检查已跳过 (SKIP_CHECK=true)"
  if ! detect_compose_cmd; then
    error "找不到可用的 Docker Compose（docker compose / docker-compose 都不可用）"
    error "  跳过环境检查不等于可以跳过 compose —— 无法继续"
    exit 1
  fi
  info "使用 Compose 命令: $COMPOSE_CMD"
else

# ==================================================================
# 阶段 0: 环境预检 — Docker / 磁盘 / 内存
# ==================================================================
echo ""
info "========================================"
info "环境预检"
info "========================================"

SKIP_CHECK="${SKIP_CHECK:-false}"
#: 只告警不阻断的问题数（最后汇总展示；内网无外网，很多"建议项"只能人工处理）
CHECK_WARN=0
warn_issue() { warn "$@"; CHECK_WARN=$((CHECK_WARN + 1)); }

# 辅助函数：语义化版本比较（左边 >= 右边返回 true）
#
# ⚠️ 老实现用 `awk -F. '{printf "%d%03d%03d", $1,$2,$3}'`，有三个真机可复现的坑：
#   ① 两段版本号（`24.0`）→ `$3` 为空 → printf 补 0，但 `%d` 遇空串在部分 awk
#      （mawk/busybox awk）直接把它当 0 且**丢弃后续位**，比较结果不稳定；
#   ② 带后缀的版本（`20.10.0-ce`、`5.10.0-1160.el7.x86_64`）→ `%d` 把 `0-ce`
#      解析成 0，看似无害；但 `1.29.2` 这种 Docker 17 时代的号会算成
#      1*10^6+29*10^3+2 = 1029002 > 20*10^3+10 = 20010（**小于被判成大于**）；
#   ③ 非数字内核后缀（`3.10.0-1160`）在 `%d` 下被截断，短版本号场景会误判。
#   现实现：逐段取**前导数字**（没有则取 0），最多 4 段，用 `sort -V` 比——
#   `sort -V` 是 coreutils 8+ 标准能力（CentOS 7 的 coreutils 8.22 已有），
#   天然处理 `1.29.2 < 20.10` 与后缀（`-ce`/`-1160`）的排序。
version_gte() {
  local a b cmp
  a=$(printf '%s' "${1:-}" | sed 's/^[vV]//')
  b=$(printf '%s' "${2:-}" | sed 's/^[vV]//')
  [ -z "$a" ] && return 1
  [ -z "$b" ] && return 0
  # 归一到 点分数字：非数字分隔一律换成 '.'，丢弃字母后缀
  _ver_norm() {
    printf '%s' "$1" \
      | sed 's/[^0-9.]/./g' \
      | tr -s '.' '.' \
      | sed 's/^\.//; s/\.$//'
  }
  a=$(_ver_norm "$a")
  b=$(_ver_norm "$b")
  [ -z "$a" ] && return 1
  [ -z "$b" ] && return 0
  cmp=$(printf '%s\n%s\n' "$a" "$b" | sort -V 2>/dev/null | tail -1)
  [ "$cmp" = "$a" ]
}

# ---------- 0. 必需命令依赖 ----------
# 背景（真实踩坑）：本脚本用 `set -euo pipefail`，任何一条缺失命令会让脚本
# **当场静默退出**（或更糟：管道里静默产出空值，把"检查通过"当结果）。
# 典型：内网最小化安装（RHEL/CentOS 7 minimal）没有 rsync、没有 sysctl；
# 而 rsync 是阶段 2/3 复制源码的唯一手段，缺了就是"更新跑到一半失败"。
# 这里在一切动作**之前**一次性判完，并区分"缺了必失败"与"缺了只降级"。
MISSING_HARD=()
for cmd in bash sed awk grep tar gzip sort docker; do
  command -v "$cmd" &>/dev/null || MISSING_HARD+=("$cmd")
done
# rsync：阶段 2/3 的唯一复制手段。缺了无法降级（cp -a 的 --delete 语义对不上），
# 但**首次部署无 code/ 目录时用不到**，故只在确实需要时硬失败（见下方判定）。
HAS_RSYNC=true
command -v rsync &>/dev/null || HAS_RSYNC=false
# df/du：只用于空间提示，缺了就跳过（不阻断）
HAS_DF=true; command -v df &>/dev/null || HAS_DF=false
HAS_NPROC=true; command -v nproc &>/dev/null || HAS_NPROC=false
# ss/netstat：端口占用检查（缺了跳过并告警）
PORT_TOOL=""
if command -v ss &>/dev/null; then PORT_TOOL="ss"
elif command -v netstat &>/dev/null; then PORT_TOOL="netstat"
fi
# 探测工具"能用"才算可用（重要）：
#   `command -v netstat` 只能证明文件存在，不证明能跑。
#   Git Bash / 精简镜像里的 netstat 是 stub，执行即非零退出；
#   在 `set -e` 下会让 `inuse=$(...)` 直接终止**整个脚本**，
#   且没有任何错误输出（实测踩到：日志停在"端口占用检查（netstat）:"就结束）。
if [ -n "$PORT_TOOL" ]; then
  if [ "$PORT_TOOL" = "ss" ]; then
    ss -ltn >/dev/null 2>&1 || PORT_TOOL=""
  else
    netstat -ltn >/dev/null 2>&1 || PORT_TOOL=""
  fi
fi

if [ ${#MISSING_HARD[@]} -gt 0 ]; then
  error "缺少必需命令: ${MISSING_HARD[*]}"
  error "  请先安装（CentOS/RHEL: yum install -y ${MISSING_HARD[*]}）"
  CHECK_FAILED=true
fi
if [ "$HAS_RSYNC" != true ]; then
  if [ -d code ] || [ "$INSTALL_DIR" != "$PACK_DIR" ]; then
    error "缺少 rsync —— 本次部署需要复制源码（code/ 或 INSTALL_DIR 分离模式），无法继续"
    error "  安装: yum install -y rsync   /   apt-get install -y rsync"
    error "  临时绕过（不推荐，会退化成逐文件 cp，慢但可用）: SKIP_CHECK=true bash deploy.sh"
    CHECK_FAILED=true
  else
    warn "未安装 rsync（本次为纯镜像部署，不需要复制源码）"
    warn "  若之后做增量更新（包内含 code/），会因缺少 rsync 失败 → 建议现在就装"
  fi
fi
if [ ${#MISSING_HARD[@]} -eq 0 ] && [ "$HAS_RSYNC" = true ]; then
  info "命令依赖: 齐全 (docker/sed/awk/grep/tar/gzip/sort/rsync)"
fi

# ---------- 0a. 检查 Docker Engine ----------
if ! command -v docker &>/dev/null; then
  error "Docker 未安装！请先安装 Docker Engine (>= 20.10)"
  CHECK_FAILED=true
else
  DOCKER_VER=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
  info "Docker Engine: $DOCKER_VER"
  if [ "$DOCKER_VER" != "unknown" ]; then
    if version_gte "$DOCKER_VER" "20.10"; then
      info "  ✅ Docker Engine >= 20.10，满足要求"
    else
      warn "  ⚠️  Docker Engine < 20.10，建议升级到 20.10+"
      warn "     当前版本不支持 Compose V2 插件，需要单独安装 docker-compose-plugin"
    fi
  fi
fi

# ---------- 0b. 检查 Docker Compose ----------
# 注意：端口检查（0e-2）会用到 $COMPOSE_CMD，所以这里必须**在任何使用之前**赋值。
if detect_compose_cmd; then
  if [ "$COMPOSE_CMD" = "docker compose" ]; then
    # `--short` 在 V2 可用；某些发行版的打包版本不支持 → 退回整行解析
    COMPOSE_VER=$(docker compose version --short 2>/dev/null \
                   || docker compose version 2>/dev/null | sed -n 's/.*[vV]\{0,1\}\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' | head -1)
    info "Docker Compose: $(docker compose version 2>/dev/null | head -1)"
    if version_gte "$COMPOSE_VER" "2.0"; then
      info "  ✅ Compose V2，满足要求"
    else
      warn "  ⚠️  Compose 版本较低 ($COMPOSE_VER)，建议升级到 v2.0+"
      warn_issue "  Compose 版本偏低（$COMPOSE_VER）"
    fi
  else
    COMPOSE_VER=$(docker-compose version --short 2>/dev/null || echo "unknown")
    info "Docker Compose: docker-compose $COMPOSE_VER"
    warn "  ⚠️  检测到 docker-compose（v1 独立二进制），本项目脚本用 V2 语法"
    warn "     v1 对 -f 叠加与 mem_limit/cpus 都支持，但部分字段语义不同（如 depends_on 条件）"
    warn "     ★ 这不是致命问题：脚本已按实际命令分派，可继续部署"
    warn "     建议后续安装 compose 插件以获得官方支持的 V2 行为"
    warn_issue "  使用 docker-compose v1（非 V2 插件）"
  fi
else
  error "Docker Compose 未安装！"
  error "  V2（推荐）: 装 docker-compose-plugin  或  dnf/apk install docker-compose-plugin"
  error "  V1（可用）: curl -L https://.../docker-compose-\$(uname -s)-\$(uname -m) -o /usr/local/bin/docker-compose"
  CHECK_FAILED=true
fi

# ---------- 0c. 检查 OS 类型 ----------
# 注意：KERNEL_MAJOR_MINOR / CGROUP_VERSION 已在预检块外初始化（见 set -u 说明），
# 这里不再重复声明，避免"初始化在这里、引用在块外"的隐患反复出现。
if [ "$(uname -s)" = "Linux" ]; then
  HOST_ARCH=$(uname -m 2>/dev/null || echo unknown)
  info "OS: Linux ($HOST_ARCH)"
  # 检查内核版本
  # ⚠️ 不用 `grep -oP`：`-P` 是 GNU grep 扩展，busybox grep（Alpine/精简镜像宿主机）
  #    没有它 → 返回空串 → 后续 `version_gte ""` 判成"非 4.0" → 静默进入 3.x 兼容
  #    分支（不报错，只是所有判断都走错路）。用 sed 通吃。
  KERNEL_VER=$(uname -r 2>/dev/null | sed -n 's/^\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p')
  KERNEL_MAJOR_MINOR="$KERNEL_VER"
  if [ -z "$KERNEL_VER" ]; then
    warn "  ⚠️  无法从 '$(uname -r 2>/dev/null)' 解析内核主次版本号"
    warn "     后续内核相关判定（cgroup 分支/预期待校验项）将被跳过，不影响部署"
  elif version_gte "$KERNEL_VER" "4.0"; then
    info "  ✅ Kernel >= 4.0，满足要求 ($(uname -r))"
  else
    info "  ⚠️  内核 $KERNEL_VER（$(uname -r)）属 3.x 线，进入兼容模式"
    info "     → cgroup 只有 v1、无 nftables、Milvus 需手工调 vm.max_map_count"
    info "     → 应用已内置 cgroup v1 内存/CPU 兜底，无需改代码"
  fi
  # 架构：内网包里的镜像是**在外网机器上 docker save 的**，架构由外网机器决定。
  # x86_64 包拷到 ARM（鲲鹏/飞腾）或相反 → 镜像能 load 但**起不来**
  # （exec format error），且报错发生在阶段 5 启动时才出现，排查成本高。
  case "$HOST_ARCH" in
    x86_64|amd64) HOST_ARCH_TAG="amd64" ;;
    aarch64|arm64) HOST_ARCH_TAG="arm64" ;;
    *) HOST_ARCH_TAG="$HOST_ARCH" ;;
  esac
  info "  架构: $HOST_ARCH_TAG"
  # cgroup 版本判定（两种探测互相校验：stat 文件系统类型 + 文件是否存在）
  CGROUP_VERSION="unknown"
  if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    CGROUP_VERSION="v2"
  elif [ -d /sys/fs/cgroup/memory ]; then
    CGROUP_VERSION="v1"
  fi
  info "  cgroup: $CGROUP_VERSION"
  if [ "$CGROUP_VERSION" = "v1" ]; then
    info "     （v1：应用走 memory.limit_in_bytes / cpu.cfs_quota_us 兜底分支）"
  fi
elif [ "$(uname -s)" = "Darwin" ]; then
  info "OS: macOS ($(uname -m))"
  CGROUP_VERSION="N/A(macOS)"
else
  info "OS: $(uname -s) ($(uname -m))"
  warn "  ⚠️  建议在 Linux 或 macOS 上部署"
fi

# ---------- 0c-2. 内核参数（vm.max_map_count / memlock）----------
# Milvus 官方硬要求：vm.max_map_count >= 262144。3.10（CentOS 7）默认只有 65530，
# 不调会在数据量上来后报 "max virtual memory areas vm.max_map_count too low" 并崩溃。
MAX_MAP_COUNT_REQ=262144
if [ -r /proc/sys/vm/max_map_count ]; then
  CUR_MAP_COUNT=$(cat /proc/sys/vm/max_map_count 2>/dev/null || echo 0)
  info "vm.max_map_count: $CUR_MAP_COUNT (需要 >= $MAX_MAP_COUNT_REQ)"
  if [ "$CUR_MAP_COUNT" -lt "$MAX_MAP_COUNT_REQ" ]; then
    if [ "$(id -u)" = "0" ]; then
      info "  → 尝试临时调整（重启失效，持久化见下方提示）"
      # sysctl 可能不存在（精简系统）→ 直接写 /proc 同一效果，两条都试
      sysctl -w vm.max_map_count=$MAX_MAP_COUNT_REQ >/dev/null 2>&1 || true
      if ! sysctl -w vm.max_map_count=$MAX_MAP_COUNT_REQ >/dev/null 2>&1; then
        echo $MAX_MAP_COUNT_REQ > /proc/sys/vm/max_map_count 2>/dev/null || true
      fi
      NEW_MAP_COUNT=$(cat /proc/sys/vm/max_map_count 2>/dev/null || echo 0)
      if [ "$NEW_MAP_COUNT" -ge "$MAX_MAP_COUNT_REQ" ]; then
        info "  ✅ 已调整为 $NEW_MAP_COUNT"
        info "     持久化（重启仍生效）: echo 'vm.max_map_count = $MAX_MAP_COUNT_REQ' >> /etc/sysctl.conf"
      else
        warn "  ⚠️  调整失败，请手工执行: sysctl -w vm.max_map_count=$MAX_MAP_COUNT_REQ"
        warn "     （容器内无法改宿主 sysctl；若脚本运行在容器里，请在宿主机上执行）"
      fi
    else
      warn "  ⚠️  当前非 root，无法自动调整。请用 root 执行（永久生效）:"
      warn "     echo 'vm.max_map_count = $MAX_MAP_COUNT_REQ' >> /etc/sysctl.conf && sysctl -p"
    fi
  else
    info "  ✅ 满足 Milvus 要求"
  fi
else
  info "vm.max_map_count: 非 Linux 或不可读，跳过"
fi

# memlock：Milvus 内部用 mmap 锁页，3.10 上默认 64KB 会失败
if [ -n "$(ulimit -l 2>/dev/null)" ]; then
  MEMLOCK=$(ulimit -l 2>/dev/null || echo "unknown")
  info "memlock (ulimit -l): $MEMLOCK"
  if [ "$MEMLOCK" != "unlimited" ] && [ "$MEMLOCK" != "unknown" ] && [ "$MEMLOCK" -lt 262144 ] 2>/dev/null; then
    warn "  ⚠️  memlock 偏小（$MEMLOCK KB）。Milvus 需要 unlimited 或 >= 262144。"
    warn "     已在 docker-compose.offline.yml 的 milvus 服务里声明 ulimits.memlock=unlimited，"
    warn "     若仍报错，请确认 dockerd 以 root 运行且未被 systemd LimitMEMLOCK 限制:"
    warn "     /etc/systemd/system/docker.service.d/limits.conf → [Service] LimitMEMLOCK=infinity"
  else
    info "  ✅ memlock 满足要求"
  fi
fi

# iptables 后端：3.10（RHEL/CentOS 7）只有 legacy 表，nf_tables 会直接失败
IPTABLES_MODE=""
if command -v iptables &>/dev/null; then
  IPTABLES_MODE=$(iptables -V 2>/dev/null | grep -oE '\([a-z]+\)' | tr -d '()' || echo "")
  info "iptables 后端: ${IPTABLES_MODE:-unknown}"
  if [ "$IPTABLES_MODE" = "nf_tables" ]; then
    if [ -n "$KERNEL_MAJOR_MINOR" ] && ! version_gte "$KERNEL_MAJOR_MINOR" "4.0"; then
      error "  ❌ 3.x 内核 + nf_tables 后端不兼容（Docker 端口映射/容器网络会失败）"
      error "     修复: update-alternatives --set iptables /usr/sbin/iptables-legacy"
      CHECK_FAILED=true
    fi
  elif [ "$IPTABLES_MODE" = "legacy" ]; then
    info "  ✅ legacy 后端，3.x 内核下可用"
  fi
fi

# ---------- 0c-3. CPU 核数（决定并发预算与单次推理线程数）----------
# 口径见 docs/24 §5-2：并发任务数按**物理核**，单次推理线程按**逻辑核**封顶且 ≤4。
# 这里只做"能不能跑"的下限检查 + 把两个数打出来，供现场对照应用自检结果。
if [ -r /proc/cpuinfo ] || [ "$HAS_NPROC" = true ]; then
  HOST_LOGICAL=$(nproc 2>/dev/null || grep -c '^processor' /proc/cpuinfo 2>/dev/null || echo 0)
  HOST_PHYS=""
  _sib=$(sed -n 's/^siblings[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p' /proc/cpuinfo 2>/dev/null | head -1)
  _cores=$(sed -n 's/^cpu cores[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p' /proc/cpuinfo 2>/dev/null | head -1)
  if [ -n "$_sib" ] && [ -n "$_cores" ] && [ "$_sib" -ge "$_cores" ] 2>/dev/null && [ "$_cores" -gt 0 ]; then
    _tpc=$(( _sib / _cores ))
    [ "$_tpc" -lt 1 ] && _tpc=1
    HOST_PHYS=$(( (HOST_LOGICAL + _tpc - 1) / _tpc ))
    info "CPU: ${HOST_LOGICAL} 逻辑核 / ${HOST_PHYS} 物理核（每核 ${_tpc} 线程，来自 /proc/cpuinfo）"
  else
    HOST_PHYS=$(( HOST_LOGICAL / 2 ))
    [ "$HOST_PHYS" -lt 1 ] && HOST_PHYS=1
    info "CPU: ${HOST_LOGICAL} 逻辑核 / 物理核未知（按 ÷2 保守估算 ${HOST_PHYS}）"
  fi
  # 4 核是"能跑"的底线：Milvus + PG + api + celery 本身就要抢核，
  # 2 核机器上生成任务会因为拿不到推理份额而长期排队（不报错、只变慢）。
  if [ "${HOST_LOGICAL:-0}" -lt 4 ]; then
    warn "  ⚠️  仅 ${HOST_LOGICAL} 个逻辑核：本项目最低建议 4 核（推荐 8 核）"
    warn "     低于 4 核时生成任务会长期排队；部署脚本会自动把并发压到 1，不会崩"
    warn_issue "  CPU 核数偏低（${HOST_LOGICAL} 逻辑核 < 4）"
  else
    info "  ✅ CPU 核数满足要求"
  fi
fi

# ---------- 0d. 检查内存 ----------
# 注意：这里读的是**宿主**内存，用于判断"机器够不够跑全套服务"。
# 应用侧的 OOM 保护看的是**容器 cgroup 上限**，两者不是一回事（见 0d-2）。
if [ -r /proc/meminfo ]; then
  MEM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
  MEM_GB=$((MEM_KB / 1024 / 1024))
  info "内存: ${MEM_GB}GB"
  if [ "$MEM_GB" -ge 8 ]; then
    info "  ✅ 内存 >= 8GB，满足要求"
  else
    warn "  ⚠️  内存 ${MEM_GB}GB < 8GB，Milvus + 模型加载可能需要更多内存"
    warn "     建议至少 8GB，推荐 16GB"
  fi
elif command -v free &>/dev/null; then
  MEM_GB=$(free -g | awk '/^Mem:/{print $2}')
  info "内存: ${MEM_GB}GB (估算)"
  if [ "$MEM_GB" -ge 8 ]; then
    info "  ✅ 内存 >= 8GB，满足要求"
  else
    warn "  ⚠️  内存 ${MEM_GB}GB 偏低"
  fi
fi

# ---------- 0d-2. 容器内存上限（OOM 保护的依据）----------
# 背景（2026-10-27 实证）：应用的 MEM_LOW_RATIO 守卫靠 cgroup 上限判断"内存是否紧张"。
# 若容器**没设** mem_limit，cgroup 就是 "max" → 守卫只能读宿主内存 →
# 判定结果与实际 OOM 风险不一致，"内存紧张自动降并发"这道保护等于没有。
# 这里只做提示（不阻断）：mem_limit 是否设置决定保护强度，不影响能否启动。
info "容器内存上限（决定 OOM 保护是否有效）:"
COMPOSE_FILE_FOR_CHECK="docker-compose.offline.yml"
RES_OVERRIDE_FOR_CHECK="docker-compose.resources.override.yml"
MEM_LIMIT_FOUND=false
if [ -f "$RES_OVERRIDE_FOR_CHECK" ]; then
  # 配额由 apply-resource-limits.sh 生成（阶段 4-2 会重算），这里先看是否已有。
  if grep -qE '^\s+mem_limit:\s*\S' "$RES_OVERRIDE_FOR_CHECK" 2>/dev/null; then
    MEM_LIMIT_FOUND=true
    info "  ✅ 已有 $RES_OVERRIDE_FOR_CHECK 声明内存上限:"
    grep -nE '^\s+mem_limit:\s*\S' "$RES_OVERRIDE_FOR_CHECK" | sed 's/^/     /'
  fi
fi
if [ "$MEM_LIMIT_FOUND" != true ]; then
  info "  尚未生成 $RES_OVERRIDE_FOR_CHECK — 阶段 4-2 会按宿主资源自动生成"
  info "     （可预先覆盖：环境变量 API_MEM_LIMIT / API_CPU_LIMIT / MILVUS_MEM_LIMIT）"
fi

# ---------- 0e. 检查磁盘空间 ----------
# ⚠️ 内网常见形态：`/var/lib/docker` 与安装目录**不在同一个分区**。
#    只查安装目录会把"镜像一 load 就 No space left on device"漏掉，
#    而且失败发生在阶段 1（load 镜像）中途，前功尽弃。
INSTALL_DIR_PATH="${INSTALL_DIR:-$PACK_DIR}"
DOCKER_ROOT=""
if command -v docker &>/dev/null; then
  DOCKER_ROOT=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo "")
fi

check_space() {  # <路径> <标签> <要求GB>
  local p="$1" label="$2" req="$3" avail_kb avail_gb
  [ "$HAS_DF" = true ] || return 0
  [ -d "$p" ] || { info "  $label: 路径 $p 不存在，跳过"; return 0; }
  avail_kb=$(df -P "$p" 2>/dev/null | awk 'NR>1{print $4}')
  [ -n "$avail_kb" ] || return 0
  avail_gb=$((avail_kb / 1024 / 1024))
  if [ "$avail_gb" -ge "$req" ]; then
    info "  ✅ $label 剩余 ${avail_gb}GB (>= ${req}GB)"
  else
    warn "  ⚠️  $label 仅剩 ${avail_gb}GB < ${req}GB"
    warn_issue "  磁盘空间不足：$label ${avail_gb}GB < ${req}GB"
  fi
}

if [ "$HAS_DF" = true ]; then
  if is_first_deploy; then
    REQ_SPACE=30
    info "磁盘空间（首次部署需 ${REQ_SPACE}GB：镜像包 ~12GB + 解包 + 模型）:"
  else
    REQ_SPACE=5
    info "磁盘空间（增量更新需 ${REQ_SPACE}GB）:"
  fi
  check_space "$INSTALL_DIR_PATH" "安装目录" "$REQ_SPACE"
  if [ -n "$DOCKER_ROOT" ] && [ "$DOCKER_ROOT" != "$INSTALL_DIR_PATH" ]; then
    # 镜像空间要求低于安装目录（镜像包解压后落在 docker root）
    check_space "$DOCKER_ROOT" "Docker 数据目录($DOCKER_ROOT)" "$REQ_SPACE"
  fi
  # inode：磁盘空间够但 inode 耗尽是最难排查的一类（"明明还有 20G 却写不进去"）。
  # 前端 node_modules / 后端 __pycache__ / Milvus 小文件多，inode 消耗不可忽视。
  INODE_LINE=$(df -Pi "$INSTALL_DIR_PATH" 2>/dev/null | awk 'NR>1{print $5" "$4}')
  if [ -n "$INODE_LINE" ]; then
    IUSE=$(echo "$INODE_LINE" | awk '{print $1}' | tr -d '%')
    IFREE=$(echo "$INODE_LINE" | awk '{print $2}')
    if [ -n "$IUSE" ] && [ "$IUSE" -ge 90 ] 2>/dev/null; then
      warn "  ⚠️  inode 使用率 ${IUSE}%（剩余 ${IFREE} 个）——磁盘有空间也可能写不进去"
      warn_issue "  inode 使用率偏高（${IUSE}%）"
    fi
  fi
else
  info "磁盘检查: df 不可用，跳过"
fi

# ---------- 0e-2. 端口占用 ----------
# 项目固定映射 6 个宿主端口。内网机器上常有"上一个部署没清干净"或
# "别的系统占了 80/8000" —— 那种情况下 `up -d` 会报端口冲突，且报错在中文
# 内网环境里经常被 docker 的英文错误消息掩盖，现场容易卡住。
# 只告警不阻断：可能是本项目自己已经在跑（那是正常的更新场景）。
if [ -n "$PORT_TOOL" ] && command -v docker &>/dev/null; then
  PORT_LIST="${NGINX_PORT:-80} ${API_PORT:-8000} 5432 6379 9001 19530 9091"
  info "端口占用检查（$PORT_TOOL）:"
  PORT_CONFLICT=0
  RUNNING_NAMES=$(docker ps --format '{{.Names}}' 2>/dev/null || echo "")
  for p in $PORT_LIST; do
    # 每处探测都带 `|| true`：`set -e` 下任何一次「工具不支持某参数/权限不足」
    # 都会让整个部署静默退出。端口检查只是告警项，绝不能有终止能力。
    if [ "$PORT_TOOL" = "ss" ]; then
      inuse=$(ss -ltn 2>/dev/null | awk -v port=":$p" '$4 ~ port"$"' | head -1 || true)
    else
      inuse=$(netstat -ltn 2>/dev/null | awk -v port=":$p" '$4 ~ port"$"' | head -1 || true)
    fi
    inuse="${inuse:-}"
    if [ -n "$inuse" ]; then
      # 本项目容器自己占的不算冲突
      if echo "$RUNNING_NAMES" | grep -qE '^ai-test-gen-'; then
        continue
      fi
      warn "  ⚠️  端口 $p 已被占用: $(echo "$inuse" | awk '{print $4}')"
      PORT_CONFLICT=$((PORT_CONFLICT + 1))
    fi
  done
  if [ "$PORT_CONFLICT" -eq 0 ]; then
    info "  ✅ 所需端口均可用（或由本项目容器占用）"
  else
    warn "     → 若冲突方是本项目旧容器，先执行: $COMPOSE_CMD down"
    warn "     → 若是其它系统占用，改用其它端口: NGINX_PORT=8080 API_PORT=8001 bash deploy.sh"
    warn_issue "  ${PORT_CONFLICT} 个端口被占用"
  fi
elif [ -z "$PORT_TOOL" ]; then
  info "端口检查: ss/netstat 均不可用，跳过"
fi

# ---------- 0e-3. SELinux ----------
# RHEL/CentOS 7 默认 enforcing。容器通过 bind mount 读写宿主机 ./data 时，
# 若文件没有 container_file_t 标签 → Permission denied。表现是 PG 起不来、
# Milvus 写不了 MinIO，日志中文很少见，排查方向容易跑偏到"目录权限"。
if command -v getenforce &>/dev/null; then
  SE_MODE=$(getenforce 2>/dev/null || echo unknown)
  info "SELinux: $SE_MODE"
  if [ "$SE_MODE" = "Enforcing" ]; then
    warn "  ⚠️  SELinux 处于 Enforcing —— 容器 bind mount 可能被拒绝（Permission denied）"
    warn "     推荐做法（三选一，从轻到重）:"
    warn "      ① 给数据目录打标签: chcon -Rt container_file_t $INSTALL_DIR_PATH/data $INSTALL_DIR_PATH/milvus"
    warn "      ② 只为本项目放宽: semanage fcontext -a -t container_file_t \"$INSTALL_DIR_PATH/(data|milvus|logs)(/.*)?\" && restorecon -R $INSTALL_DIR_PATH"
    warn "      ③ 临时: setenforce 0（重启失效，仅用于定位问题）"
    warn "     若首次启动后容器反复重启且日志只有 Permission denied，优先怀疑这里。"
    warn_issue "  SELinux Enforcing（bind mount 可能被拒）"
  fi
else
  # 不是 RHEL 系（或未安装 policycoreutils），说明没有 SELinux 或无关
  info "SELinux: 未启用/不适用"
fi

# ---------- 0f. 检查 BuildKit ----------
if docker buildx version &>/dev/null; then
  # ⚠️ `grep -oP` 在 busybox grep 下不可用；且原始输出是多行，取第一行首字段即可
  BUILDX_VER=$(docker buildx version 2>/dev/null | head -1 | awk '{print $2}')
  info "BuildKit: ${BUILDX_VER:-可用}"
  info "  ✅ BuildKit 可用，多阶段构建无问题"
else
  warn "  ⚠️  BuildKit 不可用，前端多阶段构建可能失败"
  warn_issue "  BuildKit 不可用"
fi

# ---------- 0g. 最终判断 ----------
echo ""
if [ "$CHECK_FAILED" = true ]; then
  error "环境检查未通过，请修复以上问题后重试"
  warn "如需跳过检查，设置环境变量: SKIP_CHECK=true"
  exit 1
else
  if [ "$CHECK_WARN" -gt 0 ]; then
    info "环境检查通过 ✅（有 ${CHECK_WARN} 项告警，见上方 ⚠️ —— 不阻断部署）"
  else
    info "环境检查全部通过 ✅"
  fi
fi
fi  # SKIP_CHECK else 块结束

# ==================================================================
# 阶段 1: 加载基础镜像（仅首次部署有 images/ 目录）
# ==================================================================
if [ -d images ] && [ "$(ls -A images/ 2>/dev/null)" ]; then
  info "========================================"
  info "检测到 images/ 目录，执行首次镜像加载"
  info "========================================"

  # ---- 1a. 镜像架构校验（在 load 之前做，失败代价最小）----
  # 内网包里的 tar 是**外网机器 docker save** 出来的，架构由外网机器决定。
  # x86_64 → ARM（鲲鹏/飞腾/麒麟）会把镜像 load 成功、然后在阶段 5 启动时报
  # `exec format error` —— 那时已经 load 了 12GB、清了旧容器，非常被动。
  # 这里读 tar 里 manifest.json 的 config 文件名反查架构（不需要解压全包）。
  if [ -n "$HOST_ARCH_TAG" ]; then
    ARCH_BAD=0
    for _tar in images/*.tar; do
      _arch=$(tar -xOf "$_tar" manifest.json 2>/dev/null \
               | tr ',' '\n' | sed -n 's/.*"Config":"\([^"]*\)".*/\1/p' | head -1)
      [ -z "$_arch" ] && continue
      # config 文件名形如 blobs/sha256/<id>（OCI）或 <sha>.json（旧格式），
      # 架构信息在同 tar 的 config 文件 JSON 里：architecture 字段。
      _cfgarch=$(tar -xOf "$_tar" "$_arch" 2>/dev/null \
                  | tr ',' '\n' | sed -n 's/.*"architecture"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
      [ -z "$_cfgarch" ] && continue
      case "$_cfgarch" in
        amd64|x86_64) _cfgarch="amd64" ;;
        arm64|aarch64) _cfgarch="arm64" ;;
      esac
      if [ "$_cfgarch" != "$HOST_ARCH_TAG" ]; then
        error "  ❌ $(basename "$_tar") 架构为 ${_cfgarch}，本机是 ${HOST_ARCH_TAG}"
        ARCH_BAD=$((ARCH_BAD + 1))
      fi
    done
    if [ "$ARCH_BAD" -gt 0 ]; then
      error "离线包架构与本机不匹配（${ARCH_BAD} 个镜像）——继续 load 只会在启动时失败"
      error "  请在外网 **同架构** 的机器上重新打包（pack-first.sh），或改用对应架构的离线包"
      error "  临时跳过此校验: SKIP_CHECK=true bash deploy.sh（不推荐）"
      exit 1
    fi
    info "  ✅ 镜像架构与本机一致 (${HOST_ARCH_TAG})"
  fi

  IMAGE_COUNT=$(ls images/*.tar 2>/dev/null | wc -l)
  info "加载 $IMAGE_COUNT 个镜像..."
  
  for tar in images/*.tar; do
    name=$(basename "$tar")
    printf '  → %s ... ' "$name"
    if load_output=$(docker load -i "$tar" 2>&1); then
      echo "done"
    else
      # 老实现丢弃了 $load_output：磁盘满/包损坏/tar 截断 都只显示 "done"，
      # 然后在阶段 5 才以 "image not found" 的形式暴露，排查链很长。
      echo "失败"
      error "     $load_output"
      error "  镜像加载失败，部署中止（不会带着残缺镜像继续启动）"
      exit 1
    fi
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
    if [ "$HAS_RSYNC" = true ]; then
      rsync -a --progress models/ "$TARGET_MODELS/"
    else
      # rsync 缺失时的兜底（例如纯镜像包场景没装 rsync）：cp -a 语义等价，
      # 只是没有 --progress 与断点续传。models/ 通常几十 GB，会比较久。
      warn "  rsync 不可用，改用 cp -a（无进度显示，请耐心等待）"
      cp -a models/. "$TARGET_MODELS"/
    fi
    info "模型大小: $(du -sh "$TARGET_MODELS" 2>/dev/null | cut -f1 || echo 未知)"
  fi
  # 模型完整性：pytorch_model.bin 为 0 字节（打包时被截断）不会报错，
  # 只会在首次生成时抛 "Unable to load weights" —— 这里提前拦住。
  for _m in bge-large-zh-v1.5/pytorch_model.bin bge-reranker-base/pytorch_model.bin; do
    _f="$TARGET_MODELS/$_m"
    if [ -f "$_f" ]; then
      _sz=$(wc -c < "$_f" 2>/dev/null || echo 0)
      if [ "${_sz:-0}" -lt 10485760 ]; then
        error "  ❌ 模型文件疑似损坏: $_f 仅 ${_sz} 字节（<10MB）"
        error "     请重新从外网包复制该文件（打包/传输时被截断）"
        CHECK_FAILED=true
      fi
    fi
  done
  if [ "$CHECK_FAILED" = true ]; then
    error "模型文件校验未通过，部署中止"
    exit 1
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
  # ⚠️ compose 文件与资源脚本：包里的 code/ 不含它们（打包脚本单独放到根目录），
  #    但**增量包会整体覆盖根目录的同名文件**。这里显式同步一次，避免出现
  #    "compose 更新了但旧文件还在" → 新增的服务/环境变量静默不生效。
  for f in docker-compose.offline.yml; do
    if [ -f "code/$f" ] && [ "code/$f" -nt "$f" ]; then
      cp "code/$f" "$f" && info "  已更新 $f"
    fi
  done

  # 3e. 智能合并配置文件（新增变量不覆盖已有值）
  #     - install.env → 合并到已存在的 install.env 或 .env
  #     - 保护内网已创建的 key/密码等敏感信息
  #
  # ⚠️ 顺序（v4.1.5 修正）：**先合并、后删除**。
  #    老实现先 `merge_env_config install.env .env` 再 `rm -f install.env`——
  #    看起来对，但 .env 不存在时会走 `merge_env_config install.env install.env`
  #    这一支（把模板合并进它自己，= 无操作），紧接着直接 rm 掉 install.env，
  #    **配置就此丢失**，容器全部用 settings 默认值启动（DB 密码变默认、
  #    LLM key 丢失）。这条路在"首次部署但包内没带 .env"时必现。
  if [ -f install.env ]; then
    if [ -f .env ]; then
      merge_env_config install.env .env
    else
      # 没有 .env：直接把模板落成 .env（不是"合并进自己再删"）
      cp install.env .env
      info "配置落盘: install.env → .env（首次部署）"
    fi
  fi
  # 清理包内配置模板，避免与运行时配置混淆。
  # 只有确认 .env 存在才删——否则删掉就真的没配置了。
  if [ -f .env ]; then
    rm -f install.env 2>/dev/null || true
  else
    warn "  ⚠️  未找到 .env 且包内无 install.env —— 服务将使用默认配置启动"
    warn "     若这是首次部署，请检查离线包是否完整（pack-first.sh 会生成 install.env）"
    warn_issue "  缺少 .env / install.env（使用默认配置）"
  fi
  
  # 3f. 重建应用镜像（利用已有 base 镜像缓存，只产生新 CODE 层）
  #     SKIP_CHECK=true 时预检块没跑，这里兜底探测一次（幂等，不会覆盖有效值）
  [ -n "${COMPOSE_CMD:-}" ] || detect_compose_cmd || true
  if [ -z "${COMPOSE_CMD:-}" ]; then
    error "找不到可用的 Compose 命令，无法重建镜像"
    exit 1
  fi
  
  info "重建后端镜像 (基于 ai-test-gen-base:latest 缓存)..."
  $COMPOSE_CMD -f docker-compose.offline.yml build backend
  # ⚠️ 关键（v4.1.5 实测踩到）：**build 完必须重建容器**。
  #   `docker compose up -d` 只在"服务定义/镜像 tag 变化"时重建容器；而 build 会
  #   原地覆盖同名 tag（ai-test-gen-api:latest），tag 字符串没变 → compose 认为
  #   "无需重建" → 容器继续跑**旧镜像里的代码**。
  #   现场表现极具误导性：日志显示 "backend Built"、8 个容器全 healthy、
  #   部署脚本打印"部署完成"，但新代码根本没生效（本次实测：新增的函数导入失败）。
  #   故这里显式置位，让阶段 5 用 --force-recreate。
  CODE_REBUILT=true
  
  # 前端镜像处理：
  #   1) 优先加载预构建的镜像 tar（pack-update.sh 已在外网构建好）
  #   2) 无 tar 但镜像已存在 → 跳过（无前端变更）
  #   3) 无 tar 无镜像 → 尝试构建（首次部署兜底）
  #
  # 路径说明（修复）：tar 可能在两处，必须都找 ——
  #   · $WORK_DIR/images/  → 默认模式（包目录即安装目录）
  #   · $PACK_DIR/images/  → INSTALL_DIR 分离模式（上面 rsync 明确排除了 images/，
  #                          旧代码只看相对路径 images/ 会永远找不到 → 前端静默停在旧版本）
  WEB_TAR=""
  for cand in "$WORK_DIR/images/10-ai-test-gen-web.tar" "$PACK_DIR/images/10-ai-test-gen-web.tar"; do
    if [ -f "$cand" ]; then WEB_TAR="$cand"; break; fi
  done

  if [ -n "$WEB_TAR" ]; then
    info "检测到前端镜像更新包 ($WEB_TAR)，加载新镜像..."
    docker load -i "$WEB_TAR"
    # 同理：加载同名 tag 的新前端镜像后，容器不会自动重建 → 前端静默停在旧版本
    CODE_REBUILT=true
    info "前端镜像已更新"
  elif docker image inspect ai-test-gen-web:latest &>/dev/null; then
    info "前端镜像已存在，跳过构建"
  else
    info "前端镜像不存在，尝试构建..."
    $COMPOSE_CMD -f docker-compose.offline.yml build frontend || warn "前端构建失败，请确认 npm 源可用"
  fi
  
  info "应用镜像重建完成"
  echo ""
else
  # 3g. **没有 code/ 目录的部署形态**（v4.1.6 新增）
  #   两种现实场景：
  #     · 源码目录直跑：`bash scripts/deploy.sh`（开发机 / 已在目标机上 clone）
  #     · 首次包里只带镜像 tar，没有 code/（纯镜像部署）
  #   老实现的后果（本次实测踩到）：整个阶段 3 被跳过 → 镜像不重建 →
  #   但阶段 5 因为 .env 变了会 Recreate 容器 → **新容器跑旧镜像里的老代码**，
  #   且脚本一路打印"✅ 部署完成"。表现成"改了代码但一点没生效"，
  #   比直接报错难查得多（本次是靠 `dir()` 里还有已删除的 `_PEER_BUDGET` 才发现）。
  #
  #   处理策略（两种场景分开）：
  #     · 有 backend/ 源码 → 必须重建（源码就是本次要生效的内容）
  #     · 无 backend/ 源码 → 纯镜像部署，镜像由 images/ tar 决定，这里不动
  if [ -d backend/app ] && [ -f backend/Dockerfile ]; then
    info "========================================"
    info "未检测到 code/ 目录，但存在 backend/ 源码 → 按源码目录模式重建镜像"
    info "========================================"
    [ -n "${COMPOSE_CMD:-}" ] || detect_compose_cmd || true
    if [ -z "${COMPOSE_CMD:-}" ]; then
      error "找不到可用的 Compose 命令，无法重建镜像"
      exit 1
    fi
    info "重建后端镜像 (基于 ai-test-gen-base:latest 缓存)..."
    if $COMPOSE_CMD -f docker-compose.offline.yml build backend; then
      CODE_REBUILT=true
      info "  ✅ 后端镜像已重建（阶段 5 将用 --force-recreate 让容器用上新代码）"
    else
      error "后端镜像重建失败 —— 继续部署会让容器跑旧代码，已中止"
      error "  排查: $COMPOSE_CMD -f docker-compose.offline.yml build backend"
      exit 1
    fi
  else
    info "无 code/ 也无 backend/ 源码 → 纯镜像部署（镜像由 images/ tar 决定，跳过重建）"
  fi
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
# 阶段 4-2: 资源配额 override（OOM 保护 / 并发预算的依据）
# ==================================================================
# 为什么必须做：应用的 MEM_LOW_RATIO 守卫与 task_ceiling 都读**容器 cgroup 上限**。
# 容器不设上限 → cgroup 为 "max" → 只能读宿主内存 → 判定与实际风险无关，保护失效。
# mem_limit / cpus 在 compose 里不接受空串，所以配额只能靠这个 override 注入。
# 内核无关：3.10 落 cgroup v1、4.x 落 v2，应用两条分支都支持。
echo ""
info "========================================"
info "生成容器资源配额"
info "========================================"

RES_OVERRIDE="docker-compose.resources.override.yml"
COMPOSE_FILES="-f docker-compose.offline.yml"

# 脚本位置：优先当前目录（INSTALL_DIR），回退包目录。
# 增量包里 scripts/ 与 deploy.sh 同级，两者都带上；INSTALL_DIR 分离时
# 上面 rsync 会复制 scripts/（未被排除），所以当前目录里应该也有一份。
RES_SCRIPT=""
for cand in "$WORK_DIR/scripts/apply-resource-limits.sh" "$PACK_DIR/scripts/apply-resource-limits.sh"; do
  if [ -f "$cand" ]; then RES_SCRIPT="$cand"; break; fi
done

if [ -n "$RES_SCRIPT" ]; then
  # 环境变量（API_MEM_LIMIT / API_CPU_LIMIT / MILVUS_MEM_LIMIT）可覆盖自动计算值；
  # 未设置时脚本按宿主内存与核数自动取（8GB→6g、16GB→10g、32GB→20g；CPU 取 核数-2）。
  # 脚本内部按自身位置定位 PACK_DIR，所以无论从哪调用都写到当前目录旁的 compose 位置。
  if ( cd "$WORK_DIR" && bash "$RES_SCRIPT" ); then
    if [ -f "$WORK_DIR/$RES_OVERRIDE" ]; then
      COMPOSE_FILES="-f docker-compose.offline.yml -f $RES_OVERRIDE"
      info "  → 启动时将叠加 $RES_OVERRIDE"
    fi
  else
    warn "  ⚠️  资源配额生成失败，将以「不限内存/CPU」启动（OOM 保护不生效）"
    warn "     可手工重试: bash scripts/apply-resource-limits.sh"
  fi
else
  warn "  ⚠️  未找到 scripts/apply-resource-limits.sh，跳过（OOM 保护将不生效）"
  warn "     内网包可能不完整：请确认包内含 scripts/ 目录（pack-first.sh / pack-update.sh 应已带上）"
fi

# ==================================================================
# 阶段 5: 启动服务
# ==================================================================
echo ""
info "========================================"
info "启动所有服务"
info "========================================"

# SKIP_CHECK=true 时预检块没跑 → 这里兜底探测（幂等，已有有效值时不覆盖）
[ -n "${COMPOSE_CMD:-}" ] || detect_compose_cmd || true
if [ -z "${COMPOSE_CMD:-}" ]; then
  error "找不到可用的 Compose 命令（docker compose / docker-compose 都不可用）"
  error "  安装: https://docs.docker.com/compose/install/"
  exit 1
fi

# ---- 5a. compose 配置校验（在真正 up 之前）----
# `docker compose config` 只做语法/变量插值解析，不启动容器。它能提前拦住
# 「override 与主文件 YAML 合并冲突」「env 变量缺值导致 invalid size」这类
# 一旦 up 就会带着半启动状态失败的问题（清理成本高：8 个容器部分起来）。
info "校验 compose 配置..."
if ! CFG_ERR=$($COMPOSE_CMD $COMPOSE_FILES config -q 2>&1); then
  error "compose 配置校验失败："
  printf '%s\n' "$CFG_ERR" | sed 's/^/     /'
  error "  常见原因：override 文件损坏（重新生成: bash scripts/apply-resource-limits.sh）"
  error "            .env 里配额变量写成了空值（mem_limit/cpus 不接受空串）"
  exit 1
fi
info "  ✅ compose 配置有效"

# ---- 5b. 配额变更后必须 --force-recreate ----
# ⚠️ 真实踩坑：`docker compose up -d` 对**已存在且未改镜像**的容器
#    **不会重新应用 mem_limit / cpus**——它认为容器定义没变。
#    于是"改了配额但 OOM 保护仍然不生效"，而 deploy.sh 后面还会打印
#    "已生成配额 override"，看起来一切正常（典型的假配置）。
#    判据：当前运行容器的 HostConfig 与 override 声明的值不一致 → 需要重建。
#    注意：NEED_RECREATE 已在脚本顶部初始化（阶段 3 可能已置位），这里**不重置**。
if [ -f "$WORK_DIR/$RES_OVERRIDE" ]; then
  _want_mem=$(sed -n 's/^[[:space:]]*mem_limit:[[:space:]]*\([^[:space:]]*\).*/\1/p' "$WORK_DIR/$RES_OVERRIDE" | head -1)
  _cur_mem=$(docker inspect ai-test-gen-api --format '{{.HostConfig.Memory}}' 2>/dev/null || echo "")
  _want_cpu=$(sed -n 's/^[[:space:]]*cpus:[[:space:]]*["'"'"']*\([0-9.]*\).*/\1/p' "$WORK_DIR/$RES_OVERRIDE" | head -1)
  _cur_cpu=$(docker inspect ai-test-gen-api --format '{{.HostConfig.NanoCpus}}' 2>/dev/null || echo "")
  if [ -n "$_cur_mem" ] && [ "$_cur_mem" != "0" ] && [ -n "$_want_mem" ]; then
    # 把 6g/1024m 之类折算成字节做粗比对；折算不了就不判（宁可不重建）
    _want_mem_b=$(printf '%s' "$_want_mem" | awk '
      { v=$0; u=tolower(substr(v,length(v),1)); if (u !~ /[0-9]/) { v=substr(v,1,length(v)-1) } else { u="m" }
        n=v+0; if (u=="g") n*=1024*1024*1024; else if (u=="m") n*=1024*1024; else n*=1024; printf "%.0f", n }' 2>/dev/null)
    if [ -n "$_want_mem_b" ] && [ "$_want_mem_b" -gt 0 ] 2>/dev/null && [ "$_cur_mem" != "$_want_mem_b" ]; then
      NEED_RECREATE=true
      info "  检测到内存配额变化: 当前 $((_cur_mem/1024/1024))MB → 期望 ${_want_mem}"
    fi
  fi
  if [ -n "$_cur_cpu" ] && [ "$_cur_cpu" != "0" ] && [ -n "$_want_cpu" ]; then
    _want_cpu_n=$(printf '%s' "$_want_cpu" | awk '{printf "%.0f", $1*1000000000}' 2>/dev/null)
    if [ -n "$_want_cpu_n" ] && [ "$_want_cpu_n" -gt 0 ] 2>/dev/null && [ "$_cur_cpu" != "$_want_cpu_n" ]; then
      NEED_RECREATE=true
      info "  检测到 CPU 配额变化: 当前 $(awk -v n="$_cur_cpu" 'BEGIN{printf "%.1f", n/1e9}') 核 → 期望 ${_want_cpu} 核"
    fi
  fi
fi

UP_ARGS="-d"
if [ "${CODE_REBUILT:-false}" = true ]; then
  # 本次构建/加载过镜像 → 同名 tag 被原地覆盖，compose 不会主动重建容器
  UP_ARGS="-d --force-recreate"
  info "  本次已重建/更新镜像 → 用 --force-recreate 重建容器（否则容器仍跑旧镜像里的代码）"
elif [ "$NEED_RECREATE" = true ]; then
  UP_ARGS="-d --force-recreate"
  warn "  ⚠️  容器已存在但配额声明变了 → 使用 --force-recreate 重建（否则 mem_limit/cpus 不生效）"
  warn "     重建期间服务会短暂中断（约 10-60 秒）"
fi

# ==================================================================
# 阶段 5-3: 兄弟容器 CPU 份额投影（**必须在 up -d 之前**）
# ==================================================================
# 背景：api 与 celery 是**两个容器**，各自只读自己的 cgroup。
#   api 算出"逻辑核 6 → 物理核 3 → slots 2"，celery 也算一份，
#   **合计超出整机容量**（celery 跑 embedding / Milvus 双写，最吃 CPU）。
#   应用侧无法自己解决（它不该去读别的容器信息），只能由部署侧投影。
#
# ⚠️ 时序是硬约束（实测踩坑两次）：
#   ① 投影写进 `docker exec` 起的那个进程，uvicorn 主进程**看不到**（两个进程）；
#   ② 即使落盘，主进程若已启动并读过一次，也不会重读（静态事实，只读一次并缓存）。
#   所以必须「先把投影交给 compose（环境变量 + .env + 落盘文件）→ 再 up -d」。
#   `PEER_CELERY_CPU` 在 docker-compose.offline.yml 的 *backend-env 里声明，
#   容器重建时注入主进程；落盘文件是给"容器已建好、只想改投影"的现场手段。
PEER_CPU=""
API_CPU_DECL=""
if [ -f "$WORK_DIR/$RES_OVERRIDE" ]; then
  PEER_CPU=$(awk '
    /^  celery_worker:/{f=1; next}
    f && /^  [a-z_]+:/{f=0}
    f && /cpus:/{gsub(/[^0-9.]/,"",$2); print $2; exit}' "$WORK_DIR/$RES_OVERRIDE" 2>/dev/null)
fi
if [ -f "$WORK_DIR/$RES_OVERRIDE" ]; then
  API_CPU_DECL=$(awk '
    /^  backend:/{f=1; next}
    f && /^  [a-z_]+:/{f=0}
    f && /cpus:/{gsub(/[^0-9.]/,"",$2); print $2; exit}' "$WORK_DIR/$RES_OVERRIDE" 2>/dev/null)
fi

if [ -n "$PEER_CPU" ] && [ -n "$API_CPU_DECL" ]; then
  # ⚠️ 语义（勿混）：投影的**入参是兄弟容器占用的份额**，应用侧做**减法**
  #    （自身 − 兄弟）得到可用额度（见 resource_guard.effective_cpu_limit）。
  #    这里打印的 PEER_EFFECTIVE 只是"预期结果"，供人对照，不参与计算。
  PEER_EFFECTIVE=$(awk -v a="$API_CPU_DECL" -v c="$PEER_CPU" 'BEGIN{v=a-c; if(v<1)v=1; printf "%.0f", v}')
  info "  兄弟容器投影: celery 占用 ${PEER_CPU} 核；api 自身 ${API_CPU_DECL} 核 → 预期 api 可用 ${PEER_EFFECTIVE} 核"
  export PEER_CELERY_CPU="$PEER_CPU"
  # 落盘一份（容器内 /app/data 已挂载 → 宿主机 ./data）；失败不阻断。
  if printf '%s\n# source=deploy.sh:celery=%s\n' "$PEER_CPU" "$PEER_CPU" > "$WORK_DIR/data/.peer_cpu_budget" 2>/dev/null; then
    info "  投影已落盘: data/.peer_cpu_budget（容器重启后按该值扣减预算）"
  else
    warn "  ⚠️  投影落盘失败（data/ 不可写）→ 仅靠环境变量传递"
  fi
  # 若把投影写进 .env，则**下次**不带本脚本启动容器时也能保持。
  # ⚠️ 不用 `sed -i`：GNU sed 与 BSD/macOS sed 语义不同（后者要求 `sed -i ''`），
  #    内网服务器版本杂，改用「写成临时文件再 mv」这种可移植写法。
  if [ -f "$WORK_DIR/.env" ]; then
    _env_tmp="$WORK_DIR/.env.tmp.$$"
    if grep -q '^PEER_CELERY_CPU=' "$WORK_DIR/.env" 2>/dev/null; then
      awk -v v="$PEER_CPU" '/^PEER_CELERY_CPU=/{print "PEER_CELERY_CPU=" v; next} {print}' \
        "$WORK_DIR/.env" > "$_env_tmp" 2>/dev/null && mv -f "$_env_tmp" "$WORK_DIR/.env"
    else
      cp -f "$WORK_DIR/.env" "$_env_tmp" 2>/dev/null \
        && printf 'PEER_CELERY_CPU=%s\n' "$PEER_CPU" >> "$_env_tmp" \
        && mv -f "$_env_tmp" "$WORK_DIR/.env"
    fi
    rm -f "$_env_tmp" 2>/dev/null || true
    info "  .env 已同步 PEER_CELERY_CPU（手工 compose up 时投影同样生效）"
  fi
else
  info "  未声明 celery cpus 配额（或 override 缺失）→ 不做跨容器投影"
  info "     （大盘不小时影响可忽略；4~8 核小机器建议保留 celery 配额，见 docs/24 §6）"
  # 清掉历史投影，避免"上次算 3 核、这次不投影"却仍按 3 核扣减（假配置）。
  export PEER_CELERY_CPU=""
  rm -f "$WORK_DIR/data/.peer_cpu_budget" 2>/dev/null || true
  if [ -f "$WORK_DIR/.env" ] && grep -q '^PEER_CELERY_CPU=' "$WORK_DIR/.env" 2>/dev/null; then
    _env_tmp="$WORK_DIR/.env.tmp.$$"
    awk '/^PEER_CELERY_CPU=/{print "PEER_CELERY_CPU="; next} {print}' \
      "$WORK_DIR/.env" > "$_env_tmp" 2>/dev/null && mv -f "$_env_tmp" "$WORK_DIR/.env"
    rm -f "$_env_tmp" 2>/dev/null || true
  fi
fi

info "执行: $COMPOSE_CMD $COMPOSE_FILES up $UP_ARGS"
$COMPOSE_CMD $COMPOSE_FILES up $UP_ARGS

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
# 阶段 6-2: OOM 保护自检（cgroup v1/v2 是否真的生效）
# ==================================================================
# 这是唯一能证明"3.10 上 OOM 保护没失效"的现场证据：
# mem_source=cgroup_* 说明读到了容器上限；=proc 说明读的是宿主内存，
# 守卫判定不可信（容器没设 mem_limit，或 cgroup 分支未生效）。
echo ""
info "并发预算自检（应用视角）:"

# ---- 6-2a. 兄弟容器 CPU 份额投影：**验证**（注入已在阶段 5-3 完成）----
# 这里只读不写：读 api 主进程的 health_brief，确认它真的收到了投影。
#   判据：cpu_peer_budget 非空 且 cpu_effective_logical_cores ≈ 自身 − 兄弟（下限 1）
#   为什么必须单独验证：投影写在"另一个进程"里时脚本会打印成功、主进程却毫无变化
#   （实测踩坑，脚本自报成功是假信号）。只有从主进程读回来才算数。
if [ -n "$PEER_CPU" ] && [ -n "$API_CPU_DECL" ]; then
  PEER_APPLY=$(docker exec ai-test-gen-api python -c "
import sys; sys.path.insert(0, '/app')
try:
    from app.core.resource_guard import resource_guard
    b = resource_guard.health_brief()
    print('ok', b.get('cpu_peer_budget'), b.get('cpu_effective_logical_cores'),
          b.get('task_ceiling'), b.get('cpu_peer_budget_source'))
except Exception as e:
    print('failed', str(e)[:60], '', '', '')
" 2>/dev/null | tail -1)
  if [ "$(echo "$PEER_APPLY" | awk '{print $1}')" = "ok" ]; then
    _got_peer=$(echo "$PEER_APPLY" | awk '{print $2}')
    _got_eff=$(echo "$PEER_APPLY" | awk '{print $3}')
    _got_ceil=$(echo "$PEER_APPLY" | awk '{print $4}')
    _got_src=$(echo "$PEER_APPLY" | awk '{print $5}')
    if [ -n "$_got_peer" ] && [ "$_got_peer" != "None" ]; then
      info "  ✅ 投影已在主进程生效（来源 ${_got_src:-?}）：扣除 ${_got_peer} 核 → 可用 ${_got_eff}，并发上限 ${_got_ceil}"
    else
      warn "  ⚠️  投影未到达主进程（cpu_peer_budget 为空）：预算仍按 api 自身额度算"
      warn "     常见原因：容器未重建（compose 不会因 env 变化自动重建）→ 加 --force-recreate"
      warn "     现场修法：确认 .env 里 PEER_CELERY_CPU=${PEER_CPU} 后执行"
      warn "       $COMPOSE_CMD $COMPOSE_FILES up -d --force-recreate backend"
    fi
  else
    warn "  ⚠️  投影验证失败（$(echo "$PEER_APPLY" | cut -d' ' -f2-)）：无法确认预算是否扣减"
  fi
fi

# ---- 6-2b. 内存 / CPU 上限是否真的被容器读到 ----
OOM_PROBE=$(docker exec ai-test-gen-api python -c "
import sys; sys.path.insert(0, '/app')
try:
    from app.core.resource_guard import resource_guard
    b = resource_guard.health_brief()
    print(b.get('mem_source', 'unknown'), b.get('mem_available_ratio'), b.get('task_ceiling'),
          b.get('cpu_source', 'unknown'), b.get('cpu_logical_cores', '?'))
except Exception as e:
    print('probe_failed', '', '', '', '')
" 2>/dev/null | tail -1)

MEM_SOURCE=$(echo "$OOM_PROBE" | awk '{print $1}')
MEM_RATIO=$(echo "$OOM_PROBE" | awk '{print $2}')
CEILING=$(echo "$OOM_PROBE" | awk '{print $3}')
CPU_SOURCE=$(echo "$OOM_PROBE" | awk '{print $4}')
CPU_CORES=$(echo "$OOM_PROBE" | awk '{print $5}')

case "$MEM_SOURCE" in
  cgroup_v2)
    info "  ✅ mem_source=cgroup_v2，可用比例 ${MEM_RATIO}，并发上限 ${CEILING}"
    ;;
  cgroup_v1)
    info "  ✅ mem_source=cgroup_v1（3.x 兼容分支已生效），可用比例 ${MEM_RATIO}，并发上限 ${CEILING}"
    ;;
  proc)
    warn "  ⚠️  mem_source=proc：读的是宿主内存，不是容器上限"
    warn "     → OOM 保护（MEM_LOW_RATIO 自动降并发）当前**不生效**"
    warn "     → 处理：确认 $RES_OVERRIDE 存在且启动命令带上了 -f 参数，然后重启"
    warn "        docker compose -f docker-compose.offline.yml -f $RES_OVERRIDE up -d"
    ;;
  probe_failed)
    warn "  ⚠️  自检失败（容器未就绪或代码未更新），稍后可手工执行:"
    warn "     docker exec ai-test-gen-api python -c \"import sys;sys.path.insert(0,'/app');from app.core.resource_guard import resource_guard;print(resource_guard.health_brief())\""
    ;;
  *)
    warn "  ⚠️  无法判定 mem_source（$MEM_SOURCE），可用比例 ${MEM_RATIO}"
    ;;
esac

# CPU 配额自检：cpu_source=cpu_count 意味着应用看到的是宿主全部核，
# 32 核机器上 task_ceiling 会放到 4 → bge 推理被挤爆（表现为生成变慢/超时，不报错）。
case "$CPU_SOURCE" in
  cgroup_v2|cgroup_v1)
    info "  ✅ cpu_source=$CPU_SOURCE，应用可见 ${CPU_CORES} 核（并发预算有真实依据）"
    ;;
  affinity|cpuset)
    info "  ✅ cpu_source=$CPU_SOURCE，应用可见 ${CPU_CORES} 核（按核亲和性收敛）"
    ;;
  cpu_count)
    warn "  ⚠️  cpu_source=cpu_count：应用看到的是宿主全部核（${CPU_CORES}），未受容器配额约束"
    warn "     → 大核数机器上 task_ceiling 会被放大，多个生成任务并发抢 bge 推理"
    warn "     → 处理：确认 $RES_OVERRIDE 里 backend 有 cpus，并用 -f 叠加启动"
    ;;
  *)
    warn "  ⚠️  无法判定 cpu_source（$CPU_SOURCE），应用可见 ${CPU_CORES} 核"
    ;;
esac

# 内核 / cgroup 版本一致性校验：把"3.10 与 4.x 都兼容"这件事变成现场证据。
# 3.10 → 必须 cgroup_v1；4.x → v2 或 v1 都正常（4.0~5.7 默认仍是 v1）。
if [ -n "$KERNEL_MAJOR_MINOR" ]; then
  if ! version_gte "$KERNEL_MAJOR_MINOR" "4.0"; then
    if [ "$MEM_SOURCE" = "cgroup_v1" ]; then
      info "  ✅ 内核 $KERNEL_MAJOR_MINOR（3.x 线）+ mem_source=cgroup_v1：3.10 兼容路径已确认生效"
    elif [ "$MEM_SOURCE" = "proc" ]; then
      warn "  ⚠️  内核 $KERNEL_MAJOR_MINOR（3.x 线）但 mem_source=proc：v1 分支没读到上限"
    fi
  else
    info "  ✅ 内核 $KERNEL_MAJOR_MINOR（4.x+ 线），cgroup $CGROUP_VERSION，mem_source=$MEM_SOURCE"
  fi
fi

# ==================================================================
# 阶段 7: 落盘部署记录（供下次迁移核对 / 现场排障）
# ==================================================================
# 写两个文件：
#   .deployed-version  —— 只有一行版本号，deploy.sh 启动时读它做降级判断
#   DEPLOY-RECORD.md   —— 人看的完整快照，覆盖本次部署环境与关键判定
# 为什么单独落一份 md：内网机器往往不能上网、无法回溯聊天记录，
# 现场唯一能确认"这机器什么时候、用什么包、在什么内核上部署的"就是这份文件。
DEPLOY_TS=$(date '+%Y-%m-%d %H:%M:%S')
EFFECTIVE_VERSION="${PKG_VERSION:-$CODE_VERSION}"

# 宿主资源（写进记录用；apply-resource-limits.sh 内部有自己的探测，两边不必共享）
HOST_MEM_MB=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo "")
HOST_CPU=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo "")

if [ -n "$EFFECTIVE_VERSION" ]; then
  printf '%s\n' "$EFFECTIVE_VERSION" > "$WORK_DIR/.deployed-version"
fi

cat > "$WORK_DIR/DEPLOY-RECORD.md" <<RECEOF
# 部署记录（自动生成，勿手工编辑）

> 本文件由 \`deploy.sh\` 在每次部署结束时覆盖写入，用于事后核对版本与环境。
> 手工追加的历史迁移记录请写在 \`docs/24-kernel-compat-and-intranet-deploy.md\` 的迁移记录表里。

| 项目 | 值 |
|---|---|
| 部署时间 | ${DEPLOY_TS} |
| 包版本 | ${PKG_VERSION:-<未标注>} |
| 代码版本 | ${CODE_VERSION:-<未找到>} |
| 本次部署前版本 | ${DEPLOYED_VERSION:-<首次部署>} |
| 安装目录 | ${WORK_DIR} |
| 内核 | $(uname -r 2>/dev/null || echo unknown) |
| 发行版 | $( (. /etc/os-release 2>/dev/null && echo "\$PRETTY_NAME") || echo unknown ) |
| 架构 | $(uname -m 2>/dev/null || echo unknown) |
| cgroup 版本 | ${CGROUP_VERSION:-unknown} |
| 宿主内存 | ${HOST_MEM_MB:-未知} MB |
| 宿主逻辑核 | ${HOST_CPU:-未知} |
| 宿主物理核 | ${HOST_PHYS:-未知}（拓扑来源: $([ -n "$HOST_PHYS" ] && echo cpuinfo || echo heuristic)） |
| SELinux | ${SE_MODE:-不适用} |
| Docker Root | ${DOCKER_ROOT:-未知} |
| Docker | $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown) |
| Compose | $(docker compose version --short 2>/dev/null || docker-compose version --short 2>/dev/null || echo unknown) |
| 启动参数 | up ${UP_ARGS:- -d} |

## 资源与保护判定

| 指标 | 值 | 判定 |
|---|---|---|
| mem_source | ${MEM_SOURCE:-unknown} | $([ "$MEM_SOURCE" = "proc" ] && echo '❌ OOM 保护失效' || echo '✅ 读到容器上限') |
| cpu_source | ${CPU_SOURCE:-unknown} | $([ "$CPU_SOURCE" = "cpu_count" ] && echo '⚠️ 未受配额约束' || echo '✅ 受配额约束') |
| 兄弟容器投影 | ${PEER_EFFECTIVE:-未投影} | $([ -n "$PEER_EFFECTIVE" ] && echo "✅ celery 占 ${PEER_CPU} 核，api 按剩余预算（预期 ${PEER_EFFECTIVE} 核）" || echo '— 单容器形态') |
| mem_available_ratio | ${MEM_RATIO:-?} | — |
| 应用可见核数 | ${CPU_CORES:-?} | — |
| 并发上限 task_ceiling | ${CEILING:-?} | — |

## 启动命令（后续手工操作必须带同样的 -f 参数）

\`\`\`bash
${COMPOSE_CMD} ${COMPOSE_FILES} ps
${COMPOSE_CMD} ${COMPOSE_FILES} logs -f
${COMPOSE_CMD} ${COMPOSE_FILES} restart
\`\`\`

$([ -f "$WORK_DIR/$RES_OVERRIDE" ] && echo "> ⚠️ 本机存在 \`$RES_OVERRIDE\`，**所有手工 compose 命令都必须带 \`-f $RES_OVERRIDE\`**，否则容器配额丢失、OOM 保护失效。" || echo "> 本机未生成配额 override（容器内存/CPU 不受限，OOM 保护不生效）。")
RECEOF

# ==================================================================
# 完成
# ==================================================================
echo ""
info "========================================"
info "部署完成！"
info "========================================"
echo ""
if [ -n "$EFFECTIVE_VERSION" ]; then
  echo "  部署版本: $EFFECTIVE_VERSION"
fi
echo "  部署记录: $WORK_DIR/DEPLOY-RECORD.md （已写入，供事后核对）"
echo ""
echo "  前端地址:  http://localhost:${NGINX_PORT:-80}"
echo "  API 地址:  http://localhost:${API_PORT:-8000}"
echo ""
echo "  常用命令:"
echo "    查看日志:  $COMPOSE_CMD $COMPOSE_FILES logs -f"
echo "    重启服务:  $COMPOSE_CMD $COMPOSE_FILES restart"
echo "    停止服务:  $COMPOSE_CMD $COMPOSE_FILES down"
echo ""
if [ "$COMPOSE_FILES" != "-f docker-compose.offline.yml" ]; then
  echo -e "${YELLOW}  ⚠️  后续手工命令必须带 -f $RES_OVERRIDE，否则配额丢失（OOM 保护失效）${NC}"
  echo ""
fi

if [ "$all_healthy" = false ]; then
  echo -e "${YELLOW}  提示: 部分服务尚未就绪，等待 1-2 分钟后运行以下命令检查:${NC}"
  echo "    $COMPOSE_CMD $COMPOSE_FILES ps"
fi