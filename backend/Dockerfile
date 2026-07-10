# ============================================================================
# AI-Test-Gen 应用镜像（日常构建，秒级）
# 
# FROM 基础镜像，只 COPY 业务代码。
# 基础镜像已含全部依赖，此处的 pip install 为兜底（无影响）。
# ============================================================================
ARG BASE_IMAGE=ai-test-gen-base:latest
FROM ${BASE_IMAGE}

# 兜底安装关键依赖（基础镜像已含时跳过，仅耗时 1-2 秒）
RUN pip install --no-cache-dir \
    asyncpg==0.30.0 \
    psycopg2-binary==2.9.10 \
    "marshmallow==3.25.0" \
    setuptools==75.8.0

COPY backend/ .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
