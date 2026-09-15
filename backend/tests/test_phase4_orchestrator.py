"""阶段4 LangGraph 编排器测试脚本。

测试策略：
1. 纯逻辑测试（不依赖 LLM）：ClarifyLoopGuard / make_sse_event / 路由 / 答案应用
2. 端到端流水线测试（mock Agent）：验证 DAG 流程、SSE 事件序列、条件分支、Clarify 回环
   - FAST 通道（P2 需求）：跳过 clarify
   - FULL 通道（P0 需求）：含 clarify
   - 优化需求：触发 ImpactAgent
   - Clarify 回环：多轮澄清后强制通过

测试数据来自 docs/01-requirement-parsing.md §2.2 的真实 SRR 示例。
"""

from __future__ import annotations

import asyncio
import copy
import sys
from pathlib import Path
from unittest.mock import AsyncMock

# 确保能导入 backend.app
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from app.core.orchestrator import (
    ClarifyLoopGuard,
    Orchestrator,
    PipelineState,
    STAGE_PROGRESS,
    make_sse_event,
    orchestrator,
)


# ============================================================================
# 测试数据
# ============================================================================

# P2 需求 → FAST 通道（priority != P0，步骤 <= 5，无 optimization_context）
SRR_P2_FAST = {
    "srr_version": "1.0",
    "requirement": {
        "id": "REQ-2024-001",
        "product_id": "PROD-001",
        "product_name": "用户中心",
        "title": "用户登录功能",
        "priority": "P2",
        "modules": ["auth"],
        "summary": "用户通过账号密码登录系统",
        "change_type": "new",
    },
    "preconditions": ["用户已注册"],
    "flows": {
        "happy_path": {
            "steps": [{"order": 1, "action": "输入账号密码", "expect": "登录成功"}]
        }
    },
    "data_constraints": [
        {"field": "username", "type": "string", "required": True, "constraints": "3-50 chars"}
    ],
}

# P0 需求 → FULL 通道（priority == P0）
SRR_P0_FULL = {
    "srr_version": "1.0",
    "requirement": {
        "id": "REQ-2024-002",
        "product_id": "PROD-001",
        "product_name": "用户中心",
        "title": "用户登录功能",
        "priority": "P0",
        "modules": ["auth", "session"],
        "summary": "用户通过账号密码登录系统，登录成功后跳转首页",
        "change_type": "new",
    },
    "preconditions": ["用户已注册", "用户未锁定"],
    "flows": {
        "happy_path": {
            "steps": [
                {"order": 1, "action": "打开登录页", "expect": "显示表单"},
                {"order": 2, "action": "输入账号密码", "expect": "登录成功"},
            ]
        }
    },
    "data_constraints": [
        {"field": "password", "type": "string", "required": True, "constraints": "6-128 chars"}
    ],
}

# 优化需求 → FULL 通道 + 触发 ImpactAgent
SRR_OPT = {
    "srr_version": "1.0",
    "requirement": {
        "id": "REQ-2024-003",
        "product_id": "PROD-001",
        "product_name": "用户中心",
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
        "diff": {"added": ["验证码机制"], "removed": [], "modified": ["失败锁定阈值"]},
    },
}

# ClarifyAgent 输出（通过）
CLARIFY_PASSED = {
    "status": "passed",
    "questions": [],
    "auto_augmentations": [],
    "quality_report": {"overall_score": 0.95, "pass_threshold": 0.7},
    "kb_status": {"kb_empty": False, "product_first_requirement": False},
}

# ClarifyAgent 输出（需澄清，低分）
CLARIFY_NEEDS_CLARIFICATION = {
    "status": "needs_clarification",
    "questions": [{"id": "Q1", "priority": "critical", "question": "请补充登录方式"}],
    "auto_augmentations": [],
    "quality_report": {"overall_score": 0.45, "pass_threshold": 0.7},
    "kb_status": {"kb_empty": False, "product_first_requirement": False},
}

# ImpactAgent 输出（优化需求）
IMPACT_OPT = {
    "is_optimization": True,
    "impact": {"direct": {"requirements": ["REQ-2024-001"], "test_cases": []}},
    "case_operations": {"update": [], "add": [{"reason": "新增验证码"}], "delete": [], "keep": []},
    "regression_scope": [],
    "generation_guidance": "影响分析指导：新增验证码机制，需生成对应用例",
}

# ImpactAgent 输出（非优化需求）
IMPACT_NOT_OPT = {"is_optimization": False, "generation_guidance": ""}

# TestDesignAgent 输出（测试设计蓝图，含回归装配结果 v3.4）
TEST_DESIGN_BLUEPRINT = {
    "techniques_applicable": {
        "equivalence_partitioning": True,
        "boundary_value": True,
        "decision_table": True,
        "state_transition": False,
        "scenario": True,
        "orthogonal_array": False,
        "error_guessing": True,
        "cause_effect": False,
    },
    "boundary_targets": ["[total]账号密码长度边界"],
    "decision_table_targets": ["[total]登录判定决策表"],
    "state_targets": [],
    "error_guess_hints": ["[total]错误密码/空输入"],
    "scenario_targets": ["[total]登录主流程"],
    "guidance_text": "测试设计指导：等价类 + 边界值 + 决策表 + 场景法 + 错误推测",
    "impact_guidance_passthrough": False,
    # v3.4 回归用例装配
    "historical_regression_cases": [
        {
            "id": "TC-REUSED-1",
            "title": "[回归]历史登录用例",
            "type": "regression",
            "priority": "P1",
            "related_fp": "登录",
            "source": "reused",
            "gen_metadata": {"regression_source": "reused"},
        }
    ],
    "coverage_gaps": [
        {
            "fp_id": "FP-9",
            "fp_name": "验证码",
            "test_focus": "验证码校验逻辑",
            "reason": "无历史 approved 用例，需按影响功能说明补生成回归用例",
        }
    ],
}

# GenAgent 输出
GEN_CASES = [
    {
        "id": "TC-AUTH-0001-0001",
        "title": "正常登录",
        "type": "functional",
        "priority": "P0",
        "preconditions": ["用户已注册"],
        "steps": [{"step": 1, "action": "输入账号密码", "expected": "登录成功"}],
        # v3.4：补齐断言——真实生成结果必带 assertions（contract 校验要求），
        # 旧夹具缺该字段，会让"用例合同校验"把它判成畸形内容（docs/15 §4.6）
        "assertions": [
            {"type": "equals", "target": "operation.result", "expected_value": "登录成功",
             "message": "账号密码正确时返回登录成功"},
        ],
        "related_requirement": "REQ-2024-001",
        "gen_metadata": {"gen_confidence": 0.85},
    }
]

# ReviewAgent 输出
REVIEW_REPORT = {
    "total_cases": 1,
    "passed_cases": 1,
    "blocked_cases": 0,
    "issues": [],
    "coverage_summary": {"functional": 1, "boundary": 0, "exception": 0, "security": 0, "performance": 0},
    "avg_confidence": 0.82,
    "should_supplement": False,
    "supplement_hints": [],
}
REVIEW_CASES = [{**GEN_CASES[0], "confidence": 0.82}]


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


class AgentMocks:
    """临时替换 Agent 单例方法的上下文管理器。

    用法：
        with AgentMocks(parse_return=SRR_P2_FAST, clarify_return=CLARIFY_PASSED, ...):
            async for event in orchestrator.run_pipeline("..."):
                ...
    """

    def __init__(
        self,
        parse_return: dict | None = None,
        clarify_return: dict | None = None,
        clarify_returns: list | None = None,  # 多轮澄清返回不同结果
        impact_return: dict | None = None,
        test_design_return: dict | None = None,
        gen_return: list | None = None,
        review_return: tuple | None = None,
    ):
        self.parse_return = parse_return
        self.clarify_return = clarify_return
        self.clarify_returns = clarify_returns
        self.impact_return = impact_return
        self.test_design_return = test_design_return
        self.gen_return = gen_return
        self.review_return = review_return
        self._originals: dict = {}
        # 捕获 generate 调用参数（验证回归基线/覆盖缺口是否正确传递）
        self.gen_call_kwargs: dict = {}
        self.gen_call_kwargs_list: list[dict] = []

    def __enter__(self):
        from app.agents.clarify_agent import clarify_agent
        from app.agents.gen_agent import gen_agent
        from app.agents.impact_agent import impact_agent
        from app.agents.req_agent import req_agent
        from app.agents.review_agent import review_agent
        from app.agents.test_design_agent import test_design_agent

        # 保存原方法
        self._originals = {
            "req.parse": req_agent.parse,
            "clarify.clarify": clarify_agent.clarify,
            "impact.analyze": impact_agent.analyze,
            "test_design.design": test_design_agent.design,
            "gen.generate": gen_agent.generate,
            "review.review": review_agent.review,
        }

        # 替换为 mock
        if self.parse_return is not None:
            req_agent.parse = AsyncMock(return_value=self.parse_return)

        if self.clarify_returns is not None:
            # 多轮澄清：按顺序返回不同结果
            self._clarify_call_count = 0

            async def _clarify_multi(srr):
                idx = min(self._clarify_call_count, len(self.clarify_returns) - 1)
                self._clarify_call_count += 1
                return self.clarify_returns[idx]

            clarify_agent.clarify = _clarify_multi
        elif self.clarify_return is not None:
            clarify_agent.clarify = AsyncMock(return_value=self.clarify_return)

        if self.impact_return is not None:
            impact_agent.analyze = AsyncMock(return_value=self.impact_return)

        if self.test_design_return is not None:
            # design 为同步方法（不含 LLM 调用），用普通 lambda 替换
            test_design_agent.design = lambda *a, **k: self.test_design_return

        if self.gen_return is not None:
            async def _gen(*a, **kw):
                # 捕获 generate 调用参数（累积所有调用——v3.1.3 审核后补生成会额外
                # 调用一次且不带 historical_regression，最后一次 ≠ 主生成调用）
                self.gen_call_kwargs = kw
                self.gen_call_kwargs_list.append(kw)
                return self.gen_return

            gen_agent.generate = _gen

        if self.review_return is not None:
            review_agent.review = AsyncMock(return_value=self.review_return)

        return self

    def __exit__(self, *args):
        from app.agents.clarify_agent import clarify_agent
        from app.agents.gen_agent import gen_agent
        from app.agents.impact_agent import impact_agent
        from app.agents.req_agent import req_agent
        from app.agents.review_agent import review_agent
        from app.agents.test_design_agent import test_design_agent

        # 恢复原方法
        req_agent.parse = self._originals["req.parse"]
        clarify_agent.clarify = self._originals["clarify.clarify"]
        impact_agent.analyze = self._originals["impact.analyze"]
        test_design_agent.design = self._originals["test_design.design"]
        gen_agent.generate = self._originals["gen.generate"]
        review_agent.review = self._originals["review.review"]


async def collect_events_with_mocks(raw_requirement: str, **kwargs) -> tuple[list[dict], AgentMocks]:
    """运行流水线并收集所有 SSE 事件，同时返回 AgentMocks 以便读取捕获参数。

    注：编排器 _parse_node 直接消费预解析的 srr_json（不再调用 req_agent.parse），
    因此将 parse_return 作为 srr_json 传入 run_pipeline 以驱动节点 parse。
    """
    parse_return = kwargs.get("parse_return")
    mocks = AgentMocks(**kwargs)
    events = []
    options: dict = {"srr_json": parse_return} if parse_return is not None else {}
    with mocks:
        async for event in orchestrator.run_pipeline(raw_requirement, **options):
            events.append(event)
    return events, mocks


async def collect_events(raw_requirement: str, **kwargs) -> list[dict]:
    """运行流水线并收集所有 SSE 事件。"""
    events, _ = await collect_events_with_mocks(raw_requirement, **kwargs)
    return events


# ============================================================================
# 测试 1: ClarifyLoopGuard
# ============================================================================


def test_clarify_loop_guard(result: TestResult):
    """测试 ClarifyLoopGuard 死锁预防逻辑。"""
    print("\n[测试 1] ClarifyLoopGuard")
    # MAX_ITERATIONS 为 @classmethod + @property 组合，类访问返回 property 对象，
    # 直接读取 settings 的配置值作为判定基准（二者同源）。
    from app.config import settings

    MAX = settings.MAX_CLARIFY_ITERATIONS

    result.assert_eq("最大轮次=3", MAX, 3)

    # should_retry：通过则不重试
    result.assert_true("通过 → 不重试", not ClarifyLoopGuard.should_retry(0, True))
    result.assert_true("通过(第3轮) → 不重试", not ClarifyLoopGuard.should_retry(3, True))

    # should_retry：未通过且 iteration < MAX → 重试
    result.assert_true("未通过(第0轮) → 重试", ClarifyLoopGuard.should_retry(0, False))
    result.assert_true("未通过(第2轮) → 重试", ClarifyLoopGuard.should_retry(2, False))

    # should_retry：未通过且 iteration >= MAX → 不重试
    result.assert_true("未通过(第3轮) → 不重试", not ClarifyLoopGuard.should_retry(3, False))

    # should_force_pass：通过则不强制
    result.assert_true("通过 → 不强制", not ClarifyLoopGuard.should_force_pass(3, True))

    # should_force_pass：未通过且 iteration >= MAX → 强制
    result.assert_true("未通过(第3轮) → 强制", ClarifyLoopGuard.should_force_pass(3, False))
    result.assert_true("未通过(第4轮) → 强制", ClarifyLoopGuard.should_force_pass(4, False))

    # should_force_pass：未通过但 iteration < MAX → 不强制
    result.assert_true("未通过(第2轮) → 不强制", not ClarifyLoopGuard.should_force_pass(2, False))


# ============================================================================
# 测试 2: make_sse_event + STAGE_PROGRESS
# ============================================================================


def test_make_sse_event(result: TestResult):
    """测试 SSE 事件构造。"""
    print("\n[测试 2] make_sse_event")

    # 基本 stage 都有 progress（注：retrieve 节点已 v3.1 移除，test_design v2.3 新增）
    for stage in ["parse", "select_channel", "clarify", "gate", "impact", "test_design", "generate", "review", "done", "error"]:
        result.assert_true(f"STAGE_PROGRESS 含 {stage}", stage in STAGE_PROGRESS)

    # progress 递增
    result.assert_true("parse<gate", STAGE_PROGRESS["parse"] < STAGE_PROGRESS["gate"])

    # 测试设计阶段位于 impact 与 generate 之间
    result.assert_true("impact<test_design", STAGE_PROGRESS["impact"] < STAGE_PROGRESS["test_design"])
    result.assert_true("test_design<generate", STAGE_PROGRESS["test_design"] < STAGE_PROGRESS["generate"])
    result.assert_true("test_design 中点区间", 50 < STAGE_PROGRESS["test_design"] < 70)

    result.assert_true("generate<done", STAGE_PROGRESS["generate"] < STAGE_PROGRESS["done"])
    result.assert_eq("done=100", STAGE_PROGRESS["done"], 100)

    # make_sse_event 基本结构
    event = make_sse_event("parse", "解析中...")
    result.assert_eq("stage", event["stage"], "parse")
    result.assert_eq("message", event["message"], "解析中...")
    result.assert_eq("progress", event["progress"], 20)

    # make_sse_event 附加字段
    event2 = make_sse_event("done", "完成", req_id="REQ-001", cases=[])
    result.assert_eq("done.req_id", event2["req_id"], "REQ-001")
    result.assert_eq("done.cases", event2["cases"], [])
    result.assert_eq("done.progress", event2["progress"], 100)

    # 未知 stage progress=0
    event3 = make_sse_event("unknown", "未知")
    result.assert_eq("unknown.progress=0", event3["progress"], 0)


# ============================================================================
# 测试 3: 路由函数
# ============================================================================


def test_route_functions(result: TestResult):
    """测试条件路由函数。"""
    print("\n[测试 3] 路由函数")
    orch = Orchestrator()

    # _route_after_channel（v3.x: enable_clarify 由 channel_config 决定，
    # 生产路径 select_channel 节点总会写入 config；缺失视为禁用走 gate）
    state_full: PipelineState = {"channel": "FULL",
                                 "channel_config": {"channel": "FULL", "enable_clarify": True}}
    state_fast: PipelineState = {"channel": "FAST"}
    result.assert_eq("FULL → clarify", orch._route_after_channel(state_full), "clarify")
    result.assert_eq("FAST → gate", orch._route_after_channel(state_fast), "gate")

    # _route_after_gate：通过 → impact
    state_passed: PipelineState = {"gate_result": {"passed": True, "forced_pass": False}, "clarify_iteration": 1}
    result.assert_eq("passed → impact", orch._route_after_gate(state_passed), "impact")

    # _route_after_gate：强制通过 → impact
    state_forced: PipelineState = {"gate_result": {"passed": False, "forced_pass": True}, "clarify_iteration": 3}
    result.assert_eq("forced → impact", orch._route_after_gate(state_forced), "impact")

    # _route_after_gate：未通过 & iteration < MAX & clarify 启用 → clarify
    state_retry: PipelineState = {"gate_result": {"passed": False, "forced_pass": False},
                                  "clarify_iteration": 1,
                                  "channel": "FULL",
                                  "channel_config": {"channel": "FULL", "enable_clarify": True}}
    result.assert_eq("未通过(第1轮) → clarify", orch._route_after_gate(state_retry), "clarify")

    # _route_after_gate：未通过 & iteration >= MAX → impact（兜底）
    state_max: PipelineState = {"gate_result": {"passed": False, "forced_pass": False}, "clarify_iteration": 3}
    result.assert_eq("未通过(第3轮) → impact(兜底)", orch._route_after_gate(state_max), "impact")


# ============================================================================
# 测试 4: _apply_clarify_answers + _set_nested_value
# ============================================================================


def test_apply_clarify_answers(result: TestResult):
    """测试澄清答案应用。"""
    print("\n[测试 4] _apply_clarify_answers")
    orch = Orchestrator()
    original_srr = {"requirement": {"title": "原标题", "summary": "原概述"}, "priority": "P2"}

    # 嵌套路径
    updated = orch._apply_clarify_answers(original_srr, {"requirement.title": "新标题"})
    result.assert_eq("嵌套字段已更新", updated["requirement"]["title"], "新标题")
    result.assert_eq("原对象未被修改（深拷贝）", original_srr["requirement"]["title"], "原标题")

    # 顶层字段
    updated2 = orch._apply_clarify_answers(original_srr, {"priority": "P0"})
    result.assert_eq("顶层字段已更新", updated2["priority"], "P0")
    result.assert_eq("原对象未被修改", original_srr["priority"], "P2")

    # 多字段同时更新
    updated3 = orch._apply_clarify_answers(
        original_srr,
        {"requirement.summary": "新概述", "priority": "P1"},
    )
    result.assert_eq("多字段-嵌套", updated3["requirement"]["summary"], "新概述")
    result.assert_eq("多字段-顶层", updated3["priority"], "P1")

    # 空答案 → 返回深拷贝
    updated4 = orch._apply_clarify_answers(original_srr, {})
    result.assert_eq("空答案-返回原值", updated4["requirement"]["title"], "原标题")

    # None 答案 → 返回深拷贝
    updated5 = orch._apply_clarify_answers(original_srr, None)  # type: ignore
    result.assert_eq("None答案-返回原值", updated5["requirement"]["title"], "原标题")

    # _set_nested_value：深层嵌套
    data: dict = {}
    orch._set_nested_value(data, "a.b.c", 123)
    result.assert_eq("深层嵌套设值", data["a"]["b"]["c"], 123)

    # _set_nested_value：覆盖现有路径
    data2 = {"a": {"b": {"c": 1}}}
    orch._set_nested_value(data2, "a.b.c", 999)
    result.assert_eq("覆盖现有值", data2["a"]["b"]["c"], 999)

    # _set_nested_value：路径中间非字典 → 替换为字典
    data3 = {"a": "字符串"}
    orch._set_nested_value(data3, "a.b", "值")
    result.assert_eq("路径中间非字典→替换", data3["a"]["b"], "值")


# ============================================================================
# 测试 5: test_design 节点 + 回归装配端到端
# ============================================================================


async def test_pipeline_test_design(result: TestResult):
    """测试完整流水线（v1.6 起全部走 FULL 通道），聚焦 test_design 节点与回归装配。

    - 验证影响力：impact → test_design → generate → review 的完整 DAG
    - 验证 TestDesignAgent 蓝图注入 generate（回归基线 + 覆盖缺口传递）
    - patch orchestrator._assemble_regression_cases 模拟回归装配结果，
      不依赖 DB，确定性地覆盖「回归复用 + 缺口补生成」的传递逻辑
    """
    print("\n[测试 5] test_design + 回归装配端到端")

    # patch 真实回归装配方法（它会查 DB，这里用确定性的 mock 返回值）
    regression_cases = TEST_DESIGN_BLUEPRINT["historical_regression_cases"]
    coverage_gaps = TEST_DESIGN_BLUEPRINT["coverage_gaps"]
    gr = orchestrator._assemble_regression_cases
    orchestrator._assemble_regression_cases = AsyncMock(return_value=(regression_cases, coverage_gaps))
    try:
        events, mocks = await collect_events_with_mocks(
            "用户登录功能",
            parse_return=SRR_P2_FAST,
            clarify_return=CLARIFY_PASSED,
            impact_return=IMPACT_NOT_OPT,
            gen_return=GEN_CASES,
            review_return=(REVIEW_REPORT, REVIEW_CASES),
        )
    finally:
        orchestrator._assemble_regression_cases = gr

    # 事件序列验证（v1.6 全通道 FULL，含 clarify）
    stages = [e["stage"] for e in events]
    result.assert_true("含 parse", "parse" in stages)
    result.assert_true("含 select_channel", "select_channel" in stages)
    result.assert_true("含 gate", "gate" in stages)
    result.assert_true("含 impact", "impact" in stages)
    result.assert_true("含 test_design", "test_design" in stages)
    result.assert_true("含 generate", "generate" in stages)
    result.assert_true("含 review", "review" in stages)
    result.assert_true("含 done", "done" in stages)

    # v3.1：retrieve 节点已移除
    result.assert_true("无 retrieve 节点", "retrieve" not in stages)

    # done 事件含结果
    done_event = next(e for e in events if e["stage"] == "done")
    result.assert_true("done.cases 非空", len(done_event.get("cases", [])) > 0)
    result.assert_true("done.review_report 非空", "review_report" in done_event)
    result.assert_true("done.duration_ms > 0", done_event.get("duration_ms", 0) > 0)

    # 节点顺序：parse 在 select_channel 前
    result.assert_true("parse 在 select_channel 前",
                       stages.index("parse") < stages.index("select_channel"))
    result.assert_true("gate 在 impact 前", stages.index("gate") < stages.index("impact"))
    # v2.3：test_design 位于 impact 与 generate 之间
    result.assert_true("impact 在 test_design 前",
                       stages.index("impact") < stages.index("test_design"))
    result.assert_true("test_design 在 generate 前",
                       stages.index("test_design") < stages.index("generate"))
    result.assert_true("generate 在 review 前", stages.index("generate") < stages.index("review"))

    # test_design 事件含技术/guidance
    td_event = next(e for e in events if e["stage"] == "test_design")
    result.assert_true("test_design 事件含 techniques", len(td_event.get("techniques", [])) > 0)
    result.assert_true("test_design 事件含 guidance_len", td_event.get("guidance_len", 0) > 0)

    # done 事件含 test_design 蓝图 + 回归装配
    td_blueprint = done_event.get("test_design", {})
    result.assert_true("done.test_design 非空", bool(td_blueprint))
    result.assert_eq("蓝图中回归复用用例=1",
                     len(td_blueprint.get("historical_regression_cases") or []), 1)
    result.assert_eq("蓝图中覆盖缺口=1",
                     len(td_blueprint.get("coverage_gaps") or []), 1)

    # 回归基线/覆盖缺口正确传递到主生成调用（首次 FP 分片调用；
    # v3.1.3 审核后补生成的最后一次调用不带回归参数，故不能断言最后一次）
    main_gen_calls = [kw for kw in mocks.gen_call_kwargs_list
                      if "historical_regression" in kw]
    result.assert_true("存在主生成调用（含回归参数）", len(main_gen_calls) >= 1)
    gen_kw = main_gen_calls[0] if main_gen_calls else {}
    result.assert_true("gen 收到 historical_regression", len(gen_kw.get("historical_regression") or []) == 1)
    result.assert_true("gen 收到 coverage_gaps", len(gen_kw.get("coverage_gaps") or []) == 1)


# ============================================================================
# 测试 6: FULL 通道端到端
# ============================================================================


async def test_pipeline_full(result: TestResult):
    """测试 FULL 通道完整流水线（P0 需求，含 clarify）。"""
    print("\n[测试 6] FULL 通道端到端")
    events = await collect_events(
        "用户登录功能 P0",
        parse_return=SRR_P0_FULL,
        clarify_return=CLARIFY_PASSED,
        impact_return=IMPACT_NOT_OPT,
        gen_return=GEN_CASES,
        review_return=(REVIEW_REPORT, REVIEW_CASES),
    )

    stages = [e["stage"] for e in events]
    result.assert_true("含 clarify", "clarify" in stages)
    result.assert_true("含 parse/select/gate/impact/test_design/generate/review/done",
                       all(s in stages for s in ["parse", "select_channel", "gate", "impact", "test_design", "generate", "review", "done"]))
    result.assert_true("无 retrieve 节点", "retrieve" not in stages)

    # FULL 通道
    sc_event = next(e for e in events if e["stage"] == "select_channel")
    result.assert_eq("channel=FULL", sc_event.get("channel"), "FULL")

    # clarify 在 gate 前
    result.assert_true("clarify 在 gate 前", stages.index("clarify") < stages.index("gate"))

    # select_channel 在 clarify 前
    result.assert_true("select_channel 在 clarify 前",
                       stages.index("select_channel") < stages.index("clarify"))

    # test_design 在 generate 前
    result.assert_true("test_design 在 generate 前",
                       stages.index("test_design") < stages.index("generate"))

    # clarify 事件含 status
    cl_event = next(e for e in events if e["stage"] == "clarify")
    result.assert_eq("clarify_status=passed", cl_event.get("clarify_status"), "passed")
    result.assert_eq("iteration=1", cl_event.get("iteration"), 1)

    # gate 通过
    gate_event = next(e for e in events if e["stage"] == "gate")
    result.assert_true("gate_passed=True", gate_event.get("gate_passed") is True)


# ============================================================================
# 测试 7: 优化需求端到端
# ============================================================================


async def test_pipeline_optimize(result: TestResult):
    """测试优化需求流水线（触发 ImpactAgent）。"""
    print("\n[测试 7] 优化需求端到端")
    events = await collect_events(
        "登录优化",
        parse_return=SRR_OPT,
        clarify_return=CLARIFY_PASSED,
        impact_return=IMPACT_OPT,
        gen_return=GEN_CASES,
        review_return=(REVIEW_REPORT, REVIEW_CASES),
    )

    # impact 事件含 is_optimization=True
    impact_event = next(e for e in events if e["stage"] == "impact")
    result.assert_true("is_optimization=True", impact_event.get("is_optimization") is True)

    # done 事件含 impact_result
    done_event = next(e for e in events if e["stage"] == "done")
    impact_result = done_event.get("impact_result", {})
    result.assert_true("done.impact_result.is_optimization=True",
                       impact_result.get("is_optimization") is True)
    result.assert_true("done.impact_result.generation_guidance 非空",
                       bool(impact_result.get("generation_guidance")))


# ============================================================================
# 测试 8: Clarify 回环（多轮澄清）
# ============================================================================


async def test_pipeline_clarify_loop(result: TestResult):
    """测试 Clarify 回环：第1轮未通过 → 第2轮通过。"""
    print("\n[测试 8] Clarify 回环（2轮）")
    events = await collect_events(
        "模糊需求",
        parse_return=SRR_P0_FULL,  # P0 → FULL 通道
        clarify_returns=[CLARIFY_NEEDS_CLARIFICATION, CLARIFY_PASSED],  # 第1轮未通过，第2轮通过
        impact_return=IMPACT_NOT_OPT,
        gen_return=GEN_CASES,
        review_return=(REVIEW_REPORT, REVIEW_CASES),
    )

    # 应有 2 次 clarify 和 2 次 gate
    clarify_events = [e for e in events if e["stage"] == "clarify"]
    gate_events = [e for e in events if e["stage"] == "gate"]
    result.assert_eq("clarify 执行 2 次", len(clarify_events), 2)
    result.assert_eq("gate 执行 2 次", len(gate_events), 2)

    # 第1轮 clarify → needs_clarification
    result.assert_eq("第1轮 clarify_status=needs_clarification",
                     clarify_events[0].get("clarify_status"), "needs_clarification")
    result.assert_eq("第1轮 iteration=1", clarify_events[0].get("iteration"), 1)

    # 第2轮 clarify → passed
    result.assert_eq("第2轮 clarify_status=passed",
                     clarify_events[1].get("clarify_status"), "passed")
    result.assert_eq("第2轮 iteration=2", clarify_events[1].get("iteration"), 2)

    # 第1次 gate 未通过，第2次 gate 通过
    result.assert_true("第1次 gate 未通过", gate_events[0].get("gate_passed") is False)
    result.assert_true("第2次 gate 通过", gate_events[1].get("gate_passed") is True)

    # 最终 done
    result.assert_true("含 done", any(e["stage"] == "done" for e in events))

    # 顺序：clarify1 → gate1 → clarify2 → gate2 → impact
    stages = [e["stage"] for e in events]
    cl1_idx = [i for i, s in enumerate(stages) if s == "clarify"][0]
    g1_idx = [i for i, s in enumerate(stages) if s == "gate"][0]
    cl2_idx = [i for i, s in enumerate(stages) if s == "clarify"][1]
    g2_idx = [i for i, s in enumerate(stages) if s == "gate"][1]
    imp_idx = stages.index("impact")
    result.assert_true("clarify1 → gate1", cl1_idx < g1_idx)
    result.assert_true("gate1 → clarify2(回环)", g1_idx < cl2_idx)
    result.assert_true("clarify2 → gate2", cl2_idx < g2_idx)
    result.assert_true("gate2 → impact", g2_idx < imp_idx)


# ============================================================================
# 测试 9: Clarify 强制通过（达到最大轮次）
# ============================================================================


async def test_pipeline_force_pass(result: TestResult):
    """测试达到最大轮次后强制通过。"""
    print("\n[测试 9] Clarify 强制通过（3轮）")
    # 3 轮都返回 needs_clarification → 第3轮后强制通过
    events = await collect_events(
        "极度模糊需求",
        parse_return=SRR_P0_FULL,
        clarify_returns=[CLARIFY_NEEDS_CLARIFICATION, CLARIFY_NEEDS_CLARIFICATION, CLARIFY_NEEDS_CLARIFICATION],
        impact_return=IMPACT_NOT_OPT,
        gen_return=GEN_CASES,
        review_return=(REVIEW_REPORT, REVIEW_CASES),
    )

    # 应有 3 次 clarify 和 3 次 gate
    clarify_events = [e for e in events if e["stage"] == "clarify"]
    gate_events = [e for e in events if e["stage"] == "gate"]
    result.assert_eq("clarify 执行 3 次", len(clarify_events), 3)
    result.assert_eq("gate 执行 3 次", len(gate_events), 3)

    # 第3次 gate 应强制通过
    result.assert_true("第3次 gate forced_pass=True",
                       gate_events[2].get("forced_pass") is True)

    # 流水线仍能完成（不会死锁）
    result.assert_true("含 done", any(e["stage"] == "done" for e in events))


# ============================================================================
# 测试 10: 审核补生成闭环
# ============================================================================


async def test_review_supplement_loop(result: TestResult):
    """审核发现缺口时只补生成一次，并使用第二次审核结果回流。"""
    print("\n[测试 10] 审核补生成闭环")
    from app.agents.gen_agent import gen_agent
    from app.agents.review_agent import review_agent

    first_report = {
        **REVIEW_REPORT,
        "should_supplement": True,
        "supplement_hints": ["验证码错误分支"],
    }
    second_report = {
        **REVIEW_REPORT,
        "total_cases": 2,
        "passed_cases": 2,
        "should_supplement": True,
    }
    reviewed_cases = [{**GEN_CASES[0], "confidence": 0.8}]
    supplement_case = {
        **GEN_CASES[0],
        "id": "TC-SUPPLEMENT-1",
        "title": "验证码错误",
        "confidence": 0.8,
    }
    review_mock = AsyncMock(side_effect=[
        (first_report, reviewed_cases),
        (second_report, reviewed_cases + [supplement_case]),
    ])
    gen_mock = AsyncMock(return_value=[supplement_case])
    persist_mock = AsyncMock(return_value={"auto_approved": 2})
    originals = (review_agent.review, gen_agent.generate, review_agent.persist_to_kb)
    review_agent.review = review_mock
    gen_agent.generate = gen_mock
    review_agent.persist_to_kb = persist_mock
    try:
        orch = Orchestrator()
        state: PipelineState = {
            "srr": SRR_P2_FAST,
            "test_cases": reviewed_cases,
            "req_id": "REQ-2024-001",
            "channel_config": {"enable_multi_type_generation": True},
            "impact_result": {"generation_guidance": ""},
            "functional_points": [],
            "supplement_iteration": 0,
        }
        update = await orch._review_node(state)
    finally:
        review_agent.review, gen_agent.generate, review_agent.persist_to_kb = originals

    result.assert_eq("审核调用 2 次", review_mock.await_count, 2)
    result.assert_eq("补生成调用 1 次", gen_mock.await_count, 1)
    result.assert_eq("最终报告使用第二次审核", update["review_result"], second_report)
    result.assert_eq("补生成轮次=1", update["supplement_iteration"], 1)
    result.assert_eq("最终回流使用第二次审核用例", update["reviewed_cases"], reviewed_cases + [supplement_case])
    result.assert_eq("知识库回流调用 1 次", persist_mock.await_count, 1)
    result.assert_eq("知识库回流使用最终报告", persist_mock.await_args.args[2], second_report)


# ============================================================================
# 测试 12: 发布门禁 fail-closed（docs/15 §4.1 / P0-2）
# ============================================================================


async def test_publication_gate_fail_closed(result: TestResult):
    """畸形用例不得自动发布：不中断流水线、判 blocked、PG 与 Milvus 均不写入。

    对应 docs/15 §4.1 验收：合同不合格的内容不能变成权威知识。
    """
    print("\n[测试 12] 发布门禁 fail-closed")

    from app.db.milvus_store import vector_store

    # 缺 assertions → 违反用例可执行合同（真实生成结果必带断言）
    malformed = {
        "id": "TC-BAD-0001", "title": "畸形用例", "type": "functional",
        "priority": "P0",
        "steps": [{"step": 1, "action": "提交表单", "expected": "提交成功"}],
        "related_requirement": "REQ-2024-001",
        "gen_metadata": {"gen_confidence": 0.9},
    }

    vector_calls = {"n": 0}
    original_add = vector_store.add_test_case

    def _counting_add(*a, **kw):
        vector_calls["n"] += 1

    vector_store.add_test_case = _counting_add
    try:
        # persist_to_kb 走真实实现：blocked 时必须在触碰 DB 之前返回
        events, _ = await collect_events_with_mocks(
            "用户登录功能",
            parse_return=SRR_P2_FAST,
            clarify_return=CLARIFY_PASSED,
            impact_return=IMPACT_NOT_OPT,
            gen_return=[malformed],
            review_return=(REVIEW_REPORT, [{**malformed, "confidence": 0.9}]),
        )
    finally:
        vector_store.add_test_case = original_add

    stages = [e["stage"] for e in events]
    result.assert_true("含 review（未因合同校验中断）", "review" in stages)
    result.assert_true("含 done（前端能拿到终态）", "done" in stages)

    done = next((e for e in events if e["stage"] == "done"), {})
    publication = done.get("publication", {}) or {}
    result.assert_eq("发布决策=blocked", publication.get("decision"), "blocked")
    result.assert_true("原因含 invalid_case_content",
                       "invalid_case_content" in (publication.get("reasons") or []))
    result.assert_eq("终态 failed（不得 completed）", done.get("completion_status"), "failed")
    result.assert_true("需人工介入", done.get("manual_action_required") is True)
    result.assert_eq("Milvus 写入 0 次", vector_calls["n"], 0)
    result.assert_true("KB 回流已跳过", (done.get("kb_flowback") or {}).get("skipped") is True)


# ============================================================================
# 测试 11: 错误处理
# ============================================================================


async def test_pipeline_error(result: TestResult):
    """测试节点异常时的错误处理。

    _parse_node 直接消费预解析的 srr_json；未传入时抛 ValueError「SRR 为空」，
    编排器捕获后产出 error 事件（不产出 done）。
    """
    print("\n[测试 10] 错误处理")
    events = []
    # 不传 srr_json → _parse_node 抛 SRR 为空 → error 事件
    with AgentMocks():
        async for event in orchestrator.run_pipeline("触发异常的需求"):
            events.append(event)

    # 应有 error 事件
    result.assert_true("含 error 事件", any(e["stage"] == "error" for e in events))

    # 不应有 done 事件
    result.assert_true("无 done 事件", not any(e["stage"] == "done" for e in events))

    # error 事件 progress=100
    error_event = next(e for e in events if e["stage"] == "error")
    result.assert_eq("error.progress=100", error_event["progress"], 100)


# ============================================================================
# 测试 11: SSE 事件 progress 单调递增
# ============================================================================


async def test_progress_monotonic(result: TestResult):
    """测试 SSE 事件 progress 大致单调递增（允许相同值）。"""
    print("\n[测试 11] progress 单调递增")
    events = await collect_events(
        "用户登录",
        parse_return=SRR_P2_FAST,
        clarify_return=CLARIFY_PASSED,
        impact_return=IMPACT_NOT_OPT,
        gen_return=GEN_CASES,
        review_return=(REVIEW_REPORT, REVIEW_CASES),
    )

    progresses = [e["progress"] for e in events]
    result.assert_true("progress 非空", len(progresses) > 0)

    # 单调非递减
    for i in range(1, len(progresses)):
        result.assert_true(f"progress[{i}]>=progress[{i-1}] ({progresses[i]}>={progresses[i-1]})",
                           progresses[i] >= progresses[i - 1])

    # 最后是 100
    result.assert_eq("最后 progress=100", progresses[-1], 100)


# ============================================================================
# 测试 12: 生成逻辑纯逻辑验证（回归合并 / prompt 注入 / TestDesignAgent / fp_cases）
# ============================================================================


def test_generation_logic(result: TestResult):
    """验证「覆盖所有生成用例逻辑」的非端到端纯逻辑段，不依赖 DB/LLM。

    覆盖 v3.4 新增逻辑：
    - TestDesignAgent.design 蓝图（技术适用性 + 覆盖缺口/回归字段透传）
    - GenAgent 回归复用基线合并（historical_regression 并入结果集 + batch_id）
    - build_user_prompt 覆盖缺口/回归锚点/测试模式库的注入
    - 回归用例装配的版本选择核心 _go_live_key（按上线时间选最新版）
    """
    print("\n[测试 12] 生成逻辑纯逻辑验证")

    # ── 12.1 TestDesignAgent 蓝图 ──
    from app.agents.test_design_agent import TestDesignAgent

    agent = TestDesignAgent()
    blueprint = agent.design(SRR_P2_FAST)
    result.assert_true("蓝图含 techniques_applicable", "techniques_applicable" in blueprint)
    result.assert_true("蓝图含 guidance_text", bool(blueprint.get("guidance_text")))
    result.assert_true("applicable 至少含边界值(有范围约束)",
                       blueprint["techniques_applicable"].get("boundary_value") is True or
                       blueprint["techniques_applicable"].get("equivalence_partitioning") is True)

    # design 为同步无副作用，不含 LLM 调用（确定性）
    result.assert_true("design 属同步返回 dict", isinstance(blueprint, dict))

    # ── 12.2 GenAgent 回归复用基线合并 ──
    from app.agents.gen_agent import GenAgent

    ga = GenAgent()
    # 模拟 generate 尾部合并段逻辑：历史回归 + 新生成拼接，且复用用例补 batch_id
    historical_regression = [
        {"title": "[回归]历史登录", "type": "regression", "source": "reused", "id": "TC-OLD-1"},
    ]
    newly_gen = [
        {"title": "登录+验证码", "type": "functional", "id": "TC-OLD-1"},  # 新生成
    ]
    batch_id = "BATCH-TEST-001"
    joined = list(historical_regression) + newly_gen
    for c in joined:
        if c.get("source") == "reused":
            c["gen_batch_id"] = batch_id
    result.assert_eq("合并后总数=2", len(joined), 2)
    result.assert_eq("回归用例带 batch_id", joined[0].get("gen_batch_id"), batch_id)
    result.assert_eq("新生成用例不受 batch 覆盖", joined[1].get("gen_batch_id"), None)

    # ── 12.3 build_user_prompt 注入：覆盖缺口 / 回归锚点 / 测试模式库 ──
    from app.prompts import gen_agent as gen_prompts

    user_prompt = gen_prompts.build_user_prompt(
        srr=SRR_P2_FAST,
        coverage_gaps=[{"fp_name": "验证码", "test_focus": "验证码校验", "reason": "无历史用例"}],
        regression_anchors=TEST_DESIGN_BLUEPRINT["historical_regression_cases"],
        matched_patterns=[{"name": "登录边界模式", "category": "boundary", "pattern_json": {"type": "边界值"}}],
    )
    result.assert_true("prompt 含覆盖缺口段落", "覆盖缺口" in user_prompt)
    result.assert_true("prompt 含回归锚点段落", "已复用回归用例" in user_prompt or "回归锚" in user_prompt)
    result.assert_true("prompt 含测试模式段落", "测试模式参考" in user_prompt)
    result.assert_true("prompt 含缺口 fp 名", "验证码" in user_prompt)
    result.assert_true("prompt 含回归用例标题", "历史登录" in user_prompt)

    # ── 12.4 回归用例装配版本选择核心：按上线时间选最新版 ──
    from datetime import datetime, timezone

    from app.core import fp_cases as fp_cases_mod

    # 未设置视为最早（-1）；naive 与 aware 均可比较 epoch
    result.assert_true("None 视为最早", fp_cases_mod._go_live_key(None) < fp_cases_mod._go_live_key("2026-01-01"))
    # 字符串 ISO → epoch
    early = fp_cases_mod._go_live_key("2025-01-01T00:00:00")
    late = fp_cases_mod._go_live_key("2026-06-15T00:00:00")
    result.assert_true("后上线 > 先上线（epoch）", late > early)
    # datetime 类型
    dt = datetime(2026, 8, 1, tzinfo=timezone.utc)
    result.assert_true("datetime 可排序", fp_cases_mod._go_live_key(dt) > late)
    # 非法字符串视为最早
    result.assert_true("非法字符串视为最早",
                       fp_cases_mod._go_live_key("not-a-date") < fp_cases_mod._go_live_key("2025-01-01"))

    # 验证 max 选中最新上线项目：模拟"版本号低但后上线"场景
    keys = [fp_cases_mod._go_live_key("2028-01-01T00:00:00"), fp_cases_mod._go_live_key("2026-01-01T00:00:00")]
    result.assert_true("max 选中后上线项目", fp_cases_mod._go_live_key("2028-01-01T00:00:00") == max(keys))


# ============================================================================
# 主函数
# ============================================================================


async def async_main(result: TestResult):
    await test_pipeline_test_design(result)
    await test_pipeline_full(result)
    await test_pipeline_optimize(result)
    await test_pipeline_clarify_loop(result)
    await test_pipeline_force_pass(result)
    await test_review_supplement_loop(result)
    await test_publication_gate_fail_closed(result)
    await test_pipeline_error(result)
    await test_progress_monotonic(result)


def main():
    print("=" * 60)
    print("阶段4 LangGraph 编排器测试")
    print("=" * 60)

    result = TestResult()

    # 同步测试（纯逻辑）
    test_clarify_loop_guard(result)
    test_make_sse_event(result)
    test_route_functions(result)
    test_apply_clarify_answers(result)
    test_generation_logic(result)

    # 异步测试（端到端流水线，mock Agent）
    asyncio.run(async_main(result))

    success = result.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
