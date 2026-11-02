FROM pgvector/pgvector:pg16

# ============================================================================
# zhparser 中文分词 — scws(tarball) + zhparser(Gitee 镜像)
#
# 原版用 GitHub clone 在国内网络不稳定，改为：
# - scws: xunsearch 官方 tarball（国内可访问）
# - zhparser: Gitee 镜像（github.com/amutu/zhparser 的同步副本，已适配 PG16）
# ============================================================================

ENV DEBIAN_FRONTEND=noninteractive
ENV SCWS_VERSION=1.2.3
# v4.2.2：scws 源码 tarball 与中文词典的 SHA256，构建期强校验。
# 原实现走明文 HTTP 且不做任何校验：源站被替换、或下载被中间设备截断，
# 都会「编译成功」但产出分词行为异常的镜像（词典截断时 to_tsvector 直接报错），
# 问题要到运行期才暴露。取值方式：curl -fsSL <url> | sha256sum
ENV SCWS_SHA256=60d50ac3dc42cff3c0b16cb1cfee47d8cb8c8baa142a58bc62854477b81f1af5
ENV SCWS_DICT_SHA256=0f43956721561b1f5f0102b529dbe24a1aa15b9fb6c05446367db2717f462af3

RUN set -eux \
    && sed -i 's|http://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' /etc/apt/sources.list.d/*.sources \
    && sed -i 's|http://deb.debian.org/debian-security|http://mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list.d/*.sources \
    && apt-get update \
    # -o Acquire::Retries=10 解决大包（llvm-19-dev 132MB）偶发 TCP 断连
    && apt-get install -y --no-install-recommends \
        -o Acquire::Retries=10 -o Acquire::http::Timeout=30 \
        ca-certificates wget bzip2 git make gcc autoconf automake libtool \
        postgresql-server-dev-16 \
    # ===== 1. 编译安装 scws（tarball，国内源） =====
    && cd /tmp \
    && wget -q --timeout=30 --tries=3 \
        "https://www.xunsearch.com/scws/down/scws-${SCWS_VERSION}.tar.bz2" \
        -O scws.tar.bz2 \
    && echo "${SCWS_SHA256}  scws.tar.bz2" | sha256sum -c - \
    && tar xjf scws.tar.bz2 \
    && cd "scws-${SCWS_VERSION}" \
    && ./configure --prefix=/usr/local \
    && make -j"$(nproc)" \
    && make install \
    # 安装 scws 词典（分词必需，缺失会导致 to_tsvector 报错）
    && mkdir -p /usr/local/scws/etc \
    && wget -q --timeout=30 --tries=3 \
        "https://www.xunsearch.com/scws/down/scws-dict-chs-utf8.tar.bz2" \
        -O /tmp/scws-dict.tar.bz2 \
    && echo "${SCWS_DICT_SHA256}  /tmp/scws-dict.tar.bz2" | sha256sum -c - \
    && tar xjf /tmp/scws-dict.tar.bz2 -C /usr/local/scws/etc/ \
    && ls -la /usr/local/scws/etc/dict.utf8.xdb \
    # 配置动态库路径（zhparser 运行时依赖 libscws.so）
    && echo "/usr/local/lib" > /etc/ld.so.conf.d/scws.conf \
    && ldconfig \
    && cd /tmp && rm -rf /tmp/scws* \
    # ===== 2. 编译安装 zhparser（Gitee 镜像，国内可访问） =====
    && git clone --depth 1 https://gitee.com/eipoz-opensource/zhparser.git /tmp/zhparser \
    && cd /tmp/zhparser \
    && make -j"$(nproc)" \
    && make install \
    # 验证扩展文件已安装
    && ls /usr/share/postgresql/16/extension/zhparser* \
    && ls /usr/lib/postgresql/16/lib/zhparser.so \
    && cd / && rm -rf /tmp/zhparser \
    # ===== 3. 清理编译依赖（保留运行时库 + 词典） =====
    && apt-get purge -y --autoremove git make gcc autoconf automake libtool wget bzip2 postgresql-server-dev-16 \
    && rm -rf /var/lib/apt/lists/* /usr/local/lib/libscws.la /usr/local/share/aclocal

COPY docker/init-zhparser.sql /docker-entrypoint-initdb.d/
