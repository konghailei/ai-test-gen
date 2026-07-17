"""用例导出路由。

端点清单（实现计划 T5.2）：
    POST /api/export  导出测试用例（JSON / Markdown / Gherkin / pytest）

设计依据：
- docs/04-output-format.md §3（Gherkin 转换）
- docs/04-output-format.md §4.1（pytest 适配器）
- docs/04-output-format.md §9（Markdown 输出格式）
- 实现计划 §T6（导出器）

实现状态：
- JSON / Markdown / Gherkin / pytest 四种格式均已实现
- 导出器实现位于 app/exporters/，由工厂函数 export_cases 统一调度
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_write, get_current_user
from app.db.database import get_session
from app.db.models import TestCase
from app.exporters import export_cases, SUPPORTED_FORMATS
from app.models.api_schemas import ExportRequest
from app.utils.logger import logger


router = APIRouter()


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

    cases = [r.case_json or {} for r in rows]
    logger.info(
        "[API/export] 导出: format={}, cases={}",
        req.format,
        len(cases),
    )

    # 格式校验（理论上 Pydantic Literal 已保证，此处双重防御）
    if req.format not in SUPPORTED_FORMATS:
        raise HTTPException(status_code=400, detail=f"不支持的导出格式: {req.format}")

    try:
        content, media_type, filename = export_cases(cases, req.format)
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
