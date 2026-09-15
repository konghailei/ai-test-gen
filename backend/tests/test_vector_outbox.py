"""向量出箱的离线用例（纯函数部分）。

⚠️ 刻意**不 import `app.db.models` / `app.db.database`**：这两个模块在导入时就要求
`DATABASE_URL`，一旦引入，CI 的 unit 层（无外部服务）会直接收集失败。
需要真库/真 Milvus 的行为验证放在 `backend/_probe_outbox.py`（容器内跑）。
"""
import pytest

from app.core.vector_outbox import (
    OUTBOX_BACKOFF_CAP,
    content_hash_of,
    next_retry_delay,
)


def test_content_hash_is_order_insensitive_and_stable():
    a = {"b": 2, "a": 1, "nested": {"y": [1, 2]}}
    b = {"nested": {"y": [1, 2]}, "a": 1, "b": 2}
    assert content_hash_of(a) == content_hash_of(b)
    assert len(content_hash_of(a)) == 64


def test_content_hash_changes_with_content():
    assert content_hash_of({"title": "A"}) != content_hash_of({"title": "B"})
    # 内容变化就要检测出来（用于识别"向量是不是旧版本"）
    assert content_hash_of({"title": "A", "steps": [1]}) != content_hash_of({"title": "A"})


def test_content_hash_handles_empty():
    assert content_hash_of(None) == content_hash_of({})


@pytest.mark.parametrize("retry,expected", [
    (0, 0), (1, 5), (2, 10), (3, 20), (4, 40), (5, 80), (6, 160), (7, 300), (8, 300),
])
def test_backoff_is_exponential_and_capped(retry, expected):
    assert next_retry_delay(retry) == expected


def test_backoff_never_exceeds_cap():
    for r in range(1, 30):
        assert next_retry_delay(r) <= OUTBOX_BACKOFF_CAP


def test_backoff_is_monotonic():
    seq = [next_retry_delay(r) for r in range(1, 10)]
    assert seq == sorted(seq)
