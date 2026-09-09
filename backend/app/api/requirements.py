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
import json
import re
from typing import Any, Literal
from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form, Request
from fastapi.responses import Response
from sqlalchemy import func, select, delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_admin, require_write, get_current_user, CurrentUser
from app.utils.pagination import paginate
from app.core.operation_logger import log_operation
from app.core.product_access import (
    assert_product_visible,
    apply_visible_product_filter,
    get_visible_product_ids,
)
from app.config import settings
from app.agents.clarify_agent import clarify_agent
from app.agents.impact_agent import ImpactAnalysisAgent
from app.utils.id_gen import gen_requirement_id, gen_task_id
from app.agents.req_agent import req_agent
from app.db.database import get_session, async_session
from app.db.models import Requirement, Project, AnalysisTask, FunctionalPoint, FPDependency
from app.db.models import TestCase, GenerationBatch, ReqDependency, Traceability
from app.db.models import ErrorCase  # v3.1.3 P1-3: 删除需求时级联清理异常用例
from app.db.models import FPDependencyHistory  # v2.3: 依赖审核历史
from app.models.api_schemas import (
    ApiResponse,
    ClarifyAnswerRequest,
    RequirementSummary,
    RequirementUpdateRequest,
    RequirementUploadRequest,
)
from app.utils.logger import logger
from app.core.requirement_tasks import (
    _normalize_cross_fp_flow, _normalize_fp_ref, _normalize_shared_business_rule,
    _normalize_fp_contract, _normalize_global_constraint,
    _count_clarify_rounds_since_last_regen, _merge_interaction_srr,
    _save_fps_to_db, _bump_req_revision, _update_cross_fp_interactions_on_merge,
    _update_fp_index_in_srr, _save_fp_dependencies, _table_to_markdown,
    _extract_docx_text, _describe_docx_image, _update_task,
    _run_requirement_analysis, _run_clarify_reparse,
    _run_regenerate_reparse, _run_fp_merge_task,
    _INTERACTION_SUBKEYS, _FP_REF_VERSION_RE, _DOCX_MAGIC, _MAX_FILE_SIZE,
)


# ============================================================================
# v1.5.1: FP 自动入库辅助函数
# ============================================================================















# v2.8.52: 后台任务异常回调——防止协程静默消亡
def _bg_task_done_callback(task: asyncio.Task) -> None:
    """asyncio.create_task 的 done_callback，记录未捕获异常。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error("[BG-Task] 后台协程异常退出: {}", str(exc)[:500])


def _create_bg_task(coro) -> asyncio.Task:
    """创建后台任务并附加异常回调。"""
    task = asyncio.create_task(coro)
    task.add_done_callback(_bg_task_done_callback)
    return task








async def _assert_project_editable(session: AsyncSession, project_id: str | None) -> None:
    """v2.46: 校验项目是否可编辑（需求/FP 写操作前置校验）。

    项目状态机：
    - active          — 允许编辑
    - pending_archive — 冻结编辑（用户已标记待归档，应先取消或完成归档）
    - archived        — 冻结编辑（已写入知识库，需先恢复活动）

    Args:
        session: 数据库会话
        project_id: 项目 ID；为 None（未关联项目）时不校验

    Raises:
        HTTPException 409: 项目不可编辑时
    """
    if not project_id:
        return
    from app.db.models import Project
    proj = await session.get(Project, project_id)
    if proj is None:
        return
    if proj.status == "pending_archive":
        raise HTTPException(
            status_code=409,
            detail=f"项目「{proj.name}」已标记为待归档，无法编辑需求。请先取消待归档或完成归档操作。"
        )
    if proj.status == "archived":
        raise HTTPException(
            status_code=409,
            detail=f"项目「{proj.name}」已归档，无法编辑需求。请先恢复活动后再操作。"
        )








# ============================================================================
# 文件校验辅助函数
# ============================================================================


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








async def _parse_uploaded_files(files: list[UploadFile], session=None) -> tuple[str, list[dict]]:
    """解析上传的需求文件列表，合并为纯文本。

    复用 upload_requirement_file 的解析逻辑（扩展名校验 / 魔数校验 / md/txt/docx 解析），
    供 regenerate 等需要文件上传的端点复用。

    Args:
        files: UploadFile 列表（支持 .md / .markdown / .txt / .docx）
        session: PG 异步会话（用于 docx 多模态图片识别）

    Returns:
        tuple[str, list[dict]]: (合并后的纯文本, 所有 docx 文件提取的原始图片列表)
        多文件以分隔线连接

    Raises:
        HTTPException 400: 文件格式不支持 / 内容为空 / 解析失败
    """
    MAX_FILE_SIZE = 10 * 1024 * 1024
    parts: list[str] = []
    all_images: list[dict] = []

    for file in files:
        filename = file.filename or ""
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

        if ext not in ("md", "markdown", "docx", "txt"):
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式: .{ext}，仅支持 .md / .docx / .txt",
            )

        raw_bytes = await file.read()
        if len(raw_bytes) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"文件 {filename} 大小超过 10MB 上限，请压缩后重试",
            )

        _validate_file_magic(ext, raw_bytes)

        file_text = ""
        if ext in ("md", "markdown", "txt"):
            try:
                file_text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    file_text = raw_bytes.decode("gbk")
                except UnicodeDecodeError:
                    raise HTTPException(
                        status_code=400,
                        detail=f"文件 {filename} 编码不支持，请使用 UTF-8 或 GBK 编码",
                    )
        elif ext == "docx":
            try:
                file_text, doc_images = await _extract_docx_text(raw_bytes, filename, session=session)
                all_images.extend(doc_images)
            except ImportError:
                raise HTTPException(
                    status_code=500,
                    detail="python-docx 未安装，无法解析 .docx 文件",
                )
            except Exception as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"文件 {filename} 解析失败: {str(e)[:150]}",
                )

        if not file_text or not file_text.strip():
            raise HTTPException(status_code=400, detail=f"文件 {filename} 内容为空")

        logger.info("[API/requirements] 文件解析完成: filename={}, text_len={}", filename, len(file_text))
        # v2.8.64: 多/单文件合并时增加文档边界标记，让 ClarifyAgent 能识别跨文档矛盾
        if len(files) > 1 or filename:
            parts.append(f"【文档：{filename or '未命名'}】\n{file_text}")
        else:
            parts.append(file_text)

    # v2.8.64: 多文件之间用分隔线 + 文档标记连接，便于后续 LLM 识别跨文档矛盾
    return "\n\n---\n\n".join(parts), all_images


async def _guard_product_visibility(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """P1-1 路由级产品隔离守卫。

    从路径参数提取 req_id / task_id，校验关联需求所属产品对当前用户可见：
    - req_id → Requirement.product_id
    - task_id → AnalysisTask.req_id → Requirement.product_id

    记录不存在时直接放行（由端点自身返回 404，避免守卫重复报错语义）。
    """
    path_params = request.path_params
    req_id = path_params.get("req_id")

    if not req_id and path_params.get("task_id"):
        task = await session.get(AnalysisTask, path_params["task_id"])
        req_id = task.req_id if task else None

    if not req_id:
        return

    pid_row = await session.execute(
        select(Requirement.product_id).where(Requirement.id == req_id)
    )
    pid = pid_row.scalar_one_or_none()
    if pid:
        await assert_product_visible(session, user, pid)


router = APIRouter(dependencies=[Depends(_guard_product_visibility)])


# ============================================================================
# 1. 上传需求
# ============================================================================

@router.post("/upload", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def upload_requirement(
    req: RequirementUploadRequest,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """上传需求文本（异步任务模式，与文件上传统一）。

    v2.15: 改为异步任务模式。立即创建 Requirement(analyzing) + AnalysisTask，
    后台协程执行 ReqAgent 解析 + ClarifyAgent 检测。立即返回 task_id + req_id，
    前端轮询任务进度（GET /requirements/tasks/{task_id}）。

    v2.8.69: 上传前校验项目状态——若所属项目已归档，则拒绝上传新需求。
             归档是「项目级」的一次性操作，必须保证归档冻结后无法再新增需求。

    Args:
        req: 上传请求，含 raw_text / product_id / product_name / title / priority / modules
        session: 数据库会话（依赖注入）

    Returns:
        ApiResponse: data 含 task_id / req_id / status="analyzing"
    """
    if not req.raw_text or not req.raw_text.strip():
        raise HTTPException(status_code=400, detail="需求文本不能为空")

    # P1-1 产品隔离：目标产品必须对当前用户可见（含 PROD-DEFAULT 默认产品）
    await assert_product_visible(session, current_user, req.product_id or "PROD-DEFAULT")

    # v2.8.69: 项目归档后禁止上传新需求
    if req.project_id:
        project = await session.get(Project, req.project_id)
        if project and project.status == "archived":
            raise HTTPException(
                status_code=403,
                detail=f"项目「{project.name or project.id}」已归档，禁止上传新需求"
            )

    logger.info(
        "[API/requirements] 上传需求(raw_text 异步模式): text_len={}, product_id={}, title={}",
        len(req.raw_text),
        req.product_id,
        req.title or "(自动提取)",
    )

    # v2.15: 异步分析模式——立即创建 Requirement(analyzing) + AnalysisTask，后台协程执行解析
    req_id = gen_requirement_id()
    task_id = gen_task_id()

    # 立即创建 Requirement 记录（status=analyzing，分析完成后由后台协程更新）
    record = Requirement(
        id=req_id,
        product_id=req.product_id or "PROD-DEFAULT",
        project_id=req.project_id,
        title=req.title or "(分析中...)",
        priority=req.priority,
        module=req.modules[0] if req.modules else "",
        modules=req.modules,
        tags=[],
        version=1,
        status="analyzing",
        srr_json={},
        raw_text=req.raw_text,
        source_type="markdown",
        created_by="api",
    )
    session.add(record)
    await session.flush()  # 确保 Requirement 先落库，避免 AnalysisTask 外键约束 violation

    # 创建任务记录
    task = AnalysisTask(
        id=task_id,
        task_type="upload_requirement",
        req_id=req_id,
        product_id=req.product_id or "PROD-DEFAULT",
        status="pending",
        phase="parse",
        progress=0,
        message="已接收需求文本，准备开始分析...",
        created_by="api",
    )
    session.add(task)
    await session.commit()

    # 启动后台协程（不 await，立即返回）
    _create_bg_task(_run_requirement_analysis(
        task_id=task_id,
        req_id=req_id,
        combined_raw=req.raw_text,
        product_id=req.product_id or "PROD-DEFAULT",
        product_name=req.product_name,
        title=req.title,
        priority=req.priority,
        project_id=req.project_id or "",
        module_list=req.modules,
        source_files=[],
    ))

    logger.info("[API] raw_text 上传已派发异步分析: task_id={}, req_id={}", task_id, req_id)

    await log_operation(
        username=current_user.username, user_id=current_user.user_id,
        action="upload_requirement", target_type="requirement", target_id=req_id,
        detail={"title": req.title or "(自动提取)", "product_id": req.product_id or "PROD-DEFAULT"},
    )

    return ApiResponse(
        success=True,
        message="已接收需求文本，正在后台分析",
        data={
            "task_id": task_id,
            "req_id": req_id,
            "status": "analyzing",
        },
    )


# ============================================================================
# 1b. 文件上传需求（v2.41 移除）
# ----------------------------------------------------------------------------
# 原 /upload/file 同步路径已废弃——前端统一使用 /upload/multi 异步路径
# （走完整 ClarifyAgent + KB 检索 + 质量门禁），同步路径绕过质量保障且
# 直接落库 active 导致 confirm 死锁。文件解析逻辑已抽为 _parse_uploaded_files
# 供 /upload/multi 和 /regenerate 复用。
# ============================================================================


# ============================================================================
# 1c. 异步分析后台协程（v2.14 新增 - 需求解析/澄清的异步执行）
# ============================================================================





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
    current_user: CurrentUser = Depends(get_current_user),
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

    v2.8.69: 上传前校验项目状态——项目已归档时拒绝上传新需求
    """
    module_list = [m.strip() for m in modules.split(",") if m.strip()]
    if not files or len(files) == 0:
        raise HTTPException(status_code=400, detail="请上传至少一个文件")

    # P1-1 产品隔离：目标产品必须对当前用户可见
    await assert_product_visible(session, current_user, product_id or "PROD-DEFAULT")

    # v2.8.69: 项目归档后禁止上传新需求
    if project_id:
        project = await session.get(Project, project_id)
        if project and project.status == "archived":
            raise HTTPException(
                status_code=403,
                detail=f"项目「{project.name or project.id}」已归档，禁止上传新需求"
            )

    MAX_TOTAL_SIZE = 50 * 1024 * 1024
    total_size = 0

    # v2.24: 先读取文件字节（快速操作），文件解析移到后台协程中
    # 这样任务记录可以在文件解析之前就创建，用户立即看到任务
    file_bytes_list: list[tuple[str, bytes]] = []
    MAX_SINGLE_SIZE = 10 * 1024 * 1024  # v2.8.52: 单文件 10MB 上限
    for file in files:
        filename = file.filename or ""
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        if ext not in ("md", "markdown", "docx", "txt"):
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件格式: .{ext}（文件：{filename}），仅支持 .md / .docx / .txt",
            )
        raw_bytes = await file.read()
        # v2.8.52: 单文件大小校验
        if len(raw_bytes) > MAX_SINGLE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"文件 {filename} 超过单文件 10MB 上限（当前 {len(raw_bytes) // 1024 // 1024}MB）",
            )
        # v2.8.52: 魔数校验（防止伪造扩展名）
        _validate_file_magic(ext, raw_bytes)
        total_size += len(raw_bytes)
        if total_size > MAX_TOTAL_SIZE:
            raise HTTPException(status_code=400, detail="所有文件总大小超过 50MB 上限")
        file_bytes_list.append((filename, raw_bytes))

    # v2.24: 立即创建 Requirement + AnalysisTask（phase=upload），文件解析在协程中完成
    req_id = gen_requirement_id()
    task_id = gen_task_id()

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
        raw_text="",
        raw_images=[],
        source_type="multi",
        created_by="api",
    )
    session.add(record)
    await session.flush()

    task = AnalysisTask(
        id=task_id,
        task_type="upload_requirement",
        req_id=req_id,
        product_id=product_id,
        status="pending",
        phase="upload",
        progress=0,
        message="已接收文件，准备解析...",
        created_by="api",
        # v2.24: 初始化 upload 阶段的 phase_timings
        phase_timings={"upload": {"start": datetime.now(timezone.utc).isoformat()}},
    )
    session.add(task)
    await session.commit()

    # 启动后台协程（不 await，立即返回）— 文件字节传递给协程解析
    _create_bg_task(_run_requirement_analysis(
        task_id=task_id,
        req_id=req_id,
        combined_raw="",  # v2.24: 文件解析移到协程中，初始为空
        product_id=product_id,
        product_name=product_name,
        title=title,
        priority=priority,
        project_id=project_id,
        module_list=module_list,
        source_files=[f[0] for f in file_bytes_list],
        file_bytes_list=file_bytes_list,  # v2.24: 传递原始文件字节
    ))

    logger.info("[API] 多文档上传已派发异步分析: task_id={}, req_id={}, files={}",
                task_id, req_id, [f[0] for f in file_bytes_list])

    return ApiResponse(
        success=True,
        message=f"已接收 {len(file_bytes_list)} 个文件，正在后台分析",
        data={
            "task_id": task_id,
            "req_id": req_id,
            "status": "analyzing",
            "source_files": [f[0] for f in file_bytes_list],
        },
    )


# ============================================================================
# 1e. 任务状态查询（v2.14 新增 - 供前端轮询分析进度）
# ============================================================================

@router.get("/tasks", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_tasks(
    status: str | None = Query(default=None, description="按状态筛选，支持逗号分隔多值（如 running,pending）"),
    task_type: str | None = Query(default=None, description="按任务类型筛选 upload_requirement/submit_clarify/..."),
    product_id: str | None = Query(default=None, description="按产品筛选"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """列出分析任务（支持按状态/类型/产品筛选，分页）。

    v2.20: status 参数支持逗号分隔多值（如 ``running,pending``），
    便于前端查询"所有未结束任务"。
    """
    conditions = []
    # P1-1 产品隔离：仅列出可见产品的任务
    visible_ids = await get_visible_product_ids(session, user)
    apply_visible_product_filter(conditions, visible_ids, AnalysisTask.product_id)
    if status:
        status_list = [s.strip() for s in status.split(",") if s.strip()]
        if len(status_list) == 1:
            conditions.append(AnalysisTask.status == status_list[0])
        elif status_list:
            conditions.append(AnalysisTask.status.in_(status_list))
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

    # v2.36: 携带服务器当前时间，避免前端计算耗时出现负数
    return ApiResponse(success=True, message="ok", data={
        "items": items,
        "total": total,
        "server_now": datetime.now(timezone.utc).isoformat(),
    })


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
    # v2.36: 携带服务器当前时间，避免前端用 Date.now() 计算耗时
    # 时因客户端时钟与服务器不同步导致负数
    d["server_now"] = datetime.now(timezone.utc).isoformat()
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

    d = task.to_dict()
    # v2.36: 携带服务器当前时间，避免前端计算耗时出现负数
    d["server_now"] = datetime.now(timezone.utc).isoformat()
    return ApiResponse(success=True, message="ok", data=d)


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

    # v2.23: 记录当前阶段的结束时间到 phase_timings
    if task.phase and task.phase_timings:
        timings = dict(task.phase_timings)
        phase_data = dict(timings.get(task.phase, {}))
        if "end" not in phase_data and "start" in phase_data:
            try:
                start_dt = datetime.fromisoformat(phase_data["start"])
                phase_data["end"] = task.completed_at.isoformat()
                phase_data["duration_ms"] = int((task.completed_at - start_dt).total_seconds() * 1000)
                timings[task.phase] = phase_data
                task.phase_timings = timings
            except (ValueError, TypeError):
                pass

    # v2.8.52: 若关联需求仍为 analyzing 状态，回退为 draft 避免卡死
    # （原状态可能是 draft/pending_review/confirmed，但 analyzing 期间无法得知原状态，
    #   统一回退 draft 是安全选择——用户可重新操作）
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
    status: str | None = Query(default=None, description="按状态筛选 analyzing/draft/pending_review/confirmed/archived"),
    search: str | None = Query(default=None, description="按标题模糊搜索"),
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, ge=1, le=300, description="每页条数，最大 300"),
    user: CurrentUser = Depends(get_current_user),
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
    # P1-1 产品隔离：仅列出可见产品的需求
    visible_ids = await get_visible_product_ids(session, user)
    apply_visible_product_filter(conditions, visible_ids, Requirement.product_id)
    if product_id:
        conditions.append(Requirement.product_id == product_id)
    if project_id:
        if project_id == "unassigned":
            # 筛选未绑定项目的需求（project_id IS NULL）
            conditions.append(Requirement.project_id.is_(None))
        else:
            conditions.append(Requirement.project_id == project_id)
    if priority:
        conditions.append(Requirement.priority == priority)
    if status:
        conditions.append(Requirement.status == status)
    if search:
        conditions.append(Requirement.title.ilike(f"%{search}%"))

    # 分页查询（按创建时间倒序）—— v3.2.2 收敛
    stmt = select(Requirement).order_by(Requirement.created_at.desc())
    for cond in conditions:
        stmt = stmt.where(cond)
    rows, total = await paginate(session, stmt, page, page_size)

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
            "project_id": record.project_id,  # v3.3.1: 补充 project_id，前端/脚本需据此定位所属项目
            "title": record.title,
            "priority": record.priority,
            "modules": record.modules,
            "status": record.status,
            "version": record.version,
            "srr_json": record.srr_json,  # 注意字段名 srr_json（前端和类型定义使用此名）
            "raw_text": record.raw_text,
            "raw_images": record.raw_images or [],  # v2.18: docx 原始图片（base64 data_url），供前端展示对比
            "supplementary_text": record.supplementary_text or "",  # v2.17: 补充文件内容
            "source_type": record.source_type,
            "kb_query_logs": (record.srr_json or {}).get("_kb_query_logs", []),
            "questions_count": len(((record.srr_json or {}).get("_clarify_state", {}) or {}).get("questions", [])),
            "created_at": record.created_at.isoformat() if record.created_at else "",
            "updated_at": record.updated_at.isoformat() if record.updated_at else "",
            # ===== 修复问题 3：补充向量化与归档相关字段 =====
            "vectorization_status": record.vectorization_status,  # null/processing/completed/failed
            "vectorization_error": record.vectorization_error or "",
            "go_live_date": record.go_live_date.isoformat() if record.go_live_date else "",
            "archived_at": record.archived_at.isoformat() if record.archived_at else "",
            "revision": record.revision or 1,  # v2.33: 多人协作冲突检测
        },
    )


@router.get("/{req_id}/revision", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_requirement_revision(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """v2.33: 轻量端点——返回需求的 revision 和任务状态，供前端轮询检测多人协作冲突。

    前端每 5 秒轮询此端点，对比 revision 变化时显示"需求已被他人修改，请刷新"提示条。
    同时返回是否有运行中的分析任务，用于显示"正在执行分析任务"提示。

    Returns:
        ApiResponse: data 含 revision / updated_at / task_running / task_message
    """
    result = await session.execute(
        select(Requirement.revision, Requirement.updated_at, Requirement.status).where(Requirement.id == req_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    revision = row[0] or 1
    updated_at = row[1].isoformat() if row[1] else ""
    req_status = row[2]

    # 检查是否有运行中的分析任务
    task_running = False
    task_message = ""
    task_type_label = ""
    if req_status == "analyzing":
        task_running = True
        task_message = "正在分析需求..."
    else:
        task_result = await session.execute(
            select(AnalysisTask).where(
                AnalysisTask.req_id == req_id,
                AnalysisTask.status.in_(["pending", "running"]),
            ).order_by(AnalysisTask.created_at.desc()).limit(1)
        )
        running_task = task_result.scalar_one_or_none()
        if running_task:
            task_running = True
            labels = {
                "upload_requirement": "需求解析",
                "regenerate": "重新生成",
                "merge_fp": "功能点合并",
                "submit_clarify": "澄清重解析",
            }
            task_type_label = labels.get(running_task.task_type, running_task.task_type)
            task_message = running_task.message or f"正在执行{task_type_label}任务..."

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "revision": revision,
            "updated_at": updated_at,
            "status": req_status,
            "task_running": task_running,
            "task_type": task_type_label,
            "task_message": task_message,
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

    # v2.8.66: ⭐ 不再把 _clarify_history 中历史已回答的问题合并到当前 questions
    # 用户要求：当前澄清页面只显示本轮待回答的问题；已回答的问题应去「澄清历史」查看。
    # 之前的逻辑会把历史每轮的问题都扁平化合并，导致第二轮回答时仍能看到第一轮的
    # 所有旧问题，造成"永远回答不完"的体验。

    # 合并 answers：仅用顶层 clarify_answers（最新一轮的草稿/提交）
    merged_answers: dict = dict(saved_answers)
    history = srr.get("_clarify_history") or []

    # 为每个 question 注入 answered 状态（便于前端直接展示）
    merged_questions: list[dict] = []
    for q in saved_questions:
        qid = str(q.get("id") or f"{q.get('field','')}__{q.get('question','')[:40]}")
        ans = merged_answers.get(qid)
        q2 = dict(q)
        if ans and not q2.get("answer"):
            q2["answer"] = ans if isinstance(ans, str) else (ans.get("answer") if isinstance(ans, dict) else "")
            if q2.get("answered") is None:
                q2["answered"] = True
            if "status" not in q2 or not q2.get("status"):
                q2["status"] = "answered"
        # v2.8.66: 若顶层已有答案，标记 resolved=true（避免前端重复展示）
        if ans:
            q2["resolved"] = True
        merged_questions.append(q2)

    # 排序：未回答按原顺序；已回答的标记不影响排序（因为有 resolved=true 前端会过滤掉）
    has_unresolved = any(not q.get("resolved") for q in merged_questions)

    # ⭐ kb_access_log 合并：_clarify_state.kb_access_log + history 每轮的 kb_access_log
    kb_access_log_merged: list[dict] = list(clarify_state.get("kb_access_log", []) or [])
    for h in history:
        if isinstance(h.get("kb_access_log"), list):
            kb_access_log_merged.extend(h["kb_access_log"])
    kb_status = dict(clarify_state.get("kb_status", {}) or {})
    kb_status["kb_access_log_count"] = len(kb_access_log_merged)
    kb_status["kb_access_log"] = kb_access_log_merged

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "req_id": req_id,
            "status": (
                "needs_clarification"
                if has_unresolved
                else "passed"
            ),
            "questions": merged_questions,
            "previous_answers": merged_answers,
            "quality_report": clarify_state.get("quality_report", {}),
            "kb_status": kb_status,
            "auto_augmentations": clarify_state.get("auto_augmentations", []),
            # v2.12: 字段冲突列表（docs/01 §4.4.1），供前端展示人工确认
            "field_conflicts": clarify_state.get("field_conflicts", []),
            # ⭐ 每轮澄清历史摘要（便于调试/UI 多轮展示）
            "rounds_summary": [
                {
                    "round": i + 1,
                    "questions_count": len(h.get("questions") or []),
                    "answers_count": len({k: v for k, v in (h.get("answers") or {}).items() if v}),
                    "kb_access_log_count": len(h.get("kb_access_log") or []),
                    "created_at": h.get("created_at"),
                    "note": h.get("note"),
                }
                for i, h in enumerate(history)
            ],
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
    """提交澄清问题答案 → 异步重新生成最终 SRR。

    v2.15 异步化（变更组 C）：
        1. 同步：验证答案 + 构建增强文本 + 保存澄清历史快照
        2. 创建 AnalysisTask（task_type=submit_clarify）
        3. 启动后台协程 _run_clarify_reparse（执行 LLM parse + FP 重提取）
        4. 立即返回 task_id，前端轮询 GET /tasks/{task_id} 获取进度

    Args:
        req_id: 需求 ID
        req: 澄清答案请求，含 answers 字典 {question_id: answer}
        session: 数据库会话

    Returns:
        ApiResponse: data 含 task_id / req_id / status=analyzing
    """
    if not req.answers:
        raise HTTPException(status_code=400, detail="澄清答案不能为空")

    # v2.33: 互斥锁检查——防止与其他分析任务并发冲突（v2.8.52: 传入 session 启用 advisory lock）
    await _check_no_running_task(req_id, session=session)

    # 从 DB 取需求
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    if record.status not in ("draft", "pending_review", "confirmed"):
        raise HTTPException(
            status_code=409,
            detail=f"当前状态 {record.status} 不可提交澄清（仅 draft/pending_review/confirmed 可提交）",
        )
    # v2.8.52: confirmed 状态提交澄清时明确提示——已审核 FP 将被重建
    if record.status == "confirmed":
        # 检查是否有已审核的 FP
        from sqlalchemy import func as _func
        approved_count = (await session.execute(
            _func.count(FunctionalPoint.id).where(
                FunctionalPoint.requirement_id == req_id,
                FunctionalPoint.status == "approved",
            )
        )).scalar() or 0
        if approved_count > 0:
            logger.warning(
                "[API/requirements] confirmed 需求提交澄清，{} 个已审核 FP 将被重建: req_id={}",
                approved_count, req_id,
            )
        record.status = "draft"

    raw_text = record.raw_text or ""
    srr_json = dict(record.srr_json or {})
    clarify_state = srr_json.get("_clarify_state", {}) or {}
    questions = clarify_state.get("questions", []) or []

    # v2.42: 澄清轮次收敛（docs/01 §4.1 第4条：最多 3 轮，质量达标才放行）
    # v2.43: 轮次统计基于"当前重新生成周期"，重新生成后重置计数
    # v2.8.51: 从 settings 读取，与 orchestrator ClarifyLoopGuard 保持一致
    MAX_CLARIFY_ITERATIONS = settings.MAX_CLARIFY_ITERATIONS
    existing_history = list(srr_json.get("_clarify_history", []))
    clarify_rounds_done = _count_clarify_rounds_since_last_regen(existing_history)
    if clarify_rounds_done >= MAX_CLARIFY_ITERATIONS:
        raise HTTPException(
            status_code=409,
            detail=(
                f"已达到最大澄清轮次（{MAX_CLARIFY_ITERATIONS} 轮）。"
                "剩余未解决问题已标记为 optional，不阻断流程。"
                "如需补充信息请使用「重新生成」功能上传补充文件或说明。"
            ),
        )

    # v2.29: 若 _clarify_state 已被清空（重新解析后会 pop），从最近的分析任务
    # result_json 中找回澄清问题，确保澄清历史能完整记录 Q&A 对。
    if not questions:
        try:
            recent_task_stmt = (
                select(AnalysisTask.result_json)
                .where(AnalysisTask.req_id == req_id)
                .where(AnalysisTask.task_type == "upload_requirement")
                .order_by(AnalysisTask.created_at.desc())
                .limit(1)
            )
            rt = await session.execute(recent_task_stmt)
            rt_row = rt.scalar_one_or_none()
            if rt_row:
                rt_data = rt_row if isinstance(rt_row, dict) else json.loads(rt_row)
                task_questions = rt_data.get("questions", []) if isinstance(rt_data, dict) else []
                if task_questions:
                    questions = task_questions
                    logger.info("[API/requirements] 从 task result_json 回找澄清问题: count={}", len(questions))
        except Exception as e:
            logger.warning("[API/requirements] 回找澄清问题失败: {}", str(e)[:200])

    # ===== ⓪ 保存澄清历史快照（在 re-parse 前留存 Q&A 记录） =====
    # v2.29: answers 保存为结构化 Q&A 列表（含 question 文本），而非裸 id→answer 映射，
    # 确保即使后续 _clarify_state 被清空，历史记录仍能独立展示完整 Q&A。
    qa_records: list[dict] = []
    for q in questions:
        qid = q.get("id", "") if isinstance(q, dict) else ""
        ans_val = req.answers.get(qid, "").strip() if isinstance(req.answers, dict) else ""
        if qid:
            qa_records.append({
                "id": qid,
                "field": q.get("field", "") if isinstance(q, dict) else "",
                "question": q.get("question", "") if isinstance(q, dict) else "",
                "priority": q.get("priority", "") if isinstance(q, dict) else "",
                "answer": ans_val,
            })

    history_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "clarify",
        "questions": questions,
        "answers": dict(req.answers),
        "qa_records": qa_records,
        "extra_notes": req.extra_notes or "",
        "auto_augmentations": clarify_state.get("auto_augmentations", []),
        "field_conflicts": clarify_state.get("field_conflicts", []),
        "quality_report": clarify_state.get("quality_report", {}),
    }
    clarify_history = list(srr_json.get("_clarify_history", []))
    clarify_history.append(history_entry)
    # v2.8.52: 裁剪历史记录，防止 srr_json 无界膨胀（最多保留最近 5 轮）
    MAX_CLARIFY_HISTORY = 5
    if len(clarify_history) > MAX_CLARIFY_HISTORY:
        clarify_history = clarify_history[-MAX_CLARIFY_HISTORY:]

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
    # v2.8.52: _clarify_state 可能已被 regen 分支清空，回退到 _clarify_history 最近一条
    kb_augments = (srr_json.get("_clarify_state", {}) or {}).get("auto_augmentations", [])
    if not kb_augments and clarify_history:
        # 从最近一条 clarify 历史记录中恢复 KB 补全信息
        for entry in reversed(clarify_history):
            if isinstance(entry, dict) and entry.get("auto_augmentations"):
                kb_augments = entry["auto_augmentations"]
                break
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
        "[API/requirements] 提交澄清答案并启动异步重新生成: req_id={}, answers={}, enhanced_len={}",
        req_id,
        len(req.answers),
        len(enhanced_raw),
    )

    # v2.15: 标记需求为 analyzing 状态
    # v2.8.52: 先快照原始状态，再修改（避免 original_status 被覆盖为 "analyzing"）
    original_status_snapshot = record.status
    record.status = "analyzing"
    await session.commit()

    # ===== ② 创建 AnalysisTask + 启动后台协程 =====
    task_id = gen_task_id()
    task = AnalysisTask(
        id=task_id,
        task_type="submit_clarify",
        req_id=req_id,
        product_id=record.product_id,
        status="pending",
        phase="parse",
        progress=0,
        message="已接收澄清答案，准备重新生成...",
        created_by="api",
    )
    session.add(task)
    await session.commit()

    # 启动后台协程（不 await，立即返回）
    _create_bg_task(_run_clarify_reparse(
        task_id=task_id,
        req_id=req_id,
        enhanced_raw=enhanced_raw,
        original_raw=record.raw_text or "",  # v1.6: 传入原始文本用于 FP 提取，避免增强文本导致 FP 偏差
        product_id=record.product_id,
        clarify_history=clarify_history,
        old_srr=srr_json,
        priority=record.priority,
        original_status=original_status_snapshot,  # v2.8.52: 使用状态修改前的快照
    ))

    return ApiResponse(
        success=True,
        message="已接收澄清答案，正在后台重新生成",
        data={
            "task_id": task_id,
            "req_id": req_id,
            "status": "analyzing",
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

    # v2.8.52: 互斥检查（与其他分析任务冲突检测）
    await _check_no_running_task(req_id, session=session)

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
        finalize_result = await req_agent.parse(
            raw_requirement=enhanced_raw,
            product_id=record.product_id,
        )
        final_srr = finalize_result.srr
        final_srr.setdefault("requirement", {})["id"] = req_id
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
    # v2.40: 移除 additional_details，仅保留 _baseline_context
    old_baseline = old_srr.get("_baseline_context")
    if old_baseline:
        final_data["_baseline_context"] = old_baseline
    final_data.pop("_clarify_state", None)
    final_data.pop("clarify_answers", None)
    final_data.pop("_field_sources", None)

    record.srr_json = final_data
    # v2.41: 修复 active/confirmed 死锁——原落库 active 会被 confirm_requirement 拒绝（只接受 pending_review）
    # 改为 pending_review，让用户走标准 confirm 流程，与 _run_requirement_analysis / _run_clarify_reparse 对齐
    record.status = "pending_review"
    # v2.8.52: 同步更新标题（LLM 重解析可能产生新标题）
    record.title = final_data.get("requirement", {}).get("title", record.title)

    # v2.8.52: 同步更新 FP（原 finalize 不更新 FP，导致 SRR 新 / FP 旧）
    if finalize_result.fps:
        try:
            await _save_fps_to_db(session, req_id, finalize_result.fps, record.priority, product_id=record.product_id or "")
            await _save_fp_dependencies(session, req_id, finalize_result.fp_deps)
            await _update_fp_index_in_srr(session, req_id)
            # 重新读取 final_data（_update_fp_index_in_srr 可能修改了 srr_json）
            record.srr_json = final_data
        except Exception as fp_e:
            logger.warning("[API/requirements] finalize FP 更新失败（降级跳过）: {}", str(fp_e)[:200])

    await session.commit()

    logger.info("[API/requirements] KB 确认完成: req_id={}, title={}", req_id, record.title)

    return ApiResponse(
        success=True,
        message="KB 已解答全部问题，最终 SRR 已生成，请确认",
        data={"req_id": req_id, "status": "pending_review", "srr": final_data},
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


# ============================================================================
# v2.0 需求历史版本管理：列出/查看历次重新分析前的 SRR 快照
# ============================================================================

@router.get("/{req_id}/versions", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_requirement_versions(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """列出需求的历史版本（重新分析前的 SRR 快照）。

    历史版本按时间倒序返回（最新快照在前）。每条记录含：
        - version_index: 版本序号（从 1 开始递增）
        - snapshot_at: 快照时间（ISO8601）
        - title: 当时的需求标题
        - trigger: 触发原因（regenerate / clarify 等）
        - extra_notes: 用户补充说明
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    versions = list((record.srr_json or {}).get("_version_history", []))
    # 列表视图：剥离 srr 大字段，仅返回摘要
    summary = [{
        "version_index": v.get("version_index", idx + 1),
        "snapshot_at": v.get("snapshot_at", ""),
        "title": v.get("title", ""),
        "trigger": v.get("trigger", ""),
        "extra_notes": (v.get("extra_notes", "") or "")[:200],
    } for idx, v in enumerate(versions)]
    summary.reverse()  # 最新在前

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "items": summary,
            "total": len(summary),
            "current_title": record.title,
            "current_status": record.status,
            "current_updated_at": record.updated_at.isoformat() if record.updated_at else "",
        },
    )


@router.get("/{req_id}/versions/{version_index}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_requirement_version(
    req_id: str,
    version_index: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """查看指定历史版本的完整 SRR 快照。

    Args:
        req_id: 需求 ID
        version_index: 版本序号（从 1 开始）
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    versions = list((record.srr_json or {}).get("_version_history", []))
    target = next((v for v in versions if v.get("version_index") == version_index), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"版本 v{version_index} 不存在（共 {len(versions)} 个历史版本）")

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "version_index": target.get("version_index"),
            "snapshot_at": target.get("snapshot_at", ""),
            "title": target.get("title", ""),
            "trigger": target.get("trigger", ""),
            "extra_notes": target.get("extra_notes", ""),
            "srr": target.get("srr", {}),
        },
    )


@router.post("/{req_id}/versions/{version_index}/restore", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def restore_requirement_version(
    req_id: str,
    version_index: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """切换使用指定历史版本（用版本快照替换当前 SRR）。

    当前 SRR 会先作为新版本快照保存，然后再被目标版本覆盖。
    v2.2 新增，大哥要求历史版本可直接切换使用。
    """
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    old_srr = dict(record.srr_json or {})
    versions = list(old_srr.get("_version_history", []))
    target = next((v for v in versions if v.get("version_index") == version_index), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"版本 v{version_index} 不存在（共 {len(versions)} 个历史版本）")

    target_srr = target.get("srr", {})
    if not isinstance(target_srr, dict) or not target_srr:
        raise HTTPException(status_code=400, detail="目标版本无有效 SRR 数据")

    # 当前 SRR 先保存为历史版本
    SNAPSHOT_KEYS = {"_clarify_state", "_kb_query_logs", "_clarify_history", "_version_history"}
    current_snapshot = {k: v for k, v in old_srr.items() if k not in SNAPSHOT_KEYS}
    current_title = (old_srr.get("requirement", {}) or {}).get("title", record.title)
    versions.append({
        "version_index": len(versions) + 1,
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "title": current_title,
        "trigger": "restore",
        "extra_notes": f"切换至 v{version_index} 前自动快照",
        "srr": current_snapshot,
    })

    # 用目标版本 SRR 替换当前
    restored_srr = dict(target_srr)
    restored_srr["_version_history"] = versions
    # 保留 KB 查询日志 + 澄清历史
    for preserve_key in ("_kb_query_logs", "_clarify_history"):
        if preserve_key in old_srr:
            restored_srr[preserve_key] = old_srr[preserve_key]
    record.srr_json = restored_srr
    record.status = "pending_review"
    await session.commit()

    logger.info(
        "[API/requirements] 版本切换: req_id={}, v{} -> restored, new_history_count={}",
        req_id, version_index, len(versions),
    )
    return ApiResponse(
        success=True,
        message=f"已切换至 v{version_index}",
        data={"req_id": req_id, "version_index": version_index, "status": "pending_review"},
    )


@router.post("/{req_id}/regenerate", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def regenerate_requirement(
    req_id: str,
    extra_notes: str = Form(default="", description="用户补充说明（可选，与文件至少一项）"),
    files: list[UploadFile] = File(default=[], description="重新上传的需求文件（.md / .docx / .txt，可选）"),
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """补充信息后异步重新生成需求分析（SRR）。

    v2.15 异步化（变更组 C）：
        1. 同步：构建增强文本
        2. 创建 AnalysisTask（task_type=regenerate）
        3. 启动后台协程 _run_regenerate_reparse
        4. 立即返回 task_id，前端轮询进度

    v2.17 支持补充文件上传：
        - 用户可上传补充需求文件（.md / .docx / .txt），补充文件不替代原始文件
        - 补充文件可纠正原始文件中的错误，AI 分析时以原始 + 补充合并内容为准
        - 原始文件内容保留在 raw_text 字段，补充文件内容存入 supplementary_text 字段
        - 也可仅通过 extra_notes 提供补充说明（保持原有行为）
        - 文件和补充说明至少提供一项

    允许在 status=active（已澄清/已确认）后调用。
    操作记录追加到 _clarify_history。

    Args:
        req_id: 需求 ID
        extra_notes: 用户补充说明（可选）
        files: 补充需求文件列表（可选，不替代原始文件）
        session: 数据库会话

    Returns:
        ApiResponse: data 含 task_id / req_id / status=analyzing / previous_title
    """
    extra_notes = extra_notes.strip()
    has_files = bool(files) and any(f.filename for f in files)

    # 文件和补充说明至少提供一项
    if not extra_notes and not has_files:
        raise HTTPException(status_code=400, detail="请至少提供补充说明或上传补充文件中的一项")

    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    # v2.42: 前置状态检查——archived/deprecated 状态不可重新生成
    # 允许从 draft/pending_review/confirmed/analyzing 状态触发
    if record.status in ("archived", "deprecated"):
        raise HTTPException(
            status_code=409,
            detail=f"当前状态 {record.status} 不可重新生成。"
                   "已归档/已废弃的需求请先恢复到 pending_review 再重新生成。",
        )

    # 解析补充文件（如有），存入 supplementary_text，不替换原始 raw_text
    supplementary_text: str | None = None
    if has_files:
        valid_files = [f for f in files if f.filename]
        supplementary_text, _supp_images = await _parse_uploaded_files(valid_files, session=session)
        logger.info(
            "[API/requirements] 重新生成时上传了 {} 个补充文件，supplementary_text 长度={}",
            len(valid_files), len(supplementary_text),
        )

    # v2.8.52: 快照原始状态，供失败时原样恢复
    original_status = record.status

    old_srr = dict(record.srr_json or {})
    previous_title = (old_srr.get("requirement", {}) or {}).get("title", record.title)
    original_raw = record.raw_text or ""

    # v2.42: 重新生成改造——原始资料变更为「原有解析SRR + 功能点SRR + 新上传文件 + 用户描述」
    # 走完整需求分析流程（req_agent.parse + ClarifyAgent + FP提取 + recheck），
    # 而非基于现有 SRR 修改（refine_srr）。
    # 这样可能引入新的需求内容、新的功能点、修改现有功能点和公共逻辑。
    enhanced_parts = [original_raw]

    # 注入原有解析 SRR（业务字段，剔除内部状态字段）
    SNAPSHOT_EXCLUDE_KEYS = {"_clarify_state", "_kb_query_logs", "_clarify_history",
                             "_version_history", "_baseline_context",
                             "_finalize_degraded", "_clarify_degraded",
                             "_recheck_skipped", "_recheck_error",
                             "_clarify_force_passed", "_clarify_force_passed_reason"}
    old_srr_snapshot = {k: v for k, v in old_srr.items() if k not in SNAPSHOT_EXCLUDE_KEYS}
    if old_srr_snapshot:
        enhanced_parts.append(
            f"\n\n## 原有需求分析结果（SRR）\n```json\n"
            f"{json.dumps(old_srr_snapshot, ensure_ascii=False, indent=2)}\n```"
        )

    # 注入功能点 SRR（查询现有 FP 及其子 SRR）
    try:
        fp_stmt = (
            select(FunctionalPoint)
            .where(FunctionalPoint.requirement_id == req_id)
            .order_by(FunctionalPoint.created_at)
        )
        fp_result = await session.execute(fp_stmt)
        fp_rows = fp_result.scalars().all()
        if fp_rows:
            fp_summaries = []
            for fp in fp_rows:
                fp_item = {
                    "name": fp.name,
                    "fp_type": fp.fp_type,
                    "test_hints": fp.test_hints or [],
                }
                if isinstance(fp.srr_json, dict):
                    fp_item["sub_srr"] = fp.srr_json
                fp_summaries.append(fp_item)
            enhanced_parts.append(
                f"\n\n## 原有功能点分析结果（FP SRR）\n```json\n"
                f"{json.dumps(fp_summaries, ensure_ascii=False, indent=2)}\n```"
            )
            logger.info("[API/requirements] 重新生成注入 {} 个功能点 SRR", len(fp_summaries))
    except Exception as fp_e:
        logger.warning("[API/requirements] 查询功能点 SRR 失败(降级跳过): {}", str(fp_e)[:200])

    # 注入补充文件内容
    if supplementary_text:
        enhanced_parts.append(f"\n\n## 补充需求文件内容（重新生成）\n{supplementary_text}")

    # 注入已有 KB 补全
    kb_augments = (old_srr.get("_clarify_state", {}) or {}).get("auto_augmentations", [])
    if kb_augments:
        enhanced_parts.append("\n\n## KB 自动补全信息\n")
        for aug in kb_augments:
            enhanced_parts.append(f"- {aug.get('field', '')}: {str(aug.get('value', ''))[:300]}")

    # 注入用户补充说明
    if extra_notes:
        enhanced_parts.append(f"\n\n## 补充说明（重新生成）\n{extra_notes}")
    enhanced_raw = "\n".join(enhanced_parts)

    logger.info(
        "[API/requirements] 启动异步重新生成 SRR: req_id={}, has_files={}, extra_notes_len={}, enhanced_len={}",
        req_id, has_files, len(extra_notes), len(enhanced_raw),
    )

    # v2.16: 冲突检测（v2.8.52: 传入 session 启用 advisory lock）
    await _check_no_running_task(req_id, session=session)

    # v2.15: 标记需求为 analyzing 状态
    record.status = "analyzing"
    await session.commit()

    # 创建 AnalysisTask + 启动后台协程
    task_id = gen_task_id()
    task = AnalysisTask(
        id=task_id,
        task_type="regenerate",
        req_id=req_id,
        product_id=record.product_id,
        status="pending",
        phase="parse",
        progress=0,
        message="已接收补充信息，准备重新生成..." if not has_files else "已接收补充文件，准备重新生成...",
        created_by="api",
    )
    session.add(task)
    await session.commit()

    _create_bg_task(_run_regenerate_reparse(
        task_id=task_id,
        req_id=req_id,
        enhanced_raw=enhanced_raw,
        original_raw=original_raw,
        product_id=record.product_id,
        previous_title=previous_title,
        old_srr=old_srr,
        extra_notes=extra_notes,
        supplementary_text=supplementary_text,
        original_status=original_status,
    ))

    await log_operation(
        username=current_user.username, user_id=current_user.user_id,
        action="regenerate_requirement", target_type="requirement", target_id=req_id,
        detail={"has_files": has_files, "extra_notes_len": len(extra_notes)},
    )

    return ApiResponse(
        success=True,
        message="已接收补充信息，正在后台重新生成",
        data={
            "task_id": task_id,
            "req_id": req_id,
            "status": "analyzing",
            "previous_title": previous_title,
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

    # v2.46: 校验所属项目是否可编辑（pending_archive/archived 状态下冻结）
    await _assert_project_editable(session, record.project_id)

    # 只更新需要的字段
    # v2.45 修复：检测 cross_fp_interactions 变更，手动编辑依赖规则时同步 FPDependency 表
    old_srr_json = dict(record.srr_json) if isinstance(record.srr_json, dict) else {}
    old_cross_fp = old_srr_json.get("cross_fp_interactions", {})
    new_cross_fp = req.srr_json.get("cross_fp_interactions", {})
    cross_fp_changed = old_cross_fp != new_cross_fp

    record.srr_json = req.srr_json
    if req.raw_text is not None:
        record.raw_text = req.raw_text

    # 同步顶层元数据（从 SRR 同步）
    srr = req.srr_json
    req_data = srr.get("requirement", {})
    record.title = req_data.get("title", record.title)
    record.priority = req_data.get("priority", record.priority)
    record.modules = req_data.get("modules", record.modules)
    # v2.33: 自增 revision（手动编辑属于内容变更）
    record.revision = (record.revision or 0) + 1

    # v2.45 修复：cross_fp_interactions 变更时，从 SRR 重建 FPDependency 表
    # 解析 cross_fp_flows 提取依赖关系，与现有 FPDependency 对齐
    if cross_fp_changed:
        try:
            from sqlalchemy import delete as _sa_delete
            # 从 cross_fp_flows 提取依赖关系
            flows = (new_cross_fp or {}).get("cross_fp_flows", []) if isinstance(new_cross_fp, dict) else []
            # 构建 dep 列表（_save_fp_dependencies 期望的格式）
            new_deps = []
            for flow in flows:
                if not isinstance(flow, dict):
                    continue
                src = flow.get("source_fp", "")
                tgt = flow.get("target_fp", "")
                if src and tgt and src != tgt:
                    new_deps.append({
                        "source_fp_name": src,
                        "target_fp_name": tgt,
                        "relation_type": flow.get("relation_type", "depends_on"),
                        "description": flow.get("description", ""),
                    })
            # 获取当前需求下所有 FP ID（用于只清理该需求范围内的依赖）
            fp_ids_result = await session.execute(
                select(FunctionalPoint.id).where(FunctionalPoint.requirement_id == req_id)
            )
            fp_ids = [r[0] for r in fp_ids_result.all()]
            if fp_ids:
                # 清理旧的 FPDependency（仅活跃的，不清理已冻结的版本快照）
                await session.execute(
                    _sa_delete(FPDependency).where(
                        FPDependency.source_fp_id.in_(fp_ids),
                        FPDependency.target_fp_id.in_(fp_ids),
                        FPDependency.is_active == True,  # noqa: E712
                    )
                )
                # 保存新的依赖关系
                saved = await _save_fp_dependencies(session, req_id, new_deps)
                logger.info(
                    "[API/requirements] 手动编辑触发 FPDependency 重建: req_id={}, 新建 {} 条依赖",
                    req_id, saved,
                )
        except Exception as e:
            logger.error(
                "[API/requirements] cross_fp_interactions 同步 FPDependency 失败（不阻断 SRR 更新）: req_id={}, error={}",
                req_id, str(e)[:200],
            )

    await session.commit()

    logger.info("[API/requirements] 需求已更新: req_id={}, revision={}", req_id, record.revision)

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
    # 版本关系字段：superseded_by（被谁取代）/ supersedes（取代谁）
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


@router.delete("/{req_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def delete_requirement(
    req_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """删除需求。

    规则（v2.1 设计；v2.20 权限放宽为 require_write）：
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

    # v3.1.3 P1-3：error_cases.original_case_id 对 test_cases 是 RESTRICT 外键，
    # 必须先删该需求下所有用例引用的 ErrorCase，否则整体删除触发 IntegrityError 500
    tc_ids_sub = select(TestCase.id).where(TestCase.req_id == req_id)
    ec_count = await session.execute(
        sql_delete(ErrorCase).where(ErrorCase.original_case_id.in_(tc_ids_sub))
    )
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
        "error_cases=%d, test_cases=%d, gen_batches=%d, req_deps=%d, analysis_tasks=%d, traceability=%d, fp=%d",
        ec_count.rowcount, tc_count.rowcount, gb_count.rowcount, dep_count.rowcount, at_count.rowcount, tr_count.rowcount, len(fp_ids),
    )

    await session.delete(record)
    await session.commit()

    # 同步删除向量库中的记录（主需求向量 + FP-{fp_id} 向量，避免孤儿）
    try:
        from app.db.milvus_store import vector_store

        # 删除主需求向量
        await asyncio.to_thread(vector_store.delete_requirement_from_kb, req_id)
        logger.info("[API/requirements] 已同步删除向量库主记录: req_id={}", req_id)

        # P0 修复：级联删除 FP-{fp_id} 向量，避免孤儿
        for fp_id in fp_ids:
            try:
                await asyncio.to_thread(vector_store.delete_requirement_from_kb, f"FP-{fp_id}")
            except Exception as fp_err:
                logger.warning("[API/requirements] FP 向量删除失败(不阻断): fp_id={}, error={}", fp_id, str(fp_err)[:100])
        if fp_ids:
            logger.info("[API/requirements] 已同步删除 {} 个 FP 向量", len(fp_ids))
    except Exception as e:
        logger.warning("[API/requirements] 向量库删除失败(不阻断): req_id={}, error={}", req_id, str(e)[:100])

    await log_operation(
        username=current_user.username, user_id=current_user.user_id,
        action="delete_requirement", target_type="requirement", target_id=req_id,
        detail={"status": record.status},
    )

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
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """归档需求（v2.45 起已迁移至项目级归档）。

    v2.45 变更：归档操作统一由 `POST /products/projects/{proj_id}/archive` 触发，
    项目归档时项目下所有 confirmed 需求自动归档。本端点保留仅为向后兼容，
    但会拒绝调用并引导使用项目级归档，避免出现：
    - FP 变 archived 但 FPDependency 未冻结（is_active 仍为 True）
    - 无版本快照（fp_dependency_versions）记录
    - 向量库 metadata 缺失 project_id 字段

    请改用：POST /api/products/projects/{project_id}/archive
    """
    # 查询需求所属项目
    record = await session.get(Requirement, req_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    project_id = record.project_id or "（未关联项目）"
    raise HTTPException(
        status_code=409,
        detail=(
            f"v2.45 起需求归档已迁移至项目级。请改用项目归档端点："
            f"POST /api/products/projects/{project_id}/archive"
        )
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

    # 同步更新 Milvus 向量库中的元数据（主需求 + FP 向量）
    milvus_updated = False
    milvus_error = ""
    try:
        from app.db.milvus_store import vector_store

        # 更新主需求向量
        await asyncio.to_thread(
            vector_store.update_requirement_metadata,
            req_id,
            {"go_live_date": parsed_go_live.isoformat()},
        )

        # P2 修复：同步更新该需求下所有 FP 向量的 go_live_date
        fp_result = await session.execute(
            select(FunctionalPoint.id).where(FunctionalPoint.requirement_id == req_id)
        )
        fp_ids = [r[0] for r in fp_result.fetchall()]
        fp_update_count = 0
        for fp_id in fp_ids:
            try:
                await asyncio.to_thread(
                    vector_store.update_requirement_metadata,
                    f"FP-{fp_id}",
                    {"go_live_date": parsed_go_live.isoformat()},
                )
                fp_update_count += 1
            except Exception as fp_err:
                logger.warning("[API/requirements] FP 向量元数据更新失败(不阻断): fp_id={}, error={}", fp_id, str(fp_err)[:100])
        if fp_ids:
            logger.info("[API/requirements] 已同步 {} 个 FP 向量的 go_live_date", fp_update_count)

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
    """分析需求变更的影响范围（v2.35: 基于 FP 依赖图谱分析受影响功能点）。

    v2.35 变更：
        - 保留模块级影响分析（direct_modules / indirect_modules）作为参考
        - 移除 affected_requirements（不再分析受影响的需求）
        - 新增 affected_fps：基于 FP 依赖图谱 BFS 分析受影响的功能点
        - 已归档 FP 查询知识库展示功能点说明（kb_context）

    Args:
        req_id: 需求 ID
        session: 数据库会话

    Returns:
        ApiResponse: data 含 direct_modules / indirect_modules / affected_fps / total_affected
    """
    # 1. 查询需求
    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    # 2. 调用 ImpactAnalysisAgent 的完整影响分析（模块 BFS + FP 依赖图谱 BFS + KB 查询）
    from app.agents.impact_agent import impact_agent

    try:
        impact = await impact_agent.analyze_requirement_impact(req_id, session)
    except Exception as e:
        logger.error("[API/requirements] 影响分析失败: {}", str(e)[:200])
        raise HTTPException(status_code=500, detail=f"影响分析失败: {str(e)[:200]}")

    if "error" in impact:
        raise HTTPException(status_code=404, detail=impact["error"])

    # 3. 查询模块名称，过滤孤儿依赖（模块已删除但依赖记录还在）
    mod_impact = impact.get("module_impact", {})
    direct_mod_ids = mod_impact.get("direct", [])
    indirect_mods = mod_impact.get("indirect", [])
    all_mod_ids = [m if isinstance(m, str) else m.get("module_id", "") for m in direct_mod_ids]
    all_mod_ids += [m.get("module_id", "") for m in indirect_mods if m.get("module_id")]
    all_mod_ids = [m for m in all_mod_ids if m]

    mod_names: dict[str, str] = {}
    valid_ids: set[str] = set()
    if all_mod_ids:
        from app.db.models import Module as ModuleModel
        mod_stmt = select(ModuleModel.id, ModuleModel.name).where(ModuleModel.id.in_(all_mod_ids))
        mod_result = await session.execute(mod_stmt)
        mod_names = {row[0]: row[1] for row in mod_result.all()}
        valid_ids = set(mod_names.keys())

    # 4. 构建返回数据
    req_info = impact.get("requirement", {})
    affected_fps = impact.get("affected_fps", [])

    return ApiResponse(
        success=True,
        message=f"影响分析完成：{len(direct_mod_ids)} 个直接模块，"
                f"{len(indirect_mods)} 个间接模块，"
                f"{len(affected_fps)} 个受影响功能点",
        data={
            "req_id": req_id,
            "req_title": req_info.get("title", record.title),
            "direct_modules": [
                {"id": m, "name": mod_names.get(m, f"已删除模块({m})")}
                for m in direct_mod_ids
                if m in valid_ids
            ],
            "indirect_modules": [
                {
                    "id": m.get("module_id", ""),
                    "name": mod_names.get(m.get("module_id", ""), f"已删除模块({m.get('module_id', '')})"),
                    "reason": m.get("reason", ""),
                    "depth": m.get("depth", 1),
                }
                for m in indirect_mods
                if m.get("module_id", "") in valid_ids
            ],
            "affected_fps": affected_fps,
            "total_affected": impact.get("total_affected", len(affected_fps)),
        },
    )


# ============================================================================
# v1.4 功能点（FunctionalPoint）API
# ============================================================================

from app.core import prompt_manager as pm
from app.core.llm_client import llm_client
from app.utils.id_gen import gen_requirement_id  # 复用 ID 生成


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


@router.post("/{req_id}/confirm", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def confirm_requirement(
    req_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """确认需求完成——标记为 confirmed（确认完成），允许生成测试用例。

    前置条件：
        - 需求状态为 pending_review（待确认）
        - 用户已确认 SRR 内容、功能点、影响分析无误

    确认后：
        - status → confirmed（确认完成）
        - 只有 confirmed 状态的需求可以触发用例生成

    如果后续需要重新生成，用户可再次提交澄清或重新分析，
    状态会自动重置为 pending_review 或 draft。
    """
    req = await session.get(Requirement, req_id)
    if not req:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    if req.status != "pending_review":
        raise HTTPException(
            status_code=409,
            detail=f"当前状态 {req.status} 不可确认（仅 pending_review 可确认）",
        )

    # v2.41: 校验降级标记——降级 SRR 需用户显式确认知情
    srr = req.srr_json or {}
    degraded_flags: list[str] = []
    if srr.get("_finalize_degraded"):
        degraded_flags.append("最终 SRR 生成降级")
    if srr.get("_clarify_degraded"):
        degraded_flags.append("澄清重解析降级")
    if srr.get("_recheck_skipped"):
        degraded_flags.append("再验证被跳过")
    if degraded_flags:
        raise HTTPException(
            status_code=409,
            detail=f"需求存在降级标记（{', '.join(degraded_flags)}），SRR 可能不完整。"
                   f"请先人工复核 SRR 内容，确认无误后通过 PUT /requirements/{req_id} 清除降级标记再确认",
        )

    # v1.6: 校验功能点审核状态——所有 FP 必须已审核通过
    from app.db.models import FunctionalPoint as FpModel
    fp_stmt = select(FpModel).where(
        FpModel.requirement_id == req_id,
        FpModel.status != "approved",
    )
    fp_result = await session.execute(fp_stmt)
    unapproved_fps = fp_result.scalars().all()
    if unapproved_fps:
        fp_names = [fp.name for fp in unapproved_fps[:5]]
        suffix = "..." if len(unapproved_fps) > 5 else ""
        raise HTTPException(
            status_code=409,
            detail=f"存在 {len(unapproved_fps)} 个未审核的功能点（{', '.join(fp_names)}{suffix}），"
                   f"请先审核所有功能点后再确认",
        )

    # v1.6: 校验影响分析确认状态
    # 若 SRR 中存在 impact_analysis 且包含未确认的受影响 FP，则阻止确认
    # v2.8.52: 修复死锁——原逻辑要求所有 is_affected=True 才能确认，
    # 但 confirm_impact_analysis 只能设置 is_affected=True，用户无法标记"确认不受影响"。
    # 改为检查 confirmed_at 字段（用户已审核标记），而非 is_affected 值。
    srr = req.srr_json or {}
    impact_list = srr.get("impact_analysis", []) or []
    if impact_list:
        unconfirmed_impacts = [item for item in impact_list if not item.get("confirmed_at")]
        if unconfirmed_impacts:
            fp_names = [item.get("fp_name", "") for item in unconfirmed_impacts[:5]]
            suffix = "..." if len(unconfirmed_impacts) > 5 else ""
            raise HTTPException(
                status_code=409,
                detail=f"存在 {len(unconfirmed_impacts)} 个未确认的影响范围功能点"
                       f"（{', '.join(fp_names)}{suffix}），"
                       f"请先通过影响分析确认功能点后再确认",
            )

    # v4.2.9 增强（跨需求依赖自动生成）:
    # LLM 的 fp_dependencies 提取通常只覆盖本需求内部 FP，跨需求引用实际落在
    # SRR.cross_fp_interactions.cross_fp_flows（如 "权限回收 -> 权限控制（v1.0）"），
    # 此前这些 flows 从未被转换为依赖边，导致"需求内部无法生成跨需求关联关系"。
    # 确认时统一把 cross_fp_flows 转为依赖描述并补建（_save_fp_dependencies 的
    # 跨需求解析层会把 "权限控制（v1.0）" 归一化匹配到已归档 FP "权限控制"），
    # 补建的 pending 边在下方自动审核中置为 approved 而生效，随即参与影响分析。
    try:
        cfi = (srr or {}).get("cross_fp_interactions") or {}
        flows = cfi.get("cross_fp_flows") or []
        if flows:
            flow_deps: list[dict] = []
            for flow in flows:
                if not isinstance(flow, dict):
                    continue
                src = flow.get("source_fp", "") or ""
                tgt = flow.get("target_fp", "") or ""
                if src and tgt and src != tgt:
                    rel = flow.get("relation_type") or flow.get("interaction_type") or "depends_on"
                    if rel not in ("depends_on", "data_flows", "invariants", "lifecycle_scenarios", "conflicts_with"):
                        rel = "depends_on"
                    flow_deps.append({
                        "source_fp_name": src,
                        "target_fp_name": tgt,
                        "relation_type": rel,
                        "description": flow.get("description", "") or "",
                    })
            if flow_deps:
                flow_saved = await _save_fp_dependencies(session, req_id, flow_deps)
                if flow_saved:
                    logger.info(
                        "[FP-Dep] 需求确认时由 cross_fp_flows 补充创建 {} 条依赖边（含跨需求引用）: req_id={}",
                        flow_saved, req_id,
                    )
    except Exception as e:
        logger.warning("[FP-Dep] 确认时从 cross_fp_flows 补建依赖边失败（不阻断确认）: {}", str(e)[:200])

    # v3.1: 需求确认时自动审核通过该需求产生的依赖关系（无需去依赖图谱人工审核）。
    # 依赖边由本需求 FP 拆分生成（source 属于本需求），确认 SRR 即代表用户认可其依赖关系，
    # 因此直接把本需求 source 的 pending 依赖边置为 approved，使其参与影响分析。
    fp_id_stmt = select(FpModel.id).where(FpModel.requirement_id == req_id)
    own_fp_ids = [r[0] for r in (await session.execute(fp_id_stmt)).all()]
    if own_fp_ids:
        dep_stmt = select(FPDependency).where(
            FPDependency.source_fp_id.in_(own_fp_ids),
            FPDependency.review_status == "pending",
        )
        pending_deps = (await session.execute(dep_stmt)).scalars().all()
        auto_approved = 0
        for dep in pending_deps:
            dep.review_status = "approved"
            dep.reviewed_by = current_user.username
            dep.reviewed_at = datetime.now(timezone.utc)
            session.add(FPDependencyHistory(
                dependency_id=dep.id,
                source_fp_id=dep.source_fp_id,
                target_fp_id=dep.target_fp_id,
                relation_type=dep.relation_type,
                description=dep.description,
                action="approved",
                old_review_status="pending",
                new_review_status="approved",
                changed_by=current_user.username,
                change_reason="需求确认时自动审核通过",
                related_req_id=req_id,
            ))
            auto_approved += 1
        if auto_approved:
            logger.info(
                "[FP] 需求确认自动审核通过 {} 条依赖关系: req_id={}",
                auto_approved, req_id,
            )

    req.status = "confirmed"
    await session.commit()

    logger.info("[API/requirements] 需求确认完成: req_id={}, title={}", req_id, req.title)
    await log_operation(
        username=current_user.username, user_id=current_user.user_id,
        action="confirm_requirement", target_type="requirement", target_id=req_id,
    )
    return ApiResponse(
        success=True,
        data={"req_id": req_id, "status": "confirmed"},
    )


async def _check_no_running_task(
    req_id: str,
    exclude_task_id: str | None = None,
    session: AsyncSession | None = None,
) -> None:
    """检查需求是否有进行中的任务（分析/重生成/FP合并/澄清），如有则拒绝。

    互斥规则：同一需求同时只能有一个 running/pending 的分析类任务。
    - upload_requirement / regenerate / merge_fp / submit_clarify 四种操作互斥
    - 已有任务 running/pending 时抛 HTTPException 409

    v2.8.52: 使用 PG advisory lock 消除 TOCTOU 竞态——
    互斥检查与任务创建在调用方同一事务中原子执行，防止并发请求同时通过检查。
    调用方需传入 session 参数，advisory lock 持有到该事务提交/回滚。

    Args:
        req_id: 需求 ID
        exclude_task_id: 排除的任务 ID（用于同一个任务的幂等重试场景）
        session: 调用方共享的数据库会话（lock 持有到其事务结束）

    Raises:
        HTTPException 409: 存在冲突任务
    """
    from sqlalchemy import select as _s, or_ as _or_, text as _text

    if session is None:
        # 向后兼容：无 session 时用独立 session（无锁保护，仅提示）
        session = async_session()
        own_session = True
    else:
        own_session = False

    try:
        # PG advisory lock: 同一 req_id 串行化互斥检查和任务创建
        # 锁在调用方事务提交/回滚时自动释放
        await session.execute(
            _text("SELECT pg_advisory_xact_lock(hashtext(:rid))"), {"rid": req_id}
        )

        stmt = _s(AnalysisTask).where(
            AnalysisTask.req_id == req_id,
            AnalysisTask.status.in_(["pending", "running"]),
            _or_(
                AnalysisTask.task_type == "upload_requirement",
                AnalysisTask.task_type == "regenerate",
                AnalysisTask.task_type == "merge_fp",
                AnalysisTask.task_type == "submit_clarify",
            ),
        )
        if exclude_task_id:
            stmt = stmt.where(AnalysisTask.id != exclude_task_id)
        result = await session.execute(stmt)
        conflict = result.scalar_one_or_none()
        if conflict:
            labels = {
                "upload_requirement": "需求解析",
                "regenerate": "重新生成",
                "merge_fp": "功能点合并",
                "submit_clarify": "澄清重解析",
            }
            label = labels.get(conflict.task_type, conflict.task_type)
            raise HTTPException(
                status_code=409,
                detail=f"需求正在进行「{label}」任务（{conflict.id}），请等待完成后再操作",
            )
    finally:
        if own_session:
            await session.close()


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
                "module_id": fp.module_id, "modules": fp.modules or [],
                "priority": fp.priority, "status": fp.status,
                "srr_json": fp.srr_json, "atomicity_check": fp.atomicity_check,
                "test_hints": fp.test_hints,
                "vectorization_status": fp.vectorization_status,
                "needs_module": not fp.module_id and not (fp.modules or []),
                "created_at": fp.created_at.isoformat() if fp.created_at else "",
            }
            for fp in fps
        ],
        "total": len(fps),
        "is_archived": req.status == "archived",
    })


@router.get("/{req_id}/fp/dependencies", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def fp_dependencies(req_id: str, session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """获取功能点依赖关系。

    v2.3: 返回 review_status 字段，前端按审核状态区分颜色。
    """
    fp_ids_result = await session.execute(
        select(FunctionalPoint.id).where(FunctionalPoint.requirement_id == req_id)
    )
    fp_ids = {r[0] for r in fp_ids_result.fetchall()}

    # v2.3: 查询以当前需求 FP 为 source 或 target 的所有依赖（支持跨需求依赖查看）
    dep_result = await session.execute(
        select(FPDependency).where(
            (FPDependency.source_fp_id.in_(fp_ids)) | (FPDependency.target_fp_id.in_(fp_ids))
        ).order_by(FPDependency.created_at.desc())
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
                "review_status": d.review_status,
                "reviewed_by": d.reviewed_by,
                "reviewed_at": d.reviewed_at.isoformat() if d.reviewed_at else "",
                "created_by": d.created_by,
                "created_at": d.created_at.isoformat() if d.created_at else "",
            }
            for d in deps
        ]
    })


# ============================================================================
# v2.3: FP 依赖审核流程
# 新建依赖默认 pending 状态，需审核通过后才生效（参与影响分析）
# 支持审核通过/驳回，并记录每次变动历史
# ============================================================================

class FPDependencyReviewRequest(BaseModel):
    """FP 依赖审核请求。"""
    action: str  # approve / reject
    reason: str = ""  # 审核说明（可选）


@router.patch("/{req_id}/fp/dependencies/{dep_id}/review", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_review_dependency(
    req_id: str,
    dep_id: int,
    body: FPDependencyReviewRequest,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """审核功能点依赖关系。

    v2.3 新增。将依赖从 pending 状态审核为 approved 或 rejected。
    - approved: 依赖关系生效，参与影响分析
    - rejected: 依赖关系不生效，但保留记录

    Args:
        req_id: 需求 ID
        dep_id: 依赖 ID
        body: 审核请求，含 action (approve/reject) 和 reason
        session: 数据库会话
        current_user: 当前用户

    Returns:
        ApiResponse: 含更新后的 review_status
    """
    await _load_active_requirement(req_id, session)
    dep = await session.get(FPDependency, dep_id)
    if not dep:
        raise HTTPException(status_code=404, detail="依赖关系不存在")

    # 校验 source_fp 属于该 req_id
    source_fp = await session.get(FunctionalPoint, dep.source_fp_id)
    if not source_fp or source_fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="依赖关系不属于该需求")

    if body.action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action 必须为 approve 或 reject")

    if dep.review_status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"依赖关系当前状态为 {dep.review_status}，仅 pending 状态可审核",
        )

    old_status = dep.review_status
    new_status = "approved" if body.action == "approve" else "rejected"
    dep.review_status = new_status
    dep.reviewed_by = current_user.username
    dep.reviewed_at = datetime.now(timezone.utc)

    # 写入历史记录
    session.add(FPDependencyHistory(
        dependency_id=dep.id,
        source_fp_id=dep.source_fp_id,
        target_fp_id=dep.target_fp_id,
        relation_type=dep.relation_type,
        description=dep.description,
        action=new_status,
        old_review_status=old_status,
        new_review_status=new_status,
        changed_by=current_user.username,
        change_reason=body.reason or f"用户{('审核通过' if new_status == 'approved' else '驳回')}依赖关系",
        related_req_id=req_id,
    ))

    await session.commit()
    logger.info(
        "[FP] 依赖审核: dep_id={}, {} → {}, by={}",
        dep_id, old_status, new_status, current_user.username,
    )

    return ApiResponse(
        success=True,
        message=f"依赖关系已{('审核通过' if new_status == 'approved' else '驳回')}",
        data={
            "id": dep.id,
            "review_status": dep.review_status,
            "reviewed_by": dep.reviewed_by,
            "reviewed_at": dep.reviewed_at.isoformat() if dep.reviewed_at else "",
        },
    )


@router.get("/{req_id}/fp/dependencies/history", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def fp_dependency_history(
    req_id: str,
    dep_id: int | None = Query(default=None, description="可选，按依赖ID筛选"),
    action: str | None = Query(default=None, description="可选，按操作类型筛选 (created/approved/rejected/deleted)"),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取功能点依赖关系变动历史。

    v2.3 新增。记录每次依赖关系的创建、审核、修改、删除操作。
    支持按依赖ID、操作类型筛选，关联显示对应需求变更导致的依赖关系变化。

    Args:
        req_id: 需求 ID
        dep_id: 可选，按依赖ID筛选
        action: 可选，按操作类型筛选
        limit: 返回条数上限
        session: 数据库会话

    Returns:
        ApiResponse: data.items 含历史记录列表
    """
    # 查询当前需求下所有 FP ID
    fp_ids_result = await session.execute(
        select(FunctionalPoint.id).where(FunctionalPoint.requirement_id == req_id)
    )
    fp_ids = {r[0] for r in fp_ids_result.fetchall()}
    if not fp_ids:
        return ApiResponse(success=True, message="ok", data={"items": [], "total": 0})

    # 查询历史记录（source 或 target 属于当前需求 FP）
    stmt = select(FPDependencyHistory).where(
        (FPDependencyHistory.source_fp_id.in_(fp_ids))
        | (FPDependencyHistory.target_fp_id.in_(fp_ids))
        | (FPDependencyHistory.related_req_id == req_id)
    )
    if dep_id is not None:
        stmt = stmt.where(FPDependencyHistory.dependency_id == dep_id)
    if action:
        stmt = stmt.where(FPDependencyHistory.action == action)

    stmt = stmt.order_by(FPDependencyHistory.created_at.desc()).limit(limit)
    result = await session.execute(stmt)
    history_rows = result.scalars().all()

    # 查询 FP 名称映射
    all_fp_ids = set()
    for h in history_rows:
        all_fp_ids.add(h.source_fp_id)
        all_fp_ids.add(h.target_fp_id)
    name_map = {}
    if all_fp_ids:
        names_result = await session.execute(
            select(FunctionalPoint.id, FunctionalPoint.name).where(FunctionalPoint.id.in_(all_fp_ids))
        )
        name_map = {row[0]: row[1] for row in names_result.fetchall()}

    return ApiResponse(success=True, message="ok", data={
        "items": [
            {
                "id": h.id,
                "dependency_id": h.dependency_id,
                "source_fp_id": h.source_fp_id,
                "source_name": name_map.get(h.source_fp_id, ""),
                "target_fp_id": h.target_fp_id,
                "target_name": name_map.get(h.target_fp_id, ""),
                "relation_type": h.relation_type,
                "description": h.description,
                "action": h.action,
                "old_review_status": h.old_review_status,
                "new_review_status": h.new_review_status,
                "changed_by": h.changed_by,
                "change_reason": h.change_reason,
                "related_req_id": h.related_req_id,
                "created_at": h.created_at.isoformat() if h.created_at else "",
            }
            for h in history_rows
        ],
        "total": len(history_rows),
    })


# FP 依赖合法关系类型（与 FPDependency 模型注释一致）
_FP_RELATION_TYPES = {"depends_on", "data_flows", "invariants", "lifecycle_scenarios", "conflicts_with"}


@router.post("/{req_id}/fp/dependencies", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_add_dependency(
    req_id: str,
    body: FPDependencyCreateRequest,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """创建功能点依赖关系。

    v2.3: 新建依赖默认 review_status='pending'，需审核通过后才生效（参与影响分析）。

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
        review_status="pending",  # 默认待审核
        created_by=current_user.username,
    )
    session.add(dep)
    await session.flush()  # 获取 dep.id

    # v2.3: 写入依赖历史记录
    session.add(FPDependencyHistory(
        dependency_id=dep.id,
        source_fp_id=dep.source_fp_id,
        target_fp_id=dep.target_fp_id,
        relation_type=dep.relation_type,
        description=dep.description,
        action="created",
        old_review_status="",
        new_review_status="pending",
        changed_by=current_user.username,
        change_reason="新建依赖关系",
        related_req_id=req_id,
    ))

    await session.commit()
    await session.refresh(dep)

    logger.info("[FP] 依赖创建(待审核): {} -{}-> {} (req={})",
                body.source_fp_id, body.relation_type, body.target_fp_id, req_id)

    return ApiResponse(
        success=True,
        message="依赖关系已创建（待审核，审核通过后生效）",
        data={
            "id": dep.id, "source_fp_id": dep.source_fp_id,
            "target_fp_id": dep.target_fp_id, "relation_type": dep.relation_type,
            "review_status": dep.review_status,
        },
    )


@router.delete("/{req_id}/fp/dependencies/{dep_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_remove_dependency(
    req_id: str,
    dep_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """删除功能点依赖关系。归档后不可用。

    v2.3: 删除前写入历史记录。
    """
    await _load_active_requirement(req_id, session)
    dep = await session.get(FPDependency, dep_id)
    if not dep:
        raise HTTPException(status_code=404, detail="依赖关系不存在")

    # 校验 source_fp 属于该 req_id
    source_fp = await session.get(FunctionalPoint, dep.source_fp_id)
    if not source_fp or source_fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="依赖关系不属于该需求")

    # v2.3: 写入删除历史记录
    session.add(FPDependencyHistory(
        dependency_id=None,  # 删除后置空
        source_fp_id=dep.source_fp_id,
        target_fp_id=dep.target_fp_id,
        relation_type=dep.relation_type,
        description=dep.description,
        action="deleted",
        old_review_status=dep.review_status,
        new_review_status="",
        changed_by=current_user.username,
        change_reason="用户主动删除依赖关系",
        related_req_id=req_id,
    ))

    await session.delete(dep)
    await session.commit()

    logger.info("[FP] 依赖删除: dep_id={} (req={})", dep_id, req_id)
    return ApiResponse(success=True, message="依赖关系已删除")


# ============================================================================
# v1.6.1: FP 子 SRR 手动编辑保存
# ============================================================================

class FPUpdateRequest(BaseModel):
    name: str | None = None
    module_id: str | None = None
    modules: list[str] | None = None  # v2.2: 多模块关联
    # v2.46: fp_type 增加枚举校验，与前端 FPSummary 和 fp_agent 提示词保持一致
    fp_type: Literal[
        "workflow", "constraint", "calculation",
        "api_behavior", "data_record", "config",
    ] | None = None
    priority: str | None = None
    srr_json: dict | None = None


@router.put("/{req_id}/fp/{fp_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_update(
    req_id: str,
    fp_id: str,
    body: FPUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """更新功能点名称 / 模块 / 子 SRR。v2.2 支持多模块。

    v2.35: 允许归档状态下编辑功能点，编辑后同步更新所有引用点：
        - 总 SRR fp_index / fp_summary（FP 名称/模块变更时）
        - 向量库 FP 记录（FP 已归档时重新写入 Milvus）
        - 用例 case_json.related_fp（FP 名称变更时批量更新关联用例）
        - FP 依赖关系按 ID 存储，展示层自动读取最新名称，无需额外处理
    """
    # v2.35: 归档状态也允许编辑 FP（不调用 _load_active_requirement）
    req = await session.get(Requirement, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="需求不存在")

    fp = await session.get(FunctionalPoint, fp_id)
    if not fp or fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="功能点不存在")

    # 记录变更前的名称，用于判断是否需要同步用例
    old_name = fp.name
    old_modules = list(fp.modules or [])
    old_srr_json = dict(fp.srr_json) if isinstance(fp.srr_json, dict) else {}
    name_changed = False
    srr_changed = False

    if body.name is not None and body.name != fp.name:
        fp.name = body.name
        name_changed = True
    if body.modules is not None:
        fp.modules = body.modules
    elif body.module_id is not None:
        # 旧版单模块兼容
        fp.modules = [body.module_id]
        fp.module_id = body.module_id
    if body.srr_json is not None:
        fp.srr_json = body.srr_json
        srr_changed = True

    await session.flush()

    # v2.35: 同步更新引用点
    sync_results: list[str] = []

    # 1. FP 名称变更 → 更新关联用例 case_json.related_fp / functional_point
    if name_changed:
        try:
            case_result = await session.execute(
                select(TestCase).where(
                    TestCase.req_id == req_id,
                )
            )
            cases_to_update = case_result.scalars().all()
            updated_count = 0
            for case in cases_to_update:
                case_json = dict(case.case_json) if isinstance(case.case_json, dict) else {}
                related_fp = case_json.get("related_fp", "")
                functional_point = case_json.get("functional_point", "")
                changed = False
                if related_fp == old_name:
                    case_json["related_fp"] = fp.name
                    changed = True
                if functional_point == old_name:
                    case_json["functional_point"] = fp.name
                    changed = True
                if changed:
                    case.case_json = case_json
                    updated_count += 1
            if updated_count > 0:
                sync_results.append(f"用例 related_fp 同步 {updated_count} 条")
                logger.info("[FP] 用例 related_fp 同步: fp_id={}, old_name={}, new_name={}, updated={}",
                            fp_id, old_name, fp.name, updated_count)
        except Exception as e:
            logger.error("[FP] 用例 related_fp 同步失败 fp_id={}: {}", fp_id, str(e)[:200])

    # 2. 更新总 SRR 的 fp_index / fp_summary
    try:
        await _update_fp_index_in_srr(session, req_id)
        sync_results.append("总 SRR fp_index 已更新")
    except Exception as e:
        logger.error("[FP] 总 SRR fp_index 更新失败 req_id={}: {}", req_id, str(e)[:200])

    # v2.45 修复：FP 名称变更时同步 cross_fp_interactions 中的 FP 名称引用
    # （cross_fp_flows.source_fp/target_fp、shared_business_rules.applied_fps、fp_contracts.provider_fp/consumer_fp）
    if name_changed:
        try:
            await _update_cross_fp_interactions_on_merge(session, req_id, {old_name}, fp.name)
            sync_results.append("cross_fp_interactions 已同步")
        except Exception as e:
            logger.error("[FP] cross_fp_interactions 同步失败 req_id={}: {}", req_id, str(e)[:200])

    await session.commit()

    # 3. FP 已归档 → 重新写入向量库（在 commit 后执行，避免事务未提交时写入不一致数据）
    if fp.status == "archived" and (name_changed or srr_changed):
        try:
            from app.db.milvus_store import vector_store

            fp_meta = {
                "type": "functional_point",
                "requirement_id": req_id,
                "fp_id": fp.id or "",
                "fp_name": fp.name or "",
                "modules": ",".join(fp.modules or []) if fp.modules else (fp.module_id or ""),
                "module_id": fp.module_id or "",
                "fp_type": fp.fp_type,
                "product_id": req.product_id,
                "status": "archived",
                # v2.45: 补 project_id，与 archive_project 写入的向量 metadata 保持一致
                # 避免 delete_requirement_by_project 按 project_id 过滤时漏删
                "project_id": req.project_id or "",
            }
            embed_srr = dict(fp.srr_json) if isinstance(fp.srr_json, dict) else {}
            embed_srr.setdefault("requirement", {})
            embed_srr["requirement"]["title"] = fp.name
            if fp.modules:
                embed_srr["requirement"]["modules"] = list(fp.modules)
            elif fp.module_id:
                embed_srr["requirement"]["modules"] = [fp.module_id]
            if not embed_srr["requirement"].get("summary"):
                embed_srr["requirement"]["summary"] = (
                    fp.srr_json.get("requirement", {}).get("summary", "")
                    if isinstance(fp.srr_json, dict) else ""
                ) or fp.name

            await asyncio.to_thread(
                vector_store.add_requirement,
                req_id=f"FP-{fp.id}",
                srr_json=embed_srr,
                metadata=fp_meta,
            )
            sync_results.append("向量库 FP 记录已更新")
            logger.info("[FP] 向量库 FP 记录已更新: fp_id={}", fp_id)
        except Exception as e:
            logger.warning("[FP] 向量库 FP 记录更新失败 fp_id={}: {}", fp_id, str(e)[:200])

    logger.info("[FP] 更新: fp_id={}, fields={}, sync={}", fp_id,
                [k for k, v in {"name": body.name, "module_id": body.module_id, "srr_json": body.srr_json is not None}.items() if v],
                sync_results)
    return ApiResponse(success=True, message="功能点已更新", data={"id": fp.id, "name": fp.name, "sync": sync_results})


# ============================================================================
# v3.0: FP 影响功能说明（自动生成 + 可编辑，写入 SRR impact_analysis 字段）
# ============================================================================

class ImpactDescriptionUpdateRequest(BaseModel):
    """FP 影响说明编辑保存请求。"""
    description: str
    test_focus: str = ""


# ============================================================================
# v1.7: 需求级影响分析（模块+FP 依赖 + KB + 人工审核）
# ============================================================================

class ImpactConfirmRequest(BaseModel):
    """人工审核确认受影响的 FP 列表。

    修复 H5：
    - 新增 unconfirmed_fp_ids：用户明确确认"不受影响"的 FP，写入
      impact_analysis 时标记 is_affected=False + confirmed_at，避免
      confirm_requirement 守卫认为它"未确认"而产生死锁。
    - 新增 removed_fp_ids：用户从候选列表移除（不关心 / 认为不需要验证）
      的 FP，直接从 impact_analysis 中移除对应条目。
    """

    confirmed_fp_ids: list[str] = []  # 确认「受影响」的 FP ID 列表
    unconfirmed_fp_ids: list[str] = []  # 确认「不受影响」的 FP ID 列表
    removed_fp_ids: list[str] = []  # 从 impact_analysis 中彻底移除的 FP ID 列表
    edited_descriptions: dict[str, str] = {}  # fp_id → 编辑后的影响说明


@router.post("/{req_id}/impact-analysis", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def run_impact_analysis(
    req_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """全量影响分析：模块依赖 + FP 依赖 + KB 查询。

    Returns:
        - requirement: 当前需求信息
        - module_impact: 模块级影响
        - affected_fps: 受影响 FP 列表（含 KB 上下文）
        - total_affected: 受影响 FP 总数
    """
    req = await _load_active_requirement(req_id, session)
    agent = ImpactAnalysisAgent()
    result = await agent.analyze_requirement_impact(req_id, session)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    # v3.4: 影响分析候选持久化——检测出受影响 FP 时以未确认态写入 srr.impact_analysis。
    # 作用：与 confirm_requirement 守卫（存在未确认条目时 409 阻止确认）及项目归档
    # （归档要求需求 confirmed）形成链式锁定：未确认影响范围 → 无法确认需求 → 无法归档。
    # 规则：
    #   - 仅本次分析存在受影响 FP（或历史残留未确认条目）时写入
    #   - 已确认条目（confirmed_at 存在）保留原确认结果，不覆盖
    #   - 候选中未确认的 FP 写为未确认条目（无 confirmed_at，is_affected=None）
    #   - v4.2.7 修复 P6：不在本次候选中的**已确认**旧条目同样保留，
    #     避免依赖图谱变化后用户已确认的影响范围被静默撤销（回归覆盖无声缩水）。
    #     只有"从未确认过"的历史残留未确认条目才被清除。
    affected = result.get("affected_fps") or []
    srr = dict(req.srr_json or {})
    existing_list = [e for e in (srr.get("impact_analysis") or []) if isinstance(e, dict)]
    has_unconfirmed = any(not e.get("confirmed_at") for e in existing_list)
    if affected or has_unconfirmed:
        existing_map = {e.get("fp_id"): e for e in existing_list}
        # 已确认条目原样保留（无论本次是否再次命中候选）
        new_impact: list[dict] = [e for e in existing_list if e.get("confirmed_at")]
        for a in affected:
            fid = a.get("fp_id") or ""
            if not fid:
                continue
            old = existing_map.get(fid)
            if old and old.get("confirmed_at"):
                # 已确认条目已在保留列表中，跳过（以用户确认结果为准）
                continue
            # 未确认候选：无 confirmed_at → confirm_requirement 守卫会拦截
            new_impact.append({
                "fp_id": fid,
                "fp_name": a.get("fp_name") or "",
                "fp_type": a.get("fp_type") or "",
                "module_id": a.get("module_id") or "",
                "source_req_id": a.get("req_id") or "",
                # None = 未确认（区别于 True=确认受影响 / False=确认不受影响）
                "is_affected": None,
                "impact_description": a.get("impact_reason") or f"受需求[{req.title}]影响",
            })
        srr["impact_analysis"] = new_impact
        req.srr_json = srr
        await session.commit()
        logger.info(
            "[ImpactAnalysis] 候选持久化: req={} affected={} unconfirmed={}",
            req_id, len(affected),
            len([e for e in new_impact if not e.get("confirmed_at")]),
        )

    return ApiResponse(success=True, data=result)


@router.post("/{req_id}/impact-analysis/confirm", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def confirm_impact_analysis(
    req_id: str,
    body: ImpactConfirmRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """人工审核确认受影响/不受影响 FP，写入 SRR impact_analysis。

    操作（修复 H5 死锁）：
        1. 先把 removed_fp_ids 中的条目从 impact_analysis 移除。
        2. 对 confirmed_fp_ids：标记 is_affected=True + confirmed_at。
        3. 对 unconfirmed_fp_ids：标记 is_affected=False + confirmed_at，
           这样 confirm_requirement 的「所有条目都要有 confirmed_at」守卫
           也会放行（显式"确认不受影响"也算确认）。
        4. 重复/不存在 FP 忽略；存在则覆盖旧条目。
        5. 修复 M1 竞态：PG advisory lock + SELECT FOR UPDATE 行锁，避免双写丢失。
    """
    req = await _load_active_requirement(req_id, session)
    # 行锁：避免并发 confirm_impact_analysis 双写互相覆盖
    lock_req = await session.execute(
        text("SELECT id FROM requirements WHERE id = :rid FOR UPDATE"),
        {"rid": req_id},
    )
    if not lock_req.fetchone():
        raise HTTPException(status_code=404, detail="需求不存在（行锁获取失败）")
    # v2.8.52: 使用 dict() 创建新字典，确保 SQLAlchemy 检测到 JSON 字段变更
    srr = dict(req.srr_json or {})

    removed_set = set(body.removed_fp_ids or [])
    confirmed_set = set(body.confirmed_fp_ids or [])
    unaffected_set = set(body.unconfirmed_fp_ids or [])
    # 冲突处理优先级：removed > confirmed > unaffected（同一 FP 在多个集合）
    unaffected_set -= removed_set
    confirmed_set -= removed_set
    unaffected_set -= confirmed_set

    now_iso = datetime.now(timezone.utc).isoformat()
    impact_list: list[dict] = [
        e for e in list(srr.get("impact_analysis", []) or [])
        if e.get("fp_id") not in removed_set
    ]

    written_entries: list[dict] = []

    async def _write_entry(fp_id: str, is_affected_flag: bool) -> None:
        fp = await session.get(FunctionalPoint, fp_id)
        if fp is None:
            return  # FP 已删除，跳过（修复 C3：确认时忽略被删除 FP，避免写出空字段条目）
        entry = {
            "fp_id": fp.id,
            "fp_name": fp.name,
            "fp_type": fp.fp_type,
            "module_id": fp.module_id,
            "source_req_id": fp.requirement_id,
            "is_affected": is_affected_flag,
            "impact_description": body.edited_descriptions.get(
                fp_id,
                f"受需求[{req.title}]影响" if is_affected_flag else f"确认不受需求[{req.title}]影响",
            ),
            "confirmed_at": now_iso,
        }
        # v4.2.7 修复 P1：合并保留旧条目的扩展指导字段。
        # 旧条目由 FP 影响分析（impact-description/编辑）写入 description/test_focus/
        # affected_fps/affected_modules 等，而生成 prompt 依赖这些字段构造回归指导。
        # 原实现整条覆盖会把它们全部抹掉，导致确认后回归用例生成只能靠 FP 名猜测。
        # confirmed_at/is_affected 仍以本次确认为准（用户最终结论优先）。
        old_entry = next((e for e in impact_list if e.get("fp_id") == fp_id), None)
        if isinstance(old_entry, dict):
            for _key in ("affected_fps", "affected_modules", "description", "test_focus",
                         "generated_at", "is_auto_generated", "module_name", "depth",
                         "relation_type", "relation_cn", "source_fp_id", "source_fp_name",
                         "source_req_title", "impact_reason"):
                if old_entry.get(_key) is not None:
                    entry.setdefault(_key, old_entry.get(_key))
        # 覆盖旧条目
        for i, e in enumerate(impact_list):
            if e.get("fp_id") == fp_id:
                impact_list[i] = entry
                break
        else:
            impact_list.append(entry)
        written_entries.append(entry)

    for fp_id in confirmed_set:
        await _write_entry(fp_id, True)
    for fp_id in unaffected_set:
        await _write_entry(fp_id, False)

    srr["impact_analysis"] = impact_list
    req.srr_json = srr

    await session.commit()
    logger.info(
        "[ImpactAnalysis] 确认完成: req={} affected={} unaffected={} removed={}",
        req_id,
        len([e for e in written_entries if e.get("is_affected")]),
        len([e for e in written_entries if not e.get("is_affected")]),
        len(removed_set),
    )
    return ApiResponse(
        success=True,
        data={
            "confirmed_count": len([e for e in written_entries if e.get("is_affected")]),
            "unconfirmed_count": len([e for e in written_entries if not e.get("is_affected")]),
            "removed_count": len(removed_set),
            "impact_analysis": impact_list,
        },
    )


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
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """将多个功能点合并为一个（异步任务模式）。

    v2.16 异步化 + 冲突检测：
        1. 校验冲突（不能与其他分析任务同时进行）
        2. 创建 AnalysisTask（task_type=merge_fp）
        3. 后台协程执行 LLM 合并
        4. 立即返回 task_id
    """
    if len(body.fp_ids) < 2:
        raise HTTPException(status_code=400, detail="至少需要 2 个功能点才能合并")

    req = await _load_active_requirement(req_id, session)
    await _check_no_running_task(req_id, session=session)

    # 加载所有 FP
    fps_to_merge = []
    for fid in body.fp_ids:
        fp = await session.get(FunctionalPoint, fid)
        if not fp or fp.requirement_id != req_id:
            raise HTTPException(status_code=404, detail=f"功能点 {fid} 不存在或不属于该需求")
        fps_to_merge.append(fp)

    if len(fps_to_merge) < 2:
        raise HTTPException(status_code=400, detail="有效功能点不足，无法合并")

    # 创建任务
    task_id = gen_task_id()
    task = AnalysisTask(
        id=task_id,
        task_type="merge_fp",
        req_id=req_id,
        product_id=req.product_id,
        status="pending",
        phase="merge",
        progress=0,
        message=f"正在合并 {len(fps_to_merge)} 个功能点...",
        created_by="api",
    )
    session.add(task)
    await session.commit()

    fp_ids = [fp.id for fp in fps_to_merge]
    asyncio.create_task(
        _run_fp_merge_task(
            task_id=task_id, req_id=req_id, fp_ids=fp_ids,
            merged_name=body.merged_name, product_id=req.product_id,
        )
    )

    await log_operation(
        username=current_user.username, user_id=current_user.user_id,
        action="fp_merge", target_type="functional_point", target_id=req_id,
        detail={"fp_ids": fp_ids, "merged_name": body.merged_name},
    )

    return ApiResponse(
        success=True,
        message="功能点合并已启动",
        data={"task_id": task_id, "req_id": req_id, "fp_count": len(fp_ids)},
    )


@router.patch("/{req_id}/fp/{fp_id}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def fp_update(
    req_id: str, fp_id: str, body: FPUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """编辑功能点（名称/模块/类型/优先级）。

    v2.35: 归档编辑请使用 PUT 接口（含同步更新总 SRR/向量库/用例逻辑）。
    此 PATCH 接口仅做简单字段更新，不包含同步逻辑，归档状态不建议使用。
    """
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


# ============================================================================
# v2.3: 功能点复用机制
# 需求归档后 FP 进入 archived 状态，可被新需求复用，避免重复生成
# 复用时继承原始模块信息，无需用户手动选择
# ============================================================================

@router.get("/{req_id}/fp/reusable", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def fp_list_reusable(
    req_id: str,
    module_id: str | None = Query(default=None, description="按模块筛选已归档的可复用 FP"),
    q: str | None = Query(default=None, description="按名称模糊搜索"),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """列出可复用的已归档功能点。

    v2.3 新增。需求归档后，所属 FP 进入 archived 状态，可被新需求复用。
    返回同产品下所有 status='archived' 的 FP，支持按模块、名称筛选。

    Args:
        req_id: 当前需求 ID（用于确定产品范围）
        module_id: 可选，按模块筛选
        q: 可选，按名称模糊搜索
        session: 数据库会话

    Returns:
        ApiResponse: data.items 含可复用 FP 列表，含源需求信息
    """
    req = await session.get(Requirement, req_id)
    if not req:
        raise HTTPException(status_code=404, detail="需求不存在")

    # 查询同产品下所有已归档 FP（排除当前需求自身的 FP）
    stmt = (
        select(FunctionalPoint, Requirement.title)
        .join(Requirement, FunctionalPoint.requirement_id == Requirement.id)
        .where(
            FunctionalPoint.status == "archived",
            Requirement.product_id == req.product_id,
            FunctionalPoint.requirement_id != req_id,
        )
        .order_by(FunctionalPoint.updated_at.desc())
    )

    if q:
        stmt = stmt.where(FunctionalPoint.name.ilike(f"%{q}%"))

    stmt = stmt.limit(200)
    result = await session.execute(stmt)
    rows = result.all()

    # v4.2.2 修复：modules 为 JSON 列，PostgreSQL json 类型不支持 @>（contains）操作符，
    # 直接 .contains 会抛 "operator does not exist" → HTTP 500「内部错误」。改为内存筛选，语义等价。
    if module_id:
        rows = [r for r in rows if r[0].module_id == module_id or module_id in (r[0].modules or [])]

    # 查询模块名称映射
    all_module_ids: set[str] = set()
    for fp, _ in rows:
        if fp.module_id:
            all_module_ids.add(fp.module_id)
        for m in (fp.modules or []):
            all_module_ids.add(m)

    module_names: dict[str, str] = {}
    if all_module_ids:
        from app.db.models import Module as ModuleModel
        mod_result = await session.execute(
            select(ModuleModel.id, ModuleModel.name).where(ModuleModel.id.in_(all_module_ids))
        )
        module_names = {row[0]: row[1] for row in mod_result.fetchall()}

    items = []
    for fp, src_req_title in rows:
        items.append({
            "id": fp.id,
            "name": fp.name,
            "fp_type": fp.fp_type,
            "priority": fp.priority,
            "module_id": fp.module_id,
            "modules": fp.modules or [],
            "module_names": [module_names.get(m, m) for m in (fp.modules or ([fp.module_id] if fp.module_id else []))],
            "source_req_id": fp.requirement_id,
            "source_req_title": src_req_title,
            "test_hints": fp.test_hints or [],
            "archived_at": fp.updated_at.isoformat() if fp.updated_at else "",
        })

    return ApiResponse(
        success=True,
        message=f"找到 {len(items)} 个可复用功能点",
        data={"items": items, "total": len(items)},
    )


class FPReuseRequest(BaseModel):
    """功能点复用请求。"""
    source_fp_id: str  # 源 FP ID（已归档）
    name: str | None = None  # 复用后的新名称（可选，默认继承源 FP 名称）


@router.post("/{req_id}/fp/reuse", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_reuse(
    req_id: str,
    body: FPReuseRequest,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """复用已归档的功能点。

    v2.3 新增。从同产品下已归档的需求中复用功能点：
    - 复制源 FP 的 name / fp_type / srr_json / atomicity_check / test_hints
    - 继承源 FP 的 module_id 和 modules（无需用户手动选择）
    - 新 FP 状态设为 pending_review（在新需求上下文下需重新审核）
    - 关联到当前需求（requirement_id = req_id）

    Args:
        req_id: 目标需求 ID
        body: 复用请求，含源 FP ID
        session: 数据库会话

    Returns:
        ApiResponse: data 含新 FP 信息
    """
    await _load_active_requirement(req_id, session)

    # 查询源 FP
    source_fp = await session.get(FunctionalPoint, body.source_fp_id)
    if not source_fp:
        raise HTTPException(status_code=404, detail="源功能点不存在")
    if source_fp.status != "archived":
        raise HTTPException(
            status_code=409,
            detail=f"源功能点「{source_fp.name}」未归档（当前状态: {source_fp.status}），不可复用",
        )

    # 同产品校验
    source_req = await session.get(Requirement, source_fp.requirement_id)
    target_req = await session.get(Requirement, req_id)
    if not source_req or not target_req:
        raise HTTPException(status_code=404, detail="源需求或目标需求不存在")
    if source_req.product_id != target_req.product_id:
        raise HTTPException(status_code=400, detail="源功能点与目标需求必须属于同一产品")

    # 查重 — 同需求下不能复用同名 FP
    new_name = (body.name or source_fp.name).strip()[:255]
    if not new_name:
        raise HTTPException(status_code=400, detail="功能点名称不能为空")

    dup = await session.execute(
        select(FunctionalPoint.id).where(
            FunctionalPoint.requirement_id == req_id,
            FunctionalPoint.name == new_name,
        )
    )
    if dup.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"当前需求已存在同名功能点「{new_name}」，请修改名称后重试",
        )

    # 创建新 FP — 继承源 FP 的模块信息
    new_fp = FunctionalPoint(
        id=gen_requirement_id(),
        requirement_id=req_id,
        module_id=source_fp.module_id,  # 继承源 FP 模块
        modules=list(source_fp.modules or []),  # 继承源 FP 多模块关联
        name=new_name,
        fp_type=source_fp.fp_type,
        priority=source_fp.priority,
        srr_json=dict(source_fp.srr_json) if isinstance(source_fp.srr_json, dict) else {},
        atomicity_check=dict(source_fp.atomicity_check) if isinstance(source_fp.atomicity_check, dict) else {},
        test_hints=list(source_fp.test_hints or []),
        status="pending_review",  # 复用后需在新需求上下文下重新审核
    )
    session.add(new_fp)
    await session.commit()
    await session.refresh(new_fp)

    logger.info(
        "[FP] 复用成功: source_fp_id={} → new_fp_id={}, name={}, req_id={}, module_id={}",
        source_fp.id, new_fp.id, new_fp.name, req_id, new_fp.module_id,
    )

    await log_operation(
        username=current_user.username, user_id=current_user.user_id,
        action="fp_reuse", target_type="functional_point", target_id=new_fp.id,
        detail={"source_fp_id": source_fp.id, "source_req_id": source_fp.requirement_id},
    )

    return ApiResponse(
        success=True,
        message=f"功能点「{new_fp.name}」已复用，模块已继承",
        data={
            "id": new_fp.id,
            "name": new_fp.name,
            "fp_type": new_fp.fp_type,
            "module_id": new_fp.module_id,
            "modules": new_fp.modules or [],
            "status": new_fp.status,
            "source_fp_id": source_fp.id,
            "source_req_id": source_fp.requirement_id,
        },
    )


@router.delete("/{req_id}/fp/{fp_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_delete(
    req_id: str,
    fp_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ApiResponse:
    """删除功能点。归档后不可用。

    修复 C3（跨需求引用泄漏）：
    删除 FP 时，除了清除本需求的 SRR.impact_analysis 引用，
    还要级联清理「其他所有需求」SRR 中 impact_analysis 对该 fp_id 的引用，
    否则 confirm_requirement 的守卫会让别的需求卡在"有未确认条目"。
    同时删除 Traceability 表中 src_fp_id / dest_fp_id 关联。
    """
    req = await _load_active_requirement(req_id, session)
    fp = await session.get(FunctionalPoint, fp_id)
    if not fp or fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="功能点不存在")

    # ---- C3 修复：级联清理跨需求 SRR.impact_analysis 中的 fp_id 引用 ----
    # v4.2.9 修复 P4：srr_json 列类型为 json（非 jsonb），原 SQL 直接用 jsonb_set/
    # jsonb_array_elements 操作 json 列会导致 "function jsonb_set(json) does not exist"
    # 运行时报错并被旧 catch 吞掉，跨需求清理实际从未生效。现通过 ::jsonb 显式转型。
    try:
        cleanup_stmt = text(
            """
            UPDATE requirements
               SET srr_json = jsonb_set(
                   COALESCE(srr_json::jsonb, '{}'::jsonb),
                   '{impact_analysis}',
                   COALESCE(
                       (SELECT jsonb_agg(elem)
                          FROM jsonb_array_elements(
                                   COALESCE((srr_json->'impact_analysis')::jsonb, '[]'::jsonb)
                               ) AS elem
                         WHERE elem->>'fp_id' <> :fp_id),
                       '[]'::jsonb
                   )
               )::json
             WHERE COALESCE((srr_json->'impact_analysis')::jsonb, '[]'::jsonb) @>
                   jsonb_build_array(jsonb_build_object('fp_id', :fp_id::text))
            """
        )
        res = await session.execute(cleanup_stmt, {"fp_id": fp_id})
        logger.info(
            "[FP-Delete] 级联清理 SRR.impact_analysis 引用: fp={} 行={}",
            fp_id, res.rowcount,
        )
    except Exception as e:  # pragma: no cover - 保护型清理，失败不应阻断删除
        logger.warning(
            "[FP-Delete] 级联清理 SRR.impact_analysis 引用失败: fp={} error={}",
            fp_id, str(e)[:200],
        )

    # ---- 清理可追溯性矩阵中的 FP 关联 ----
    # v4.2.9 修复 P3：使用真实字段 Traceability.requirement_field（review_agent 以
    # "fp:{fp_id}" 写入），原实现引用不存在的 src_fp_id/dest_fp_id 属性属死代码，
    # 每次执行必抛 AttributeError 被吞，删除 FP 后追溯记录永远残留。
    try:
        from app.db.models import Traceability  # 局部导入，避免循环依赖
        from sqlalchemy import or_
        tr_cleanup = delete(Traceability).where(
            Traceability.requirement_field == f"fp:{fp_id}"
        )
        await session.execute(tr_cleanup)
        # 兼容历史写入格式（related_fp 曾用名称而非 ID 时以 fp:{fp_id} 精确匹配可能漏掉）
        tr_cleanup2 = delete(Traceability).where(
            Traceability.requirement_field.like(f"fp:{fp_id}%")
        )
        await session.execute(tr_cleanup2)
    except Exception as e:
        # v2.8.60: 静默吞异常改为日志记录（Traceability 表可能不存在或已删除，不阻断主流程但需可追溯）
        logger.warning("清理 FP {} 的 Traceability 关联失败（非阻断）: {}", fp_id, str(e)[:200])

    # 删除关联依赖
    dep_result = await session.execute(
        select(FPDependency).where(
            (FPDependency.source_fp_id == fp_id) | (FPDependency.target_fp_id == fp_id)
        )
    )
    for dep in dep_result.scalars().all():
        await session.delete(dep)
    await session.delete(fp)

    # ---- 同步删除本需求 SRR.impact_analysis 中该 fp 的条目（再兜底一次）----
    if isinstance(req.srr_json, dict) and "impact_analysis" in req.srr_json:
        srr = dict(req.srr_json)
        before = len(srr.get("impact_analysis") or [])
        srr["impact_analysis"] = [
            e for e in (srr["impact_analysis"] or []) if e.get("fp_id") != fp_id
        ]
        if before != len(srr["impact_analysis"]):
            req.srr_json = srr

    await session.commit()
    # v2.22: 删除 FP 后更新总 SRR 的 fp_index 和 fp_summary
    try:
        await _update_fp_index_in_srr(session, req_id)
        await session.commit()
    except Exception as e:
        logger.warning("[FP-Delete] 更新 fp_index/fp_summary 失败（不阻断）: {}", str(e)[:200])
    # v2.45 修复：清理 Milvus 中的 FP 向量（边界场景：归档→恢复→删除FP 时避免孤儿向量）
    try:
        from app.db.milvus_store import vector_store
        await asyncio.to_thread(vector_store.delete_requirement_from_kb, f"FP-{fp_id}")
    except Exception as e:
        logger.warning("[FP-Delete] 清理 FP 向量失败（不阻断）: fp_id={}, error={}", fp_id, str(e)[:150])
    await log_operation(
        username=current_user.username, user_id=current_user.user_id,
        action="fp_delete", target_type="functional_point", target_id=fp_id,
        detail={"req_id": req_id},
    )
    return ApiResponse(success=True, message="功能点已删除")


# ============================================================================
# v1.5.1: FP 审核
# ============================================================================


@router.patch("/{req_id}/fp/{fp_id}/approve", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def fp_approve(
    req_id: str, fp_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """审核通过单个功能点。归档后不可用。

    v2.3: 强制要求功能点必须分配模块才能审核通过。
    """
    await _load_active_requirement(req_id, session)
    fp = await session.get(FunctionalPoint, fp_id)
    if not fp or fp.requirement_id != req_id:
        raise HTTPException(status_code=404, detail="功能点不存在")
    if fp.status == "approved":
        return ApiResponse(success=True, message="该功能点已审核通过")
    # v2.3: 模块校验 — 未分配模块的功能点不允许审核通过
    if not fp.module_id and not (fp.modules or []):
        raise HTTPException(
            status_code=409,
            detail=f"功能点「{fp.name}」未分配模块，请先编辑该功能点并选择对应模块后再审核通过",
        )
    fp.status = "approved"
    await session.commit()
    logger.info("[FP] 审核通过: fp_id={}, name={}", fp_id, fp.name)
    return ApiResponse(success=True, message=f"功能点「{fp.name}」已审核通过")


# ============================================================================
# 17. 需求 SRR → XMind 导出
# ============================================================================

@router.get("/{req_id}/export/xmind", dependencies=[Depends(get_current_user)])
async def export_requirement_xmind(
    req_id: str,
    format: str = "xmind",
    session: AsyncSession = Depends(get_session),
) -> Response:
    """导出需求 SRR 为 XMind 思维导图（.xmind ZIP 文件）。

    支持两种 XMind 格式（与测试用例导出保持一致）：
    - ``xmind``（默认）：XMind 2020+ / Zen / 2022+ JSON 格式（content.json）
    - ``xmind8``：XMind 8 兼容的 XMAP 2.0 XML 格式（content.xml）

    树结构：
        需求标题（根）
          ├── 总体SRR（一级）
          │   ├── 基本信息（优先级/类型/摘要）
          │   ├── 主流程（Happy Path 步骤）
          │   ├── 异常分支
          │   ├── 业务规则 / 边界条件 / 数据约束 / 验收标准 / 前置条件
          │   └── 附加详情
          ├── [fp_type] 功能点1（一级）
          │   ├── 摘要 / 主流程 / 异常分支 / 业务规则 / ...
          │   └── 测试提示
          └── [fp_type] 功能点2（一级）

    Args:
        req_id: 需求 ID
        format: 导出格式，``xmind``（JSON）或 ``xmind8``（XML）
        session: 数据库会话

    Returns:
        Response: .xmind 文件（application/zip）
    """
    fmt = (format or "xmind").strip().lower()
    if fmt not in ("xmind", "xmind8"):
        raise HTTPException(status_code=400, detail=f"不支持的导出格式: {format}，支持: xmind / xmind8")

    stmt = select(Requirement).where(Requirement.id == req_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()

    if record is None:
        raise HTTPException(status_code=404, detail=f"需求 {req_id} 不存在")

    srr = record.srr_json or {}
    if not isinstance(srr, dict):
        srr = {}

    # 查询功能点列表，合并到 srr 中供导出器使用（srr_json 本身不含 FP 详细数据）
    from sqlalchemy import select as sa_select
    fp_result = await session.execute(
        sa_select(
            FunctionalPoint.id, FunctionalPoint.name, FunctionalPoint.fp_type,
            FunctionalPoint.priority, FunctionalPoint.srr_json, FunctionalPoint.test_hints,
            FunctionalPoint.status,
        )
        .where(FunctionalPoint.requirement_id == req_id)
        .order_by(FunctionalPoint.created_at)
    )
    fp_rows = fp_result.all()
    if fp_rows:
        srr = dict(srr)  # 浅拷贝避免修改 ORM 持有对象
        srr["functional_points"] = [
            {
                "fp_id": row[0],
                "name": row[1],
                "fp_type": row[2],
                "priority": row[3],
                "srr_json": row[4] or {},
                "test_hints": row[5] or [],
                "status": row[6],
            }
            for row in fp_rows
        ]

    # v4.2.5：注入"受影响功能点"的最新内容，供 XMind 导出时按其他功能点的内容展示。
    # 受影响功能点是外部功能点（不在本需求 functional_points 中），需从 DB 查询其 SRR。
    # 查询"最新版本"：以受影响条目记录的 fp_id 精确取该 FP；同名多版本时由 fp_id 天然区分。
    impact_raw = srr.get("impact_analysis") or []
    impact_ids = [e.get("fp_id") for e in impact_raw if isinstance(e, dict) and e.get("fp_id")]
    if impact_ids:
        detail_map: dict[str, dict] = {}
        ifp_res = await session.execute(
            sa_select(FunctionalPoint.id, FunctionalPoint.name, FunctionalPoint.fp_type,
                      FunctionalPoint.srr_json)
            .where(FunctionalPoint.id.in_(impact_ids))
        )
        for fid, fname, ftype, fsrr in ifp_res.all():
            detail_map[fid] = {
                "name": fname,
                "fp_type": ftype or "",
                "srr_json": fsrr or {},
            }
        if detail_map:
            srr = dict(srr)
            srr["impact_fps_detail"] = detail_map

    # 安全文件名（去除路径分隔符等特殊字符）
    raw_title = (srr.get("requirement") or {}).get("title") if isinstance(srr.get("requirement"), dict) else record.title
    raw_title = str(raw_title or req_id)
    safe_title = "".join(c for c in raw_title if c not in '\\/:*?"<>|\r\n\t').strip()[:60] or req_id

    try:
        if fmt == "xmind8":
            from app.exporters.req_xmind import export_requirement_to_xmind
            content = export_requirement_to_xmind(srr, req_title=record.title or "", req_id=req_id)
        else:
            from app.exporters.req_xmind import export_requirement_to_xmind_json
            content = export_requirement_to_xmind_json(srr, req_title=record.title or "", req_id=req_id)
    except Exception as e:
        logger.exception("[API/requirements] XMind 导出失败: req_id={}, format={}", req_id, fmt)
        raise HTTPException(status_code=500, detail=f"XMind 导出失败: {str(e)[:200]}")

    # 文件名后缀区分格式（与测试用例导出一致）
    suffix = "-xmind8" if fmt == "xmind8" else ""
    filename = f"{safe_title}_{req_id}{suffix}.xmind"
    # RFC 5987: 中文文件名需用 filename* 编码（Starlette header 用 latin-1，不支持中文）
    from urllib.parse import quote
    filename_ascii = f"requirement_{req_id}{suffix}.xmind"  # ASCII fallback
    filename_utf8 = quote(filename, safe="")  # URL 编码后的中文文件名
    logger.info("[API/requirements] XMind 导出: req_id={}, format={}, size={} bytes", req_id, fmt, len(content))

    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename_ascii}\"; filename*=UTF-8''{filename_utf8}",
            "Content-Length": str(len(content)),
        },
    )
