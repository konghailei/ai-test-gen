"""分片并行拼接顺序单测（v3.5.9.1 修复锁死，不加载真实模型/不建真进程池）。

背景缺陷（2026-10-13 复核实锤）：旧交织分片 [texts[i::workers] for i ...]
按 chunk 顺序 extend 拼回 → 向量整体错位分配给文本（texts=[t0..t7] workers=2
返回 [v0,v2,v4,v6,v1,v3,v5,v7]）。dev 默认串行不触发，服务器开
DEDUP_EMBED_WORKERS>1 即判定失效。修复 = 连续分块 + 按提交位置回填。

本文件验证（FakePool 注入，秒级）：
1. 顺序保真：out[k] 是 texts[k] 的向量（文本真实索引入向量首维）；
2. 回填完整性：任意长度 / 任意 workers 档位无空位（空位触发 RuntimeError）；
3. 串行路径不受影响（workers=1 直接走 serial_encode）；
4. 不均分场景（len 不足 workers*2 时回退串行）；
5. 修复后的生产路径与串行语义一致性（同文本集逐位相同）。
"""
import pytest

from app.config import settings
from app.db import milvus_store as ms


class _Fut:
    def __init__(self, vecs):
        self._vecs = vecs

    def result(self):
        return self._vecs


class _FakePool:
    """同步假池：encode(texts[a:b]) → [[文本全局索引, 0]]，提交即算。"""

    def __init__(self, texts):
        self._texts = texts
        self.submitted_chunks = []

    def submit(self, fn, args):
        chunk, _model_path = args
        self.submitted_chunks.append(chunk)
        return _Fut([[float(self._texts.index(t)), 0.0] for t in chunk])


def _serial_encode(texts):
    return [[float(i), 0.0] for i in range(len(texts))]


@pytest.fixture
def restore_cfg():
    origin = (settings.DEDUP_EMBED_WORKERS, ms._DEDUP_PROC_POOL)
    yield
    settings.DEDUP_EMBED_WORKERS, ms._DEDUP_PROC_POOL = origin


def _run_with_fake_pool(texts, workers, monkeypatch):
    pool = _FakePool(texts)
    monkeypatch.setattr(ms, "_get_dedup_proc_pool", lambda w: pool)
    monkeypatch.setattr(settings, "DEDUP_EMBED_WORKERS", str(workers))
    return ms._encode_dedup_texts(texts, "/models/fake", _serial_encode), pool


def test_parallel_chunk_order_preserved(monkeypatch, restore_cfg):
    """顺序保真：workers=2/3/4 下 out[k][0] == k（文本索引与向量对齐）。"""
    for workers in (2, 3, 4):
        texts = [f"case-{i}" for i in range(12)]
        out, _ = _run_with_fake_pool(texts, workers, monkeypatch)
        got = [int(v[0]) for v in out]
        assert got == list(range(len(texts))), (
            f"workers={workers} 向量错位: got={got}"
        )


def test_parallel_fills_every_slot(monkeypatch, restore_cfg):
    """回填完整性：任意 n × workers 组合（含不整除）无空位。"""
    for n, workers in [(8, 3), (11, 4), (7, 2), (100, 7)]:
        texts = [f"c{i}" for i in range(n)]
        out, _ = _run_with_fake_pool(texts, workers, monkeypatch)
        assert len(out) == n
        assert all(v is not None for v in out)
        assert [int(v[0]) for v in out] == list(range(n))


def test_serial_path_untouched(monkeypatch, restore_cfg):
    """workers=1（dev 默认）直接走串行，不建池不并行。"""
    monkeypatch.setattr(settings, "DEDUP_EMBED_WORKERS", "")
    pool_calls = {"n": 0}

    def _no_pool(w):
        pool_calls["n"] += 1
        return None

    monkeypatch.setattr(ms, "_get_dedup_proc_pool", _no_pool)
    texts = ["a", "b", "c"]
    out = ms._encode_dedup_texts(texts, "/models/fake", _serial_encode)
    assert [int(v[0]) for v in out] == [0, 1, 2]
    assert pool_calls["n"] == 0  # workers=1 根本不进并行分支


def test_small_batch_falls_back_to_serial(monkeypatch, restore_cfg):
    """len(texts) < workers*2 → 不值得并行，回退串行（生产行为）。"""
    texts = ["only", "three", "cases"]  # 3 < 2*2
    out, pool = _run_with_fake_pool(texts, 2, monkeypatch)
    # workers=2 但 len=3 < 4 → 不进并行分支
    assert [int(v[0]) for v in out] == [0, 1, 2]
    assert pool.submitted_chunks == []


def test_parallel_chunking_is_contiguous(monkeypatch, restore_cfg):
    """连续分块（非交织）：chunk 内文本索引连续、块间无缝衔接。"""
    texts = [f"case-{i}" for i in range(12)]
    _, pool = _run_with_fake_pool(texts, 3, monkeypatch)
    flat = [t for c in pool.submitted_chunks for t in c]
    assert flat == texts  # 拼接后 = 原顺序（连续分块的特征）
    # 每个 chunk 内部索引连续
    for c in pool.submitted_chunks:
        idx = [int(t.split("-")[1]) for t in c]
        assert idx == list(range(idx[0], idx[0] + len(idx)))


def test_parallel_matches_serial_semantics(monkeypatch, restore_cfg):
    """同文本集：并行路径与串行路径向量逐位相同（语义一致性）。"""
    texts = [f"t{i}-payload" for i in range(15)]
    serial = _serial_encode(texts)
    out, _ = _run_with_fake_pool(texts, 4, monkeypatch)
    assert out == serial
