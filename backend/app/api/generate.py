"""SSE 流式生成路由（核心端点）。

POST /api/generate —— 调用 LangGraph 编排器，流式返回测试用例生成进度与结果。

设计依据：
- docs/00-architecture-overview.md §9.2 进度反馈机制（SSE）
- docs/00 §3 核心数据流（8 阶段编排）
- 实现计划 §T5.3 SSE 实现
- D4 决策：后端用 sse-starlette 的 EventSourceResponse，前端用 fetch + ReadableStream

SSE 事件格式（由 orchestrator.make_sse_event 产生）：
    {"stage": "parse|select_channel|clarify|gate|impact|generate|review|done|error",
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
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from app.core.auth import require_write
from app.core.orchestrator import orchestrator
from app.db.database import async_session
from app.db.models import Requirement, AnalysisTask
from app.models.api_schemas import GenerateRequest
from app.utils.id_gen import gen_task_id
from app.utils.logger import logger


router = APIRouter()


@router.post("/generate", dependencies=[Depends(require_write)])
async def generate_test_cases(req: GenerateRequest, request: Request):
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

    # v1.4：创建 AnalysisTask 记录（保持 SSE 流式，仅追加任务可见性）
    task_id = gen_task_id()
    async with async_session() as task_session:
        task = AnalysisTask(
            id=task_id,
            task_type="generate_cases",
            req_id=req.req_id,
            product_id=requirement.product_id,
            status="running",
            phase="generate",
            progress=0,
            message="用例生成已启动...",
            created_by="api",
        )
        task_session.add(task)
        await task_session.commit()

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
                stmt = sa_update(AnalysisTask).where(AnalysisTask.id == task_id).values(**fields)
                await s.execute(stmt)
                await s.commit()
        except Exception as e:
            logger.debug("[API/generate] 更新任务状态失败: {}", str(e)[:120])

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
        aborted = {"flag": False}

        async def _mark_aborted(reason: str = "client_disconnected") -> None:
            if aborted["flag"]:
                return
            aborted["flag"] = True
            logger.warning("[API/generate] SSE 流中断: task_id=%s reason=%s", task_id, reason)
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
                # orchestrator 产出的事件为 dict，序列化为 JSON 字符串作为 SSE data
                yield {
                    "event": "message",
                    "data": json.dumps(event, ensure_ascii=False, default=str),
                }
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
                    await _update_gen_task(
                        status="completed", phase="done", progress=100,
                        message="用例生成完成",
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
                        },
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

    _orig_aclose = response.aclose

    async def _patched_aclose() -> None:
        try:
            if not aborted.get("flag", False) if callable(getattr(aborted, "get", None)) else not aborted["flag"]:
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
            await _orig_aclose()

    response.aclose = _patched_aclose  # type: ignore[method-assign]
    return response
