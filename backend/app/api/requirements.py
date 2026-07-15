"""需求管理路由。

提供需求的上传解析、列表查询、详情查看、澄清问题获取与答案提交。

端点清单（实现计划 T5.2）：
    POST   /api/requirements/upload              上传需求，返回 req_id + 初始 SRR
    GET    /api/requirements                      需求列表（分页）
    GET    /api/requirements/{id}                 需求详情
    GET    /api/requirements/{id}/clarify/questions  获取待回答的澄清问题
    POST   /api/requirements/{id}/clarify         提交澄清答案

设计依据：
- docs/00-architecture-overview.md §2 访问层
- docs/01-requirement-parsing.md §3（ReqAgent 解析）+ §4（ClarifyAgent 澄清）
- D2 决策：Clarify 流程 API 驱动（不使用 LangGraph interrupt）

流程说明：
    1. 上传需求 → 调用 req_agent.parse 解析为 SRR → KB 检索历史需求 →
       ClarifyAgent 检测生成问题 → 保存为 draft 状态 → 返回问题列表
    2. （前端展示问题给用户选择/回答）
    3. 提交澄清答案 → 应用答案 → 重新调用 LLM 生成最终 SRR →
       状态更新为 active → 返回最终 SRR
    4. 后续调用 /api/generate 时，使用 active 状态的已完成 SRR
"""

from __future__ import annotations

from datetime import datetime, timezone, date
import asyncio
from typing import Any
from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_admin, require_write, get_current_user, CurrentUser
from app.agents.clarify_agent import clarify_agent
from app.utils.id_gen import gen_requirement_id, gen_task_id
from app.agents.req_agent import req_agent
from app.db.database import get_session, async_session
from app.db.models import Requirement, Project, AnalysisTask, FunctionalPoint, FPDependency
from app.db.models import TestCase, GenerationBatch, ReqDependency, Traceability
from app.models.api_schemas import (
    ApiResponse,
    ClarifyAnswerRequest,
    RegenerateRequest,
    RequirementSummary,
    RequirementUpdateRequest,
    RequirementUploadRequest,
)
from app.utils.logger import logger


# ============================================================================
# v1.5.1: FP 自动入库辅助函数
# ============================================================================

async def _save_fps_to_db(
    session: AsyncSession, req_id: str, fps: list[dict], priority: str = "P2",
) -> int:
    """将 ReqAgent 提取的功能点列表存入 functional_points 表。

    每个 FP 的 sub_srr → srr_json，suggested_module 暂存不解析（用户手动分配模块）。
    返回实际入库的 FP 数量。
    """
    if not fps:
        return 0

    saved = 0
    for fp in fps:
        fp_id = gen_requirement_id()
        sub_srr = fp.get("sub_srr", {}) if isinstance(fp, dict) else {}

        record = FunctionalPoint(
            id=fp_id,
            requirement_id=req_id,
            name=((fp.get("name") or "未命名功能点") if isinstance(fp, dict) else "未命名功能点")[:255],
            fp_type=(
                fp.get("fp_type", "workflow") if isinstance(fp, dict) else "workflow"
            ),
            priority=priority,
            srr_json={
                **sub_srr,
                "summary": fp.get("summary", "") if isinstance(fp, dict) else "",
                "suggested_module": fp.get("suggested_module", "") if isinstance(fp, dict) else "",
            },
            atomicity_check=(
                fp.get("atomicity_check", {}) if isinstance(fp, dict) else {}
            ),
            test_hints=(
                fp.get("test_hints", []) if isinstance(fp, dict) else []
            ),
            status="pending_review",
        )
        session.add(record)
        saved += 1

    await session.flush()  # P0-1: 确保 FP 写入 DB，_save_fp_dependencies 可查询到
    logger.info("[FP-AutoSave] 需求 {} 自动存入 {} 个功能点", req_id, saved)
    return saved


async def _save_fp_dependencies(
    session: AsyncSession, req_id: str, deps: list[dict],
) -> int:
    """将 LLM 提取的 FP 依赖关系存入 fp_dependencies 表。

    依赖关系中的 source_fp_name/target_fp_name 需先映射为实际的 FP ID。
    返回成功创建的依赖条数。
    """
    if not deps:
        return 0

    # 加载该需求下所有 FP 的 name→id 映射
    fp_result = await session.execute(
        select(FunctionalPoint.id, FunctionalPoint.name).where(
            FunctionalPoint.requirement_id == req_id
        )
    )
    name_to_id = {row[1]: row[0] for row in fp_result.fetchall()}

    saved = 0
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        src_name = dep.get("source_fp_name", "")
        tgt_name = dep.get("target_fp_name", "")
        src_id = name_to_id.get(src_name)
        tgt_id = name_to_id.get(tgt_name)
        if not src_id or not tgt_id or src_id == tgt_id:
            continue
        relation = dep.get("relation_type", "depends_on")
        if relation not in ("depends_on", "data_flows", "invariants", "lifecycle_scenarios", "conflicts_with"):
            relation = "depends_on"
        # 查重
        dup = await session.execute(
            select(FPDependency).where(
                FPDependency.source_fp_id == src_id,
                FPDependency.target_fp_id == tgt_id,
                FPDependency.relation_type == relation,
            )
        )
        if dup.scalar_one_or_none():
            continue
        session.add(FPDependency(
            source_fp_id=src_id,
            target_fp_id=tgt_id,
            relation_type=relation,
            description=dep.get("description", ""),
        ))
        saved += 1

    if saved:
        logger.info("[FP-Dep] 需求 {} 自动创建 {} 条依赖关系", req_id, saved)
    return saved


# ============================================================================
# 文件校验辅助函数
# ============================================================================

_DOCX_MAGIC = b"PK\x03\x04"  # Office Open XML 文件头（.docx）
_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


def _validate_file_magic(ext: str, raw_bytes: bytes) -> None:
    """通过魔数（magic bytes）校验文件类型，抵御篡改扩展名攻击。

    Args:
        ext: 扩展名（小写，不含点号）
        raw_bytes: 文件前 N 个字节

    Raises:
        HTTPException 400: 魔数校验不通过
    """
    if ext == "docx":
        if not raw_bytes.startswith(_DOCX_MAGIC):
            raise HTTPException(
                status_code=400,
                detail="文件格式不匹配：扩展名为 .docx 但文件头不是 Office Open XML 格式",
            )
    # .md / .markdown：魔数校验意义不大，靠 UTF-8/GBK 解码校验


router = APIRouter()


# ============================================================================
# 1. 上传需求
# ============================================================================

@router.post("/upload", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def upload_requirement(
    req: RequirementUploadRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """上传需求文本，解析为 SRR 后执行 KB 增强 + ClarifyAgent 检测。

    改进流程（v2.3）：
        1. ReqAgent 初始解析
        2. ClarifyAgent 自动执行（KB 检索 + 完整性/歧义性检测 + 问题生成）
        3. KB 自动补全字段直接应用到 SRR
        4. 状态设为 draft（待澄清），问题列表返回前端
        5. 用户提交澄清答案后，重新生成最终 SRR（参见 /{id}/clarify POST）

    Args:
        req: 上传请求，含 raw_text（必填）/ product_id / product_name / title / priority / modules
        session: 数据库会话（依赖注入）

    Returns:
        ApiResponse: data 含 req_id / srr（已 KB 增强的初始 SRR）/ questions / status / title
    """
    if not req.raw_text or not req.raw_text.strip():
        raise HTTPException(status_code=400, detail="需求文本不能为空")

    logger.info(
        "[API/requirements] 上传需求: text_len={}, product_id={}, title={}",
        len(req.raw_text),
        req.product_id,
        req.title or "(自动提取)",
    )

    # ===== ① ReqAgent 解析初始 SRR =====
    try:
        srr = await req_agent.parse(
            raw_requirement=req.raw_text,
            product_id=req.product_id,
            product_name=req.product_name,
        )
    except Exception as e:
        err_msg = str(e)[:300]
        logger.error("[API/requirements] 需求解析失败: {}", err_msg)
        # ... 错误处理保持不变 ...
        if "401" in err_msg or "authentication" in err_msg.lower() or "api key" in err_msg.lower():
            hint = (
                "LLM API Key 无效或未配置，无法解析需求。\n\n"
                "解决办法：\n"
                "1. 进入「系统设置」→ LLM 供应商\n"
                "2. 点击「修改配置」，输入有效的 API Key\n"
                "3. 保存后点击「测试连接」确认连通性\n"
                "4. 再重新上传需求"
            )
        elif "model not found" in err_msg.lower() or "not found" in err_msg.lower():
            hint = (
                "LLM 模型不存在，请检查模型名称是否正确。\n\n"
                "解决办法：进入「系统设置」→ 检查「模型名称」字段"
            )
        elif "connect" in err_msg.lower() or "timeout" in err_msg.lower() or "refused" in err_msg.lower():
            hint = (
                "无法连接到 LLM API，请检查网络。\n\n"
                "解决办法：\n"
                "1. 检查 API 地址是否正确\n"
                "2. 确认当前网络可以访问该 API\n"
                "3. 如需代理，请配置网络环境"
            )
        else:
            hint = f"需求解析失败: {err_msg[:200]}"

        return ApiResponse(
            success=False,
            message=hint,
            data=None,
        )

    req_id = srr.get("requirement", {}).get("id", "")
    if not req_id:
        raise HTTPException(status_code=500, detail="解析失败：未生成需求 ID")

    # 若调用方指定了 title/priority/modules，覆盖 SRR 中的值（text）
    req_data = srr.setdefault("requirement", {})
    if req.title:
        req_data["title"] = req.title
    if req.priority:
        req_data["priority"] = req.priority
    if req.modules:
        req_data["modules"] = req.modules

    # P0 修复：先创建并 flush Requirement 记录，避免 _save_fps_to_db 时外键违反
    record = Requirement(
        id=req_id,
        product_id=req_data.get("product_id", req.product_id or "PROD-DEFAULT"),
        project_id=req.project_id,
        title=req_data.get("title", ""),
        priority=req_data.get("priority", "P2"),
        module=req_data.get("modules", [""])[0] if req_data.get("modules") else "",
        modules=req_data.get("modules", []),
        tags=[],
        version=1,
        status="draft",  # 临时状态，后续根据 clarify 结果更新
        srr_json=srr,
        raw_text=req.raw_text,
        source_type="markdown",
        created_by="api",
    )
    session.add(record)
    await session.flush()  # 确保 Requirement 落库，FP 外键可引用

    # v1.5.1: 自动保存 ReqAgent 一体式提取的功能点
    await _save_fps_to_db(session, req_id, req_agent.last_fps, req.priority)
    await _save_fp_dependencies(session, req_id, req_agent.last_fp_dependencies)

    # ===== ② ClarifyAgent 自动检测（KB 检索 + 问题生成） =====
    clarify_result = None
    questions = []
    try:
        clarify_result = await clarify_agent.clarify(srr, session=session)
        questions = clarify_result.get("questions", [])
        logger.info(
            "[API/requirements] ClarifyAgent 检测完成: req_id={}, status={}, questions={}",
            req_id,
            clarify_result.get("status", ""),
            len(questions),
        )
    except Exception as e:
        logger.warning("[API/requirements] ClarifyAgent 检测失败（不阻断）: {}", str(e)[:200])

    # ===== ③ 缺口判定：是否仍有需人工澄清的问题（以 clarify 返回的 status 为准） =====
    augmented_srr = clarify_result.get("augmented_srr", srr) if clarify_result else srr
    has_unresolved = (clarify_result.get("status") == "needs_clarification") if clarify_result else False

    # ===== ④ 持久化 =====
    # v2.8: 发现缺口→暂存draft等待澄清；无缺口→直接生成最终SRR→active
    if has_unresolved:
        # 有未解答问题 → 暂存 draft，等待人工澄清
        status = "draft"
        srr_json_data = dict(augmented_srr)
        if clarify_result:
            srr_json_data["_clarify_state"] = {
                "questions": questions,
                "auto_augmentations": clarify_result.get("auto_augmentations", []),
                "kb_status": clarify_result.get("kb_status", {}),
                "quality_report": clarify_result.get("quality_report", {}),
                # v2.12: 字段冲突列表（docs/01 §4.4.1），供前端展示人工确认
                "field_conflicts": clarify_result.get("field_conflicts", []),
            }
            kb_logs = clarify_result.get("kb_access_log", [])
            if kb_logs:
                srr_json_data["_kb_query_logs"] = kb_logs

        logger.info("[API/requirements] 有未解答问题 → draft: req_id={}, pending={}",
                    req_id, len(questions))

    else:
        # 无缺口或 KB 全解答 → 生成最终 SRR → active
        logger.info("[API/requirements] KB已解答或无缺口 → 生成最终SRR: req_id={}", req_id)

        # 融合 KB 补全信息到原始文本
        kb_augments = clarify_result.get("auto_augmentations", []) if clarify_result else []
        if kb_augments:
            parts = [req.raw_text, "\n\n## KB 自动补全信息\n"]
            for aug in kb_augments:
                parts.append(f"- {aug.get('field', '')}: {str(aug.get('value', ''))[:300]}")
            enhanced_raw = "\n".join(parts)
        else:
            enhanced_raw = req.raw_text

        try:
            final_srr = await req_agent.parse(
                raw_requirement=enhanced_raw,
                product_id=req.product_id or "PROD-DEFAULT",
            )
            final_srr_data = dict(final_srr)
        except Exception as e:
            logger.error("[API/requirements] 最终SRR生成失败→降级: {}", str(e)[:200])
            final_srr_data = dict(srr)
            final_srr_data["_finalize_degraded"] = True

        # 保留 KB 日志
        if clarify_result:
            kb_logs = clarify_result.get("kb_access_log", [])
            if kb_logs:
                final_srr_data["_kb_query_logs"] = kb_logs

        final_srr_data.pop("_clarify_state", None)
        # v2.12: 清理临时字段来源标记（不写入正式 SRR Schema）
        final_srr_data.pop("_field_sources", None)
        status = "active"
        srr_json_data = final_srr_data

    # 更新已存在的 Requirement 记录（P0 修复：record 在 _save_fps_to_db 前已创建）
    record.status = status
    record.srr_json = srr_json_data
    await session.commit()

    logger.info(
        "[API/requirements] 需求已保存: req_id={}, title={}, status={}, questions={}",
        req_id,
        req_data.get("title", ""),
        status,
        len(questions),
    )

    return ApiResponse(
        success=True,
        message="需求分析完成" + ("，有未解答问题待人工澄清" if has_unresolved else "，已自动生成最终SRR"),
        data={
            "req_id": req_id,
            "srr": srr_json_data,
            "title": req_data.get("title", ""),
            "priority": req_data.get("priority", ""),
            "status": status,
            "questions": questions,
            "has_unanswered_questions": has_unresolved,
            "kb_answered": clarify_result.get("kb_answered_count", 0) if clarify_result else 0,
            "kb_total": clarify_result.get("kb_total_count", 0) if clarify_result else 0,
            "kb_status": clarify_result.get("kb_status", {}) if clarify_result else {},
            "auto_augmentations_count": len(clarify_result.get("auto_augmentations", [])) if clarify_result else 0,
            "kb_empty": clarify_result.get("kb_status", {}).get("kb_empty", True) if clarify_result else True,
        },
    )


# ============================================================================
# 1b. 文件上传需求（支持 .md / .docx）
# ============================================================================

@router.post("/upload/file", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def upload_requirement_file(
    file: UploadFile = File(..., description="需求文件（.md 或 .docx）"),
    product_id: str = Form(default="PROD-DEFAULT", description="产品 ID"),
    product_name: str = Form(default="", description="产品名称"),
    title: str = Form(default="", description="需求标题（可选，留空则从内容提取）"),
    priority: str = Form(default="P2", description="优先级 P0-P3"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """上传需求文件（.md 或 .docx），自动解析文本后调用 ReqAgent 生成 SRR。

    支持的文件格式：
    - .md / .markdown：直接读取文本内容
    - .docx：使用 python-docx 解析为纯文本

    安全校验：
    - 文件扩展名白名单（仅 .md / .docx）
    - MIME 类型二次校验（字段检查 + 魔数检查）
    - 文件大小上限：10MB（避免内存溢出）

    设计依据：
    - docs/01-requirement-parsing.md §3（ReqAgent 解析流程）
    - docs/00 §2 访问层：文件上传是标准的接入方式
    """
    # 1. 校验文件扩展名
    filename = file.filename or ""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    if ext not in ("md", "markdown", "docx", "txt"):
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: .{ext}，仅支持 .md / .docx / .txt",
        )

    # 1.5 文件大小检查（10MB 上限）
    MAX_FILE_SIZE = 10 * 1024 * 1024
    raw_bytes = await file.read()
    if len(raw_bytes) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail="文件大小超过 10MB 上限，请压缩后重试",
        )

    logger.info(
        "[API/requirements] 上传文件: filename={}, ext=.{}, size={}, product_id={}, title={}",
        filename,
        ext,
        len(raw_bytes),
        product_id,
        title or "(自动提取)",
    )

    # 2. MIME 类型魔数校验（抵御篡改扩展名的攻击）
    _validate_file_magic(ext, raw_bytes)

    # 3. 读取并解析文件内容
    raw_text = ""

    if ext in ("md", "markdown", "txt"):
        # 直接读取为 UTF-8 文本
        try:
            raw_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            # 尝试 GBK 编码（Windows 常见）
            try:
                raw_text = raw_bytes.decode("gbk")
            except UnicodeDecodeError:
                raise HTTPException(
                    status_code=400,
                    detail="文件编码不支持，请使用 UTF-8 或 GBK 编码的 .md 文件",
                )
    elif ext == "docx":
        # 使用 python-docx 解析
        try:
            from io import BytesIO
            from docx import Document

            doc = Document(BytesIO(raw_bytes))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            raw_text = "\n".join(paragraphs)

            if not raw_text.strip():
                # docx 中段落为空，尝试读取表格
                for table in doc.tables:
                    for row in table.rows:
                        cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                        if cells:
                            raw_text += " | ".join(cells) + "\n"
        except ImportError:
            raise HTTPException(
                status_code=500,
                detail="python-docx 未安装，无法解析 .docx 文件。请执行: pip install python-docx",
            )
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f".docx 文件解析失败: {str(e)[:150]}",
            )

    if not raw_text or not raw_text.strip():
        raise HTTPException(status_code=400, detail="文件内容为空")

    logger.info("[API/requirements] 文件解析完成: raw_text_len={}", len(raw_text))

    # 3. 调用 ReqAgent 解析需求为 SRR
    try:
        srr = await req_agent.parse(
            raw_requirement=raw_text,
            product_id=product_id,
            product_name=product_name,
        )
    except Exception as e:
        logger.error("[API/requirements] 需求解析失败: {}", str(e)[:200])
        raise HTTPException(
            status_code=500,
            detail=f"需求解析失败: {str(e)[:200]}",
        )

    req_id = srr.get("requirement", {}).get("id", "")
    if not req_id:
        raise HTTPException(status_code=500, detail="解析失败：未生成需求 ID")

    # 确保 req_id 唯一（防止 LLM 生成重复 ID）
    existing = await session.get(Requirement, req_id)
    if existing:
        # 重新生成唯一 ID（线程安全计数器，避免并发冲突）
        req_id = gen_requirement_id()
        srr["requirement"]["id"] = req_id
        logger.warning("[API/requirements] req_id 冲突 (file)，重新生成: new_id={}", req_id)

    # v1.5.1: 自动保存 FP
    req_data = srr.setdefault("requirement", {})
    if title:
        req_data["title"] = title
    if priority:
        req_data["priority"] = priority

    # P0 修复：先创建并 flush Requirement 记录，避免 _save_fps_to_db 时外键违反
    record = Requirement(
        id=req_id,
        product_id=req_data.get("product_id", product_id),
        title=req_data.get("title", ""),
        priority=req_data.get("priority", "P2"),
        module=req_data.get("modules", [""])[0] if req_data.get("modules") else "",
        modules=req_data.get("modules", []),
        tags=[],
        version=1,
        status="active",
        srr_json=srr,
        raw_text=raw_text,
        source_type=ext,
        created_by="api",
    )
    session.add(record)
    await session.flush()

    await _save_fps_to_db(session, req_id, req_agent.last_fps, priority)
    await _save_fp_dependencies(session, req_id, req_agent.last_fp_dependencies)
    await session.commit()

    logger.info(
        "[API/requirements] 文件上传需求已保存: req_id={}, title={}, source=file.{}",
        req_id,
        req_data.get("title", ""),
        ext,
    )

    return ApiResponse(
        success=True,
        message=f".{ext} 文件需求解析成功",
        data={
            "req_id": req_id,
            "srr": srr,
            "title": req_data.get("title", ""),
            "priority": req_data.get("priority", ""),
            "source_file": filename,
        },
    )


# ============================================================================
# 1c. 异步分析后台协程（v2.14 新增 - 需求解析/澄清的异步执行）
# ============================================================================

async def _update_task(task_id: str, **fields) -> None:
    """更新 AnalysisTask 记录（独立 session，供后台协程使用）。"""
    try:
        async with async_session() as s:
            from sqlalchemy import update as sa_update
            stmt = sa_update(AnalysisTask).where(AnalysisTask.id == task_id).values(**fields)
            await s.execute(stmt)
            await s.commit()
    except Exception as e:
        logger.warning("[Task] 更新任务状态失败: task_id={}, error={}", task_id, str(e)[:150])


async def _run_requirement_analysis(
    task_id: str,
    req_id: str,
    combined_raw: str,
    product_id: str,
    product_name: str,
    title: str,
    priority: str,
    project_id: str,
    module_list: list[str],
    source_files: list[str],
) -> None:
    """后台执行需求解析 + 澄清（异步协程，使用独立 session）。

    流程：
    1. phase=parse: req_agent.parse() 解析需求
    2. phase=kb_search: clarify_agent.clarify() KB 检索 + 问题生成
    3. 若有问题: phase=clarify, status=completed, result_json 含 questions
       若无问题: phase=regenerate, 生成 final_srr, Requirement.status=active
    """
    from app.db.models import Requirement as ReqModel
    from sqlalchemy import update as sa_update

    async def _is_cancelled() -> bool:
        """检测任务是否被用户取消（status=interrupted）。"""
        try:
            async with async_session() as s:
                t = await s.get(AnalysisTask, task_id)
                return t is not None and t.status == "interrupted"
        except Exception:
            return False

    try:
        # ===== phase 1: parse =====
        await _update_task(task_id, status="running", phase="parse", progress=10,
                           message="正在调用 LLM 解析需求文档...")

        srr = await req_agent.parse(
            raw_requirement=combined_raw,
            product_id=product_id,
            product_name=product_name or "",
        )
        if not isinstance(srr, dict):
            raise RuntimeError(f"需求解析返回异常类型: {type(srr).__name__}")

        # P0-2: 立即捕获 FP，避免二次 parse 覆盖
        captured_fps = list(req_agent.last_fps)
        captured_fp_deps = list(req_agent.last_fp_dependencies)

        if await _is_cancelled():
            logger.info("[Task] 任务已取消，提前退出: task_id={}", task_id)
            return

        req_data = srr.setdefault("requirement", {})
        # 覆盖调用方指定的字段
        if title:
            req_data["title"] = title
        if priority:
            req_data["priority"] = priority
        if module_list:
            req_data["modules"] = module_list

        await _update_task(task_id, phase="parse", progress=30, message="需求解析完成，正在检索知识库...")

        if await _is_cancelled():
            logger.info("[Task] 任务已取消（kb_search 前），提前退出: task_id={}", task_id)
            return

        # ===== phase 2: kb_search + clarify =====
        await _update_task(task_id, phase="kb_search", progress=40,
                           message="正在检索知识库历史需求并补充缺失字段...")

        clarify_result = None
        questions = []
        async with async_session() as s:
            try:
                clarify_result = await clarify_agent.clarify(srr, session=s)
                questions = clarify_result.get("questions", [])
            except Exception as e:
                logger.warning("[Task] ClarifyAgent 检测失败（不阻断）: {}", str(e)[:200])

        await _update_task(task_id, phase="clarify", progress=70,
                           message=f"知识库检索完成，检测到 {len(questions)} 个待补充问题" if questions else "知识库检索完成，正在生成最终分析...")

        # ===== 构建持久化数据 =====
        multi_augmented = clarify_result.get("augmented_srr", srr) if clarify_result else srr
        srr_json_data = dict(multi_augmented)
        if clarify_result:
            srr_json_data["_clarify_state"] = {
                "questions": questions,
                "auto_augmentations": clarify_result.get("auto_augmentations", []),
                "kb_status": clarify_result.get("kb_status", {}),
                "quality_report": clarify_result.get("quality_report", {}),
                "field_conflicts": clarify_result.get("field_conflicts", []),
            }
            kb_logs = clarify_result.get("kb_access_log", [])
            if kb_logs:
                srr_json_data["_kb_query_logs"] = kb_logs

        needs_clarify = (clarify_result.get("status") == "needs_clarification") if clarify_result else (len(questions) > 0)
        final_status = "draft" if needs_clarify else "active"

        # ===== phase 3: regenerate（无澄清问题时生成最终 SRR）=====
        if not needs_clarify:
            await _update_task(task_id, phase="regenerate", progress=85,
                               message="正在生成最终需求分析结果...")
            kb_augments = clarify_result.get("auto_augmentations", []) if clarify_result else []
            if kb_augments:
                parts = [combined_raw, "\n\n## KB 自动补全信息\n"]
                for aug in kb_augments:
                    parts.append(f"- {aug.get('field', '')}: {str(aug.get('value', ''))[:300]}")
                enhanced_raw = "\n".join(parts)
            else:
                enhanced_raw = combined_raw
            try:
                final_srr = await req_agent.parse(
                    raw_requirement=enhanced_raw,
                    product_id=product_id or "PROD-DEFAULT",
                )
                final_srr_data = dict(final_srr)
                # 保留 KB 日志
                if clarify_result:
                    kb_logs = clarify_result.get("kb_access_log", [])
                    if kb_logs:
                        final_srr_data["_kb_query_logs"] = kb_logs
                final_srr_data.pop("_clarify_state", None)
                final_srr_data.pop("_field_sources", None)
                srr_json_data = final_srr_data
            except Exception as e:
                logger.error("[Task] 最终SRR生成失败→降级: {}", str(e)[:200])
                final_srr_data = dict(srr)
                final_srr_data["_finalize_degraded"] = True
                srr_json_data = final_srr_data

        # ===== 持久化 Requirement（更新已创建的 analyzing 记录）=====
        async with async_session() as s:
            stmt = sa_update(ReqModel).where(ReqModel.id == req_id).values(
                title=req_data.get("title", ""),
                priority=req_data.get("priority", "P2"),
                module=module_list[0] if module_list else "",
                modules=module_list,
                status=final_status,
                srr_json=srr_json_data,
                raw_text=combined_raw,
            )
            await s.execute(stmt)
            await s.commit()

        # v1.5.1: 自动保存 ReqAgent 一体式提取的功能点 + 依赖关系
        try:
            async with async_session() as s:
                await _save_fps_to_db(s, req_id, captured_fps, req_data.get("priority", "P2"))
                await _save_fp_dependencies(s, req_id, captured_fp_deps)
                await s.commit()
        except Exception as e:
            logger.warning("[Task] FP 自动保存失败（不阻断）: {}", str(e)[:200])

        # ===== 完成 =====
        result = {
            "req_id": req_id,
            "title": req_data.get("title", ""),
            "status": final_status,
            "questions": questions,
            "has_unanswered_questions": needs_clarify,
            "source_files": source_files,
            "kb_status": clarify_result.get("kb_status", {}) if clarify_result else {},
            "kb_empty": clarify_result.get("kb_status", {}).get("kb_empty", True) if clarify_result else True,
        }
        await _update_task(
            task_id, status="completed", phase="done", progress=100,
            message="分析完成" + ("，有待补充问题" if needs_clarify else ""),
            result_json=result, completed_at=datetime.now(timezone.utc),
        )
        logger.info("[Task] 需求分析完成: task_id={}, req_id={}, status={}, questions={}",
                    task_id, req_id, final_status, len(questions))

    except Exception as e:
        err_msg = str(e)[:500]
        logger.error("[Task] 需求分析失败: task_id={}, req_id={}, error={}", task_id, req_id, err_msg)
        await _update_task(
            task_id, status="failed", phase="", error=err_msg,
            message="分析失败: " + err_msg[:200], completed_at=datetime.now(timezone.utc),
        )
        # 失败时将 Requirement 标记为 draft（便于用户看到记录）
        try:
            async with async_session() as s:
                stmt = sa_update(Requirement).where(Requirement.id == req_id).values(
                    status="draft", title=title or "(分析失败)",
                )
                await s.execute(stmt)
                await s.commit()
        except Exception as e:
            logger.warning("[API/requirements] 失败回退 Requirement 状态失败 req_id={}: {}", req_id, str(e)[:150])


# ============================================================================
# 1d. 多文件上传需求（v2.1 新增：需求文档 + 设计文档联合分析）
# ============================================================================

@router.post("/upload/multi", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def upload_requirement_multi(
    files: list[UploadFile] = File(..., description="多文件列表（需求文档 + 设计文档）"),
    product_id: str = Form(default="PROD-DEFAULT", description="产品 ID"),
    product_name: str = Form(default="", description="产品名称"),
    title: str = Form(default="", description="需求标题（可选）"),
    priority: str = Form(default="P2", description="优先级 P0-P3"),
    project_id: str = Form(default="", description="项目/版本 ID"),
    modules: str = Form(default="", description="关联模块（逗号分隔）"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """上传多个需求文件（.md / .docx），DocMergeAgent 合并后统一分析。

    支持场景：
    - 上传需求文档 + 设计文档，联合分析互为上下文补充
    - 上传多个需求的片段，合并为一个完整需求

    支持格式：.md / .markdown / .docx
    文件上限：单个 10MB，总大小不超过 50MB

    v2.1 新增：
    - 多文档融合分析（DocMergeAgent）
    - 冲突检测与合并
    """
    module_list = [m.strip() for m in modules.split(",") if m.strip()]
    if not files or len(files) == 0:
        raise HTTPException(status_code=400, detail="请上传至少一个文件")

    MAX_TOTAL_SIZE = 50 * 1024 * 1024
    total_size = 0
    doc_contents: list[dict] = []

    for file in files:
        filename = file.filename or ""
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        if ext not in ("md", "markdown", "docx", "txt"):
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式: .{ext}（文件：{filename}），仅支持 .md / .docx / .txt",
            )
        raw_bytes = await file.read()
        total_size += len(raw_bytes)
        if total_size > MAX_TOTAL_SIZE:
            raise HTTPException(status_code=400, detail="所有文件总大小超过 50MB 上限")

        if ext in ("md", "markdown", "txt"):
            text = raw_bytes.decode("utf-8", errors="replace")
        else:
            try:
                from io import BytesIO
                from docx import Document
                doc = Document(BytesIO(raw_bytes))
                text = "\n".join(p.text for p in doc.paragraphs)
            except ImportError:
                raise HTTPException(status_code=500, detail="python-docx 未安装，无法解析 .docx 文件")
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"解析 {filename} 失败: {str(e)[:200]}")

        doc_contents.append({
            "filename": filename,
            "text": text,
        })

    # 多文档合并（DocMergeAgent 逻辑：LLM 融合分析）
    merged_text_parts = []
    for doc in doc_contents:
        merged_text_parts.append(f"【文档：{doc['filename']}】\n{doc['text']}")
    combined_raw = "\n\n---\n\n".join(merged_text_parts)

    # v2.14: 异步分析模式——立即创建 Requirement(analyzing) + AnalysisTask，后台协程执行解析
    req_id = gen_requirement_id()
    task_id = gen_task_id()

    # 立即创建 Requirement 记录（status=analyzing，分析完成后更新）
    record = Requirement(
        id=req_id,
        product_id=product_id,
        project_id=project_id or None,
        title=title or "(分析中...)",
        priority=priority,
        module=module_list[0] if module_list else "",
        modules=module_list,
        tags=[],
        version=1,
        status="analyzing",
        srr_json={},
        raw_text=combined_raw,
        source_type="multi",
        created_by="api",
    )
    session.add(record)
    await session.flush()  # 确保 Requirement 先落库，避免 AnalysisTask 外键约束 violation

    # 创建任务记录
    task = AnalysisTask(
        id=task_id,
        task_type="upload_requirement",
        req_id=req_id,
        product_id=product_id,
        status="pending",
        phase="parse",
        progress=0,
        message="已接收文件，准备开始分析...",
        created_by="api",
    )
    session.add(task)
    await session.commit()

    # 启动后台协程（不 await，立即返回）
    asyncio.create_task(_run_requirement_analysis(
        task_id=task_id,
        req_id=req_id,
        combined_raw=combined_raw,
        product_id=product_id,
        product_name=product_name,
        title=title,
        priority=priority,
        project_id=project_id,
        module_list=module_list,
        source_files=[d["filename"] for d in doc_contents],
    ))

    logger.info("[API] 多文档上传已派发异步分析: task_id={}, req_id={}, files={}",
                task_id, req_id, [d["filename"] for d in doc_contents])

    return ApiResponse(
        success=True,
        message=f"已接收 {len(doc_contents)} 个文件，正在后台分析",
        data={
            "task_id": task_id,
            "req_id": req_id,
            "status": "analyzing",
            "source_files": [d["filename"] for d in doc_contents],
        },
    )


# ============================================================================
# 1e. 任务状态查询（v2.14 新增 - 供前端轮询分析进度）
# ============================================================================

@router.get("/tasks", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_tasks(
    status: str | None = Query(default=None, description="按状态筛选 pending/running/completed/failed/interrupted"),
    task_type: str | None = Query(default=None, description="按任务类型筛选 upload_requirement/submit_clarify/..."),
    product_id: str | None = Query(default=None, description="按产品筛选"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """列出分析任务（支持按状态/类型/产品筛选，分页）。"""
    conditions = []
    if status:
        conditions.append(AnalysisTask.status == status)
    if task_type:
        conditions.append(AnalysisTask.task_type == task_type)
    if product_id:
        conditions.append(AnalysisTask.product_id == product_id)

    count_stmt = select(func.count(AnalysisTask.id))
    for cond in conditions:
        count_stmt = count_stmt.where(cond)
    total = (await session.execute(count_stmt)).scalar() or 0

    stmt = select(AnalysisTask).order_by(AnalysisTask.created_at.desc())
    for cond in conditions:
        stmt = stmt.where(cond)
    stmt = stmt.offset(offset).limit(limit)
    result = await session.execute(stmt)
    rows = result.scalars().all()

    # 关联需求标题（避免 N+1，批量查询）
    req_ids = {r.req_id for r in rows if r.req_id}
    req_titles: dict[str, str] = {}
    if req_ids:
        title_rows = await session.execute(
            select(Requirement.id, Requirement.title).where(Requirement.id.in_(req_ids))
        )
        req_titles = dict(title_rows.all())

    items = []
    for r in rows:
        d = r.to_dict()
        d["req_title"] = req_titles.get(r.req_id or "", "")
        items.append(d)

    return ApiResponse(success=True, message="ok", data={"items": items, "total": total})


@router.get("/tasks/{task_id}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """查询单个任务状态（供前端轮询进度）。"""
    task = await session.get(AnalysisTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    d = task.to_dict()
    # 关联需求标题
    if task.req_id:
        req = await session.get(Requirement, task.req_id)
        d["req_title"] = req.title if req else ""
    else:
        d["req_title"] = ""

    return ApiResponse(success=True, message="ok", data=d)


@router.get("/{req_id}/task", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_task_by_req(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """按需求 ID 查询关联的最新任务（供需求详情页恢复进度）。"""
    stmt = (
        select(AnalysisTask)
        .where(AnalysisTask.req_id == req_id)
        .order_by(AnalysisTask.created_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    task = result.scalar_one_or_none()

    if not task:
        return ApiResponse(success=True, message="无关联任务", data=None)

    return ApiResponse(success=True, message="ok", data=task.to_dict())


@router.post("/tasks/{task_id}/cancel", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def cancel_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """取消分析任务（将 running/pending 标记为 interrupted）。

    Note:
        - 无法真正终止正在运行的 asyncio 协程，仅标记状态。
          后台协程在下次更新状态时会检测到 interrupted 并提前退出。
        - 已完成/已失败/已中断的任务不可取消。

    Raises:
        HTTPException 404: 任务不存在
        HTTPException 409: 任务不在可取消状态
    """
    task = await session.get(AnalysisTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status not in ("pending", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"任务状态为 {task.status}，无法取消（仅 pending/running 可取消）",
        )

    task.status = "interrupted"
    task.error = "用户手动取消"
    task.message = "已取消"
    task.completed_at = datetime.now(timezone.utc)

    # 若关联需求仍为 analyzing 状态，回退为 draft 避免卡死
    if task.req_id:
        req = await session.get(Requirement, task.req_id)
        if req and req.status == "analyzing":
            req.status = "draft"
            logger.info("[API] 任务取消，需求状态回退: req_id={} analyzing→draft", task.req_id)

    await session.commit()
    logger.info("[API] 任务已取消: task_id={}, task_type={}", task_id, task.task_type)

    return ApiResponse(success=True, message="任务已取消", data=task.to_dict())


# ============================================================================
# 2. 需求列表（分页）
# ============================================================================

@router.get("", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_requirements(
    product_id: str | None = Query(default=None, description="按产品 ID 筛选"),
    project_id: str | None = Query(default=None, description="按项目 ID 筛选"),
    priority: str | None = Query(default=None, description="按优先级筛选 P0-P3"),
    status: str | None = Query(default=None, description="按状态筛选 draft/active/archived"),
    search: str | None = Query(default=None, description="按标题模糊搜索"),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数，最大 100"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取需求列表（分页）。

    Args:
        product_id: 按产品 ID 筛选（可选）
        project_id: 按项目/版本 ID 筛选（可选）
        priority: 按优先级筛选（可选）
        status: 按状态筛选（可选）
        search: 按标题模糊搜索（可选）
        page: 页码，从 1 开始
        page_size: 每页条数
        session: 数据库会话

    Returns:
        ApiResponse: data 含 items（RequirementSummary 列表）/ total / page / page_size
    """
    conditions = []
    if product_id:
        conditions.append(Requirement.product_id == product_id)
    if project_id:
        conditions.append(Requirement.project_id == project_id)
    if priority:
        conditions.append(Requirement.priority == priority)
    if status:
        conditions.append(Requirement.status == status)
    if search:
        conditions.append(Requirement.title.ilike(f"%{search}%"))

    # 计算总数
    count_stmt = select(func.count(Requirement.id))
    for cond in conditions:
        count_stmt = count_stmt.where(cond)
    total = (await session.execute(count_stmt)).scalar() or 0

    # 分页查询（按创建时间倒序）
    stmt = select(Requirement).order_by(Requirement.created_at.desc())
    for cond in conditions:
        stmt = stmt.where(cond)
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(stmt)
    rows = result.scalars().all()

    items = [
        RequirementSummary(
            id=r.id,
            title=r.title,
            product_id=r.product_id,
            project_id=r.project_id or "",
            priority=r.priority,
            status=r.status,
            source_type=r.source_type,
            modules=r.modules or [],
            tags=r.tags or [],
            created_at=r.created_at.isoformat() if r.created_at else "",
        ).model_dump()
        for r in rows
    ]

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
# 3. 需求详情
# ============================================================================

@router.get("/{req_id}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_requirement(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取需求详情（含完整 SRR JSON）。

    Args:
        req_id: 需求 ID
        session: 数据库会话

    Returns:
        ApiResponse: data 含需求元数据 + srr_json + raw_text

    Raises:
        HTTPException 404: 需求不存在
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "id": record.id,
            "product_id": record.product_id,
            "title": record.title,
            "priority": record.priority,
            "modules": record.modules,
            "status": record.status,
            "version": record.version,
            "srr_json": record.srr_json,  # 注意字段名 srr_json（前端和类型定义使用此名）
            "raw_text": record.raw_text,
            "source_type": record.source_type,
            "kb_query_logs": (record.srr_json or {}).get("_kb_query_logs", []),
            "questions_count": len(((record.srr_json or {}).get("_clarify_state", {}) or {}).get("questions", [])),
            "created_at": record.created_at.isoformat() if record.created_at else "",
            "updated_at": record.updated_at.isoformat() if record.updated_at else "",
        },
    )


# ============================================================================
# 4. 获取澄清问题
# ============================================================================

@router.get("/{req_id}/clarify/questions", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_clarify_questions(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取需求的待回答澄清问题。

    调用 ClarifyAgent 对 SRR 执行澄清分析，返回需要人工确认的问题列表。
    本端点不修改数据库，仅返回问题供前端展示。

    Args:
        req_id: 需求 ID
        session: 数据库会话

    Returns:
        ApiResponse: data 含 status / questions / quality_report / kb_status

    Raises:
        HTTPException 404: 需求不存在
    """
    # 从 DB 取 SRR
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    srr = record.srr_json or {}
    if not srr:
        raise HTTPException(status_code=400, detail="需求 SRR 为空，无法澄清")

    logger.info("[API/requirements] 获取澄清问题: req_id={}", req_id)

    # 读取上传时保存的澄清状态（快照：暂不处理后回来看到的应是同一份问题，不重新生成）
    clarify_state = srr.get("_clarify_state", {}) or {}
    saved_questions = clarify_state.get("questions", []) or []
    saved_answers = srr.get("clarify_answers", {}) or {}

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "req_id": req_id,
            "status": "needs_clarification" if saved_questions else "passed",
            "questions": saved_questions,
            "previous_answers": saved_answers,
            "quality_report": clarify_state.get("quality_report", {}),
            "kb_status": clarify_state.get("kb_status", {}),
            "auto_augmentations": clarify_state.get("auto_augmentations", []),
            # v2.12: 字段冲突列表（docs/01 §4.4.1），供前端展示人工确认
            "field_conflicts": clarify_state.get("field_conflicts", []),
        },
    )


# ============================================================================
# 5. 提交澄清答案
# ============================================================================

@router.post("/{req_id}/clarify", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def submit_clarify_answers(
    req_id: str,
    req: ClarifyAnswerRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """提交澄清问题答案 → 重新生成最终 SRR。

    改进流程（v2.3）：
        1. 用户答案应用到 SRR
        2. 使用原始文本 + 用户补充信息作为增强输入，
           重新调用 ReqAgent 生成更完整的最终 SRR
        3. 状态从 draft 更新为 active
        4. 返回最终 SRR

    Args:
        req_id: 需求 ID
        req: 澄清答案请求，含 answers 字典 {question_id: answer}
        session: 数据库会话

    Returns:
        ApiResponse: data 含 req_id / srr（最终 SRR）/ status
    """
    if not req.answers:
        raise HTTPException(status_code=400, detail="澄清答案不能为空")

    # 从 DB 取需求
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    raw_text = record.raw_text or ""
    srr_json = dict(record.srr_json or {})
    clarify_state = srr_json.get("_clarify_state", {}) or {}
    questions = clarify_state.get("questions", []) or []

    # ===== ⓪ 保存澄清历史快照（在 re-parse 前留存 Q&A 记录） =====
    history_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "clarify",
        "questions": questions,
        "answers": dict(req.answers),
        "extra_notes": req.extra_notes or "",
        "auto_augmentations": clarify_state.get("auto_augmentations", []),
        "field_conflicts": clarify_state.get("field_conflicts", []),
        "quality_report": clarify_state.get("quality_report", {}),
    }
    clarify_history = list(srr_json.get("_clarify_history", []))
    clarify_history.append(history_entry)

    # ===== ① 构建增强后的需求文本（原始文本 + Q&A 补充 + 额外说明） =====
    enhanced_parts = [raw_text]

    # 将 Q&A 对格式化为结构化补充信息
    qa_pairs = []
    for q in questions:
        qid = q.get("id", "")
        answer = req.answers.get(qid, "").strip()
        if answer:
            question_text = q.get("question", "")
            field = q.get("field", "")
            qa_pairs.append(f"- 【{field}】{question_text}\n  回答：{answer}")

    # v2.12: 处理字段冲突解决答案（conflict_<field> 键）
    # 用户在前端对字段冲突选择"采用 KB"/"自定义"后，答案以 conflict_<field> 键提交
    field_conflicts = clarify_state.get("field_conflicts", []) or []
    for c in field_conflicts:
        c_field = c.get("field", "")
        conflict_answer = req.answers.get(f"conflict_{c_field}", "").strip()
        if conflict_answer:
            existing_src = c.get("existing_source", "")
            suggested_src = c.get("suggested_source", "")
            qa_pairs.append(
                f"- 【{c_field}】字段冲突解决（原文来源:{existing_src} / KB建议来源:{suggested_src}）\n"
                f"  最终采用值：{conflict_answer}"
            )

    if qa_pairs:
        enhanced_parts.append("\n\n## 补充信息（用户澄清）\n")
        enhanced_parts.extend(qa_pairs)

    # 合并 KB 已自动补全的字段（避免人工澄清时丢失 KB 已查到的答案）
    kb_augments = (srr_json.get("_clarify_state", {}) or {}).get("auto_augmentations", [])
    if kb_augments:
        enhanced_parts.append("\n\n## KB 自动补全信息\n")
        for aug in kb_augments:
            val = aug.get("value", "")
            enhanced_parts.append(f"- {aug.get('field', '')}: {str(val)[:300]}")

    # 额外补充说明
    if req.extra_notes and req.extra_notes.strip():
        enhanced_parts.append(f"\n\n## 额外补充说明\n{req.extra_notes.strip()}")

    enhanced_raw = "\n".join(enhanced_parts)

    logger.info(
        "[API/requirements] 提交澄清答案并重新生成: req_id={}, answers={}, enhanced_len={}",
        req_id,
        len(req.answers),
        len(enhanced_raw),
    )

    # ===== ② 重新调用 ReqAgent 生成最终 SRR =====
    try:
        final_srr = await req_agent.parse(
            raw_requirement=enhanced_raw,
            product_id=record.product_id,
        )
        logger.info("[API/requirements] 最终 SRR 生成成功: req_id={}", req_id)
    except Exception as e:
        logger.error("[API/requirements] 最终 SRR 生成失败: {}", str(e)[:200])
        # 降级：使用原有 SRR + 应用答案
        final_srr = dict(srr_json)
        # 移除澄清状态标记
        final_srr.pop("_clarify_state", None)
        final_srr.pop("clarify_answers", None)
        final_srr.pop("_field_sources", None)
        # 标记为降级
        final_srr["_clarify_degraded"] = True
        logger.warning("[API/requirements] 降级使用原有 SRR: req_id={}", req_id)

    # ===== ③ 更新 DB =====
    final_data = dict(final_srr)
    # 移除临时状态标记
    final_data.pop("_clarify_state", None)
    final_data.pop("clarify_answers", None)
    final_data.pop("_field_sources", None)
    # 保留 KB 查询日志（不在 re-parse 中丢失）
    if original_kb_logs := (dict(record.srr_json or {}).get("_kb_query_logs")):
        final_data["_kb_query_logs"] = original_kb_logs
    # 保留澄清历史（re-parse 生成的新 SRR 不含 _clarify_history，需手动迁移）
    final_data["_clarify_history"] = clarify_history
    # 合并旧 SRR 的 KB 基线补全内容（_baseline_context / additional_details）
    # re-parse 后是新 SRR，KB 已补全的结构化上下文需手动迁移
    old_srr = dict(record.srr_json or {})
    for field in ("_baseline_context", "additional_details"):
        old_val = old_srr.get(field)
        if old_val:
            if field == "additional_details":
                # additional_details 累加而非覆盖
                existing = final_data.get("additional_details", "")
                final_data["additional_details"] = str(old_val) + "\n" + str(existing)
            else:
                final_data[field] = old_val

    record.srr_json = final_data
    record.status = "active"
    await session.commit()

    logger.info(
        "[API/requirements] 澄清完成，需求已激活: req_id={}, title={}",
        req_id,
        final_data.get("requirement", {}).get("title", ""),
    )

    # v1.5.3: 澄清后 re-parse 已提取新 FP，替换旧 FP（澄清内容更完整）
    if req_agent.last_fps:
        try:
            from sqlalchemy import delete as sa_del
            # 删除旧 FP + 依赖
            old_fps = await session.execute(
                select(FunctionalPoint.id).where(FunctionalPoint.requirement_id == req_id)
            )
            old_ids = {r[0] for r in old_fps.fetchall()}
            if old_ids:
                await session.execute(
                    sa_del(FPDependency).where(
                        (FPDependency.source_fp_id.in_(old_ids)) | (FPDependency.target_fp_id.in_(old_ids))
                    )
                )
                await session.execute(
                    sa_del(FunctionalPoint).where(FunctionalPoint.requirement_id == req_id)
                )
            await session.flush()
            await _save_fps_to_db(session, req_id, req_agent.last_fps, record.priority)
            await _save_fp_dependencies(session, req_id, req_agent.last_fp_dependencies)
            await session.commit()
            logger.info("[Clarify] 澄清后重新提取 FP: {} 个", len(req_agent.last_fps))
        except Exception as e:
            logger.warning("[Clarify] FP 重新提取失败（不阻断）: {}", str(e)[:200])

    return ApiResponse(
        success=True,
        message="澄清完成，需求已确认",
        data={
            "req_id": req_id,
            "srr": final_data,
            "status": "active",
            "answers_count": len(req.answers),
        },
    )


# ============================================================================
# 5b. 确认生成最终 SRR（v2.7：KB 已解答时跳过人工，直接生成）
# ============================================================================

@router.post("/{req_id}/finalize", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def finalize_requirement(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """确认需求分析结果并生成最终 SRR。

    KB 已解答全部问题 → 将 KB 补全信息融合进原始需求文本 → ReqAgent 重解析 → 生成最终 SRR。
    无需人工介入，直接标记为 active。

    Args:
        req_id: 需求 ID
        session: 数据库会话
    """
    from app.agents.req_agent import req_agent

    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")
    if record.status != "draft":
        raise HTTPException(status_code=400, detail="只能对 draft 状态的需求执行此操作")

    srr = record.srr_json or {}
    raw_text = record.raw_text or ""

    # 收集 KB 已解答的补全信息
    kb_augments = (srr.get("_clarify_state", {}) or {}).get("auto_augmentations", [])
    if kb_augments:
        parts = [raw_text, "\n\n## KB 自动补全信息\n"]
        for aug in kb_augments:
            parts.append(f"- {aug.get('field', '')}: {str(aug.get('value', ''))[:300]}")
        enhanced_raw = "\n".join(parts)
    else:
        enhanced_raw = raw_text

    logger.info("[API/requirements] 最终 SRR 生成: req_id={}, kb_augments={}", req_id, len(kb_augments))

    try:
        final_srr = await req_agent.parse(
            raw_requirement=enhanced_raw,
            product_id=record.product_id,
        )
    except Exception as e:
        logger.error("[API/requirements] 最终 SRR 生成失败: {}", str(e)[:200])
        # 降级：使用原始 SRR
        final_srr = dict(srr)
        final_srr.pop("_clarify_state", None)
        final_srr.pop("clarify_answers", None)
        final_srr.pop("_field_sources", None)
        final_srr["_finalize_degraded"] = True

    # 保留 KB 日志
    final_data = dict(final_srr)
    if original_logs := (dict(record.srr_json or {}).get("_kb_query_logs")):
        final_data["_kb_query_logs"] = original_logs
    # 保留澄清历史（finalize 也可能在已有历史的需求上执行）
    if existing_history := (dict(record.srr_json or {}).get("_clarify_history")):
        final_data["_clarify_history"] = existing_history
    # 合并旧 SRR 的 KB 基线补全内容
    old_srr = dict(record.srr_json or {})
    for field in ("_baseline_context", "additional_details"):
        old_val = old_srr.get(field)
        if old_val:
            if field == "additional_details":
                existing = final_data.get("additional_details", "")
                final_data["additional_details"] = str(old_val) + "\n" + str(existing)
            else:
                final_data[field] = old_val
    final_data.pop("_clarify_state", None)
    final_data.pop("clarify_answers", None)
    final_data.pop("_field_sources", None)

    record.srr_json = final_data
    record.status = "active"
    await session.commit()

    logger.info("[API/requirements] KB 确认完成: req_id={}, title={}", req_id, record.title)

    return ApiResponse(
        success=True,
        message="KB 已解答全部问题，最终 SRR 已生成",
        data={"req_id": req_id, "status": "active", "srr": final_data},
    )


# ============================================================================
# 5c. 澄清历史 + 重新生成
# ============================================================================

@router.get("/{req_id}/clarify/history", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_clarify_history(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取需求的澄清与重新生成历史。

    返回 _clarify_history 数组（按时间正序），每条记录含 timestamp / action /
    questions / answers / extra_notes / auto_augmentations / quality_report 等。
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    history = list((record.srr_json or {}).get("_clarify_history", []))
    history.reverse()  # 前端展示倒序（最新在前）

    return ApiResponse(
        success=True,
        message="ok",
        data={"items": history, "total": len(history)},
    )


@router.post("/{req_id}/regenerate", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def regenerate_requirement(
    req_id: str,
    req: RegenerateRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """补充信息后重新生成需求分析（SRR）。

    允许在 status=active（已澄清/已确认）后调用，用户通过 extra_notes 提供补充
    说明，系统将原始文本 + 已有 KB 补全 + 补充说明拼接后重新调用 ReqAgent 解析，
    生成新的 SRR。操作记录追加到 _clarify_history。

    Args:
        req_id: 需求 ID
        req: 含 extra_notes（用户补充说明）
        session: 数据库会话

    Returns:
        ApiResponse: data 含 req_id / srr（新 SRR）/ status
    """
    extra_notes = req.extra_notes.strip()
    if not extra_notes:
        raise HTTPException(status_code=400, detail="补充说明不能为空")

    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    raw_text = record.raw_text or ""
    old_srr = dict(record.srr_json or {})
    previous_title = (old_srr.get("requirement", {}) or {}).get("title", record.title)

    # 构建增强文本：原始文本 + 已有 KB 补全 + 用户补充说明
    enhanced_parts = [raw_text]

    kb_augments = (old_srr.get("_clarify_state", {}) or {}).get("auto_augmentations", [])
    if kb_augments:
        enhanced_parts.append("\n\n## KB 自动补全信息\n")
        for aug in kb_augments:
            enhanced_parts.append(f"- {aug.get('field', '')}: {str(aug.get('value', ''))[:300]}")

    enhanced_parts.append(f"\n\n## 补充说明（重新生成）\n{extra_notes}")
    enhanced_raw = "\n".join(enhanced_parts)

    logger.info(
        "[API/requirements] 重新生成 SRR: req_id={}, extra_notes_len={}, enhanced_len={}",
        req_id, len(extra_notes), len(enhanced_raw),
    )

    # 调用 ReqAgent 重新解析
    try:
        final_srr = await req_agent.parse(
            raw_requirement=enhanced_raw,
            product_id=record.product_id,
        )
        logger.info("[API/requirements] 重新生成 SRR 成功: req_id={}", req_id)
    except Exception as e:
        logger.error("[API/requirements] 重新生成 SRR 失败: {}", str(e)[:200])
        raise HTTPException(status_code=500, detail=f"重新生成失败: {str(e)[:200]}")

    # 记录历史
    new_title = (final_srr.get("requirement", {}) or {}).get("title", "")
    history_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "regenerate",
        "extra_notes": extra_notes,
        "previous_title": previous_title,
        "new_title": new_title,
    }
    clarify_history = list(old_srr.get("_clarify_history", []))
    clarify_history.append(history_entry)

    # 更新 DB
    final_data = dict(final_srr)
    # 保留 KB 查询日志
    if original_logs := old_srr.get("_kb_query_logs"):
        final_data["_kb_query_logs"] = original_logs
    # 保留澄清历史
    final_data["_clarify_history"] = clarify_history
    # 迁移 KB 基线补全内容
    for field in ("_baseline_context", "additional_details"):
        old_val = old_srr.get(field)
        if old_val:
            if field == "additional_details":
                existing = final_data.get("additional_details", "")
                final_data["additional_details"] = str(old_val) + "\n" + str(existing)
            else:
                final_data[field] = old_val

    record.srr_json = final_data
    # 重新生成后更新标题
    if new_title:
        record.title = new_title
    await session.commit()

    logger.info(
        "[API/requirements] 重新生成完成: req_id={}, title={}->{}",
        req_id, previous_title, new_title,
    )

    return ApiResponse(
        success=True,
        message="需求分析已重新生成",
        data={
            "req_id": req_id,
            "srr": final_data,
            "status": record.status,
            "previous_title": previous_title,
            "new_title": new_title,
        },
    )


# ============================================================================
# 6. 编辑需求（直接更新 SRR JSON）
# ============================================================================

@router.put("/{req_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def update_requirement(
    req_id: str,
    req: RequirementUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """更新需求的 SRR JSON（手动编辑）。

    用户可以直接修改 SRR 中的任意字段，修改后的 SRR 会立即持久化。
    适用于：
    - 修复 LLM 解析错误
    - 用户主动补充/修改字段内容
    - 应用澄清答案后微调

    Args:
        req_id: 需求 ID
        req: 更新请求，含 srr_json（必须）和 raw_text（可选）
        session: 数据库会话
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    # 只更新需要的字段
    record.srr_json = req.srr_json
    if req.raw_text is not None:
        record.raw_text = req.raw_text

    # 同步顶层元数据（从 SRR 同步）
    srr = req.srr_json
    req_data = srr.get("requirement", {})
    record.title = req_data.get("title", record.title)
    record.priority = req_data.get("priority", record.priority)
    record.modules = req_data.get("modules", record.modules)

    await session.commit()

    logger.info("[API/requirements] 需求已更新: req_id={}", req_id)

    return ApiResponse(
        success=True,
        message="需求已更新",
        data={"req_id": req_id},
    )


# ============================================================================
# 7. 删除需求（已归档不可删除）
# ============================================================================

@router.get("/{req_id}/delete-preview", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def delete_preview(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """预览删除需求将产生的级联影响。

    返回各类依赖记录的计数，供前端弹窗提示用户确认。
    """
    record = await session.get(Requirement, req_id)
    if not record:
        raise HTTPException(status_code=404, detail="需求不存在")

    from sqlalchemy import func as sql_func

    # 子表计数
    tc_count = await session.scalar(
        select(sql_func.count()).select_from(TestCase).where(TestCase.req_id == req_id)
    )
    gb_count = await session.scalar(
        select(sql_func.count()).select_from(GenerationBatch).where(GenerationBatch.req_id == req_id)
    )
    dep_count = await session.scalar(
        select(sql_func.count()).select_from(ReqDependency).where(
            (ReqDependency.source_req_id == req_id) | (ReqDependency.target_req_id == req_id)
        )
    )
    at_count = await session.scalar(
        select(sql_func.count()).select_from(AnalysisTask).where(AnalysisTask.req_id == req_id)
    )
    tr_count = await session.scalar(
        select(sql_func.count()).select_from(Traceability).where(Traceability.requirement_id == req_id)
    )
    fp_count = await session.scalar(
        select(sql_func.count()).select_from(FunctionalPoint).where(FunctionalPoint.requirement_id == req_id)
    )
    # superseded_by / supersedes
    super_count = await session.scalar(
        select(sql_func.count()).select_from(Requirement).where(
            (Requirement.superseded_by == req_id) | (Requirement.supersedes == req_id)
        )
    )

    return ApiResponse(success=True, message="ok", data={
        "req_id": req_id,
        "title": record.title,
        "status": record.status,
        "dependencies": {
            "test_cases": tc_count or 0,
            "generation_batches": gb_count or 0,
            "req_dependencies": dep_count or 0,
            "analysis_tasks": at_count or 0,
            "traceability": tr_count or 0,
            "functional_points": fp_count or 0,
            "superseded_refs": super_count or 0,
        },
    })


@router.delete("/{req_id}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def delete_requirement(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """删除需求。

    规则（v2.1 设计）：
    - active / superseded / deprecated 状态 → 允许删除（从 DB 永久移除）
    - archived 状态 → ❌ 拒绝删除（已归档进入知识库，删除会导致 KB 数据不一致）

    如果需要删除已归档需求，请先将其状态改为 active 后再删除。
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    if record.status == "archived":
        raise HTTPException(
            status_code=409,
            detail=(
                f"需求 {req_id} 已归档，无法删除。"
                "已归档的需求已进入知识库，删除会导致知识库数据不一致。"
                "如需删除，请先将状态改为「活动中」后再操作。"
            ),
        )

    logger.info("[API/requirements] 删除需求: req_id={}, status={}", req_id, record.status)

    # ---- 级联清理子表（无 ondelete=CASCADE 的 FK，显式删除）----
    from sqlalchemy import delete as sql_delete, update as sql_update

    # 测试用例
    tc_count = await session.execute(sql_delete(TestCase).where(TestCase.req_id == req_id))
    # 生成批次
    gb_count = await session.execute(sql_delete(GenerationBatch).where(GenerationBatch.req_id == req_id))
    # 需求间依赖
    dep_count = await session.execute(
        sql_delete(ReqDependency).where(
            (ReqDependency.source_req_id == req_id) | (ReqDependency.target_req_id == req_id)
        )
    )
    # 异步分析任务（req_id 可为 NULL，直接删除）
    at_count = await session.execute(sql_delete(AnalysisTask).where(AnalysisTask.req_id == req_id))
    # 需求-用例追溯
    tr_count = await session.execute(sql_delete(Traceability).where(Traceability.requirement_id == req_id))

    # 功能点（虽有 ondelete=CASCADE，但 FPDependency 也需清理）
    fp_result = await session.execute(
        select(FunctionalPoint.id).where(FunctionalPoint.requirement_id == req_id)
    )
    fp_ids = {r[0] for r in fp_result.fetchall()}
    if fp_ids:
        await session.execute(
            sql_delete(FPDependency).where(
                (FPDependency.source_fp_id.in_(fp_ids)) | (FPDependency.target_fp_id.in_(fp_ids))
            )
        )
        await session.execute(
            sql_delete(FunctionalPoint).where(FunctionalPoint.requirement_id == req_id)
        )

    # 清理其他需求对当前需求的引用（superseded_by / supersedes）
    await session.execute(
        sql_update(Requirement).where(Requirement.superseded_by == req_id).values(superseded_by=None)
    )
    await session.execute(
        sql_update(Requirement).where(Requirement.supersedes == req_id).values(supersedes=None)
    )

    logger.info(
        "[API/requirements] 清理子表: "
        "test_cases=%d, gen_batches=%d, req_deps=%d, analysis_tasks=%d, traceability=%d, fp=%d",
        tc_count.rowcount, gb_count.rowcount, dep_count.rowcount, at_count.rowcount, tr_count.rowcount, len(fp_ids),
    )

    await session.delete(record)
    await session.commit()

    # 同步删除向量库中的记录（避免产生孤儿向量）
    try:
        from app.db.milvus_store import vector_store
        await asyncio.to_thread(vector_store.delete_requirement_from_kb, req_id)
        logger.info("[API/requirements] 已同步删除向量库记录: req_id={}", req_id)
    except Exception as e:
        logger.warning("[API/requirements] 向量库删除失败(不阻断): req_id={}, error={}", req_id, str(e)[:100])

    return ApiResponse(
        success=True,
        message=f"需求 {req_id} 已删除",
        data={"deleted": True, "status": record.status},
    )


# ============================================================================
# 8. 归档需求（active → archived，数据导入知识库）
# ============================================================================

class ArchiveRequest(BaseModel):
    """归档请求体。"""
    go_live_date: str | None = None

@router.post("/{req_id}/archive", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def archive_requirement(
    req_id: str,
    body: ArchiveRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """归档需求。

    规则：
    - active 状态 → 允许归档
    - draft 状态 → 拒绝归档（需先完成澄清）
    - archived / deprecated 状态 → 拒绝归档（已归档或已废弃）
    - 归档后数据写入向量知识库，供后续需求检索参考
    - 上线时间（go_live_date）用于 KB 检索时区分最新需求

    Args:
        req_id: 需求 ID
        body: 归档请求体，含 go_live_date（YYYY-MM-DDTHH:MM 或 YYYY-MM-DD）
        session: 数据库会话

    Returns:
        ApiResponse: data 含 req_id / status
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    if record.status == "draft":
        raise HTTPException(
            status_code=409,
            detail="需求尚未完成澄清（draft 状态），请先完成澄清后再归档",
        )

    if record.status == "archived":
        raise HTTPException(
            status_code=409,
            detail=f"需求 {req_id} 已归档，无需重复归档",
        )

    if record.status == "deprecated":
        raise HTTPException(
            status_code=409,
            detail="已废弃的需求无法归档",
        )

    # 解析上线时间（支持 YYYY-MM-DDTHH:MM 或 YYYY-MM-DD）
    parsed_go_live = None
    if body and body.go_live_date:
        try:
            # 尝试解析为 datetime（含时分）
            parsed_go_live = datetime.fromisoformat(body.go_live_date)
        except ValueError:
            try:
                # 降级为 date（补零时时间）
                parsed_go_live = datetime.fromisoformat(body.go_live_date + "T00:00:00")
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"上线时间格式不正确: {body.go_live_date}，需为 YYYY-MM-DDTHH:MM 或 YYYY-MM-DD",
                )

    # 查询项目版本号（用于 KB 检索版本溯源）
    project_version = ""
    if record.project_id:
        proj_result = await session.execute(
            select(Project.version).where(Project.id == record.project_id)
        )
        proj_row = proj_result.scalar_one_or_none()
        if proj_row:
            project_version = proj_row

    # 更新状态 + 上线日期 + 归档时间（真实持久化，区别于 updated_at）
    old_status = record.status
    record.status = "archived"
    record.go_live_date = parsed_go_live
    record.archived_at = datetime.now(timezone.utc)
    await session.commit()

    # 写入向量知识库（供后续需求检索参考）
    # 注意：向量化是异步任务（Celery）。此处仅“派发”任务，不代表向量已落库。
    # 因此字段命名为 kb_task_queued（而非 kb_written），避免误导“已写入”。
    kb_task_queued = False
    kb_error = ""
    milvus_meta = {
        "product_id": record.product_id,
        "modules": ",".join(record.modules or []),
        "priority": record.priority,
        "status": "archived",
        "go_live_date": parsed_go_live.isoformat() if parsed_go_live else "",
        "project_version": project_version,
    }
    try:
        from app.tasks.celery_app import celery_app

        if celery_app is not None:
            # 异步双写：PG 已 commit，Celery 异步计算 Embedding + 写入 Milvus
            celery_app.send_task(
                "app.tasks.embedding_tasks.embed_and_store_requirement",
                args=[record.id, record.srr_json or {}, milvus_meta],
                queue="embedding",
            )
            kb_task_queued = True
            logger.info("[API/requirements] 异步写入任务已派发: req_id={}, go_live_date={}, project_version={}",
                        req_id, milvus_meta["go_live_date"], project_version)
        else:
            # Celery 未安装/未配置 → 降级为同步写入（同步即已落库）
            from app.db.milvus_store import vector_store
            await asyncio.to_thread(
                vector_store.add_requirement,
                req_id=record.id,
                srr_json=record.srr_json or {},
                metadata=milvus_meta,
            )
            kb_task_queued = True
            logger.info("[API/requirements] 已写入知识库(同步降级): req_id={}, go_live_date={}, project_version={}",
                        req_id, milvus_meta["go_live_date"], project_version)
    except Exception as e:
        kb_error = str(e)[:300]
        logger.warning("[API/requirements] 知识库写入失败（不阻断）: req_id={}, error={}", req_id, kb_error[:200])

    logger.info("[API/requirements] 归档成功: req_id={}, old_status={}, kb_task_queued={}", req_id, old_status, kb_task_queued)

    # v1.5.3: 归档时将 FP 及其依赖关系写入向量知识库
    fp_kb_count = 0
    try:
        async with AsyncSession() as fp_session:
            fp_result = await fp_session.execute(
                select(FunctionalPoint).where(FunctionalPoint.requirement_id == req_id)
            )
            all_fps = fp_result.scalars().all()
            for f in all_fps:
                fp_meta = {
                    "type": "functional_point",
                    "requirement_id": req_id,
                    "module_id": f.module_id or "",
                    "fp_type": f.fp_type,
                    "product_id": record.product_id,
                    "status": "archived",
                }
                # 构造 _srr_to_text 兼容的嵌入格式（复用子 SRR 的结构化字段）
                embed_srr = dict(f.srr_json) if isinstance(f.srr_json, dict) else {}
                embed_srr.setdefault("requirement", {})
                embed_srr["requirement"]["title"] = f.name
                if not embed_srr["requirement"].get("summary"):
                    embed_srr["requirement"]["summary"] = (
                        f.srr_json.get("requirement", {}).get("summary", "")
                        if isinstance(f.srr_json, dict) else ""
                    ) or f.name
                # 同步写入 Milvus
                try:
                    from app.db.milvus_store import vector_store
                    await asyncio.to_thread(
                        vector_store.add_requirement,
                        req_id=f"FP-{f.id}",
                        srr_json=embed_srr,
                        metadata=fp_meta,
                    )
                    fp_kb_count += 1
                except Exception as e:
                    logger.warning("[Archive] FP 写入 Milvus 失败 fp_id={}: {}", f.id, str(e)[:150])
    except Exception as e:
        logger.warning("[Archive] FP KB 写入失败（不阻断）: {}", str(e)[:200])

    message = "需求已归档，知识库写入任务已异步派发" if kb_task_queued else "需求已归档，但知识库写入任务派发失败（请检查 Celery 是否运行 / 嵌入模型是否就绪）"
    return ApiResponse(
        success=True,
        message=message,
        data={
            "req_id": req_id,
            "status": "archived",
            "kb_task_queued": kb_task_queued,
            "kb_error": kb_error if not kb_task_queued else "",
            "go_live_date": parsed_go_live.isoformat() if parsed_go_live else "",
            "project_version": project_version,
        },
    )


# ============================================================================
# 8.5 修改上线时间（已归档需求）
# ============================================================================

class UpdateGoLiveDateRequest(BaseModel):
    """修改上线时间请求体。"""
    go_live_date: str


@router.patch("/{req_id}/go-live-date", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def update_go_live_date(
    req_id: str,
    body: UpdateGoLiveDateRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """修改已归档需求的上线时间。

    上线时间随时可能变更，此接口允许修改已归档需求的 go_live_date，
    并同步更新 Milvus 向量库中的元数据（用于 KB 检索时按上线时间排序）。

    Args:
        req_id: 需求 ID
        body: 含 go_live_date（YYYY-MM-DDTHH:MM 或 YYYY-MM-DD）
        session: 数据库会话

    Returns:
        ApiResponse: data 含 req_id / go_live_date / milvus_updated
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    if record.status != "archived":
        raise HTTPException(
            status_code=409,
            detail=f"仅已归档需求可修改上线时间（当前状态: {record.status}）",
        )

    # 解析上线时间
    try:
        parsed_go_live = datetime.fromisoformat(body.go_live_date)
    except ValueError:
        try:
            parsed_go_live = datetime.fromisoformat(body.go_live_date + "T00:00:00")
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"上线时间格式不正确: {body.go_live_date}，需为 YYYY-MM-DDTHH:MM 或 YYYY-MM-DD",
            )

    old_go_live = record.go_live_date
    record.go_live_date = parsed_go_live
    await session.commit()

    logger.info(
        "[API/requirements] 上线时间已修改: req_id={}, {} → {}",
        req_id,
        old_go_live.isoformat() if old_go_live else "(空)",
        parsed_go_live.isoformat(),
    )

    # 同步更新 Milvus 向量库中的元数据
    milvus_updated = False
    milvus_error = ""
    try:
        from app.db.milvus_store import vector_store
        await asyncio.to_thread(
            vector_store.update_requirement_metadata,
            req_id,
            {"go_live_date": parsed_go_live.isoformat()},
        )
        milvus_updated = True
    except Exception as e:
        milvus_error = str(e)[:300]
        logger.warning(
            "[API/requirements] Milvus 元数据更新失败（不阻断）: req_id={}, error={}",
            req_id, milvus_error[:200],
        )

    return ApiResponse(
        success=True,
        message="上线时间已更新" + ("，向量库元数据已同步" if milvus_updated else "，但向量库元数据同步失败"),
        data={
            "req_id": req_id,
            "go_live_date": parsed_go_live.isoformat(),
            "milvus_updated": milvus_updated,
            "milvus_error": milvus_error if not milvus_updated else "",
        },
    )


# ============================================================================
# 9. 影响分析（基于模块依赖图谱）
# ============================================================================

@router.get("/{req_id}/impact", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def analyze_requirement_impact(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """分析需求变更的影响范围。

    基于需求关联的模块 + 模块依赖图谱 BFS 遍历，找到：
    - 直接涉及的模块
    - 间接影响的模块（通过依赖关系传播）
    - 受影响的其他需求

    Args:
        req_id: 需求 ID
        session: 数据库会话

    Returns:
        ApiResponse: data 含 direct_modules / indirect_modules / affected_requirements
    """
    # 1. 查询需求
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    # 2. 获取需求的关联模块
    req_modules = record.modules or []
    req_title = record.title
    req_product = record.product_id

    if not req_modules:
        return ApiResponse(
            success=True,
            message="该需求未关联模块，无法分析影响范围",
            data={
                "req_id": req_id,
                "req_title": req_title,
                "direct_modules": [],
                "indirect_modules": [],
                "affected_requirements": [],
            },
        )

    # 3. 调用 ImpactAnalysisAgent 的 BFS 分析
    from app.agents.impact_agent import impact_agent

    srr = record.srr_json or {}
    srr["requirement"] = srr.get("requirement", {}) or {}
    srr["requirement"]["id"] = req_id
    srr["requirement"]["product_id"] = req_product
    srr["requirement"]["modules"] = req_modules

    try:
        impact = await impact_agent._analyze_module_impact(req_modules, session)
    except Exception as e:
        logger.error("[API/requirements] 影响分析 BFS 失败: {}", str(e)[:200])
        raise HTTPException(status_code=500, detail=f"影响分析失败: {str(e)[:200]}")

    # 4. 查询受影响模块下的其他需求
    all_affected = impact.get("all_affected_modules", [])
    affected_reqs: list[dict] = []
    if all_affected:
        # 排除当前需求自身
        stmt = select(Requirement).where(
            Requirement.product_id == req_product,
            Requirement.id != req_id,
        )
        result = await session.execute(stmt)
        all_reqs = result.scalars().all()

        for r in all_reqs:
            r_modules = r.modules or []
            if any(m in all_affected for m in r_modules):
                affected_reqs.append({
                    "id": r.id,
                    "title": r.title,
                    "priority": r.priority,
                    "modules": [m for m in r_modules if m in all_affected],
                })

    # 5. 查询模块名称，并过滤孤儿依赖（模块已删除但依赖记录还在）
    mod_names: dict[str, str] = {}
    valid_ids: set[str] = set()
    if all_affected:
        from app.db.models import Module as ModuleModel
        mod_stmt = select(ModuleModel.id, ModuleModel.name).where(ModuleModel.id.in_(all_affected))
        mod_result = await session.execute(mod_stmt)
        mod_names = {row[0]: row[1] for row in mod_result.all()}
        valid_ids = set(mod_names.keys())

    return ApiResponse(
        success=True,
        message=f"影响分析完成：{len(impact.get('direct_modules', []))} 个直接模块，"
                f"{len(impact.get('indirect_modules', []))} 个间接模块，"
                f"{len(affected_reqs)} 个受影响需求",
        data={
            "req_id": req_id,
            "req_title": req_title,
            "direct_modules": [
                {"id": m, "name": mod_names.get(m, f"已删除模块({m})")}
                for m in impact.get("direct_modules", [])
                if m in valid_ids
            ],
            "indirect_modules": [
                {
                    "id": m.get("module_id", ""),
                    "name": mod_names.get(m.get("module_id", ""), f"已删除模块({m.get('module_id', '')})"),
                    "reason": m.get("reason", ""),
                    "depth": m.get("depth", 1),
                }
                for m in impact.get("indirect_modules", [])
                if m.get("module_id", "") in valid_ids
            ],
            "affected_requirements": affected_reqs,
        },
    )


# ============================================================================
# v1.4 功能点（FunctionalPoint）API
# ============================================================================

from app.core import prompt_manager as pm
from app.core.llm_client import llm_client
from app.utils.id_gen import gen_requirement_id  # 复用 ID 生成


class FPExtractBPRequest(BaseModel):
    """BP 提取请求。"""
    req_id: str


class FPSplitRequest(BaseModel):
    """FP 拆分请求。bp_names 为空时 = 全量重新拆分（删除旧 FP，从原始文档重新生成）。"""
    req_id: str
    bp_names: list[str] = []  # 空列表→重新拆分模式


class FPUpdateRequest(BaseModel):
    """FP 编辑请求。"""
    name: str | None = None
    module_id: str | None = None
    fp_type: str | None = None
    priority: str | None = None


class FPDependencyCreateRequest(BaseModel):
    """FP 依赖创建请求。"""
    source_fp_id: str
    target_fp_id: str
    relation_type: str = "depends_on"
    description: str = ""


async def _load_active_requirement(req_id: str, session: AsyncSession) -> Requirement:
    """加载需求并校验非归档状态。归档后的功能点应只读。"""
    req = await session.get(Requirement, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="需求不存在")
    if req.status == "archived":
        raise HTTPException(
            status_code=409,
            detail="需求已归档，功能点已锁定，不可执行此操作。归档后的功能点仅支持查看。",
        )
    return req


@router.post("/{req_id}/fp/extract-bp", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def fp_extract_bp(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """从需求 SRR 提取候选业务点（BP）。归档后不可用。"""
    req = await _load_active_requirement(req_id, session)
    srr = req.srr_json or {}
    modules = ", ".join(req.modules or [])

    # 精简 SRR 去掉大字段
    srr_snippet = {
        k: v for k, v in srr.items()
        if k not in ("_raw_text", "additional_details", "assumptions", "dependencies", "api_definitions", "data_model", "relationships", "non_functional", "ui_rules", "user_roles")
    }
    import json as _json
    snippet_text = _json.dumps(srr_snippet, ensure_ascii=False)[:8000]

    prompt = pm.get("fp_agent", "FP_EXTRACT_BP_USER_PROMPT").format(
        req_title=req.title, modules=modules, srr_snippet=snippet_text,
    )

    try:
        result = await llm_client.call_json(
            prompt=prompt,
            system_prompt=pm.get("fp_agent", "FP_EXTRACT_BP_SYSTEM_PROMPT"),
            schema_hint={"business_points": list},
            temperature=0.2,
        )
        return ApiResponse(success=True, message="ok", data=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"BP 提取失败: {str(e)[:200]}")


@router.post("/{req_id}/fp/split", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def fp_split(
    req_id: str,
    body: FPSplitRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """在选中的 BP 范围内拆分功能点并持久化。bp_names 为空时全量重新拆分。归档后不可用。"""
    req = await _load_active_requirement(req_id, session)
    srr = req.srr_json or {}

    # v1.5.1: "重新拆分"模式 — 删除旧 FP + 从原始文档一体式重新生成
    if not body.bp_names or len(body.bp_names) == 0:
        from sqlalchemy import delete as sa_delete

        logger.info("[FP] 全量重新拆分: req_id={}", req_id)
        # 删除旧 FP + 依赖
        old_fps = await session.execute(
            select(FunctionalPoint.id).where(FunctionalPoint.requirement_id == req_id)
        )
        old_ids = {r[0] for r in old_fps.fetchall()}
        if old_ids:
            await session.execute(
                sa_delete(FPDependency).where(
                    (FPDependency.source_fp_id.in_(old_ids)) | (FPDependency.target_fp_id.in_(old_ids))
                )
            )
            await session.execute(
                sa_delete(FunctionalPoint).where(FunctionalPoint.requirement_id == req_id)
            )
        await session.flush()
        # 调用 ReqAgent 一体式提取
        raw_text = req.raw_text or ""
        from app.agents.req_agent import req_agent as _agent
        product_name = srr.get("requirement", {}).get("product_name", "") or ""
        try:
            new_fps = await _agent._fp_extract_from_raw(raw_text, srr, product_name)
        except Exception as e:
            logger.error("[FP] 重新拆分失败: {}", str(e)[:200])
            raise HTTPException(status_code=500, detail=f"重新拆分失败: {str(e)[:200]}")
        saved = await _save_fps_to_db(session, req_id, new_fps, req.priority)
        await _save_fp_dependencies(session, req_id, _agent.last_fp_dependencies)
        await session.commit()
        return ApiResponse(
            success=True,
            message=f"已重新拆分 {saved} 个功能点",
            data={"functional_points": [
                {"id": "", "name": fp.get("name", ""), "fp_type": fp.get("fp_type", "workflow"), "module_id": None}
                for fp in new_fps
            ]},
        )

    # 精简 SRR
    srr_snippet = {
        k: v for k, v in srr.items()
        if k not in ("_raw_text", "additional_details", "assumptions", "dependencies", "api_definitions", "data_model", "relationships", "non_functional", "ui_rules", "user_roles")
    }
    import json as _json
    snippet_text = _json.dumps(srr_snippet, ensure_ascii=False)[:6000]

    all_fps = []
    for bp_name in body.bp_names:
        prompt = pm.get("fp_agent", "FP_SPLIT_USER_PROMPT").format(
            bp_name=bp_name, bp_summary="", req_title=req.title, srr_snippet=snippet_text,
        )
        try:
            result = await llm_client.call_json(
                prompt=prompt,
                system_prompt=pm.get("fp_agent", "FP_SPLIT_SYSTEM_PROMPT"),
                schema_hint={"functional_points": list},
                temperature=0.2,
            )
            all_fps.extend(result.get("functional_points", []))
        except Exception:
            continue

    created = []
    for fp in all_fps:
        fp_id = gen_requirement_id()
        record = FunctionalPoint(
            id=fp_id,
            requirement_id=req_id,
            module_id=fp.get("suggested_module_id"),
            name=fp.get("name", "未命名功能点"),
            fp_type=fp.get("fp_type", "workflow"),
            priority=req.priority,
            srr_json={
                "happy_path": fp.get("happy_path", ""),
                "alternate_flows": fp.get("alternate_flows", ""),
                "business_rules": fp.get("business_rules", []),
                "triggers": fp.get("triggers", ""),
                "expected_results": fp.get("expected_results", ""),
                "boundary_elements": fp.get("boundary_elements", ""),
                "summary": fp.get("summary", ""),
            },
            atomicity_check=fp.get("atomicity_check", {}),
            test_hints=fp.get("test_hints", []),
        )
        session.add(record)
        created.append({
            "id": fp_id,
            "name": record.name,
            "fp_type": record.fp_type,
            "module_id": record.module_id,
        })

    # 关联分析
    if len(created) >= 2:
        fp_list_text = "\n".join(
            f"- {f['name']}" for f in created
        )
        try:
            assoc_result = await llm_client.call_json(
                prompt=pm.get("fp_agent", "FP_ASSOCIATE_USER_PROMPT").format(
                    req_title=req.title, fp_list=fp_list_text,
                ),
                system_prompt=pm.get("fp_agent", "FP_ASSOCIATE_SYSTEM_PROMPT"),
                schema_hint={"dependencies": list},
                temperature=0.2,
            )
            id_map = {f["name"]: f["id"] for f in created}
            deps_added = 0
            for dep in assoc_result.get("dependencies", []):
                src_id = id_map.get(dep.get("source_fp_name", ""))
                tgt_id = id_map.get(dep.get("target_fp_name", ""))
                if src_id and tgt_id and src_id != tgt_id:
                    session.add(FPDependency(
                        source_fp_id=src_id,
                        target_fp_id=tgt_id,
                        relation_type=dep.get("relation_type", "depends_on"),
                        description=dep.get("description", ""),
                    ))
                    deps_added += 1
            logger.info("[FP] 关联分析完成: {} 条依赖", deps_added)
        except Exception as e:
            logger.warning("[FP] 关联分析失败（不阻断拆分）: {}", str(e)[:200])

    await session.commit()
    return ApiResponse(
        success=True,
        message=f"已拆分 {len(created)} 个功能点",
        data={"functional_points": created, "total": len(created)},
    )


@router.get("/{req_id}/fp", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def fp_list(req_id: str, session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """获取需求的功能点列表。"""
    req = await session.get(Requirement, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="需求不存在")

    result = await session.execute(
        select(FunctionalPoint).where(
            FunctionalPoint.requirement_id == req_id
        ).order_by(FunctionalPoint.created_at)
    )
    fps = result.scalars().all()
    return ApiResponse(success=True, message="ok", data={
        "items": [
            {
                "id": fp.id, "name": fp.name, "fp_type": fp.fp_type,
                "module_id": fp.module_id, "priority": fp.priority, "status": fp.status,
                "srr_json": fp.srr_json, "atomicity_check": fp.atomicity_check,
                "test_hints": fp.test_hints,
                "vectorization_status": fp.vectorization_status,
                "created_at": fp.created_at.isoformat() if fp.created_at else "",
            }
            for fp in fps
        ],
        "total": len(fps),
        "is_archived": req.status == "archived",
    })


@router.get("/{req_id}/fp/dependencies", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def fp_dependencies(req_id: str, session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """获取功能点依赖关系。"""
    fp_ids_result = await session.execute(
        select(FunctionalPoint.id).where(FunctionalPoint.requirement_id == req_id)
    )
    fp_ids = {r[0] for r in fp_ids_result.fetchall()}

    dep_result = await session.execute(
        select(FPDependency).where(FPDependency.source_fp_id.in_(fp_ids))
    )
    deps = dep_result.scalars().all()

    # 获取名称映射
    all_ids = {d.source_fp_id for d in deps} | {d.target_fp_id for d in deps}
    name_map = {}
    if all_ids:
        names_result = await session.execute(
            select(FunctionalPoint.id, FunctionalPoint.name).where(FunctionalPoint.id.in_(all_ids))
        )
        name_map = {row[0]: row[1] for row in names_result.fetchall()}

    return ApiResponse(success=True, message="ok", data={
        "dependencies": [
            {
                "id": d.id, "source_fp_id": d.source_fp_id,
                "source_name": name_map.get(d.source_fp_id, ""),
                "target_fp_id": d.target_fp_id,
                "target_name": name_map.get(d.target_fp_id, ""),
                "relation_type": d.relation_type,
                "description": d.description,
            }
            for d in deps
        ]
    })


# FP 依赖合法关系类型（与 FPDependency 模型注释一致）
_FP_RELATION_TYPES = {"depends_on", "data_flows", "invariants", "lifecycle_scenarios", "conflicts_with"}


@router.post("/{req_id}/fp/dependencies", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_add_dependency(
    req_id: str,
    body: FPDependencyCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """创建功能点依赖关系。

    Note:
        - source_fp 必须属于该 req_id，且 req 未归档
        - target_fp 可属于同产品下任意需求（支持跨需求依赖）
        - relation_type 必须为 5 种合法值之一
    """
    await _load_active_requirement(req_id, session)

    # 校验关系类型
    if body.relation_type not in _FP_RELATION_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"relation_type 必须为 {', '.join(sorted(_FP_RELATION_TYPES))} 之一",
        )

    # 不自引用
    if body.source_fp_id == body.target_fp_id:
        raise HTTPException(status_code=400, detail="source 和 target 不能为同一功能点")

    # 校验 source 属于该 req_id
    source_fp = await session.get(FunctionalPoint, body.source_fp_id)
    if not source_fp or source_fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="source 功能点不存在或不属于该需求")

    # 校验 target 存在且同产品
    target_fp = await session.get(FunctionalPoint, body.target_fp_id)
    if not target_fp:
        raise HTTPException(status_code=404, detail="target 功能点不存在")

    # 同产品校验
    source_req = await session.get(Requirement, source_fp.requirement_id)
    target_req = await session.get(Requirement, target_fp.requirement_id)
    if not source_req or not target_req or source_req.product_id != target_req.product_id:
        raise HTTPException(status_code=400, detail="source 和 target 功能点必须属于同一产品")

    # 查重
    dup = await session.execute(
        select(FPDependency).where(
            FPDependency.source_fp_id == body.source_fp_id,
            FPDependency.target_fp_id == body.target_fp_id,
            FPDependency.relation_type == body.relation_type,
        )
    )
    if dup.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="该依赖关系已存在")

    dep = FPDependency(
        source_fp_id=body.source_fp_id,
        target_fp_id=body.target_fp_id,
        relation_type=body.relation_type,
        description=body.description,
    )
    session.add(dep)
    await session.commit()
    await session.refresh(dep)

    logger.info("[FP] 依赖创建: {} -{}-> {} (req={})",
                body.source_fp_id, body.relation_type, body.target_fp_id, req_id)

    return ApiResponse(success=True, message="依赖关系已创建", data={
        "id": dep.id, "source_fp_id": dep.source_fp_id,
        "target_fp_id": dep.target_fp_id, "relation_type": dep.relation_type,
    })


@router.delete("/{req_id}/fp/dependencies/{dep_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_remove_dependency(
    req_id: str,
    dep_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """删除功能点依赖关系。归档后不可用。"""
    await _load_active_requirement(req_id, session)
    dep = await session.get(FPDependency, dep_id)
    if not dep:
        raise HTTPException(status_code=404, detail="依赖关系不存在")

    # 校验 source_fp 属于该 req_id
    source_fp = await session.get(FunctionalPoint, dep.source_fp_id)
    if not source_fp or source_fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="依赖关系不属于该需求")

    await session.delete(dep)
    await session.commit()

    logger.info("[FP] 依赖删除: dep_id={} (req={})", dep_id, req_id)
    return ApiResponse(success=True, message="依赖关系已删除")


# ============================================================================
# v3.0: FP 影响功能说明（自动生成 + 可编辑，写入 SRR impact_analysis 字段）
# ============================================================================

class ImpactDescriptionUpdateRequest(BaseModel):
    """FP 影响说明编辑保存请求。"""
    description: str
    test_focus: str = ""


@router.post("/{req_id}/fp/{fp_id}/impact-description", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_generate_impact_description(
    req_id: str,
    fp_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """自动生成功能点影响说明并写入需求 SRR。

    流程：
        1. 校验 FP 存在且属于该 req_id
        2. 调用 ImpactAnalysisAgent._analyze_fp_impact()（FP 反向 BFS + 模块 BFS + LLM 合成）
        3. 读需求 SRR，追加/覆盖 impact_analysis 数组中同 fp_id 的条目
        4. 写回 DB，返回生成结果

    Args:
        req_id: 需求 ID
        fp_id: 功能点 ID
        session: 数据库会话

    Returns:
        ApiResponse: data 含完整的影响分析结果
    """
    # 1. 校验 FP 属于该 req_id
    fp = await session.get(FunctionalPoint, fp_id)
    if not fp:
        raise HTTPException(status_code=404, detail="功能点不存在")
    if fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="功能点不属于该需求")

    # 2. 调用 FP 影响分析
    from app.agents.impact_agent import impact_agent

    try:
        impact_result = await impact_agent._analyze_fp_impact(fp_id, session)
    except Exception as e:
        logger.error("[API/requirements] FP 影响分析失败 fp_id={}: {}", fp_id, str(e)[:200])
        raise HTTPException(status_code=500, detail=f"影响分析失败: {str(e)[:200]}")

    # 3. 写入需求 SRR 的 impact_analysis 字段
    req = await session.get(Requirement, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="需求不存在")

    srr = dict(req.srr_json or {})
    impact_list = list(srr.get("impact_analysis", []))

    # 同 fp_id 覆盖旧条目
    impact_list = [item for item in impact_list if item.get("fp_id") != fp_id]
    impact_list.append(impact_result)

    srr["impact_analysis"] = impact_list
    req.srr_json = srr

    await session.commit()

    logger.info(
        "[API/requirements] FP 影响说明已生成并写入 SRR: req_id={}, fp_id={}, affected_fps={}, affected_modules={}",
        req_id, fp_id,
        len(impact_result.get("affected_fps", [])),
        len(impact_result.get("affected_modules", [])),
    )

    return ApiResponse(
        success=True,
        message="影响说明已生成并保存到需求 SRR",
        data=impact_result,
    )


@router.put("/{req_id}/impact-description/{fp_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_update_impact_description(
    req_id: str,
    fp_id: str,
    body: ImpactDescriptionUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """编辑保存功能点影响说明（用户手动修改后的持久化）。

    Args:
        req_id: 需求 ID
        fp_id: 功能点 ID
        body: 含 description / test_focus
        session: 数据库会话

    Returns:
        ApiResponse: 确认保存成功
    """
    req = await session.get(Requirement, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="需求不存在")

    srr = dict(req.srr_json or {})
    impact_list = list(srr.get("impact_analysis", []))

    # 查找匹配条目
    found = False
    for item in impact_list:
        if item.get("fp_id") == fp_id:
            item["description"] = body.description
            if body.test_focus:
                item["test_focus"] = body.test_focus
            item["is_auto_generated"] = False
            found = True
            break

    if not found:
        raise HTTPException(
            status_code=404,
            detail=f"未找到 fp_id={fp_id} 的影响说明条目，请先调用 POST 生成",
        )

    srr["impact_analysis"] = impact_list
    req.srr_json = srr

    await session.commit()

    logger.info("[API/requirements] FP 影响说明已更新: req_id={}, fp_id={}", req_id, fp_id)

    return ApiResponse(success=True, message="影响说明已保存")


# ============================================================================
# v1.5.3: FP 合并（多选 → LLM 整理 → 合并为一个 FP）
# ============================================================================

class FPMergeRequest(BaseModel):
    fp_ids: list[str]  # 至少 2 个
    merged_name: str = ""  # 合并后功能点名称（为空则 LLM 自动生成）


@router.post("/{req_id}/fp/merge", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_merge(
    req_id: str,
    body: FPMergeRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """将多个功能点合并为一个，LLM 重新整理子 SRR 保证功能不遗漏。"""
    if len(body.fp_ids) < 2:
        raise HTTPException(status_code=400, detail="至少需要 2 个功能点才能合并")

    await _load_active_requirement(req_id, session)

    # 加载所有 FP
    fps_to_merge = []
    for fid in body.fp_ids:
        fp = await session.get(FunctionalPoint, fid)
        if not fp or fp.requirement_id != req_id:
            raise HTTPException(status_code=404, detail=f"功能点 {fid} 不存在或不属于该需求")
        fps_to_merge.append(fp)

    if len(fps_to_merge) < 2:
        raise HTTPException(status_code=400, detail="有效功能点不足，无法合并")

    # 用 LLM 整理合并后的子 SRR — 序列化完整子 SRR 避免字段遗漏
    import json as _j
    fp_jsons = []
    for fp in fps_to_merge:
        d = {
            "name": fp.name,
            "fp_type": fp.fp_type,
            "test_hints": fp.test_hints or [],
        }
        if isinstance(fp.srr_json, dict):
            d["sub_srr"] = fp.srr_json
        fp_jsons.append(d)

    custom_name_hint = f"\n\n**用户指定的合并后名称：{body.merged_name}**" if body.merged_name else ""

    merge_prompt = f"""将以下多个功能点合并为一个完整的功能点。

这些功能点原本属于同一个业务功能，因需求拆解过细被分成了多个。请按标准 SRR 格式重新生成一份**完整**的子 SRR，保证所有功能细节不遗漏。{custom_name_hint}

合并规则：
- 如果上面提供了用户指定名称，使用该名称；否则取一个最能概括所有子功能点的名称
- fp_type 选最匹配的（workflow/constraint/calculation/api_behavior/data_record/config）
- flows.happy_path.steps：将各子 FP 的步骤整合为一条完整的步骤链（order/action/expect）
- flows.alternate_flows：所有异常分支合并且去重（name/trigger/expect）
- boundary_conditions：保留所有边界条件
- business_rules：保留所有业务规则（id/rule/type），去重
- data_constraints：保留所有字段约束（field/type/required/constraints/example），去重
- acceptance_criteria：保留所有验收标准（id/description/type），去重
- preconditions：取并集去重
- test_hints：合并去重

原始功能点（完整子 SRR）：
{_j.dumps(fp_jsons, ensure_ascii=False, indent=2)}

请输出合并后的功能点 JSON。子 SRR 必须使用标准格式（requirement/flows.happy_path.steps/flows.alternate_flows/boundary_conditions/business_rules/data_constraints/acceptance_criteria/preconditions）。"""

    try:
        merged = await llm_client.call_json(
            prompt=merge_prompt,
            system_prompt=pm.get("fp_agent", "FP_INTEGRATED_SYSTEM_PROMPT"),
            schema_hint={"name": str, "fp_type": str, "summary": str, "suggested_module": str, "sub_srr": dict, "test_hints": list},
            temperature=0.2,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"合并失败：LLM 调用出错：{str(e)[:200]}")

    # 创建合并后的 FP
    merged_id = gen_requirement_id()
    primary = fps_to_merge[0]  # 以第一个 FP 的模块归属为准

    merged_fp = FunctionalPoint(
        id=merged_id,
        requirement_id=req_id,
        module_id=primary.module_id,
        name=body.merged_name or merged.get("name", "合并功能点"),
        fp_type=merged.get("fp_type", primary.fp_type),
        priority=primary.priority,
        status="pending_review",  # 合并后需重新审核
        srr_json=merged.get("sub_srr", {}),
        test_hints=merged.get("test_hints", []),
        atomicity_check={"trigger": True, "result": True, "boundary": True},
    )
    session.add(merged_fp)

    # 删除旧 FP（先删依赖）
    old_ids = {fp.id for fp in fps_to_merge}
    from sqlalchemy import delete as _del
    await session.execute(
        _del(FPDependency).where(
            (FPDependency.source_fp_id.in_(old_ids)) | (FPDependency.target_fp_id.in_(old_ids))
        )
    )
    for fp in fps_to_merge:
        await session.delete(fp)

    await session.commit()
    logger.info("[FP] 合并完成: {} → {}, req={}", [fp.name for fp in fps_to_merge], merged_fp.name, req_id)
    return ApiResponse(
        success=True,
        message=f"已将 {len(fps_to_merge)} 个功能点合并为「{merged_fp.name}」",
        data={"id": merged_fp.id, "name": merged_fp.name, "fp_type": merged_fp.fp_type},
    )


@router.patch("/{req_id}/fp/{fp_id}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def fp_update(
    req_id: str, fp_id: str, body: FPUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """编辑功能点（名称/模块/类型/优先级）。归档后不可用。"""
    await _load_active_requirement(req_id, session)
    fp = await session.get(FunctionalPoint, fp_id)
    if not fp or fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="功能点不存在")
    if body.name is not None:
        fp.name = body.name
    if body.module_id is not None:
        fp.module_id = body.module_id
    if body.fp_type is not None:
        fp.fp_type = body.fp_type
    if body.priority is not None:
        fp.priority = body.priority
    await session.commit()
    return ApiResponse(success=True, message="功能点已更新")


@router.get("/{req_id}/fp/search-names", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def fp_search_names(
    req_id: str, q: str = Query(min_length=1),
    product_id: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """搜索已有功能点名称（跨需求复用命名）。"""
    stmt = select(FunctionalPoint.id, FunctionalPoint.name, FunctionalPoint.requirement_id).where(
        FunctionalPoint.name.ilike(f"%{q}%")
    ).order_by(FunctionalPoint.created_at.desc()).limit(20)

    if product_id:
        req_ids_result = await session.execute(
            select(Requirement.id).where(Requirement.product_id == product_id)
        )
        req_ids = {r[0] for r in req_ids_result.fetchall()}
        if req_ids:
            stmt = stmt.where(FunctionalPoint.requirement_id.in_(req_ids))

    result = await session.execute(stmt)
    rows = result.fetchall()
    return ApiResponse(success=True, message="ok", data={
        "names": [{"id": r[0], "name": r[1], "req_id": r[2]} for r in rows],
    })


@router.delete("/{req_id}/fp/{fp_id}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def fp_delete(req_id: str, fp_id: str, session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """删除功能点。归档后不可用。"""
    await _load_active_requirement(req_id, session)
    fp = await session.get(FunctionalPoint, fp_id)
    if not fp or fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="功能点不存在")
    # 删除关联依赖
    dep_result = await session.execute(
        select(FPDependency).where(
            (FPDependency.source_fp_id == fp_id) | (FPDependency.target_fp_id == fp_id)
        )
    )
    for dep in dep_result.scalars().all():
        await session.delete(dep)
    await session.delete(fp)
    await session.commit()
    return ApiResponse(success=True, message="功能点已删除")


# ============================================================================
# v1.5.1: FP 审核 + AI 重生成
# ============================================================================

class FPRegenerateRequest(BaseModel):
    """单 FP AI 重生成请求。"""
    hint: str = ""  # 用户补充说明（可选，引导 LLM 调整内容）


@router.patch("/{req_id}/fp/{fp_id}/approve", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_approve(
    req_id: str, fp_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """审核通过单个功能点。归档后不可用。"""
    await _load_active_requirement(req_id, session)
    fp = await session.get(FunctionalPoint, fp_id)
    if not fp or fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="功能点不存在")
    if fp.status == "approved":
        return ApiResponse(success=True, message="该功能点已审核通过")
    fp.status = "approved"
    await session.commit()
    logger.info("[FP] 审核通过: fp_id={}, name={}", fp_id, fp.name)
    return ApiResponse(success=True, message=f"功能点「{fp.name}」已审核通过")


@router.patch("/{req_id}/fp/{fp_id}/regenerate", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_regenerate(
    req_id: str, fp_id: str,
    body: FPRegenerateRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """AI 重新生成单个功能点的子 SRR。可传入 hint 引导生成方向。归档后不可用。"""
    req = await _load_active_requirement(req_id, session)
    fp = await session.get(FunctionalPoint, fp_id)
    if not fp or fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="功能点不存在")

    hint = body.hint if body and body.hint else ""
    srr = req.srr_json or {}
    raw_text = req.raw_text or ""

    from app.core import prompt_manager as pm2
    regenerate_prompt = f"""重新生成以下功能点的子 SRR。

原始需求：{srr.get("requirement", {}).get("title", req.title)}
功能点名称：{fp.name}
当前功能点类型：{fp.fp_type}
{f'用户补充说明：{hint}' if hint else ''}

原始需求文档（参考）：
{raw_text[:8000]}

请严格按照一体式 FP 拆分的输出格式，只输出这一个功能点的完整 JSON（含 sub_srr）。"""

    try:
        result = await llm_client.call_json(
            prompt=regenerate_prompt,
            system_prompt=pm.get("fp_agent", "FP_INTEGRATED_SYSTEM_PROMPT"),
            schema_hint={"name": str, "fp_type": str, "summary": str, "sub_srr": dict, "atomicity_check": dict, "test_hints": list},
            temperature=0.2,
        )
        sub_srr = result.get("sub_srr", {}) if isinstance(result, dict) else {}
        fp.srr_json = {
            **sub_srr,
            "summary": result.get("summary", "") if isinstance(result, dict) else "",
            "suggested_module": result.get("suggested_module", "") if isinstance(result, dict) else "",
        }
        if isinstance(result, dict):
            fp.atomicity_check = result.get("atomicity_check", fp.atomicity_check)
            fp.test_hints = result.get("test_hints", fp.test_hints)
            if result.get("name") and result["name"] != fp.name:
                fp.name = result["name"][:255]
            if result.get("fp_type"):
                fp.fp_type = result["fp_type"]
        fp.status = "pending_review"  # 重生成后需重新审核
        await session.commit()
        return ApiResponse(
            success=True,
            message=f"功能点「{fp.name}」已重新生成，待审核",
            data={"name": fp.name, "fp_type": fp.fp_type, "srr_json": fp.srr_json, "test_hints": fp.test_hints},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重生成失败: {str(e)[:200]}")
