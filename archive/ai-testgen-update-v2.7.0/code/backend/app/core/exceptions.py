"""自定义异常与错误码。

替代泛化 RuntimeError，提供可区分、可处理的异常类型。
调用方可以根据异常类型做出不同的降级/重试/报错决策。
"""

from __future__ import annotations


class LLMError(Exception):
    """LLM 相关错误的基类。"""
    pass


class LLMBreakerOpenError(LLMError):
    """断路器开启，服务暂不可用。"""
    pass


class LLMAuthenticationError(LLMError):
    """LLM API Key 无效或认证失败。"""
    pass


class LLMNoProviderAvailable(LLMError):
    """所有 LLM 提供商均不可用。"""
    pass


class LLMJsonParseError(LLMError):
    """LLM 返回内容无法解析为 JSON。"""
    pass


class LLMRetryExhausted(LLMError):
    """LLM 调用重试耗尽。"""
    pass


class DatabaseError(Exception):
    """数据库操作错误的基类。"""
    pass


class DatabaseLockError(DatabaseError):
    """数据库锁定（并发写入冲突）。"""
    pass


class EntityNotFoundError(Exception):
    """请求的资源不存在。"""
    pass


class ValidationError(Exception):
    """输入校验失败。"""
    pass


class AuthenticationError(Exception):
    """认证失败。"""
    pass


class RateLimitExceeded(Exception):
    """请求频率超限。"""
    pass


class EncryptionError(Exception):
    """数据加密/解密失败。"""
    pass
