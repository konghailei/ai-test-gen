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

    def _cache_key(
        self,
        query: str,
        product_id: str,
        coll_type: str,
        modules: str = "",
        doc_type: str = "",
        status: str = "",
        top_k_bucket: int = 0,
    ) -> str:
        """生成缓存 key。
        - 修复 H8：把 status / top_k_bucket（top_k 分桶）纳入 key，防止不同筛选条件/返回数量的结果相互串扰。
        - top_k_bucket 分桶（如 10/20/40/80）：避免 top_k=11 与 top_k=12 各存一份缓存，控制缓存膨胀。
        """
        raw = f"{query}|{product_id}|{modules}|{doc_type}|{coll_type}|{status}|{top_k_bucket}"
        # v2.8.55 Bug A-2: product_id 作为 key 前缀，支持按产品 SCAN 失效缓存
        return f"kb_cache:{product_id}:{hashlib.md5(raw.encode()).hexdigest()}"

    @staticmethod
    def _bucket_top_k(top_k: int) -> int:
        """把任意 top_k 映射到 {10, 20, 40, 80} 桶，避免因 top_k 微小差异导致缓存命中率过低。"""
        if top_k <= 10:
            return 10
        if top_k <= 20:
            return 20
        if top_k <= 40:
            return 40
        return 80

    async def get(
        self,
        query: str,
        product_id: str,
        coll_type: str,
        modules: str = "",
        doc_type: str = "",
        status: str = "",
        top_k: int = 40,
    ) -> list[dict] | None:
        """查询缓存。返回 None 表示未命中。

        查询顺序：L2 LRU → L1 Redis。
        """
        bucket = self._bucket_top_k(top_k)
        key = self._cache_key(query, product_id, coll_type, modules, doc_type, status or "", bucket)

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

    async def set(
        self,
        query: str,
        product_id: str,
        coll_type: str,
        results: list[dict],
        modules: str = "",
        doc_type: str = "",
        status: str = "",
        top_k: int = 40,
    ) -> None:
        """写入缓存（L2 LRU + L1 Redis）。"""
        if not results:
            return
        bucket = self._bucket_top_k(top_k)
        key = self._cache_key(query, product_id, coll_type, modules, doc_type, status or "", bucket)
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

    async def invalidate_by_product(self, product_id: str) -> int:
        """v2.8.55 Bug A-2: 按产品失效缓存。

        归档项目时调用，防止 KB augmentation 使用过期缓存（24h TTL 内）。
        返回失效的缓存条目数。
        """
        count = 0
        prefix = f"kb_cache:{product_id}:"

        # L2 LRU: 遍历删除匹配前缀的 key
        with self._lru_lock:
            keys_to_del = [k for k in self._lru if k.startswith(prefix)]
            for k in keys_to_del:
                del self._lru[k]
            count += len(keys_to_del)

        # L1 Redis: SCAN + DELETE
        redis = self._get_redis()
        if redis:
            try:
                deleted = 0
                for key in redis.scan_iter(match=f"{prefix}*", count=500):
                    redis.delete(key)
                    deleted += 1
                count += deleted
            except Exception as e:
                logger.warning("[KbCache] Redis 失效缓存失败: product_id={}, err={}",
                               product_id, str(e)[:100])

        logger.info("[KbCache] 按产品失效缓存: product_id={}, 清理 {} 条",
                    product_id, count)
        return count


kb_cache = KbCache()
