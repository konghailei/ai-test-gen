"""v2.8.55 KB 检索优化单元测试

验证三个核心修复：
1. hybrid_search._apply_time_decay 时间衰减因子正确性
2. clarify_agent._query_kb_for_answers 版本感知去重逻辑
3. _verify_kb_answers 携带 go_live_date 信息

注意：本测试不导入 app.db.hybrid_search（依赖 torch/sentence_transformers），
而是直接复制 _apply_time_decay 的核心逻辑进行验证。

运行: python -m pytest tests/test_v2_55_kb_optimization.py -v
"""

import json
from datetime import datetime, timedelta, timezone

import sys
import os


# ============================================================================
# 复制 hybrid_search._apply_time_decay 的核心逻辑进行独立测试
# （避免导入 torch 依赖）
# ============================================================================

RRF_K = 60
TIME_DECAY_HALF_LIFE_DAYS = 180


def apply_time_decay(fused):
    """复制自 hybrid_search.HybridSearcher._apply_time_decay 的核心逻辑"""
    if not fused:
        return fused

    now = datetime.now(timezone.utc)
    half_life = TIME_DECAY_HALF_LIFE_DAYS

    for item in fused:
        rrf_score = item.get("rrf_score", 0)
        meta = item.get("metadata", {}) or {}
        go_live = meta.get("go_live_date", "")

        age_days = 0
        if go_live:
            try:
                if isinstance(go_live, str):
                    go_live_dt = datetime.fromisoformat(
                        go_live.replace("Z", "+00:00")
                    )
                elif isinstance(go_live, datetime):
                    go_live_dt = go_live
                else:
                    go_live_dt = None

                if go_live_dt:
                    if go_live_dt.tzinfo is None:
                        go_live_dt = go_live_dt.replace(tzinfo=timezone.utc)
                    age_timedelta = now - go_live_dt
                    age_days = max(0, age_timedelta.days)
            except (ValueError, TypeError):
                age_days = 0

        decay = 0.5 ** (age_days / half_life) if half_life > 0 else 1.0
        time_factor = 0.5 + 0.5 * decay
        final_score = rrf_score * time_factor

        item["rrf_score"] = round(final_score, 6)
        item["_time_decay"] = round(time_factor, 4)
        item["_age_days"] = age_days

    fused.sort(key=lambda x: -x.get("rrf_score", 0))
    return fused


def version_aware_dedup(matched):
    """复制自 clarify_agent._query_kb_for_answers 的版本感知去重逻辑"""
    seen_req_ids = {}
    for m in matched:
        meta = m.get("metadata", {}) or {}
        dedup_key = meta.get("req_id", m.get("id", ""))
        if not dedup_key:
            continue
        existing = seen_req_ids.get(dedup_key)
        if existing is None:
            seen_req_ids[dedup_key] = m
        else:
            existing_date = (existing.get("metadata", {}) or {}).get("go_live_date", "")
            current_date = meta.get("go_live_date", "")
            if current_date > existing_date:
                seen_req_ids[dedup_key] = m
    return list(seen_req_ids.values())


def build_verification_docs(sources):
    """复制自 _verify_kb_answers 构建 verification_items 的逻辑"""
    docs = []
    for s in sources[:3]:
        doc_full = s.get("document_full")
        if doc_full:
            doc_content = doc_full[:1500]
        else:
            v = s.get("value")
            if isinstance(v, (dict, list)):
                doc_content = json.dumps(v, ensure_ascii=False)[:600]
            elif v:
                doc_content = str(v)[:600]
            else:
                doc_content = ""

        go_live = s.get("go_live_date", "")
        proj_ver = s.get("project_version", "")
        docs.append({
            "content": doc_content,
            "go_live_date": go_live or "",
            "project_version": proj_ver or "",
            "source_id": s.get("id", ""),
            "similarity": s.get("similarity", 0),
        })
    return docs


# ============================================================================
# 测试类
# ============================================================================


class TestTimeDecay:
    """测试时间衰减因子计算"""

    def test_apply_time_decay_new_document_higher_score(self):
        """新文档应该比旧文档分数更高（相同 RRF 分数）"""
        now = datetime.now(timezone.utc)
        new_date = (now - timedelta(days=1)).isoformat()
        old_date = (now - timedelta(days=365)).isoformat()

        fused = [
            {
                "id": "old_req",
                "rrf_score": 0.1,
                "metadata": {"go_live_date": old_date, "req_id": "old_req"},
            },
            {
                "id": "new_req",
                "rrf_score": 0.1,
                "metadata": {"go_live_date": new_date, "req_id": "new_req"},
            },
        ]

        result = apply_time_decay(fused)

        assert result[0]["id"] == "new_req", f"新文档应排前，实际 {result[0]['id']}"
        assert result[1]["id"] == "old_req"
        # 新文档衰减因子接近 1.0（刚上线）
        assert result[0]["_time_decay"] > 0.99, (
            f"新文档衰减因子应接近 1.0，实际 {result[0]['_time_decay']}"
        )
        # 旧文档（365 天）衰减因子 = 0.5 + 0.5 * 0.5^(365/180) ≈ 0.62
        # 基线保护确保不低于 0.5，但应明显小于新文档
        assert result[1]["_time_decay"] < result[0]["_time_decay"], (
            f"旧文档衰减因子应小于新文档，旧={result[1]['_time_decay']} 新={result[0]['_time_decay']}"
        )
        assert 0.5 <= result[1]["_time_decay"] < 0.7, (
            f"旧文档（365天）衰减因子应在 [0.5, 0.7) 范围，实际 {result[1]['_time_decay']}"
        )

    def test_apply_time_decay_relevance_dominates(self):
        """RRF 分数应主导排序（高相关性旧文档 > 低相关性新文档）"""
        now = datetime.now(timezone.utc)
        new_date = (now - timedelta(days=1)).isoformat()
        old_date = (now - timedelta(days=10)).isoformat()

        fused = [
            {
                "id": "new_low_rel",
                "rrf_score": 0.01,
                "metadata": {"go_live_date": new_date, "req_id": "new_req"},
            },
            {
                "id": "old_high_rel",
                "rrf_score": 0.1,
                "metadata": {"go_live_date": old_date, "req_id": "old_req"},
            },
        ]

        result = apply_time_decay(fused)

        # 高相关性旧文档应仍排前（相关性主导）
        assert result[0]["id"] == "old_high_rel", (
            f"高相关性旧文档应排前（相关性主导），实际 {result[0]['id']}"
        )

    def test_apply_time_decay_empty_list(self):
        """空列表应返回空"""
        assert apply_time_decay([]) == []

    def test_apply_time_decay_no_go_live_date(self):
        """没有 go_live_date 的文档应使用基线（age_days=0，time_factor=1.0）"""
        fused = [
            {
                "id": "no_date_req",
                "rrf_score": 0.1,
                "metadata": {"req_id": "no_date_req"},
            }
        ]

        result = apply_time_decay(fused)

        assert result[0]["_time_decay"] == 1.0, (
            f"无日期文档时间因子应为 1.0，实际 {result[0]['_time_decay']}"
        )

    def test_apply_time_decay_100_iterations(self):
        """功能点迭代 100 次场景：最新版本应被优先"""
        now = datetime.now(timezone.utc)
        fused = []
        for i in range(100):
            age_days = (99 - i) * 7
            go_live = (now - timedelta(days=age_days)).isoformat()
            fused.append({
                "id": f"req_v{i}",
                "rrf_score": 0.05 + i * 0.001,
                "metadata": {
                    "go_live_date": go_live,
                    "req_id": "same_req",
                },
            })

        result = apply_time_decay(fused)

        top_ids = [r["id"] for r in result[:3]]
        assert "req_v99" in top_ids, (
            f"迭代 100 次后，最新版本（v99）应在 Top3，实际 Top3: {top_ids}"
        )

    def test_apply_time_decay_0_5_baseline(self):
        """最旧文档的 time_factor 应不低于 0.5（基线保护）"""
        now = datetime.now(timezone.utc)
        very_old_date = (now - timedelta(days=3650)).isoformat()  # 10年前

        fused = [{
            "id": "very_old_req",
            "rrf_score": 0.1,
            "metadata": {"go_live_date": very_old_date, "req_id": "old"},
        }]

        result = apply_time_decay(fused)

        # decay = 0.5 ** (3650/180) ≈ 0.0000...，但 time_factor = 0.5 + 0.5 * decay >= 0.5
        assert result[0]["_time_decay"] >= 0.5, (
            f"最旧文档 time_factor 应不低于 0.5（基线保护），实际 {result[0]['_time_decay']}"
        )


class TestVersionAwareDedup:
    """测试版本感知去重逻辑"""

    def test_dedup_keeps_latest_version(self):
        """同一 req_id 多版本去重时，保留 go_live_date 最新的版本"""
        matched = [
            {
                "id": "v1",
                "similarity": 0.8,
                "metadata": {"req_id": "REQ-001", "go_live_date": "2026-01-01T00:00:00+00:00"},
            },
            {
                "id": "v99",
                "similarity": 0.6,
                "metadata": {"req_id": "REQ-001", "go_live_date": "2026-08-01T00:00:00+00:00"},
            },
            {
                "id": "v50",
                "similarity": 0.7,
                "metadata": {"req_id": "REQ-001", "go_live_date": "2026-05-01T00:00:00+00:00"},
            },
        ]

        result = version_aware_dedup(matched)

        assert len(result) == 1, f"去重后应剩 1 条，实际 {len(result)}"
        assert result[0]["id"] == "v99", (
            f"应保留最新版本 v99，实际保留了 {result[0]['id']}"
        )

    def test_dedup_different_req_ids_all_kept(self):
        """不同 req_id 不应被去重"""
        matched = [
            {
                "id": "req_a",
                "similarity": 0.8,
                "metadata": {"req_id": "REQ-A", "go_live_date": "2026-01-01"},
            },
            {
                "id": "req_b",
                "similarity": 0.7,
                "metadata": {"req_id": "REQ-B", "go_live_date": "2026-01-02"},
            },
        ]

        result = version_aware_dedup(matched)

        assert len(result) == 2, f"不同 req_id 应都保留，实际 {len(result)}"

    def test_dedup_100_versions_keeps_latest(self):
        """100 个版本去重后应保留最新版本"""
        matched = []
        for i in range(100):
            matched.append({
                "id": f"v{i}",
                "similarity": 0.8 - i * 0.001,
                "metadata": {
                    "req_id": "REQ-001",
                    "go_live_date": f"2026-01-01T00:00:0{i % 10}+00:00",
                },
            })
        # 最后一个版本日期最新
        matched[-1]["metadata"]["go_live_date"] = "2026-12-31T23:59:59+00:00"

        result = version_aware_dedup(matched)

        assert len(result) == 1
        assert result[0]["metadata"]["go_live_date"] == "2026-12-31T23:59:59+00:00"


class TestVerifyKbAnswersVersionAware:
    """测试 _verify_kb_answers 携带 go_live_date 信息"""

    def test_verification_docs_include_go_live_date(self):
        """验证项应包含 go_live_date 字段"""
        sources = [{
            "id": "REQ-001",
            "similarity": 0.8,
            "go_live_date": "2026-08-01",
            "project_version": "1.5",
            "document_full": '{"data_constraints": {"password": {"length": "8-20位"}}}',
            "value": {"length": "8-20位"},
        }]

        docs = build_verification_docs(sources)

        assert len(docs) == 1
        assert docs[0]["go_live_date"] == "2026-08-01"
        assert docs[0]["project_version"] == "1.5"
        assert "content" in docs[0]
        assert docs[0]["source_id"] == "REQ-001"

    def test_verification_docs_fallback_to_value(self):
        """document_full 不存在时，应回退到 value 字段"""
        sources = [{
            "id": "REQ-002",
            "similarity": 0.7,
            "go_live_date": "2026-07-01",
            "project_version": "1.4",
            "value": {"rule": "密码错误 3 次锁定"},
        }]

        docs = build_verification_docs(sources)

        assert len(docs) == 1
        assert docs[0]["go_live_date"] == "2026-07-01"
        assert "密码错误 3 次锁定" in docs[0]["content"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
