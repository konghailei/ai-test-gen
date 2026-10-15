"""KB 问答相关度门槛与检索来源标记单测（v4.0.1 / 缺陷 S6）。

背景（TEST_PLAN §F S6）：原实现把检索结果无条件截 top-8 当作「参考来源」，
未覆盖的提问会返回 8 条相似度 0.43-0.47 的噪声来源，而答案正文却是
「知识库中未找到相关内容」——前端展示「参考来源」却无有效内容，自相矛盾。

守卫目标：
  1. 向量路（COSINE 余弦）低于门槛的来源必须被剔除；
  2. **BM25 降级路不受门槛约束**（其 similarity 是 PG ts_rank，量纲不可比；
     需求要求 Milvus 不可用时降级为 PG 结果，不能把它滤空）；
  3. 门槛可置 0 回退旧行为；
  4. `_rrf_fuse` 正确标记 `_score_kind`（向量优先于 BM25）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))  # 支持 python tests/xxx.py 直跑

from app.config import settings
from app.api.knowledge_qa import _filter_by_relevance
from app.db.hybrid_search import hybrid_searcher


def _src(id_: str, sim: float, kind: str | None = "vector") -> dict:
    d: dict = {"id": id_, "similarity": sim, "document": f"doc-{id_}"}
    if kind is not None:
        d["_score_kind"] = kind
    return d


def test_drops_vector_sources_below_threshold(monkeypatch):
    monkeypatch.setattr(settings, "KB_QA_MIN_SIMILARITY", 0.5)
    srcs = [_src("A", 0.746), _src("B", 0.44), _src("C", 0.47), _src("D", 0.51)]
    kept, dropped = _filter_by_relevance(srcs)
    assert [s["id"] for s in kept] == ["A", "D"]
    assert dropped == 2


def test_keeps_bm25_fallback_regardless_of_ts_rank(monkeypatch):
    """Milvus 不可用时 BM25 降级路必须存活（ts_rank 天然很小）。"""
    monkeypatch.setattr(settings, "KB_QA_MIN_SIMILARITY", 0.5)
    srcs = [_src("V", 0.44), _src("B", 0.03, kind="bm25")]
    kept, dropped = _filter_by_relevance(srcs)
    assert [s["id"] for s in kept] == ["B"]
    assert dropped == 1


def test_untagged_sources_treated_as_vector(monkeypatch):
    """历史缓存（无 _score_kind）按向量路处理，保证老缓存也受门槛约束。"""
    monkeypatch.setattr(settings, "KB_QA_MIN_SIMILARITY", 0.5)
    srcs = [_src("OLD", 0.42, kind=None)]
    kept, dropped = _filter_by_relevance(srcs)
    assert kept == []
    assert dropped == 1


def test_threshold_zero_disables_filtering(monkeypatch):
    monkeypatch.setattr(settings, "KB_QA_MIN_SIMILARITY", 0.0)
    srcs = [_src("A", 0.1), _src("B", 0.9)]
    kept, dropped = _filter_by_relevance(srcs)
    assert len(kept) == 2
    assert dropped == 0


def test_missing_similarity_is_treated_as_zero(monkeypatch):
    monkeypatch.setattr(settings, "KB_QA_MIN_SIMILARITY", 0.5)
    srcs = [{"id": "N", "_score_kind": "vector"}]  # 无 similarity 字段
    kept, dropped = _filter_by_relevance(srcs)
    assert kept == []
    assert dropped == 1


def test_rrf_fuse_marks_score_kind():
    """向量命中优先标记为 vector；仅 BM25 命中标记为 bm25。"""
    vector_results = [{"id": "V1", "similarity": 0.8}, {"id": "SHARED", "similarity": 0.7}]
    bm25_results = [{"id": "SHARED", "similarity": 0.05}, {"id": "B1", "similarity": 0.03}]
    fused = hybrid_searcher._rrf_fuse(vector_results, bm25_results)
    kinds = {item["id"]: item.get("_score_kind") for item in fused}
    assert kinds["V1"] == "vector"
    assert kinds["SHARED"] == "vector"   # 两路都命中 → 向量口径优先
    assert kinds["B1"] == "bm25"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
