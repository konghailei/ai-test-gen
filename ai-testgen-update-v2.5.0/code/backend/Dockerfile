# ============================================================================
# AI-Test-Gen 应用镜像（日常构建，秒级）
#
# FROM 基础镜像，只 COPY 业务代码。基础镜像已含全部 Python 依赖。
# ============================================================================
ARG BASE_IMAGE=ai-test-gen-base:latest
FROM ${BASE_IMAGE}

COPY backend/ .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
