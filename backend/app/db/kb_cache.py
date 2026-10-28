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

import asyncio
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
    def bucket_top_k(top_k: int) -> int:
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
        bucket = self.bucket_top_k(top_k)
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
                # v3.4.1 修复 L-10：redis-py 同步客户端在 async 函数里直调会阻塞事件循环，
                # 统一包 asyncio.to_thread（Redis 抖动时不再拖住整个 API 进程）
                cached = await asyncio.to_thread(redis.get, key)
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
        bucket = self.bucket_top_k(top_k)
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
                # v3.4.1 修复 L-10：同步 setex 包 to_thread，避免阻塞事件循环
                await asyncio.to_thread(redis.setex, key, self.CACHE_TTL, value)
            except Exception as e:
                logger.debug("[KbCache] Redis 写入失败: {}", str(e)[:80])

    def _invalidate_prefix_sync(self, prefix: str) -> int:
        """同步清理指定前缀的缓存条目（L2 LRU + L1 Redis）。

        v3.4.1：抽公共实现。Redis 用的是同步客户端，因此本方法只应在同步上下文
        （to_thread / Celery worker / 同步工具函数）中调用。
        """
        count = 0
        with self._lru_lock:
            keys_to_del = [k for k in self._lru if k.startswith(prefix)]
            for k in keys_to_del:
                del self._lru[k]
            count += len(keys_to_del)

        redis = self._get_redis()
        if redis:
            try:
                for key in redis.scan_iter(match=f"{prefix}*", count=500):
                    redis.delete(key)
                    count += 1
            except Exception as e:
                logger.debug("[KbCache] Redis 失效缓存失败: prefix={}, err={}",
                             prefix, str(e)[:100])
        return count

    def invalidate_by_product_sync(self, product_id: str) -> int:
        """按产品失效缓存的同步版本。

        v3.4.1 修复 M-2：知识库内容变更（需求向量新增/删除）后必须失效缓存，
        否则 24h TTL 内会返回旧结果（新归档需求"搜不到"、已删除需求"还在"）。
        同步版供 milvus_store 等同步代码路径调用（常已被 to_thread 包裹）。
        """
        count = self._invalidate_prefix_sync(f"kb_cache:{product_id}:")
        if count:
            logger.info("[KbCache] 按产品失效缓存(sync): product_id={}, 清理 {} 条",
                        product_id, count)
        return count

    async def invalidate_by_product(self, product_id: str) -> int:
        """v2.8.55 Bug A-2: 按产品失效缓存。

        归档项目时调用，防止 KB augmentation 使用过期缓存（24h TTL 内）。
        返回失效的缓存条目数。

        v3.4.1 修复 L-10：整体放线程池执行（内部含同步 Redis SCAN/DELETE）。
        """
        count = await asyncio.to_thread(
            self._invalidate_prefix_sync, f"kb_cache:{product_id}:"
        )
        logger.info("[KbCache] 按产品失效缓存: product_id={}, 清理 {} 条",
                    product_id, count)
        return count


kb_cache = KbCache()
