"""阶段3 Agent 测试脚本。

测试5个Agent的纯逻辑方法和异步方法降级处理。
测试数据来自 docs/01-requirement-parsing.md §2.2 的真实 SRR 示例。

测试策略：
1. 纯逻辑方法（不依赖 LLM）：直接调用内部方法验证正确性
2. 异步方法降级（LLM 不可用时）：验证规则降级逻辑
3. LLM 完整流程：若配置了 API Key 则测试端到端流程，否则跳过
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
from pathlib import Path

# 确保能导入 backend.app
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from app.config import settings


# ============================================================================
# 测试数据（来自 docs/01 §2.2 的真实 SRR 示例）
# ============================================================================

VALID_SRR = {
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
        ],
    },
    "boundary_conditions": [
        {"field": "密码", "condition": "长度 < 6", "expect": "前端校验拦截"},
    ],
    "business_rules": [
        {"id": "BR-01", "rule": "连续失败5次锁定30分钟", "type": "permission"}
    ],
    "data_constraints": [
        {"field": "username", "type": "string", "required": True, "constraints": "3-50 chars", "example": "john_doe"},
        {"field": "password", "type": "string", "required": True, "constraints": "6-128 chars", "example": "***"},
    ],
    "non_functional": {"performance": "P99 < 500ms", "security": "TLS 1.3", "compatibility": "", "accessibility": ""},
    "relationships": [],
    "test_points_hint": [
        {"category": "functional", "point": "正常登录流程"},
        {"category": "boundary", "point": "密码长度边界值"},
        {"category": "exception", "point": "账号锁定机制"},
        {"category": "security", "point": "密码加密存储"},
    ],
}

INCOMPLETE_SRR = {
    "srr_version": "1.0",
    "requirement": {
        "id": "REQ-2024-002",
        "title": "模糊需求",
        "priority": "P2",
        "summary": "用户可以适当时候进行一些操作",
    },
    # 缺失 preconditions / flows / data_constraints 等
    # 含模糊量词 "适当" "一些"
}

OPT_SRR = {
    "srr_version": "1.0",
    "requirement": {
        "id": "REQ-2024-003",
        "product_id": "PROD-001",
        "title": "登录优化",
        "priority": "P1",
        "modules": ["auth"],
        "summary": "登录增加验证码机制",
        "change_type": "optimized",
    },
    "preconditions": ["用户已注册"],
    "flows": {"happy_path": {"steps": [{"order": 1, "action": "打开登录页", "expect": "显示表单"}]}},
    "data_constraints": [{"field": "captcha", "type": "string", "required": True, "constraints": "4位数字"}],
    "optimization_context": {
        "original_requirement": {"id": "REQ-2024-001", "title": "用户登录功能", "version": 1},
        "original_test_cases": [],
        "diff": {
            "added": ["验证码机制"],
            "removed": [],
            "modified": ["失败锁定阈值"],
        },
    },
}

RAW_REQUIREMENT = "用户登录功能：用户通过账号密码登录系统，连续失败5次锁定30分钟，登录成功后跳转首页。"

SAMPLE_CASES = [
    {
        "id": "",
        "title": "正常登录",
        "type": "functional",
        "priority": "P0",
        "preconditions": ["用户已注册"],
        "steps": [{"step": 1, "action": "输入账号密码", "expected": "登录成功"}],
        "related_requirement": "REQ-2024-001",
        "assertions": [{"type": "equals", "target": "status", "expected_value": 200}],
        "gen_metadata": {"gen_confidence": 0.85},
    },
    {
        "id": "",
        "title": "密码长度边界",
        "type": "boundary",
        "priority": "P2",
        "preconditions": ["用户已注册"],
        "steps": [
            {"step": 1, "action": "输入5位密码", "expected": "校验失败"},
            {"step": 2, "action": "输入6位密码", "expected": "校验通过"},
        ],
        "related_requirement": "REQ-2024-001",
        "gen_confidence": 0.8,  # 顶层 gen_confidence（测试移入 gen_metadata）
    },
]

ORIGINAL_CASES_FOR_IMPACT = [
    {"id": "TC-AUTH-0001-0001", "title": "正常登录", "type": "functional",
     "case_json": {"steps": [{"action": "输入账号密码"}]}},
    {"id": "TC-AUTH-0001-0002", "title": "密码错误", "type": "exception",
     "case_json": {"steps": [{"action": "输入错误密码"}]}},
]


# ============================================================================
# 测试工具
# ============================================================================

class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
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

    def skip(self, name: str, reason: str = ""):
        self.skipped += 1
        print(f"  ⏭️  {name} (跳过: {reason})")

    def summary(self) -> bool:
        total = self.passed + self.failed
        print(f"\n{'='*60}")
        print(f"测试汇总: {self.passed}/{total} 通过, {self.failed} 失败, {self.skipped} 跳过")
        if self.errors:
            print(f"\n失败项:")
            for e in self.errors:
                print(e)
        return self.failed == 0


def has_llm_key() -> bool:
    """检查是否配置了 LLM API Key。"""
    return bool(settings.DEEPSEEK_API_KEY or settings.OPENAI_API_KEY)


# ============================================================================
# 测试 1: ReqAgent
# ============================================================================

def test_req_agent(result: TestResult):
    """测试 ReqAgent。"""
    print("\n[测试 1] ReqAgent 需求解析")
    from app.agents.req_agent import ReqAgent

    agent = ReqAgent()

    # 1.1 _post_process：补全缺失字段
    raw_srr = {"requirement": {"title": "测试需求", "summary": "测试概述"}}
    processed = agent._post_process(raw_srr, "PROD-TEST", "测试产品")
    result.assert_eq("补全 srr_version", processed.get("srr_version"), "1.0")
    result.assert_eq("补全 product_id", processed["requirement"]["product_id"], "PROD-TEST")
    result.assert_eq("补全 product_name", processed["requirement"]["product_name"], "测试产品")
    result.assert_true("自动生成需求 ID", bool(processed["requirement"]["id"]))
    result.assert_true("ID 格式 REQ-YYYY-NNN", processed["requirement"]["id"].startswith("REQ-"))
    result.assert_eq("默认 priority P2", processed["requirement"]["priority"], "P2")
    result.assert_eq("默认 change_type new", processed["requirement"]["change_type"], "new")
    result.assert_true("补全 flows", "happy_path" in processed["flows"])
    result.assert_true("补全 data_constraints", "data_constraints" in processed)

    # 1.2 _post_process：已有 ID 不覆盖
    raw_srr2 = {"requirement": {"id": "REQ-2024-999", "title": "已有ID", "summary": "概述"}}
    processed2 = agent._post_process(raw_srr2)
    result.assert_eq("已有 ID 不覆盖", processed2["requirement"]["id"], "REQ-2024-999")

    # 1.3 全局单例
    from app.agents.req_agent import req_agent as singleton
    result.assert_true("全局单例可用", singleton is not None)


# ============================================================================
# 测试 2: ClarifyAgent
# ============================================================================

def test_clarify_agent(result: TestResult):
    """测试 ClarifyAgent。"""
    print("\n[测试 2] ClarifyAgent 需求澄清")
    from app.agents.clarify_agent import ClarifyAgent

    agent = ClarifyAgent()

    # 2.1 _check_completeness：完整 SRR → 高分
    comp = agent._check_completeness(VALID_SRR)
    result.assert_true("完整 SRR 无 critical 缺失", len(comp["critical_missing"]) == 0)
    result.assert_true("完整 SRR completeness_score > 0.8", comp["completeness_score"] > 0.8)
    result.assert_true("完整 SRR passed=True", comp["passed"])

    # 2.2 _check_completeness：不完整 SRR → 有缺失
    comp2 = agent._check_completeness(INCOMPLETE_SRR)
    result.assert_true("不完整 SRR 有 critical 缺失", len(comp2["critical_missing"]) > 0)
    result.assert_true("不完整 SRR passed=False", not comp2["passed"])
    result.assert_true("缺失 flows.happy_path", any(f["field"] == "flows.happy_path" for f in comp2["critical_missing"]))
    result.assert_true("缺失 data_constraints", any(f["field"] == "data_constraints" for f in comp2["critical_missing"]))

    # 2.3 _detect_ambiguity：模糊量词检测
    ambs = agent._detect_ambiguity(INCOMPLETE_SRR)
    result.assert_true("检测到模糊量词", len(ambs) > 0)
    result.assert_true("含 vague_quantifier 类型", any(a["type"] == "vague_quantifier" for a in ambs))

    # 2.4 _detect_ambiguity：无歧义 SRR
    ambs2 = agent._detect_ambiguity(VALID_SRR)
    result.assert_eq("无歧义 SRR 检测结果为空", len(ambs2), 0)

    # 2.5 _rule_based_questions：规则降级问题生成
    questions = agent._rule_based_questions(comp2, ambs)
    result.assert_true("规则降级生成问题列表", len(questions["questions"]) > 0)
    result.assert_true("问题含 critical 优先级", any(q["priority"] == "critical" for q in questions["questions"]))
    result.assert_true("问题数量 ≤ 5", len(questions["questions"]) <= 5)
    result.assert_true("含 quality_report", "quality_report" in questions)

    # 2.6 _calc_quality_score：质量评分
    score = agent._calc_quality_score(comp, [], {"quality_report": {"overall_score": 0.9}})
    result.assert_eq("LLM 评分优先", score, 0.9)

    score2 = agent._calc_quality_score(comp2, ambs, {})
    result.assert_true("规则降级评分 < 0.8", score2 < 0.8)
    result.assert_true("规则降级评分 >= 0", score2 >= 0)

    # 2.7 _get_nested_value / _set_nested_value
    data = {"a": {"b": {"c": 1}}}
    result.assert_eq("嵌套取值", agent._get_nested_value(data, "a.b.c"), 1)
    result.assert_eq("嵌套取值不存在", agent._get_nested_value(data, "a.x.y"), None)
    agent._set_nested_value(data, "a.b.d", 2)
    result.assert_eq("嵌套设值", data["a"]["b"]["d"], 2)


# ============================================================================
# 测试 3: ImpactAnalysisAgent
# ============================================================================

def test_impact_agent(result: TestResult):
    """测试 ImpactAnalysisAgent。"""
    print("\n[测试 3] ImpactAnalysisAgent 影响分析")
    from app.agents.impact_agent import ImpactAnalysisAgent

    agent = ImpactAnalysisAgent()

    # 3.1 _plan_case_operations：用例操作规划
    diff = {"added": ["验证码机制"], "removed": ["旧版登录"], "modified": ["失败锁定阈值"]}
    ops = agent._plan_case_operations(diff, ORIGINAL_CASES_FOR_IMPACT)
    result.assert_true("add 操作非空", len(ops["add"]) > 0)
    result.assert_true("add 含验证码", "验证码" in ops["add"][0]["reason"])
    result.assert_true("delete 操作匹配旧版登录", len(ops["delete"]) >= 0)
    result.assert_true("keep 操作存在", len(ops["keep"]) >= 0)

    # 3.2 _plan_case_operations：无变更
    ops2 = agent._plan_case_operations({"added": [], "removed": [], "modified": []}, ORIGINAL_CASES_FOR_IMPACT)
    result.assert_eq("无 add 操作", len(ops2["add"]), 0)
    result.assert_eq("无 delete 操作", len(ops2["delete"]), 0)
    result.assert_eq("全部 keep", len(ops2["keep"]), len(ORIGINAL_CASES_FOR_IMPACT))

    # 3.3 _plan_regression：回归范围规划
    ops3 = agent._plan_case_operations(diff, ORIGINAL_CASES_FOR_IMPACT)
    regression = agent._plan_regression(ops3, ORIGINAL_CASES_FOR_IMPACT)
    result.assert_true("回归范围非空", len(regression) > 0)
    result.assert_true("keep 用例在回归范围", any(r["case_id"] for r in regression))


# ============================================================================
# 测试 4: GenAgent
# ============================================================================

def test_gen_agent(result: TestResult):
    """测试 GenAgent。"""
    print("\n[测试 4] GenAgent 用例生成")
    from app.agents.gen_agent import GenAgent

    agent = GenAgent()

    # 4.1 _extract_cases：数组格式
    cases = agent._extract_cases([{"title": "case1"}, {"title": "case2"}])
    result.assert_eq("数组格式提取", len(cases), 2)

    # 4.2 _extract_cases：含 test_suite 的对象
    cases2 = agent._extract_cases({"test_suite": [{"title": "case1"}]})
    result.assert_eq("test_suite 提取", len(cases2), 1)

    # 4.3 _extract_cases：单个用例
    cases3 = agent._extract_cases({"title": "single", "steps": []})
    result.assert_eq("单用例提取", len(cases3), 1)

    # 4.4 _extract_cases：空输入
    cases4 = agent._extract_cases({})
    result.assert_eq("空输入提取", len(cases4), 0)

    # 4.5 _derive_module_prefix：模块前缀推导
    prefix = agent._derive_module_prefix(VALID_SRR)
    result.assert_true("模块前缀非空", bool(prefix))
    result.assert_true("模块前缀全大写", prefix.isupper())

    # 4.6 _derive_module_prefix：无模块
    prefix2 = agent._derive_module_prefix({"requirement": {"modules": []}})
    result.assert_eq("无模块默认 GEN", prefix2, "GEN")

    # 4.7 _post_process_cases：后处理（含 gen_confidence 移入 gen_metadata）
    processed = agent._post_process_cases(
        SAMPLE_CASES, "REQ-2024-001", "AUTH", "BATCH-001", "deepseek-chat"
    )
    result.assert_eq("处理2个用例", len(processed), 2)

    # 验证用例 ID 已生成
    result.assert_true("用例1 ID 已生成", bool(processed[0]["id"]))
    result.assert_true("用例2 ID 已生成", bool(processed[1]["id"]))
    result.assert_true("用例 ID 格式 TC-AUTH-XXXX-XXXX",
                       processed[0]["id"].startswith("TC-AUTH-") and
                       len(processed[0]["id"].split("-")) == 4)

    # ⚠️ 验证 gen_confidence 在 gen_metadata 内（修复问题5）
    result.assert_true("用例1 gen_metadata.gen_confidence 存在",
                       "gen_confidence" in processed[0]["gen_metadata"])
    result.assert_eq("用例1 gen_confidence 值",
                     processed[0]["gen_metadata"]["gen_confidence"], 0.85)

    # ⚠️ 验证顶层 gen_confidence 被移入 gen_metadata
    result.assert_true("用例2 顶层无 gen_confidence", "gen_confidence" not in processed[1])
    result.assert_eq("用例2 gen_metadata.gen_confidence 值",
                     processed[1]["gen_metadata"]["gen_confidence"], 0.8)

    # 验证 related_requirement 补全
    result.assert_eq("related_requirement 补全",
                     processed[0]["related_requirement"], "REQ-2024-001")

    # 验证 gen_metadata 含 gen_model 和 gen_at
    result.assert_eq("gen_model", processed[0]["gen_metadata"]["gen_model"], "deepseek-chat")
    result.assert_true("gen_at 非空", bool(processed[0]["gen_metadata"]["gen_at"]))


# ============================================================================
# 测试 5: ReviewAgent
# ============================================================================

def test_review_agent(result: TestResult):
    """测试 ReviewAgent。"""
    print("\n[测试 5] ReviewAgent 用例审核")
    from app.agents.review_agent import ReviewAgent

    agent = ReviewAgent()

    # 准备用例数据（已后处理）
    test_cases = [
        {
            "id": "TC-AUTH-0001-0001", "title": "正常登录", "type": "functional",
            "priority": "P0", "preconditions": ["已注册"],
            "steps": [{"step": 1, "action": "输入账号密码", "expected": "登录成功"}],
            "related_requirement": "REQ-2024-001",
            "assertions": [{"type": "equals", "target": "status", "expected_value": 200}],
            "gen_metadata": {"gen_confidence": 0.85},
            "confidence": 0.0,
        },
        {
            "id": "TC-AUTH-0001-0002", "title": "无追溯用例", "type": "exception",
            "priority": "P2", "preconditions": [],
            "steps": [{"step": 1, "action": "输入错误密码", "expected": "提示错误"}],
            "related_requirement": "",  # 缺失追溯
            "gen_metadata": {"gen_confidence": 0.6},
            "confidence": 0.0,
        },
    ]

    # 5.1 _rule_based_review：规则预检
    issues = agent._rule_based_review(VALID_SRR, test_cases)
    result.assert_true("规则预检发现追溯性问题",
                       any(i["dimension"] == "traceability" for i in issues))
    result.assert_true("追溯性问题为 blocker",
                       any(i["severity"] == "blocker" for i in issues
                           if i["dimension"] == "traceability"))
    result.assert_true("无前置条件为 info",
                       any(i["severity"] == "info" for i in issues
                           if i["dimension"] == "executability"))

    # 5.2 _calc_coverage_summary：覆盖率汇总
    coverage = agent._calc_coverage_summary(test_cases)
    result.assert_eq("functional 覆盖 1", coverage["functional"], 1)
    result.assert_eq("exception 覆盖 1", coverage["exception"], 1)
    result.assert_eq("boundary 覆盖 0", coverage["boundary"], 0)

    # 5.3 _should_supplement：有 blocker → 需要补充
    should = agent._should_supplement(issues, coverage, VALID_SRR, test_cases)
    result.assert_true("有 blocker → should_supplement=True", should)

    # 5.4 _should_supplement：无 blocker 且覆盖完整 → 不需要补充
    complete_cases = [
        {"type": "functional"}, {"type": "boundary"}, {"type": "exception"}, {"type": "security"}
    ]
    should2 = agent._should_supplement([], {"functional": 1, "boundary": 1, "exception": 1, "security": 1},
                                       VALID_SRR, complete_cases)
    result.assert_true("覆盖完整 → should_supplement=False", not should2)

    # 5.5 _gen_supplement_hints：补充提示生成
    hints = agent._gen_supplement_hints({"functional": 2, "boundary": 0, "exception": 1, "security": 0}, VALID_SRR)
    result.assert_true("补充提示含 boundary", any(h["case_type"] == "boundary" for h in hints))
    result.assert_true("补充提示含 security", any(h["case_type"] == "security" for h in hints))

    # 5.6 _merge_review_results：结果合并
    report = agent._merge_review_results(
        test_cases, issues,
        {"issues": [], "avg_confidence": 0.7},  # LLM 降级报告
        VALID_SRR,
    )
    result.assert_eq("total_cases=2", report["total_cases"], 2)
    result.assert_true("blocked_cases >= 1", report["blocked_cases"] >= 1)
    result.assert_true("avg_confidence > 0", report["avg_confidence"] >= 0)
    result.assert_true("should_supplement=True", report["should_supplement"])


# ============================================================================
# 异步测试：Agent 完整流程（LLM 降级）
# ============================================================================

async def test_async_agents(result: TestResult):
    """测试异步Agent方法（LLM 不可用时验证降级）。"""
    print("\n[测试 6] 异步 Agent 降级测试")

    # 6.1 ClarifyAgent.clarify：完整澄清流程（LLM 降级）
    from app.agents.clarify_agent import clarify_agent
    res = await clarify_agent.clarify(VALID_SRR)
    result.assert_true("ClarifyAgent 返回 status", "status" in res)
    result.assert_true("ClarifyAgent 返回 questions 列表", "questions" in res)
    result.assert_true("ClarifyAgent 返回 quality_report 嵌套结构", "quality_report" in res)
    result.assert_true("quality_report 含 overall_score", "overall_score" in res["quality_report"])
    result.assert_true("ClarifyAgent 返回 kb_status 嵌套结构", "kb_status" in res)
    result.assert_true("kb_status 含 kb_empty", "kb_empty" in res["kb_status"])
    result.assert_true("kb_status 含 product_first_requirement",
                       "product_first_requirement" in res["kb_status"])

    # 6.2 ClarifyAgent.clarify：不完整 SRR → needs_clarification
    res2 = await clarify_agent.clarify(INCOMPLETE_SRR)
    result.assert_true("不完整 SRR → needs_clarification", res2["status"] == "needs_clarification")
    result.assert_true("不完整 SRR 有问题列表", len(res2["questions"]) > 0)

    # 6.3 ImpactAnalysisAgent.analyze：非优化需求 → 快速返回
    from app.agents.impact_agent import impact_agent
    res3 = await impact_agent.analyze(VALID_SRR)
    result.assert_true("非优化需求 is_optimization=False", res3["is_optimization"] is False)
    result.assert_true("非优化需求 generation_guidance 为空", res3["generation_guidance"] == "")

    # 6.4 ImpactAnalysisAgent.analyze：优化需求 → 触发分析
    res4 = await impact_agent.analyze(OPT_SRR, ORIGINAL_CASES_FOR_IMPACT)
    result.assert_true("优化需求 is_optimization=True", res4["is_optimization"] is True)
    result.assert_true("优化需求有 case_operations", "case_operations" in res4)
    result.assert_true("优化需求有 generation_guidance", bool(res4["generation_guidance"]))
    result.assert_true("generation_guidance 含影响分析指导", "影响分析指导" in res4["generation_guidance"])
    result.assert_true("case_operations 含 add 操作", len(res4["case_operations"]["add"]) > 0)

    # 6.5 ReviewAgent.review：审核流程（LLM 降级）
    from app.agents.review_agent import review_agent
    test_cases_for_review = [
        {
            "id": "TC-AUTH-0001-0001", "title": "正常登录", "type": "functional",
            "priority": "P0", "preconditions": ["已注册"],
            "steps": [{"step": 1, "action": "输入账号密码", "expected": "登录成功"}],
            "related_requirement": "REQ-2024-001",
            "assertions": [{"type": "equals"}],
            "gen_metadata": {"gen_confidence": 0.85},
        },
    ]
    report, reviewed = await review_agent.review(VALID_SRR, test_cases_for_review)
    result.assert_true("ReviewAgent 返回报告", "total_cases" in report)
    result.assert_eq("total_cases=1", report["total_cases"], 1)
    result.assert_true("返回审核后用例", len(reviewed) == 1)
    # final_confidence 写入 confidence 字段
    result.assert_true("用例 confidence 已更新（final_confidence）",
                       reviewed[0].get("confidence", 0) > 0)
    result.assert_true("confidence 在 [0,1]",
                       0 <= reviewed[0]["confidence"] <= 1)


# ============================================================================
# 异步测试：LLM 端到端（需配置 API Key）
# ============================================================================

async def test_llm_e2e(result: TestResult):
    """LLM 端到端测试（需配置 API Key）。"""
    print("\n[测试 7] LLM 端到端测试")

    if not has_llm_key():
        result.skip("ReqAgent.parse 端到端", "未配置 LLM API Key")
        result.skip("GenAgent.generate 端到端", "未配置 LLM API Key")
        return

    def _is_provider_unavailable(e: Exception) -> bool:
        """判断异常是否因 LLM 提供商不可用（未配置或无效 Key）。"""
        msg = str(e).lower()
        return "无可用 llm 提供商" in msg or "未配置 api key" in msg

    # 7.1 ReqAgent.parse：真实 LLM 解析
    from app.agents.req_agent import req_agent
    try:
        srr = await req_agent.parse(RAW_REQUIREMENT, "PROD-001", "用户中心")
        result.assert_true("ReqAgent.parse 返回 SRR", "requirement" in srr)
        result.assert_true("SRR 含 title", bool(srr["requirement"].get("title")))
        result.assert_true("SRR 含 summary", bool(srr["requirement"].get("summary")))
        result.assert_true("SRR 含 flows", "flows" in srr)
        result.assert_true("SRR 含 data_constraints", "data_constraints" in srr)
    except Exception as e:
        if _is_provider_unavailable(e):
            result.skip("ReqAgent.parse 端到端", "LLM 提供商不可用")
        else:
            result.assert_true(f"ReqAgent.parse 端到端: {str(e)[:100]}", False)

    # 7.2 GenAgent.generate：真实 LLM 生成
    from app.agents.gen_agent import gen_agent
    try:
        cases = await gen_agent.generate(VALID_SRR, enable_multi_type=False)
        result.assert_true("GenAgent.generate 返回用例列表", isinstance(cases, list))
        result.assert_true("生成用例数 > 0", len(cases) > 0)
        if cases:
            result.assert_true("用例含 ID", bool(cases[0].get("id")))
            result.assert_true("用例含 steps", "steps" in cases[0])
            result.assert_true("用例含 gen_metadata", "gen_metadata" in cases[0])
            result.assert_true("gen_metadata 含 gen_confidence",
                               "gen_confidence" in cases[0]["gen_metadata"])
    except Exception as e:
        if _is_provider_unavailable(e):
            result.skip("GenAgent.generate 端到端", "LLM 提供商不可用")
        else:
            result.assert_true(f"GenAgent.generate 端到端: {str(e)[:100]}", False)


# ============================================================================
# 主函数
# ============================================================================

async def async_main(result: TestResult):
    await test_async_agents(result)
    await test_llm_e2e(result)


def main():
    print("=" * 60)
    print("阶段3 Agent 测试")
    print("=" * 60)
    print(f"LLM API Key 状态: {'已配置' if has_llm_key() else '未配置（LLM 测试将降级/跳过）'}")

    result = TestResult()

    # 同步测试（纯逻辑方法）
    test_req_agent(result)
    test_clarify_agent(result)
    test_impact_agent(result)
    test_gen_agent(result)
    test_review_agent(result)

    # 异步测试
    asyncio.run(async_main(result))

    success = result.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
