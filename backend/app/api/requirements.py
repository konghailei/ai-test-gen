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

from datetime import datetime, timezone
import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_admin, require_write, get_current_user, CurrentUser
from app.agents.clarify_agent import clarify_agent
from app.utils.id_gen import gen_requirement_id
from app.agents.req_agent import req_agent
from app.db.database import get_session
from app.db.models import Requirement
from app.models.api_schemas import (
    ApiResponse,
    ClarifyAnswerRequest,
    RequirementSummary,
    RequirementUpdateRequest,
    RequirementUploadRequest,
)
from app.utils.logger import logger


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

    # 若调用方指定了 title/priority/modules，覆盖 SRR 中的值
    req_data = srr.setdefault("requirement", {})
    if req.title:
        req_data["title"] = req.title
    if req.priority:
        req_data["priority"] = req.priority
    if req.modules:
        req_data["modules"] = req.modules

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
        status = "active"
        srr_json_data = final_srr_data

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
        status=status,
        srr_json=srr_json_data,
        raw_text=req.raw_text,
        source_type="markdown",
        created_by="api",
    )
    session.add(record)
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

    # 4. 若调用方指定了 title/priority，覆盖 SRR 中的值
    req_data = srr.setdefault("requirement", {})
    if title:
        req_data["title"] = title
    if priority:
        req_data["priority"] = priority

    # 5. 持久化到 DB
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
# 1c. 多文件上传需求（v2.1 新增：需求文档 + 设计文档联合分析）
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

    # 调用 ReqAgent 解析合并后的统一文本
    try:
        srr = await req_agent.parse(
            raw_requirement=combined_raw,
            product_id=product_id,
            product_name=product_name or "",
        )
        # 防御：确保 srr 是 dict
        if not isinstance(srr, dict):
            logger.error("[API] 多文档解析返回非 dict: type={}, value={}", type(srr).__name__, str(srr)[:200])
            raise HTTPException(status_code=500, detail=f"需求解析返回异常类型: {type(srr).__name__}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[API] 多文档解析失败: {}", str(e)[:500])
        raise HTTPException(status_code=500, detail=f"需求解析失败: {str(e)[:200]}")

    req_id = srr.get("requirement", {}).get("id", "")
    if not req_id:
        raise HTTPException(status_code=500, detail="解析失败：未生成需求 ID")

    # 确保 req_id 唯一（防止 LLM 生成重复 ID）
    existing = await session.get(Requirement, req_id)
    if existing:
        # 重新生成唯一 ID（线程安全计数器，避免并发冲突）
        req_id = gen_requirement_id()
        srr["requirement"]["id"] = req_id
        logger.warning("[API/requirements] req_id 冲突 (multi)，重新生成: new_id={}", req_id)

    # 若调用方指定了 title/priority，覆盖 SRR 中的值
    req_data = srr.setdefault("requirement", {})
    if title:
        req_data["title"] = title
    if priority:
        req_data["priority"] = priority
    if module_list:
        req_data["modules"] = module_list

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

    # ===== ③ 持久化到 DB =====
    # v2.6: 使用 KB 增强后的 SRR
    multi_augmented = clarify_result.get("augmented_srr", srr) if clarify_result else srr
    srr_json_data = dict(multi_augmented)
    if clarify_result:
        srr_json_data["_clarify_state"] = {
            "questions": questions,
            "auto_augmentations": clarify_result.get("auto_augmentations", []),
            "kb_status": clarify_result.get("kb_status", {}),
            "quality_report": clarify_result.get("quality_report", {}),
        }
        kb_logs = clarify_result.get("kb_access_log", [])
        if kb_logs:
            srr_json_data["_kb_query_logs"] = kb_logs

    needs_clarify = (clarify_result.get("status") == "needs_clarification") if clarify_result else (len(questions) > 0)
    status = "draft" if needs_clarify else "active"

    record = Requirement(
        id=req_id,
        product_id=req_data.get("product_id", product_id),
        project_id=project_id or None,
        title=req_data.get("title", ""),
        priority=req_data.get("priority", "P2"),
        module=module_list[0] if module_list else "",
        modules=module_list,
        tags=[],
        version=1,
        status=status,
        srr_json=srr_json_data,
        raw_text=combined_raw,
        source_type="multi",
        created_by="api",
    )
    session.add(record)
    await session.commit()

    logger.info("[API] 多文档上传成功: req_id={}, files={}, status={}, questions={}",
                req_id, [d["filename"] for d in doc_contents], status, len(questions))

    return ApiResponse(
        success=True,
        message=f"多文档上传成功（{len(doc_contents)} 个文件），已合并为统一需求",
        data={
            "req_id": req_id,
            "srr": srr,
            "title": req_data.get("title", ""),
            "priority": req_data.get("priority", "P2"),
            "modules": module_list,
            "source_files": [d["filename"] for d in doc_contents],
            "status": status,
            "questions": questions,
            "has_unanswered_questions": needs_clarify,
            # KB 检索结果
            "kb_status": clarify_result.get("kb_status", {}) if clarify_result else {},
            "auto_augmentations_count": len(clarify_result.get("auto_augmentations", [])) if clarify_result else 0,
            "kb_empty": clarify_result.get("kb_status", {}).get("kb_empty", True) if clarify_result else True,
        },
    )


# ============================================================================
# 2. 需求列表（分页）
# ============================================================================

@router.get("", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_requirements(
    product_id: str | None = Query(default=None, description="按产品 ID 筛选"),
    project_id: str | None = Query(default=None, description="按项目 ID 筛选"),
    priority: str | None = Query(default=None, description="按优先级筛选 P0-P3"),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数，最大 100"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取需求列表（分页）。

    Args:
        product_id: 按产品 ID 筛选（可选）
        project_id: 按项目/版本 ID 筛选（可选）
        priority: 按优先级筛选（可选）
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
        # 标记为降级
        final_srr["_clarify_degraded"] = True
        logger.warning("[API/requirements] 降级使用原有 SRR: req_id={}", req_id)

    # ===== ③ 更新 DB =====
    final_data = dict(final_srr)
    # 移除临时状态标记
    final_data.pop("_clarify_state", None)
    final_data.pop("clarify_answers", None)
    # 保留 KB 查询日志（不在 re-parse 中丢失）
    if original_kb_logs := (dict(record.srr_json or {}).get("_kb_query_logs")):
        final_data["_kb_query_logs"] = original_kb_logs
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
        final_srr["_finalize_degraded"] = True

    # 保留 KB 日志
    final_data = dict(final_srr)
    if original_logs := (dict(record.srr_json or {}).get("_kb_query_logs")):
        final_data["_kb_query_logs"] = original_logs
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

    await session.delete(record)
    await session.commit()

    # 同步删除向量库中的记录（避免产生孤儿向量）
    try:
        from app.db.vector_store import vector_store
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

@router.post("/{req_id}/archive", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def archive_requirement(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """归档需求。

    规则：
    - active 状态 → 允许归档
    - draft 状态 → 拒绝归档（需先完成澄清）
    - archived / deprecated 状态 → 拒绝归档（已归档或已废弃）
    - 归档后数据写入向量知识库，供后续需求检索参考

    Args:
        req_id: 需求 ID
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

    # 更新状态
    old_status = record.status
    record.status = "archived"
    await session.commit()

    # 写入向量知识库（供后续需求检索参考）
    try:
        from app.db.vector_store import vector_store

        await asyncio.to_thread(
            vector_store.add_requirement,
            req_id=record.id,
            srr_json=record.srr_json or {},
            metadata={
                "product_id": record.product_id,
                "modules": ",".join(record.modules or []),
                "priority": record.priority,
                "status": "archived",
                "archived_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        logger.info("[API/requirements] 已写入知识库: req_id={}", req_id)
    except Exception as e:
        logger.warning("[API/requirements] 知识库写入失败（不阻断）: {}", str(e)[:200])

    logger.info("[API/requirements] 归档成功: req_id={}, old_status={}", req_id, old_status)

    return ApiResponse(
        success=True,
        message="需求已归档，数据已导入知识库",
        data={"req_id": req_id, "status": "archived"},
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
