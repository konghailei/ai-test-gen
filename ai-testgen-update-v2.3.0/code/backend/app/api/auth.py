"""认证路由（登录 + 令牌获取 + 当前用户信息）。

端点清单：
    POST /api/auth/login          使用用户名密码登录，获取 Bearer Token
    POST /api/auth/change-password 修改当前用户密码
    GET  /api/auth/me             获取当前登录用户信息（需要 Authorization Header）

安全设计（v2.2）：
- 登录 IP 维度限流（5 次/分钟/用户名）
- 5 次连续失败锁定 15 分钟（按用户名+IP 组合）
- 密码使用 PBKDF2-SHA256 哈希存储
- JWT 令牌内含用户信息（sub/username/role），中间件可直接读取
- 首次启动自动创建默认管理员（用户名 admin，密码见种子数据配置）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth import AuthToken, _decode
from app.db.database import get_session
from app.db.models import User
from app.models.api_schemas import ApiResponse
from app.utils.logger import logger
from app.utils.password import verify_password


router = APIRouter()


# ============================================================================
# 登录限流（v2.2 新增 — IP + 用户名维度）
# ============================================================================

@dataclass
class _LoginTracker:
    """登录失败追踪（内存实现，单进程适用）。"""
    attempts: list[float] = field(default_factory=list)  # 最近失败时间戳
    locked_until: float = 0.0  # 锁定截止时间戳

    @property
    def is_locked(self) -> bool:
        return time.time() < self.locked_until

    def record_failure(self) -> None:
        now = time.time()
        # 清除 1 分钟前的记录
        self.attempts = [t for t in self.attempts if now - t < 60]
        self.attempts.append(now)
        # 5 次失败立即锁定 15 分钟
        if len(self.attempts) >= 5:
            self.locked_until = now + 900  # 15 分钟
            logger.warning("登录锁定 15 分钟: 连续失败 {} 次", len(self.attempts))

    def clear(self) -> None:
        self.attempts = []
        self.locked_until = 0.0


# 全局追踪字典：key = f"{username}:{ip}" → _LoginTracker
_login_trackers: dict[str, _LoginTracker] = {}


def _get_login_key(username: str, ip: str) -> str:
    return f"{username}:{ip}"


def _check_login_rate_limit(username: str, ip: str) -> None:
    """检查登录频率限制。

    Raises:
        HTTPException 429: 频率超限
        HTTPException 423: 账号已锁定
    """
    now = time.time()
    key = _get_login_key(username, ip)
    tracker = _login_trackers.get(key)

    # 清理过期记录（每 5 分钟一次全量清理）
    if len(_login_trackers) > 1000:
        expired = [k for k, v in _login_trackers.items()
                   if now - max(v.attempts + [0]) > 3600]
        for k in expired:
            del _login_trackers[k]

    if tracker:
        # 检查是否锁定
        if tracker.is_locked:
            remaining = int(tracker.locked_until - now)
            raise HTTPException(
                status_code=423,
                detail=f"账户已临时锁定（密码错误超过 5 次），请等待 {remaining} 秒后重试",
            )
        # 检查频率
        recent = [t for t in tracker.attempts if now - t < 60]
        if len(recent) >= 5:
            raise HTTPException(
                status_code=429,
                detail="登录频率过高，请 1 分钟后重试",
            )


def _record_login_failure(username: str, ip: str) -> None:
    key = _get_login_key(username, ip)
    tracker = _login_trackers.setdefault(key, _LoginTracker())
    tracker.record_failure()


def _clear_login_failures(username: str, ip: str) -> None:
    key = _get_login_key(username, ip)
    tracker = _login_trackers.get(key)
    if tracker:
        tracker.clear()


# ============================================================================
# 请求/响应模型
# ============================================================================

class LoginRequest(BaseModel):
    """登录请求模型。"""
    username: str = Field(..., min_length=1, max_length=64, description="用户名")
    password: str = Field(..., min_length=1, max_length=128, description="密码")


class UserInfo(BaseModel):
    """用户信息响应模型。"""
    id: int
    username: str
    role: str
    permissions: list[str] = Field(default_factory=list, description="菜单权限列表")
    display_name: str
    email: str


# ============================================================================
# 路由
# ============================================================================

@router.post("/login", response_model=ApiResponse)
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """用户登录，返回 Bearer Token。

    验证用户名和密码，成功后签发 JWT 令牌。
    令牌有效期可配置（默认 settings.JWT_EXPIRE_MINUTES 分钟）。

    安全（v2.2）：
    - IP + 用户名维度限流：5 次/分钟
    - 5 次连续失败锁定 15 分钟
    """
    # 获取客户端 IP：直接使用 TCP 连接地址，不信任可被客户端伪造的 X-Forwarded-For。
    # 此前直接取 X-Forwarded-For 头，攻击者只需轮换该头即可绕过登录限流与失败锁定，
    # 实现无限次撞库。与全局限流 rate_limiter.py 取 IP 方式保持一致。
    client_ip = request.client.host if request.client else "unknown"

    # 检查限流 + 锁定
    _check_login_rate_limit(body.username, client_ip)

    # 查询用户
    result = await session.execute(
        select(User).where(User.username == body.username)
    )
    user = result.scalar_one_or_none()

    if user is None:
        logger.warning("[auth] 登录失败：用户不存在 - username={}", body.username)
        _record_login_failure(body.username, client_ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # 检查账户是否启用
    if not user.is_active:
        logger.warning("[auth] 登录失败：账户已禁用 - username={}", body.username)
        _record_login_failure(body.username, client_ip)
        raise HTTPException(status_code=401, detail="账户已禁用，请联系管理员")

    # 验证密码
    if not verify_password(body.password, user.password_hash):
        logger.warning("[auth] 登录失败：密码错误 - username={}", body.username)
        _record_login_failure(body.username, client_ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # 登录成功：清除失败计数
    _clear_login_failures(body.username, client_ip)

    # 从 Role 表加载角色名和菜单权限
    role_name = "user"
    permissions: list[str] = []
    if user.role_id:
        from app.db.models import Role
        stmt = select(Role).where(Role.id == user.role_id)
        result = await session.execute(stmt)
        role = result.scalar_one_or_none()
        if role:
            role_name = role.name
            permissions = role.menus or []

    # 签发令牌
    token = AuthToken.create(
        username=user.username,
        role=role_name,
        user_id=user.id,
        permissions=permissions,
    )

    logger.info("[auth] 登录成功: username={}, role={}, menus={}",
                 user.username, role_name, len(permissions))

    return ApiResponse(
        success=True,
        message="登录成功",
        data={
            "token": token,
            "token_type": "Bearer",
            "expires_in_minutes": settings.JWT_EXPIRE_MINUTES,
            "user": UserInfo(
                id=user.id,
                username=user.username,
                role=role_name,
                permissions=permissions,
                display_name=user.display_name,
                email=user.email,
            ).model_dump(),
        },
    )


# ============================================================================
# 修改密码请求模型
# ============================================================================

class ChangePasswordRequest(BaseModel):
    """修改密码请求模型。"""
    current_password: str = Field(..., min_length=1, max_length=128, description="当前密码")
    new_password: str = Field(..., min_length=4, max_length=128, description="新密码（至少 4 位）")


@router.post("/change-password", response_model=ApiResponse)
async def change_password(
    body: ChangePasswordRequest,
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """修改当前用户密码。

    要求提供当前密码验证身份，通过后更新为新密码哈希。

    Args:
        body: ChangePasswordRequest（current_password + new_password）
        authorization: Bearer token
        session: 数据库会话

    Returns:
        ApiResponse: 修改成功提示

    Raises:
        HTTPException 401: 未登录或令牌无效
        HTTPException 401: 当前密码错误
        HTTPException 400: 新密码与当前密码相同
    """
    # 解析 token，提取用户信息
    if not authorization:
        raise HTTPException(status_code=401, detail="缺少 Authorization 请求头")

    token = authorization
    if token.startswith("Bearer "):
        token = token[7:]

    payload = _decode(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="令牌无效或已过期")
    if payload.get("exp", 0) < time.time():
        raise HTTPException(status_code=401, detail="令牌已过期")

    username = payload.get("username", "")
    if not username:
        raise HTTPException(status_code=401, detail="令牌中缺少用户信息")

    # 查询用户
    result = await session.execute(
        select(User).where(User.username == username)
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")

    # 验证当前密码
    if not verify_password(body.current_password, user.password_hash):
        logger.warning("[auth] 修改密码失败：当前密码错误 - username={}", username)
        raise HTTPException(status_code=401, detail="当前密码错误")

    # 检查新旧密码是否相同
    if body.current_password == body.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")

    # 更新密码
    from app.utils.password import hash_password
    user.password_hash = hash_password(body.new_password)
    await session.commit()

    logger.info("[auth] 用户 {} 已成功修改密码", username)

    return ApiResponse(
        success=True,
        message="密码修改成功，请使用新密码重新登录",
    )


@router.get("/me", response_model=ApiResponse)
async def get_current_user(
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取当前登录用户信息。

    从 Authorization Header 中提取令牌，解析令牌中的用户信息后从 DB 查询。

    Args:
        authorization: Bearer token（从 Header 自动提取）
        session: 数据库会话

    Returns:
        ApiResponse: 用户信息

    Raises:
        HTTPException 401: 令牌无效或用户不存在
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="缺少 Authorization 请求头")

    token = authorization
    if token.startswith("Bearer "):
        token = token[7:]

    payload = _decode(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="令牌无效或已过期")
    if payload.get("exp", 0) < time.time():
        raise HTTPException(status_code=401, detail="令牌已过期")

    username = payload.get("username", "")
    if not username:
        raise HTTPException(status_code=401, detail="令牌中缺少用户信息")

    result = await session.execute(
        select(User).where(User.username == username)
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")

    # 加载角色权限
    role_name = payload.get("role", "user")
    permissions: list[str] = payload.get("permissions") or []
    if user.role_id:
        from app.db.models import Role
        role_result = await session.execute(select(Role).where(Role.id == user.role_id))
        role = role_result.scalar_one_or_none()
        if role:
            role_name = role.name
            permissions = role.menus or []

    return ApiResponse(
        success=True,
        message="ok",
        data=UserInfo(
            id=user.id,
            username=user.username,
            role=role_name,
            permissions=permissions,
            display_name=user.display_name,
            email=user.email,
        ).model_dump(),
    )
