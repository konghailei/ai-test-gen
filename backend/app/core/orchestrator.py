r"""LangGraph DAG 编排器。

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

DAG 结构（v3.4.4：原 FAST/FULL 通道分级已删除，分支条件改为「是否启用生成期澄清」）：
    START → parse → select_channel（生成准备）
                      ↓
                [route_after_channel]
                 /              \
        启用生成期澄清        未启用（生产实际路径）
                ↓                ↓
            clarify          gate(直接)
                ↓                ↓
           [route_after_gate]    ↓
            /     |     \        ↓
         retry  pass  force    ↓
           ↓     ↓      ↓       ↓
        clarify impact impact   ↓
                  ↓      ↓      ↓
          test_design test_design  ↓
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
from app.core.channel_selector import build_generation_config
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
        channel_config: GenerationConfig 序列化后的 dict（本次生成的环节开关）
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
    # v3.4：批内近重复去掉的记录（可审计：谁因为和谁重复被去掉）
    dedup_removed: list
    publication: dict
    completion_status: str
    failed_subtasks: list
    task_id: str
    _fp_subtasks: list
    _fp_groups: list
    # ⚠️ v3.4（docs/15 §4.2 第 1 条）补声明：这三个字段此前**未声明**，会被 LangGraph
    # 静默过滤 → `_gen_node` 里 `state.get("resume")` 恒为 False、
    # `state.get("resume_checkpoint")` 恒为 None —— **断点续跑从未真正生效过**。
    # 实测审计（state.get 全量比对 PipelineState 声明）抓出；同源问题已修过三例
    #（consistency_check / functional_points / content_errors）。
    resume: bool
    resume_checkpoint: dict
    _last_node: str
    # v3.4.5（缺陷 E）：跨通道/跨子轮语义去重的留痕（generate 节点产出，
    # done 事件消费）。未声明会被 LangGraph 静默过滤 → 前端永远看不到。
    semantic_dedup_removed: list

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
        **extra: 附加字段（如 req_id、cases_count 等）

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


def initial_degraded_flags(checkpoint_backend_degraded: bool) -> list[str]:
    """流水线启动时的降级标记（v3.4.6 / 缺陷 A 修复）。

    ⚠️ 为什么需要这个函数：
    `_ensure_checkpoint_backend()` 在 PostgresSaver 初始化失败时置
    `self._checkpoint_backend_degraded = True`，但该标记**全库只有定义与赋值、
    零消费方**（2026-09-28 审计实证）——docstring 承诺的
    「done 事件的 degraded_flags 可见」是假承诺，检查点静默退化为 memory
    （重启即丢断点），运维完全无感知。

    修复方式：在流水线入口把标记转为初始 state 的 `degraded_flags`，
    之后各节点沿用「list(state.get("degraded_flags")) + append」的既有形态
    逐级透传到 done 事件与 publication 决策。

    ⚠️ `degraded_flags` **必须**在 PipelineState 中显式声明，否则 LangGraph
    会静默过滤未声明字段（累计已踩 5 例），入口注入的标记会被丢弃。

    Args:
        checkpoint_backend_degraded: Orchestrator 实例上的降级标记

    Returns:
        初始 degraded_flags 列表（未降级时为空列表）
    """
    if not checkpoint_backend_degraded:
        return []
    return ["checkpoint_backend_degraded"]


def _srr_is_fp_degraded(srr: dict | None) -> tuple[bool, str]:
    """检测 SRR 上的功能点提取降级标记（生成前置 fail-closed 门禁用）。

    读取两个来源，任一命中即视为降级：
      - `_fp_extract_degraded`（需求侧 ReqAgent 落库标记，v3.4.6 / 缺陷 B）
      - `_finalize_degraded`（最终 SRR 重生成失败、降级沿用首次解析结果）

    ⚠️ 为什么生成阶段要拦这个：
    `Requirement.srr_json` 是生成流水线的**唯一输入**（`_parse_node` 直接消费）。
    若该 SRR 是在「功能点提取失败（LLM 不可用）」或「最终 SRR 重生成失败」的条件下
    产出的，那么基于它生成的全部用例都建立在**不完整/未校验的需求**之上。
    此类内容同样属于「质量不佳」，不应产出可被自动核准的用例。

    Returns:
        (是否降级, 原因文案)
    """
    if not isinstance(srr, dict):
        return False, ""
    fp_flag = bool(srr.get("_fp_extract_degraded"))
    fin_flag = bool(srr.get("_finalize_degraded"))
    if not (fp_flag or fin_flag):
        return False, ""
    reason = str(
        srr.get("_fp_extract_error") or srr.get("_finalize_error") or ""
    ).strip()[:200]
    which = []
    if fp_flag:
        which.append("_fp_extract_degraded")
    if fin_flag:
        which.append("_finalize_degraded")
    detail = "需求侧降级标记=" + "+".join(which)
    if reason:
        detail += f"（原因：{reason}）"
    return True, detail


class DegradedInputError(RuntimeError):
    """生成前置门禁：SRR 带降级标记，拒绝启动流水线（fail-closed）。

    设计依据（2026-09-28 大哥拍板「改成硬失败，不允许质量不佳的内容漏过去」）：
    - 降级 SRR 产出的用例不得进入自动发布流程；此前只在 done 事件里透出
      `degraded_flags`，产物仍会落到 PG（仅拦 Milvus），且报告中心标记为
      「待人工审核」——**人工点一下 approve 就能把降级内容变成权威知识**。
    - 改为入口硬失败后，降级根本不会产生用例产物，从源头杜绝。

    继承 `RuntimeError` 而非 `Exception`：便于调用方与既有 `except Exception`
    兼容，同时语义上是"运行条件不满足"而非编程错误。

    ⚠️ 异常由 `run_pipeline` 内部捕获并转为 `error` 事件（**终态可见**），
    不会向上击穿成 500 —— fail-closed 要落成终态，不是裸异常。
    """


class CheckpointBackendError(RuntimeError):
    """检查点后端（postgres）初始化失败 —— 拒绝启动流水线（fail-closed）。

    设计依据（2026-09-28 大哥拍板「改成硬失败」）：
    默认配置 `PIPELINE_CHECKPOINT_BACKEND=postgres` 下，若 PostgresSaver
    初始化失败（PG 不可达 / 无权限 / 建表失败），此前会静默降级为 MemorySaver：
    流水线照常跑完并产出一批用例，而"重启后断点不可恢复"这一事实仅存于日志。
    降级属于**部署前置条件未满足**，应在入口拒绝，而不是带伤产出。

    与「显式配置 memory」的区别：显式 `=memory` 是用户主动选择，照常运行；
    声明 `=postgres` 却连不上则是异常，必须修好再跑。

    继承 `RuntimeError`，由 `run_pipeline` 捕获并转为 `error` 事件。
    """


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

    通道选择（doc-00 §9.1）已于 v3.4.4 删除，现无 FULL/FAST 通道机制。
    生成准备节点（select_channel）只构造环节开关，由各节点据此决定执行或跳过：
        - enable_clarify：是否启用澄清（已确认需求生成期 clarify 恒跳过）
        - enable_impact_analysis：是否启用影响分析（impact 节点据此决定执行或跳过）
        - enable_multi_type_generation：是否启用多类型用例生成
        - enable_full_review：是否启用完整审核

    Clarify 回环（doc-01 §5.3 + doc-03 §6.7）：
        - QualityGate 不达标且无 blocker-only 问题时，回环到 ClarifyAgent
        - 最多 3 轮，超过则强制通过 + 警告

    Usage:
        orchestrator = Orchestrator()
        async for event in orchestrator.run_pipeline("用户登录功能..."):
            print(event)  # SSE 事件 dict
    """

    def __init__(self) -> None:
        """初始化编排器。

        v3.4（docs/15 §4.2）：图改为**惰性构建**——检查点后端（PostgresSaver）需要 async
        初始化（连接池 + 建表），不能在 import 时的同步 __init__ 里完成；
        首次 run_pipeline 时 `_ensure_checkpoint_backend()` → `_get_graph()`。
        """
        self.graph = None
        self._checkpointer = None          # MemorySaver | AsyncPostgresSaver
        self._checkpoint_pool = None       # psycopg AsyncConnectionPool（postgres 后端）
        self._checkpoint_backend_degraded = False
        # v3.1.3 细粒度生成进度事件队列（run_pipeline 内创建，_gen_node 消费）
        from contextvars import ContextVar
        self._gen_event_queue: ContextVar = ContextVar("gen_event_queue", default=None)

    # ========================================================================
    # v3.1.3 断点续跑 checkpoint（断电/断网中断后，续跑跳过已完成功能点）
    # ========================================================================
    async def _save_gen_checkpoint(self, task_id: str, fp_groups: list[dict],
                                   fingerprint: dict | None = None) -> None:
        """把各功能点已生成的结果写入 analysis_tasks.result_json，供断点续跑恢复。

        Args:
            task_id: 生成任务 ID
            fp_groups: 每功能点 {fp_id, fp_name, case_count, cases}
            fingerprint: **输入指纹**（docs/15 §4.2）——记录这份断点对应哪份输入
                （SRR/FP 集合/需求修订/prompt/模型/合同版本），续跑前逐字段比对，
                不一致就拒绝续跑。不传时保留旧指纹（不回退成"无指纹"）。
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
                old_cp = dict(old.get("checkpoint") or {})
                new_cp = {"fp_groups": list(fp_groups), "completed": len(fp_groups)}
                # 指纹：新值优先；不传则保留旧值（绝不能回退成"无指纹"——那会让续跑校验失效）
                new_cp["fingerprint"] = fingerprint or old_cp.get("fingerprint") or {}
                old["checkpoint"] = new_cp
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

    async def _ensure_checkpoint_backend(self) -> None:
        """确保检查点后端就绪（幂等，首次 run_pipeline 时调用一次）。

        - postgres（默认）：AsyncPostgresSaver + psycopg 连接池（autocommit），
          `setup()` 幂等建 checkpoints/checkpoint_blobs/checkpoint_writes 三张表；
          thread_id = task_id —— 进程重启后任务级状态仍可审计与恢复（durable execution）；
        - memory：进程内内存 saver，**必须在 settings 中显式声明才允许使用**
          （`PIPELINE_CHECKPOINT_BACKEND=memory`，供单机开发 / 无 PG 演示场景）。

        ⚠️ v3.4.6 起改为 **fail-closed**（2026-09-28 大哥拍板「改成硬失败」）：
        当配置为 postgres（默认）却初始化失败时，**直接抛 `CheckpointBackendError`，
        不再静默降级为 memory**。原因：
          - 静默降级到 memory 意味着"重启后断点不可恢复、durable execution 失效"，
            而流水线会照常产出一批看似正常的用例 —— 降级产物会带着
            「断点不可恢复」这一事实流入知识库，且此前只有日志/一个标记能察觉；
          - 检查点降级属于**部署前置条件未满足**，应当在入口就拒绝启动，
            而不是带着降级状态跑完全程。

        与「显式配置 memory」的区别（这个边界很重要）：
          - 显式声明 `=memory` → 用户的主动选择，照常运行（不抛错）；
          - 声明 `=postgres` 但连不上 → **异常**，必须修好 PG 再跑。
        绝对允许 memory 静默顶替 postgres，等于让"数据库挂了"伪装成"一切正常"。

        Raises:
            CheckpointBackendError: 配置 postgres 但初始化失败（含 DSN 为空）
        """
        if self._checkpointer is not None:
            return
        backend = str(getattr(settings, "PIPELINE_CHECKPOINT_BACKEND", "postgres") or "memory").lower()
        if backend == "postgres":
            try:
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
                from psycopg_pool import AsyncConnectionPool

                dsn = str(settings.DATABASE_URL or "").replace("postgresql+asyncpg://", "postgresql://", 1)
                if not dsn:
                    raise ValueError("DATABASE_URL 为空，无法初始化 PostgresSaver")
                pool = AsyncConnectionPool(
                    dsn, min_size=1, max_size=4, open=False,
                    kwargs={"autocommit": True},
                )
                await pool.open(wait=True, timeout=15)
                saver = AsyncPostgresSaver(pool)
                await saver.setup()
                self._checkpoint_pool = pool
                self._checkpointer = saver
                logger.info("[Orchestrator] 检查点后端 = postgres（thread_id=task_id，表已就绪）")
                return
            except Exception as e:
                # v3.4.6 fail-closed：配置为 postgres 却初始化失败 → 拒绝启动，
                # 不再静默降级为 memory（见本方法 docstring）。
                self._checkpoint_backend_degraded = True
                logger.error(
                    "[Orchestrator] ⛔ PostgresSaver 初始化失败，拒绝启动流水线"
                    "（fail-closed，不再降级为 memory）: {}",
                    str(e)[:200],
                )
                raise CheckpointBackendError(
                    "检查点后端（postgres）初始化失败，已拒绝启动用例生成流水线"
                    "（fail-closed）。当前配置要求断点可恢复（durable execution），"
                    "不允许静默降级为内存检查点——那会让重启后的断点静默失效。"
                    f"请修复 PG 连接/建表后重试；若确需内存检查点请在配置中"
                    f"显式设置 PIPELINE_CHECKPOINT_BACKEND=memory。原因：{str(e)[:200]}"
                ) from e
        from langgraph.checkpoint.memory import MemorySaver
        logger.warning(
            "[Orchestrator] 检查点后端 = memory（显式配置；重启后断点不可恢复）"
        )
        self._checkpointer = MemorySaver()

    def _get_graph(self):
        if self.graph is None:
            self.graph = self._build_graph(checkpointer=self._checkpointer)
        return self.graph

    async def close_checkpoint_backend(self) -> None:
        """释放检查点连接池（lifespan 关闭阶段调用）。"""
        if self._checkpoint_pool is not None:
            try:
                await self._checkpoint_pool.close()
            except Exception:  # noqa: BLE001
                pass
            self._checkpoint_pool = None

    def _build_graph(self, checkpointer=None):
        """构建 LangGraph 状态图。

        节点：parse / select_channel / clarify / gate / impact / test_design / generate / review
        条件边：
            - select_channel → clarify（仅当启用生成期澄清；已确认需求恒走 gate）
            - gate → impact (通过或强制) / clarify (回环重试)

        Returns:
            CompiledGraph: 已编译的可执行图
        """
        graph: StateGraph = StateGraph(PipelineState)

        # 注册节点（v3.1：移除 retrieve 节点，SRR 足够详细无需 KB 检索）
        graph.add_node("parse", self._parse_node)
        graph.add_node("select_channel", self._prepare_node)
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

        # 条件边：select_channel → 启用生成期澄清则走 clarify，否则直接到 gate
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

        return graph.compile(checkpointer=checkpointer)

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

    async def _prepare_node(self, state: PipelineState) -> dict:
        """节点 2：生成准备（原「通道选择」；v3.4.4 去除通道分级后更名）。

        节点 key 仍为 "select_channel"，以保持 SSE stage 与前端契约不变。

        v3.4.4 变更：
            原 FAST / FULL 通道分级已于 v1.6 废止（恒为 FULL，原因见
            `core/channel_selector.py` 模块 docstring），本次清理掉 channel 概念。
            本节点现在的实际职责是**决定本次生成启用哪些环节**。

        关键行为：
            若传入 existing_req_id（两个生产入口 generate.py / task_selfheal.py 都会传），
            说明需求已走完确认流程、澄清已在需求分析链路完成，因此强制跳过生成期澄清：
                - 生成期再澄清无意义（问题无人回答）
                - 且易因 LLM 配额 / 网络故障卡死
            multi_type / full_review / impact_analysis 等质量能力全部保留。

        Returns:
            dict: state 更新，含 channel_config（环节开关）
        """
        srr = state.get("srr", {})
        req_id = state.get("req_id", "")
        existing_req_id = state.get("existing_req_id", "") or ""

        logger.info(
            "[Orchestrator] ▶ 节点 select_channel(生成准备): req_id={} existing_req_id={}",
            req_id, existing_req_id,
        )

        config = build_generation_config(srr)
        # 已确认需求：强制跳过生成期澄清（进入 gate → impact → ...），
        # 多类型生成、完整审核、影响分析等质量能力全部保留。
        if existing_req_id:
            config = config.model_copy(update={"enable_clarify": False})
            logger.info(
                "[Orchestrator] 确认需求已存在（existing_req_id={}），跳过生成期澄清: "
                "保留 multi_type & full_review & impact_analysis，仅把 enable_clarify=False",
                existing_req_id,
            )
        next_node = "clarify" if config.enable_clarify else "gate"

        logger.info(
            "[Orchestrator] ◀ 节点 select_channel 完成 ▶ 下一节点 {}: "
            "clarify={}, impact={}, multi_type={}, full_review={}",
            next_node,
            config.enable_clarify,
            config.enable_impact_analysis,
            config.enable_multi_type_generation,
            config.enable_full_review,
        )

        return {
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

        # v3.4.4：FAST 概念已删除，此处只看 enable_clarify（已确认需求恒为 False）。
        # gate 未通过时直接强制通过，不回环 clarify（无人回答，回环无意义且易卡死）
        _cfg = state.get("channel_config", {}) or {}
        clarify_disabled = not (
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
            warnings.append("未启用生成期澄清（已确认需求），QualityGate 未通过但强制放行")
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

        # 透传影响分析未确认标记到 degraded_flags（保持与 _test_design_node 同构）
        degraded_flags: list[str] = list(state.get("degraded_flags", []) or [])
        if result.get("impact_unconfirmed"):
            degraded_flags.append("impact_unconfirmed")

        logger.info(
            # v3.1.3: 修正过时日志——v2.3 起 impact 的下一节点是 test_design 而非 generate
            "[Orchestrator] ◀ 节点 impact 完成 ▶ 下一节点 test_design: "
            "is_optimization={}, guidance_len={}, original_cases={}, unconfirmed={}",
            is_opt,
            guidance_len,
            len(original_cases),
            bool(result.get("impact_unconfirmed")),
        )

        return {"impact_result": result, "degraded_flags": degraded_flags}

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
        根据 channel_config（本次生成的环节开关）决定是否启用分类生成，注入影响分析指导。
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
            # v3.4.1 修复 L-13：分母固定为功能点总数（functional_points）。
            # 原实现取"已产生 subtask 的 FP 个数"作分母，会随生成推进逐条增长 ——
            # 第 1 个 FP 完成时显示 100%，第 2 个 FP 开始时又掉回 50%，进度来回跳；
            # 且失败的 FP 计入分母却永不计入分子，进度永远到不了 100%。
            # max(...) 保留原兜底：FP 为空走全量兜底时 total 至少等于 covered。
            total_now = len(functional_points or [])
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
        # v3.4（docs/15 §4.2 第 3 条）：计算本任务的**输入指纹**，随每次 checkpoint 写入。
        # 续跑时 API 层会用"当前输入"重算并逐字段比对（不一致 → 409）。
        # 注意这里要取需求修订号（DB），保证与 API 侧的指纹字段完全一致。
        checkpoint_fingerprint: dict = {}
        try:
            # v3.4.3 修复 P-1：写入侧改为复用 API 续跑校验侧的**同一个函数**
            # load_current_fingerprint（它从 DB 取 product_id / revision / srr_json / FP 集），
            # 从此两侧取值完全同源，不可能再分叉。
            # 原实现写入侧取 SRR 内的 product_id、校验侧取 DB 列；普查实测 42 条需求中有 13 条
            # 的 SRR 里没有该字段 —— 这类需求一旦生成中断后续跑，指纹比对必然不一致 → 409。
            from app.core.pipeline_fingerprint import load_current_fingerprint as _load_fp
            from app.db.database import async_session as _async_session
            from app.db.models import Requirement as _Requirement
            async with _async_session() as _s:
                _req_obj = await _s.get(_Requirement, req_id)
                if _req_obj is not None:
                    checkpoint_fingerprint = await _load_fp(
                        _s, _req_obj, _req_obj.srr_json or {}
                    )
                else:
                    logger.warning(
                        "[Orchestrator] 需求 {} 不存在，本次 checkpoint 不带输入指纹", req_id
                    )
        except Exception as _fp_err:
            logger.warning("[Orchestrator] 输入指纹计算失败（本次 checkpoint 不带指纹，续跑时将被拒绝）: {}",
                           str(_fp_err)[:140])

        # 1) 各功能点分片定向生成（小 prompt）
        # v3.5.4（docs/21 D-5）：大 FP 按要点类型二次分片。
        # 增量文档常把几十个要点塞进同一个 FP（v2.0 实测 1 FP/97 要点 →
        # 单分片 prompt 膨胀 → LLM 覆盖面不足，覆盖率仅 26.8%）。
        # 对要点数 > _BIG_FP_POINT_THRESHOLD 的 FP，按要点类型分组拆成多批，
        # 每批只携带本批要点（fp_scope 机制复用），prompt 缩小、覆盖聚焦。
        from app.utils.coverage import collect_points as _collect_points

        def _split_big_fp(fp: dict) -> list[dict]:
            """单 FP 要点数超阈值时按要点类型分组切子批；否则返回原 [fp]。"""
            sub = fp.get("srr_json") if isinstance(fp.get("srr_json"), dict) else {}
            pts = _collect_points(srr, [fp])
            if len(pts) <= 40:
                return [fp]
            # 分组键：happy_path / alt_flow / rule+shared_rule / data / 其余扩展类型
            def _gk(p: dict) -> str:
                t = p.get("ptype", "")
                if t == "happy_path":
                    return "happy_path"
                if t in ("alt_flow", "alternate_flow"):
                    return "alt_flow"
                if t in ("rule", "shared_rule"):
                    return "rule"
                if t == "data":
                    return "data"
                return "extended"

            groups: dict[str, list[dict]] = {}
            for p in pts:
                groups.setdefault(_gk(p), []).append(p)
            # 只有一个组（全部同类型超限）→ 按数量对半切，避免死循环
            if len(groups) == 1:
                only = next(iter(groups.values()))
                mid = len(only) // 2
                groups = {"part1": only[:mid], "part2": only[mid:]}
            shards: list[dict] = []
            for gk, gpts in groups.items():
                if not gpts:
                    continue
                shard = dict(fp)
                shard["srr_json"] = dict(sub or {})
                # 注入本批要点清单：GenAgent prompt 构建读取 points 字段做定向
                shard["srr_json"]["points"] = [
                    {"id": p["id"], "type": p["ptype"], "description": p["desc"],
                     "priority": p["priority"]} for p in gpts
                ]
                shard["name"] = f"{fp.get('name', '')} [{gk}]"
                shards.append(shard)
            logger.info(
                "[Orchestr] 大FP[{}]要点数={} 超阈值，按类型二次分片为 {} 批: {}",
                fp.get("name", ""), len(pts), len(shards),
                {k: len(v) for k, v in groups.items()},
            )
            return shards

        # ── v3.5.5（docs/22 方案①）：FP 子批受控并发生成 ──
        # FP 之间无数据依赖（各生成各的，汇合点统一三道去重：同名 / 3-gram / semantic 0.92），
        # 原串行循环是纯浪费（自举验证实测 3 FP 串行 LLM ≈ 43.5min）。
        # 并发度从 kb_settings 动态读取（设置页可调，改后立即对下一次生成生效）；
        # 1 = 串行回退（与 v3.5.4 行为完全一致）。
        #
        # 关键语义保持（docs/22 §1 可行性验证结论）：
        #   a) checkpoint 完成即写：每次写入都是全量覆盖（fp_groups 整体重写），
        #      任意完成交织下单调递增、最终一致，无需加锁；
        #      纪律 = append 与 save 必须在同一完成回调内成对出现。
        #   b) 进度事件按 fp_id key 刷新 + 全量快照下发，乱序安全，前端零改动。
        #   c) 单 FP 失败不阻断其它 FP（N9 降级标记语义不变）。
        #   d) D-5 大 FP 子批在单 FP 任务内仍串行（共享该 FP 的要点上下文）。
        gen_fp_conc = 3
        try:
            from app.db.database import async_session as _gs_session
            from app.api.settings import get_gen_fp_concurrency as _get_conc
            async with _gs_session() as _s:
                gen_fp_conc = await _get_conc(_s)
        except Exception as e:
            logger.warning("[Orchestrator] 读取生成并发配置失败（用默认 3）: {}", str(e)[:100])
        _pending_fps = [fp for fp in fp_list if fp.get("id") not in completed_fps]
        logger.info("[Orchestrator] FP 并发度={}（pending FP={} 个）", gen_fp_conc, len(_pending_fps))

        async def _gen_one_fp(fp: dict) -> None:
            """单 FP（含其 D-5 子批）完整生成：局部收集 → 完成回调内合并 + checkpoint。"""
            fid = fp.get("id", "")
            fname = fp.get("name", "")
            # v3.5.4 D-5：大 FP 二次分片（要点级定向，prompt 缩小）
            try:
                shards = _split_big_fp(fp)
            except Exception as e:
                logger.warning("[Orchestrator] FP[{}] 二次分片失败（回退整 FP 生成）: {}", fname, str(e)[:120])
                shards = [fp]
            fp_cases_all: list[dict] = []   # 局部收集（并发下不直接 extend 共享列表）
            shard_total_cases = 0
            single_shard_failed = False     # 单分片 FP 失败：跳过完成回调（原语义：续跑重试）
            async with _gen_sem:
                for _si, shard in enumerate(shards, 1):
                    shard_name = shard.get("name", fname)
                    if len(shards) > 1:
                        await _emit_gen(
                            f"功能点 [{fname}] 子批 {_si}/{len(shards)} 生成中...",
                            {"key": fid, "name": fname, "status": "generating", "cases": shard_total_cases},
                        )
                    else:
                        await _emit_gen(
                            f"功能点 [{shard_name}] 生成中...",
                            {"key": fid, "name": fname, "status": "generating", "cases": 0},
                        )
                    try:
                        fp_cases = await gen_agent.generate(
                            srr,
                            enable_multi_type=enable_multi_type,
                            impact_guidance=impact_guidance,
                            functional_points=[shard],
                            fp_scope=shard,
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
                            {"key": fid, "name": fname, "status": "failed", "cases": shard_total_cases},
                        )
                        fp_cases = []
                        # 子批失败不阻断其它子批，但该 FP 计为失败批次（与原语义一致）
                        if len(shards) == 1:
                            single_shard_failed = True
                            break
                    for c in fp_cases:
                        if not c.get("related_fp"):
                            c["related_fp"] = fid
                    fp_cases_all.extend(fp_cases)
                    shard_total_cases += len(fp_cases)
            # ── 完成回调：合并 + checkpoint（append 与 save 成对、全量覆盖收敛）──
            # v3.5.5 守卫修复：单分片 FP 失败时不进入完成回调（与 v3.5.4 原语义一致：
            # 失败 FP 不写 checkpoint、不发 done → 续跑时 completed_fps 不含它，会重试）。
            # 若这里无条件执行，失败 FP 会被记成"完成 0 条"，续跑被跳过、永不重试。
            if single_shard_failed:
                return
            resolved_cases.extend(fp_cases_all)
            checkpoint_groups.append({
                "fp_id": fid, "fp_name": fname,
                "case_count": shard_total_cases, "cases": list(fp_cases_all),
            })
            await _emit_gen(
                f"功能点 [{fname}] 生成完成：{shard_total_cases} 条"
                + (f"（{len(shards)} 子批）" if len(shards) > 1 else ""),
                {"key": fid, "name": fname, "status": "done", "cases": shard_total_cases},
            )
            try:
                await self._save_gen_checkpoint(task_id, checkpoint_groups, fingerprint=checkpoint_fingerprint)
            except Exception as e:
                logger.debug("[Orchestrator] checkpoint 写入失败（不阻断）: {}", str(e)[:120])

        if gen_fp_conc <= 1 or len(_pending_fps) <= 1:
            # 串行回退（v3.5.4 行为）；单 FP 也无需并发开销
            _gen_sem = asyncio.Semaphore(1)  # _gen_one_fp 内引用，占位保证定义完整
            for fp in _pending_fps:
                await _gen_one_fp(fp)
        else:
            _gen_sem = asyncio.Semaphore(gen_fp_conc)
            _gather_results = await asyncio.gather(
                *[_gen_one_fp(fp) for fp in _pending_fps], return_exceptions=True
            )
            # gather 兜底：任务级异常（理论上 _gen_one_fp 内部已捕获 LLM 错误，
            # 此处只防 _emit_gen / checkpoint 等基础设施异常导致该 FP 静默丢失）
            for fp, r in zip(_pending_fps, _gather_results):
                if isinstance(r, BaseException):
                    fid, fname = fp.get("id", ""), fp.get("name", "")
                    logger.warning(
                        "[Orchestrator] 功能点[{}]并发任务异常（不阻断，续跑将重试）: {}",
                        fname, str(r)[:150],
                    )
                    if "fp_generate_failed" not in degraded_flags:
                        degraded_flags.append("fp_generate_failed")
                    await _emit_gen(
                        f"功能点 [{fname}] 生成失败",
                        {"key": fid, "name": fname, "status": "failed", "cases": 0},
                    )
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
        # v3.4：**跨生成通道**的近重复去重（逐 FP / 全量兜底 / 跨 FP 集成 / 审核后补生成
        # 各自去重，跨通道只有完全同名能被拦住）。实测一次 56 条里有 22 条落在重复簇里，
        # 直接推高用例库冗余。阈值偏保守（宁可漏报不误删），命中记录写进日志与 done 事件。
        from app.utils.case_dedup import dedup_near_duplicates
        cases, dedup_removed = dedup_near_duplicates(cases)
        if dedup_removed:
            logger.info(
                "[Orchestrator] 批内近重复去重: 去掉 {} 条（保留 {} 条）。示例: {}",
                len(dedup_removed), len(cases),
                "; ".join(
                    f"{r['removed_title'][:24]} ≈ {r['kept_title'][:24]}（{r['reason']}）"
                    for r in dedup_removed[:3]
                ),
            )
        # v3.4.5（缺陷 E，对称补漏）：3-gram 只抓得住"表面像"的重复，跨通道**语义改写**
        #   （integration 重述 FP 分片场景）实测 3-gram 0/10 命中——补一道整例 JSON
        #   embedding 语义去重（阈值 0.92 + 数字护栏，定标命中 8/10 误删 0）。
        #   主汇总点与补生成合并点（review 节点）两处对称应用。
        from app.utils.case_dedup import dedup_semantic
        try:
            from app.db.milvus_store import vector_store as _vs_gen
            _embed_gen = _vs_gen.embed_texts
        except Exception:  # noqa: BLE001
            _embed_gen = None
        # bge 推理是同步 CPU 调用，按 GenAgent 先例用 to_thread 包装（避免阻塞事件循环）
        cases, semantic_removed = await asyncio.to_thread(
            dedup_semantic, cases, embed_func=_embed_gen
        )
        if semantic_removed:
            logger.info(
                "[Orchestrator] 跨通道语义去重: 去掉 {} 条（保留 {} 条）。示例: {}",
                len(semantic_removed), len(cases),
                "; ".join(
                    f"{r['removed_title'][:24]} ≈ {r['kept_title'][:24]}（{r['reason']}）"
                    for r in semantic_removed[:3]
                ),
            )
            state["semantic_dedup_removed"] = semantic_removed
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
            "dedup_removed": dedup_removed,
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
            # v3.4.5（缺陷 B）：补生成是**增强步骤**，失败必须降级而非击穿。
            #   实证 3/3：主用例集已完整生成（27 / 11 / 9 条），仅因补生成这一次
            #   LLM 调用超时，run_pipeline 就整条抛异常——已生成用例全部丢弃、
            #   0 落库，用户拿到的结果是「跑了十几分钟、额度烧掉、零产出」。
            #   正确语义：保留首次 review 的主用例集继续走审核与入库，
            #   并把 supplement_failed 如实透出（覆盖率缺口不再被填补）。
            try:
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
                # v3.4.5（缺陷 E）：补生成合并后必须再过一次**语义级**近重复去重。
                #   实证：09-16 批次 21 条里 10 条跨通道重复（47.6%），根因是
                #   补生成/跨通道场景改写后的标题 3-gram 相似度全部 < 0.48
                #   （_generate_node 1245 行的批内去重阈值 0.55，命中 0/10），
                #   只有整例 JSON embedding 抓得住（重复对 0.88~0.98）。
                #   阈值 0.92 + 数字护栏：定标命中 8/10、误删 0；漏的留给人工审核。
                #   失败语义：embedding 环节抖动 → 降级 3-gram，不击穿。
                from app.utils.case_dedup import dedup_semantic
                try:
                    from app.db.milvus_store import vector_store as _vs
                    _embed = _vs.embed_texts
                except Exception:  # noqa: BLE001
                    _embed = None
                # bge 推理是同步 CPU 调用，用 to_thread 包装（与 GenAgent 同一先例）
                merged_cases, _sem_removed = await asyncio.to_thread(
                    dedup_semantic, merged_cases, embed_func=_embed
                )
                if _sem_removed:
                    logger.info(
                        "[Orchestrator] 跨子轮语义去重: 去掉 {} 条（保留 {} 条）。示例: {}",
                        len(_sem_removed), len(merged_cases),
                        "; ".join(
                            f"{r['removed_title'][:24]} ≈ {r['kept_title'][:24]}（{r['reason']}）"
                            for r in _sem_removed[:3]
                        ),
                    )
                report, reviewed_cases = await review_agent.review(
                    srr, merged_cases, functional_points=functional_points
                )
                # v3.4.5（缺陷 E）注意顺序：必须写在 review() **之后**——review 返回
                # 全新 report，先写后 review 会被整体覆盖（标记到不了 done 事件）。
                if _sem_removed:
                    report["semantic_dedup_removed"] = _sem_removed
                supplement_iteration = 1
                # v3.1.3: 二次 review 后仍建议补生成 → 标记已达上限（补生成最多 1 次），
                # 避免前端把 should_supplement=True 误解为"还会再补"
                if report.get("should_supplement"):
                    report["supplement_exhausted"] = True
            except Exception as e:  # noqa: BLE001
                supplement_iteration = 1
                report["supplement_failed"] = True
                report["supplement_error"] = str(e)[:300]
                report["supplement_exhausted"] = True
                _degraded.append("supplement_failed")
                logger.error(
                    "[Orchestrator] ⚠️ 补生成失败，保留主用例集 {} 条继续走审核/入库"
                    "（覆盖率缺口未填补，done 事件带 supplement_failed）: {}",
                    len(reviewed_cases), str(e)[:200],
                )

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
        elif publication["decision"] == "partial_block" and reviewed_cases:
            # v3.5.2（docs/21 方案B"剔坏留好"）：partial_block 允许回流——
            # persist_to_kb 内部会剔除坏条进 error_cases，剩余按置信度分级入库。
            # 批次级质量信号（coverage 等）通过 degraded 标记 + publication 透出。
            logger.warning(
                "[Orchestrator] ✂ 发布决策=partial_block，执行剔坏留好回流: {}",
                publication.get("case_level_reasons"),
            )
            if "publication_partial_block" not in _degraded:
                _degraded.append("publication_partial_block")
            try:
                kb_result = await review_agent.persist_to_kb(
                    srr, reviewed_cases, report, publication_context=publication_context,
                )
                logger.info(
                    "[Orchestrator] partial_block 剔坏留好回流完成: purged={}, auto_approved={}, pending={}, rejected={}",
                    kb_result.get("purged_cases", 0),
                    kb_result.get("auto_approved", 0),
                    kb_result.get("pending_review", 0),
                    kb_result.get("rejected", 0),
                )
                if kb_result.get("vector_write_failed"):
                    report["vector_write_failed"] = kb_result["vector_write_failed"]
                    report.setdefault("warnings", []).append(
                        f"{kb_result['vector_write_failed']} 条用例的 Milvus 向量写入失败，"
                        "PG 已入库但回归检索基线缺失；请检查 Milvus 服务后重新批量审核补写。"
                    )
            except Exception as kb_exc:
                logger.error("[Orchestrator] partial_block 回流异常: {}", str(kb_exc)[:200])
                kb_result = {"error": str(kb_exc)[:300]}
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
        """条件路由：生成准备后决定是否进入生成期澄清。

        v3.4.4：FAST/FULL 概念已删除，只看 channel_config.enable_clarify。
        已确认需求该值为 False（其澄清在需求分析链路完成），故生产路径恒走 gate。

        Args:
            state: 当前流水线状态

        Returns:
            str: "clarify" 或 "gate"
        """
        cfg = state.get("channel_config", {}) or {}
        enable_clarify = cfg.get("enable_clarify", False) if isinstance(cfg, dict) else getattr(cfg, "enable_clarify", False)
        return "clarify" if enable_clarify else "gate"

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

        # v3.4.4 防御：未启用生成期澄清（已确认需求）时绝不回环
        _cfg = state.get("channel_config", {}) or {}
        _enable_clarify = (
            _cfg.get("enable_clarify", False) if isinstance(_cfg, dict)
            else getattr(_cfg, "enable_clarify", False)
        )
        if not _enable_clarify:
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

        # v3.4（docs/15 §4.2）：图惰性编译依赖检查点后端已就绪，故后端初始化
        # 必须在建图之前完成。但**不得放在 try 之外**——`_ensure_checkpoint_backend`
        # v3.4.6 起对 postgres 初始化失败改为抛 `CheckpointBackendError`，
        # 若在 try 之外抛出会击穿成 500（违背「fail-closed 落成终态」）。
        # 因此下面把「后端初始化 + 建图 + 前置门禁 + 初始 state」整体移入 try。
        graph = None

        # v3.4.6（缺陷 A 修复）：检查点后端降级必须**到达消费方**。
        # 反例（2026-09-28 实测）：_ensure_checkpoint_backend 在 PG 初始化失败时置
        # self._checkpoint_backend_degraded = True，但该标记**全库只有定义与赋值、
        # 零消费方**——文档承诺的「done 事件 degraded_flags 可见」是假承诺。
        # 后果：检查点静默退化为 memory（重启即丢断点），运维完全无感知。
        # 修复：在流水线入口把它注入初始 state 的 degraded_flags，之后各节点
        # 沿用「list(state.get("degraded_flags")) + append」的既有形态逐级透传到
        # done 事件与 publication 决策（降级必须进人工审核，不得自动核准）。
        # ⚠️ v3.4.6 补充（大哥拍板硬失败）：postgres 初始化失败时
        # `_ensure_checkpoint_backend()` 现已直接抛 `CheckpointBackendError`
        # （见其 docstring），故 `initial_degraded` 仅在**显式配置 memory** 等
        # 情况下非空；该注入链保留为通用兜底，确保"降级启动"永远可见。
        initial_degraded: list[str] = initial_degraded_flags(
            getattr(self, "_checkpoint_backend_degraded", False)
        )
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
            # ⚠️ 必须显式声明 degraded_flags（PipelineState 已声明），
            # 否则 LangGraph 静默过滤 → 入口注入的标记丢失。
            "degraded_flags": initial_degraded,
        }
        if initial_degraded:
            logger.warning(
                "[Orchestrator] ⚠️ 流水线以降级标记启动（检查点后端非 postgres，"
                "重启后断点不可恢复）: {}", initial_degraded,
            )

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
            # v3.4.6（缺陷 A 硬失败）：检查点后端初始化。postgres 配置下失败会抛
            # `CheckpointBackendError`，在 try 内 → 转为 error 事件（终态可见）。
            await self._ensure_checkpoint_backend()
            graph = self._get_graph()

            # v3.4.6（fail-closed 前置门禁）：输入 SRR 带降级标记 → 拒绝生成。
            # 大哥拍板「改成硬失败，不允许质量不佳的内容漏过去」（2026-09-28）。
            # 背景：降级 SRR 此前只在 done 事件透出 degraded_flags，产物仍落入 PG
            # （仅拦 Milvus），人工点一次 approve 即可把降级内容变成权威知识。
            # 前置门禁把问题挡在**产生副作用之前**：不启动流水线 ⇒ 无用例产物 ⇒
            # 不可能被 approve ⇒ 权威库不被污染。
            # 放在 try 内（而非函数开头）是为了让失败落成**终态**：本函数末尾的
            # except 会把它转为 error 事件 + DLQ 记录，不击穿成 500。
            _degraded_input, _degraded_detail = _srr_is_fp_degraded(srr_json)
            if _degraded_input:
                logger.error(
                    "[Orchestrator] ⛔ 生成前置门禁：SRR 带降级标记，拒绝启动流水线 "
                    "(fail-closed)。req_id={}, task_id={}, {}",
                    existing_req_id, task_id, _degraded_detail,
                )
                raise DegradedInputError(
                    "需求 SRR 处于降级状态，已拒绝生成用例（fail-closed）。"
                    "基于降级需求产出的用例质量不可保证，不会被发布。"
                    f"请先在「需求管理」中重新分析该需求以修复降级。{_degraded_detail}"
                )

            # v3.1.3: 追踪最后完成的节点——final_state 为本地 dict（非 LangGraph state 通道），
            # 写入 _last_node 不会被 StateGraph 过滤；异常写 DLQ 时用于定位失败节点
            # v3.1.3 事件队列：把 LangGraph 的节点完成事件与 _gen_node 内部的细粒度
            # FP 进度事件合并到一个队列流式透给前端（按序 interleave）。
            queue: asyncio.Queue = asyncio.Queue()
            queue_token = self._gen_event_queue.set(queue)

            # v3.5.6：LLM 等待重试回调 —— llm_client 在"三家全败等待重试"时，
            # 通过此回调推 SSE 事件给前端（红色提示条：供应商暂不可用/请人工排查）。
            # ContextVar 隔离：只影响当前生成任务，不影响其它协程/后台脚本。
            async def _llm_wait_retry_cb(message: str, level: str,
                                         attempt: int, total_retries: int,
                                         extra: dict | None = None) -> None:
                from app.core.orchestrator import make_sse_event
                payload = {
                    "message": message, "level": level,
                    "attempt": attempt, "total_retries": total_retries,
                }
                if extra:
                    payload.update(extra)  # v3.5.6: 倒计时片携带 remain_sec
                ev = make_sse_event("generate", message, wait_retry=payload)
                try:
                    await queue.put(ev)
                except Exception:
                    pass

            from app.core.llm_client import llm_client as _llm_for_cb
            cb_token = _llm_for_cb._wait_retry_cb.set(_llm_wait_retry_cb)

            # v3.4（docs/15 §4.10）：节点级 span。
            # stream_mode="updates" 的 chunk 到达 = 该节点执行结束：
            # 「本 chunk 到达时刻 - 上个 chunk 到达时刻」= 本 chunk 节点的执行时长
            #（首个 chunk 的前置开销不计入任何节点）。
            _node_spans: list[dict] = []
            _span_last_ts: float | None = None

            async def _producer() -> None:
                nonlocal _span_last_ts
                try:
                    # thread_id = task_id：同任务重入共享检查点；临时运行用合成 ID
                    import uuid as _uuid
                    _thread_id = task_id or f"run-{_uuid.uuid4().hex}"
                    async for chunk in graph.astream(
                        initial_state,
                        config={"configurable": {"thread_id": _thread_id}},
                        stream_mode="updates",
                    ):
                        _now = time.monotonic()
                        _names = [k for k in chunk.keys() if k != "__end__"]
                        if _names:
                            if _span_last_ts is not None:
                                _node_spans.append({
                                    "node": _names[0],
                                    "duration_ms": int((_now - _span_last_ts) * 1000),
                                })
                            _span_last_ts = _now
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
                    # v3.5.6: 重置 LLM 等待重试回调
                    try:
                        from app.core.llm_client import llm_client as _llm_for_cb
                        _llm_for_cb._wait_retry_cb.reset(cb_token)
                    except Exception:
                        pass

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
                dedup_removed=final_state.get("dedup_removed", []),
                semantic_dedup_removed=final_state.get("semantic_dedup_removed", []),
                # v3.4（docs/15 §4.10）：节点级 span（trace = 任务 → 节点 → LLM 调用）
                node_spans=_node_spans,
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

            # v3.4.6（fail-closed 前置门禁）：**故意拒绝**与**意外故障**要分流。
            # 降级输入被拒 / 检查点后端未就绪，都是**确定性的前置条件不满足**
            # （重试必然再次失败），推入 DLQ 只会让运维反复重试一个不可能成功的
            # 任务；且错误文案需要原样传达给用户（指明修复动作），不该被
            # "流水线异常:"前缀污染。
            if isinstance(e, DegradedInputError):
                _reject_reason, _reject_at = "degraded_srr_rejected", "entry"
            elif isinstance(e, CheckpointBackendError):
                _reject_reason, _reject_at = "checkpoint_backend_unavailable", "entry"
            else:
                _reject_reason, _reject_at = "pipeline_error", ""
            _is_gate_reject = bool(_reject_reason != "pipeline_error")

            if not _is_gate_reject:
                # L4 数据层防护：主流水线异常写入 DLQ，支持事后审计与手动重试
                # 对应 docs/00 §2 L4 防护承诺，闭合 orchestrator 主流水线死信兜底
                try:
                    from app.core.dlq import dlq
                    dlq.push(
                        task_type="pipeline",
                        payload={
                            "req_id": final_state.get("req_id", ""),
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
                str(e)[:400] if _is_gate_reject else f"流水线异常: {str(e)[:200]}",
                req_id=final_state.get("req_id", ""),
                # 前端可据 reason 精确判定并给出对应引导
                # （degraded_srr_rejected → 重新分析需求；checkpoint_backend_unavailable → 检查 PG）
                reason=_reject_reason,
                rejected_at=_reject_at,
            )


# ============================================================================
# 全局单例
# ============================================================================

orchestrator = Orchestrator()
