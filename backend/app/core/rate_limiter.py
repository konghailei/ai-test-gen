"""内存 Token Bucket 限流中间件。

实现架构文档 docs/00 L1 防护的"RPM 限速"能力。
非分布式版本，适用于单实例部署；生产多实例需切换 Redis 版。

设计依据：
- docs/00-architecture-overview.md §2（L1 防护: RPM+TPM 双轴限速）
- docs/00-architecture-overview.md §9（Token Bucket 算法）

线程安全：使用 asyncio.Lock 保护计数器操作，避免异步协程竞争。
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from app.config import settings
from app.utils.logger import logger


class TokenBucket:
    """内存 Token Bucket 限流器。

    每 `window_seconds` 秒补充 `limit` 个令牌。
    每个请求消耗 1 个令牌，令牌耗尽则拒绝。

    线程安全：使用 asyncio.Lock 保护计数器，避免 FastAPI 异步协程并发竞争。
    """

    def __init__(self, limit: int, window_seconds: int = 60):
        """初始化 Token Bucket。

        Args:
            limit: 窗口期内最大请求数
            window_seconds: 窗口大小（秒）
        """
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = asyncio.Lock()
        self._buckets: dict[str, dict] = defaultdict(lambda: {
            "tokens": limit,
            "last_refill": time.time(),
        })

    async def allow(self, key: str) -> bool:
        """检查是否允许请求通过（线程安全）。

        Args:
            key: 限流标识（通常是客户端 IP）

        Returns:
            bool: True 表示允许通过
        """
        async with self._lock:
            now = time.time()
            # 惰性清理：桶数量超阈值时，删除长期不活跃的桶，防止内存无限增长。
            # 每个唯一 IP 永久驻留会导致 OOM（IPv6/NAT 轮换/恶意伪造不同源 IP）。
            if len(self._buckets) > 10000:
                stale_cutoff = now - self.window_seconds * 10
                stale_keys = [
                    k for k, v in self._buckets.items()
                    if v["last_refill"] < stale_cutoff
                ]
                for k in stale_keys:
                    del self._buckets[k]
            bucket = self._buckets[key]

            # 补充令牌
            elapsed = now - bucket["last_refill"]
            if elapsed >= self.window_seconds:
                bucket["tokens"] = self.limit
                bucket["last_refill"] = now
            else:
                refill = elapsed / self.window_seconds * self.limit
                if refill > 0:
                    bucket["tokens"] = min(float(self.limit), bucket["tokens"] + refill)
                    bucket["last_refill"] = now - (elapsed % self.window_seconds)

            # 消耗令牌
            if bucket["tokens"] > 0:
                bucket["tokens"] -= 1
                return True
            return False


# 全局限流器
_bucket = TokenBucket(limit=settings.API_RPM_LIMIT, window_seconds=settings.API_RATE_WINDOW)


async def rate_limit_middleware(request: Request, call_next: Callable) -> Response:
    """限流中间件。

    对 /api/ 路径下的请求进行限流检查。
    健康检查、文档路径跳过限流。

    Usage:
        app.middleware("http")(rate_limit_middleware)
    """
    path = request.url.path

    if not path.startswith("/api/"):
        return await call_next(request)

    if path == "/api/health":
        return await call_next(request)

    client_ip = request.client.host if request.client else "unknown"

    if not await _bucket.allow(client_ip):
        logger.warning("[限流] 客户端 {} 请求频率超限: path={}", client_ip, path)
        return JSONResponse(
            status_code=429,
            content={
                "success": False,
                "message": "请求频率超限，请稍后再试",
                "data": {
                    "retry_after": _bucket.window_seconds,
                    "limit": _bucket.limit,
                },
            },
            headers={"Retry-After": str(_bucket.window_seconds)},
        )

    return await call_next(request)
