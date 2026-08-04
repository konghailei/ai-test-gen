"""死信队列（Dead Letter Queue）— Redis List 实现。

用于持久化异步任务失败记录，支持事后审计和手动重试。

设计依据：docs/03-case-generation-flow.md §6.6
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import redis as _redis

from app.config import settings

logger = logging.getLogger(__name__)

DLQ_KEY = "aitestgen:dlq:v1"


class DeadLetterQueue:
    """基于 Redis List 的轻量死信队列。

    修复 H7（内存泄漏）：
    - push 后 LTRIM 0..9999，上限 10000 条。
    - push 后 EXPIRE 7 天，防止 Redis List 无界增长 OOM。
    - 提供 `_guard()` 辅助函数保证 push/cleanup 原子性。

    Usage:
        dlq = DeadLetterQueue()
        dlq.push("embedding", {"req_id": "REQ-001"}, "timeout")
        pending = dlq.peek(10)
    """

    # 上限（DLQ 不应无限增长）
    MAX_LEN = 10_000
    # 死信保存天数（7 天）
    TTL_SECONDS = 7 * 24 * 3600

    def __init__(self) -> None:
        try:
            self._r = _redis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
            self._r.ping()
            self._ok = True
        except Exception:
            self._r = None
            self._ok = False
            logger.warning("[DLQ] Redis 不可用，死信队列已禁用")

    def _apply_size_guard(self) -> None:
        """每次 push 后应用大小上限 + TTL 保护（H7）。"""
        if not self._ok:
            return
        try:
            # Redis pipeline：一次性发送 ltrim + expire，减少 RTT
            pipe = self._r.pipeline(transaction=False)
            pipe.ltrim(DLQ_KEY, 0, self.MAX_LEN - 1)
            pipe.expire(DLQ_KEY, self.TTL_SECONDS)
            pipe.execute()
        except Exception as e:
            logger.warning("[DLQ] size guard 执行失败: {}", str(e)[:200])

    def push(
        self,
        task_type: str,
        payload: dict,
        error: str,
        max_retries: int = 3,
    ) -> bool:
        """写入死信记录。

        Args:
            task_type: 任务类型（embedding / milvus_write / pg_write）
            payload: 任务原始参数
            error: 错误信息
            max_retries: 已重试次数

        Returns:
            bool: 写入是否成功
        """
        if not self._ok:
            logger.debug("[DLQ] push 跳过（Redis 不可用）: task_type={}", task_type)
            return False
        try:
            record = {
                "task_type": task_type,
                "payload": payload,
                "error": str(error)[:2000],
                "max_retries": max_retries,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "pending",
            }
            self._r.rpush(DLQ_KEY, json.dumps(record, ensure_ascii=False))
            # H7: 应用 LTRIM + EXPIRE
            self._apply_size_guard()
            logger.info("[DLQ] 写入死信: task_type={}, error={}", task_type, str(error)[:100])
            return True
        except Exception as e:
            logger.error("[DLQ] push 失败: {}", str(e)[:200])
            return False

    def peek(self, limit: int = 20) -> list[dict]:
        """查看死信队列（不移除）。

        Returns:
            list[dict]: 死信记录列表
        """
        if not self._ok:
            return []
        try:
            raw = self._r.lrange(DLQ_KEY, 0, limit - 1)
            return [json.loads(r) for r in raw if r]
        except Exception:
            return []

    def pop(self, limit: int = 1) -> list[dict]:
        """从死信队列取出并移除（用于重试处理）。

        Returns:
            list[dict]: 取出的记录
        """
        if not self._ok:
            return []
        items = []
        for _ in range(limit):
            raw = self._r.lpop(DLQ_KEY)
            if not raw:
                break
            items.append(json.loads(raw))
        if items:
            logger.info("[DLQ] 取出 {} 条死信待重试", len(items))
        return items

    def count(self) -> int:
        """死信队列长度。"""
        if not self._ok:
            return 0
        try:
            return self._r.llen(DLQ_KEY) or 0
        except Exception:
            return 0


# 全局单例
dlq = DeadLetterQueue()
