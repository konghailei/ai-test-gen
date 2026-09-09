"""密码哈希工具。

使用 hashlib.pbkdf2_hmac（SHA-256）进行密码哈希，标准库实现零外部依赖。
每次哈希自动生成随机盐值，防止彩虹表攻击。

算法参数：
- 哈希函数：SHA-256
- 迭代次数：600,000 次（满足 OWASP 2023 推荐 ≥ 600K）
- 盐值长度：16 字节（128 位）
- 输出格式：$pbkdf2-sha256$iterations$salt$hash（自描述，便于未来算法升级）
"""

from __future__ import annotations

import hashlib
import os
import secrets


# 算法常量
ALGORITHM = "pbkdf2-sha256"
DEFAULT_ITERATIONS = 600_000
SALT_LENGTH = 16


def hash_password(password: str, iterations: int = DEFAULT_ITERATIONS) -> str:
    """对密码进行哈希。

    Args:
        password: 明文密码
        iterations: PBKDF2 迭代次数（默认 600,000）

    Returns:
        str: 自描述格式的哈希值，格式为 $pbkdf2-sha256$iterations$salt$hash

    Raises:
        ValueError: 密码为空或过短
    """
    if not password or len(password) < 4:
        raise ValueError("密码长度不能少于 4 位")

    salt = secrets.token_hex(SALT_LENGTH)
    pwd_bytes = password.encode("utf-8")
    salt_bytes = salt.encode("ascii")

    dk = hashlib.pbkdf2_hmac("sha256", pwd_bytes, salt_bytes, iterations)
    hash_str = dk.hex()

    return f"${ALGORITHM}${iterations}${salt}${hash_str}"


def verify_password(password: str, password_hash: str) -> bool:
    """验证密码是否匹配哈希。

    Args:
        password: 待验证的明文密码
        password_hash: 自描述格式的哈希值

    Returns:
        bool: True 表示密码匹配
    """
    try:
        parts = password_hash.split("$")
        if len(parts) != 5 or parts[0] != "":
            return False

        # 密码哈希分段：[算法, 迭代次数, 盐, 哈希值]
        alg = parts[1]
        iterations = int(parts[2])
        salt = parts[3]
        expected_hash = parts[4]

        if alg != ALGORITHM:
            # 算法不匹配（可能是旧版本），返回 False
            return False

        pwd_bytes = password.encode("utf-8")
        salt_bytes = salt.encode("ascii")

        dk = hashlib.pbkdf2_hmac("sha256", pwd_bytes, salt_bytes, iterations)
        actual_hash = dk.hex()

        # 恒定时间比较
        return secrets.compare_digest(actual_hash, expected_hash)
    except (ValueError, IndexError):
        return False
