#!/bin/bash
# AI-Test-Gen 一键安装脚本（Linux/macOS）
#
# 使用方式：
#   bash scripts/setup.sh
#
# 设计依据：
# - docs/05-technical-selection.md §8（Docker Compose 先行）
# - docs/00-architecture-overview.md §9.1（MVP 链路）
#
# 自动完成：
#   1. 检查 Python 3.11+
#   2. 创建 .env（如不存在）
#   3. 创建 venv 并安装依赖
#   4. 校验 Milvus 依赖

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$PROJECT_DIR/backend"

echo "============================================"
echo " AI-Test-Gen 环境初始化"
echo "============================================"
echo ""

# 检查 Python
PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
    echo "[错误] 未检测到 Python，请安装 Python 3.11+"
    exit 1
fi

# 检查版本
PYTHON_VERSION=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
$PYTHON -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" || {
    echo "[错误] 需要 Python 3.11+，当前版本: $PYTHON_VERSION"
    exit 1
}
echo "[OK] Python $PYTHON_VERSION"

cd "$BACKEND_DIR"

# 1. 创建 .env
echo ""
echo "[1/4] 检查 .env 配置文件..."
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "[提示] 已从 .env.example 创建 .env，请编辑并填入 API Key"
    else
        echo "[警告] 未找到 .env.example，跳过"
    fi
else
    echo "[OK] .env 已存在"
fi

# 2. 创建虚拟环境
echo ""
echo "[2/4] 创建虚拟环境..."
if [ ! -d "venv" ]; then
    $PYTHON -m venv venv
    echo "[OK] 虚拟环境已创建"
else
    echo "[OK] 虚拟环境已存在，跳过"
fi

# 3. 安装依赖
echo ""
echo "[3/4] 安装依赖..."
source venv/bin/activate
pip install -r requirements.txt --quiet
echo "[OK] 依赖安装完成"

# 4. 校验 Milvus 依赖
echo ""
echo "[4/4] 校验 Milvus 依赖..."
$PYTHON -c "import pymilvus" 2>/dev/null || echo "警告: pymilvus 未安装，向量库功能不可用"
echo "[OK] 依赖校验完成"

echo ""
echo "============================================"
echo "  初始化完成！启动方式："
echo ""
echo "    cd backend"
echo "    source venv/bin/activate"
echo "    uvicorn app.main:app --reload --port 8000"
echo ""
echo "  或使用 Docker:"
echo "    docker compose up -d"
echo "============================================"
