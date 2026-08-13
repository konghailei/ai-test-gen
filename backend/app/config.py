"""配置管理模块。

集中管理所有配置项，支持通过环境变量或 .env 文件覆盖默认值。
配置项对应架构文档 docs/00-05 中的技术决策。

设计依据：
- LLM 配置：docs/05-technical-selection.md §2（DeepSeek 主力 + OpenAI 备用）
- 数据库：docs/05-technical-selection.md §11（PostgreSQL + Milvus）
- 知识库配置：docs/02-knowledge-base.md §2.5（知识库维护 + 检索配置）
- 质量门禁阈值：docs/01-requirement-parsing.md §5.2（冷启动 0.60 / 正常 0.75）
- 最大澄清轮次：docs/01-requirement-parsing.md §5.3（最多 3 轮）
- 限流配置：docs/05-technical-selection.md §11.2

安全说明：
- API Key 在数据库中以加密形式存储（Fernet 对称加密）
- 响应中 API Key 自动脱敏（仅显示前3后4位）
- 生产环境必须设置 APP_SECRET_KEY（用于 JWT 和加密）
"""

import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置。

    通过环境变量或 .env 文件加载，优先级：
    环境变量 > .env 文件 > 类属性默认值
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ========== 安全配置 ==========
    """应用密钥（用于 JWT 签名 + 加密盐值）

    安全策略：
    - 生产环境（DEBUG=False）必须通过环境变量设置，否则启动报错
    - 开发环境（DEBUG=True）允许使用默认值
    """
    APP_SECRET_KEY: str = ""

    """Fernet 主密钥（用于加密数据库中存储的 API Key）

    安全策略：
    - 不再硬编码默认密钥（旧版固定密钥已废弃）
    - 优先级：环境变量 > 密钥文件（自动生成）> 启动报错（生产环境）
    - crypto.py 负责实际加载逻辑，此处仅作占位
    """
    MASTER_KEY: str = ""

    """JWT 令牌过期时间（分钟）"""
    JWT_EXPIRE_MINUTES: int = 480  # 8 小时

    # ========== LLM 配置 ==========
    """LLM 提供商：deepseek（主力）或 openai（备用）"""
    LLM_PROVIDER: str = "deepseek"

    """DeepSeek API Key（用户从 .env 配置）"""
    DEEPSEEK_API_KEY: str = ""

    """DeepSeek API 基础地址（OpenAI 兼容接口）"""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"

    """DeepSeek 默认模型"""
    DEEPSEEK_MODEL: str = "deepseek-chat"

    """OpenAI API Key（备用提供商）"""
    OPENAI_API_KEY: str = ""

    """OpenAI API 基础地址"""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"

    """OpenAI 默认模型"""
    OPENAI_MODEL: str = "gpt-4o"

    # ========== 数据目录配置 ==========
    """数据目录路径（相对 backend 目录，存放日志、密钥等持久化文件）"""
    DATA_DIR: str = "./data"

    # ========== 向量存储（Milvus，系统唯一向量库后端） ==========
    """向量存储后端：milvus（ChromaDB 已弃用，统一使用 Milvus）"""
    VECTOR_STORE: str = "milvus"

    """Milvus 服务地址（仅 VECTOR_STORE=milvus 时生效）"""
    MILVUS_HOST: str = "localhost"

    """Milvus gRPC 端口"""
    MILVUS_PORT: int = 19530

    """Milvus Collection 名称前缀（按环境隔离）"""
    MILVUS_COLLECTION_PREFIX: str = "aitestgen"

    # ========== 数据库配置（PostgreSQL，系统唯一关系数据库后端） ==========
    """PostgreSQL 连接字符串（必填，格式：postgresql://user:pass@host:port/dbname）"""
    DATABASE_URL: str = ""

    """PG 连接池大小"""
    PG_POOL_SIZE: int = 10

    """PG 连接超时（秒）"""
    PG_TIMEOUT: int = 30

    """LLM 调用超时（秒），0 表示不限时"""
    LLM_TIMEOUT: int = 0

    # ========== 嵌入模型配置 ==========
    """嵌入模型类型：local（sentence-transformers，默认）| cloud（远程 API 嵌入）。
    注：ChromaDB 已弃用，chroma_default 选项不再可用；模型实际加载由 EMBEDDING_MODEL_NAME 决定。"""
    EMBEDDING_MODEL_TYPE: str = "local"

    """本地嵌入模型名称（仅 EMBEDDING_MODEL_TYPE=local 时生效）"""
    EMBEDDING_MODEL_NAME: str = "BAAI/bge-large-zh-v1.5"

    """嵌入模型计算设备：cpu | cuda | mps"""
    EMBEDDING_DEVICE: str = "cpu"

    # ========== 知识库维护配置 ==========
    """时间衰减率（半衰期 ~180 天）"""
    KB_DECAY_RATE: float = 0.005

    """复用加分系数"""
    KB_BOOST_REUSE: float = 1.5

    """最低权重（防止完全遗忘）"""
    KB_MIN_WEIGHT: float = 0.1

    """超过多少天未使用的知识自动归档"""
    KB_CLEANUP_DAYS: int = 365

    # ========== 检索配置 ==========
    """召回数量"""
    MATCH_TOP_K: int = 20

    """最终返回数量"""
    MATCH_FINAL_N: int = 5

    """最低相似度阈值"""
    MATCH_SIMILARITY_THRESHOLD: float = 0.70

    """GenAgent Few-shot 注入相似度阈值（优化 5：可配置，默认 0.70）"""
    FEW_SHOT_SIMILARITY_THRESHOLD: float = 0.70

    """是否启用 RRF 融合"""
    MATCH_ENABLE_RRF: bool = True

    """是否启用图谱扩展"""
    MATCH_ENABLE_GRAPH: bool = False

    # ========== 重排器配置 ==========
    """是否启用 Cross-Encoder 重排（bge-reranker-base）"""
    RERANKER_ENABLED: bool = True

    """重排模型名称（本地路径或 HuggingFace ID）"""
    RERANKER_MODEL_NAME: str = "BAAI/bge-reranker-base"

    """重排模型计算设备"""
    RERANKER_DEVICE: str = "cpu"

    """重排 Top-N 数量（超过此数量才触发重排）"""
    RERANKER_THRESHOLD: int = 5

    # ========== 质量门禁配置 ==========
    """正常模式质量评分通过阈值（docs/01 §5.2）"""
    DEFAULT_PASS_SCORE: float = 0.75

    """冷启动模式质量评分通过阈值（KB 为空时放宽，docs/01 §5.2）"""
    COLD_START_PASS_SCORE: float = 0.60

    """ClarifyAgent 最大迭代轮次（docs/01 §5.3，防止死循环）"""
    MAX_CLARIFY_ITERATIONS: int = 3

    """ClarifyAgent Optional 类问题上限（v2.1：Critical/Important 不限条数）"""
    CLARIFY_OPTIONAL_LIMIT: int = 20

    # ========== KB 访问日志配置 ==========
    """是否启用 KB 访问日志（v2.1 新增）"""
    KB_ACCESS_LOG_ENABLED: bool = True

    """单次需求分析最多记录条数"""
    KB_ACCESS_LOG_MAX_ENTRIES: int = 50

    # ========== Redis 配置 ==========
    """Redis 主机地址"""
    REDIS_HOST: str = ""

    """Redis 端口"""
    REDIS_PORT: int = 6379

    """Redis 数据库编号"""
    REDIS_DB: int = 0

    """Redis 密码"""
    REDIS_PASSWORD: str = ""

    """LLM 每分钟最大请求数（限流用）"""
    LLM_RPM_LIMIT: int = 500

    """LLM 每分钟最大 Token 数"""
    LLM_TPM_LIMIT: int = 100000

    # ========== Celery 配置 ==========
    """Celery broker URL（Redis）"""
    CELERY_BROKER_URL: str = ""

    """Celery result backend URL（Redis）"""
    CELERY_RESULT_BACKEND: str = ""

    # ========== 限流配置 ==========
    """全局 API 每分钟最大请求数（内存 Token Bucket 限流）"""
    API_RPM_LIMIT: int = 60

    """限流窗口大小（秒）"""
    API_RATE_WINDOW: int = 60

    # ========== 服务配置 ==========
    """后端服务端口"""
    APP_PORT: int = 8000

    """前端开发服务地址（CORS 白名单）"""
    FRONTEND_URL: str = "http://localhost:5173"

    """调试模式（生产环境必须设为 False）"""
    DEBUG: bool = False

    @property
    def pg_async_url(self) -> str:
        """获取 PostgreSQL 异步连接字符串。

        Returns:
            str: 异步 PG URL（使用 asyncpg 驱动）

        Raises:
            ValueError: 未配置 DATABASE_URL 时抛出
        """
        if not self.DATABASE_URL:
            raise ValueError("DATABASE_URL 未配置，PostgreSQL 连接字符串为必填项")
        # 替换 postgresql:// 为 postgresql+asyncpg://
        return self.DATABASE_URL.replace(
            "postgresql://",
            "postgresql+asyncpg://"
        ).replace(
            "postgres://",
            "postgresql+asyncpg://"
        )

    @property
    def pg_sync_url(self) -> str:
        """获取 PostgreSQL 同步连接字符串（供 prompt_store 等同步场景使用）。

        Returns:
            str: 同步 PG URL（使用 psycopg2 驱动）

        Raises:
            ValueError: 未配置 DATABASE_URL 时抛出
        """
        if not self.DATABASE_URL:
            raise ValueError("DATABASE_URL 未配置，PostgreSQL 连接字符串为必填项")
        # 替换为 psycopg2 同步驱动
        return self.DATABASE_URL.replace(
            "postgresql+asyncpg://",
            "postgresql+psycopg2://"
        ).replace(
            "postgresql://",
            "postgresql+psycopg2://"
        ).replace(
            "postgres://",
            "postgresql+psycopg2://"
        )

    @property
    def llm_config(self) -> dict:
        """获取当前 LLM 提供商配置。

        优先级：DB 持久化配置 > .env 文件默认值.
        """
        # 优先从 DB 加载的配置读取
        provider_cfgs = getattr(self, "_all_provider_configs", {})
        db_cfg = provider_cfgs.get(self.LLM_PROVIDER)
        if db_cfg and db_cfg.get("api_key"):
            return db_cfg

        # 回退到 .env
        if self.LLM_PROVIDER == "openai":
            return {
                "api_key": self.OPENAI_API_KEY,
                "base_url": self.OPENAI_BASE_URL,
                "model": self.OPENAI_MODEL,
            }
        return {
            "api_key": self.DEEPSEEK_API_KEY,
            "base_url": self.DEEPSEEK_BASE_URL,
            "model": self.DEEPSEEK_MODEL,
        }

    @property
    def fallback_llm_config(self) -> dict | None:
        """获取备用 LLM 配置（故障转移用）。

        Returns:
            dict | None: 备用配置；若主提供商是 deepseek 则返回 openai 配置，反之亦然。
            若备用 Key 未配置则返回 None。
        """
        if self.LLM_PROVIDER == "deepseek":
            if self.OPENAI_API_KEY:
                return {
                    "api_key": self.OPENAI_API_KEY,
                    "base_url": self.OPENAI_BASE_URL,
                    "model": self.OPENAI_MODEL,
                }
        else:
            if self.DEEPSEEK_API_KEY:
                return {
                    "api_key": self.DEEPSEEK_API_KEY,
                    "base_url": self.DEEPSEEK_BASE_URL,
                    "model": self.DEEPSEEK_MODEL,
                }
        return None

    @property
    def has_redis(self) -> bool:
        """判断是否配置了 Redis。

        Returns:
            bool: True 表示已配置 Redis 主机
        """
        return bool(self.REDIS_HOST)

    def ensure_data_dirs(self) -> None:
        """确保数据目录存在。

        在应用启动时调用，创建数据所需的目录结构（密钥、日志等）。
        """
        Path(self.DATA_DIR).mkdir(parents=True, exist_ok=True)

    def validate_security(self) -> list[str]:
        """启动时安全校验，返回警告列表（空列表表示无警告）。

        生产环境（DEBUG=False）发现致命问题会抛 RuntimeError。

        Returns:
            list[str]: 警告消息列表

        Raises:
            RuntimeError: 生产环境缺少必要的安全配置
        """
        warnings: list[str] = []

        # APP_SECRET_KEY 校验
        if not self.APP_SECRET_KEY:
            if not self.DEBUG:
                raise RuntimeError(
                    "生产环境必须设置 APP_SECRET_KEY 环境变量（用于 JWT 签名）"
                )
            # 开发环境自动生成
            import secrets as _secrets
            self.APP_SECRET_KEY = _secrets.token_urlsafe(32)
            warnings.append("开发环境：已自动生成临时 APP_SECRET_KEY，重启后 JWT 失效")
        elif len(self.APP_SECRET_KEY) < 16:
            warnings.append("APP_SECRET_KEY 长度不足 16 字符，建议使用更长的随机串")

        # MASTER_KEY 校验（LLM API Key 加密密钥）
        # v2.8.60 修复：原代码使用 os.environ.get("MASTER_KEY") 读取，
        # 但 pydantic-settings 已将 .env 中的 MASTER_KEY 加载到 self.MASTER_KEY，
        # 本地 python -m app.main 启动时（未 export 到 shell）会导致 self.MASTER_KEY 有值
        # 但 os.environ.get 返回空，触发误报 RuntimeError。
        master_key_env = self.MASTER_KEY or ""
        if not master_key_env and not self.DEBUG:
            raise RuntimeError(
                "生产环境必须设置 MASTER_KEY 环境变量（用于加密数据库中存储的 LLM API Key）。\n"
                "生成方法: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
                "注意: 请妥善保管此密钥，丢失将导致已加密 API Key 永久不可解密。"
            )
        if not master_key_env and self.DEBUG:
            # 开发/内网环境将使用确定性派生密钥 + 密钥文件持久化
            # 实际密钥由 crypto.py:_derive_default_master_key() 生成
            warnings.append(
                "开发环境：未设置 MASTER_KEY，将使用内网固定派生密钥。"
                "生产环境请务必设置 MASTER_KEY 环境变量。"
            )
        if master_key_env:
            # 简单格式校验（Fernet 密钥为 44 字符 base64）
            if len(master_key_env) != 44 or not master_key_env.endswith("="):
                warnings.append("MASTER_KEY 格式可能不正确（应为 44 字符 base64），请检查")

        return warnings


# 全局配置单例
settings = Settings()
