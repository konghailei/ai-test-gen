"""日志配置模块。

使用 loguru 提供结构化日志，支持控制台彩色输出和文件轮转。
"""

import sys
from loguru import logger
from app.config import settings


def setup_logger() -> None:
    """初始化全局日志配置。

    配置内容：
    - 控制台输出：彩色格式，DEBUG 级别
    - 文件输出：JSON 格式，INFO 级别，按天轮转，保留 7 天
    """
    # 移除默认 handler
    logger.remove()

    # 控制台输出（彩色）
    logger.add(
        sys.stderr,
        level="DEBUG" if settings.DEBUG else "INFO",
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # 文件输出（轮转）
    logger.add(
        "logs/aitestgen_{time:YYYY-MM-DD}.log",
        level="INFO",
        rotation="00:00",      # 每天轮转
        retention="7 days",    # 保留 7 天
        compression="zip",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | "
            "{name}:{function}:{line} - {message}"
        ),
        encoding="utf-8",
    )


# 导出 logger 单例
__all__ = ["logger", "setup_logger"]
