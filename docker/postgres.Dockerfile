FROM pgvector/pgvector:pg16

# 安装 zhparser 中文分词扩展
RUN apt-get update && apt-get install -y \
    postgresql-16-zhparser \
    && rm -rf /var/lib/apt/lists/*

# 初始化脚本：创建扩展和中文全文搜索配置
COPY docker/init-zhparser.sql /docker-entrypoint-initdb.d/
