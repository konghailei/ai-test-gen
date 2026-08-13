"""角色管理 API（v2.12 新增）。

端点清单：
    GET    /api/admin/roles           角色列表
    POST   /api/admin/roles           创建角色
    PUT    /api/admin/roles/{id}      更新角色（含菜单权限）
    DELETE /api/admin/roles/{id}      删除角色
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_admin
from app.db.database import get_session
from app.db.models import Role, User, DEFAULT_MENUS
from app.models.api_schemas import ApiResponse
from app.utils.logger import logger

router = APIRouter(prefix="", tags=["角色管理"])


# ============================================================================
# 请求/响应模型
# ============================================================================

class RoleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=32, description="角色标识")
    label: str = Field(default="", max_length=64, description="显示名称")
    menus: list[str] = Field(default_factory=list, description="菜单权限列表")


class RoleUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=32, description="角色标识")
    label: str | None = Field(default=None, max_length=64, description="显示名称")
    menus: list[str] | None = Field(default=None, description="菜单权限列表")


class RoleResponse(BaseModel):
    id: int
    name: str
    label: str
    menus: list[str]


# ============================================================================
# 路由
# ============================================================================

@router.get("/roles", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def list_roles(session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """获取所有角色列表。"""
    result = await session.execute(select(Role).order_by(Role.id))
    roles = result.scalars().all()
    return ApiResponse(
        success=True, message="ok",
        data=[RoleResponse(id=r.id, name=r.name, label=r.label, menus=r.menus or []).model_dump() for r in roles],
    )


@router.post("/roles", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def create_role(body: RoleCreate, session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """创建新角色。"""
    existing = await session.execute(select(Role).where(Role.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"角色 {body.name} 已存在")

    # 过滤无效菜单名
    valid_menus = [m for m in body.menus if m in DEFAULT_MENUS]
    role = Role(name=body.name, label=body.label or body.name, menus=valid_menus)
    session.add(role)
    await session.commit()
    await session.refresh(role)

    logger.info("[roles] 角色已创建: name={}, menus={}", role.name, valid_menus)
    return ApiResponse(
        success=True, message="创建成功",
        data=RoleResponse(id=role.id, name=role.name, label=role.label, menus=role.menus or []).model_dump(),
    )


@router.put("/roles/{role_id}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def update_role(role_id: int, body: RoleUpdate, session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """更新角色信息及菜单权限。"""
    result = await session.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")

    if body.name is not None and body.name != role.name:
        dup = await session.execute(select(Role).where(Role.name == body.name))
        if dup.scalar_one_or_none():
            raise HTTPException(status_code=400, detail=f"角色名 {body.name} 已存在")
        role.name = body.name
    if body.label is not None:
        role.label = body.label
    if body.menus is not None:
        role.menus = [m for m in body.menus if m in DEFAULT_MENUS]

    await session.commit()
    await session.refresh(role)

    logger.info("[roles] 角色已更新: id={}, menus={}", role.id, role.menus)
    return ApiResponse(
        success=True, message="更新成功",
        data=RoleResponse(id=role.id, name=role.name, label=role.label, menus=role.menus or []).model_dump(),
    )


@router.delete("/roles/{role_id}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def delete_role(role_id: int, session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """删除角色（不允许删除有关联用户的角色）。"""
    result = await session.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")

    if role.name == "admin":
        raise HTTPException(status_code=400, detail="系统管理员角色不可删除")

    user_count = await session.execute(select(User).where(User.role_id == role_id))
    if user_count.scalars().first() is not None:
        raise HTTPException(status_code=400, detail="该角色下有关联用户，请先解除关联再删除")

    await session.delete(role)
    await session.commit()

    logger.info("[roles] 角色已删除: id={}, name={}", role_id, role.name)
    return ApiResponse(success=True, message="删除成功")


@router.get("/menus", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def list_menus() -> ApiResponse:
    """获取所有可用菜单项（供前端勾选）。"""
    menu_labels = {
        "dashboard": "仪表盘", "requirements": "需求管理", "generate": "用例生成",
        "testcases": "测试用例", "dependency_graph": "依赖图谱",
        "knowledge": "知识库", "knowledge_qa": "知识问答", "correction_review": "纠正审核",
        "projects_modules": "项目与模块", "admin": "管理后台",
        "admin_products": "产品管理", "admin_prompts": "提示词管理", "admin_settings": "系统设置",
        "admin_dashboard": "管理首页", "admin_users": "用户管理", "admin_roles": "角色管理",
        "admin_kb": "知识库配置", "admin_logs": "日志查看",
    }
    menus = [{"key": m, "label": menu_labels.get(m, m)} for m in DEFAULT_MENUS]
    return ApiResponse(success=True, message="ok", data=menus)
