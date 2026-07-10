# AI-Test-Gen 后端 Docker 镜像
#
# 设计依据：
# - docs/05-technical-selection.md §8.1（Docker Compose 开发/小团队）
# - 多阶段构建：构建阶段 → 运行阶段（减小镜像体积）
#
# 用法：
#   docker build -t ai-test-gen:latest .
#   docker run -p 8000:8000 --env-file .env ai-test-gen:latest

# ============================================================================
# Stage 1: 构建阶段（安装依赖 + 编译）
# ============================================================================
FROM python:3.12-slim AS builder

WORKDIR /app

# 安装系统依赖（ChromaDB ONNX 需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖清单并安装
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ============================================================================
# Stage 2: 运行阶段（最小镜像）
# ============================================================================
FROM python:3.12-slim AS runner

WORKDIR /app

# 复制已安装的依赖
COPY --from=builder /root/.local /root/.local

# 确保 PATH 包含用户安装的二进制路径
ENV PATH=/root/.local/bin:$PATH

# 复制应用代码
COPY backend/ .

# 创建数据目录（SQLite + ChromaDB 持久化）
RUN mkdir -p /app/data

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

# 启动命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
