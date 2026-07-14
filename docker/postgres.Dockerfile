FROM pgvector/pgvector:pg16

# ============================================================================
# zhparser 中文分词 — 双源码编译（scws → zhparser）
#
# zhparser 依赖 scws（Simple Chinese Word Segmentation）库。
# 两者在 Debian Bookworm / PGDG 16 中均未收录，需从源码编译。
# ============================================================================

ENV DEBIAN_FRONTEND=noninteractive

RUN sed -i 's|http://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' /etc/apt/sources.list.d/*.sources \
    && sed -i 's|http://deb.debian.org/debian-security|http://mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list.d/*.sources \
    && apt-get update \
    # 安装编译依赖（含 autotools 用于 scws）
    && apt-get install -y --no-install-recommends \
        ca-certificates git make gcc autoconf automake libtool postgresql-server-dev-16 \
    # 1. 编译安装 scws — GitHub 源码 + acprep 引导 configure
    && git clone --depth 1 https://github.com/hightman/scws /tmp/scws \
    && cd /tmp/scws \
    && sh acprep \
    && ./configure --prefix=/usr/local \
    && make -j$(nproc) && make install \
    && cd / && rm -rf /tmp/scws \
    # 2. 编译安装 zhparser
    && git clone --depth 1 https://github.com/amutu/zhparser.git /tmp/zhparser \
    && cd /tmp/zhparser \
    && make -j$(nproc) && make install \
    && cd / && rm -rf /tmp/zhparser \
    # 3. 保留运行时 scws 库，清理全部编译依赖
    && cp /usr/local/lib/libscws* /usr/lib/ \
    && apt-get purge -y --autoremove git make gcc autoconf automake libtool ca-certificates postgresql-server-dev-16 \
    && rm -rf /var/lib/apt/lists/* /usr/local/lib/libscws.la /usr/local/share/aclocal

COPY docker/init-zhparser.sql /docker-entrypoint-initdb.d/
