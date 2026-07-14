"""LangGraph DAG 编排器。

将 5 个 Agent 串联为 DAG 工作流，实现从原始需求到测试用例的端到端生成。
使用 LangGraph StateGraph 声明流程结构，通过 astream 流式输出 SSE 事件。

设计依据：
- docs/00-architecture-overview.md §2 编排层
- docs/00-architecture-overview.md §3 核心数据流（8 阶段）
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
               retrieve ←──────┘
                  ↓
                generate
                  ↓
                 review
                  ↓
                 END

关键决策（plan §五 D1-D10）：
    - D2：Clarify 流程采用 API 驱动，不使用 LangGraph interrupt
    - D3：MatchAgent 检索逻辑合并到 orchestrator 的 retrieve_node
    - D4：SSE 用 sse-starlette（本模块只产事件 dict，序列化由 API 层负责）
"""

from __future__ import annotations

import copy
import time
import asyncio
from typing import Any, AsyncGenerator, TypedDict

from langgraph.graph import END, StateGraph

from app.agents.clarify_agent import clarify_agent
from app.agents.gen_agent import gen_agent
from app.agents.impact_agent import impact_agent
from app.agents.req_agent import req_agent
from app.agents.review_agent import review_agent
from app.config import settings
from app.core.channel_selector import select_channel
from app.core.quality_gate import quality_gate
from app.db.milvus_store import vector_store
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
        few_shot_context: retrieve_node 的 KB 检索结果
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

    # 节点输出
    srr: dict
    req_id: str
    channel: str
    channel_config: dict
    clarify_result: dict
    gate_result: dict
    impact_result: dict
    few_shot_context: dict
    test_cases: list
    review_result: dict
    reviewed_cases: list
    kb_flowback: dict

    # 控制流
    clarify_iteration: int
    forced_pass: bool
    error: str

    # 元信息
    start_time: float


# ============================================================================
# SSE 事件构造（plan §T4 第 389-395 行，doc-00 §9.2）
# ============================================================================

# stage → progress 百分比映射（doc-00 §9.2）
STAGE_PROGRESS: dict[str, int] = {
    "parse": 20,
    "select_channel": 25,
    "clarify": 30,
    "gate": 40,
    "impact": 50,
    "retrieve": 55,
    "generate": 75,
    "review": 90,
    "done": 100,
    "error": 100,
}


def make_sse_event(stage: str, message: str, **extra: Any) -> dict:
    """构造 SSE 事件字典。

    Args:
        stage: 阶段名称（parse/select_channel/clarify/gate/impact/retrieve/generate/review/done/error）
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

    MAX_ITERATIONS: int = settings.MAX_CLARIFY_ITERATIONS

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
        return iteration < ClarifyLoopGuard.MAX_ITERATIONS

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
        return iteration >= ClarifyLoopGuard.MAX_ITERATIONS


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

    # ========================================================================
    # 图构建
    # ========================================================================

    def _build_graph(self):
        """构建 LangGraph 状态图。

        节点：parse / select_channel / clarify / gate / impact / retrieve / generate / review
        条件边：
            - select_channel → clarify (FULL) / gate (FAST)
            - gate → impact (通过或强制) / clarify (回环重试)

        Returns:
            CompiledGraph: 已编译的可执行图
        """
        graph: StateGraph = StateGraph(PipelineState)

        # 注册节点
        graph.add_node("parse", self._parse_node)
        graph.add_node("select_channel", self._select_channel_node)
        graph.add_node("clarify", self._clarify_node)
        graph.add_node("gate", self._gate_node)
        graph.add_node("impact", self._impact_node)
        graph.add_node("retrieve", self._retrieve_node)
        graph.add_node("generate", self._gen_node)
        graph.add_node("review", self._review_node)

        # 入口
        graph.set_entry_point("parse")

        # 固定边
        graph.add_edge("parse", "select_channel")
        graph.add_edge("clarify", "gate")
        graph.add_edge("impact", "retrieve")
        graph.add_edge("retrieve", "generate")
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
        """节点 1：ReqAgent 解析原始需求为 SRR。

        对应 docs/00 §3 阶段①。

        Returns:
            dict: state 更新，含 srr / req_id / clarify_iteration / start_time
        """
        raw = state.get("raw_requirement", "")
        product_id = state.get("product_id", "")
        product_name = state.get("product_name", "")

        logger.info(
            "[Orchestrator] ▶ 节点 parse: raw_len={}, product_id={}",
            len(raw),
            product_id,
        )

        srr = await req_agent.parse(raw, product_id, product_name)
        req_id = srr.get("requirement", {}).get("id", "")

        # 如果传入了已有 req_id（从 DB 取的需求），覆盖新解析的 req_id
        # 避免重新解析生成新 req_id 导致 test_cases 外键违反
        existing_req_id = state.get("existing_req_id", "")
        if existing_req_id:
            logger.info(
                "[Orchestrator] 使用已有 req_id 覆盖新解析 req_id: {} → {}",
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

        Returns:
            dict: state 更新，含 channel / channel_config
        """
        srr = state.get("srr", {})
        req_id = state.get("req_id", "")

        logger.info("[Orchestrator] ▶ 节点 select_channel: req_id={}", req_id)

        config = select_channel(srr)
        channel = config.channel
        next_node = "clarify" if config.is_full else "gate"

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
        # 判断是否强制通过
        forced = ClarifyLoopGuard.should_force_pass(iteration, result.passed)
        # FAST 通道跳过 clarify，gate 未通过时直接强制通过，不回环
        if channel == "FAST" and not result.passed and not forced:
            forced = True
            gate_dict["forced_pass"] = True
            warnings = gate_dict.get("warnings", []) or []
            warnings.append("FAST 通道跳过澄清，QualityGate 未通过但强制放行")
            gate_dict["warnings"] = warnings
            logger.warning(
                "[Orchestrator] FAST 通道 gate 未通过，强制放行（不回环 clarify）"
            )
        elif forced:
            gate_dict["forced_pass"] = True
            warnings = gate_dict.get("warnings", []) or []
            warnings.append(
                f"达到最大澄清轮次（{ClarifyLoopGuard.MAX_ITERATIONS}），强制通过"
            )
            gate_dict["warnings"] = warnings
            logger.warning(
                "[Orchestrator] QualityGate 强制通过（达到最大轮次 {}）",
                ClarifyLoopGuard.MAX_ITERATIONS,
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
            "[Orchestrator] ◀ 节点 impact 完成 ▶ 下一节点 retrieve: "
            "is_optimization={}, guidance_len={}, original_cases={}",
            is_opt,
            guidance_len,
            len(original_cases),
        )

        return {"impact_result": result}

    async def _retrieve_node(self, state: PipelineState) -> dict:
        """节点 6：KB 检索（MatchAgent 合并到编排器，D3 决策）。

        执行综合 KB 检索（相似需求 + 相似用例 + 测试模式），结果存入 state。

        Note:
            ClarifyAgent 和 GenAgent 内部已各自检索 KB，此节点主要起
            "预热缓存 + 日志 + SSE stage" 作用，不破坏 Agent 封装。
            retrieve_node 的检索结果存入 state.few_shot_context 供日志和调试用，
            GenAgent 仍使用自身的检索逻辑。

        Returns:
            dict: state 更新，含 few_shot_context
        """
        srr = state.get("srr", {})
        req_id = state.get("req_id", "")
        summary = srr.get("requirement", {}).get("summary", "")
        product_id = srr.get("requirement", {}).get("product_id", "")
        modules = srr.get("requirement", {}).get("modules", []) or []

        logger.info("[Orchestrator] ▶ 节点 retrieve: req_id={}", req_id)

        few_shot_context: dict[str, list] = {
            "similar_reqs": [],
            "similar_cases": [],
            "patterns": [],
        }

        # 构建 KB 查询日志
        kb_logs = []

        # 相似需求
        req_error = None
        try:
            few_shot_context["similar_reqs"] = await asyncio.to_thread(
                vector_store.search_similar_requirements,
                query=summary, product_id=product_id, modules=modules, top_k=3,
            )
        except Exception as e:
            req_error = str(e)[:150]
            logger.debug(
                "[Orchestrator] retrieve 相似需求失败（不阻断）: {}",
                str(e)[:120],
            )
        kb_logs.append({
            "type": "similar_requirements",
            "label": "需求检索",
            "query": f"查找与当前需求功能相似的同类历史需求，为 Few-Shot 生成提供参考模板",
            "query_detail": summary,
            "result_count": len(few_shot_context["similar_reqs"]),
            "results": [
                {
                    "id": r.get("id", ""),
                    "title": r.get("title", ""),
                    "summary": r.get("summary", "")[:200],
                    "similarity": r.get("distance", r.get("similarity", 0)),
                }
                for r in few_shot_context["similar_reqs"][:5]
            ],
            "error": req_error,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

        # 相似用例（v2.2: 按 product_id 过滤）
        case_error = None
        try:
            few_shot_context["similar_cases"] = await asyncio.to_thread(
                vector_store.search_similar_cases,
                query=summary, top_k=3, product_id=product_id or None,
            )
        except Exception as e:
            case_error = str(e)[:150]
            logger.debug(
                "[Orchestrator] retrieve 相似用例失败（不阻断）: {}",
                str(e)[:120],
            )
        kb_logs.append({
            "type": "similar_cases",
            "label": "用例检索",
            "query": "检索历史上为同类需求生成的测试用例，为当前用例生成提供 Few-Shot 参考",
            "query_detail": summary,
            "result_count": len(few_shot_context["similar_cases"]),
            "results": [
                {
                    "id": c.get("id", ""),
                    "title": c.get("title", ""),
                    "type": c.get("type", ""),
                    "similarity": c.get("distance", c.get("similarity", 0)),
                }
                for c in few_shot_context["similar_cases"][:5]
            ],
            "error": case_error,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

        # 测试模式（v2.2: 按 product_id 过滤）
        pattern_error = None
        try:
            few_shot_context["patterns"] = await asyncio.to_thread(
                vector_store.search_patterns,
                query=summary, top_k=3, product_id=product_id or None,
            )
        except Exception as e:
            pattern_error = str(e)[:150]
            logger.debug(
                "[Orchestrator] retrieve 测试模式失败（不阻断）: {}",
                str(e)[:120],
            )
        kb_logs.append({
            "type": "patterns",
            "label": "模式检索",
            "query": "检索适合当前需求类型的测试模式（CRUD/状态机/认证/...)，为用例生成提供结构模板",
            "query_detail": summary,
            "result_count": len(few_shot_context["patterns"]),
            "results": [
                {
                    "id": p.get("id", ""),
                    "name": p.get("name", ""),
                    "category": p.get("category", ""),
                    "similarity": p.get("distance", p.get("similarity", 0)),
                }
                for p in few_shot_context["patterns"][:5]
            ],
            "error": pattern_error,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

        # 持久化 KB 日志到需求记录的 srr_json 中
        try:
            from sqlalchemy import select
            from app.db.database import async_session
            from app.db.models import Requirement

            async with async_session() as db_session:
                stmt = select(Requirement).where(Requirement.id == req_id)
                result = await db_session.execute(stmt)
                record = result.scalar_one_or_none()
                if record and record.srr_json is not None:
                    srr_data = dict(record.srr_json)
                    existing_logs = srr_data.get("_kb_query_logs", [])
                    if isinstance(existing_logs, list):
                        existing_logs.extend(kb_logs)
                        srr_data["_kb_query_logs"] = existing_logs
                    else:
                        srr_data["_kb_query_logs"] = kb_logs
                    record.srr_json = srr_data
                    await db_session.commit()
                    logger.debug(
                        "[Orchestrator] KB 日志已持久化: req_id={}, logs={}",
                        req_id, len(kb_logs),
                    )
        except Exception as e:
            logger.debug("[Orchestrator] KB 日志持久化失败（不阻断）: {}", str(e)[:120])

        logger.info(
            "[Orchestrator] ◀ 节点 retrieve 完成 ▶ 下一节点 generate: "
            "similar_reqs={}, similar_cases={}, patterns={}",
            len(few_shot_context["similar_reqs"]),
            len(few_shot_context["similar_cases"]),
            len(few_shot_context["patterns"]),
        )

        return {"few_shot_context": few_shot_context}

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

        enable_multi_type = channel_config.get("enable_multi_type_generation", False)
        impact_guidance = impact_result.get("generation_guidance", "") or ""

        # v1.4：从 DB 加载已拆分的功能点，用于 FP 定向生成
        functional_points: list[dict] = []
        if req_id:
            try:
                from app.db.database import async_session
                from app.db.models import FunctionalPoint
                from sqlalchemy import select
                async with async_session() as fp_session:
                    fp_result = await fp_session.execute(
                        select(FunctionalPoint).where(
                            FunctionalPoint.requirement_id == req_id
                        )
                    )
                    fp_records = fp_result.scalars().all()
                    functional_points = [
                        {
                            "id": fp.id,
                            "name": fp.name,
                            "fp_type": fp.fp_type,
                            "srr_json": fp.srr_json or {},
                            "test_hints": fp.test_hints or [],
                            "atomicity_check": fp.atomicity_check or {},
                        }
                        for fp in fp_records
                    ]
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

        logger.info(
            "[Orchestrator] ▶ 节点 generate: req_id={}, multi_type={}, has_guidance={}, fp_count={}",
            req_id,
            enable_multi_type,
            bool(impact_guidance),
            len(functional_points),
        )

        cases = await gen_agent.generate(
            srr,
            enable_multi_type=enable_multi_type,
            impact_guidance=impact_guidance,
            functional_points=functional_points or None,
        )

        logger.info(
            "[Orchestrator] ◀ 节点 generate 完成 ▶ 下一节点 review: cases={}, fp_tagged={}",
            len(cases),
            sum(1 for c in cases if c.get("related_fp")),
        )

        return {"test_cases": cases}

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

        report, reviewed_cases = await review_agent.review(srr, test_cases)

        # 知识库回流闭环（docs/02 §4.1 + docs/00 §3 阶段⑧）
        kb_result = {}
        if reviewed_cases:
            try:
                kb_result = await review_agent.persist_to_kb(srr, reviewed_cases, report)
                logger.info(
                    "[Orchestrator] KB 回流完成: auto_approved={}, pending={}, rejected={}",
                    kb_result.get("auto_approved", 0),
                    kb_result.get("pending_review", 0),
                    kb_result.get("rejected", 0),
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

        return {
            "review_result": report,
            "reviewed_cases": reviewed_cases,
            "kb_flowback": kb_result,
        }

    # ========================================================================
    # 条件路由
    # ========================================================================

    def _route_after_channel(self, state: PipelineState) -> str:
        """条件路由：select_channel 后选择 FULL→clarify / FAST→gate。

        Args:
            state: 当前流水线状态

        Returns:
            str: "clarify" 或 "gate"
        """
        channel = state.get("channel", "FAST")
        return "clarify" if channel == "FULL" else "gate"

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
            from sqlalchemy import select

            from app.db.database import async_session
            from app.db.models import TestCase

            async with async_session() as session:
                stmt = select(TestCase).where(TestCase.req_id == original_req_id)
                result = await session.execute(stmt)
                rows = result.scalars().all()
                return [
                    {
                        "id": r.id,
                        "title": r.title,
                        "type": r.type,
                        "case_json": r.case_json,
                    }
                    for r in rows
                ]
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
        product_id: str = "",
        product_name: str = "",
        clarify_answers: dict | None = None,
        existing_req_id: str = "",
    ) -> AsyncGenerator[dict, None]:
        """运行完整流水线，流式 yield SSE 事件。

        使用 LangGraph compiled.astream(stream_mode="updates") 流式执行，
        每个节点完成后 yield 一个 SSE 事件，最终 yield done 事件含完整结果。

        Args:
            raw_requirement: 原始需求文本
            product_id: 产品 ID（可选）
            product_name: 产品名称（可选）
            clarify_answers: 交互式澄清答案（可选，首轮不传；D2 决策 API 驱动）
            existing_req_id: 已有需求 ID（可选，从 DB 取的需求）。传入时复用此 ID，
                避免重新解析生成新 req_id 导致 test_cases 外键违反。

        Yields:
            dict: SSE 事件，格式：
                {"stage": "parse|select_channel|clarify|gate|impact|retrieve|generate|review|done|error",
                 "message": "...",
                 "progress": 0-100,
                 ...附加字段}

        Note:
            - 节点异常被捕获，yield error 事件后停止
            - Clarify 回环（最多 3 轮）会多次 yield clarify/gate 事件
            - 最终 done 事件含 reviewed_cases / review_report / 各节点结果
        """
        initial_state: PipelineState = {
            "raw_requirement": raw_requirement,
            "product_id": product_id,
            "product_name": product_name,
            "clarify_answers": clarify_answers or {},
            "clarify_iteration": 0,
            "start_time": time.time(),
            "existing_req_id": existing_req_id,
        }

        # 节点名 → SSE stage 映射
        node_to_stage = {
            "parse": "parse",
            "select_channel": "select_channel",
            "clarify": "clarify",
            "gate": "gate",
            "impact": "impact",
            "retrieve": "retrieve",
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
            "retrieve": "知识库检索中...",
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
            async for chunk in self.graph.astream(initial_state, stream_mode="updates"):
                # chunk 格式：{node_name: state_update_dict}
                for node_name, update in chunk.items():
                    if node_name == "__end__":
                        continue
                    if not isinstance(update, dict):
                        continue

                    # 合并到 final_state
                    final_state.update(update)

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
                        event["blockers_count"] = len(gr.get("blockers", []) or [])
                    elif node_name == "impact":
                        ir = final_state.get("impact_result", {}) or {}
                        event["is_optimization"] = ir.get("is_optimization", False)
                    elif node_name == "generate":
                        event["cases_count"] = len(final_state.get("test_cases", []) or [])
                    elif node_name == "review":
                        rr = final_state.get("review_result", {}) or {}
                        event["review_passed"] = rr.get("passed_cases", 0)
                        event["review_blocked"] = rr.get("blocked_cases", 0)
                        event["should_supplement"] = rr.get("should_supplement", False)
                        event["avg_confidence"] = rr.get("avg_confidence", 0.0)

                    yield event

            # 流水线完成
            duration = time.time() - final_state.get("start_time", time.time())
            done_event = make_sse_event(
                "done",
                "流水线完成",
                req_id=final_state.get("req_id", ""),
                channel=final_state.get("channel", ""),
                srr=final_state.get("srr"),
                cases=final_state.get("reviewed_cases", []),
                review_report=final_state.get("review_result", {}),
                clarify_result=final_state.get("clarify_result", {}),
                gate_result=final_state.get("gate_result", {}),
                impact_result=final_state.get("impact_result", {}),
                few_shot_context=final_state.get("few_shot_context", {}),
                kb_flowback=final_state.get("kb_flowback", {}),
                duration_ms=int(duration * 1000),
            )
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
            yield make_sse_event(
                "error",
                f"流水线异常: {str(e)[:200]}",
                req_id=final_state.get("req_id", ""),
            )


# ============================================================================
# 全局单例
# ============================================================================

orchestrator = Orchestrator()
