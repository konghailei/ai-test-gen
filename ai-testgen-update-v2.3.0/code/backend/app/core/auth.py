"""JWT 认证模块。

提供用户令牌签发、验证和 FastAPI 中间件。
支持多用户：令牌 Payload 中携带 username / role / user_id。

设计原则：
- 使用 Python 标准库 hashlib + hmac 实现，零外部依赖
- 令牌 HMAC-SHA256 签名，防篡改
- 白名单路径（健康检查、登录）跳过认证
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Optional

from fastapi import Header, HTTPException, Request, Depends
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings
from app.utils.logger import logger


# ============================================================================
# 令牌创建与验证
# ============================================================================

class AuthToken:
    """JWT 风格的认证令牌。

    Payload 结构：
        {
            "sub": "<username>",         # 用户名
            "username": "<username>",
            "role": "admin|user|viewer",
            "user_id": 1,
            "iat": 1718000000,           # 签发时间
            "exp": 1718004800            # 过期时间
        }
    """

    @staticmethod
    def create(
        username: str = "admin",
        role: str = "admin",
        user_id: int = 1,
        permissions: list[str] | None = None,
        expire_minutes: int | None = None,
    ) -> str:
        """签发新令牌。

        Args:
            username: 用户名
            role: 用户角色
            user_id: 用户 ID
            permissions: 菜单权限列表（v2.12）
            expire_minutes: 过期分钟数（默认 settings.JWT_EXPIRE_MINUTES）

        Returns:
            str: JWT 格式令牌
        """
        expire = expire_minutes or settings.JWT_EXPIRE_MINUTES
        payload: dict = {
            "sub": username,
            "username": username,
            "role": role,
            "user_id": user_id,
            "iat": int(time.time()),
            "exp": int(time.time()) + expire * 60,
        }
        if permissions is not None:
            payload["permissions"] = permissions
        return _encode(payload)

    @staticmethod
    def verify(token: str) -> bool:
        """验证令牌有效性。

        Args:
            token: 待验证的令牌

        Returns:
            bool: True 表示有效
        """
        try:
            payload = _decode(token)
            if payload is None:
                return False
            # 检查过期
            if payload.get("exp", 0) < time.time():
                return False
            return True
        except Exception:
            return False


# ============================================================================
# JWT 编解码（HMAC-SHA256）
# ============================================================================

def _base64url_encode(data: bytes) -> str:
    """URL-safe base64 编码（无填充）。"""
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _base64url_decode(s: str) -> bytes:
    """URL-safe base64 解码（兼容无填充）。"""
    import base64
    # 补回填充
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def _encode(payload: dict) -> str:
    """HMAC-SHA256 JWT 编码。"""
    # Header
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _base64url_encode(json.dumps(header, separators=(",", ":")).encode())
    # Payload
    payload_b64 = _base64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    # Signature
    message = f"{header_b64}.{payload_b64}"
    secret = settings.APP_SECRET_KEY.encode("utf-8")
    signature = hmac.new(secret, message.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = _base64url_encode(signature)
    return f"{message}.{sig_b64}"


def _decode(token: str) -> dict | None:
    """HMAC-SHA256 JWT 解码并验签。"""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_b64, payload_b64, sig_b64 = parts

    # 重建签名验证
    message = f"{header_b64}.{payload_b64}"
    secret = settings.APP_SECRET_KEY.encode("utf-8")
    expected_sig = hmac.new(secret, message.encode("utf-8"), hashlib.sha256).digest()
    actual_sig = _base64url_decode(sig_b64)
    if not hmac.compare_digest(expected_sig, actual_sig):
        return None

    # 解析 Payload
    try:
        payload_json = _base64url_decode(payload_b64)
        return json.loads(payload_json)
    except Exception:
        return None


# ============================================================================
# 中间件
# ============================================================================

# 无需认证的白名单路径
AUTH_WHITELIST = {
    "/api/health",
    "/api/auth/login",
    "/docs",
    "/redoc",
    "/openapi.json",
}


async def auth_middleware(request: Request, call_next):
    """认证中间件。

    对需要认证的路径检查 Authorization Header。
    白名单路径（健康检查、登录、OpenAPI 文档）跳过认证。

    Usage:
        app.middleware("http")(auth_middleware)
    """
    path = request.url.path.rstrip("/") or "/"

    # 白名单路径跳过认证
    if path in AUTH_WHITELIST or path.startswith(("/docs/", "/redoc/", "/openapi.json")):
        return await call_next(request)

    # OPTIONS 预检请求跳过
    if request.method == "OPTIONS":
        return await call_next(request)

    # 检查认证头
    auth = request.headers.get("authorization")
    if not auth:
        return JSONResponse(
            status_code=401,
            content={"success": False, "message": "缺少 Authorization 请求头", "data": None},
        )

    token = auth
    if token.startswith("Bearer "):
        token = token[7:]

    if not AuthToken.verify(token):
        return JSONResponse(
            status_code=401,
            content={"success": False, "message": "令牌无效或已过期", "data": None},
        )

    # 将用户信息注入 request.state，供下游使用（v2.2 新增）
    payload = _decode(token)
    if payload:
        request.state.current_user = CurrentUser(
            username=payload.get("username", ""),
            role=payload.get("role", "viewer"),
            user_id=payload.get("user_id", 0),
        )

    return await call_next(request)


# 预置管理令牌（首次启动时可在日志中查看）
_DEFAULT_TOKEN: str | None = None


def get_default_token() -> str:
    """获取预置管理令牌。

    首次调用时生成并缓存，重启后失效（需要重新登录获取）。

    Returns:
        str: Bearer token
    """
    global _DEFAULT_TOKEN
    if _DEFAULT_TOKEN is None:
        _DEFAULT_TOKEN = AuthToken.create(
            username="admin",
            role="admin",
            user_id=1,
            expire_minutes=1440,  # 24 小时
        )
    return _DEFAULT_TOKEN


# ============================================================================
# RBAC 依赖项（v2.2 新增）
# ============================================================================

# Bearer token 提取器（用于 Depends）
_bearer_scheme = HTTPBearer(auto_error=False)


class CurrentUser:
    """当前请求用户信息（从 JWT 解析）。"""

    def __init__(self, username: str, role: str, user_id: int):
        self.username = username
        self.role = role  # admin / user / viewer
        self.user_id = user_id

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_write(self) -> bool:
        """user 及以上角色可写（admin + user）。"""
        return self.role in ("admin", "user")

    def __repr__(self) -> str:
        return f"CurrentUser(username={self.username!r}, role={self.role!r})"


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> CurrentUser:
    """FastAPI 依赖项：从 Authorization Header 解析当前用户。

    Usage:
        @router.get("/xxx", dependencies=[Depends(get_current_user)])
        def handler(user: CurrentUser = Depends(get_current_user)):
            print(user.username, user.role)

    Raises:
        HTTPException(401): 未提供 token 或 token 无效
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="缺少认证凭据")
    token = credentials.credentials
    payload = _decode(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="令牌无效或已过期")
    if payload.get("exp", 0) < time.time():
        raise HTTPException(status_code=401, detail="令牌已过期")
    return CurrentUser(
        username=payload.get("username", ""),
        role=payload.get("role", "viewer"),
        user_id=payload.get("user_id", 0),
    )


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """FastAPI 依赖项：要求 admin 角色。

    Usage:
        @router.delete("/xxx", dependencies=[Depends(require_admin)])
        def handler(): ...
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def require_write(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """FastAPI 依赖项：要求 user 或 admin 角色（可写权限）。

    viewer 角色被拒绝。

    Usage:
        @router.post("/xxx", dependencies=[Depends(require_write)])
        def handler(): ...
    """
    if not user.can_write:
        raise HTTPException(status_code=403, detail="需要写权限（user 或 admin 角色）")
    return user
