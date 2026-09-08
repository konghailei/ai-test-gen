"""阶段2核心组件测试脚本。

使用 docs/01-requirement-parsing.md §2.2 的真实 SRR 示例（用户登录功能）
验证 channel_selector、quality_gate、prompts 各模块功能正确。

测试覆盖：
1. channel_selector: FAST/FULL 通道选择逻辑
2. quality_gate: 7 项检查 + 动态阈值（冷启动/正常）
3. prompts: 5 个提示词模块的导入和函数调用
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# 确保能导入 backend.app
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))


# ============================================================================
# 测试数据：来自 docs/01-requirement-parsing.md §2.2 的真实 SRR 示例
# ============================================================================

# 完整有效的 SRR（用户登录功能，P0 优先级 → FULL 通道）
VALID_SRR_P0 = {
    "srr_version": "1.0",
    "requirement": {
        "id": "REQ-2024-001",
        "product_id": "PROD-001",
        "product_name": "用户中心",
        "title": "用户登录功能",
        "priority": "P0",
        "modules": ["auth", "session"],
        "summary": "用户通过账号密码登录系统，登录成功后跳转首页",
    },
    "preconditions": ["用户已在系统注册", "用户未处于锁定状态"],
    "flows": {
        "happy_path": {
            "steps": [
                {"order": 1, "action": "打开登录页", "expect": "显示登录表单"},
                {"order": 2, "action": "输入有效账号密码", "expect": "输入框正常"},
                {"order": 3, "action": "点击登录按钮", "expect": "登录成功，跳转首页"},
            ]
        },
        "alternate_flows": [
            {"name": "账号不存在", "trigger": "输入未注册账号", "expect": "提示「账号不存在」"},
            {"name": "密码错误", "trigger": "输入错误密码", "expect": "提示「密码错误」"},
        ],
    },
    "boundary_conditions": [
        {"field": "密码", "condition": "长度 < 6", "expect": "前端校验拦截"},
        {"field": "密码", "condition": "长度 = 6", "expect": "允许提交"},
    ],
    "business_rules": [
        # type 枚举：validation/calculation/workflow/permission（见 SRR.BusinessRule）
        {"id": "BR-01", "rule": "连续失败5次锁定30分钟", "type": "permission"}
    ],
    "data_constraints": [
        {"field": "username", "type": "string", "required": True, "constraints": "3-50 chars", "example": "john_doe"},
        {"field": "password", "type": "string", "required": True, "constraints": "6-128 chars", "example": "***"},
    ],
    "non_functional": {
        "performance": "P99 < 500ms",
        "security": "TLS 1.3, bcrypt加密",
        "compatibility": "",
        "accessibility": "",
    },
    "relationships": [
        {"target": "REQ-2024-000", "type": "depends_on", "strength": "strong", "note": ""}
    ],
    "test_points_hint": [
        {"category": "functional", "point": "正常登录流程验证"},
        {"category": "boundary", "point": "密码长度边界值测试"},
        {"category": "exception", "point": "账号锁定机制验证"},
        {"category": "security", "point": "密码加密存储验证"},
    ],
}

# 简单需求 SRR（P2 优先级，步骤 ≤5 → FAST 通道）
SIMPLE_SRR_P2 = {
    "srr_version": "1.0",
    "requirement": {
        "id": "REQ-2024-002",
        "product_id": "PROD-001",
        "product_name": "用户中心",
        "title": "用户登出功能",
        "priority": "P2",
        "modules": ["auth"],
        "summary": "用户点击登出按钮退出登录",
    },
    "preconditions": ["用户已登录"],
    "flows": {
        "happy_path": {
            "steps": [
                {"order": 1, "action": "点击登出按钮", "expect": "退出登录，跳转登录页"},
            ]
        },
        "alternate_flows": [],
    },
    "boundary_conditions": [],
    "business_rules": [],
    "data_constraints": [
        {"field": "session_token", "type": "string", "required": True, "constraints": "非空", "example": "xxx"},
    ],
    "non_functional": {"performance": "", "security": "", "compatibility": "", "accessibility": ""},
    "relationships": [],
    "test_points_hint": [{"category": "functional", "point": "登出流程"}],
}

# 优化需求 SRR（含 optimization_context → FULL 通道 + 影响分析）
OPT_SRR = {
    "srr_version": "1.0",
    "requirement": {
        "id": "REQ-2024-003",
        "product_id": "PROD-001",
        "product_name": "用户中心",
        "title": "用户登录功能优化",
        "priority": "P1",
        "modules": ["auth"],
        "summary": "登录增加验证码机制，连续失败3次弹出验证码",
        "change_type": "optimized",
        "supersedes": "REQ-2024-001",
    },
    "preconditions": ["用户已在系统注册"],
    "flows": {
        "happy_path": {
            "steps": [
                {"order": 1, "action": "打开登录页", "expect": "显示登录表单"},
                {"order": 2, "action": "输入有效账号密码", "expect": "输入框正常"},
                {"order": 3, "action": "点击登录按钮", "expect": "登录成功，跳转首页"},
            ]
        },
        "alternate_flows": [],
    },
    "boundary_conditions": [],
    "business_rules": [
        {"id": "BR-02", "rule": "连续失败3次弹出验证码", "type": "permission"}
    ],
    "data_constraints": [],
    "non_functional": {"performance": "", "security": "", "compatibility": "", "accessibility": ""},
    "relationships": [],
    "test_points_hint": [{"category": "security", "point": "验证码触发机制"}],
    "optimization_context": {
        "original_requirement": {
            "id": "REQ-2024-001",
            "title": "用户登录功能",
            "version": 1,
        },
        "original_test_cases": [],
        "diff": {
            "added": ["验证码机制"],
            "removed": [],
            "modified": ["失败锁定阈值 5→3"],
        },
    },
}


# ============================================================================
# 测试工具
# ============================================================================

class TestResult:
    """测试结果收集器。"""
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors: list[str] = []

    def assert_true(self, name: str, condition: bool, detail: str = ""):
        if condition:
            self.passed += 1
            print(f"  ✅ {name}")
        else:
            self.failed += 1
            msg = f"  ❌ {name}: {detail}" if detail else f"  ❌ {name}"
            print(msg)
            self.errors.append(msg)

    def assert_eq(self, name: str, actual, expected):
        if actual == expected:
            self.passed += 1
            print(f"  ✅ {name}")
        else:
            self.failed += 1
            msg = f"  ❌ {name}: 期望 {expected!r}, 实际 {actual!r}"
            print(msg)
            self.errors.append(msg)

    def summary(self) -> bool:
        total = self.passed + self.failed
        print(f"\n{'='*60}")
        print(f"测试汇总: {self.passed}/{total} 通过, {self.failed} 失败")
        if self.errors:
            print(f"\n失败项:")
            for e in self.errors:
                print(e)
        return self.failed == 0


# ============================================================================
# 测试 1: channel_selector
# ============================================================================

def test_channel_selector(result: TestResult):
    """测试通道选择器。"""
    print("\n[测试 1] channel_selector 通道选择器")
    from app.core.channel_selector import select_channel, ChannelConfig

    # 1.1 P0 优先级 → FULL 通道
    cfg = select_channel(VALID_SRR_P0)
    result.assert_eq("P0 需求 → FULL 通道", cfg.channel, "FULL")
    result.assert_true("P0 启用 clarify", cfg.enable_clarify)
    result.assert_true("P0 启用 multi_type", cfg.enable_multi_type_generation)
    result.assert_true("P0 启用 full_review", cfg.enable_full_review)
    result.assert_true("P0 非优化需求不启用 impact_analysis", not cfg.enable_impact_analysis)
    result.assert_true("is_full 属性", cfg.is_full)
    result.assert_true("is_fast 属性为 False", not cfg.is_fast)

    # 1.2 P2 简单需求 → v1.6 起统一 FULL 通道（避免通道误判导致漏生成用例）
    cfg = select_channel(SIMPLE_SRR_P2)
    result.assert_eq("P2 简单需求 → FULL 通道（v1.6 全 FULL）", cfg.channel, "FULL")
    result.assert_true("FULL 启用 clarify", cfg.enable_clarify)
    result.assert_true("FULL 启用 multi_type", cfg.enable_multi_type_generation)
    result.assert_true("FULL 启用 full_review", cfg.enable_full_review)
    result.assert_true("is_full 属性", cfg.is_full)

    # 1.3 优化需求（含 optimization_context）→ FULL + impact_analysis
    cfg = select_channel(OPT_SRR)
    result.assert_eq("优化需求 → FULL 通道", cfg.channel, "FULL")
    result.assert_true("优化需求启用 impact_analysis", cfg.enable_impact_analysis)

    # 1.4 步骤 >5 → FULL 通道（即使 P2 优先级）
    long_srr = json.loads(json.dumps(SIMPLE_SRR_P2))
    long_srr["requirement"]["priority"] = "P2"
    long_srr["flows"]["happy_path"]["steps"] = [
        {"order": i, "action": f"步骤{i}", "expect": f"预期{i}"} for i in range(1, 7)
    ]
    cfg = select_channel(long_srr)
    result.assert_eq("步骤>5 → FULL 通道", cfg.channel, "FULL")

    # 1.5 缺失字段容错（空 SRR）→ v1.6 起统一 FULL
    cfg = select_channel({})
    result.assert_eq("空 SRR → FULL 通道（v1.6 全 FULL）", cfg.channel, "FULL")


# ============================================================================
# 测试 2: quality_gate
# ============================================================================

async def test_quality_gate(result: TestResult):
    """测试质量门禁。"""
    print("\n[测试 2] quality_gate 质量门禁")
    from app.core.quality_gate import QualityGate, QualityGateResult, quality_gate

    # 2.1 正常模式 + 高质量评分 → 通过
    clarify_result = {
        "kb_status": {"kb_empty": False, "product_first_requirement": False},
        "quality_report": {"overall_score": 0.85},
        "questions": [],
    }
    gate = QualityGate()
    res = await gate.check(VALID_SRR_P0, clarify_result)
    result.assert_true("正常模式高质量 → passed=True", res.passed)
    result.assert_eq("正常模式 pass_score=0.75", res.pass_score, 0.75)
    result.assert_true("正常模式 cold_start=False", not res.cold_start)
    result.assert_eq("无 blockers", len(res.blockers), 0)
    result.assert_true("recommendation 含通过标记", "通过" in res.recommendation)

    # 2.2 冷启动模式（KB 为空）→ 阈值放宽至 0.60
    clarify_cold = {
        "kb_status": {"kb_empty": True, "product_first_requirement": False},
        "quality_report": {"overall_score": 0.65},
        "questions": [],
    }
    res = await gate.check(VALID_SRR_P0, clarify_cold)
    result.assert_eq("冷启动 pass_score=0.60", res.pass_score, 0.60)
    result.assert_true("冷启动 cold_start=True", res.cold_start)
    # 0.65 >= 0.60 → 通过
    result.assert_true("冷启动 0.65>=0.60 → passed=True", res.passed)
    # 冷启动警告
    cold_warnings = [w for w in res.warnings if "冷启动" in w]
    result.assert_true("冷启动模式有警告提示", len(cold_warnings) > 0)

    # 2.3 质量评分低于阈值 → Blocker
    clarify_low = {
        "kb_status": {"kb_empty": False, "product_first_requirement": False},
        "quality_report": {"overall_score": 0.50},
        "questions": [],
    }
    res = await gate.check(VALID_SRR_P0, clarify_low)
    result.assert_true("评分 0.50<0.75 → passed=False", not res.passed)
    score_blockers = [b for b in res.blockers if "质量评分" in b]
    result.assert_true("有质量评分 Blocker", len(score_blockers) > 0)

    # 2.4 有未解决 Critical 问题 → Blocker
    clarify_critical = {
        "kb_status": {"kb_empty": False, "product_first_requirement": False},
        "quality_report": {"overall_score": 0.85},
        "questions": [
            {"id": "Q001", "priority": "critical", "resolved": False},
            {"id": "Q002", "priority": "important", "resolved": False},
        ],
    }
    res = await gate.check(VALID_SRR_P0, clarify_critical)
    result.assert_true("有 critical 问题 → passed=False", not res.passed)
    critical_blockers = [b for b in res.blockers if "Critical" in b]
    result.assert_true("有 Critical 问题 Blocker", len(critical_blockers) > 0)

    # 2.5 Critical 问题已解决 → 不阻断
    clarify_resolved = {
        "kb_status": {"kb_empty": False, "product_first_requirement": False},
        "quality_report": {"overall_score": 0.85},
        "questions": [
            {"id": "Q001", "priority": "critical", "resolved": True},
        ],
    }
    res = await gate.check(VALID_SRR_P0, clarify_resolved)
    result.assert_true("Critical 已解决 → passed=True", res.passed)

    # 2.6 关键字段缺失（Schema 通过但字段为空）→ Blocker
    bad_srr = {"requirement": {"title": "", "summary": ""}, "flows": {}}
    res = await gate.check(bad_srr, {"kb_status": {}, "quality_report": None, "questions": []})
    result.assert_true("关键字段缺失 → passed=False", not res.passed)
    result.assert_true("有 title Blocker", any("requirement.title" in b for b in res.blockers))
    result.assert_true("有 summary Blocker", any("requirement.summary" in b for b in res.blockers))

    # 2.7 Schema 校验失败 → 仅 1 个 Blocker，不产生冗余的关键字段 Blocker
    # SRR 缺少 requirement 字段（必填），Schema 校验失败
    invalid_srr = {"flows": {"happy_path": {"steps": []}}}
    res = await gate.check(invalid_srr, {"kb_status": {}, "quality_report": None, "questions": []})
    result.assert_true("Schema 校验失败 → passed=False", not res.passed)
    result.assert_true("有 Schema Blocker", any("Schema" in b for b in res.blockers))
    # 修复问题8：Schema 失败时应跳过关键字段检查，不产生冗余 blocker
    result.assert_true("Schema 失败时无冗余关键字段 Blocker",
                       not any("requirement.title" in b for b in res.blockers))

    # 2.8 全局单例可用
    result.assert_true("全局单例 quality_gate 可用", quality_gate is not None)


# ============================================================================
# 测试 3: prompts 模块
# ============================================================================

def test_prompts(result: TestResult):
    """测试提示词模板模块。"""
    print("\n[测试 3] prompts 提示词模板")
    from app.prompts import (
        req_agent, clarify_agent, gen_agent, review_agent
    )
    # v3.1.7: prompts/impact_agent.py 已下线（影响分析为纯规则实现），
    # import 失败才是正确行为
    try:
        import app.prompts.impact_agent  # noqa: F401
        impact_prompts_gone = False
    except ModuleNotFoundError:
        impact_prompts_gone = True
    result.assert_true("impact_agent 提示词已下线（v3.1.7）", impact_prompts_gone)

    # 3.1 ReqAgent
    result.assert_true("req_agent.SYSTEM_PROMPT 非空", bool(req_agent.SYSTEM_PROMPT))
    result.assert_true("req_agent.SRR_SCHEMA_GUIDE 非空", bool(req_agent.SRR_SCHEMA_GUIDE))
    result.assert_true("req_agent.FEW_SHOT_EXAMPLE 非空", bool(req_agent.FEW_SHOT_EXAMPLE))
    user_prompt = req_agent.build_user_prompt("用户登录功能", "PROD-001", "用户中心")
    result.assert_true("req_agent.build_user_prompt 返回非空", bool(user_prompt))
    result.assert_true("user_prompt 含需求文本", "用户登录功能" in user_prompt)
    result.assert_true("user_prompt 含 Schema 指导", "srr_version" in user_prompt)

    # 3.2 ClarifyAgent
    result.assert_true("clarify_agent.SYSTEM_PROMPT 非空", bool(clarify_agent.SYSTEM_PROMPT))
    result.assert_true("clarify_agent.QUESTIONS_SCHEMA 非空", bool(clarify_agent.QUESTIONS_SCHEMA))
    result.assert_true("clarify_agent.AMBIGUITY_PATTERNS 含 vague_quantifier",
                       "vague_quantifier" in clarify_agent.AMBIGUITY_PATTERNS)
    result.assert_true("clarify_agent.COMPLETENESS_CHECKLIST 含 critical",
                       "critical" in clarify_agent.COMPLETENESS_CHECKLIST)
    cp = clarify_agent.build_user_prompt(VALID_SRR_P0, [{"field": "x", "value": "y"}])
    result.assert_true("clarify_agent.build_user_prompt 返回非空", bool(cp))
    result.assert_true("clarify prompt 含 SRR JSON", "REQ-2024-001" in cp)

    # 3.3 ImpactAnalysisAgent（v3.1.7: 提示词下线，规则实现）
    from app.agents.impact_agent import ImpactAnalysisAgent
    _ia = ImpactAnalysisAgent.__new__(ImpactAnalysisAgent)
    d, tf = ImpactAnalysisAgent._build_fp_impact_description(
        _ia, {"fp_id": "FP-1", "fp_name": "用户登录"},
        [{"fp_name": "权限校验", "depth": 0}], [{"module_name": "用户中心", "reason": "直接模块"}],
    )
    result.assert_true("impact 影响说明规则版非空", bool(d) and bool(tf))
    result.assert_true("影响说明含受影响 FP", "权限校验" in d)
    d2, _ = ImpactAnalysisAgent._build_fp_impact_description(
        _ia, {"fp_id": "FP-1", "fp_name": "用户登录"}, [], [])
    result.assert_true("无影响时兜底文案", "无被依赖关系" in d2)

    # 3.4 GenAgent
    result.assert_true("gen_agent.SYSTEM_PROMPT 非空", bool(gen_agent.SYSTEM_PROMPT))
    result.assert_true("gen_agent.UTCF_CASE_SCHEMA 非空", bool(gen_agent.UTCF_CASE_SCHEMA))
    # FULL 通道（分类生成）
    gp_full = gen_agent.build_user_prompt(
        VALID_SRR_P0,
        few_shot_cases=[{"id": "TC-AUTH-0001-0001", "title": "历史用例"}],
        matched_patterns=[{"name": "auth_pattern"}],
        enable_multi_type=True,
    )
    result.assert_true("gen_agent FULL prompt 非空", bool(gp_full))
    result.assert_true("FULL prompt 含功能测试要求", "功能测试" in gp_full)
    result.assert_true("FULL prompt 含边界值测试要求", "边界值测试" in gp_full)
    # FAST 通道（批量生成）
    gp_fast = gen_agent.build_user_prompt(SIMPLE_SRR_P2, enable_multi_type=False)
    result.assert_true("FAST prompt 含核心测试要求", "核心测试用例" in gp_fast)
    # 优化需求注入影响指导
    gp_opt = gen_agent.build_user_prompt(OPT_SRR, impact_guidance="# 影响分析指导\n测试")
    result.assert_true("优化需求 prompt 含影响指导", "影响分析指导" in gp_opt)

    # 3.5 ReviewAgent
    result.assert_true("review_agent.SYSTEM_PROMPT 非空", bool(review_agent.SYSTEM_PROMPT))
    result.assert_true("review_agent.REVIEW_REPORT_SCHEMA 非空", bool(review_agent.REVIEW_REPORT_SCHEMA))
    test_cases = [
        {"id": "TC-AUTH-0001-0001", "title": "正常登录", "type": "functional",
         "preconditions": ["已注册"], "steps": [{"step": 1, "action": "x", "expected": "y"}],
         "related_requirement": "REQ-2024-001", "assertions": [{"type": "equals"}],
         "gen_metadata": {"gen_confidence": 0.8}},
    ]
    rp = review_agent.build_user_prompt(VALID_SRR_P0, test_cases, [0.8])
    result.assert_true("review_agent.build_user_prompt 返回非空", bool(rp))
    # v5.0 降载：不再注入全量 SRR JSON，仅注入需求标题与测试点提示
    result.assert_true("review prompt 含需求标题（降载后）", str((VALID_SRR_P0.get("requirement") or {}).get("title", ""))[:20] in rp)
    # 测试置信度计算
    conf = review_agent.calculate_confidence(test_cases[0], [{"similarity": 0.9}])
    result.assert_true("calculate_confidence 返回 [0,1]", 0.0 <= conf <= 1.0)
    result.assert_true("calculate_confidence > 0（有 match + 完整字段）", conf > 0.5)
    # 无 match_results
    conf_no_match = review_agent.calculate_confidence(test_cases[0])
    result.assert_true("无 match 时置信度仍有效", 0.0 <= conf_no_match <= 1.0)
    # pattern_bonus
    case_with_pattern = dict(test_cases[0])
    case_with_pattern["source_pattern"] = "auth_pattern"
    conf_pattern = review_agent.calculate_confidence(case_with_pattern, [{"similarity": 0.9}])
    result.assert_true("有 source_pattern 时置信度更高", conf_pattern >= conf)


# ============================================================================
# 主函数
# ============================================================================

def main():
    print("=" * 60)
    print("阶段2 核心组件测试")
    print("=" * 60)

    result = TestResult()

    test_channel_selector(result)
    asyncio.run(test_quality_gate(result))
    test_prompts(result)

    success = result.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
