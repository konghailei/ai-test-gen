"""知识问答路由。

提供知识库问答（流式 SSE）、人工纠错、纠错内容归档等功能。

端点清单：
    POST /api/knowledge-qa/ask              流式问答，检索知识库+LLM 流式分析
    POST /api/knowledge-qa/chat             追问对话
    POST /api/knowledge-qa/correct           人工纠错
    POST /api/knowledge-qa/archive           纠错内容归档到知识库
    GET  /api/knowledge-qa/history           问答历史
    GET  /api/knowledge-qa/history/{id}      问答详情
    DELETE /api/knowledge-qa/history/{id}    删除问答记录
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse
from sqlalchemy import func, select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_write
from app.core.llm_client import llm_client
from app.core import prompt_manager
from app.db.database import get_session
from app.db.models import KnowledgeQA, Requirement, TestCase, Product
from app.db.milvus_store import vector_store
from app.models.api_schemas import (
    ApiResponse,
    KbQAAskRequest,
    KbQACharRequest,
    KbQACorrectRequest,
    KbQAArchiveRequest,
    KbQARecord,
)
from app.utils.id_gen import gen_requirement_id, gen_test_case_id
from app.utils.logger import logger

router = APIRouter()

# ============================================================================
# 知识问答 Prompt 模板 — 通过 prompt_manager 统一管理，支持管理后台编辑
# ============================================================================
# QA_SYSTEM_PROMPT 定义已移至 app/prompts/knowledge_qa.py，
# 此处通过 prompt_manager.get("knowledge_qa", "QA_SYSTEM_PROMPT") 获取。


def _build_qa_prompt(question: str, kb_results: list[dict], enriched_sources: list[dict]) -> str:
    """构建问答 Prompt。

    Args:
        question: 用户问题
        kb_results: 向量检索原始结果（含完整文档）
        enriched_sources: 经过 PG 过滤的来源列表（已在 _enrich_sources 中过滤孤儿记录）

    Returns:
        str: 组装后的 Prompt
    """
    # 用 enriched_sources 的 id 集合过滤 kb_results，去除孤儿向量
    valid_ids = {s["id"] for s in enriched_sources}
    # 同时建立 id -> title 映射（enriched_sources 中有 PG 查询到的真实标题）
    id_to_title = {s["id"]: s["title"] for s in enriched_sources}
    valid_results = [r for r in kb_results if r.get("id") in valid_ids]

    context_parts = []
    if valid_results:
        for i, r in enumerate(valid_results, 1):
            source_type = r.get("type", "unknown")
            source_id = r.get("id", "未知")
            # 优先使用 enriched_sources 中的真实标题（产品名·需求名），其次用 metadata 中的 title
            title = id_to_title.get(source_id) or r.get("metadata", {}).get("title") or source_id
            doc = r.get("document", "")
            meta = r.get("metadata", {}) or {}
            go_live = meta.get("go_live_date", "")
            proj_ver = meta.get("project_version", "")
            ver_info = f" | 版本: v{proj_ver}" if proj_ver else ""
            date_info = f" | 上线日期: {go_live}" if go_live else ""
            context_parts.append(
                f"[来源 {i}] ID: {source_id} | 类型: {source_type} | 标题: {title}{ver_info}{date_info}\n"
                f"内容: {doc[:800]}\n"
            )
        context_text = "\n---\n".join(context_parts)
    else:
        context_text = "（知识库中未找到相关参考信息）"

    prompt = (
        f"## 知识库参考上下文\n\n"
        f"{context_text}\n\n"
        f"---\n\n"
        f"## 用户问题\n\n"
        f"{question}\n\n"
        f"---\n\n"
        f"请基于以上知识库上下文，回答用户的问题。"
        f"如果同一功能检索到多个版本的需求，请以上线日期靠后的最新版本为准，"
        f"并在回答中注明参考的版本和上线日期。"
        f"引用来源时请使用「来源类型: 来源标题」格式。"
        f"绝对不要编造任何不存在的 ID、编号或需求号。"
        f"如果上下文不足以完整回答，请基于已有信息给出最佳推断，并说明信息局限性。"
    )
    return prompt


def _record_to_dict(record: KnowledgeQA) -> dict:
    """将 ORM 记录转为字典。"""
    return {
        "id": record.id,
        "question": record.question,
        "answer": record.answer,
        "corrected_answer": record.corrected_answer,
        "correction_notes": record.correction_notes,
        "archived": record.archived,
        "archived_type": record.archived_type,
        "archived_id": record.archived_id,
        "reference_sources": record.reference_sources,
        "conversation_history": record.conversation_history,
        "created_at": record.created_at.isoformat() if record.created_at else "",
        "updated_at": record.updated_at.isoformat() if record.updated_at else "",
    }


async def _enrich_sources(
    kb_sources: list[dict], session: AsyncSession
) -> list[dict]:
    """丰富参考来源信息：从 PG 查询产品名称和需求标题。

    Args:
        kb_sources: 向量检索结果
        session: 数据库会话

    Returns:
        list[dict]: 丰富后的来源列表
    """
    # 收集所有需要查询的 ID
    req_ids = [s["id"] for s in kb_sources if s.get("type") == "requirement"]
    case_ids = [s["id"] for s in kb_sources if s.get("type") == "test_case"]

    # 查询需求详情（获取 title + product_id）
    req_map: dict[str, dict] = {}
    if req_ids:
        stmt = select(Requirement.id, Requirement.title, Requirement.product_id).where(
            Requirement.id.in_(req_ids)
        )
        result = await session.execute(stmt)
        for row in result.all():
            req_map[row[0]] = {"title": row[1], "product_id": row[2]}

    # 查询测试用例详情
    case_map: dict[str, dict] = {}
    if case_ids:
        stmt = select(TestCase.id, TestCase.title).where(TestCase.id.in_(case_ids))
        result = await session.execute(stmt)
        for row in result.all():
            case_map[row[0]] = {"title": row[1]}

    # 查询产品名称
    product_ids = set()
    for info in req_map.values():
        if info.get("product_id"):
            product_ids.add(info["product_id"])
    product_names: dict[str, str] = {}
    if product_ids:
        stmt = select(Product.id, Product.name).where(Product.id.in_(product_ids))
        result = await session.execute(stmt)
        product_names = {row[0]: row[1] for row in result.all()}

    # 构建丰富后的来源列表（过滤掉 PG 中不存在的孤儿记录）
    enriched = []
    for src in kb_sources:
        src_type = src.get("type", "")
        src_id = src.get("id", "")
        title = ""
        product_name = ""
        found = False

        if src_type == "requirement" and src_id in req_map:
            info = req_map[src_id]
            title = info["title"]
            pname = product_names.get(info.get("product_id", ""), "")
            if pname:
                product_name = pname
            found = True

        elif src_type == "test_case" and src_id in case_map:
            title = case_map[src_id]["title"]
            found = True

        # 跳过未在 PG 中找到的孤儿向量记录
        if not found and src_type == "requirement":
            logger.warning("[API/knowledge-qa] 跳过孤儿向量: id={}, type={}", src_id, src_type)
            continue

        display_title = title or src.get("title") or src.get("name") or "(已删除)"
        if product_name:
            display_title = f"{product_name} · {display_title}"

        enriched.append({
            "id": src_id,
            "title": display_title,
            "type": src_type,
            "similarity": round(src.get("similarity", 0), 4),
            "summary": src.get("document", "")[:200] if src.get("document") else "",
        })

    return enriched


# ============================================================================
# 1. 流式知识问答（SSE）
# ============================================================================

async def _stream_qa_answer(
    question: str,
    kb_sources: list[dict],
    product_id: str | None,
    session: AsyncSession,
    kb_status: dict | None = None,
    kb_provider: str | None = None,
):
    """流式生成问答回答（SSE 事件流）。

    流程：检索 → 构建 Prompt → 流式 LLM 输出。

    Args:
        kb_status: 知识库检索状态诊断信息，含:
            - search_ok: bool  检索是否成功执行（Milvus 可用）
            - error: str       检索失败时的错误信息
            - total: int       检索到的来源数
    """
    # 按 go_live_date 降序 + 相似度降序排序（上线日期靠后的优先）
    kb_sources.sort(
        key=lambda x: (
            (x.get("metadata", {}) or {}).get("go_live_date", ""),
            x.get("similarity", 0),
        ),
        reverse=True,
    )
    kb_sources = kb_sources[:8]

    # 丰富来源信息（查询产品名+标题），同时过滤孤儿向量
    reference_sources = await _enrich_sources(kb_sources, session)

    # 发送来源信息作为第一条事件（保持数组格式，前端兼容）
    yield {
        "event": "sources",
        "data": json.dumps(reference_sources, ensure_ascii=False),
    }

    # 构建 Prompt（传入过滤后的 reference_sources 以避免孤儿向量被用作 LLM 上下文）
    prompt = _build_qa_prompt(question, kb_sources, reference_sources)

    # 降级策略（简化版）：KB 不可用时不向 Prompt 注入诊断信息，避免污染 LLM 输出。
    # KB 完全不可用的拦截在 kqa_ask 端点中提前返回（不进入此流式函数）。
    # kb_status 仅用于 done 事件返回（前端展示诊断信息）。

    full_answer = ""

    try:
        # 调用 LLM（非流式，一次性返回，以 chunk 事件发送）
        # 使用 KB 专属 LLM（若已设置），否则使用全局主 LLM
        answer = await llm_client.call(
            prompt=prompt,
            system_prompt=prompt_manager.get("knowledge_qa", "QA_SYSTEM_PROMPT"),
            temperature=0.3,
            max_tokens=4096,
            provider=kb_provider,
        )
        full_answer = answer
        yield {"event": "chunk", "data": answer}
    except Exception as e:
        logger.error("[API/knowledge-qa] LLM 调用失败: {}", str(e)[:200])
        error_msg = f"抱歉，AI 分析服务暂时不可用。错误：{str(e)[:100]}"
        full_answer = error_msg
        yield {"event": "chunk", "data": error_msg}

    # 最后发送完成事件（含记录 ID）
    import uuid
    record_id = f"KQA-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
    yield {
        "event": "done",
        "data": json.dumps({
            "id": record_id,
            "question": question,
            "answer_full": full_answer,
            "reference_sources": reference_sources,
            "kb_status": kb_status or {"search_ok": True, "error": "", "total": len(reference_sources)},
        }, ensure_ascii=False),
    }

    # 异步保存记录（不阻塞 SSE 流）
    try:
        conversation_history = [
            {"role": "user", "content": question},
            {"role": "assistant", "content": full_answer},
        ]
        record = KnowledgeQA(
            id=record_id,
            question=question,
            answer=full_answer,
            reference_sources=reference_sources,
            conversation_history=conversation_history,
            created_by="system",
            # 产品隔离：记录归属的产品 ID（归档时用于关联到正确产品）
            product_id=product_id or "PROD-DEFAULT",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(record)
        await session.commit()
        logger.info("[API/knowledge-qa] 问答完成并保存: id={}, answer_len={}", record_id, len(full_answer))
    except Exception as e:
        logger.error("[API/knowledge-qa] 保存问答记录失败: {}", str(e)[:200])


@router.post("/ask", dependencies=[Depends(get_current_user)])
async def kqa_ask(
    req: KbQAAskRequest,
    session: AsyncSession = Depends(get_session),
):
    """流式知识问答（SSE 事件流）。

    流程：检索 → 构建 Prompt → LLM 流式输出 → 返回 SSE 事件。

    SSE 事件类型：
    - sources: 参考来源列表（JSON）
    - chunk: LLM 生成的文本片段
    - done: 完成，含 record_id + 完整回答
    """
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    product_id = req.product_id
    modules = req.modules

    logger.info(
        "[API/knowledge-qa] 流式问答: question={}, product_id={}, modules={}",
        question[:100], product_id, modules,
    )

    # 1. 检索知识库（带产品隔离 + 模块过滤 + 仅已归档需求）
    #    需求检索走 hybrid_searcher（向量 + BM25 + RRF + 重排）
    #    用例检索保持直接调用 vector_store（hybrid_searcher 暂未支持 test_cases）
    kb_sources: list[dict[str, Any]] = []
    search_errors: list[str] = []
    try:
        from app.db.hybrid_search import hybrid_searcher
        req_results = await hybrid_searcher.search_requirements(
            query=question, product_id=product_id, modules=modules, top_k=5,
            status="archived", session=session,
        )
        for r in req_results:
            r["type"] = "requirement"
            kb_sources.append(r)
    except Exception as e:
        err_msg = str(e)[:200]
        search_errors.append(f"需求检索: {err_msg}")
        logger.warning("[API/knowledge-qa] 检索需求失败: {}", err_msg[:100])

    try:
        case_results = await asyncio.to_thread(
            vector_store.search_similar_cases,
            question, top_k=5, product_id=product_id, modules=modules,
        )
        for r in case_results:
            r["type"] = "test_case"
            if not any(s.get("id") == r.get("id") and s.get("type") == "test_case" for s in kb_sources):
                kb_sources.append(r)
    except Exception as e:
        err_msg = str(e)[:200]
        search_errors.append(f"用例检索: {err_msg}")
        logger.warning("[API/knowledge-qa] 检索用例失败: {}", err_msg[:100])

    # 构建知识库检索状态诊断
    kb_status = {
        "search_ok": len(search_errors) == 0,
        "error": "; ".join(search_errors) if search_errors else "",
        "total": len(kb_sources),
    }

    logger.info(
        "[API/knowledge-qa] 检索完成: sources={}, search_ok={}, errors={}",
        len(kb_sources), kb_status["search_ok"], len(search_errors),
    )

    # 降级策略（简化版）：KB 完全不可用（两路检索都失败且无结果）→ 不调用 LLM，直接返回错误。
    # 避免空上下文幻觉；KB 可用但 0 结果时正常调用 LLM（LLM 会回答"未找到相关信息"）。
    if not kb_status["search_ok"] and kb_status["total"] == 0:
        logger.warning("[API/knowledge-qa] KB 完全不可用，跳过 LLM 调用: errors={}", search_errors)
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "message": "知识库检索暂时不可用，请稍后重试",
                "data": {"kb_status": kb_status},
            },
        )

    # 读取知识库专属 LLM 供应商（若未设置则使用全局主 LLM）
    from app.api.knowledge import get_kb_llm_provider
    kb_provider = await get_kb_llm_provider(session)

    return EventSourceResponse(_stream_qa_answer(
        question, kb_sources, product_id, session, kb_status,
        kb_provider=kb_provider,
    ))


# ============================================================================
# 1.5 追问对话（多轮对话）
# ============================================================================

@router.post("/chat", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def kqa_chat(
    req: KbQACharRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """追问对话（多轮对话）。

    基于已有问答记录继续对话，适合对 AI 回答进行总结、提炼或修改。

    Args:
        req: 追问请求（含 record_id + question）
        session: 数据库会话

    Returns:
        ApiResponse: data 含 answer / conversation_history
    """
    # 1. 查找原始记录
    result = await session.execute(select(KnowledgeQA).where(KnowledgeQA.id == req.record_id))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail=f"问答记录不存在: {req.record_id}")

    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="追问内容不能为空")

    logger.info("[API/knowledge-qa] 追问: {} -> {}", req.record_id, question[:100])

    # 2. 构建对话上下文 Prompt
    history = record.conversation_history or []
    history_parts = []
    for msg in history:
        role_label = "用户" if msg["role"] == "user" else "助手"
        history_parts.append(f"{role_label}: {msg['content']}")
    history_text = "\n\n".join(history_parts)

    chat_prompt = (
        f"## 对话历史\n\n{history_text}\n\n"
        f"---\n\n"
        f"## 用户最新消息\n\n{question}\n\n"
        f"---\n\n"
        f"请基于以上对话历史回答用户。"
        f"如果用户要求总结或提炼，请给出精简版本。"
        f"如果用户要求修改之前的回答，直接给出修改后的完整版本。"
    )

    # 3. 调用 LLM（使用 KB 专属 LLM + prompt_manager 管理的提示词）
    from app.api.knowledge import get_kb_llm_provider
    kb_provider = await get_kb_llm_provider(session)
    try:
        answer = await llm_client.call(
            prompt=chat_prompt,
            system_prompt=prompt_manager.get("knowledge_qa", "CHAT_SYSTEM_PROMPT"),
            temperature=0.3,
            max_tokens=4096,
            provider=kb_provider,
        )
    except Exception as e:
        logger.error("[API/knowledge-qa] 追问 LLM 调用失败: {}", str(e)[:200])
        answer = "抱歉，AI 分析服务暂时不可用，请稍后重试。错误：" + str(e)[:100]

    # 4. 更新对话历史
    try:
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer})
        record.conversation_history = history
        record.updated_at = datetime.now(timezone.utc)
        logger.info("[API/knowledge-qa] 追问完成: {} (共 {} 轮对话)", req.record_id, len(history) // 2)
    except Exception as e:
        logger.error("[API/knowledge-qa] 更新追问记录失败: {}", str(e)[:200])

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "record_id": req.record_id,
            "question": question,
            "answer": answer,
            "conversation_history": history,
        },
    )


# ============================================================================
# 2. 人工纠错
# ============================================================================

@router.post("/correct", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def kqa_correct(
    req: KbQACorrectRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """提交人工纠错。

    用户对 AI 的回答进行修改和补充。

    Args:
        req: 纠错请求（含 id / corrected_answer / correction_notes）
        session: 数据库会话

    Returns:
        ApiResponse: data 含更新后的记录信息
    """
    result = await session.execute(select(KnowledgeQA).where(KnowledgeQA.id == req.id))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail=f"问答记录不存在: {req.id}")

    try:
        record.corrected_answer = req.corrected_answer
        record.correction_notes = req.correction_notes
        record.updated_at = datetime.now(timezone.utc)
        logger.info("[API/knowledge-qa] 纠错提交: {} -> {}chars", req.id, len(req.corrected_answer))
    except Exception as e:
        logger.error("[API/knowledge-qa] 保存纠错失败: {}", str(e)[:200])
        raise HTTPException(status_code=500, detail="保存纠错失败，请稍后重试")

    return ApiResponse(
        success=True,
        message="纠错已保存",
        data=_record_to_dict(record),
    )


# ============================================================================
# 3. 归档到知识库
# ============================================================================

@router.post("/archive", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def kqa_archive(
    req: KbQAArchiveRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """将纠错后的内容归档到知识库。

    根据 archive_type 归档为需求（requirement）或测试用例（test_case）。

    Args:
        req: 归档请求
        session: 数据库会话

    Returns:
        ApiResponse: data 含 archived_id / archived_type / title
    """
    result = await session.execute(select(KnowledgeQA).where(KnowledgeQA.id == req.id))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail=f"问答记录不存在: {req.id}")

    # 使用的最终内容：优先使用纠错后的内容
    final_content = req.content

    archived_id = ""
    title = req.title or f"知识问答归档 - {record.question[:50]}"

    # 产品隔离：从问答记录获取真实 product_id（替代硬编码 PROD-DEFAULT）
    real_product_id = record.product_id or "PROD-DEFAULT"
    archived_now = datetime.now(timezone.utc)

    if req.archive_type == "requirement":
        # 归档为需求
        archived_id = gen_requirement_id()
        new_req = Requirement(
            id=archived_id,
            product_id=real_product_id,
            title=title,
            priority="P2",
            status="archived",
            srr_json={
                "requirement": {
                    "title": title,
                    "summary": final_content[:500],
                    "product_id": real_product_id,
                },
                "flows": {},
                "business_rules": [],
                "boundary_conditions": [],
            },
            raw_text=final_content,
            source_type="knowledge_qa",
            created_by="system",
            created_at=archived_now,
            updated_at=archived_now,
            archived_at=archived_now,
        )
        session.add(new_req)

        # 异步双写向量库（Celery 不可用时降级为同步）
        try:
            from app.tasks.celery_app import celery_app
            archive_meta = {
                "title": title, "type": "knowledge_qa", "qa_id": req.id,
                "product_id": real_product_id,
                "status": "archived",
                "archived_at": archived_now.isoformat(),
            }
            if celery_app is not None:
                celery_app.send_task(
                    "app.tasks.embedding_tasks.embed_and_store_requirement",
                    args=[archived_id, new_req.srr_json, archive_meta],
                    queue="embedding",
                )
            else:
                await asyncio.to_thread(vector_store.add_requirement, archived_id, new_req.srr_json, metadata=archive_meta)
        except Exception as e:
            logger.warning("[API/knowledge-qa] 向量库归档失败: {}", str(e)[:100])

    elif req.archive_type == "test_case":
        # 归档为测试用例
        archived_id = gen_test_case_id("KQA")
        new_case = TestCase(
            id=archived_id,
            req_id="KQA-ARCHIVE",
            product_id=real_product_id,
            title=title,
            type="functional",
            priority="P1",
            status="approved",
            case_json={
                "title": title,
                "description": final_content[:500],
                "steps": [],
                "expected": final_content,
            },
            source="knowledge_qa",
            confidence=1.0,
            created_at=archived_now,
            updated_at=archived_now,
            archived_at=archived_now,
        )
        session.add(new_case)

        # 异步双写向量库（Celery 不可用时降级为同步）
        try:
            from app.tasks.celery_app import celery_app
            archive_meta = {
                "title": title, "type": "knowledge_qa", "qa_id": req.id,
                "product_id": real_product_id,
            }
            if celery_app is not None:
                celery_app.send_task(
                    "app.tasks.embedding_tasks.embed_and_store_test_case",
                    args=[archived_id, new_case.case_json, archive_meta],
                    queue="embedding",
                )
            else:
                await asyncio.to_thread(vector_store.add_test_case, archived_id, new_case.case_json, metadata=archive_meta)
        except Exception as e:
            logger.warning("[API/knowledge-qa] 向量库归档失败: {}", str(e)[:100])

    else:
        raise HTTPException(status_code=400, detail=f"不支持的归档类型: {req.archive_type}")

    # 更新问答记录
    try:
        record.archived = True
        record.archived_type = req.archive_type
        record.archived_id = archived_id
        record.updated_at = datetime.now(timezone.utc)
        logger.info("[API/knowledge-qa] 归档完成: {} -> {} ({})", req.id, archived_id, req.archive_type)
    except Exception as e:
        logger.error("[API/knowledge-qa] 更新归档记录失败: {}", str(e)[:200])

    return ApiResponse(
        success=True,
        message=f"已归档到知识库（{req.archive_type}）",
        data={
            "archived_id": archived_id,
            "archived_type": req.archive_type,
            "title": title,
        },
    )


# ============================================================================
# 4. 问答历史列表
# ============================================================================

@router.get("/history", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def kqa_history(
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取知识问答历史记录。

    Args:
        page: 页码
        page_size: 每页条数
        session: 数据库会话

    Returns:
        ApiResponse: data 含 items / total / page / page_size
    """
    total_stmt = select(func.count(KnowledgeQA.id))
    total = (await session.execute(total_stmt)).scalar() or 0

    stmt = (
        select(KnowledgeQA)
        .order_by(KnowledgeQA.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    records = result.scalars().all()

    items = [_record_to_dict(r) for r in records]

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )


# ============================================================================
# 5. 问答详情
# ============================================================================

@router.get("/history/{record_id}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def kqa_detail(
    record_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取单条问答详情。

    Args:
        record_id: 问答记录 ID
        session: 数据库会话

    Returns:
        ApiResponse: data 含完整问答记录
    """
    result = await session.execute(select(KnowledgeQA).where(KnowledgeQA.id == record_id))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail=f"问答记录不存在: {record_id}")

    return ApiResponse(
        success=True,
        message="ok",
        data=_record_to_dict(record),
    )


# ============================================================================
# 6. 删除问答记录
# ============================================================================

@router.delete("/history/{record_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def kqa_delete(
    record_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """删除问答记录。

    Args:
        record_id: 问答记录 ID
        session: 数据库会话

    Returns:
        ApiResponse: data 含 deleted / record_id
    """
    result = await session.execute(select(KnowledgeQA).where(KnowledgeQA.id == record_id))
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail=f"问答记录不存在: {record_id}")

    await session.delete(record)

    return ApiResponse(
        success=True,
        message="已删除",
        data={"deleted": True, "record_id": record_id},
    )
