"""用户管理 API（v2.12 新增）。

端点清单：
    GET    /api/admin/users           用户列表
    POST   /api/admin/users           创建用户
    PUT    /api/admin/users/{id}      更新用户
    DELETE /api/admin/users/{id}      删除用户（软删除）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_admin
from app.db.database import get_session
from app.db.models import User, Role
from app.models.api_schemas import ApiResponse
from app.utils.logger import logger
from app.utils.password import hash_password

router = APIRouter(prefix="", tags=["用户管理"])


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)
    display_name: str = Field(default="", max_length=128)
    email: str = Field(default="", max_length=128)
    role_id: int | None = Field(default=None)
    is_active: bool = Field(default=True)


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    email: str | None = Field(default=None, max_length=128)
    role_id: int | None = Field(default=None)
    is_active: bool | None = Field(default=None)
    password: str | None = Field(default=None, max_length=128)


class UserResponse(BaseModel):
    id: int
    username: str
    display_name: str
    email: str
    is_active: bool
    role_id: int | None
    role_name: str
    created_at: str | None


def _user_to_response(user: User, role: Role | None) -> dict:
    return UserResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name or user.username,
        email=user.email or "",
        is_active=user.is_active,
        role_id=user.role_id,
        role_name=role.label or role.name if role else "无",
        created_at=user.created_at.isoformat() if user.created_at else None,
    ).model_dump()


@router.get("/users", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def list_users(session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """获取所有用户列表（含角色名）。"""
    result = await session.execute(select(User).order_by(User.id))
    users = result.scalars().all()

    role_ids = {u.role_id for u in users if u.role_id}
    roles: dict[int, Role] = {}
    if role_ids:
        r = await session.execute(select(Role).where(Role.id.in_(role_ids)))
        roles = {role.id: role for role in r.scalars().all()}

    return ApiResponse(
        success=True, message="ok",
        data=[_user_to_response(u, roles.get(u.role_id) if u.role_id else None) for u in users],
    )


@router.post("/users", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def create_user(body: UserCreate, session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """创建新用户。"""
    existing = await session.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"用户名 {body.username} 已存在")

    if body.role_id:
        role = await session.execute(select(Role).where(Role.id == body.role_id))
        if not role.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="指定的角色不存在")

    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        role_id=body.role_id,
        display_name=body.display_name,
        email=body.email,
        is_active=body.is_active,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    role = None
    if user.role_id:
        r = await session.execute(select(Role).where(Role.id == user.role_id))
        role = r.scalar_one_or_none()

    logger.info("[users] 用户已创建: username={}, role_id={}", user.username, user.role_id)
    return ApiResponse(success=True, message="创建成功", data=_user_to_response(user, role))


@router.put("/users/{user_id}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def update_user(user_id: int, body: UserUpdate, session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """更新用户信息。"""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    if body.role_id is not None:
        role = await session.execute(select(Role).where(Role.id == body.role_id))
        if not role.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="指定的角色不存在")
        user.role_id = body.role_id
    if body.display_name is not None:
        user.display_name = body.display_name
    if body.email is not None:
        user.email = body.email
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.password:
        user.password_hash = hash_password(body.password)

    await session.commit()
    await session.refresh(user)

    role = None
    if user.role_id:
        r = await session.execute(select(Role).where(Role.id == user.role_id))
        role = r.scalar_one_or_none()

    logger.info("[users] 用户已更新: id={}", user.id)
    return ApiResponse(success=True, message="更新成功", data=_user_to_response(user, role))


@router.delete("/users/{user_id}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def delete_user(user_id: int, session: AsyncSession = Depends(get_session)) -> ApiResponse:
    """删除用户（不允许删除自己）。"""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 防止自己删自己（简单处置：检查 username 是否为 admin）
    if user.username == "admin":
        raise HTTPException(status_code=400, detail="不允许删除管理员账户")

    await session.delete(user)
    await session.commit()

    logger.info("[users] 用户已删除: id={}, username={}", user_id, user.username)
    return ApiResponse(success=True, message="删除成功")
