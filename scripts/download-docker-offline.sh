#!/bin/bash
# ============================================================================
# download-docker-offline.sh — 下载 Docker + Compose 离线安装包
#
# 在能联网的外网机器上运行，下载 Docker 静态二进制 + Compose 插件，
# 输出到指定目录，可打包进离线部署包中。
#
# 用法:
#   bash scripts/download-docker-offline.sh [版本号] [输出目录]
#
# 示例:
#   bash scripts/download-docker-offline.sh                     # 默认版本
#   bash scripts/download-docker-offline.sh 28.1.1 ./docker-offline
#
# 下载内容（Linux x86_64）:
#   - docker-<version>.tgz          # Docker 静态二进制（含 dockerd, containerd, runc…）
#   - docker-compose-linux-x86_64   # Compose V2 插件
#   - buildx-v<version>.linux-amd64 # BuildKit 插件
# ============================================================================
set -euo pipefail

# ---------- 版本配置（可在此修改） ----------
DOCKER_VERSION="${1:-28.1.1}"
COMPOSE_VERSION="v2.35.1"
BUILDX_VERSION="v0.23.0"
OUTPUT_DIR="${2:-./docker-offline}"

echo "============================================"
echo " 下载 Docker 离线安装包"
echo " 架构: Linux x86_64"
echo " Docker:  ${DOCKER_VERSION}"
echo " Compose: ${COMPOSE_VERSION}"
echo " BuildX:  ${BUILDX_VERSION}"
echo " 输出:    ${OUTPUT_DIR}"
echo "============================================"

mkdir -p "$OUTPUT_DIR"

# ==================================================================
# 1. Docker 静态二进制
# ==================================================================
DOCKER_URL="https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz"
DOCKER_TGZ="docker-${DOCKER_VERSION}.tgz"

echo ""
echo "--- [1/3] Docker Engine ---"
echo "  URL: $DOCKER_URL"
if [ -f "$OUTPUT_DIR/$DOCKER_TGZ" ]; then
  echo "  已存在，跳过"
else
  echo -n "  下载中... "
  if wget -q --timeout=60 --tries=3 -O "$OUTPUT_DIR/$DOCKER_TGZ" "$DOCKER_URL"; then
    echo "done ($(du -h "$OUTPUT_DIR/$DOCKER_TGZ" | cut -f1))"
  else
    echo "失败！"
    echo "  请手动下载: $DOCKER_URL"
    exit 1
  fi
fi

# ==================================================================
# 2. Docker Compose V2 插件
# ==================================================================
COMPOSE_URL="https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64"
COMPOSE_FILE="docker-compose-linux-x86_64"

echo ""
echo "--- [2/3] Docker Compose V2 ---"
echo "  URL: $COMPOSE_URL"
if [ -f "$OUTPUT_DIR/$COMPOSE_FILE" ]; then
  echo "  已存在，跳过"
else
  echo -n "  下载中... "
  if wget -q --timeout=60 --tries=3 -O "$OUTPUT_DIR/$COMPOSE_FILE" "$COMPOSE_URL"; then
    chmod +x "$OUTPUT_DIR/$COMPOSE_FILE"
    echo "done ($(du -h "$OUTPUT_DIR/$COMPOSE_FILE" | cut -f1))"
  else
    echo "失败！"
    echo "  请手动下载: $COMPOSE_URL"
    exit 1
  fi
fi

# ==================================================================
# 3. Docker BuildX 插件
# ==================================================================
BUILDX_URL="https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-amd64"
BUILDX_FILE="buildx-${BUILDX_VERSION}.linux-amd64"

echo ""
echo "--- [3/3] Docker BuildX ---"
echo "  URL: $BUILDX_URL"
if [ -f "$OUTPUT_DIR/$BUILDX_FILE" ]; then
  echo "  已存在，跳过"
else
  echo -n "  下载中... "
  if wget -q --timeout=60 --tries=3 -O "$OUTPUT_DIR/$BUILDX_FILE" "$BUILDX_URL"; then
    chmod +x "$OUTPUT_DIR/$BUILDX_FILE"
    echo "done ($(du -h "$OUTPUT_DIR/$BUILDX_FILE" | cut -f1))"
  else
    echo "失败！"
    echo "  请手动下载: $BUILDX_URL"
    exit 1
  fi
fi

# ==================================================================
# 4. 生成安装脚本
# ==================================================================
echo ""
echo "--- 生成 install-docker.sh ---"

cat > "$OUTPUT_DIR/install-docker.sh" << 'INSTALL_SCRIPT'
#!/bin/bash
# ============================================================================
# install-docker.sh — 内网 Linux x86_64 离线安装 Docker + Compose
#
# 用法:
#   sudo bash install-docker.sh                 # 安装到 /usr/local
#   sudo bash install-docker.sh /opt/docker     # 安装到自定义目录
#
# 效果（默认安装到 /usr/local）:
#   /usr/local/bin/docker          # Docker CLI
#   /usr/local/bin/dockerd         # Docker daemon
#   /usr/local/bin/containerd      # 容器运行时
#   /usr/local/bin/runc            # 容器运行器
#   /usr/local/lib/docker/cli-plugins/docker-compose  # Compose V2
#   /usr/local/lib/docker/cli-plugins/docker-buildx   # BuildX
# ============================================================================
set -euo pipefail

INSTALL_DIR="${1:-/usr/local}"
BIN_DIR="$INSTALL_DIR/bin"
PLUGIN_DIR="$INSTALL_DIR/lib/docker/cli-plugins"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 检查 root
if [ "$EUID" -ne 0 ]; then
  echo "[ERROR] 请以 root 运行: sudo bash install-docker.sh"
  exit 1
fi

echo "============================================"
echo " Docker 离线安装 (x86_64)"
echo " 安装目录: $INSTALL_DIR"
echo "============================================"

# ---- 1. 解压 Docker 静态二进制 ----
echo ""
echo "--- [1/3] 安装 Docker Engine ---"
DOCKER_TGZ=$(ls "$SCRIPT_DIR"/docker-*.tgz 2>/dev/null | head -1)
if [ -z "$DOCKER_TGZ" ]; then
  echo "[ERROR] 未找到 docker-*.tgz，请放在同目录下"
  exit 1
fi
echo "  解压: $DOCKER_TGZ → $BIN_DIR"
mkdir -p "$BIN_DIR"
tar xzf "$DOCKER_TGZ" -C "$BIN_DIR" --strip-components=1 docker/docker docker/dockerd
# 只解压 docker 和 dockerd，其他用系统自带或包中的
tar xzf "$DOCKER_TGZ" -C "$BIN_DIR" --strip-components=1 \
  docker/docker docker/dockerd docker/containerd \
  docker/containerd-shim-runc-v2 docker/runc docker/ctr \
  2>/dev/null || true
chmod +x "$BIN_DIR/docker" "$BIN_DIR/dockerd" 2>/dev/null

echo "  Docker CLI:  $BIN_DIR/docker"
echo "  Docker Daemon: $BIN_DIR/dockerd"
$BIN_DIR/docker --version 2>/dev/null || true

# ---- 2. 安装 Compose V2 插件 ----
echo ""
echo "--- [2/3] 安装 Docker Compose V2 ---"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose-linux-x86_64"
if [ -f "$COMPOSE_FILE" ]; then
  mkdir -p "$PLUGIN_DIR"
  cp "$COMPOSE_FILE" "$PLUGIN_DIR/docker-compose"
  chmod +x "$PLUGIN_DIR/docker-compose"
  echo "  Compose 插件: $PLUGIN_DIR/docker-compose"
  $BIN_DIR/docker compose version 2>/dev/null || true
else
  echo "  [SKIP] 未找到 docker-compose-linux-x86_64"
fi

# ---- 3. 安装 BuildX 插件 ----
echo ""
echo "--- [3/3] 安装 Docker BuildX ---"
BUILDX_FILE=$(ls "$SCRIPT_DIR"/buildx-*.linux-amd64 2>/dev/null | head -1)
if [ -n "$BUILDX_FILE" ]; then
  mkdir -p "$PLUGIN_DIR"
  cp "$BUILDX_FILE" "$PLUGIN_DIR/docker-buildx"
  chmod +x "$PLUGIN_DIR/docker-buildx"
  echo "  BuildX 插件: $PLUGIN_DIR/docker-buildx"
  $BIN_DIR/docker buildx version 2>/dev/null || true
else
  echo "  [SKIP] 未找到 buildx-*.linux-amd64"
fi

# ---- 4. systemd 服务 ----
echo ""
echo "--- 配置 systemd 服务 ---"
if [ -f "$BIN_DIR/dockerd" ] && [ ! -f /etc/systemd/system/docker.service ]; then
  cat > /etc/systemd/system/docker.service << 'SERVICEEOF'
[Unit]
Description=Docker Application Container Engine
Documentation=https://docs.docker.com
After=network-online.target firewalld.service containerd.service
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/local/bin/dockerd
ExecReload=/bin/kill -s HUP $MAINPID
TimeoutStartSec=0
RestartSec=2
Restart=always
StartLimitBurst=3
StartLimitInterval=60s
LimitNPROC=infinity
LimitCORE=infinity
LimitNOFILE=infinity
TasksMax=infinity
Delegate=yes
KillMode=process
OOMScoreAdjust=-500

[Install]
WantedBy=multi-user.target
SERVICEEOF

  systemctl daemon-reload
  echo "  docker.service 已创建"
else
  echo "  [SKIP] docker.service 已存在或 dockerd 未安装"
fi

# ---- 4.5 创建默认 daemon.json（如果不存在） ----
echo ""
echo "--- 配置 Docker daemon.json ---"
if [ ! -f /etc/docker/daemon.json ]; then
  mkdir -p /etc/docker
  cat > /etc/docker/daemon.json << 'DAEMONEOF'
{
  "exec-opts": ["native.cgroupdriver=systemd"],
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "100m",
    "max-file": "3"
  },
  "storage-driver": "overlay2",
  "storage-opts": [
    "overlay2.override_kernel_check=true"
  ],
  "iptables": true,
  "ip-forward": true,
  "live-restore": true
}
DAEMONEOF
  echo "  /etc/docker/daemon.json 已创建（默认配置，可按需修改）"
  echo "  提示：内网环境如需私有 Registry 或镜像加速，请编辑此文件添加:"
  echo '    "insecure-registries": ["192.168.1.100:5000"]'
  echo '    "registry-mirrors": ["https://your-mirror:5000"]'
  echo "  提示：旧内核（< 4.0）不支持 overlay2，请将 storage-driver 改为 overlay"
else
  echo "  [SKIP] /etc/docker/daemon.json 已存在，保留现有配置"
fi

# ---- 5. 验证 ----
echo ""
echo "============================================"
echo " 安装完成！验证:"
echo "============================================"
echo "  执行: $BIN_DIR/docker --version"
echo "  执行: $BIN_DIR/docker compose version"
echo "  执行: $BIN_DIR/docker buildx version"
echo ""
echo "  启动 Docker 服务:"
echo "    systemctl enable docker"
echo "    systemctl start docker"
echo "    docker info"
echo ""
echo "  注意：如果 $INSTALL_DIR 不是 /usr/local，"
echo "  需要将 $BIN_DIR 加入 PATH"
INSTALL_SCRIPT

chmod +x "$OUTPUT_DIR/install-docker.sh"

# ==================================================================
# 完成
# ==================================================================
echo ""
echo "============================================"
echo " 下载完成！"
echo "============================================"
echo ""
echo "输出目录: $(cd "$OUTPUT_DIR" && pwd)"
echo ""
ls -lh "$OUTPUT_DIR"/
echo ""
echo "下一步:"
echo "  1. 将整个 $(cd "$OUTPUT_DIR" && pwd) 目录拷贝到内网服务器"
echo "  2. 在内网服务器执行:"
echo "     sudo bash install-docker.sh"
echo "     systemctl enable docker && systemctl start docker"
echo ""