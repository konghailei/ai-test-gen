"""知识库检索缓存 — L1 Redis 语义缓存 + L2 本地 LRU 缓存。

设计依据：
- docs/02-knowledge-base.md §9.4（多级缓存）
- 查询是只读操作，缓存安全
- 缓存 key: (query, product_id, modules, collection_type)
- L1 Redis: TTL 24h，跨进程共享
- L2 LRU: 进程内，最近 1000 次查询

降级策略：
- Redis 不可用 → 仅用 L2 LRU（不影响功能）
- L2 未命中 + Redis 未命中 → 返回 None（触发实际检索）
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from threading import Lock

from app.config import settings
from app.utils.logger import logger


class KbCache:
    """二级缓存：L1 Redis + L2 LRU。"""

    LRU_MAX_SIZE = 1000
    CACHE_TTL = 86400  # 24 小时

    def __init__(self):
        self._lru: OrderedDict[str, str] = OrderedDict()
        self._lru_lock = Lock()
        self._redis = None
        self._redis_checked = False

    def _get_redis(self):
        """延迟获取 Redis 连接（仅在 has_redis 时初始化）。"""
        if not self._redis_checked:
            self._redis_checked = True
            if settings.has_redis:
                try:
                    import redis
                    self._redis = redis.Redis(
                        host=settings.REDIS_HOST,
                        port=settings.REDIS_PORT,
                        db=settings.REDIS_DB,
                        password=settings.REDIS_PASSWORD or None,
                        decode_responses=True,
                        socket_timeout=2,
                    )
                    self._redis.ping()
                    logger.info(
                        "[KbCache] Redis 连接成功: {}:{}",
                        settings.REDIS_HOST, settings.REDIS_PORT,
                    )
                except Exception as e:
                    logger.warning("[KbCache] Redis 连接失败，仅用 LRU: {}", str(e)[:100])
                    self._redis = None
        return self._redis

    def _cache_key(self, query: str, product_id: str, coll_type: str, modules: str = "") -> str:
        """生成缓存 key。modules 参与缓存键以避免跨模块缓存污染。"""
        raw = f"{query}|{product_id}|{modules}|{coll_type}"
        return f"kb_cache:{hashlib.md5(raw.encode()).hexdigest()}"

    async def get(self, query: str, product_id: str, coll_type: str, modules: str = "") -> list[dict] | None:
        """查询缓存。返回 None 表示未命中。

        查询顺序：L2 LRU → L1 Redis。
        """
        key = self._cache_key(query, product_id, coll_type, modules)

        # L2 LRU
        with self._lru_lock:
            if key in self._lru:
                self._lru.move_to_end(key)
                try:
                    return json.loads(self._lru[key])
                except Exception:
                    del self._lru[key]

        # L1 Redis
        redis = self._get_redis()
        if redis:
            try:
                cached = redis.get(key)
                if cached:
                    # 回填 L2
                    with self._lru_lock:
                        self._lru[key] = cached
                        if len(self._lru) > self.LRU_MAX_SIZE:
                            self._lru.popitem(last=False)
                    return json.loads(cached)
            except Exception as e:
                logger.debug("[KbCache] Redis 读取失败: {}", str(e)[:80])

        return None

    async def set(self, query: str, product_id: str, coll_type: str, results: list[dict], modules: str = "") -> None:
        """写入缓存（L2 LRU + L1 Redis）。"""
        if not results:
            return
        key = self._cache_key(query, product_id, coll_type, modules)
        try:
            value = json.dumps(results, ensure_ascii=False)
        except Exception:
            return

        # L2 LRU
        with self._lru_lock:
            self._lru[key] = value
            if len(self._lru) > self.LRU_MAX_SIZE:
                self._lru.popitem(last=False)

        # L1 Redis
        redis = self._get_redis()
        if redis:
            try:
                redis.setex(key, self.CACHE_TTL, value)
            except Exception as e:
                logger.debug("[KbCache] Redis 写入失败: {}", str(e)[:80])


kb_cache = KbCache()
