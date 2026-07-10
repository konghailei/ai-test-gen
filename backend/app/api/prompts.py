"""提示词管理 API — 管理员可查看、编辑、重置各 Agent 的提示词。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import require_admin
from app.core import prompt_manager
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
async def update_prompt(module: str, name: str, req: PromptUpdateRequest):
    """更新提示词（保存用户自定义值）。"""
    try:
        prompt_manager.update(module, name, req.value)
        logger.info("[API/prompts] 提示词已更新: {}.{}", module, name)
        return ApiResponse(success=True, message="提示词已保存", data=None)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {str(e)[:200]}")


@router.post("/{module}/{name}/reset", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def reset_prompt(module: str, name: str):
    """恢复提示词为默认值。"""
    try:
        prompt_manager.reset(module, name)
        logger.info("[API/prompts] 提示词已重置: {}.{}", module, name)
        return ApiResponse(success=True, message="已恢复默认值", data=None)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重置失败: {str(e)[:200]}")
