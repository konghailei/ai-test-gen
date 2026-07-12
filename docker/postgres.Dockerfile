FROM pgvector/pgvector:pg16

# 尝试安装 zhparser 中文分词扩展
# postgresql-16-zhparser 在部分 Debian/PG 发行版中不可用，安装失败时静默跳过，
# 系统自动降级为 simple 文本搜索配置（hybrid_searcher._get_ts_config 已处理）。
RUN apt-get update \
    && apt-get install -y postgresql-16-zhparser \
    && rm -rf /var/lib/apt/lists/* \
    || (echo "WARNING: postgresql-16-zhparser not available, BM25 will use 'simple' config" \
        && rm -rf /var/lib/apt/lists/*)

# 初始化脚本：尝试创建 zhparser 扩展（不可用时自动跳过）
COPY docker/init-zhparser.sql /docker-entrypoint-initdb.d/
