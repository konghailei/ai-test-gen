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
    CheckpointBackendError,
    ClarifyLoopGuard,
    DegradedInputError,
    Orchestrator,
    PipelineState,
    STAGE_PROGRESS,
    _srr_is_fp_degraded,
    initial_degraded_flags,
    make_sse_event,
    orchestrator,
)
from app.core.publication_policy import decide_publication


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

    # _route_after_channel（v3.4.4：FAST/FULL 概念已删除，只看 channel_config.enable_clarify）
    state_enabled: PipelineState = {"channel_config": {"enable_clarify": True}}
    state_disabled: PipelineState = {"channel_config": {"enable_clarify": False}}
    result.assert_eq("启用生成期澄清 → clarify", orch._route_after_channel(state_enabled), "clarify")
    result.assert_eq("未启用（已确认需求）→ gate", orch._route_after_channel(state_disabled), "gate")

    # _route_after_gate：通过 → impact
    state_passed: PipelineState = {"gate_result": {"passed": True, "forced_pass": False}, "clarify_iteration": 1}
    result.assert_eq("passed → impact", orch._route_after_gate(state_passed), "impact")

    # _route_after_gate：强制通过 → impact
    state_forced: PipelineState = {"gate_result": {"passed": False, "forced_pass": True}, "clarify_iteration": 3}
    result.assert_eq("forced → impact", orch._route_after_gate(state_forced), "impact")

    # _route_after_gate：未通过 & iteration < MAX & clarify 启用 → clarify
    state_retry: PipelineState = {"gate_result": {"passed": False, "forced_pass": False},
                                  "clarify_iteration": 1,
                                  "channel_config": {"enable_clarify": True}}
    result.assert_eq("未通过(第1轮) → clarify", orch._route_after_gate(state_retry), "clarify")

    # _route_after_gate：未通过 & iteration >= MAX → impact（兜底）
    state_max: PipelineState = {"gate_result": {"passed": False, "forced_pass": False}, "clarify_iteration": 3}
    result.assert_eq("未通过(第3轮) → impact(兜底)", orch._route_after_gate(state_max), "impact")


# ============================================================================
# 测试 3b: 检查点后端降级的到达性（v3.4.6 / 缺陷 A）
# ============================================================================


def test_initial_degraded_flags(result: TestResult):
    """`checkpoint_backend_degraded` 必须能到达消费方（done 事件 / 发布决策）。

    反例（2026-09-28 审计实证）：`_ensure_checkpoint_backend` 在 PostgresSaver
    初始化失败时置 `self._checkpoint_backend_degraded = True`，但该标记全库只有
    定义与赋值、**零消费方** —— docstring 承诺的「done 事件 degraded_flags 可见」
    是假承诺，检查点静默退化为 memory（重启即丢断点）而运维无感知。

    四条断言对应四个必经环节，缺一即回到"写了但到不了"：
      ① 未降级 → 不注入（不能凭空造标记）；
      ② 降级 → 注入正确 flag；
      ③ done 事件真的带出来（make_sse_event 透传）；
      ④ 发布决策把它算进 reasons（降级必须进人工审核，不得自动核准）。
    """
    print("\n[测试 3b] 检查点降级标记的到达性（缺陷 A）")

    # ① 未降级不注入
    result.assert_eq("未降级 → 无标记", initial_degraded_flags(False), [])
    # ② 降级注入正确 flag
    result.assert_eq("降级 → checkpoint_backend_degraded",
                     initial_degraded_flags(True), ["checkpoint_backend_degraded"])

    # ③ 标记能在 done 事件透出（模拟入口注入 → 各节点 append → done 构造）
    flags = initial_degraded_flags(True)
    flags = list(flags) + ["fp_load_failed"]  # 节点沿用既有 append 形态
    done = make_sse_event("done", "流水线完成", degraded_flags=flags)
    result.assert_true("done 事件携带 checkpoint_backend_degraded",
                       "checkpoint_backend_degraded" in (done.get("degraded_flags") or []),
                       f"got={done.get('degraded_flags')}")

    # ④ 发布决策消费：降级 → 非 auto_approve（fail-closed，必须人工审核）
    pub = decide_publication(
        {"review_status": "complete", "total_cases": 1, "issues": [],
         "point_coverage": {"coverage_status": "mapped", "mapping_based": True,
                            "coverage_rate": 1.0}},
        [{"id": "TC-1"}],
        gate_result={"passed": True, "forced_pass": False},
        degraded_flags=["checkpoint_backend_degraded"],
    )
    result.assert_true("降级进入 publication.reasons",
                       "checkpoint_backend_degraded" in (pub.get("reasons") or []),
                       f"reasons={pub.get('reasons')}")
    result.assert_true("降级 → 不允许自动核准",
                       pub.get("decision") != "auto_approve", f"decision={pub.get('decision')}")


# ============================================================================
# 测试 3c: 降级输入前置门禁（v3.4.6 硬失败 / 大哥拍板）
# ============================================================================


def test_srr_is_fp_degraded(result: TestResult):
    """`_srr_is_fp_degraded` 纯逻辑：两个来源 + 假降级防护。

    该函数是生成侧 fail-closed 门禁的**唯一判定入口**。判定错了会两向出事：
      - 漏判（该拦不拦）→ 降级 SRR 产出的用例进入自动发布流程；
      - 误判（不该拦乱拦）→ 正常需求无法生成（比降级更糟，直接堵死主路径）。
    故必须有「未降级 → 不触发」的反向断言，防止条件写反或默认 True。
    """
    print("\n[测试 3c] 降级输入判定（_srr_is_fp_degraded）")

    # ① 干净 SRR → 不触发（假降级防护，防判定条件写反 / 默认值错误）
    ok, detail = _srr_is_fp_degraded({"requirement": {"title": "正常需求"}})
    result.assert_true("干净 SRR 不触发门禁", not ok, f"detail={detail}")
    result.assert_eq("干净 SRR 无原因文案", detail, "")

    # ② 非 dict 输入（None / 字符串）→ 不触发，且不得抛异常
    ok_none, _ = _srr_is_fp_degraded(None)
    ok_str, _ = _srr_is_fp_degraded("not-a-dict")
    result.assert_true("None 输入不触发且不抛异常", not ok_none)
    result.assert_true("非 dict 输入不触发且不抛异常", not ok_str)

    # ③ 来源一：_fp_extract_degraded（需求侧 ReqAgent 落库标记）
    ok_fp, detail_fp = _srr_is_fp_degraded(
        {"_fp_extract_degraded": True, "_fp_extract_error": "TimeoutError: LLM 超时"}
    )
    result.assert_true("_fp_extract_degraded → 触发门禁", ok_fp)
    result.assert_true("原因文案含来源标记", "_fp_extract_degraded" in detail_fp, detail_fp)
    result.assert_true("原因文案含底层错误", "TimeoutError" in detail_fp, detail_fp)

    # ④ 来源二：_finalize_degraded（最终 SRR 重生成失败、降级沿用首次解析）
    ok_fin, detail_fin = _srr_is_fp_degraded(
        {"_finalize_degraded": True, "_finalize_error": "ValueError: 字段缺失"}
    )
    result.assert_true("_finalize_degraded → 触发门禁", ok_fin)
    result.assert_true("原因文案含来源标记", "_finalize_degraded" in detail_fin, detail_fin)

    # ⑤ 双来源同时命中 → 两个都出现在文案里（便于定位是哪条链路降级）
    ok_both, detail_both = _srr_is_fp_degraded(
        {"_fp_extract_degraded": True, "_finalize_degraded": True}
    )
    result.assert_true("双来源同时触发", ok_both)
    result.assert_true("文案列出两个来源",
                       "_fp_extract_degraded" in detail_both and "_finalize_degraded" in detail_both,
                       detail_both)

    # ⑥ 显式假值（False / 0 / ""）→ 不触发（防止把 falsy 当降级）
    ok_false, _ = _srr_is_fp_degraded({"_fp_extract_degraded": False, "_finalize_degraded": 0})
    result.assert_true("显式假值不触发", not ok_false)


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

    # 生成准备节点（原通道选择）：v3.4.4 起不再携带 channel 字段（通道分级已删除）
    sc_event = next(e for e in events if e["stage"] == "select_channel")
    result.assert_true("事件不再携带 channel 字段", "channel" not in sc_event)

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
# 测试 10b: 补生成失败不得击穿流水线（缺陷 B 防回归）
# ============================================================================


async def test_review_supplement_failure_keeps_cases(result: TestResult):
    """补生成是增强步骤：LLM 失败时保留主用例集继续审核/入库，不得整条抛异常。

    缺陷形态（实证 3/3）：主用例集已完整生成（27/11/9 条），仅因补生成这一次
    LLM 调用超时，run_pipeline 就整条抛异常 → 已生成用例全部丢弃、0 落库，
    用户拿到「跑了十几分钟、额度烧掉、零产出」。
    """
    print("\n[测试 10b] 补生成失败不得击穿流水线")
    from app.agents.gen_agent import gen_agent
    from app.agents.review_agent import review_agent

    first_report = {
        **REVIEW_REPORT,
        "should_supplement": True,
        "supplement_hints": ["验证码错误分支"],
    }
    reviewed_cases = [{**GEN_CASES[0], "confidence": 0.8}]
    review_mock = AsyncMock(side_effect=[(first_report, reviewed_cases)])
    # 补生成直接炸——模拟所有 LLM 供应商不可用
    gen_mock = AsyncMock(side_effect=RuntimeError("所有 LLM 提供商均不可用（已尝试 2 家，耗时 240s）"))
    persist_mock = AsyncMock(return_value={"auto_approved": 1})
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
        raised = None
        try:
            update = await orch._review_node(state)
        except Exception as e:  # noqa: BLE001
            raised = e
    finally:
        review_agent.review, gen_agent.generate, review_agent.persist_to_kb = originals

    result.assert_eq("补生成失败不应抛出（不击穿流水线）", raised, None)
    if raised is not None:
        return
    result.assert_eq("补生成失败后仍保留主用例集", update["reviewed_cases"], reviewed_cases)
    result.assert_eq("补生成轮次仍记为 1（不重试）", update["supplement_iteration"], 1)
    result.assert_true("报告标记 supplement_failed",
                       update["review_result"].get("supplement_failed") is True)
    result.assert_true("报告标记 supplement_exhausted（前端不再期待补生成）",
                       update["review_result"].get("supplement_exhausted") is True)
    result.assert_true("降级标记含 supplement_failed",
                       "supplement_failed" in (update.get("degraded_flags") or []))
    result.assert_eq("主用例集仍走知识库回流", persist_mock.await_count, 1)


# ============================================================================
# 测试 12: 发布门禁 fail-closed（docs/15 §4.1 / P0-2）
# ============================================================================


async def test_publication_gate_fail_closed(result: TestResult):
    """畸形用例不得自动发布：不中断流水线、判 partial_block、坏条剔除好条不污染。

    v3.5.2（docs/21 方案B"剔坏留好"）更新契约：
    - 内容级缺陷 → partial_block（原 blocked 整批连坐已废弃）
    - 坏条剔除（KB 回流统计 purged_cases），剩余条正常分级
    - 本用例只有 1 条畸形用例 → 剔除后无存活 → fail-closed 退化为拒绝写入
    对应 docs/15 §4.1 验收：合同不合格的内容不能变成权威知识。
    """
    print("\n[测试 12] 发布门禁 fail-closed（v3.5.2 剔坏留好）")

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
        # persist_to_kb 走真实实现：单条畸形 → 剔除后无存活 → 必须拒绝写入
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
    result.assert_eq("发布决策=partial_block（剔坏留好）",
                     publication.get("decision"), "partial_block")
    result.assert_true("case_level_reasons 含 invalid_case_content",
                       "invalid_case_content" in (publication.get("case_level_reasons") or []))
    result.assert_eq("终态 awaiting_human_review（非 failed）",
                     done.get("completion_status"), "awaiting_human_review")
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


async def test_gate_reject_degraded_srr(result: TestResult):
    """降级 SRR → 入口门禁拒绝（fail-closed）：error 事件 + 无 done + 不推 DLQ。

    大哥拍板「改成硬失败，不允许质量不佳的内容漏过去」（2026-09-28）。
    此前降级 SRR 只在 done 事件透出 degraded_flags，产物仍落入 PG（仅拦 Milvus），
    人工点一次 approve 即可把降级内容变成权威知识 —— 门禁把它挡在**产生副作用之前**。

    四条断言对应门禁的四个可观测面：
      ① 产出 error 事件（终态可见，非裸 500）；
      ② error 事件的 reason=degraded_srr_rejected / rejected_at=entry（前端可精确判定）；
      ③ 不产出 done 事件（没有产物 ⇒ 不可能被 approve ⇒ 权威库不被污染）；
      ④ 门禁拒绝**不推 DLQ**（故意拒绝 ≠ 意外故障，重试必然再次失败）。
    """
    print("\n[测试 13] 降级 SRR 入口门禁（硬失败）")

    # 构造带降级标记的 SRR（需求侧 ReqAgent 落库标记形态）
    degraded_srr = copy.deepcopy(SRR_P2_FAST)
    degraded_srr["_fp_extract_degraded"] = True
    degraded_srr["_fp_extract_error"] = "TimeoutError: LLM provider unavailable"

    dlq_push_calls: list[dict] = []

    def _fake_push(*args, **kwargs):
        dlq_push_calls.append({"args": args, "kwargs": kwargs})
        return True

    import app.core.dlq as _dlq_mod
    _orig_push = _dlq_mod.dlq.push
    _dlq_mod.dlq.push = _fake_push
    try:
        events = []
        with AgentMocks():
            async for event in orchestrator.run_pipeline("用户登录", srr_json=degraded_srr):
                events.append(event)
    finally:
        _dlq_mod.dlq.push = _orig_push

    # ① error 事件存在
    result.assert_true("含 error 事件", any(e["stage"] == "error" for e in events))
    # ② reason / rejected_at 精确
    err = next((e for e in events if e["stage"] == "error"), {})
    result.assert_eq("reason=degraded_srr_rejected", err.get("reason"), "degraded_srr_rejected")
    result.assert_eq("rejected_at=entry", err.get("rejected_at"), "entry")
    # 错误文案不得被「流水线异常:」前缀污染（需给用户可执行的修复指引）
    result.assert_true("错误文案含修复指引（重新分析）",
                       "重新分析" in str(err.get("message", "")),
                       str(err.get("message", ""))[:120])
    # ③ 无 done 事件
    result.assert_true("无 done 事件（不产生产物）", not any(e["stage"] == "done" for e in events))
    # ④ 门禁拒绝不推 DLQ
    result.assert_eq("门禁拒绝不推 DLQ", len(dlq_push_calls), 0)


async def test_gate_reject_checkpoint_backend(result: TestResult):
    """检查点后端初始化失败 → fail-closed：error 事件 + reason=checkpoint_backend_unavailable。

    对应 `_ensure_checkpoint_backend()` 的 v3.4.6 硬失败改造：
    配置 postgres 却初始化失败时不再静默降级为 memory（重启即丢断点），
    而是在 try 内抛 CheckpointBackendError → 转为 error 事件（终态可见，不击穿 500）。
    """
    print("\n[测试 14] 检查点后端不可用门禁（硬失败）")

    dlq_push_calls: list[dict] = []

    def _fake_push(*args, **kwargs):
        dlq_push_calls.append({"args": args, "kwargs": kwargs})
        return True

    # 注入：让 _ensure_checkpoint_backend 抛 CheckpointBackendError
    async def _raise_cb_err():
        raise CheckpointBackendError("模拟：PostgresSaver 初始化失败")

    import app.core.dlq as _dlq_mod
    _orig_push = _dlq_mod.dlq.push
    _orig_ensure = orchestrator._ensure_checkpoint_backend
    _orig_checkpointer = orchestrator._checkpointer
    _dlq_mod.dlq.push = _fake_push
    orchestrator._ensure_checkpoint_backend = _raise_cb_err
    # 确保走初始化分支（否则 _checkpointer 非空会直接 return）
    orchestrator._checkpointer = None
    try:
        events = []
        with AgentMocks():
            async for event in orchestrator.run_pipeline("用户登录", srr_json=SRR_P2_FAST):
                events.append(event)
    finally:
        _dlq_mod.dlq.push = _orig_push
        orchestrator._ensure_checkpoint_backend = _orig_ensure
        orchestrator._checkpointer = _orig_checkpointer

    err = next((e for e in events if e["stage"] == "error"), {})
    result.assert_true("含 error 事件", bool(err))
    result.assert_eq("reason=checkpoint_backend_unavailable",
                     err.get("reason"), "checkpoint_backend_unavailable")
    result.assert_eq("rejected_at=entry", err.get("rejected_at"), "entry")
    result.assert_true("无 done 事件", not any(e["stage"] == "done" for e in events))
    result.assert_eq("门禁拒绝不推 DLQ", len(dlq_push_calls), 0)


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
    await test_review_supplement_failure_keeps_cases(result)
    await test_publication_gate_fail_closed(result)
    await test_pipeline_error(result)
    await test_gate_reject_degraded_srr(result)
    await test_gate_reject_checkpoint_backend(result)
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
    test_initial_degraded_flags(result)
    test_srr_is_fp_degraded(result)
    test_apply_clarify_answers(result)
    test_generation_logic(result)

    # 异步测试（端到端流水线，mock Agent）
    asyncio.run(async_main(result))

    success = result.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
