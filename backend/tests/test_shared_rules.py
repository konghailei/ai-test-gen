# -*- coding: utf-8 -*-
"""v2.48 共享业务规则逐条编译层单测。

覆盖（规则层确定性产出 → 强断言）：
  1. 向量 ID 确定性 + 规范化（空白/标点变体判为同一条）
  2. 规则抽取：dict/list/标量三形态、去重、跳过空值
  3. 最小 SRR 经 _srr_to_text 渲染后**只含本规则正文**（不掺其他规则 → 无稀释）
  4. 出箱 payload 必备 metadata（type/status=archived/product_id）
  5. 与 fp_crsr 的规范化口径一致（同一条规则两侧判同）
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.shared_rules import (  # noqa: E402
    iter_shared_rules,
    shared_rule_outbox_payload,
    shared_rule_srr,
    shared_rule_vector_id,
)
from app.db.milvus_store import MilvusVectorStore  # noqa: E402

# _srr_to_text 只用 self._SRR_TEXT_MAX_CHARS，用轻量桩即可
_STUB = types.SimpleNamespace(_SRR_TEXT_MAX_CHARS=MilvusVectorStore._SRR_TEXT_MAX_CHARS)

RULE_LOCK = "用户密码加盐哈希存储，登录连续失败5次锁定30分钟"
RULE_TOKEN = "会话令牌有效期8小时，到期即时失效"
RULE_RATE = "接口默认限流每分钟60次，超限返回429"

CFI_SRR = {
    "cross_fp_interactions": {
        "shared_business_rules": [
            {"id": "BR-1", "rule": RULE_LOCK, "applies_to": ["用户与角色管理"]},
            {"id": "BR-2", "rule": RULE_TOKEN},
            RULE_RATE,  # 标量形态（v2.40 前后数据不一致）
            {"id": "BR-9", "rule": "   "},  # 空规则 → 跳过
            {"id": "BR-1b", "rule": RULE_LOCK},  # 重复 → 去重
        ],
        # v2.48：global_constraints 同样逐条编译
        "global_constraints": [
            {"rule": "事件循环采样间隔为100ms"},
            {"rule": "stale任务阈值为60分钟"},
            {"rule": "所有接口：请求频率不做额外限制（原文未提及）"},  # 解析器元注释 → 跳过
        ],
    }
}


def test_vector_id_deterministic_and_normalized():
    a = shared_rule_vector_id("PROD-1", RULE_LOCK)
    b = shared_rule_vector_id("PROD-1", "用户密码加盐哈希存储，登录连续失败5次锁定30分钟")
    # 空白/标点变体应判为同一条
    c = shared_rule_vector_id("PROD-1", "用户密码加盐哈希存储 登录连续失败5次锁定30分钟。")
    assert a == b, "同一文本必须得到同一 ID"
    assert a == c, "去空白与标点后应判为同一条"
    assert a.startswith("SRULE-"), "ID 前缀必须为 SRULE-"
    # 不同产品隔离
    assert shared_rule_vector_id("PROD-2", RULE_LOCK) != a, "跨产品必须隔离"


def test_iter_shared_rules_shapes_dedupe_and_skip_empty():
    rules = iter_shared_rules(CFI_SRR)
    texts = [r["text"] for r in rules]
    assert texts == [RULE_LOCK, RULE_TOKEN, RULE_RATE,
                     "事件循环采样间隔为100ms", "stale任务阈值为60分钟"], \
        f"应保序去重、跳空、跳元注释、并纳入全局约束，实际={texts}"
    assert rules[0]["rule_id"] == "BR-1"
    assert rules[0]["applies_to"] == "用户与角色管理", "applies_to 列表应拍平为字符串"
    assert rules[1]["applies_to"] == "", "缺失 applies_to 应回落空串"
    # 来源子块标记（便于追溯/筛选）
    assert rules[0]["block"] == "shared_business_rules"
    assert rules[3]["block"] == "global_constraints"


def test_iter_shared_rules_skips_meta_comments():
    """解析器元注释（非规则本身）不得当规则嵌入，否则污染知识库。"""
    srr = {"cross_fp_interactions": {"global_constraints": [
        {"rule": "事件循环采样间隔为100ms"},
        {"rule": "所有接口：请求频率不做额外限制（原文未提及）"},
        {"rule": "某些字段：文档未提及取值范围"},
    ]}}
    texts = [r["text"] for r in iter_shared_rules(srr)]
    assert texts == ["事件循环采样间隔为100ms"], f"元注释应被过滤，实际={texts}"


def test_iter_shared_rules_tolerates_missing_block():
    assert iter_shared_rules({}) == []
    assert iter_shared_rules({"cross_fp_interactions": None}) == []
    assert iter_shared_rules({"cross_fp_interactions": {"shared_business_rules": "单条字符串"}})[0]["text"] \
        == "单条字符串", "非列表形态应兼容为单条"


def test_rule_srr_text_contains_only_own_rule():
    """核心断言：单条规则的向量文本只含本规则，不掺其他规则（→ 不被稀释）。"""
    text = MilvusVectorStore._srr_to_text(_STUB, shared_rule_srr(RULE_LOCK))
    assert RULE_LOCK in text, "向量文本必须含规则正文"
    assert RULE_TOKEN not in text, "不得掺入其它规则（否则语义稀释）"
    assert RULE_RATE not in text, "不得掺入其它规则（否则语义稀释）"
    assert "共享业务规则" in text, "标题应表明来源类型"


def test_payload_required_metadata():
    p = shared_rule_outbox_payload(
        product_id="PROD-1", rule_text=RULE_LOCK,
        source_req_id="REQ-1", source_req_title="基线需求",
        version_id="1.0-x", project_id="PROJ-1",
    )
    md = p["metadata"]
    assert md["type"] == "shared_rule"
    # 缺 status=archived 会导致向量存在但永远检索不到（检索侧硬过滤）
    assert md["status"] == "archived"
    assert md["product_id"] == "PROD-1"
    assert md["rule_text"] == RULE_LOCK
    assert "requirement_json" in p, "投递体须为 SRR 形态（复用 add_requirement）"


def test_norm_rule_consistent_with_crsr():
    """与 fp_crsr 的规范化口径一致——同一条规则在两侧判同。"""
    from app.core.fp_crsr import _norm_text
    from app.core.shared_rules import _norm_rule
    samples = [
        RULE_LOCK,
        "  会话令牌：有效期 8 小时（到期即失效）  ",
        "接口默认限流每分钟60次,超限返回429",
    ]
    for s in samples:
        assert _norm_rule(s) == _norm_text(s), f"规范化口径不一致: {s!r}"


def test_filter_shared_rule_switch_behavior():
    """灰度开关：False 时剔除 shared_rule（两种 type 形态都识别），True 时原样返回。"""
    from app.core.shared_rules import filter_shared_rule_switch
    srcs = [
        {"id": "SRULE-1", "type": "shared_rule", "metadata": {"type": "shared_rule"}},
        {"id": "REQ-1", "type": "requirement", "metadata": {"type": "requirement"}},
        {"id": "SRULE-2", "metadata": {"type": "shared_rule"}},   # 仅 metadata.type（clarify 形态）
        {"id": "FP-1", "metadata": {"type": "functional_point"}},
    ]
    # 默认（不传 enabled）读 settings，默认 True → 不过滤
    assert [s["id"] for s in filter_shared_rule_switch(srcs)] == \
        ["SRULE-1", "REQ-1", "SRULE-2", "FP-1"]
    on = filter_shared_rule_switch(srcs, enabled=True)
    assert [s["id"] for s in on] == ["SRULE-1", "REQ-1", "SRULE-2", "FP-1"]
    off = filter_shared_rule_switch(srcs, enabled=False)
    assert [s["id"] for s in off] == ["REQ-1", "FP-1"], \
        "关闭时必须剔除全部 shared_rule（含仅 metadata.type 的形态）"


def test_switch_consumed_by_both_call_sites():
    """回归守卫：kqa 与 clarify 必须共用同一开关过滤实现。

    历史缺陷（假配置）：开关只在 knowledge_qa 一处落地，clarify_kb 的 doc_type
    硬编码含 shared_rule 且无过滤 → 配置 docstring 声明「kqa/clarify 两处」实际
    只生效一处，「一键回退」在澄清链路失效。此守卫防止再次退化为「各写一份」。
    """
    import inspect

    from app.agents import clarify_kb as _ckb
    from app.api import knowledge_qa as _kqa
    assert "filter_shared_rule_switch" in inspect.getsource(_kqa), \
        "KB 问答检索必须消费灰度开关（filter_shared_rule_switch）"
    assert "filter_shared_rule_switch" in inspect.getsource(_ckb), \
        "澄清检索必须消费灰度开关（filter_shared_rule_switch）——否则回退在澄清链路失效"


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  [PASS] {name}")
            except AssertionError as e:
                fails += 1
                print(f"  [FAIL] {name}: {e}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  [ERROR] {name}: {type(e).__name__}: {e}")
    print(f"\n共享规则单测: {'全部通过' if fails == 0 else f'{fails} 项失败'}")
    sys.exit(1 if fails else 0)
