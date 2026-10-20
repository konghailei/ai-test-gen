# -*- coding: utf-8 -*-
"""api_definitions 反臆造校验单测（v2.49）。

覆盖：臆造剔除 / 原文命中保留 / 缺 /api 前缀同构 / 空原文 fail-open / 畸形输入。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agents.req_agent import ReqAgent  # noqa: E402

RAW = """
3.12.6 可观测：系统提供服务器资源监控（`GET /api/observability/system-resources`）、
任务链路追踪（`GET /api/observability/trace/{task_id}`）、
向量出箱状态（`GET /api/observability/vector-outbox`）。
3.3.6 解析任务进行时，系统支持主动取消（`POST /requirements/tasks/{task_id}/cancel`）。
3.12.3 调用保护：大模型调用异常时熔断（连续失败停止调用并自动恢复），支持指数退避重试。
"""


def test_drops_hallucinated_interfaces():
    """只有能力描述、无路径的部分被臆造成接口 → 必须剔除。"""
    items = [
        {"path": "/api/llm/fallback", "method": "POST"},
        {"path": "/api/retrieval/status", "method": "GET"},
    ]
    kept, dropped = ReqAgent._filter_api_definitions(items, RAW)
    assert kept == []
    assert "/api/llm/fallback" in dropped
    assert "/api/retrieval/status" in dropped


def test_keeps_documented_interfaces():
    items = [
        {"path": "/api/observability/vector-outbox", "method": "GET"},
        {"path": "/api/observability/system-resources", "method": "GET"},
    ]
    kept, dropped = ReqAgent._filter_api_definitions(items, RAW)
    assert len(kept) == 2
    assert dropped == []


def test_prefix_variant_and_param_segment_match():
    """原文缺 /api 前缀、且带 {task_id} 占位 → LLM 补全路径后仍应命中。"""
    items = [{"path": "/api/requirements/tasks/{task_id}/cancel", "method": "POST"}]
    kept, dropped = ReqAgent._filter_api_definitions(items, RAW)
    assert len(kept) == 1, f"应命中原文的 /requirements/tasks/{{task_id}}/cancel，实际 dropped={dropped}"


def test_empty_raw_text_fail_open():
    """无原文（历史回填等）→ 不过滤，保持原样。"""
    items = [{"path": "/api/anything"}]
    kept, dropped = ReqAgent._filter_api_definitions(items, "")
    assert len(kept) == 1
    assert dropped == []


def test_malformed_items():
    assert ReqAgent._filter_api_definitions([], "x") == ([], [])
    assert ReqAgent._filter_api_definitions(None, "x") == ([], [])
    kept, dropped = ReqAgent._filter_api_definitions([{"path": "   "}, "裸字符串"], RAW)
    assert kept == []
    assert len(dropped) == 1  # 空 path 被记录；非 dict 直接跳过


if __name__ == "__main__":
    import traceback
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except Exception:
                fails += 1
                print(f"  FAIL {name}")
                traceback.print_exc()
    print(f"\n{'全部通过' if not fails else f'{fails} 项失败'}")
    sys.exit(1 if fails else 0)
