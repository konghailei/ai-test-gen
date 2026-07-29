"""提示词管理 API — 管理员可查看、编辑、重置各 Agent 的提示词。

v2.3 新增版本管理：
- 所有修改自动记录历史版本
- 支持查看历史版本列表
- 支持从任意历史版本恢复
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.auth import require_admin, get_current_user, CurrentUser
from app.core import prompt_manager
from app.core.operation_logger import log_operation
from app.models.api_schemas import ApiResponse
from app.utils.logger import logger

router = APIRouter(prefix="/admin/prompts", tags=["admin/prompts"])


class PromptUpdateRequest(BaseModel):
    value: str


@router.get("", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def list_prompts():
    """列出所有可编辑的提示词（含当前值和默认值）。"""
    return ApiResponse(
        success=True,
        message="ok",
        data={"prompts": prompt_manager.list_prompts()},
    )


@router.put("/{module}/{name}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def update_prompt(
    module: str,
    name: str,
    req: PromptUpdateRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """更新提示词（保存用户自定义值）。

    v2.3: 自动记录修改历史，含修改人、修改时间、修改前后值对比。
    """
    try:
        prompt_manager.update(module, name, req.value, changed_by=current_user.username)
        logger.info("[API/prompts] 提示词已更新: {}.{} (by={})", module, name, current_user.username)
        await log_operation(
            username=current_user.username, user_id=current_user.user_id,
            action="update_prompt", target_type="prompt", target_id=f"{module}.{name}",
        )
        return ApiResponse(success=True, message="提示词已保存", data=None)
    except Exception as e:
        await log_operation(
            username=current_user.username, user_id=current_user.user_id,
            action="update_prompt", target_type="prompt", target_id=f"{module}.{name}",
            success=False, error_message=str(e)[:200],
        )
        raise HTTPException(status_code=500, detail=f"保存失败: {str(e)[:200]}")


@router.post("/{module}/{name}/reset", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def reset_prompt(
    module: str,
    name: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """恢复提示词为默认值。

    v2.3: 自动记录恢复操作到历史表。
    """
    try:
        prompt_manager.reset(module, name, changed_by=current_user.username)
        logger.info("[API/prompts] 提示词已重置: {}.{} (by={})", module, name, current_user.username)
        await log_operation(
            username=current_user.username, user_id=current_user.user_id,
            action="reset_prompt", target_type="prompt", target_id=f"{module}.{name}",
        )
        return ApiResponse(success=True, message="已恢复默认值", data=None)
    except Exception as e:
        await log_operation(
            username=current_user.username, user_id=current_user.user_id,
            action="reset_prompt", target_type="prompt", target_id=f"{module}.{name}",
            success=False, error_message=str(e)[:200],
        )
        raise HTTPException(status_code=500, detail=f"重置失败: {str(e)[:200]}")


# ============================================================================
# v2.3: 提示词版本管理
# ============================================================================

@router.get("/{module}/{name}/history", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def list_prompt_history(
    module: str,
    name: str,
    limit: int = Query(default=50, ge=1, le=500, description="返回条数上限"),
):
    """获取提示词修改历史列表。

    v2.3 新增。返回最近 N 次修改记录，按时间倒序。
    每条记录包含：修改时间、修改人、修改前后值、修改摘要。
    """
    try:
        history = prompt_manager.list_history(module, name, limit=limit)
        return ApiResponse(
            success=True,
            message=f"找到 {len(history)} 条历史记录",
            data={"items": history, "total": len(history)},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取历史记录失败: {str(e)[:200]}")


@router.post("/{module}/{name}/restore/{history_id}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def restore_prompt_from_history(
    module: str,
    name: str,
    history_id: int,
    current_user: CurrentUser = Depends(get_current_user),
):
    """从历史版本恢复提示词。

    v2.3 新增。将指定历史版本的值恢复为当前提示词值，
    并自动记录本次恢复操作到历史表，确保操作可追溯。

    Args:
        module: 模块名
        name: 提示词名
        history_id: 历史记录 ID
        current_user: 当前用户

    Returns:
        ApiResponse: 含恢复后的提示词信息
    """
    try:
        result = prompt_manager.restore_from_history(history_id, changed_by=current_user.username)
        logger.info(
            "[API/prompts] 提示词已从历史恢复: {}.{} (history_id={}, by={})",
            module, name, history_id, current_user.username,
        )
        await log_operation(
            username=current_user.username, user_id=current_user.user_id,
            action="restore_prompt", target_type="prompt", target_id=f"{module}.{name}",
            detail={"history_id": history_id},
        )
        return ApiResponse(
            success=True,
            message=f"已从历史版本 #{history_id} 恢复",
            data=result,
        )
    except ValueError as e:
        await log_operation(
            username=current_user.username, user_id=current_user.user_id,
            action="restore_prompt", target_type="prompt", target_id=f"{module}.{name}",
            detail={"history_id": history_id},
            success=False, error_message=str(e)[:200],
        )
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        await log_operation(
            username=current_user.username, user_id=current_user.user_id,
            action="restore_prompt", target_type="prompt", target_id=f"{module}.{name}",
            detail={"history_id": history_id},
            success=False, error_message=str(e)[:200],
        )
        raise HTTPException(status_code=500, detail=f"恢复失败: {str(e)[:200]}")
