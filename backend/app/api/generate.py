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

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from app.core.auth import require_write, get_current_user, CurrentUser
from app.core.orchestrator import orchestrator
from app.core.product_access import assert_product_visible
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
            task = result.scalars().first() if from_task else result.scalars().first()
            if task is None or not isinstance(task.result_json, dict):
                return None
            cp = task.result_json.get("checkpoint")
            if not isinstance(cp, dict) or not cp.get("fp_groups"):
                return None
            return {"checkpoint": cp, "source_task_id": task.id}
    except Exception as e:
        logger.warning("[API/generate] 续跑 checkpoint 查找失败: {}", str(e)[:120])
        return None


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
        - 通道选择：当前由 orchestrator 内部 select_channel 自动决定。
          req.channel 作为"期望通道"接收并记录日志，实际通道以响应中 done 事件的
          channel 字段为准。强制覆盖通道的能力留待后续优化。
        - 澄清答案：若需求已提交澄清答案（存储在 srr_json.clarify_answers），
          会透传给 orchestrator.run_pipeline，在 _clarify_node 首轮应用到 SRR。
        - 错误处理：orchestrator 内部异常会 yield 一个 error 事件，SSE 流正常结束。
        - v1.4：创建 AnalysisTask 记录，使生成任务在「任务管理」页面可见。
          SSE 流式机制不变，仅追加任务状态追踪。
    """
    # 从 DB 查询需求，取出 raw_text 和澄清答案
    async with async_session() as session:
        stmt = select(Requirement).where(Requirement.id == req.req_id)
        result = await session.execute(stmt)
        requirement = result.scalar_one_or_none()

    if requirement is None:
        logger.warning("[API/generate] 需求不存在: req_id={}", req.req_id)
        raise HTTPException(status_code=404, detail=f"需求 {req.req_id} 不存在")

    # v3.1.3 P1-1 产品隔离：需求所属产品对当前用户不可见则拒绝生成
    await assert_product_visible(session, user, requirement.product_id)

    raw_text = requirement.raw_text or ""
    srr_json = requirement.srr_json or {}

    # v1.6：仅 confirmed（确认完成）状态的需求可生成测试用例。
    # 用户需先确认 SRR 内容、功能点、影响分析无误后，通过 POST /{req_id}/confirm 确认，
    # 状态变为 confirmed 后方可生成。
    if requirement.status != "confirmed":
        logger.warning(
            "[API/generate] 需求状态非 confirmed，拒绝生成: req_id={}, status={}",
            req.req_id, requirement.status,
        )
        raise HTTPException(
            status_code=409,
            detail=f"需求状态为 {requirement.status}，仅 confirmed（确认完成）状态可生成测试用例。"
                   f"请先确认需求分析结果（SRR/功能点/影响分析）后再试。",
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
            req.req_id, requirement.status,
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
        product_id=requirement.product_id,
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
        "[API/generate] 启动 SSE 流式生成: req_id={}, task_id={}, expected_channel={}, "
        "has_clarify_answers={}, has_srr={}, raw_text_len={}",
        req.req_id,
        task_id,
        req.channel,
        bool(clarify_answers),
        has_srr,
        len(raw_text),
    )

    async def _update_gen_task(**fields) -> None:
        """更新生成任务状态（静默失败，不阻断 SSE 流）。"""
        try:
            from sqlalchemy import update as sa_update
            async with async_session() as s:
                stmt = sa_update(AnalysisTask).where(
                    AnalysisTask.id == task_id,
                    AnalysisTask.status.in_(["pending", "running"]),
                ).values(**fields)
                await s.execute(stmt)
                await s.commit()
        except Exception as e:
            logger.debug("[API/generate] 更新任务状态失败: {}", str(e)[:120])

    # Request-local state shared with aclose; do not reference a nested local.
    aborted = {"flag": False}
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

        try:
            async for event in orchestrator.run_pipeline(
                raw_requirement=raw_text,
                srr_json=srr_json if has_srr else None,
                product_id=requirement.product_id,
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
                                product_id=requirement.product_id,
                                req_title=_req_title,
                                gate_result=_gate_result,
                                clarify_result=event.get("clarify_result", {}) or {},
                            ))
                        if _review_report.get("total_cases"):
                            _records.append(build_case_report(
                                task_id=task_id,
                                req_id=req.req_id,
                                product_id=requirement.product_id,
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
            # 修复 H6：generator 正常/异常/被 aclose 三种终止路径统一进入 finally
            # 只要还没完成且未失败/中止，客户端断连则写 aborted；
            # 若任务 status 仍为 running（极端情况），也写入 unknown_abort，避免永久 running 幽灵。
            if not aborted["flag"]:
                try:
                    # 无论如何尝试再检查一次连接；已 completed/failed 的任务状态不会变
                    if await request.is_disconnected():
                        await _mark_aborted("finally_client_disconnected")
                except Exception:
                    # 检查失败：用 DB 行状态兜底
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
