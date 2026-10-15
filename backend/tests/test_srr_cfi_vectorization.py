"""v4.1.1 缺陷 T4 + 「生成退化为针对原文」回归用例。

覆盖两处修复：
  ① `milvus_store._srr_to_text`：v2.34 起跨 FP 共享业务规则迁入
     `cross_fp_interactions.shared_business_rules`，此前该块被整段忽略
     → 权限/锁定/令牌等全局规则不进 L0 向量 → 知识问答系统性漏召。
  ② `prompts.gen_agent.sanitize_srr_for_generation` / `build_user_prompt`：
     SRR 随库携带 `_raw_text`（原文全文）与 legacy `additional_details`
     → json.dumps(srr) 把原文整段注入生成 prompt → 生成退化为「照抄原文」。

另覆盖：FP 分片通道下「跨 FP 共享规则」经 shared_rules 参数显式下发。
"""
from __future__ import annotations

import pytest

from app.db.milvus_store import MilvusVectorStore, cfi_to_text_parts, srr_rule_text
from app.prompts.gen_agent import build_user_prompt, sanitize_srr_for_generation

# ---------------------------------------------------------------------------
# 纯函数：CFI → 文本片段
# ---------------------------------------------------------------------------


def test_srr_rule_text_handles_dict_and_scalar():
    assert srr_rule_text({"rule": "登录连续失败5次锁定账号30分钟"}) == "登录连续失败5次锁定账号30分钟"
    assert srr_rule_text({"rule_desc": "密码加盐哈希"}) == "密码加盐哈希"
    assert srr_rule_text("会话令牌有效期8小时") == "会话令牌有效期8小时"
    assert srr_rule_text(None) == ""


def test_cfi_to_text_parts_dict_shaped_shared_rules():
    """新数据形态：shared_business_rules = [{id, rule, applied_fps}]。"""
    cfi = {
        "shared_business_rules": [
            {"id": "SBR-001", "rule": "登录连续失败5次锁定账号30分钟", "applied_fps": ["登录", "风控"]},
            {"id": "SBR-002", "rule": "会话令牌有效期8小时", "applied_fps": ["登录", "鉴权"]},
        ]
    }
    parts = cfi_to_text_parts(cfi)
    joined = "\n".join(parts)
    assert any(p.startswith("共享业务规则: ") for p in parts)
    assert "登录连续失败5次锁定账号30分钟" in joined
    assert "会话令牌有效期8小时" in joined


def test_cfi_to_text_parts_string_shaped_shared_rules():
    """历史数据形态：shared_business_rules = ["...", "..."]（v2.40 前后不一致）。"""
    cfi = {"shared_business_rules": ["用户密码加盐哈希存储", "登录连续失败5次锁定账号30分钟"]}
    joined = "\n".join(cfi_to_text_parts(cfi))
    assert "用户密码加盐哈希存储" in joined
    assert "登录连续失败5次锁定账号30分钟" in joined


def test_cfi_to_text_parts_renders_all_four_sub_blocks():
    cfi = {
        "shared_business_rules": ["全局限流每分钟60次"],
        "global_constraints": ["所有接口响应符合全局约束"],
        "cross_fp_flows": [
            {"name": "需求流转", "source_fp": "上传", "target_fp": "解析",
             "interaction_type": "sequence", "description": "上传后触发解析"}
        ],
        "fp_contracts": [
            {"provider_fp": "变更", "consumer_fp": "影响分析",
             "data_fields": ["req_id", "变更类型"], "contract_desc": "变更后传 req_id"}
        ],
    }
    parts = cfi_to_text_parts(cfi)
    labels = {p.split(":", 1)[0] for p in parts}
    assert {"共享业务规则", "全局约束", "跨功能点流程", "功能点契约"} <= labels


@pytest.mark.parametrize("cfi", [None, {}, [], "oops", 0])
def test_cfi_to_text_parts_tolerates_empty_or_malformed(cfi):
    assert cfi_to_text_parts(cfi) == []


# ---------------------------------------------------------------------------
# T4：_srr_to_text 必须纳入 cross_fp_interactions（真实缺陷回归）
# ---------------------------------------------------------------------------


def _store() -> MilvusVectorStore:
    """不触发 __init__ 的轻量实例（_srr_to_text 只依赖类属性）。"""
    return MilvusVectorStore.__new__(MilvusVectorStore)


def test_srr_to_text_includes_cfi_shared_rules():
    """T4 核心回归：v2.34 迁到 CFI 的共享规则必须出现在向量文本里。

    修复前：顶层 business_rules 恒为 None（新数据）且 CFI 被整段忽略
    → 「锁定 30 分钟」永远检索不到（Q2 实测 top-1 相似度仅 0.3896）。
    """
    srr = {
        "requirement": {"title": "用户登录"},
        "cross_fp_interactions": {
            "shared_business_rules": [
                {"id": "SBR-001", "rule": "登录连续失败5次锁定账号30分钟"},
                {"id": "SBR-002", "rule": "会话令牌有效期8小时"},
            ]
        },
    }
    text = _store()._srr_to_text(srr)
    assert "登录连续失败5次锁定账号30分钟" in text
    assert "会话令牌有效期8小时" in text
    assert "共享业务规则" in text


def test_srr_to_text_keeps_legacy_top_level_business_rules():
    """兼容旧 SRR：顶层 business_rules（dict 条目）仍需可读渲染。"""
    srr = {
        "requirement": {"title": "订单"},
        "business_rules": [{"id": "BR-01", "rule": "订单金额 = 商品总价 + 运费 - 优惠金额"}],
    }
    text = _store()._srr_to_text(srr)
    assert "订单金额 = 商品总价 + 运费 - 优惠金额" in text
    # 不应退化为 Python dict repr（旧实现 str(dict) 产物）
    assert "{'id':" not in text


def test_srr_to_text_unchanged_when_no_cfi():
    srr = {"requirement": {"title": "简单需求"}, "user_roles": ["管理员"]}
    text = _store()._srr_to_text(srr)
    assert "需求标题: 简单需求" in text
    assert "共享业务规则" not in text


# ---------------------------------------------------------------------------
# 生成侧：SRR 净化（原文不得进 prompt）
# ---------------------------------------------------------------------------


def test_sanitize_strips_raw_text_and_internal_keys():
    srr = {
        "requirement": {"title": "登录"},
        "_raw_text": "# 用户登录功能需求\n\n1. 用户访问登录页",
        "_clarify_history": [{"q": "x"}],
        "_kb_query_logs": ["y"],
        "additional_details": "# 用户登录功能需求\n\n1. 用户访问登录页",
        "flows": {"happy_path": {"steps": [{"description": "输入账号密码"}]}},
    }
    cleaned = sanitize_srr_for_generation(srr)
    assert "_raw_text" not in cleaned
    assert "_clarify_history" not in cleaned
    assert "_kb_query_logs" not in cleaned
    assert "additional_details" not in cleaned
    assert cleaned["requirement"] == {"title": "登录"}
    assert "flows" in cleaned
    # 入参不被就地修改
    assert "_raw_text" in srr


def test_sanitize_is_noop_for_non_dict():
    assert sanitize_srr_for_generation(None) is None
    assert sanitize_srr_for_generation("x") == "x"


def test_build_user_prompt_excludes_raw_text():
    """修复前：json.dumps(srr) 把 _raw_text 全文塞进「当前需求」，生成退化为照抄原文。"""
    raw_marker = "# 用户登录功能需求（原文首行）"
    srr = {
        "requirement": {"title": "用户登录", "summary": "账号密码登录"},
        "_raw_text": raw_marker + "\n\n大段与生成无关的正文……",
        "flows": {"happy_path": {"steps": [{"order": 1, "description": "输入账号密码"}]}},
    }
    prompt = build_user_prompt(srr=srr, functional_points=[], enable_multi_type=True)
    assert raw_marker not in prompt
    assert "_raw_text" not in prompt
    assert "用户登录" in prompt


def test_build_user_prompt_injects_shared_rules_param():
    """FP 分片通道：子 SRR 无 CFI，必须经 shared_rules 下发总 SRR 的共享规则。"""
    fp_srr = {
        "requirement": {"title": "登录"},
        "flows": {"happy_path": {"steps": [{"order": 1, "description": "输入账号密码"}]}},
    }
    fp = {"id": "FP-001", "name": "账号登录", "fp_type": "workflow", "srr_json": fp_srr}
    shared = [{"id": "SBR-001", "rule": "登录连续失败5次锁定账号30分钟", "applied_fps": ["登录", "风控"]}]

    without = build_user_prompt(srr=fp_srr, functional_points=[fp], enable_multi_type=True)
    with_rules = build_user_prompt(
        srr=fp_srr, functional_points=[fp], enable_multi_type=True, shared_rules=shared
    )
    assert "## 跨 FP 共享业务规则" not in without
    assert "## 跨 FP 共享业务规则" in with_rules
    assert "登录连续失败5次锁定账号30分钟" in with_rules
