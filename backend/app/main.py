"""FastAPI 应用入口。

组装所有中间件、路由、生命周期事件，对外暴露 ASGI 应用。

设计依据：
- docs/00-architecture-overview.md §2 访问层 + 编排层
- docs/00 §9.2 进度反馈（SSE，阶段3接入）
- 用户硬约束：所有中间件必须 async/await，不得使用回调风格

安全更新（v2）：
- JWT 认证中间件（除 /api/health 和 /api/auth/login 外全量保护）
- 内存 Token Bucket 限流（/api/* 全量覆盖）
- API Key 加密存储（Fernet 对称加密）
- CORS 白名单较之前收紧

启动方式：
    cd backend
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.config import settings
from app.core.auth import auth_middleware, get_default_token
from app.core.rate_limiter import rate_limit_middleware
from app.db.database import init_db
from app.db.milvus_store import vector_store
from app.models.api_schemas import ApiResponse
from app.utils.logger import logger, setup_logger


# ============================================================================
# 应用生命周期管理
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期上下文。

    启动时执行：
    1. 配置日志系统
    2. 初始化数据库（建表 + 种子数据）
    3. 预热向量库连接

    关闭时执行：
    1. 记录关闭事件

    Args:
        app: FastAPI 应用实例（由框架注入）
    """
    # ===== 启动阶段 =====
    setup_logger()
    logger.info("=" * 60)
    logger.info("AI测试用例生成系统 v{} 启动中...", __version__)
    logger.info("LLM 提供商: {}", settings.LLM_PROVIDER)
    logger.info("数据库: PostgreSQL")
    logger.info("向量存储后端: {}", settings.VECTOR_STORE)
    logger.info("限流: {} RPM", settings.API_RPM_LIMIT)
    logger.info("DEBUG 模式: {}", settings.DEBUG)
    logger.info("=" * 60)

    # v2.2: 启动时安全校验
    try:
        security_warnings = settings.validate_security()
        for w in security_warnings:
            logger.warning("安全警告: {}", w)
    except RuntimeError as e:
        logger.error("安全校验失败，拒绝启动: {}", str(e))
        raise

    # 仅在 DEBUG 模式下输出默认管理令牌，避免生产日志泄露凭证
    if settings.DEBUG:
        default_token = get_default_token()
        logger.debug(
            "默认管理令牌（调试用，24h有效，重启后变更）: Authorization: Bearer {}",
            default_token,
        )

    try:
        # 初始化数据库（建表 + 种子数据）
        await init_db()
    except Exception as e:
        logger.error("init_db 失败（应用将以降级模式启动）: {}", str(e))
        logger.warning("数据库初始化异常，但 LLM 配置加载仍将尝试")

    # 从 DB 加载持久化的 LLM 配置（覆盖 .env 默认值）
    # 独立于 init_db 执行：即使建表/迁移失败，也要确保 LLM 配置可用
    try:
        from app.api.settings import load_db_config_to_settings
        from app.db.database import async_session
        async with async_session() as session:
            await load_db_config_to_settings(session)
    except Exception as e:
        logger.warning("从 DB 加载 LLM 配置失败（使用 .env 默认值）: {}", str(e)[:120])

    try:
        # 预热向量库（懒加载触发客户端创建）
        _ = await asyncio.to_thread(lambda: vector_store.test_patterns)
        logger.info("向量库已就绪")
    except Exception as e:
        logger.warning("向量库预热失败（不影响启动）: {}", str(e)[:120])

    logger.info("AI测试用例生成系统 启动完成，监听端口 {}", settings.APP_PORT)

    yield

    # ===== 关闭阶段 =====
    logger.info("AI测试用例生成系统 正在关闭...")


# ============================================================================
# 创建 FastAPI 应用
# ============================================================================

app = FastAPI(
    title="AI测试用例生成系统 API",
    description=(
        "AI 驱动的测试用例自动生成系统。\n\n"
        "将非结构化软件需求经 5 个 Agent 流水线（ReqAgent → ClarifyAgent → "
        "QualityGate → ImpactAnalysisAgent → GenAgent → ReviewAgent）自动转化为"
        "符合 UTCF 规范的可执行测试用例。\n\n"
        "架构文档见 /docs 目录。"
    ),
    version=__version__,
    lifespan=lifespan,
    # v2.2: 生产环境关闭 API 文档，防止结构泄露
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
)


# ============================================================================
# 中间件（按执行顺序注册：内层中间件先执行）
# ============================================================================

# CORS 跨域（允许前端开发服务访问）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.FRONTEND_URL,
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """请求日志中间件。

    记录每个请求的方法、路径、耗时、状态码，便于调试与监控。

    Args:
        request: 请求对象
        call_next: 下一个中间件/路由处理函数

    Returns:
        Response: 响应对象
    """
    start_time = time.time()
    method = request.method
    path = request.url.path

    # 跳过健康检查与文档路径的日志（降低噪声）
    skip_log = path in ("/api/health", "/docs", "/redoc", "/openapi.json")

    response = await call_next(request)

    if not skip_log:
        duration_ms = (time.time() - start_time) * 1000
        logger.info(
            "{} {} → {} ({:.0f}ms)",
            method,
            path,
            response.status_code,
            duration_ms,
        )

    return response


# 认证中间件（除白名单路径外全部保护）
@app.middleware("http")
async def auth(request: Request, call_next):
    """认证中间件包装器。"""
    return await auth_middleware(request, call_next)


# 限流中间件（在所有 API 路径上执行，注册在 auth 之后使其处于更外层，
# 确保未认证请求也计入限流统计）
@app.middleware("http")
async def rate_limit(request: Request, call_next):
    """限流中间件包装器。"""
    return await rate_limit_middleware(request, call_next)


# ============================================================================
# 全局异常处理
# ============================================================================

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局未捕获异常处理器。

    将所有未处理异常转为统一的 ApiResponse 格式，避免向前端暴露堆栈。

    Args:
        request: 请求对象
        exc: 未捕获的异常

    Returns:
        JSONResponse: 统一错误响应（500）
    """
    logger.error(
        "未捕获异常 [{} {}]: {}",
        request.method,
        request.url.path,
        str(exc),
    )
    return JSONResponse(
        status_code=500,
        content=ApiResponse(
            success=False,
            message="服务器内部错误，请查看日志",
            data=None,
        ).model_dump(),
    )


# ============================================================================
# 健康检查
# ============================================================================

def _check_llm_configured() -> bool:
    """检查 LLM 是否已配置（优先 DB 配置，其次 .env）。"""
    api_key = settings.llm_config.get("api_key", "")
    if api_key and api_key != "sk-dummy":
        return True
    provider_cfgs = getattr(settings, "_all_provider_configs", {})
    primary = provider_cfgs.get(settings.LLM_PROVIDER, {})
    pk = primary.get("api_key", "") if primary else ""
    return bool(pk and pk != "sk-dummy")


def _check_vector_store() -> dict:
    """检查向量库（Milvus）连接状态和集合统计。

    返回结构：
        {
            "connected": bool,
            "error": str,           # 连接失败时的错误信息
            "model_ready": bool,     # 嵌入模型是否已加载
            "collections": {         # 各 Collection 的记录数
                "requirements": int,
                "test_cases": int,
                "patterns": int,
                "errors": int,
            },
            "kb_empty": bool,        # 所有 Collection 均为空
        }
    """
    result = {
        "connected": False,
        "error": "",
        "model_ready": False,
        "collections": {},
        "kb_empty": True,
        "kb_unavailable": False,
    }
    try:
        result["connected"] = True
        result["model_ready"] = vector_store.is_model_ready()
        counts = {}
        all_empty = True
        any_coll_error = False
        for coll_key in ("requirements", "test_cases", "patterns", "req_pats", "errors"):
            try:
                collection = vector_store._get_collection(coll_key)
                count = collection.num_entities
                counts[coll_key] = count
                if count > 0:
                    all_empty = False
            except Exception:
                counts[coll_key] = -1
                any_coll_error = True
        result["collections"] = counts
        # 当有连接/查询错误时标记 kb_unavailable；若集合查询全部成功且全部为 0 才是 kb_empty=True
        result["kb_unavailable"] = (not result["connected"]) or (not result["model_ready"]) or any_coll_error
        result["kb_empty"] = (not result["kb_unavailable"]) and all_empty
    except Exception as e:
        result["error"] = str(e)[:200]
        result["kb_unavailable"] = True
        result["kb_empty"] = False
    return result


@app.get("/api/health", tags=["系统"])
async def health_check() -> ApiResponse:
    """健康检查端点。

    返回应用基本信息与运行状态，供前端/负载均衡探活。

    Returns:
        ApiResponse: 包含 version / llm_provider / kb_empty / vector_db 等状态信息
    """
    vs_status = await asyncio.to_thread(_check_vector_store)

    return ApiResponse(
        success=True,
        message="ok",
        data={
            "version": __version__,
            "llm_provider": settings.LLM_PROVIDER,
            "llm_configured": _check_llm_configured(),
            "kb_empty": vs_status["kb_empty"],
            "kb_unavailable": vs_status["kb_unavailable"],
            "cold_start_mode": (not vs_status["kb_unavailable"]) and vs_status["kb_empty"],
            "db_type": "postgresql",
            "vector_store": os.getenv("VECTOR_STORE", "milvus"),
            "vector_db": {
                "connected": vs_status["connected"],
                "error": vs_status["error"],
                "model_ready": vs_status["model_ready"],
                "collections": vs_status["collections"],
            },
        },
    )


# ============================================================================
# 路由注册
# ============================================================================

from app.api import api_router

app.include_router(api_router, prefix="/api")


# ============================================================================
# 入口
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.APP_PORT,
        reload=settings.DEBUG,
    )
