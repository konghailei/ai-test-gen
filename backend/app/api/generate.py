"""SSE 流式生成路由（核心端点）。

POST /api/generate —— 调用 LangGraph 编排器，流式返回测试用例生成进度与结果。

设计依据：
- docs/00-architecture-overview.md §9.2 进度反馈机制（SSE）
- docs/00 §3 核心数据流（9 节点编排：parse → select_channel → clarify → gate → impact → test_design → generate → review → done）
- 实现计划 §T5.3 SSE 实现
- D4 决策：后端用 sse-starlette 的 EventSourceResponse，前端用 fetch + ReadableStream

SSE 事件格式（由 orchestrator.make_sse_event 产生）：
    {"stage": "parse|select_channel|clarify|gate|impact|test_design|generate|review|done|error",
     "message": "人类可读进度消息",
     "progress": 0-100,
     ...附加字段}

前端消费方式（D4 决策）：
    由于 POST 请求不支持原生 EventSource，前端使用 fetch + ReadableStream 读取 SSE 流。
    每条事件的 data 字段为 JSON 字符串，前端 JSON.parse 后按 stage 分发处理。

流程说明：
    1. 前端先调用 POST /api/requirements/upload 上传需求，得到 req_id
    2. （可选）GET /api/requirements/{id}/clarify/questions 获取澄清问题
    3. （可选）POST /api/requirements/{id}/clarify 提交澄清答案
    4. POST /api/generate 传入 req_id，启动 SSE 流式生成
    5. 后端从 DB 取 raw_text + clarify_answers，调用 orchestrator.run_pipeline
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from app.core.auth import require_write, get_current_user, CurrentUser
from app.core.orchestrator import orchestrator
from app.core.product_access import assert_product_visible
from app.core.case_type_taxonomy import summarize_by_group
from app.db.database import async_session
from app.db.models import Requirement, AnalysisTask
from app.models.api_schemas import GenerateRequest
from app.utils.id_gen import gen_task_id
from app.utils.logger import logger


router = APIRouter()


async def _find_resume_checkpoint(req_id: str, from_task: str = "") -> dict | None:
    """查找可续跑的 checkpoint。

    断点续跑：从最近一次「中断/中止/作废」的 generate_cases 任务读取 result_json.checkpoint。
    返回 (resume_checkpoint, source_task_id)。

    Args:
        req_id: 需求 ID
        from_task: 指定来源任务 ID（留空自动查找最近可续任务）

    Returns:
        dict | None: {"checkpoint": {...}, "source_task_id": "..."}
    """
    try:
        async with async_session() as _s:
            if from_task:
                stmt = select(AnalysisTask).where(AnalysisTask.id == from_task)
            else:
                stmt = (
                    select(AnalysisTask)
                    .where(
                        AnalysisTask.req_id == req_id,
                        AnalysisTask.task_type == "generate_cases",
                        AnalysisTask.status.in_(["interrupted", "aborted", "cancelled", "failed"]),
                    )
                    .order_by(AnalysisTask.created_at.desc())
                )
            result = await _s.execute(stmt)
            task = result.scalars().first()
            if task is None or not isinstance(task.result_json, dict):
                return None
            cp = task.result_json.get("checkpoint")
            if not isinstance(cp, dict) or not cp.get("fp_groups"):
                return None
            return {"checkpoint": cp, "source_task_id": task.id}
    except HTTPException:
        raise
    except Exception as e:
        # v4.1.9：查询失败不能静默返回 None——调用方会把 None 当成「无可用
        # checkpoint」直接从头生成，把用户已有的进度覆盖掉。失败必须可见。
        logger.error(
            "[API/generate] 续跑 checkpoint 查找失败，中止续跑: req_id={}, err={}",
            req_id, str(e)[:200],
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "续跑检查点查询失败，已中止本次续跑以避免覆盖已有进度。"
                "请稍后重试；若确认要放弃旧进度，请改选「重新生成」。"
            ),
        ) from e


@router.post("/generate", dependencies=[Depends(require_write)])
async def generate_test_cases(
    req: GenerateRequest,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    """SSE 流式生成测试用例。

    Args:
        req: 生成请求，包含 req_id（必填）和 channel（可选，期望通道）

    Returns:
        EventSourceResponse: SSE 流式响应，每个事件含 stage/message/progress 等字段

    Raises:
        HTTPException 404: req_id 对应的需求不存在

    Note:
        - 生成期澄清：本链路默认跳过（需求已在上传阶段澄清并确认，
          见 orchestrator 生成准备节点）；v3.4.4 起不再有 FAST/FULL 通道概念。
        - 澄清答案：若需求已提交澄清答案（存储在 srr_json.clarify_answers），
          会透传给 orchestrator.run_pipeline，在 _clarify_node 首轮应用到 SRR。
        - 错误处理：orchestrator 内部异常会 yield 一个 error 事件，SSE 流正常结束。
        - v1.4：创建 AnalysisTask 记录，使生成任务在「任务管理」页面可见。
          SSE 流式机制不变，仅追加任务状态追踪。
    """
    # 从 DB 查询需求，取出 raw_text 和澄清答案。
    # v4.1.9：查询、404、可见性校验必须同处一个 session 作用域——原实现在
    # `async with` 块外调用 assert_product_visible(session, ...)，用的是已 close
    # 的 session，依赖「close 后可隐式重开」这一脆弱行为；块外继续读 ORM 属性
    # 同样不可靠（expire_on_commit 配置一变就会触发 DetachedInstanceError）。
    async with async_session() as session:
        stmt = select(Requirement).where(Requirement.id == req.req_id)
        result = await session.execute(stmt)
        requirement = result.scalar_one_or_none()

        if requirement is None:
            logger.warning("[API/generate] 需求不存在: req_id={}", req.req_id)
            raise HTTPException(status_code=404, detail=f"需求 {req.req_id} 不存在")

        # v3.1.3 P1-1 产品隔离：需求所属产品对当前用户不可见则拒绝生成
        req_product_id = requirement.product_id
        await assert_product_visible(session, user, req_product_id)

        # 块内取完所需标量，后续只用这些快照
        raw_text = requirement.raw_text or ""
        srr_json = requirement.srr_json or {}
        req_status = requirement.status

    # v1.6：仅 confirmed（确认完成）状态的需求可生成测试用例。
    # 用户需先确认 SRR 内容、功能点、影响分析无误后，通过 POST /{req_id}/confirm 确认，
    # 状态变为 confirmed 后方可生成。
    # v3.5.3：archived 需求允许 resume 续跑（归档后重新生成走 resume 语义，
    # 不破坏归档状态；全新生成仍要求 confirmed）。
    if req_status != "confirmed" and not (req.resume and req.resume_from_task):
        logger.warning(
            "[API/generate] 需求状态非 confirmed，拒绝生成: req_id={}, status={}, resume={}",
            req.req_id, req_status, req.resume,
        )
        raise HTTPException(
            status_code=409,
            detail=f"需求状态为 {req_status}，仅 confirmed（确认完成）状态可生成测试用例。"
                   f"请先确认需求分析结果（SRR/功能点/影响分析）后再试。"
                   f"（archived 需求可用 resume 续跑）",
        )

    # v3.1.3 修复 P2：confirmed 后重跑影响分析会写入新的未确认候选条目，
    # 但生成入口此前只校验 status，导致这些新候选被 prompt 端静默排除、
    # 回归覆盖无声缩水且无任何提示。此处与 confirm_requirement 守卫对齐：
    # 存在未确认影响条目时拒绝生成，强制用户先完成影响确认。
    impact_items = srr_json.get("impact_analysis") or []
    unconfirmed_impact = [
        it for it in impact_items
        if isinstance(it, dict) and not it.get("confirmed_at")
    ]
    if unconfirmed_impact:
        logger.warning(
            "[API/generate] 存在未确认影响分析条目，拒绝生成: req_id={}, count={}",
            req.req_id, len(unconfirmed_impact),
        )
        raise HTTPException(
            status_code=409,
            detail=f"存在 {len(unconfirmed_impact)} 个未确认的影响分析条目，"
                   f"请先到影响分析页完成确认后再生成测试用例。",
        )

    # 生成用例必须使用已确认的 SRR（需求分析/澄清后形成的最终结构化需求），
    # 不允许回退到原始文本，防止 SRR 简化导致通道选择错误和用例质量下降。
    has_srr = bool(srr_json.get("requirement"))
    if not has_srr:
        logger.error(
            "[API/generate] 需求 SRR 为空，无法生成: req_id={}, status={}",
            req.req_id, req_status,
        )
        raise HTTPException(
            status_code=400,
            detail="需求 SRR 为空，无法生成测试用例。请先完成需求分析流程。",
        )

    # 从 srr_json 提取澄清答案（由 /requirements/{id}/clarify 端点存入）
    clarify_answers = srr_json.get("clarify_answers") or None

    # v3.1.3 断点续跑：resume=True 时从最近一次中断/作废任务 checkpoint 继续
    _resume_meta: dict | None = None
    if req.resume:
        _resume_meta = await _find_resume_checkpoint(req.resume_from_task, req.resume_from_task) if req.resume_from_task else await _find_resume_checkpoint(req.req_id)
        if not _resume_meta:
            logger.warning(
                "[API/generate] 续跑请求但无可用 checkpoint，将从头生成: req_id={}, resume_from={}",
                req.req_id, req.resume_from_task,
            )
        else:
            # v3.4（docs/15 §4.2 第 3/4 条）：续跑前校验**输入指纹**。
            # 旧 checkpoint 只记"哪些 FP 完成了"，不记它对应哪份输入 ——
            # 改完需求再续跑，会把旧产物拼到新需求上。现在逐字段比对：
            # 需求修订 / SRR 内容 / FP 集合 / prompt 版本 / 模型 / 合同版本，
            # 任一不同 → 409；只有显式 fallback_to_restart=true 才放弃旧断点从头跑。
            from app.core.pipeline_fingerprint import (
                compare_fingerprints,
                load_current_fingerprint,
                mismatch_message,
            )

            async with async_session() as fp_session:
                current_fp = await load_current_fingerprint(fp_session, requirement, srr_json)
            stored_fp = (_resume_meta.get("checkpoint") or {}).get("fingerprint")
            ok, diffs = compare_fingerprints(stored_fp, current_fp)
            if not ok and not req.fallback_to_restart:
                detail = mismatch_message(diffs)
                logger.warning(
                    "[API/generate] 续跑被输入指纹拦截: req_id={}, task={}, diffs={}",
                    req.req_id, _resume_meta.get("source_task_id"),
                    [(d["field"], d["stored"], d["current"]) for d in diffs],
                )
                raise HTTPException(status_code=409, detail=detail)
            if not ok:
                logger.warning(
                    "[API/generate] 输入指纹不一致但已显式 fallback_to_restart，"
                    "放弃旧断点从头生成: req_id={}, diffs={}",
                    req.req_id, [d["field"] for d in diffs],
                )
                _resume_meta = None

    # v3.1.3 P1-2 并发互斥 → v3.4（docs/15 §4.3）改为**原子占用**：
    # 原先"session A 查活动任务 → session B 插任务"存在 TOCTOU（两个并发请求可能都查到
    # "没有活动任务"），且 `resume=True` 会绕过互斥。现在由 core/task_guard 在**同一事务**内
    # 完成 advisory lock → 僵尸回收 → 活动检查 → 插入，并有 partial unique index 兜底。
    # 并发生成会导致 SRR/用例双写冲突与任务状态互相覆盖。
    task_id = gen_task_id()
    from app.core.task_guard import reserve_generation_slot

    slot = await reserve_generation_slot(
        req_id=req.req_id,
        task_id=task_id,
        product_id=req_product_id,
        created_by="api",
    )
    if slot["status"] == "conflict":
        logger.warning(
            "[API/generate] 并发互斥触发，拒绝重复生成: req_id={}, active_task={}, resume={}",
            req.req_id, slot.get("active_task_id"), req.resume,
        )
        raise HTTPException(
            status_code=409,
            detail=f"该需求已有正在执行的任务（{slot.get('active_task_id') or '未知'}），"
                   f"请等待其完成，或到任务管理页取消后再发起生成。"
                   f"（断点续跑同样受互斥约束）",
        )


    logger.info(
        "[API/generate] 启动 SSE 流式生成: req_id={}, task_id={}, "
        "has_clarify_answers={}, has_srr={}, raw_text_len={}",
        req.req_id,
        task_id,
        bool(clarify_answers),
        has_srr,
        len(raw_text),
    )

    async def _update_gen_task(**fields) -> None:
        """更新生成任务状态（静默失败，不阻断 SSE 流）。

        v3.5.3：失败日志升 WARNING 并打印完整异常——取消路径的收尸 task
        若静默失败（原 DEBUG 级 + 空 message），缺陷会伪装成"收尸从未执行"。
        """
        try:
            from sqlalchemy import update as sa_update
            async with async_session() as s:
                stmt = sa_update(AnalysisTask).where(
                    AnalysisTask.id == task_id,
                    AnalysisTask.status.in_(["pending", "running"]),
                ).values(**fields)
                await s.execute(stmt)
                await s.commit()
        except asyncio.CancelledError:
            logger.warning("[API/generate] 收尸 task 被取消（进程关闭中，交给看门狗兜底）: task_id={}", task_id)
            raise
        except Exception as e:
            logger.warning("[API/generate] 更新任务状态失败: task_id={}, err={}", task_id, repr(e)[:200])

    # Request-local state shared with aclose; do not reference a nested local.
    aborted = {"flag": False}

    async def _shielded_reaper(tid: str, progress: int) -> None:
        """v3.5.3.1：shield 包裹的收尸任务。

        - shield：即使创建它的请求 scope 被整体取消（应用关闭），收尸仍能跑完；
        - 显式 status 守卫（IN pending,running）：任务已终态时 UPDATE 匹配 0 行，幂等。
        """
        try:
            await asyncio.shield(_update_gen_task(
                status="aborted",
                phase="generate",
                progress=max(0, progress),
                message="生成已被中断（连接断开，可用断点续跑恢复）",
                error="SSE cancelled: client disconnected or server shutdown",
                completed_at=datetime.now(timezone.utc),
            ))
            logger.warning("[API/generate] ✅ SSE 断连收尸完成: task_id={}", tid)
        except asyncio.CancelledError:
            logger.error("[API/generate] ⚠️ 收尸 task 也被取消（应用正在关闭，交给重启清扫兜底）: task_id={}", tid)
        except Exception as e:  # noqa: BLE001
            logger.error("[API/generate] ⚠️ 收尸 task 异常: task_id={}, err={}", tid, repr(e)[:200])

    # SSE 事件生成器：包装 orchestrator.run_pipeline 的 AsyncGenerator
    async def event_generator():
        """SSE 事件生成器。

        将 orchestrator 产出的 dict 事件序列化为 SSE 协议格式：
            event: message
            data: <json_string>

        修复 H6（SSE 断连 task=running 幽灵任务）：
        - 通过 request.is_disconnected() 轮询 + aclose 钩子检测客户端断连；
        - 一旦发现断连，立即把 AnalysisTask 写为 status=aborted 并退出循环，
          避免任务永远停留在 running，导致前端无法再重试。
        """
        async def _mark_aborted(reason: str = "client_disconnected") -> None:
            if aborted["flag"]:
                return
            aborted["flag"] = True
            logger.warning("[API/generate] SSE 流中断: task_id={} reason={}", task_id, reason)
            await _update_gen_task(
                status="aborted",
                phase="generate",
                progress=max(0, getattr(event_generator, "_last_progress", 0)),
                message=f"生成已被用户中断（{reason}）",
                error=f"SSE 流中断: {reason}",
                completed_at=datetime.now(timezone.utc),
            )

        # 安装断开钩子：sse_starlette 在客户端关闭时会调用 response.aclose()，
        # 我们通过 finally 兜底；这里也先注入一个 is_disconnected 轮询。
        last_disconnect_check = 0.0

        # v4.0.2：全局资源准入门。
        # 系统是平台，多人可能同时点生成。`reserve_generation_slot` 只防"同一需求
        # 双跑"，orchestrator 的 `_gen_sem` 只限"单任务内 FP 并发"，都不看服务器
        # 还能吃多少 → N 个用户同时生成 = N×FP并发 个本地模型推理同时开跑，
        # 实测把事件循环拖到秒级。这里按 CPU 核数定限额，资源不足就**排队等待
        # 释放**（用户明确要求的行为），并把排队状态通过 SSE 告诉前端。
        _gate_admitted = False
        _acq_task: asyncio.Task | None = None
        _rg = None
        _slot: dict = {}

        def _client_gone() -> bool:
            """排队期间检测客户端是否已离开（避免用户关了页面还白等一个槽位）。"""
            try:
                return bool(request.is_disconnected())
            except Exception:  # noqa: BLE001
                return False

        # 说明：资源准入与流水线共用下面这一个 try/finally。
        # 不能把准入放在 finally 之外——那样「排队超时提前 return」会绕过槽位
        # 归还逻辑；同理也不能让 CancelledError 从准入段裸抛出去。
        try:
            try:
                from app.core.resource_guard import resource_guard as _rg
                # 快路径：有空位立即进（单用户场景零开销，不建任务不排队）
                _gate_admitted = _rg.gate.try_acquire(task_id)
            except Exception as e:  # noqa: BLE001 - 守卫故障不得阻断生成（fail-open）
                logger.warning(
                    "[API/generate] 资源准入门异常（按无限制放行）: task_id={}, err={}",
                    task_id, repr(e)[:150],
                )
                _gate_admitted = True

            if not _gate_admitted:
                # 需要排队：先告知前端，再边等边推心跳（否则用户对着一个
                # 完全不动的页面，根本不知道是在排队还是卡死了）
                _q_started = time.monotonic()

                def _queued_event(elapsed_s: int, position: int) -> dict:
                    return {
                        "event": "message",
                        "data": json.dumps({
                            "stage": "queued",
                            "message": (
                                f"系统资源紧张，已进入排队（第 {position} 位，已等待 {elapsed_s} 秒）"
                                f"——正在等待其它生成任务释放资源，请勿关闭页面"
                            ),
                            "progress": 0,
                            "req_id": req.req_id,
                            "queue_position": position,
                            "queue_elapsed_s": elapsed_s,
                            "ceiling": _rg.task_ceiling(),
                        }, ensure_ascii=False),
                    }

                try:
                    _pos = _rg.gate.waiting_count + 1
                    _ceil = _rg.task_ceiling()
                    logger.info(
                        "[API/generate] 生成任务进入资源排队: task_id={}, 排队位={}, 并发上限={}",
                        task_id, _pos, _ceil,
                    )
                    yield _queued_event(0, _pos)
                    _acq_task = asyncio.create_task(
                        _rg.gate.acquire(task_id, abort_check=_client_gone),
                        name=f"resource-gate-{task_id}",
                    )
                    while True:
                        _done, _ = await asyncio.wait({_acq_task}, timeout=5.0)
                        if _acq_task in _done:
                            _slot = _acq_task.result()
                            break
                        yield _queued_event(
                            int(time.monotonic() - _q_started), _rg.gate.waiting_count + 1,
                        )
                except asyncio.CancelledError:
                    # 排队期间客户端断连/服务关闭：取消排队任务；若它恰好已拿到
                    # 槽位，由下面 finally 的孤儿兜底归还。
                    if _acq_task is not None:
                        _acq_task.cancel()
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "[API/generate] 资源排队异常（按无限制放行）: task_id={}, err={}",
                        task_id, repr(e)[:150],
                    )
                    _slot = {"admitted": True, "reason": "guard_error", "waited_ms": 0.0}
                    _gate_admitted = True

                if not _gate_admitted and not _slot.get("admitted"):
                    _reason = str(_slot.get("reason") or "")
                    _detail = {
                        "wait_timeout": (
                            f"系统资源紧张，排队 {int(_slot.get('waited_ms', 0) / 1000)} 秒"
                            f"仍未获得资源，请稍后重试（可先取消其它进行中的生成任务）"
                        ),
                        "queue_full": (
                            f"当前排队任务过多（第 {int(_slot.get('queue_position', 0))} 位），"
                            f"请稍后重试或先取消其它进行中的生成任务"
                        ),
                        "client_gone": "客户端已断开，取消排队",
                    }.get(_reason, f"未能获得生成资源（{_reason or 'unknown'}）")
                    logger.warning(
                        "[API/generate] 资源准入未通过: task_id={}, reason={}, waited_ms={}",
                        task_id, _reason, _slot.get("waited_ms"),
                    )
                    await _update_gen_task(
                        status="failed", phase="generate",
                        error=f"resource_gate: {_reason}",
                        message=_detail,
                        completed_at=datetime.now(timezone.utc),
                    )
                    yield {
                        "event": "message",
                        "data": json.dumps({
                            "stage": "error",
                            "message": _detail,
                            "progress": 100,
                            "req_id": req.req_id,
                            "error_kind": "resource_unavailable",
                        }, ensure_ascii=False),
                    }
                    return
                _gate_admitted = True
                if _slot.get("waited_ms"):
                    logger.info(
                        "[API/generate] 排队后获得生成资源: task_id={}, waited_ms={}, ceiling={}",
                        task_id, _slot.get("waited_ms"), _slot.get("ceiling"),
                    )

            async for event in orchestrator.run_pipeline(
                raw_requirement=raw_text,
                srr_json=srr_json if has_srr else None,
                product_id=req_product_id,
                product_name=srr_json.get("requirement", {}).get("product_name", ""),
                clarify_answers=clarify_answers,
                existing_req_id=req.req_id,
                task_id=task_id,
                resume=req.resume,
                resume_checkpoint=(_resume_meta or {}).get("checkpoint"),
            ):
                # H6: 检查客户端是否已断开（每 5 秒至少检查一次；避免 event 间隔过长时检测延迟）
                now_ts = getattr(event_generator, "_clock", 0) or 0  # noqa: F841
                if hasattr(event_generator, "_last_progress"):
                    event_generator._last_progress = event.get("progress", event_generator._last_progress)
                else:
                    event_generator._last_progress = event.get("progress", 0)

                if await request.is_disconnected():
                    await _mark_aborted("client_closed_tab_or_stop")
                    break
                # Commit terminal quality evidence before sending done. A client
                # may close immediately on done; persistence must not live after it.
                if event.get("stage") == "done":
                    from app.core.publication_policy import quality_outcome
                    event.update(quality_outcome(event))
                # 同步更新任务进度（基于 SSE stage）
                stage = event.get("stage", "")
                progress = event.get("progress", 0)
                if stage == "done":
                    # v2.0：保存完整统计信息到 result_json，供下次生成时一致性检查使用
                    # 字段对齐 orchestrator._gen_node 读取：case_count / type_dist / case_titles
                    done_cases = event.get("cases") or []
                    type_dist: dict[str, int] = {}
                    case_titles: list[str] = []
                    for c in done_cases:
                        t = c.get("type", "unknown")
                        type_dist[t] = type_dist.get(t, 0) + 1
                        title = c.get("title", "")
                        if title:
                            case_titles.append(title)
                    # v3.1.3: 显式统计回归复用用例数，供 R2 质量验证直接读取
                    reused_count = sum(1 for c in done_cases if c.get("source") == "reused")
                    regression_count = type_dist.get("regression", 0)
                    # v3.1.3: 从回归复用摘要中读取编排器装配的复用计数
                    reuse_summary = event.get("regression_reuse_summary", {}) or {}
                    await _update_gen_task(
                        status="failed" if event.get("completion_status") == "failed" else "completed",
                        phase="done", progress=100,
                        message="生成结果待人工审核" if event.get("manual_action_required") else "用例生成完成",
                        completed_at=datetime.now(timezone.utc),
                        result_json={
                            "case_count": len(done_cases),
                            "type_dist": type_dist,
                            # v2.49：展示口径分组统计（boundary/exception/exploratory ⊂ functional）。
                            # 任务列表按此展示，避免"边界测试"独立成类；type_dist 保留细分供技术矩阵。
                            "type_group_dist": summarize_by_group(done_cases),
                            "case_titles": case_titles,
                            # 同时保存审核与一致性摘要，便于任务管理页展示
                            "review_passed": event.get("review_report", {}).get("passed_cases", 0),
                            "review_blocked": event.get("review_report", {}).get("blocked_cases", 0),
                            "avg_confidence": event.get("review_report", {}).get("avg_confidence", 0.0),
                            "consistency_inconsistent": (
                                event.get("consistency_check", {}) or {}
                            ).get("is_inconsistent", False),
                            "duration_ms": event.get("duration_ms", 0),
                            # 要点级覆盖映射（RTM 口径），供任务详情页与一致性检查使用
                            "point_coverage": event.get("point_coverage", {}),
                            # v3.1.3: 回归复用计数（显式字段，供 R2 验证）
                            "reused_count": reused_count,
                            "regression_count": regression_count,
                            "regression_reuse_summary": reuse_summary,
                            # Full quality decision survives SSE disconnection.
                            **quality_outcome(event),
                            "review_report": event.get("review_report", {}),
                            "gate_result": event.get("gate_result", {}),
                            "kb_flowback": event.get("kb_flowback", {}),
                            # v3.4（docs/15 §4.8）：降级与发布证据全量落 task，
                            # 离开 SSE 页面后任务管理页/复盘仍能看到完整决策链
                            "review_degraded": bool(
                                (event.get("review_report") or {}).get("review_degraded")
                            ),
                            "gate_forced": bool(
                                (event.get("gate_result") or {}).get("forced_pass")
                            ),
                            "dedup_removed": event.get("dedup_removed", []),
                            # v3.4（docs/15 §4.10）：节点级 span，trace 树的中间层
                            "node_spans": event.get("node_spans", []),
                            "kb_write_status": {
                                "skipped": bool(
                                    (event.get("kb_flowback") or {}).get("skipped")
                                ),
                                "auto_approved": (event.get("kb_flowback") or {}).get("auto_approved", 0),
                                "pending_review": (event.get("kb_flowback") or {}).get("pending_review", 0),
                                "errors": (event.get("kb_flowback") or {}).get("errors", 0),
                            },
                        },
                    )

                    # v3.1.3 评审报告中心 P1：SRR 门禁报告 + 用例审核报告落库。
                    # 失败不阻断主流程（报告中心仅作展示/后续采纳消费）。
                    try:
                        from app.core.review_reports import (
                            build_case_report,
                            build_srr_report,
                            save_review_reports,
                        )
                        _req_title = srr_json.get("requirement", {}).get("title", "")
                        _gen_batch = next(
                            (c.get("gen_batch_id", "") for c in done_cases if c.get("gen_batch_id")),
                            "",
                        )
                        _records = []
                        _gate_result = event.get("gate_result", {}) or {}
                        _review_report = event.get("review_report", {}) or {}
                        if _gate_result.get("passed") is not None or _gate_result.get("blockers"):
                            _records.append(build_srr_report(
                                task_id=task_id,
                                req_id=req.req_id,
                                product_id=req_product_id,
                                req_title=_req_title,
                                gate_result=_gate_result,
                                clarify_result=event.get("clarify_result", {}) or {},
                            ))
                        if _review_report.get("total_cases"):
                            _records.append(build_case_report(
                                task_id=task_id,
                                req_id=req.req_id,
                                product_id=req_product_id,
                                req_title=_req_title,
                                review_report=_review_report,
                                gen_batch_id=_gen_batch,
                                # v3.4（docs/15 P0-3）：终态与降级证据一并落库
                                completion_status=event.get("completion_status", ""),
                                degraded_flags=event.get("degraded_flags") or [],
                                failed_subtasks=event.get("failed_subtasks") or [],
                            ))
                        if _records:
                            await save_review_reports(_records)
                    except Exception as e:
                        logger.warning(
                            "[API/generate] 评审报告落库失败（不阻断）: {}", str(e)[:150],
                        )
                elif stage == "error":
                    await _update_gen_task(
                        status="failed", phase="generate",
                        error=event.get("message", "")[:500],
                        message="生成失败: " + event.get("message", "")[:200],
                        completed_at=datetime.now(timezone.utc),
                    )
                elif progress:
                    await _update_gen_task(progress=progress, message=event.get("message", "")[:200])
                yield {
                    "event": "message",
                    "data": json.dumps(event, ensure_ascii=False, default=str),
                }
        except asyncio.CancelledError:
            # v3.5.3 修复缺陷①（SSE 断连协程静默消失）：
            # 客户端断连 → sse-starlette task_group.cancel_scope.cancel() →
            # CancelledError 注入本生成器（except Exception 接不住，它是
            # BaseException）→ 流水线协程连同 LLM 流式调用一起被取消。
            # 此时本 scope 已被取消，任何 await 都会立刻再抛 Cancelled——
            # DB 收尸必须抛到取消 scope 之外执行（create_task 新任务）。
            # _update_gen_task 自带 status IN (pending, running) 守卫：
            # 任务已 completed/failed 时 UPDATE 匹配 0 行，天然幂等。
            logger.warning(
                "[API/generate] SSE 生成器被取消（客户端断连/服务关闭）: task_id={}",
                task_id,
            )
            _progress = getattr(event_generator, "_last_progress", 0)
            # v3.5.3.1：改用 ShieldedReaper——裸 create_task 在「整个应用关闭」
            # （uvicorn reload / 容器重启）时会被事件循环顺手取消，收尸再次失败
            # （09-30 09:06:53 实证）。shield + 独立 task 让收尸不受外层取消影响；
            # 进程真死时由重启清扫（database.py init_db）+ 看门狗双兜底。
            asyncio.get_running_loop().create_task(_shielded_reaper(
                task_id, _progress,
            ))
            raise  # 取消必须继续传播，不能吞掉
        except Exception as e:
            # 兜底：orchestrator 内部未捕获的异常（正常情况已被 run_pipeline 的
            # try/except 捕获并 yield error 事件，此处为防御性处理）
            logger.error("[API/generate] SSE 流异常: {}", str(e)[:200])
            await _update_gen_task(
                status="failed", error=str(e)[:500],
                message="生成流程异常: " + str(e)[:200],
                completed_at=datetime.now(timezone.utc),
            )
            error_event = {
                "stage": "error",
                "message": f"生成流程异常: {str(e)[:200]}",
                "progress": 100,
                "req_id": req.req_id,
            }
            yield {
                "event": "message",
                "data": json.dumps(error_event, ensure_ascii=False),
            }
        finally:
            # v4.0.2：归还资源槽位。**必须同步归还**——本 finally 在客户端断连时
            # 处于已取消作用域（uvicorn 取消 SSE 任务），任何 await 都会立刻再抛
            # CancelledError；用 async release 会导致槽位永久泄漏、后续所有生成
            # 任务无限排队。故 resource_guard.GenerationGate.release() 是同步方法。
            if _rg is not None and (_gate_admitted or _acq_task is not None):
                # 孤儿兜底：排队任务在生成器被关闭前后恰好拿到了槽位，也必须归还，
                # 否则那个槽位谁都不会再释放。
                if _acq_task is not None and not _acq_task.done():
                    _acq_task.cancel()
                if not _gate_admitted and _acq_task is not None \
                        and _acq_task.done() and not _acq_task.cancelled():
                    try:
                        if _acq_task.result().get("admitted"):
                            _gate_admitted = True
                    except Exception:  # noqa: BLE001
                        pass
                if _gate_admitted:
                    try:
                        _rg.gate.release(task_id)
                        logger.info("[API/generate] 已归还生成资源槽位: task_id={}", task_id)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "[API/generate] 归还资源槽位失败（槽位可能泄漏）: task_id={}, err={}",
                            task_id, repr(e)[:150],
                        )

            # v3.5.3：不再在 finally 里 await is_disconnected()——取消路径下
            # 该 await 会立即再抛 Cancelled，导致清理代码自身被掐断（原缺陷根因）。
            # 取消路径的收尸已由 except CancelledError 分支的 create_task 完成；
            # 正常完成路径 aborted["flag"] 为 False 且任务已是终态，
            # 这里只在「非取消、未标记、未发 done/error」的遗漏场景兜底一次。
            if not aborted["flag"]:
                try:
                    if await request.is_disconnected():
                        await _mark_aborted("finally_client_disconnected")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # 检查失败：收尸已由取消分支的 create_task 兜底
                    pass

    # 构建 EventSourceResponse，并把 generator 的 _mark_aborted 绑定到 response 的 aclose 钩子，
    # 覆盖 sse-starlette 在客户端 TCP 断连时没有"写任务状态"的缺口。
    response = EventSourceResponse(event_generator())

    _orig_aclose = getattr(response, "aclose", None)

    async def _patched_aclose() -> None:
        try:
            if not aborted["flag"]:
                try:
                    if await request.is_disconnected():
                        # 直接 DB 更新，不依赖 event_generator 是否还活着
                        from sqlalchemy import update as _sa_update
                        async with async_session() as s:
                            stmt = (
                                _sa_update(AnalysisTask)
                                .where(AnalysisTask.id == task_id, AnalysisTask.status == "running")
                                .values(
                                    status="aborted",
                                    phase="generate",
                                    message="生成已被中断（连接关闭）",
                                    error="aclose: client disconnected",
                                    completed_at=datetime.now(timezone.utc),
                                )
                            )
                            await s.execute(stmt)
                            await s.commit()
                except Exception:
                    pass
        finally:
            if _orig_aclose is not None:
                await _orig_aclose()

    response.aclose = _patched_aclose  # type: ignore[method-assign]
    return response
