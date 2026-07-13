# ============================================================================
# AI-Test-Gen 应用镜像（日常构建，秒级）
# 
# FROM 基础镜像，只 COPY 业务代码。
# 基础镜像已含全部依赖，此处的 pip install 为兜底（无影响）。
# ============================================================================
ARG BASE_IMAGE=ai-test-gen-base:latest
FROM ${BASE_IMAGE}

# 兜底安装关键依赖（基础镜像已含时跳过，仅耗时数秒）
# 注：setuptools 须 <80 保留 pkg_resources（pymilvus 依赖），
# 且须 >=77 满足 torch>=2.6 的声明，故与 requirements.txt 保持一致。
RUN pip install --no-cache-dir \
    asyncpg==0.30.0 \
    psycopg2-binary==2.9.10 \
    "marshmallow==3.25.0" \
    "setuptools>=77,<80" \
    "celery[redis]==5.4.0" \
    redis==5.0.1

COPY backend/ .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
