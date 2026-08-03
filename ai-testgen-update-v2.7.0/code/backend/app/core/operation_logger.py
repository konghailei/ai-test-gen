"""用户操作日志记录器。

提供 log_operation 异步函数，在关键业务端点（登录、上传、删除、归档、
FP 合并/复用/删除、提示词修改等）调用，将用户操作写入 operation_logs 表。

设计原则：
- 日志记录不能阻断主流程（try/except 包裹，失败只 logger.warning）
- 使用独立 session 写入，不依赖调用方 session
- 支持同步降级：若数据库不可用，仅记录到 loguru 日志，不影响业务
"""

from __future__ import annotations

from app.utils.logger import logger


async def log_operation(
    *,
    username: str = "",
    user_id: int | None = None,
    action: str,
    target_type: str = "",
    target_id: str = "",
    detail: dict | None = None,
    ip_address: str = "",
    success: bool = True,
    error_message: str = "",
) -> None:
    """记录一条用户操作日志。

    所有参数均为关键字参数，便于在端点中调用。日志写入失败不阻断主流程。

    Args:
        username: 操作者用户名（来自 CurrentUser.username）
        user_id: 操作者用户 ID（来自 CurrentUser.user_id）
        action: 操作类型，如 "login"/"upload_requirement"/"delete_requirement" 等
        target_type: 操作目标类型，如 "requirement"/"functional_point"/"prompt" 等
        target_id: 操作目标 ID（如需求 ID、FP ID）
        detail: 操作详情（JSON 可序列化字典，如 {"title": "...", "status": "..."}）
        ip_address: 请求来源 IP
        success: 操作是否成功（默认 True）
        error_message: 操作失败时的错误信息（默认空）

    Usage:
        await log_operation(
            username=user.username,
            user_id=user.user_id,
            action="delete_requirement",
            target_type="requirement",
            target_id=req_id,
            ip_address=request.client.host if request.client else "",
        )
    """
    try:
        from app.db.database import async_session
        from app.db.models import OperationLog

        log_entry = OperationLog(
            user_id=user_id,
            username=username or None,
            action=action,
            target_type=target_type or None,
            target_id=target_id or None,
            detail=detail if isinstance(detail, dict) else None,
            ip_address=ip_address or None,
            success=success,
            error_message=error_message or None,
        )
        async with async_session() as session:
            session.add(log_entry)
            await session.commit()
    except Exception as e:
        # 日志记录失败不阻断主流程，仅记录警告
        logger.warning(
            "[OperationLog] 操作日志记录失败（不阻断）: action={}, error={}",
            action,
            str(e)[:200],
        )
