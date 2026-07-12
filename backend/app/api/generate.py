"""SSE 流式生成路由（核心端点）。

POST /api/generate —— 调用 LangGraph 编排器，流式返回测试用例生成进度与结果。

设计依据：
- docs/00-architecture-overview.md §9.2 进度反馈机制（SSE）
- docs/00 §3 核心数据流（8 阶段编排）
- 实现计划 §T5.3 SSE 实现
- D4 决策：后端用 sse-starlette 的 EventSourceResponse，前端用 fetch + ReadableStream

SSE 事件格式（由 orchestrator.make_sse_event 产生）：
    {"stage": "parse|select_channel|clarify|gate|impact|retrieve|generate|review|done|error",
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

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from app.core.auth import require_write
from app.core.orchestrator import orchestrator
from app.db.database import async_session
from app.db.models import Requirement
from app.models.api_schemas import GenerateRequest
from app.utils.logger import logger


router = APIRouter()


@router.post("/generate", dependencies=[Depends(require_write)])
async def generate_test_cases(req: GenerateRequest):
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
    if not raw_text:
        logger.warning("[API/generate] 需求 raw_text 为空，无法生成: req_id={}", req.req_id)
        raise HTTPException(status_code=400, detail="需求原始文本为空，无法生成测试用例")

    # 从 srr_json 提取澄清答案（由 /requirements/{id}/clarify 端点存入）
    srr_json = requirement.srr_json or {}
    clarify_answers = srr_json.get("clarify_answers") or None

    logger.info(
        "[API/generate] 启动 SSE 流式生成: req_id={}, expected_channel={}, "
        "has_clarify_answers={}, raw_text_len={}",
        req.req_id,
        req.channel,
        bool(clarify_answers),
        len(raw_text),
    )

    # SSE 事件生成器：包装 orchestrator.run_pipeline 的 AsyncGenerator
    async def event_generator():
        """SSE 事件生成器。

        将 orchestrator 产出的 dict 事件序列化为 SSE 协议格式：
            event: message
            data: <json_string>
        """
        try:
            async for event in orchestrator.run_pipeline(
                raw_requirement=raw_text,
                product_id=requirement.product_id,
                product_name=srr_json.get("requirement", {}).get("product_name", ""),
                clarify_answers=clarify_answers,
                existing_req_id=req.req_id,
            ):
                # orchestrator 产出的事件为 dict，序列化为 JSON 字符串作为 SSE data
                yield {
                    "event": "message",
                    "data": json.dumps(event, ensure_ascii=False, default=str),
                }
        except Exception as e:
            # 兜底：orchestrator 内部未捕获的异常（正常情况已被 run_pipeline 的
            # try/except 捕获并 yield error 事件，此处为防御性处理）
            logger.error("[API/generate] SSE 流异常: {}", str(e)[:200])
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

    return EventSourceResponse(event_generator())
