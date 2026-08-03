"""用例导出路由。

端点清单（实现计划 T5.2）：
    POST /api/export  导出测试用例（JSON / Markdown / Gherkin / pytest / XMind）

设计依据：
- docs/04-output-format.md §3（Gherkin 转换）
- docs/04-output-format.md §4.1（pytest 适配器）
- docs/04-output-format.md §9（Markdown 输出格式）
- 实现计划 §T6（导出器）

实现状态：
- JSON / Markdown / Gherkin / pytest / XMind(ZEN+8) 五种格式均已实现
- 导出器实现位于 app/exporters/，由工厂函数 export_cases 统一调度
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_write, get_current_user
from app.db.database import get_session
from app.db.models import TestCase, Requirement, FunctionalPoint, Project
from app.exporters import export_cases, SUPPORTED_FORMATS
from app.models.api_schemas import ExportRequest
from app.utils.logger import logger


router = APIRouter()


def _extract_fp_id(case_json: dict) -> str:
    """从 case_json 中提取功能点 ID（与 testcases API 的 _extract_fp 一致）。

    优先取 related_fp，其次 functional_point。
    """
    if not case_json:
        return ""
    return case_json.get("related_fp", "") or case_json.get("functional_point", "") or ""


async def _resolve_case_context(rows: list[TestCase], session: AsyncSession) -> list[dict]:
    """构建导出用 case dict 列表，并注入 req_title / project_name / functional_point（名称）。

    XMind 导出依赖 req_title 显示需求名称、functional_point 显示功能点名称，
    但 case_json.related_fp 存的是 fp_id（部分 fp_id 格式类似 req_id），
    需要批量查询 Requirement / FunctionalPoint / Project 表做名称映射，与列表 API 保持一致。
    """
    # 收集需要解析的 ID
    req_ids: set[str] = set()
    fp_ids: set[str] = set()
    for r in rows:
        if r.req_id:
            req_ids.add(r.req_id)
        fp_id = _extract_fp_id(r.case_json or {})
        if fp_id:
            fp_ids.add(fp_id)

    # 批量查询需求标题 + project_id
    req_title_map: dict[str, str] = {}
    req_project_map: dict[str, str] = {}  # req_id → project_id
    if req_ids:
        req_rows = await session.execute(
            select(Requirement.id, Requirement.title, Requirement.project_id).where(Requirement.id.in_(req_ids))
        )
        for rid, rtitle, pid in req_rows.all():
            req_title_map[rid] = rtitle
            if pid:
                req_project_map[rid] = pid

    # 批量查询项目名称
    project_name_map: dict[str, str] = {}
    project_ids = set(req_project_map.values())
    if project_ids:
        proj_rows = await session.execute(
            select(Project.id, Project.name).where(Project.id.in_(project_ids))
        )
        project_name_map = {pid: pname for pid, pname in proj_rows.all()}

    # 批量查询功能点名称
    fp_name_map: dict[str, str] = {}
    if fp_ids:
        fp_rows = await session.execute(
            select(FunctionalPoint.id, FunctionalPoint.name).where(FunctionalPoint.id.in_(fp_ids))
        )
        fp_name_map = {fid: fname for fid, fname in fp_rows.all()}

    # 组装 case dict，注入解析后的字段
    cases: list[dict] = []
    for r in rows:
        cj = r.case_json or {}
        fp_id = _extract_fp_id(cj)
        fp_resolved = fp_name_map.get(fp_id, fp_id) if fp_id else ""
        case = dict(cj)  # 浅拷贝，避免污染原始 case_json
        # 注入需求标题（XMind 导出器优先读 req_title）
        case["req_id"] = r.req_id
        case["req_title"] = req_title_map.get(r.req_id, r.req_id) if r.req_id else ""
        # 注入项目名称（XMind 导出器在需求节点前加项目名前缀）
        case["project_name"] = project_name_map.get(req_project_map.get(r.req_id, ""), "")
        # 注入解析后的功能点名称（覆盖原 fp_id，XMind 导出器优先读 functional_point）
        case["functional_point"] = fp_resolved
        cases.append(case)
    return cases


@router.post("", dependencies=[Depends(get_current_user)])
async def export_testcases(
    req: ExportRequest,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """导出测试用例为指定格式。

    Args:
        req: 导出请求，含 format（格式）和 case_ids（指定用例，留空导出全部）
        session: 数据库会话

    Returns:
        Response: 文件流响应，含 Content-Disposition 附件头

    Raises:
        HTTPException 400: 不支持的格式
        HTTPException 404: 指定的用例不存在

    Note:
        - JSON 格式：返回 application/json，直接下载
        - Markdown 格式：返回 text/markdown，用于 Review / 文档归档
        - Gherkin 格式：返回 text/plain，用于 Cucumber 执行
        - pytest 格式：返回 text/x-python，用于自动化测试执行
        - XMind 格式：返回 application/zip，思维导图（需求→功能点→用例→步骤）
    """
    # 查询用例（v2.2: 强制 case_ids 非空，防止全量导出所有产品数据）
    if not req.case_ids:
        raise HTTPException(
            status_code=400,
            detail="必须提供至少一个 case_id 进行导出（防止批量数据泄漏）",
        )
    stmt = select(TestCase).where(TestCase.id.in_(req.case_ids))
    result = await session.execute(stmt)
    rows = result.scalars().all()

    if not rows:
        raise HTTPException(status_code=404, detail="未找到可导出的用例")

    # 解析 req_title 和 functional_point 名称（与列表 API 一致），注入 case dict
    cases = await _resolve_case_context(rows, session)
    logger.info(
        "[API/export] 导出: format={}, cases={}, include_details={}",
        req.format,
        len(cases),
        req.include_details,
    )

    # 格式校验（理论上 Pydantic Literal 已保证，此处双重防御）
    if req.format not in SUPPORTED_FORMATS:
        raise HTTPException(status_code=400, detail=f"不支持的导出格式: {req.format}")

    try:
        content, media_type, filename = export_cases(
            cases, req.format, project_name=req.project_name,
            include_details=req.include_details,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # xmind 返回 bytes（ZIP 二进制），其他格式返回 str
    if isinstance(content, bytes):
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            },
        )

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )
