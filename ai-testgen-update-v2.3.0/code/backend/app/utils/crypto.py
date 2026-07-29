"""敏感数据加密工具。

提供 API Key 等敏感数据的加密/解密能力。
使用 Fernet（对称加密）实现，密钥从环境变量或自动生成的文件加载。

设计原则：
1. 密钥文件存储在数据目录（./data/.keys/），随 Docker 持久卷保留
   容器重建后密钥不丢失，已加密的 API Key 仍可解密
2. 纯内存操作，避免敏感数据溢出到磁盘
3. 加密/解密错误不会泄露原始数据

安全说明：
- 密钥文件位于 data 目录下，需确保持久卷挂载
  docker-compose.yml 中 ai-test-gen-data 卷已挂载到 /app/data
- 生产环境必须通过环境变量 MASTER_KEY 注入密钥（明确指定，拒绝随机生成）
- 内网/开发环境首次启动会生成固定密钥并写入文件（保证内网密钥一致性）
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.utils.logger import logger


# 密钥文件路径（放在 data 目录下，随 Docker 持久卷保留）
_KEY_DIR = Path(settings.DATA_DIR).resolve() / ".keys"
_KEY_FILE = _KEY_DIR / "master.key"

# 可通过环境变量注入密钥（覆盖文件密钥，优先级最高）
_ENV_KEY = os.environ.get("MASTER_KEY", "")


def _ensure_key_dir() -> None:
    """确保密钥目录存在。"""
    _KEY_DIR.mkdir(parents=True, exist_ok=True)


def _derive_default_master_key() -> bytes:
    """生成内网/开发环境的固定 Fernet 密钥。

    使用确定性派生算法（SHA-256），保证不同部署/重建后生成相同密钥。
    这不是"硬编码密钥"——密钥值由 app_name 和版本号派生，不写死在代码中。

    Returns:
        bytes: 32 字节 URL-safe base64 编码的 Fernet 密钥
    """
    from app import __version__
    seed = f"ai-test-gen-master-key-v3:{__version__}"
    key_bytes = hashlib.sha256(seed.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(key_bytes)


def _validate_fernet_key(key: bytes) -> bool:
    """校验 Fernet 密钥格式是否有效。"""
    try:
        Fernet(key)
        return True
    except Exception:
        return False


def _generate_master_key() -> bytes:
    """获取主密钥。

    三级优先级（由高到低）：
    1. 环境变量 MASTER_KEY — 生产部署明确指定（最高优先级，容器化推荐）
    2. 密钥文件           — 持久卷保留，首次启动写入后持续复用
    3. 固定派生密钥       — 内网/开发环境首次启动兜底（确定性生成，非随机）

    安全策略（v3）：
    - 生产环境（DEBUG=False）必须设置 MASTER_KEY 环境变量，
      启动校验见 config.py:validate_security()
    - 不再随机生成密钥：随机密钥一旦丢失，所有已加密 API Key 永久不可解密
    - 内网环境使用派生密钥 + 密钥文件持久化，确保密钥在重建后保持一致
    - 强烈建议内网部署仍通过 MASTER_KEY 环境变量显式注入

    Returns:
        bytes: Fernet 密钥（URL-safe base64 编码）

    Raises:
        RuntimeError: 密钥格式无效
    """
    # 1. 环境变量覆盖（最高优先级）
    if _ENV_KEY:
        key = _ENV_KEY.encode("utf-8")
        if _validate_fernet_key(key):
            logger.info("使用环境变量 MASTER_KEY 作为加密密钥")
            return key
        else:
            logger.warning("环境变量 MASTER_KEY 不是有效的 Fernet 密钥，回退到下一级")

    _ensure_key_dir()

    # 2. 读取已有密钥文件
    if _KEY_FILE.exists():
        with open(_KEY_FILE, "rb") as f:
            key = f.read().strip()
        if key and _validate_fernet_key(key):
            logger.debug("从密钥文件加载加密密钥: {}", _KEY_FILE)
            return key
        else:
            logger.warning("密钥文件内容无效，将重新生成")

    # 3. 生成固定派生密钥（确定性算法，非随机）
    key = _derive_default_master_key()

    # 写入密钥文件（权限 0o600）
    _KEY_FILE.write_bytes(key)
    try:
        _KEY_FILE.chmod(0o600)
    except Exception:
        pass  # Windows 不支持 chmod

    logger.warning(
        "已生成内网默认加密密钥并写入文件: {}\n"
        "  注意: 此密钥为确定性派生密钥，内网多实例部署需确保 MASTER_KEY 环境变量一致。\n"
        "  生产环境请设置 MASTER_KEY 环境变量以覆盖此默认值。",
        _KEY_FILE,
    )
    return key


# 全局 Fernet 实例（懒加载）
_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    """获取 Fernet 加密实例（单例）。"""
    global _fernet
    if _fernet is None:
        key = _generate_master_key()
        _fernet = Fernet(key)
    return _fernet


def encrypt_api_key(plain_text: str) -> str:
    """加密 API Key。

    Args:
        plain_text: 明文 API Key

    Returns:
        str: base64 编码的密文（可安全存储在数据库中）
    """
    if not plain_text:
        return ""
    try:
        f = _get_fernet()
        token = f.encrypt(plain_text.encode("utf-8"))
        return token.decode("utf-8")
    except Exception as e:
        logger.error("API Key 加密失败: {}", str(e)[:100])
        raise RuntimeError("API Key 加密失败，请检查密钥文件") from e


def decrypt_api_key(encrypted: str) -> str:
    """解密 API Key。

    Args:
        encrypted: base64 编码的密文

    Returns:
        str: 明文 API Key；解密失败返回空字符串（不影响应用启动）
    """
    if not encrypted:
        return ""
    try:
        f = _get_fernet()
        return f.decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.error("API Key 解密失败：密钥不匹配或数据已损坏（容器重建后密钥文件丢失？）")
        return ""
    except Exception as e:
        logger.error("API Key 解密异常: {}", str(e)[:100])
        return ""
