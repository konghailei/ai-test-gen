"""LangGraph DAG 编排器。

将 6 个 Agent 串联为 DAG 工作流，实现从原始需求到测试用例的端到端生成。
使用 LangGraph StateGraph 声明流程结构，通过 astream 流式输出 SSE 事件。

设计依据：
- docs/00-architecture-overview.md §2 编排层
- docs/00-architecture-overview.md §3 核心数据流（8 阶段）
- docs/00-architecture-overview.md §3.6（从需求分析到测试设计的衔接，v2.3）
- docs/00-architecture-overview.md §9.1 通道选择
- docs/00-architecture-overview.md §9.2 进度反馈机制（SSE）
- docs/01-requirement-parsing.md §5.3 门禁流程（3 轮迭代）
- docs/03-case-generation-flow.md §6.7 ClarifyLoopGuard 死锁预防
- docs/05-technical-selection.md §3.2 LangGraph 流程定义示例

DAG 结构：
    START → parse → select_channel
                      ↓
                [route_after_channel]
                 /              \
               FULL             FAST
                ↓                ↓
            clarify          gate(直接)
                ↓                ↓
           [route_after_gate]    ↓
            /     |     \        ↓
         retry  pass  force    ↓
           ↓     ↓      ↓       ↓
        clarify impact impact   ↓
                  ↓      ↓      ↓
          test_design test_design  ↓   ⭐ v2.3 新增测试设计节点
                  ↓      ↓      ↓
               generate ←──────┘
                  ↓
                 review
                  ↓
                 END

关键决策（plan §五 D1-D10）：
    - D2：Clarify 流程采用 API 驱动，不使用 LangGraph interrupt
    - D3：用例生成不再检索知识库（v3.1 起移除 retrieve_node，直接基于 SRR 生成）
    - D4：SSE 用 sse-starlette（本模块只产事件 dict，序列化由 API 层负责）
"""

from __future__ import annotations

import copy
import time
import asyncio
from typing import Any, AsyncGenerator, TypedDict

from langgraph.graph import END, StateGraph

from app.agents.clarify_agent import clarify_agent
from app.agents.gen_agent import gen_agent, GenAgent
from app.agents.impact_agent import impact_agent
from app.agents.req_agent import req_agent
from app.agents.review_agent import review_agent
from app.agents.test_design_agent import test_design_agent
from app.config import settings
from app.core.channel_selector import select_channel
from app.core.quality_gate import quality_gate
from app.utils.logger import logger


# ============================================================================
# PipelineState 状态定义（plan §T4 第 338-352 行）
# ============================================================================


class PipelineState(TypedDict, total=False):
    """LangGraph 流水线状态。

    沿途累积各节点输出，供下游节点读取。
    使用 total=False 让所有字段可选，允许渐进式填充。

    字段说明：
        # ---- 输入 ----
        raw_requirement: 原始需求文本
        product_id: 产品 ID（可选，传入则注入到 SRR）
        product_name: 产品名称（可选）
        clarify_answers: 交互式澄清答案（可选，首轮不传）
        existing_req_id: 已有需求 ID（可选，复用避免 test_cases FK 违反）

        # ---- 各节点输出 ----
        srr: ReqAgent 解析后的 SRR JSON
        req_id: 需求 ID（从 SRR 提取，便于日志追踪）
        channel: 通道类型 "FAST" / "FULL"
        channel_config: ChannelConfig 序列化后的 dict
        clarify_result: ClarifyAgent 输出
        gate_result: QualityGate 输出（dict 形式）
        impact_result: ImpactAnalysisAgent 输出
        test_design_blueprint: TestDesignAgent 测试设计蓝图（v2.3 新增）
        test_cases: GenAgent 生成的用例列表
        review_result: ReviewAgent 审核报告
        reviewed_cases: ReviewAgent 更新后的用例列表

        # ---- 控制流 ----
        clarify_iteration: 当前已完成的澄清轮次（0/1/2/3）
        forced_pass: 是否强制通过（达到最大轮次仍未通过时为 True）
        error: 异常信息

        # ---- 元信息 ----
        start_time: 流水线启动时间戳
    """

    # 输入
    raw_requirement: str
    product_id: str
    product_name: str
    clarify_answers: dict
    existing_req_id: str
    # ⚠️ srr_json：调用方传入的预解析 SRR（用于 _parse_node 跳过 LLM 重新解析）。
    # 必须在 PipelineState 中显式声明，否则 LangGraph StateGraph 会过滤掉
    # 未定义字段（initial_state 中传入也会被丢弃），导致 _parse_node 读到 None。
    srr_json: dict

    # 节点输出
    srr: dict
    req_id: str
    channel: str
    channel_config: dict
    clarify_result: dict
    gate_result: dict
    impact_result: dict
    test_design_blueprint: dict
    # ⚠️ 必须在 PipelineState 中显式声明（同 consistency_check），否则 LangGraph 会过滤掉
    # 未定义字段，导致 _review_node 的要点级覆盖统计拿不到 FP 级要点（仅统计总 SRR）
    functional_points: list
    test_cases: list
    review_result: dict
    reviewed_cases: list
    kb_flowback: dict
    regression_reuse_summary: dict
    supplement_iteration: int
    # v2.0：一致性检查结果（由 _gen_node 产出，done 事件消费）
    # ⚠️ 必须在 PipelineState 中显式声明，否则 LangGraph StateGraph 会过滤掉
    # 未定义字段，导致 done 事件中 consistency_check 始终为空
    consistency_check: dict
    # v3.1.3：降级标记（由各节点追加，done 事件透传，前端可提示能力降级）
    # ⚠️ 必须显式声明（同 consistency_check），否则被 StateGraph 过滤
    degraded_flags: list
    # v3.4：用例合同校验问题（只记录不上抛，随 state 透传到发布策略）
    content_errors: list
    publication: dict
    completion_status: str
    failed_subtasks: list
    task_id: str
    _fp_subtasks: list
    _fp_groups: list

    # 控制流
    clarify_iteration: int
    forced_pass: bool
    error: str

    # 元信息
    start_time: float


# ============================================================================
# SSE 事件构造（plan §T4 第 389-395 行，doc-00 §9.2）
# ============================================================================

# stage → progress 百分比映射（doc-00 §9.2，v3.1 移除 retrieve）
STAGE_PROGRESS: dict[str, int] = {
    "parse": 20,
    "select_channel": 25,
    "clarify": 30,
    "gate": 40,
    "impact": 50,
    "test_design": 62,
    "generate": 70,
    "review": 90,
    "done": 100,
    "error": 100,
}


def make_sse_event(stage: str, message: str, **extra: Any) -> dict:
    """构造 SSE 事件字典。

    Args:
        stage: 阶段名称（parse/select_channel/clarify/gate/impact/test_design/generate/review/done/error）
        message: 人类可读的进度消息
        **extra: 附加字段（如 req_id、channel、cases_count 等）

    Returns:
        dict: SSE 事件，格式：
            {"stage": "...", "message": "...", "progress": 0-100, ...附加字段}

    Note:
        本函数只产 dict，不负责 SSE 序列化（由 API 层的 sse-starlette 负责）。
    """
    event: dict[str, Any] = {
        "stage": stage,
        "message": message,
        "progress": STAGE_PROGRESS.get(stage, 0),
    }
    event.update(extra)
    return event


# ============================================================================
# ClarifyLoopGuard（doc-03 §6.7 第 1019-1048 行）
# ============================================================================


class ClarifyLoopGuard:
    """防止 QualityGate ↔ ClarifyAgent 无限循环。

    设计依据：doc-03 §6.7
    最大轮次：settings.MAX_CLARIFY_ITERATIONS（默认 3）
    达到上限仍未通过时强制通过并标记警告，避免流水线死锁。

    与 LangGraph 条件边的协作：
        _route_after_gate 调用本类的 should_retry / should_force_pass 判断路由。
    """

    @staticmethod
    def should_retry(iteration: int, gate_passed: bool) -> bool:
        """判断是否应该回环到 clarify 再试一轮。

        Args:
            iteration: 当前已完成的澄清轮次
            gate_passed: QualityGate 是否通过

        Returns:
            True 表示应该回环到 clarify（未通过且仍有剩余轮次）
        """
        if gate_passed:
            return False
        return iteration < settings.MAX_CLARIFY_ITERATIONS

    @staticmethod
    def should_force_pass(iteration: int, gate_passed: bool) -> bool:
        """判断是否应该强制通过（达到最大轮次仍未通过）。

        Args:
            iteration: 当前已完成的澄清轮次
            gate_passed: QualityGate 是否通过

        Returns:
            True 表示应强制通过，避免死锁
        """
        if gate_passed:
            return False
        return iteration >= settings.MAX_CLARIFY_ITERATIONS


# ============================================================================
# Orchestrator 编排器
# ============================================================================


class Orchestrator:
    """LangGraph DAG 编排器。

    将 5 个 Agent 串联为 DAG 工作流：
        ReqAgent → ClarifyAgent → QualityGate → [ImpactAgent] → GenAgent → ReviewAgent

    通道选择（doc-00 §9.1）：
        - FULL：完整流程（含 clarify 回环 + impact + 多类型生成 + 完整审核）
        - FAST：跳过 clarify，直接 gate → gen → review

    Clarify 回环（doc-01 §5.3 + doc-03 §6.7）：
        - QualityGate 不达标且无 blocker-only 问题时，回环到 ClarifyAgent
        - 最多 3 轮，超过则强制通过 + 警告

    Usage:
        orchestrator = Orchestrator()
        async for event in orchestrator.run_pipeline("用户登录功能..."):
            print(event)  # SSE 事件 dict
    """

    def __init__(self) -> None:
        """初始化编排器，构建并编译 LangGraph 状态图。"""
        self.graph = self._build_graph()
        # v3.1.3 细粒度生成进度事件队列（run_pipeline 内创建，_gen_node 消费）
        from contextvars import ContextVar
        self._gen_event_queue: ContextVar = ContextVar("gen_event_queue", default=None)

    # ========================================================================
    # v3.1.3 断点续跑 checkpoint（断电/断网中断后，续跑跳过已完成功能点）
    # ========================================================================
    async def _save_gen_checkpoint(self, task_id: str, fp_groups: list[dict]) -> None:
        """把各功能点已生成的结果写入 analysis_tasks.result_json，供断点续跑恢复。

        Args:
            task_id: 生成任务 ID
            fp_groups: 每功能点 {fp_id, fp_name, case_count, cases}
        """
        if not task_id:
            return
        try:
            from app.db.database import async_session
            from app.db.models import AnalysisTask
            from sqlalchemy import select as _sel
            async with async_session() as _s:
                row = await _s.execute(_sel(AnalysisTask).where(AnalysisTask.id == task_id))
                t = row.scalar_one_or_none()
                if t is None:
                    return
                old = dict(t.result_json or {})
                old["checkpoint"] = {"fp_groups": list(fp_groups), "completed": len(fp_groups)}
                t.result_json = old
                await _s.commit()
        except Exception as e:
            logger.debug("[Orchestrator] checkpoint 持久化失败（不阻断）: {}", str(e)[:120])

    async def _load_gen_checkpoint(self, task_id: str) -> dict | None:
        """读取任务的 checkpoint（含已完成功能点的生成结果）。"""
        if not task_id:
            return None
        try:
            from app.db.database import async_session
            from app.db.models import AnalysisTask
            from sqlalchemy import select as _sel
            async with async_session() as _s:
                row = await _s.execute(_sel(AnalysisTask).where(AnalysisTask.id == task_id))
                t = row.scalar_one_or_none()
                if t is None or not isinstance(t.result_json, dict):
                    return None
                cp = t.result_json.get("checkpoint")
                return cp if isinstance(cp, dict) else None
        except Exception as e:
            logger.warning("[Orchestrator] checkpoint 读取失败: {}", str(e)[:120])
            return None

    # ========================================================================
    # 图构建
    # ========================================================================

    def _build_graph(self):
        """构建 LangGraph 状态图。

        节点：parse / select_channel / clarify / gate / impact / test_design / generate / review
        条件边：
            - select_channel → clarify (FULL) / gate (FAST)
            - gate → impact (通过或强制) / clarify (回环重试)

        Returns:
            CompiledGraph: 已编译的可执行图
        """
        graph: StateGraph = StateGraph(PipelineState)

        # 注册节点（v3.1：移除 retrieve 节点，SRR 足够详细无需 KB 检索）
        graph.add_node("parse", self._parse_node)
        graph.add_node("select_channel", self._select_channel_node)
        graph.add_node("clarify", self._clarify_node)
        graph.add_node("gate", self._gate_node)
        graph.add_node("impact", self._impact_node)
        # v2.3：新增测试设计节点 TestDesignAgent（impact → test_design → generate）
        graph.add_node("test_design", self._test_design_node)
        graph.add_node("generate", self._gen_node)
        graph.add_node("review", self._review_node)

        # 入口
        graph.set_entry_point("parse")

        # 固定边（v2.3：impact → test_design → generate，跳过 retrieve）
        graph.add_edge("parse", "select_channel")
        graph.add_edge("clarify", "gate")
        graph.add_edge("impact", "test_design")
        graph.add_edge("test_design", "generate")
        graph.add_edge("generate", "review")
        graph.add_edge("review", END)

        # 条件边：select_channel → FULL 走 clarify / FAST 直接到 gate
        graph.add_conditional_edges(
            "select_channel",
            self._route_after_channel,
            {"clarify": "clarify", "gate": "gate"},
        )

        # 条件边：gate → 通过走 impact / 不通过且可重试回 clarify / 强制通过走 impact
        graph.add_conditional_edges(
            "gate",
            self._route_after_gate,
            {"impact": "impact", "clarify": "clarify"},
        )

        return graph.compile()

    # ========================================================================
    # 节点实现
    # ========================================================================

    async def _parse_node(self, state: PipelineState) -> dict:
        """节点 1：使用已确认的 SRR（需求分析/澄清后形成的最终结构化需求）。

        对应 docs/00 §3 阶段①。
        生成用例必须使用已确认的 SRR，不允许回退到 LLM 重新解析原始文本，
        防止 SRR 简化导致通道选择错误和用例质量下降。

        Raises:
            ValueError: state 中无有效的 srr_json

        Returns:
            dict: state 更新，含 srr / req_id / clarify_iteration / start_time
        """
        product_id = state.get("product_id", "")

        pre_parsed_srr = state.get("srr_json")
        if not pre_parsed_srr or not pre_parsed_srr.get("requirement"):
            logger.error(
                "[Orchestrator] _parse_node: SRR 为空，无法生成用例。"
                "req_id={}, 请确保需求已完成分析确认流程。",
                state.get("existing_req_id", ""),
            )
            raise ValueError(
                "SRR 为空，无法生成测试用例。请先完成需求分析流程（需求解析→澄清→确认）。"
            )

        srr = pre_parsed_srr
        req_id = srr.get("requirement", {}).get("id", "")

        logger.info(
            "[Orchestrator] ▶ 节点 parse (使用已有 SRR): req_id={}, title={}, "
            "flows.steps={}",
            req_id,
            srr.get("requirement", {}).get("title", ""),
            len(srr.get("flows", {}).get("happy_path", {}).get("steps", []) or []),
        )

        # 如果传入了已有 req_id（从 DB 取的需求），覆盖 SRR 中的 req_id
        existing_req_id = state.get("existing_req_id", "")
        if existing_req_id:
            logger.info(
                "[Orchestrator] 使用已有 req_id 覆盖 SRR 中的 req_id: {} → {}",
                req_id, existing_req_id,
            )
            req_id = existing_req_id
            srr.setdefault("requirement", {})["id"] = existing_req_id

        logger.info(
            "[Orchestrator] ◀ 节点 parse 完成 ▶ 下一节点 select_channel: "
            "req_id={}, title={}",
            req_id,
            srr.get("requirement", {}).get("title", ""),
        )

        return {
            "srr": srr,
            "req_id": req_id,
            "clarify_iteration": 0,
            "start_time": time.time(),
        }

    async def _select_channel_node(self, state: PipelineState) -> dict:
        """节点 2：通道选择（FAST / FULL）。

        对应 docs/00 §9.1。
        判定规则：priority==P0 或 happy_path 步骤 > 5 或 含 optimization_context → FULL。

        重要修正（v3.x 新增）：
            若传入 existing_req_id，说明用户已通过 API 走完确认流程
            （SRR / FP / 影响分析均已审核确认，status == confirmed），
            此时**不应**再自动回 clarify 回环，否则会导致：
                ① 生成前无谓消耗 LLM（产出的 needs_clarification 问题已无意义，
                  因为无人回答）
                ② 网络/配额等故障导致 clarify 卡死，长时间看似无 LLM 调用
            因此对已确认需求强制跳过 enable_clarify（但保留 multi_type /
            full_review / impact_analysis 等"高质量生成侧"能力，等同于
            FULL 通道仅关闭 clarify）。

        Returns:
            dict: state 更新，含 channel / channel_config
        """
        srr = state.get("srr", {})
        req_id = state.get("req_id", "")
        existing_req_id = state.get("existing_req_id", "") or ""

        logger.info("[Orchestrator] ▶ 节点 select_channel: req_id={} existing_req_id={}", req_id, existing_req_id)

        config = select_channel(srr)
        # 已确认需求：强制跳过 clarify（进入 gate → impact → ...），
        # 仍保留 FULL 的其他质量能力（多类型、完整审核、影响分析）。
        if existing_req_id:
            from app.core.channel_selector import ChannelConfig
            config = ChannelConfig(
                channel=config.channel,  # 保留 FULL 名义
                enable_clarify=False,    # ★ 关键：不再跑 clarify 回环
                enable_impact_analysis=config.enable_impact_analysis,
                enable_multi_type_generation=True,
                enable_full_review=True,
            )
            logger.info(
                "[Orchestrator] 确认需求已存在（existing_req_id={})，跳过 ClarifyAgent 回环: "
                "保留 multi_type & full_review & impact_analysis，仅把 enable_clarify=False",
                existing_req_id,
            )
        next_node = "clarify" if (config.is_full and config.enable_clarify) else "gate"
        channel = config.channel

        logger.info(
            "[Orchestrator] ◀ 节点 select_channel 完成 ▶ 下一节点 {}: "
            "channel={}, clarify={}, impact={}, multi_type={}, full_review={}",
            next_node,
            channel,
            config.enable_clarify,
            config.enable_impact_analysis,
            config.enable_multi_type_generation,
            config.enable_full_review,
        )

        return {
            "channel": channel,
            "channel_config": config.model_dump(),
        }

    async def _clarify_node(self, state: PipelineState) -> dict:
        """节点 3：ClarifyAgent 需求澄清。

        对应 docs/00 §3 阶段②。
        首轮若有交互式澄清答案，先应用到 SRR（D2 决策，API 驱动）。

        Returns:
            dict: state 更新，含 clarify_result / clarify_iteration（+1）/ 可能更新 srr
        """
        srr = state.get("srr", {})
        iteration = state.get("clarify_iteration", 0) + 1
        req_id = state.get("req_id", "")

        logger.info(
            "[Orchestrator] ▶ 节点 clarify (第{}轮): req_id={}",
            iteration,
            req_id,
        )

        # 若首轮有交互式澄清答案，先应用到 SRR（ClarifyAgent 未提供 refine 方法，由编排器实现）
        answers = state.get("clarify_answers")
        if answers and iteration == 1:
            srr = self._apply_clarify_answers(srr, answers)
            logger.debug(
                "[Orchestrator] 已应用交互式澄清答案: keys={}",
                list(answers.keys()) if isinstance(answers, dict) else len(answers),
            )

        result = await clarify_agent.clarify(srr)

        # 优先使用 KB 增强后的 SRR，避免 augmented_srr 丢失。
        # ClarifyAgent.clarify() 内部用 KB 自动补全生成 augmented_srr（v2.6），
        # 若不写回 state，下游 GenAgent/RetrieveAgent 拿到的是补全前的草稿 SRR。
        augmented_srr = result.get("augmented_srr")
        if augmented_srr:
            srr = augmented_srr

        status = result.get("status", "")
        questions = result.get("questions", [])
        score = result.get("quality_report", {}).get("overall_score", 0.0)

        logger.info(
            "[Orchestrator] ◀ 节点 clarify 完成 (第{}轮) ▶ 下一节点 gate: "
            "status={}, questions={}, overall_score={:.3f}",
            iteration,
            status,
            len(questions),
            score,
        )

        return {
            "clarify_result": result,
            "clarify_iteration": iteration,
            "srr": srr,  # 可能被 _apply_clarify_answers 更新
        }

    async def _gate_node(self, state: PipelineState) -> dict:
        """节点 4：QualityGate 质量门禁检查。

        对应 docs/00 §3 阶段③ + docs/01 §5.3。
        达到最大澄清轮次仍未通过时强制通过（ClarifyLoopGuard）。

        Returns:
            dict: state 更新，含 gate_result / forced_pass
        """
        srr = state.get("srr", {})
        clarify_result = state.get("clarify_result", {})
        iteration = state.get("clarify_iteration", 0)
        req_id = state.get("req_id", "")

        logger.info(
            "[Orchestrator] ▶ 节点 gate (第{}轮): req_id={}",
            iteration,
            req_id,
        )

        result = await quality_gate.check(srr, clarify_result)
        gate_dict = result.model_dump()

        channel = state.get("channel", "FAST")
        # v3.x：已确认需求（enable_clarify=False）与 FAST 通道同待遇：
        # gate 未通过时直接强制通过，不回环 clarify（无人回答，回环无意义且易卡死）
        _cfg = state.get("channel_config", {}) or {}
        clarify_disabled = channel == "FAST" or not (
            _cfg.get("enable_clarify", False) if isinstance(_cfg, dict)
            else getattr(_cfg, "enable_clarify", False)
        )
        # 判断是否强制通过
        forced = ClarifyLoopGuard.should_force_pass(iteration, result.passed)
        # 跳过 clarify 的场景（FAST 通道 / 已确认需求），gate 未通过时直接强制通过，不回环
        if clarify_disabled and not result.passed and not forced:
            forced = True
            gate_dict["forced_pass"] = True
            warnings = gate_dict.get("warnings", []) or []
            warnings.append("跳过澄清场景（FAST 通道或已确认需求），QualityGate 未通过但强制放行")
            gate_dict["warnings"] = warnings
            logger.warning(
                "[Orchestrator] 跳过澄清场景 gate 未通过，强制放行（不回环 clarify）"
            )
        elif forced:
            gate_dict["forced_pass"] = True
            warnings = gate_dict.get("warnings", []) or []
            warnings.append(
                f"达到最大澄清轮次（{settings.MAX_CLARIFY_ITERATIONS}），强制通过"
            )
            gate_dict["warnings"] = warnings
            logger.warning(
                "[Orchestrator] QualityGate 强制通过（达到最大轮次 {}）",
                settings.MAX_CLARIFY_ITERATIONS,
            )
        else:
            gate_dict["forced_pass"] = False

        next_node = "impact" if (result.passed or forced) else "clarify"
        logger.info(
            "[Orchestrator] ◀ 节点 gate 完成 (第{}轮) ▶ 下一节点 {}: "
            "passed={}, blockers={}, warnings={}, cold_start={}, forced={}",
            iteration,
            next_node,
            gate_dict.get("passed"),
            len(gate_dict.get("blockers", []) or []),
            len(gate_dict.get("warnings", []) or []),
            gate_dict.get("cold_start"),
            forced,
        )

        return {
            "gate_result": gate_dict,
            "forced_pass": forced,
        }

    async def _impact_node(self, state: PipelineState) -> dict:
        """节点 5：ImpactAnalysisAgent 影响分析。

        对应 docs/00 §3 阶段④.5。
        仅优化需求（含 optimization_context）且通道启用影响分析时触发实际分析。

        Returns:
            dict: state 更新，含 impact_result
        """
        srr = state.get("srr", {})
        req_id = state.get("req_id", "")
        channel_config = state.get("channel_config", {}) or {}

        logger.info("[Orchestrator] ▶ 节点 impact: req_id={}", req_id)

        # 若通道关闭影响分析，直接返回空结果（FAST 通道优化）
        if not channel_config.get("enable_impact_analysis", True):
            logger.info("[Orchestrator] 通道关闭 impact 分析，跳过")
            return {"impact_result": {"is_optimization": False, "generation_guidance": ""}}

        # 检索原始用例（优化需求时传给 ImpactAgent 做操作规划）
        original_cases = await self._fetch_original_cases(srr)

        result = await impact_agent.analyze(srr, original_cases)

        is_opt = result.get("is_optimization", False)
        guidance_len = len(result.get("generation_guidance", "") or "")

        logger.info(
            # v3.1.3: 修正过时日志——v2.3 起 impact 的下一节点是 test_design 而非 generate
            "[Orchestrator] ◀ 节点 impact 完成 ▶ 下一节点 test_design: "
            "is_optimization={}, guidance_len={}, original_cases={}",
            is_opt,
            guidance_len,
            len(original_cases),
        )

        return {"impact_result": result}

    # v3.1：已移除 KB 检索节点（原 _retrieve_node）。用例生成直接基于 SRR，
    # 不再从知识库检索相似需求/用例/模式注入（该节点已不注册进 LangGraph）。

    async def _test_design_node(self, state: PipelineState) -> dict:
        """节点 4.5：TestDesignAgent 测试设计（v2.3 新增）。

        对应 docs/00 §3 阶段④.5（TestDesignAgent）与 §3.6（从需求分析到测试设计的衔接）。
        从 SRR（总 SRR + FP 子 SRR）自动判定可应用的测试设计技术，产出结构化
        测试设计蓝图（techniques_applicable / boundary_targets / decision_table_targets /
        state_targets / error_guess_hints / scenario_targets + guidance_text）。

        蓝图作为独立阶段产物写入 PipelineState，供 GenAgent（generate）消费与展示；
        GenAgent 内部已复用同源 `_build_test_design_guidance` 注入 prompt，
        因此本节点不改变既有用例生成结果结构。

        Returns:
            dict: state 更新，含 test_design_blueprint
        """
        srr = state.get("srr", {})
        req_id = state.get("req_id", "")
        impact_result = state.get("impact_result", {}) or {}
        impact_guidance = impact_result.get("generation_guidance", "") or ""

        # 从 DB 加载已拆分的功能点，用于 FP 子 SRR 分析
        # v3.1.3: FP 统一加载源——本节点是流水线内首个 FP 消费者，加载完整形状
        # （含 atomicity_check）写入 state，_gen_node/补生成/覆盖率统计统一读 state，
        # 避免同流水线多次查 DB 且形状不一（首次生成与补生成收到不同数据）
        # v3.1.3: ORM 查询收敛到 db/repositories，形状由仓储层唯一定义
        functional_points: list[dict] = []
        degraded_flags: list[str] = list(state.get("degraded_flags", []) or [])
        if req_id:
            try:
                from app.db.repositories import load_functional_points
                functional_points = await load_functional_points(req_id)
            except Exception as e:
                logger.warning(
                    "[Orchestrator] 测试设计加载功能点失败（降级仅总SRR分析）: {}",
                    str(e)[:150],
                )
                # v3.1.3: 降级可观测——FP 加载失败标记透传 done 事件，前端可提示
                degraded_flags.append("fp_load_failed")

        logger.info(
            "[Orchestrator] ▶ 节点 test_design: req_id={}, fp_count={}, has_guidance={}",
            req_id,
            len(functional_points),
            bool(impact_guidance),
        )

        blueprint = test_design_agent.design(
            srr,
            functional_points=functional_points,
            impact_guidance=impact_guidance,
        )

        logger.info(
            "[Orchestrator] ◀ 节点 test_design 完成 ▶ 下一节点 generate: "
            "适用技术={}, guidance_len={}",
            sum(1 for v in blueprint.get("techniques_applicable", {}).values() if v),
            len(blueprint.get("guidance_text", "") or ""),
        )

        # v3.4: 回归历史用例装配——影响分析确认的受影响 FP，直接拉取其最新版
        # approved 用例（functional_point_cases，按项目 go_live_date 选最新线）归类到回归，
        # 并输出覆盖缺口供 generate 补生成（回归测试选择 RTS）。
        # 复用历史保持稳定与成本可控；缺口才交给 LLM 增量生成。
        historical_regression, coverage_gaps = await self._assemble_regression_cases(
            srr,
            functional_points=functional_points,
        )

        blueprint["historical_regression_cases"] = historical_regression
        blueprint["coverage_gaps"] = coverage_gaps
        if historical_regression or coverage_gaps:
            logger.info(
                "[Orchestrator] ◀ test_design 回归装配: reused={}, gaps={}",
                len(historical_regression), len(coverage_gaps),
            )

        return {
            "test_design_blueprint": blueprint,
            "functional_points": functional_points,
            "degraded_flags": degraded_flags,
            "regression_reuse_summary": {
                "reused_count": len(historical_regression),
                "gap_count": len(coverage_gaps),
            },
        }

    async def _assemble_regression_cases(
        self,
        srr: dict,
        functional_points: list[dict] | None = None,
    ) -> tuple[list[dict], list[dict]]:
        """装配受影响功能点的最新版回归用例 + 覆盖缺口。

        v3.4 新增（回归测试选择 RTS）：
        - 对影响分析中已确认的受影响 FP（is_affected=True），取 functional_point_cases
          固化的最新版 approved 用例，标记 type=regression + 标题[回归]、source=reused；
        - 该 FP 无历史用例 / 覆盖不足 → 记入 coverage_gaps，交由 generate 补生成。
        - 未固化的 FP（active 项目）回退到 schema 现场聚合，保证实时性。

        Returns:
            tuple: (historical_regression: list[dict], coverage_gaps: list[dict])
        """
        srr_req = srr.get("requirement", {}) if isinstance(srr, dict) else {}
        product_id = srr_req.get("product_id", "")
        req_id = srr_req.get("id", "")

        # 影响分析确认的受影响 FP 名称集合
        affected_names: list[dict] = []  # [{fp_name, fp_id, test_focus}]
        impact_items = srr.get("impact_analysis", []) if isinstance(srr, dict) else []
        for it in impact_items:
            if not isinstance(it, dict):
                continue
            if it.get("is_affected") is True and it.get("confirmed_at"):
                affected_names.append({
                    "fp_id": it.get("fp_id", ""),
                    "fp_name": it.get("fp_name", ""),
                    "test_focus": it.get("test_focus", ""),
                })

        historical_regression: list[dict] = []
        coverage_gaps: list[dict] = []
        if not product_id or not affected_names:
            return historical_regression, coverage_gaps

        from app.db.database import async_session
        from app.core.fp_cases import aggregate_latest_cases

        # v3.1.3: try 下沉到循环体内——单个 FP 装配失败只跳过该 FP，
        # 不再因一个 FP 中断导致其余 FP 的回归装配全部丢失；
        # session 级失败（连接不可用）整体降级为无回归基线（不阻断主流程）
        try:
            session_ctx = async_session()
        except Exception as e:
            logger.warning(
                "[Orchestrator] 回归装配 session 创建失败（降级为无回归基线）: err={}",
                str(e)[:150],
            )
            return historical_regression, coverage_gaps

        async with session_ctx as session:
            for item in affected_names:
                fp_name = item.get("fp_name", "")
                if not fp_name:
                    continue
                try:
                    agg = await aggregate_latest_cases(
                        session,
                        product_id,
                        fp_name,
                        fp_id=item.get("fp_id") or None,
                    )
                except Exception as e:
                    logger.warning(
                        "[Orchestrator] 单个 FP 回归装配失败（跳过该 FP）: fp={}, err={}",
                        fp_name, str(e)[:120],
                    )
                    continue
                cases = agg.get("cases") or []
                if not cases:
                    # 无历史用例 → 缺口，交 GenAgent 补生成
                    coverage_gaps.append({
                        "fp_id": item.get("fp_id", ""),
                        "fp_name": fp_name,
                        "test_focus": item.get("test_focus", ""),
                        "reason": "无历史 approved 用例，需按影响功能说明补生成回归用例",
                    })
                    continue
                # 复用历史用例并标记为回归
                for c in cases:
                    if not isinstance(c, dict):
                        continue
                    cc = dict(c)
                    cc["type"] = "regression"
                    cc["title"] = f"[回归]{cc.get('title', '')}"
                    cc["related_fp"] = fp_name
                    cc["source"] = "reused"
                    cc["source_case_id"] = cc.get("id", "")
                    cc["gen_metadata"] = dict(cc.get("gen_metadata") or {})
                    cc["gen_metadata"].setdefault("regression_source", "reused")
                    historical_regression.append(cc)

        return historical_regression, coverage_gaps

    async def _gen_node(self, state: PipelineState) -> dict:
        """节点 7：GenAgent 生成测试用例。

        对应 docs/00 §3 阶段⑤。
        根据 ChannelConfig 决定是否启用分类生成，注入影响分析指导。
        v1.4：若需求已拆分功能点（FP），从 DB 加载并传入 GenAgent，
        使 LLM 按 FP 定向生成用例，用例标注 related_fp 供追溯。

        Returns:
            dict: state 更新，含 test_cases
        """
        srr = state.get("srr", {})
        channel_config = state.get("channel_config", {}) or {}
        impact_result = state.get("impact_result", {}) or {}
        req_id = state.get("req_id", "")

        enable_multi_type = channel_config.get("enable_multi_type_generation", True)
        impact_guidance = impact_result.get("generation_guidance", "") or ""

        # v1.4：从 DB 加载已拆分的功能点，用于 FP 定向生成
        # v3.1.3: 优先读 state（test_design 节点已统一加载完整形状，含 atomicity_check），
        # 保证首次生成与补生成收到相同数据；state 缺失时回退查 DB（直接调用兜底）
        # v3.1.3: ORM 查询收敛到 db/repositories
        functional_points: list[dict] = state.get("functional_points", []) or []
        degraded_flags: list[str] = list(state.get("degraded_flags", []) or [])
        if not functional_points and req_id:
            try:
                from app.db.repositories import load_functional_points
                functional_points = await load_functional_points(req_id)
                if functional_points:
                    logger.info(
                        "[Orchestrator] 加载功能点: req_id={}, fp_count={}",
                        req_id, len(functional_points),
                    )
            except Exception as e:
                logger.warning(
                    "[Orchestrator] 加载功能点失败（不阻断生成）: req_id={}, err={}",
                    req_id, str(e)[:150],
                )
                degraded_flags.append("fp_load_failed")

        logger.info(
            "[Orchestrator] ▶ 节点 generate: req_id={}, multi_type={}, has_guidance={}, fp_count={}",
            req_id,
            enable_multi_type,
            bool(impact_guidance),
            len(functional_points),
        )

        # ── v3.1.3 FP 分片生成 ──
        # 不再把全量 SRR 一次性喂给 LLM（避免 116K+ 大 prompt 引起卡死/超时/字数爆炸）。
        # 改造：按功能点分片小 prompt 定向生成 + 跨功能点/集成独立通道；
        # 每个 FP 完成后立即写 checkpoint（断电/断网可续跑），并推进细粒度进度事件。
        task_id = state.get("task_id", "")
        resume_enabled = bool(state.get("resume", False))
        resolved_cases: list[dict] = []   # 汇总用例
        completed_fps: set[str] = set()   # 断点恢复时已完成的 FP
        checkpoint_groups: list[dict] = []  # 含用例，用于断点持久化
        fp_subtasks: list[dict] = []        # 细粒度进度卡片
        _gen_queue = self._gen_event_queue.get()

        async def _emit_gen(msg: str, subtask: dict | None = None) -> None:
            """推送细粒度 FP 进度事件（经事件队列按序透出）。"""
            nonlocal fp_subtasks
            if subtask is not None:
                key = subtask.get("key", "")
                fp_subtasks = [s for s in fp_subtasks if s.get("key") != key]
                fp_subtasks.append(subtask)
            snap = [dict(s) for s in fp_subtasks]
            covered_now = sum(1 for s in fp_subtasks if s.get("status") in ("done", "resumed"))
            total_now = sum(1 for s in fp_subtasks if not s.get("key", "").startswith("__"))
            ev = make_sse_event(
                "generate", msg,
                fp_subtask=subtask,
                subtasks=snap,
                fp_progress={"covered": covered_now, "total": max(total_now, covered_now)},
            )
            if _gen_queue is not None:
                try:
                    await _gen_queue.put(ev)
                except Exception:
                    pass
            else:
                logger.debug("[Orchestrator] [gen-sub] {}", msg)

        # 断点恢复：优先使用续跑接口注入的 resume_checkpoint（上一个中断任务），
        # 否则回退按当前 task_id 读取（同一任务重入场景）。
        rchk: dict | None = state.get("resume_checkpoint")
        if resume_enabled and not rchk:
            try:
                rchk = await self._load_gen_checkpoint(task_id)
            except Exception:
                rchk = None
        if rchk:
            for g in rchk.get("fp_groups", []) or []:
                fid = g.get("fp_id", "")
                if not fid or fid in completed_fps:
                    continue
                completed_fps.add(fid)
                for c in g.get("cases", []) or []:
                    resolved_cases.append(c)
                fp_subtasks.append({
                    "key": fid, "name": g.get("fp_name", ""),
                    "status": "resumed", "cases": len(g.get("cases", []) or []),
                })
                checkpoint_groups.append(dict(g))  # 累积，保证二次中断也能续
                logger.info("[Orchestrator] 断点续跑：跳过已完成的 FP={}", fid)

        fp_list = functional_points or []
        # 1) 各功能点分片定向生成（小 prompt）
        for fp in fp_list:
            fid = fp.get("id", "")
            fname = fp.get("name", "")
            if fid in completed_fps:
                continue
            await _emit_gen(
                f"功能点 [{fname}] 生成中...",
                {"key": fid, "name": fname, "status": "generating", "cases": 0},
            )
            try:
                fp_cases = await gen_agent.generate(
                    srr,
                    enable_multi_type=enable_multi_type,
                    impact_guidance=impact_guidance,
                    functional_points=[fp],
                    fp_scope=fp,
                    historical_regression=state.get("test_design_blueprint", {}).get(
                        "historical_regression_cases", []
                    ),
                    coverage_gaps=state.get("test_design_blueprint", {}).get(
                        "coverage_gaps", []
                    ),
                )
            except Exception as e:
                # v3.3（修 N9）：生成通道失败会**静默减少用例数量**——
                # done 事件只报"流水线完成"，用户不知道少了哪些功能点的用例。
                logger.warning(
                    "[Orchestrator] 功能点[{}]分片生成失败（不阻断，续跑将重试）: {}",
                    fname, str(e)[:150],
                )
                if "fp_generate_failed" not in degraded_flags:
                    degraded_flags.append("fp_generate_failed")
                await _emit_gen(
                    f"功能点 [{fname}] 生成失败",
                    {"key": fid, "name": fname, "status": "failed", "cases": 0},
                )
                continue
            for c in fp_cases:
                if not c.get("related_fp"):
                    c["related_fp"] = fid
            resolved_cases.extend(fp_cases)
            checkpoint_groups.append({
                "fp_id": fid, "fp_name": fname,
                "case_count": len(fp_cases), "cases": list(fp_cases),
            })
            await _emit_gen(
                f"功能点 [{fname}] 生成完成：{len(fp_cases)} 条",
                {"key": fid, "name": fname, "status": "done", "cases": len(fp_cases)},
            )
            try:
                await self._save_gen_checkpoint(task_id, checkpoint_groups)
            except Exception as e:
                logger.debug("[Orchestrator] checkpoint 写入失败（不阻断）: {}", str(e)[:120])

        # v3.1.7 修复：FP 为空时的批量兜底通道。
        # v3.1.3 FP 分片改造后，无功能点需求（FP 拆分失败/被跳过/极简需求）会静默
        # 生成 0 条用例（FP 循环不执行、集成通道无 cross_fp 数据也不执行）。
        # 恢复单次全量 SRR 生成，保证任何需求至少有一轮生成。
        if not fp_list:
            await _emit_gen("无功能点拆分，按完整需求单次生成...", None)
            try:
                batch_cases = await gen_agent.generate(
                    srr,
                    enable_multi_type=enable_multi_type,
                    impact_guidance=impact_guidance,
                    functional_points=[],
                    historical_regression=state.get("test_design_blueprint", {}).get(
                        "historical_regression_cases", []
                    ),
                    coverage_gaps=state.get("test_design_blueprint", {}).get(
                        "coverage_gaps", []
                    ),
                )
                for c in batch_cases or []:
                    resolved_cases.append(c)
                logger.info(
                    "[Orchestrator] FP 为空批量兜底生成: {} 条", len(batch_cases or []),
                )
            except Exception as e:
                # v3.3（修 N9）：同"分片生成失败"——静默减少用例数量，需标记
                logger.warning(
                    "[Orchestrator] 全量批量生成失败（不阻断）: {}", str(e)[:150],
                )
                if "batch_generate_failed" not in degraded_flags:
                    degraded_flags.append("batch_generate_failed")

        # 2) Cross-FP content lives under cross_fp_interactions since v2.40.
        _global_srr: dict = {}
        try:
            interactions = srr.get("cross_fp_interactions") or {}
            _cross = interactions.get("cross_fp_flows") or srr.get("cross_fp_flows", []) or []
            _shared = interactions.get("shared_business_rules") or srr.get("shared_business_rules", []) or []
            _data = interactions.get("global_constraints") or srr.get("data_constraints") or {}
            if _cross or _shared or (isinstance(_data, dict) and _data):
                _global_srr = {
                    "requirement": srr.get("requirement", {}),
                    "cross_fp_flows": _cross,
                    "shared_business_rules": _shared,
                    "global_constraints": interactions.get("global_constraints") or srr.get("global_constraints", ""),
                    "flows": srr.get("flows", {}),
                }
        except Exception:
            _global_srr = {}
        if _global_srr:
            await _emit_gen(
                "跨功能点/集成用例生成中...",
                {"key": "__integration__", "name": "跨功能点/集成", "status": "generating", "cases": 0},
            )
            try:
                intg_cases = await gen_agent.generate(
                    srr,
                    enable_multi_type=enable_multi_type,
                    impact_guidance=impact_guidance,
                    functional_points=[],
                    global_srr=_global_srr,
                    historical_regression=state.get("test_design_blueprint", {}).get(
                        "historical_regression_cases", []
                    ),
                    coverage_gaps=state.get("test_design_blueprint", {}).get(
                        "coverage_gaps", []
                    ),
                )
                for c in intg_cases:
                    if not c.get("type") or c.get("type") == "functional":
                        c["type"] = "integration"
                resolved_cases.extend(intg_cases)
                await _emit_gen(
                    f"跨功能点/集成用例生成完成：{len(intg_cases)} 条",
                    {"key": "__integration__", "name": "跨功能点/集成", "status": "done", "cases": len(intg_cases)},
                )
            except Exception as e:
                degraded_flags.append("integration_generate_failed")
                logger.warning("[Orchestrator] 跨功能点用例生成失败（不阻断）: {}", str(e)[:150])
                await _emit_gen(
                    "跨功能点/集成用例生成失败",
                    {"key": "__integration__", "name": "跨功能点/集成", "status": "failed", "cases": 0},
                )

        # 3) 汇总 + 跨功能点去重（保持原有语义）
        cases = resolved_cases
        _dedup_keys: set[str] = set()
        _uniq: list[dict] = []
        for c in cases:
            k = c.get("title", "") or c.get("id", "")
            if k and k in _dedup_keys:
                continue
            if k:
                _dedup_keys.add(k)
            _uniq.append(c)
        cases = _uniq
        # v3.4（docs/15 §4.1 / P0-2）：合同校验**只标记不中断**。
        # 空集/畸形用例必须走完 review → done，由发布策略判 blocked / awaiting_human_review；
        # 在此抛异常会让前端连 done 事件都拿不到（"0 用例不得 completed"是**终态**要求，
        # 不是"让流水线崩掉"）。每通道内的校验同理已移除——否则内容问题会被误记成
        # "生成通道失败"，既丢用例又给出错误的降级原因。
        from app.utils.case_validation import validation_issues
        content_errors = validation_issues(cases)
        if content_errors:
            logger.error(
                "[Orchestrator] ⚠️ 生成用例不满足可执行合同（{} 项，转发布策略判定）: {}",
                len(content_errors), content_errors[:3],
            )

        logger.info(
            "[Orchestrator] ◀ 节点 generate（FP分片）完成 ▶ 下一节点 review: cases={}, fp_tagged={}, fp_groups={}",
            len(cases),
            sum(1 for c in cases if c.get("related_fp")),
            len(fp_list),
        )
        state["_fp_subtasks"] = fp_subtasks
        state["_fp_groups"] = checkpoint_groups

        # ── v3.2 + v2.0 增强: 一致性检查（vs 上次生成 + 语义相似度 + 类型方差）──
        # 对应 docs/03 §4.2 ConsistencyChecker
        # v3.1.3: ① ORM 收敛到 db/repositories；② 分层捕获——DB 失败（warning + 降级标记）
        # 与 embedding 失败（debug，非关键）区分，consistency_check 留 error 标记
        # 使下游能区分"无历史任务"与"查询失败"；③ semantic_sim 用 None 表示计算失败，
        # 与合法值 0.0（完全不相似）区分，消除二义性误判
        consistency_check = {}
        prev_result = None
        try:
            from app.db.repositories import load_latest_completed_task
            # v2.0 修复：仓储层只取已完成任务（排除 running/failed），避免 prev_result 为空误判
            prev_result = await load_latest_completed_task(req_id)
        except Exception as e:
            logger.warning(
                "[Orchestrator] 一致性检查历史基线查询失败（降级跳过对比）: {}", str(e)[:120]
            )
            degraded_flags.append("consistency_db_failed")
            consistency_check = {"error": "db_unavailable"}

        if isinstance(prev_result, dict) and prev_result:
            try:
                prev_count = prev_result.get("case_count", 0)
                prev_types = prev_result.get("type_dist", {})
                prev_titles = prev_result.get("case_titles", [])
                cur_count = len(cases)
                cur_titles = [c.get("title", "") for c in cases]
                cur_types: dict[str, int] = {}
                for c in cases:
                    t = c.get("type", "unknown")
                    cur_types[t] = cur_types.get(t, 0) + 1
                count_diff = abs(cur_count - prev_count) / max(prev_count, 1)

                # v2.0 增强 1: 类型分布方差（Jaccard 相似度）
                all_types = set(prev_types.keys()) | set(cur_types.keys())
                if all_types:
                    intersection = sum(
                        min(prev_types.get(t, 0), cur_types.get(t, 0)) for t in all_types
                    )
                    union = sum(
                        max(prev_types.get(t, 0), cur_types.get(t, 0)) for t in all_types
                    )
                    type_jaccard = intersection / union if union > 0 else 1.0
                else:
                    type_jaccard = 1.0

                # v2.0 增强 2: 语义相似度（基于标题 Embedding）
                # v3.1.3: None = 计算失败/跳过（不参与判定）；float = 合法相似度（0.0 也是有效值）
                semantic_sim: float | None = None
                if prev_titles and cur_titles:
                    try:
                        from app.db.milvus_store import vector_store
                        import asyncio as _aio
                        all_titles = prev_titles + cur_titles
                        embeddings = await _aio.to_thread(vector_store.embed_texts, all_titles)
                        if embeddings and len(embeddings) == len(all_titles):
                            prev_embs = embeddings[:len(prev_titles)]
                            cur_embs = embeddings[len(prev_titles):]
                            # 计算平均相似度：每个当前用例找最佳匹配
                            sims = []
                            for ce in cur_embs:
                                best = max(
                                    GenAgent.cosine_similarity(ce, pe)
                                    for pe in prev_embs
                                ) if prev_embs else 0.0
                                sims.append(best)
                            semantic_sim = sum(sims) / len(sims) if sims else 0.0
                    except Exception as sim_e:
                        logger.debug("[Orchestrator] 语义相似度计算跳过: {}", str(sim_e)[:100])

                # 综合一致性判定
                # v2.0 三维度: 用例数方差(<20%) + 类型分布(Jaccard>0.7) + 语义相似度(>0.7)
                count_inconsistent = count_diff > 0.2
                type_inconsistent = type_jaccard < 0.7
                semantic_inconsistent = (
                    semantic_sim is not None and semantic_sim < 0.7
                )  # None 表示计算失败，跳过该维度

                is_inconsistent = count_inconsistent or (
                    type_inconsistent and semantic_inconsistent
                )

                consistency_check = {
                    "prev_case_count": prev_count,
                    "curr_case_count": cur_count,
                    "count_diff_pct": round(count_diff * 100, 1),
                    "prev_type_dist": prev_types,
                    "curr_type_dist": cur_types,
                    "type_jaccard": round(type_jaccard, 3),
                    "semantic_similarity": round(semantic_sim, 3) if semantic_sim is not None else None,
                    "is_inconsistent": is_inconsistent,
                    # v2.0 三维度判定明细
                    "count_inconsistent": count_inconsistent,
                    "type_inconsistent": type_inconsistent,
                    "semantic_inconsistent": semantic_inconsistent,
                }
                if is_inconsistent:
                    logger.warning(
                        "[Orchestrator/GenAgent] ⚠️ 一致性告警: req_id={}, "
                        "prev_count={}, curr_count={}, diff={:.0f}%, "
                        "type_jaccard={:.3f}, semantic_sim={}",
                        req_id, prev_count, cur_count, count_diff * 100,
                        type_jaccard,
                        "n/a" if semantic_sim is None else f"{semantic_sim:.3f}",
                    )
            except Exception as e:
                logger.debug("[Orchestrator] 一致性检查计算跳过（非阻塞）: {}", str(e)[:120])
        # v2.0 修复：无论是否有历史任务，都保存当前生成的统计信息，
        # 供下次生成对比使用。保存逻辑移到 _review_node 之后由 API 层处理，
        # 这里仅负责读取历史数据做对比。

        return {
            "test_cases": cases,
            "consistency_check": consistency_check,
            "degraded_flags": degraded_flags,
            "content_errors": content_errors,
            # v3.1.3 细粒度进度透出
            "_fp_subtasks": state.get("_fp_subtasks", []) or [],
            "_fp_groups": state.get("_fp_groups", []) or [],
            "failed_subtasks": [s for s in fp_subtasks if s.get("status") == "failed"],
        }

    async def _review_node(self, state: PipelineState) -> dict:
        """节点 8：ReviewAgent 用例审核 → 知识库回流闭环。

        对应 docs/00 §3 阶段⑥ + 阶段⑧（回流存储）。
        审核完成后自动将高置信度用例回流到知识库（PG + Vector DB 双写）。

        Returns:
            dict: state 更新，含 review_result / reviewed_cases / kb_flowback
        """
        srr = state.get("srr", {})
        test_cases = state.get("test_cases", []) or []
        req_id = state.get("req_id", "")

        logger.info(
            "[Orchestrator] ▶ 节点 review: req_id={}, cases={}",
            req_id,
            len(test_cases),
        )

        from app.utils.case_validation import validation_issues
        from app.core.publication_policy import decide_publication
        functional_points = state.get("functional_points", []) or []
        _degraded = list(state.get("degraded_flags", []) or [])
        _degraded.extend((state.get("clarify_result") or {}).get("degraded_flags") or [])
        # v3.4（docs/15 §4.1 / P0-2）：合同问题只记录、不中断——处置权在发布策略。
        content_errors = list(dict.fromkeys([
            *(state.get("content_errors") or []),
            *validation_issues(test_cases),
        ]))
        if content_errors:
            logger.error(
                "[Orchestrator] ⚠️ 用例不满足可执行合同（{} 项，禁止自动发布）: {}",
                len(content_errors), content_errors[:3],
            )
        report, reviewed_cases = await review_agent.review(
            srr, test_cases, functional_points=functional_points
        )
        # 编排层观测到的合同问题并进报告：证据要能随 report 落库/展示，
        # 不能只存在于 decision 的 reasons 里（ReviewAgent 真实调用时已自带）。
        if content_errors and not report.get("content_errors"):
            report["content_errors"] = content_errors

        supplement_iteration = state.get("supplement_iteration", 0)
        if report.get("review_degraded"):
            _degraded.append("review_llm_failed")
        if report.get("should_supplement") and not report.get("review_degraded") and supplement_iteration < 1:
            hints = report.get("supplement_hints", []) or []
            logger.info(
                "[Orchestrator] Review 建议补生成，执行第{}次补生成: hints={}",
                supplement_iteration + 1,
                len(hints),
            )
            # 结构化 supplement_gaps：coverage_gap hint 的未覆盖要点按 FP 分组定向补生成
            fp_name_map = {fp.get("id", ""): fp.get("name", "") for fp in functional_points}
            grouped: dict[str, list[dict]] = {}
            for hint in hints:
                if not isinstance(hint, dict) or hint.get("case_type") != "coverage_gap":
                    continue
                for p in hint.get("uncovered_points", []) or []:
                    if not isinstance(p, dict) or not p.get("id"):
                        continue
                    grouped.setdefault(p.get("fp_id", ""), []).append(p)
            supplement_gaps = [
                {
                    "fp_id": fid,
                    "fp_name": fp_name_map.get(fid, "") if fid else "总需求要点",
                    "test_focus": "; ".join(f"[{p['id']}] {p.get('desc', '')}" for p in pts[:15]),
                    "reason": f"{len(pts)} 个 SRR 要点未覆盖，需定向补生成",
                }
                for fid, pts in grouped.items()
            ]
            # 非 coverage_gap 的旧式 hint（字符串/类型缺口）保持原有映射
            for hint in hints:
                if isinstance(hint, dict) and hint.get("case_type") == "coverage_gap":
                    continue
                name = str(hint)
                if name.strip():
                    supplement_gaps.append({"name": name[:80], "reason": f"review_supplement: {name[:80]}"})
            if not supplement_gaps:
                supplement_gaps = [{"name": "补充审核发现的覆盖缺口", "reason": "review_supplement"}]
            supplement_cases = await gen_agent.generate(
                srr,
                enable_multi_type=(state.get("channel_config", {}) or {}).get(
                    "enable_multi_type_generation", True
                ),
                impact_guidance=(state.get("impact_result", {}) or {}).get(
                    "generation_guidance", ""
                ) or "",
                functional_points=functional_points,
                coverage_gaps=supplement_gaps,
            )
            merged_cases = gen_agent.merge_regression_cases(reviewed_cases, supplement_cases)
            report, reviewed_cases = await review_agent.review(
                srr, merged_cases, functional_points=functional_points
            )
            supplement_iteration = 1
            # v3.1.3: 二次 review 后仍建议补生成 → 标记已达上限（补生成最多 1 次），
            # 避免前端把 should_supplement=True 误解为"还会再补"
            if report.get("should_supplement"):
                report["supplement_exhausted"] = True

        # Decide BEFORE any write, then re-evaluate inside persist_to_kb.
        if report.get("review_degraded"):
            _degraded.append("review_llm_failed")
        _degraded = list(dict.fromkeys(_degraded))
        publication_context = {
            "gate_result": state.get("gate_result"),
            "forced_pass": bool(state.get("forced_pass")),
            "degraded_flags": _degraded,
            "failed_subtasks": state.get("failed_subtasks") or [],
        }
        publication = decide_publication(
            report, reviewed_cases, **publication_context,
            content_errors=content_errors,
        )
        report["publication"] = publication
        kb_result = {}
        if publication["decision"] == "blocked":
            # v3.4（docs/15 §4.1）：fail-closed 的正确形态是"不写 + 终态可见"，
            # 不是抛异常。抛异常会让 review → done 消失，failed_subtasks/
            # completion_status 全部无法上报（前端只看到"流水线异常"）。
            logger.error(
                "[Orchestrator] ⛔ 发布决策=blocked，跳过知识库回流"
                "（PG 与 Milvus 均不写入）: {}",
                publication.get("reasons"),
            )
            if "publication_blocked" not in _degraded:
                _degraded.append("publication_blocked")
            kb_result = {
                "skipped": True,
                "skip_reason": "publication_blocked",
                "reasons": publication.get("reasons", []),
            }
        elif reviewed_cases:
            try:
                kb_result = await review_agent.persist_to_kb(
                    srr, reviewed_cases, report, publication_context=publication_context,
                )
                logger.info(
                    "[Orchestrator] KB 回流完成: auto_approved={}, pending={}, rejected={}, vector_write_failed={}",
                    kb_result.get("auto_approved", 0),
                    kb_result.get("pending_review", 0),
                    kb_result.get("rejected", 0),
                    kb_result.get("vector_write_failed", 0),
                )
                # P1-4: 向量双写失败时在 review 报告中显式标记，前端可见
                if kb_result.get("vector_write_failed"):
                    report["vector_write_failed"] = kb_result["vector_write_failed"]
                    report.setdefault("warnings", []).append(
                        f"{kb_result['vector_write_failed']} 条用例的 Milvus 向量写入失败，"
                        "PG 已入库但回归检索基线缺失；请检查 Milvus 服务后重新批量审核补写。"
                    )
            except Exception as e:
                logger.error("[Orchestrator] KB 回流失败（不阻断）: {}", str(e)[:200])
                kb_result = {"error": str(e)[:200]}

        logger.info(
            "[Orchestrator] ◀ 节点 review 完成 ▶ END: "
            "total={}, passed={}, blocked={}, avg_conf={:.3f}, should_supplement={}, "
            "kb_auto_approved={}, kb_pending={}",
            report.get("total_cases"),
            report.get("passed_cases"),
            report.get("blocked_cases"),
            report.get("avg_confidence", 0.0),
            report.get("should_supplement"),
            kb_result.get("auto_approved", 0),
            kb_result.get("pending_review", 0),
        )

        # v3.3（修 N8）：审核降级标记透传——LLM 审核失败时 ReviewAgent 返回
        # 「全部通过、0 拦截」的规则预检结果，与"审核真的通过"在数据上无法区分。
        # 必须标记，否则"审核能力静默缺失"会被当成"质量很好"。
        if report.get("review_degraded") and "review_llm_failed" not in _degraded:
            _degraded.append("review_llm_failed")
            logger.error(
                "[Orchestrator] ⚠️ 本次审核为**降级结果**（LLM 审核失败，仅规则预检）: "
                "passed={} 不代表真实审核通过。degraded_flags={}",
                report.get("passed_cases"), _degraded,
            )

        return {
            "review_result": report,
            "reviewed_cases": reviewed_cases,
            "kb_flowback": kb_result,
            "supplement_iteration": supplement_iteration,
            "degraded_flags": _degraded,
            "publication": publication,
            "failed_subtasks": state.get("failed_subtasks") or [],
        }

    # ========================================================================
    # 条件路由
    # ========================================================================

    def _route_after_channel(self, state: PipelineState) -> str:
        """条件路由：select_channel 后选择 FULL→clarify / FAST→gate。

        v3.x 新增：当 state.channel_config.enable_clarify=False（confirmed 需求跳过），
        即使 channel=="FULL" 也直接走 gate，避免 confirmed 需求在生成阶段
        无谓进入 clarify 回环（无人回答、易卡死于 LLM 配额/网络）。

        Args:
            state: 当前流水线状态

        Returns:
            str: "clarify" 或 "gate"
        """
        channel = state.get("channel", "FAST")
        cfg = state.get("channel_config", {}) or {}
        enable_clarify = cfg.get("enable_clarify", False) if isinstance(cfg, dict) else getattr(cfg, "enable_clarify", False)
        if channel == "FULL" and enable_clarify:
            return "clarify"
        return "gate"

    def _route_after_gate(self, state: PipelineState) -> str:
        """条件路由：gate 后判断走 impact / 回 clarify。

        路由规则（doc-01 §5.3 + doc-03 §6.7）：
            - passed=True 或 forced_pass=True → impact（继续生成）
            - passed=False 且 iteration < MAX → clarify（回环重试）
            - passed=False 且 iteration >= MAX → impact（强制通过，由 _gate_node 标记）

        Args:
            state: 当前流水线状态

        Returns:
            str: "impact" 或 "clarify"
        """
        gate_result = state.get("gate_result", {}) or {}
        passed = gate_result.get("passed", False)
        forced = gate_result.get("forced_pass", False)
        iteration = state.get("clarify_iteration", 0)

        if passed or forced:
            return "impact"

        # v3.x 防御：clarify 被禁用（已确认需求 / FAST）时绝不回环
        _cfg = state.get("channel_config", {}) or {}
        _enable_clarify = (
            _cfg.get("enable_clarify", False) if isinstance(_cfg, dict)
            else getattr(_cfg, "enable_clarify", False)
        )
        if not _enable_clarify or state.get("channel", "FAST") == "FAST":
            logger.warning(
                "[Orchestrator] QualityGate 未通过但 clarify 已禁用，兜底强制进入 impact"
            )
            return "impact"

        if ClarifyLoopGuard.should_retry(iteration, passed):
            logger.info(
                "[Orchestrator] ⟲ QualityGate 未通过，回环到 clarify (第{}→{}轮)",
                iteration,
                iteration + 1,
            )
            return "clarify"

        # 兜底：强制走 impact（理论上 _gate_node 已标记 forced_pass，此处为防御性处理）
        logger.warning(
            "[Orchestrator] QualityGate 未通过但已达最大轮次，兜底强制进入 impact"
        )
        return "impact"

    # ========================================================================
    # 辅助方法
    # ========================================================================

    async def _fetch_original_cases(self, srr: dict) -> list[dict]:
        """检索优化需求的原始测试用例（供 ImpactAgent 做操作规划）。

        从 optimization_context.original_requirement.id 查询关联用例。
        失败时返回空列表（不阻断主流程）。

        Args:
            srr: SRR JSON

        Returns:
            list[dict]: 原始用例列表，每项含 {id, title, type, case_json}
        """
        opt_context = srr.get("optimization_context", {}) or {}
        original_req = opt_context.get("original_requirement", {}) or {}
        original_req_id = original_req.get("id", "")

        if not original_req_id:
            return []

        try:
            # v3.1.3: ORM 查询收敛到 db/repositories
            from app.db.repositories import load_cases_by_req

            return await load_cases_by_req(original_req_id)
        except Exception as e:
            logger.debug(
                "[Orchestrator] 检索原始用例失败（不阻断）: {}",
                str(e)[:120],
            )
            return []

    def _apply_clarify_answers(self, srr: dict, answers: dict) -> dict:
        """将交互式澄清答案应用到 SRR。

        ClarifyAgent 未提供 refine 方法（D2 决策），由编排器实现。
        支持两种答案格式：
            - {field_path: value}：field_path 含点号则按嵌套路径设置
            - {top_key: value}：直接设置顶层字段

        Args:
            srr: SRR JSON（不会被修改，内部深拷贝）
            answers: 澄清答案字典

        Returns:
            dict: 更新后的 SRR（深拷贝）
        """
        srr_copy = copy.deepcopy(srr)

        if not answers or not isinstance(answers, dict):
            return srr_copy

        for key, value in answers.items():
            if "." in key:
                self._set_nested_value(srr_copy, key, value)
            else:
                srr_copy[key] = value

        return srr_copy

    def _set_nested_value(self, data: dict, path: str, value: Any) -> None:
        """设置嵌套字段值（路径格式 a.b.c）。

        Args:
            data: 目标字典（会被原地修改）
            path: 字段路径，如 "requirement.summary"
            value: 要设置的值

        Note:
            若路径中间节点不存在，自动创建空字典；若中间节点不是字典，
            则替换为空字典以保证澄清答案能正确写入（避免 LLM 输出不一致导致冲突）。
        """
        keys = path.split(".")
        current = data
        for k in keys[:-1]:
            if k not in current or not isinstance(current[k], dict):
                current[k] = {}
            current = current[k]
        current[keys[-1]] = value

    # ========================================================================
    # 主入口
    # ========================================================================

    async def run_pipeline(
        self,
        raw_requirement: str,
        srr_json: dict | None = None,
        product_id: str = "",
        product_name: str = "",
        clarify_answers: dict | None = None,
        existing_req_id: str = "",
        task_id: str = "",
        resume: bool = False,
        resume_checkpoint: dict | None = None,
    ) -> AsyncGenerator[dict, None]:
        """运行完整流水线，流式 yield SSE 事件。

        P3-6: 入口设置任务环境上下文（contextvars），该任务链内所有
        LLM 调用自动携带 task_id/req_id 落 llm_call_logs，打通 trace 树。

        使用 LangGraph compiled.astream(stream_mode="updates") 流式执行，
        每个节点完成后 yield 一个 SSE 事件，最终 yield done 事件含完整结果。

        Args:
            raw_requirement: 原始需求文本（仅用于启动日志长度统计；srr_json 为空时 _parse_node 直接报错，无回退解析）
            srr_json: 需求分析/澄清后形成的最终 SRR（优先使用）。
                传入时 _parse_node 跳过 LLM 重新解析，直接使用此 SRR，
                避免通道选择错误（问题：REQ-1784269856180-7617 只生成1个用例）。
            product_id: 产品 ID（可选）
            product_name: 产品名称（可选）
            clarify_answers: 交互式澄清答案（可选，首轮不传；D2 决策 API 驱动）
            existing_req_id: 已有需求 ID（可选，从 DB 取的需求）。传入时复用此 ID，
                避免重新解析生成新 req_id 导致 test_cases 外键违反。

        Yields:
            dict: SSE 事件，格式：
                {"stage": "parse|select_channel|clarify|gate|impact|test_design|generate|review|done|error",
                 "message": "...",
                 "progress": 0-100,
                 ...附加字段}

        Note:
            - 节点异常被捕获，yield error 事件后停止
            - Clarify 回环（最多 3 轮）会多次 yield clarify/gate 事件
            - 最终 done 事件含 reviewed_cases / review_report / 各节点结果
        """
        from app.core.task_context import set_task_context
        # 任务环境上下文：整个 generator 生命周期内生效（asyncio 任务链继承），
        # 进程内常驻无害——同名任务再次运行会被下一次 set 覆盖。
        set_task_context(task_id=task_id or "", req_id=existing_req_id or "")
        initial_state: PipelineState = {
            "raw_requirement": raw_requirement,
            "srr_json": srr_json,
            "product_id": product_id,
            "product_name": product_name,
            "clarify_answers": clarify_answers or {},
            "clarify_iteration": 0,
            "start_time": time.time(),
            "existing_req_id": existing_req_id,
            "task_id": task_id,
            "resume": resume,
            "resume_checkpoint": resume_checkpoint,
        }

        # 节点名 → SSE stage 映射
        node_to_stage = {
            "parse": "parse",
            "select_channel": "select_channel",
            "clarify": "clarify",
            "gate": "gate",
            "impact": "impact",
            "test_design": "test_design",
            "generate": "generate",
            "review": "review",
        }

        # 节点进度消息
        node_messages = {
            "parse": "ReqAgent 解析需求中...",
            "select_channel": "通道选择中...",
            "clarify": "ClarifyAgent 需求澄清中...",
            "gate": "QualityGate 质量门禁检查中...",
            "impact": "ImpactAnalysisAgent 影响分析中...",
            "test_design": "TestDesignAgent 测试设计中...",
            "generate": "GenAgent 生成测试用例中...",
            "review": "ReviewAgent 审核用例中...",
        }

        final_state: dict = {}

        logger.info(
            "[Orchestrator] ╔══ 流水线启动: raw_len={}, product_id={} ══╗",
            len(raw_requirement),
            product_id,
        )

        try:
            # v3.1.3: 追踪最后完成的节点——final_state 为本地 dict（非 LangGraph state 通道），
            # 写入 _last_node 不会被 StateGraph 过滤；异常写 DLQ 时用于定位失败节点
            # v3.1.3 事件队列：把 LangGraph 的节点完成事件与 _gen_node 内部的细粒度
            # FP 进度事件合并到一个队列流式透给前端（按序 interleave）。
            queue: asyncio.Queue = asyncio.Queue()
            queue_token = self._gen_event_queue.set(queue)

            async def _producer() -> None:
                try:
                    async for chunk in self.graph.astream(initial_state, stream_mode="updates"):
                        for node_name, update in chunk.items():
                            if node_name == "__end__":
                                continue
                            if not isinstance(update, dict):
                                continue

                            # 合并到 final_state
                            final_state.update(update)
                            final_state["_last_node"] = node_name

                            stage = node_to_stage.get(node_name, node_name)
                            message = node_messages.get(node_name, f"{node_name} 完成")

                            # 构造 SSE 事件
                            event = make_sse_event(stage, message)

                            # 附加节点关键数据（便于前端展示）
                            if node_name == "parse":
                                event["req_id"] = final_state.get("req_id", "")
                            elif node_name == "select_channel":
                                event["channel"] = final_state.get("channel", "")
                            elif node_name == "clarify":
                                cr = final_state.get("clarify_result", {}) or {}
                                event["clarify_status"] = cr.get("status", "")
                                event["questions_count"] = len(cr.get("questions", []) or [])
                                event["iteration"] = final_state.get("clarify_iteration", 0)
                            elif node_name == "gate":
                                gr = final_state.get("gate_result", {}) or {}
                                event["gate_passed"] = gr.get("passed", False)
                                event["forced_pass"] = gr.get("forced_pass", False)
                                event["gate_result"] = gr
                                event["blockers_count"] = len(gr.get("blockers", []) or [])
                                event["warnings"] = gr.get("warnings", []) or []
                                event["manual_action_required"] = bool(gr.get("forced_pass"))
                            elif node_name == "impact":
                                ir = final_state.get("impact_result", {}) or {}
                                event["is_optimization"] = ir.get("is_optimization", False)
                            elif node_name == "test_design":
                                td = final_state.get("test_design_blueprint", {}) or {}
                                event["regression_reuse_summary"] = final_state.get("regression_reuse_summary", {})
                                event["techniques"] = [
                                    k for k, v in td.get("techniques_applicable", {}).items() if v
                                ]
                                event["guidance_len"] = len(td.get("guidance_text", "") or "")
                            elif node_name == "generate":
                                event["cases_count"] = len(final_state.get("test_cases", []) or [])
                                event["subtasks"] = final_state.get("_fp_subtasks", []) or []
                            elif node_name == "review":
                                rr = final_state.get("review_result", {}) or {}
                                event["review_passed"] = rr.get("passed_cases", 0)
                                event["review_blocked"] = rr.get("blocked_cases", 0)
                                event["should_supplement"] = rr.get("should_supplement", False)
                                event["supplement_iteration"] = final_state.get("supplement_iteration", 0)
                                event["avg_confidence"] = rr.get("avg_confidence", 0.0)
                                event["point_coverage"] = rr.get("point_coverage", {})

                            await queue.put(event)
                except Exception as e:
                    # v3.1.3 修复：节点异常不能静默吞掉。此前 astream 抛异常时
                    # producer 只在 finally 放 None 哨兵，消费循环误判为正常
                    # 结束并继续走 done 分支——用户看到「流水线完成」但 0 用例，
                    # 无 error 事件、DLQ 也不记录。这里把异常对象传给消费循环，
                    # 由外层 except 统一处理（yield error + 写 DLQ）。
                    await queue.put(e)
                finally:
                    await queue.put(None)

            producer = asyncio.create_task(_producer())
            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    if isinstance(event, BaseException):
                        raise event
                    yield event
            finally:
                producer.cancel()
                try:
                    await producer
                except asyncio.CancelledError:
                    pass
                finally:
                    self._gen_event_queue.reset(queue_token)

            # ── v3.2: FP 覆盖率矩阵 ──
            # v3.1.3: 优先读 state（test_design 已统一加载），避免流水线内第 3 次 FP 查询
            fp_coverage = {}
            try:
                reviewed_cases = final_state.get("reviewed_cases", []) or []
                req_id_final = final_state.get("req_id", "")
                state_fps = final_state.get("functional_points", []) or []
                if state_fps:
                    fp_list = [(fp.get("id", ""), fp.get("name", "")) for fp in state_fps]
                elif req_id_final:
                    # v3.1.3: ORM 查询收敛到 db/repositories
                    from app.db.repositories import load_fp_id_names

                    fp_list = await load_fp_id_names(req_id_final)
                else:
                    fp_list = []
                if fp_list:
                    # v3.5: name/id 双匹配——LLM 可能将 related_fp 填为 FP.id 或 FP.name
                    # （历史 prompt 指示不一致），统一归一化为 FP.id 再统计，
                    # 避免按 name 标注的用例被误判为未覆盖
                    name_to_id = {fname: fid for fid, fname in fp_list}
                    total_fps = len(fp_list)
                    covered_ids = set()
                    for c in reviewed_cases:
                        rf = c.get("related_fp", "")
                        if not rf:
                            continue
                        resolved_id = name_to_id.get(rf, rf)
                        if resolved_id in {fid for fid, _ in fp_list}:
                            covered_ids.add(resolved_id)
                    uncovered = [
                        {"id": fid, "name": fname}
                        for fid, fname in fp_list if fid not in covered_ids
                    ]
                    fp_coverage = {
                        "total_fps": total_fps,
                        "covered_fps": len(covered_ids),
                        "coverage_rate": round(len(covered_ids) / max(total_fps, 1), 2),
                        "uncovered": uncovered,
                    }
            except Exception as e:
                logger.debug("[Orchestrator] FP 覆盖率计算跳过: {}", str(e)[:120])

            # 流水线完成
            duration = time.time() - final_state.get("start_time", time.time())
            done_message = "流水线完成"
            gate_result = final_state.get("gate_result", {}) or {}
            manual_action_required = bool(gate_result.get("forced_pass"))
            if manual_action_required:
                done_message = "流水线完成，门禁未完全通过，请人工修改需求后再确认"
            done_event = make_sse_event(
                "done",
                done_message,
                req_id=final_state.get("req_id", ""),
                channel=final_state.get("channel", ""),
                srr=final_state.get("srr"),
                cases=final_state.get("reviewed_cases", []),
                review_report=final_state.get("review_result", {}),
                clarify_result=final_state.get("clarify_result", {}),
                gate_result=final_state.get("gate_result", {}),
                impact_result=final_state.get("impact_result", {}),
                kb_flowback=final_state.get("kb_flowback", {}),
                fp_coverage=fp_coverage,
                point_coverage=(final_state.get("review_result", {}) or {}).get("point_coverage", {}),
                consistency_check=final_state.get("consistency_check", {}),
                # v3.1.3: 降级标记透传（fp_load_failed / consistency_db_failed 等），
                # 前端可据此提示"功能点定向生成/一致性对比不可用"
                degraded_flags=final_state.get("degraded_flags", []) or [],
                test_design=final_state.get("test_design_blueprint", {}),
                regression_reuse_summary=final_state.get("regression_reuse_summary", {}),
                manual_action_required=manual_action_required,
                supplement_iteration=final_state.get("supplement_iteration", 0),
                duration_ms=int(duration * 1000),
                publication=final_state.get("publication", {}),
                failed_subtasks=final_state.get("failed_subtasks", []),
            )
            from app.core.publication_policy import quality_outcome
            done_event.update(quality_outcome(done_event))
            if done_event["manual_action_required"]:
                done_event["message"] = "生成结果待人工审核，未获自动核准"
            logger.info(
                "[Orchestrator] ╚══ 流水线完成: req_id={}, duration={:.2f}s ══╝",
                final_state.get("req_id", ""),
                duration,
            )
            yield done_event

        except Exception as e:
            logger.error("[Orchestrator] ╳ 流水线异常: {}", str(e)[:200])
            import traceback

            logger.debug("[Orchestrator] 异常堆栈:\n{}", traceback.format_exc())

            # v3.1.3: PipelineState.error 字段写入方（此前为死声明）——
            # final_state 为本地 dict，供 error 事件与 DLQ 统一读取
            final_state["error"] = str(e)[:200]

            # L4 数据层防护：主流水线异常写入 DLQ，支持事后审计与手动重试
            # 对应 docs/00 §2 L4 防护承诺，闭合 orchestrator 主流水线死信兜底
            try:
                from app.core.dlq import dlq
                dlq.push(
                    task_type="pipeline",
                    payload={
                        "req_id": final_state.get("req_id", ""),
                        "channel": final_state.get("channel", ""),
                        "stage": final_state.get("_last_node", "unknown"),
                    },
                    error=str(e)[:2000],
                    max_retries=3,
                )
            except Exception as dlq_e:
                logger.warning(
                    "[Orchestrator] DLQ 写入失败（不阻断）: {}", str(dlq_e)[:120]
                )

            yield make_sse_event(
                "error",
                f"流水线异常: {str(e)[:200]}",
                req_id=final_state.get("req_id", ""),
            )


# ============================================================================
# 全局单例
# ============================================================================

orchestrator = Orchestrator()
