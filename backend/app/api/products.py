"""产品/项目/模块管理 + 需求依赖图谱 API。

路由前缀：/api/products（由 __init__.py 注册）
设计依据：
- docs/02-knowledge-base.md §2.4（需求依赖图谱）
- models.py: Product → Project → Module 三级层次

安全更新（v2）：
- 所有接受 body 的端点使用 Pydantic 验证模型，替代裸 dict
- 输入校验：必填字段、字符串长度限制、ID 格式

修复（v3）：
- 产品 ID 自动生成改为时间戳后缀，避免全局计数器重启后重复
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from app.core.auth import CurrentUser, require_admin, require_write, get_current_user
from app.core.product_access import (
    apply_visible_product_filter,
    assert_product_visible,
    get_visible_product_ids,
    invalidate_product_cache,
)
from app.core.product_guard import (
    product_id_of_dependency_version,
    product_id_of_fp,
    product_id_of_module,
    product_id_of_project,
)
from app.core.vector_outbox import enqueue as _enqueue_outbox
from pydantic import BaseModel, Field
from sqlalchemy import select, delete, update, exc as sa_exc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.db.models import (
    Product, Project, Module, Requirement, ModuleDependency, TestCase, User,
    FunctionalPoint, FPDependency, AnalysisTask, GenerationBatch, ReqDependency,
    Traceability, ErrorCase,
    FPDependencyVersion, FPDependencyVersionItem, FPDependencyHistory,
)
from app.models.api_schemas import ApiResponse
from app.utils.logger import logger

def _as_int(value: object) -> int | None:
    """路径参数转 int（失败返回 None，由端点自身返回 422/404）。"""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def _guard_product_scope(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """路由级产品归属守卫（v4.1.9）。

    从路径参数解析资源所属产品，校验对当前用户可见：
        pid / product_id → 本身即产品 ID
        proj_id          → projects.product_id
        mod_id           → modules.product_id
        fp_id            → functional_points.requirement_id → requirements.product_id
        version_pk / v1  → fp_dependency_versions.project_id → projects.product_id

    资源不存在 → 放行（端点自身返回 404）；product_id 为空 → 放行；
    admin →``assert_product_visible`` 内部直接放行。

    注意：本守卫只处理**路径参数**。``GET /dependency-graph`` 的 product_id 是
    query 参数，取不到，由端点自身按可见集合过滤。
    """
    path = request.path_params
    pid = path.get("pid") or path.get("product_id")

    if not pid:
        if path.get("proj_id"):
            pid = await product_id_of_project(session, path["proj_id"])
        elif path.get("mod_id"):
            pid = await product_id_of_module(session, path["mod_id"])
        elif path.get("fp_id"):
            pid = await product_id_of_fp(session, path["fp_id"])
        else:
            pid = await product_id_of_dependency_version(
                session, _as_int(path.get("version_pk") or path.get("v1"))
            )

    if pid:
        await assert_product_visible(session, user, pid)


# v4.1.9：守卫挂在 router 级，一次覆盖产品/项目/模块/依赖版本的全部读写端点。
# 与 requirements.py 的 _guard_product_visibility 同一模式，避免逐端点补漏。
router = APIRouter(dependencies=[Depends(_guard_product_scope)])


def _is_unique_violation(exc: Exception, constraint_name: str) -> bool:
    """检测 PostgreSQL/SQLAlchemy 唯一约束冲突。"""
    msg = str(exc.orig) if hasattr(exc, "orig") else str(exc)
    return constraint_name.lower() in msg.lower() or "UNIQUE constraint failed" in msg


# ============================================================================
# Pydantic 请求模型
# ============================================================================

class CreateProductRequest(BaseModel):
    """创建产品的请求模型。"""
    name: str = Field(..., min_length=1, max_length=128, description="产品名称")
    description: str = Field(default="", max_length=1024, description="产品描述")
    id: str | None = Field(default=None, max_length=64, description="产品 ID（可选，留空自动生成）")
    visible_to_user_ids: list[int] | None = Field(default=None, description="可查看此产品的用户ID列表（P1-1 隔离，空=全员可见）")


class UpdateProductRequest(BaseModel):
    """更新产品的请求模型。"""
    name: str | None = Field(default=None, min_length=1, max_length=128, description="产品名称")
    description: str | None = Field(default=None, max_length=1024, description="产品描述")
    visible_to_user_ids: list[int] | None = Field(default=None, description="可查看此产品的用户ID列表")


class CreateProjectRequest(BaseModel):
    """创建项目的请求模型。"""
    name: str = Field(..., min_length=1, max_length=128, description="项目名称")
    description: str = Field(default="", max_length=1024, description="项目描述")
    id: str | None = Field(default=None, max_length=64, description="项目 ID（可选）")
    version: str = Field(default="1.0", max_length=32, description="项目版本号")


class UpdateProjectRequest(BaseModel):
    """更新项目的请求模型（字段均可选，按需更新）。"""
    name: str | None = Field(default=None, min_length=1, max_length=128, description="项目名称")
    description: str | None = Field(default=None, max_length=1024, description="项目描述")
    version: str | None = Field(default=None, max_length=32, description="项目版本号（同一产品内不可重复）")


class CreateModuleRequest(BaseModel):
    """创建模块的请求模型。"""
    name: str = Field(..., min_length=1, max_length=64, description="模块名称")
    description: str = Field(default="", max_length=512, description="模块描述")


class AddDependencyEdgeRequest(BaseModel):
    """添加依赖连线的请求模型。"""
    source: str = Field(..., min_length=1, max_length=64, description="源模块 ID")
    target: str = Field(..., min_length=1, max_length=64, description="目标模块 ID")
    relation_type: str = Field(default="depends_on", max_length=32, description="关系类型")
    strength: str = Field(default="medium", max_length=16, description="强度")


# ============================================================================
# 1. 产品 CRUD
# ============================================================================

@router.get("/", response_model=ApiResponse)
async def list_products(
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """列出用户有权查看的产品。管理员可看全部。"""
    from app.core.auth import CurrentUser
    result = await session.execute(select(Product).order_by(Product.created_at.desc()))
    rows = result.scalars().all()

    is_admin = user.role == "admin"
    filtered = []
    for r in rows:
        vids = r.visible_to_user_ids or []
        if is_admin or not vids or user.user_id in vids:
            filtered.append({
                "id": r.id, "name": r.name, "description": r.description,
                "visible_to_user_ids": vids,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            })
    return ApiResponse(success=True, message="ok", data=filtered)


@router.post("/", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def create_product(body: CreateProductRequest, session: AsyncSession = Depends(get_session)):
    """创建产品。产品 ID 自动生成（时间戳后缀 + 随机数防冲突）。"""
    if body.id:
        pid = body.id
    else:
        # 使用时间戳 + 随机后缀，避免全局计数器重启后重复
        ts = int(time.time() * 1000) % 100000
        rand_suffix = random.randint(100, 999)
        pid = f"PROD-{ts:05d}-{rand_suffix}"

    record = Product(
        id=pid, name=body.name, description=body.description,
        visible_to_user_ids=body.visible_to_user_ids,  # P1-1 隔离名单随创建落库
    )
    session.add(record)
    try:
        await session.commit()
    except sa_exc.IntegrityError as e:
        await session.rollback()
        if _is_unique_violation(e, "uq_product_name"):
            raise HTTPException(status_code=409, detail=f"产品名称「{body.name}」已存在，请使用其他名称")
        raise HTTPException(status_code=500, detail="创建产品失败")
    logger.info("[API/products] 产品已创建: id={}, name={}", pid, body.name)
    # v4.1.9：新建产品必须立刻进可见性矩阵，否则 5s TTL 内它「不在矩阵里」，
    # get_visible_product_ids 会把它漏掉（新建产品对创建者本人不可见）
    invalidate_product_cache()
    return ApiResponse(success=True, message="产品已创建", data={"id": pid, "name": body.name})


@router.put("/{pid}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def update_product(pid: str, body: UpdateProductRequest, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Product).where(Product.id == pid))
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(404, "产品不存在")
    if body.name is not None:
        row.name = body.name
    if body.description is not None:
        row.description = body.description
    if body.visible_to_user_ids is not None:
        row.visible_to_user_ids = body.visible_to_user_ids
    try:
        await session.commit()
    except sa_exc.IntegrityError as e:
        await session.rollback()
        if _is_unique_violation(e, "uq_product_name"):
            raise HTTPException(status_code=409, detail=f"产品名称「{body.name}」已存在，请使用其他名称")
        raise HTTPException(status_code=500, detail="更新产品失败")
    # v4.1.9：可见名单可能已变，立即失效缓存（不能等 5s TTL）
    invalidate_product_cache()
    return ApiResponse(success=True, message="已更新")


@router.get("/{pid}/delete-check", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def delete_product_check(pid: str, session: AsyncSession = Depends(get_session)):
    """产品删除预检查：返回产品名称与关联数据统计，供前端二次确认。

    前端删除前调用此接口，展示「将删除 X 个项目/Y 个模块/Z 个需求…」，
    并要求用户输入产品名称才能继续删除。
    """
    result = await session.execute(select(Product).where(Product.id == pid))
    prod = result.scalar_one_or_none()
    if not prod:
        raise HTTPException(404, "产品不存在")

    # 统计各关联表数量（不真正删除）
    async def _count(stmt):
        r = await session.execute(select(func.count()).select_from(stmt.subquery()))
        return r.scalar() or 0

    project_cnt = await _count(select(Project.id).where(Project.product_id == pid))
    module_cnt = await _count(select(Module.id).where(Module.product_id == pid))
    req_cnt = await _count(select(Requirement.id).where(Requirement.product_id == pid))

    # 需求关联的子表数量
    req_ids_subq = select(Requirement.id).where(Requirement.product_id == pid).scalar_subquery()
    tc_cnt = await _count(select(TestCase.id).where(TestCase.req_id.in_(req_ids_subq)))
    fp_cnt = await _count(select(FunctionalPoint.id).where(FunctionalPoint.requirement_id.in_(req_ids_subq)))

    return ApiResponse(success=True, message="ok", data={
        "id": prod.id,
        "name": prod.name,
        "stats": {
            "projects": project_cnt,
            "modules": module_cnt,
            "requirements": req_cnt,
            "test_cases": tc_cnt,
            "functional_points": fp_cnt,
        },
    })


@router.delete("/{pid}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def delete_product(
    pid: str,
    session: AsyncSession = Depends(get_session),
    confirm_name: str | None = Query(default=None, description="二次确认：产品名称（必须与实际名称完全一致）"),
):
    """删除产品及关联的项目、模块（级联）。

    安全要求：必须传入 confirm_name 参数，且与产品名称完全一致才执行删除。
    前端在删除前应弹出输入框，要求用户手动输入产品名称。
    """
    # 1) 查产品是否存在 + 校验确认名称
    result = await session.execute(select(Product).where(Product.id == pid))
    prod = result.scalar_one_or_none()
    if not prod:
        raise HTTPException(404, "产品不存在")
    if not confirm_name:
        raise HTTPException(400, "请输入产品名称以确认删除")
    if confirm_name.strip() != prod.name:
        raise HTTPException(400, f"输入的产品名称「{confirm_name}」与实际名称「{prod.name}」不匹配，删除已阻止")

    # 2) 查该产品下所有需求，用于级联删除与清理向量库
    req_result = await session.execute(
        select(Requirement.id).where(Requirement.product_id == pid)
    )
    req_ids = [row[0] for row in req_result.all()]

    # 3) 应用层级联删除关联数据（先子表后父表，避免外键冲突）
    # 必须先删引用 Project/Requirement 的子表，最后再删 Project 与 Product 本身
    try:
        # 模块依赖边（旧库可能未生效 ondelete=CASCADE，显式删除更安全）
        mod_ids_subq = select(Module.id).where(Module.product_id == pid).scalar_subquery()
        await session.execute(
            delete(ModuleDependency).where(
                ModuleDependency.source_module_id.in_(mod_ids_subq)
                | ModuleDependency.target_module_id.in_(mod_ids_subq)
            )
        )
        await session.execute(delete(Module).where(Module.product_id == pid))
        await _cascade_delete_requirements(session, req_ids)
        await session.execute(delete(Project).where(Project.product_id == pid))
        await session.execute(delete(Product).where(Product.id == pid))

        # v4.1.9：范围删除入箱（与业务删除同事务）。原实现 commit 后直删 Milvus，
        # 失败仅 warning + 单条退化，仍失败就是永久孤儿。scope 事件由 relay
        # 幂等投递：按 product_id 批量删需求/FP/用例向量（内部含 KB 缓存失效）。
        _enqueue_outbox(
            session,
            aggregate_type="scope",
            aggregate_id=f"product:{pid}",
            action="delete",
            revision=0,
            payload={"scope_type": "product", "scope_id": pid, "product_id": pid},
            created_by="delete_product",
        )
        await session.commit()
    except sa_exc.IntegrityError as e:
        await session.rollback()
        logger.error("[API/products] 产品删除失败(外键冲突): pid={}, error={}", pid, str(e)[:200])
        raise HTTPException(500, f"删除失败：存在未清理的关联数据（外键冲突）。详情：{str(e.orig)[:200]}")
    except Exception as e:
        await session.rollback()
        logger.error("[API/products] 产品删除失败: pid={}, error={}", pid, str(e)[:200])
        raise HTTPException(500, f"删除失败：{str(e)[:200]}")

    # v4.1.9：向量清理已随业务删除同事务入箱（见上），此处不再直删。
    # 原先的「批量删 + 失败退化为逐条」两条路径都已由 relay 的 scope 投递替代。

    # v4.1.9：产品行已删，立即失效可见性缓存（否则 5s 内仍会出现在可见列表里）
    invalidate_product_cache()

    logger.info("[API/products] 产品已删除: id={}, name={}, 关联需求 {} 条已清理", pid, prod.name, len(req_ids))
    return ApiResponse(success=True, message="已删除")


# ============================================================================
# 2. 项目 CRUD
# ============================================================================

@router.get("/{pid}/projects", response_model=ApiResponse)
async def list_projects(
    pid: str,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
):
    """v2.8.69: 补全统一登录态依赖（与 list_products 对齐）和空值安全防护。
    避免 r.created_at=None 时 .isoformat() 崩溃或未登录态绕过导致的前端异常。
    """
    from app.core.auth import CurrentUser
    from app.core.product_access import assert_product_visible
    # P1-1 产品隔离：父产品不可见则列出项目同样拒绝
    await assert_product_visible(session, _user, pid)
    result = await session.execute(
        select(Project).where(Project.product_id == pid).order_by(Project.created_at.desc())
    )
    rows = result.scalars().all()
    data = []
    for r in rows:
        try:
            created_at = r.created_at.isoformat() if r.created_at else ""
        except Exception:
            created_at = ""
        try:
            go_live_date = r.go_live_date.isoformat() if r.go_live_date else ""
        except Exception:
            go_live_date = ""
        try:
            archived_at = r.archived_at.isoformat() if r.archived_at else ""
        except Exception:
            archived_at = ""
        data.append({
            "id": r.id or "",
            "product_id": r.product_id or "",
            "name": r.name or "",
            "description": r.description or "",
            "version": r.version or "1.0",
            "created_at": created_at,
            "status": r.status or "active",
            "go_live_date": go_live_date,
            "archived_at": archived_at,
            "archived_by": r.archived_by or "",
        })
    return ApiResponse(success=True, message="ok", data=data)


@router.get("/projects/{proj_id}/detail", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_project_detail(
    proj_id: str,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
):
    """v3.2.7: 项目详情（支撑项目详情页"需求/用例"双标签）。

    项目详情页需要独立获取项目元信息（名称/版本/状态/归档时间），
    此前仅有按产品列项目的接口，刷新详情页会丢失项目信息，故补充单查接口。

    注意：路径刻意用 /projects/{proj_id}/detail，避免与 /{pid}/projects 列表路由歧义。
    """
    from app.core.auth import CurrentUser
    from app.core.product_access import assert_product_visible

    result = await session.execute(select(Project).where(Project.id == proj_id))
    proj = result.scalar_one_or_none()
    if not proj:
        raise HTTPException(status_code=404, detail=f"项目 {proj_id} 不存在")
    # P1-1 产品隔离：父产品不可见则拒绝
    await assert_product_visible(session, _user, proj.product_id)

    def _iso(dt):
        try:
            return dt.isoformat() if dt else ""
        except Exception:
            return ""

    return ApiResponse(success=True, message="ok", data={
        "id": proj.id or "",
        "product_id": proj.product_id or "",
        "name": proj.name or "",
        "description": proj.description or "",
        "version": proj.version or "1.0",
        "status": proj.status or "active",
        "created_at": _iso(proj.created_at),
        "go_live_date": _iso(proj.go_live_date),
        "archived_at": _iso(proj.archived_at),
        "archived_by": proj.archived_by or "",
    })


@router.post("/{pid}/projects", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def create_project(pid: str, body: CreateProjectRequest, session: AsyncSession = Depends(get_session)):
    """创建项目。项目 ID 自动生成（时间戳后缀 + 随机数防冲突）。"""
    if body.id:
        proj_id = body.id
    else:
        ts = int(time.time() * 1000) % 100000
        rand_suffix = random.randint(100, 999)
        proj_id = f"PROJ-{pid.split('-')[-1]}-{ts:05d}-{rand_suffix}"

    # 版本号查重（同一产品内不可重复）
    version = (body.version or "1.0").strip()
    dup_version = await session.execute(
        select(Project).where(Project.product_id == pid, Project.version == version)
    )
    if dup_version.scalars().first():
        raise HTTPException(
            status_code=409,
            detail=f"项目版本号「{version}」在当前产品下已存在，请使用其他版本号",
        )

    record = Project(
        id=proj_id, product_id=pid, name=body.name, description=body.description,
        version=version,
    )
    session.add(record)
    try:
        await session.commit()
    except sa_exc.IntegrityError as e:
        await session.rollback()
        if _is_unique_violation(e, "uq_project_product_name"):
            raise HTTPException(status_code=409, detail=f"该项目名称「{body.name}」在当前产品下已存在")
        raise HTTPException(status_code=500, detail="创建项目失败")
    return ApiResponse(success=True, message="项目已创建", data={"id": proj_id, "name": body.name})


@router.put("/projects/{proj_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def update_project(proj_id: str, body: UpdateProjectRequest, session: AsyncSession = Depends(get_session)):
    """更新项目名称、描述、版本号。版本号在同一产品内不可重复。"""
    result = await session.execute(select(Project).where(Project.id == proj_id))
    proj = result.scalar_one_or_none()
    if not proj:
        raise HTTPException(status_code=404, detail="项目不存在")

    new_name = body.name.strip() if body.name else None
    new_desc = body.description if body.description is not None else None
    new_version = body.version.strip() if body.version else None

    # 名称查重（同产品内不能与其他项目重名）
    if new_name is not None and new_name != proj.name:
        dup_name = await session.execute(
            select(Project).where(
                Project.product_id == proj.product_id,
                Project.name == new_name,
                Project.id != proj_id,
            )
        )
        if dup_name.scalars().first():
            raise HTTPException(status_code=409, detail=f"项目名称「{new_name}」在当前产品下已存在")

    # 版本号查重（同一产品内不可重复）
    if new_version is not None and new_version != proj.version:
        dup_version = await session.execute(
            select(Project).where(
                Project.product_id == proj.product_id,
                Project.version == new_version,
                Project.id != proj_id,
            )
        )
        if dup_version.scalars().first():
            raise HTTPException(
                status_code=409,
                detail=f"项目版本号「{new_version}」在当前产品下已存在，请使用其他版本号",
            )

    if new_name is not None:
        proj.name = new_name
    if new_desc is not None:
        proj.description = new_desc
    if new_version is not None:
        proj.version = new_version
    proj.updated_at = datetime.now(timezone.utc)

    try:
        await session.commit()
    except sa_exc.IntegrityError as e:
        await session.rollback()
        if _is_unique_violation(e, "uq_project_product_name"):
            raise HTTPException(status_code=409, detail=f"项目名称「{new_name}」在当前产品下已存在")
        if _is_unique_violation(e, "uq_project_product_version"):
            raise HTTPException(
                status_code=409,
                detail=f"项目版本号「{new_version}」在当前产品下已存在，请使用其他版本号",
            )
        raise HTTPException(status_code=500, detail="更新项目失败")
    return ApiResponse(
        success=True,
        message="项目已更新",
        data={"id": proj_id, "name": proj.name, "version": proj.version},
    )


async def _cascade_delete_requirements(session: AsyncSession, req_ids: list[str]) -> None:
    """应用层级联删除一批需求及其所有子表。

    这些子表对 requirements.id 的外键均为 RESTRICT（未设 ondelete=CASCADE），
    必须显式先删子表再删需求，否则触发外键冲突（ForeignKeyViolationError）。
    顺序：Traceability（同时引用 test_cases 与 requirements，须最先）→
    ErrorCase（引用 test_cases，须在 TestCase 之前）→ TestCase →
    FPDependency（引用 functional_points，须在 FunctionalPoint 之前）→
    FunctionalPoint → AnalysisTask / GenerationBatch / ReqDependency；
    Requirement 最后删除。

    注：FunctionalPoint/FPDependency 虽在 models.py 标注 ondelete=CASCADE，
    但部分旧库约束可能未生效（Base.metadata.create_all 不修改已存在表的外键），
    为保证删除一定能成功，这里统一显式删除，不依赖 DB 级 CASCADE。
    """
    if not req_ids:
        return
    # 收集这批需求关联的测试用例 ID，用于清理引用 test_cases 的子表
    tc_rows = await session.execute(select(TestCase.id).where(TestCase.req_id.in_(req_ids)))
    tc_ids = [r[0] for r in tc_rows.all()]

    # 收集这批需求关联的功能点 ID，用于清理 FPDependency
    fp_rows = await session.execute(
        select(FunctionalPoint.id).where(FunctionalPoint.requirement_id.in_(req_ids))
    )
    fp_ids = [r[0] for r in fp_rows.all()]

    # 1) 追溯矩阵：同时引用 test_cases 与 requirements，必须在 TestCase 之前删除
    trace_cond = Traceability.requirement_id.in_(req_ids)
    if tc_ids:
        trace_cond = trace_cond | Traceability.test_case_id.in_(tc_ids)
    await session.execute(delete(Traceability).where(trace_cond))
    # 2) 错误案例库：original_case_id 引用 test_cases.id（RESTRICT），必须在 TestCase 之前删除
    if tc_ids:
        await session.execute(delete(ErrorCase).where(ErrorCase.original_case_id.in_(tc_ids)))
    # 3) 测试用例
    await session.execute(delete(TestCase).where(TestCase.req_id.in_(req_ids)))
    # 4) 功能点依赖关系：引用 functional_points.id（RESTRICT/CASCADE），须在 FunctionalPoint 之前删除
    if fp_ids:
        await session.execute(
            delete(FPDependency).where(
                FPDependency.source_fp_id.in_(fp_ids) | FPDependency.target_fp_id.in_(fp_ids)
            )
        )
    # 5) 功能点本身
    if fp_ids:
        await session.execute(delete(FunctionalPoint).where(FunctionalPoint.id.in_(fp_ids)))
    # 6) 其余引用需求的子表
    await session.execute(delete(AnalysisTask).where(AnalysisTask.req_id.in_(req_ids)))
    await session.execute(delete(GenerationBatch).where(GenerationBatch.req_id.in_(req_ids)))
    await session.execute(
        delete(ReqDependency).where(
            ReqDependency.source_req_id.in_(req_ids) | ReqDependency.target_req_id.in_(req_ids)
        )
    )
    # 7) 需求本身（所有子表已清理完毕）
    await session.execute(delete(Requirement).where(Requirement.id.in_(req_ids)))


@router.delete("/projects/{proj_id}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def delete_project(proj_id: str, session: AsyncSession = Depends(get_session)):
    """删除项目及关联的需求、用例（级联），并清理向量库。

    所有引用 requirements.id 的外键均为 RESTRICT，故由 _cascade_delete_requirements
    显式按"先子表后父表"顺序清理，避免外键冲突。
    v2.45: 补充清理 FPDependencyVersion/FPDependencyVersionItem（旧库可能未生效 ondelete=CASCADE），
           并使用按 project_id 批量清理向量（含需求向量 + FP 向量 + 用例向量）。
    """
    # 先收该项目下所有需求 ID，用于级联删除与清理向量库
    req_result = await session.execute(
        select(Requirement.id).where(Requirement.project_id == proj_id)
    )
    req_ids = [r[0] for r in req_result.all()]

    # v4.1.9：scope 事件需要 product_id（失效 KB 缓存）与 fp_ids
    # （FP CRSR 向量的 metadata 没有 project_id，按 project 批量删删不掉）
    project_product_id = (
        await session.execute(select(Project.product_id).where(Project.id == proj_id))
    ).scalar_one_or_none()
    fp_ids: list[str] = []
    if req_ids:
        fp_rows = await session.execute(
            select(FunctionalPoint.id).where(FunctionalPoint.requirement_id.in_(req_ids))
        )
        fp_ids = [r[0] for r in fp_rows.all()]

    try:
        # 应用层级联删除需求及其所有子表（analysis_tasks / traceability 等）
        await _cascade_delete_requirements(session, req_ids)
        # v2.45: 显式清理版本快照表（旧库 ondelete=CASCADE 可能未生效）
        version_pks_subq = select(FPDependencyVersion.id).where(
            FPDependencyVersion.project_id == proj_id
        ).scalar_subquery()
        await session.execute(delete(FPDependencyVersionItem).where(
            FPDependencyVersionItem.version_pk.in_(version_pks_subq)
        ))
        await session.execute(delete(FPDependencyVersion).where(
            FPDependencyVersion.project_id == proj_id
        ))
        await session.execute(delete(Project).where(Project.id == proj_id))

        # v4.1.9：范围删除入箱（同事务），替代 commit 后的批量直删 + 逐条退化
        _enqueue_outbox(
            session,
            aggregate_type="scope",
            aggregate_id=f"project:{proj_id}",
            action="delete",
            revision=0,
            payload={
                "scope_type": "project",
                "scope_id": proj_id,
                "product_id": project_product_id or "",
                "fp_ids": fp_ids,
            },
            created_by="delete_project",
        )
        await session.commit()
    except sa_exc.IntegrityError as e:
        await session.rollback()
        logger.error("[API/products] 项目删除失败(外键冲突): proj_id={}, error={}", proj_id, str(e)[:200])
        raise HTTPException(500, f"删除失败（外键冲突）：{str(e.orig)[:200]}")
    except Exception as e:
        await session.rollback()
        logger.error("[API/products] 项目删除失败: proj_id={}, error={}", proj_id, str(e)[:200])
        raise HTTPException(500, f"删除失败：{str(e)[:200]}")

    # v4.1.9：向量清理已随业务删除同事务入箱（见上），此处不再直删。

    logger.info("[API/products] 项目已删除: id={}, 关联需求 {} 条已清理", proj_id, len(req_ids))
    return ApiResponse(success=True, message="已删除")


# ============================================================================
# v2.45: 项目归档 / 恢复活动（从需求级迁移到项目级）
# ============================================================================

class ProjectArchiveRequest(BaseModel):
    """项目归档请求模型。"""
    go_live_date: str = Field(..., description="上线时间 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM")
    change_summary: str = Field(default="", max_length=1024, description="变更说明（可选）")


async def _build_fp_relations_summary(fp_id: str, deps: list, fp_name_map: dict) -> str:
    """聚合单个 FP 的所有依赖描述，生成关系概括。"""
    parts = []
    for d in deps:
        if d.source_fp_id == fp_id:
            other_name = fp_name_map.get(d.target_fp_id, d.target_fp_id)
            parts.append(f"→ {other_name}（{d.relation_type}）: {d.description or ''}")
        elif d.target_fp_id == fp_id:
            other_name = fp_name_map.get(d.source_fp_id, d.source_fp_id)
            parts.append(f"← {other_name}（{d.relation_type}）: {d.description or ''}")
    return "\n".join(parts) if parts else ""


@router.post("/projects/{proj_id}/archive", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def archive_project(
    proj_id: str,
    body: ProjectArchiveRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """项目归档（二状态机：active → archived 直接流转）。

    规则：
    - 仅 active 状态可归档
    - 项目下所有需求必须为 confirmed 状态（防止未审核需求进入知识库）
    - 项目状态 active → archived
    - 项目下所有需求按 created_at 排序依次归档（archived + go_live_date + archived_at）
    - 所有 FP.status → archived，同步 summary/relations_summary
    - 生成依赖版本快照（fp_dependency_versions + items）
    - 当前 FPDependency.is_active → False（冻结为历史版本）
    - 写入向量库（metadata 新增 project_id）

    Args:
        proj_id: 项目 ID
        body: 含 go_live_date（上线时间）和 change_summary（变更说明）
    """
    # 1. 查询项目
    project = await session.get(Project, proj_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"项目 {proj_id} 不存在")
    # 仅活跃状态可归档（active → archived 直接流转）
    if project.status == "archived":
        raise HTTPException(status_code=409, detail="项目已归档，无需重复归档")
    if project.status != "active":
        raise HTTPException(
            status_code=409,
            detail=f"项目当前状态为「{project.status}」，仅活跃状态可归档"
        )

    # 2. 查询项目下所有需求，校验状态
    req_result = await session.execute(
        select(Requirement).where(Requirement.project_id == proj_id).order_by(Requirement.created_at)
    )
    requirements = req_result.scalars().all()
    if not requirements:
        raise HTTPException(status_code=400, detail="项目下无需求，无法归档")

    non_confirmed = [r for r in requirements if r.status not in ("confirmed", "active")]
    if non_confirmed:
        unconfirmed_titles = [r.title or r.id for r in non_confirmed[:5]]
        # v2.45 修复：错误信息与逻辑一致——代码允许 confirmed 和 active 两种状态归档
        raise HTTPException(
            status_code=409,
            detail=f"项目下有 {len(non_confirmed)} 个需求未确认（仅 confirmed 或 active 可归档）: {', '.join(unconfirmed_titles)}"
        )

    # 3. 解析上线时间
    try:
        parsed_go_live = datetime.fromisoformat(body.go_live_date)
    except ValueError:
        try:
            parsed_go_live = datetime.fromisoformat(body.go_live_date + "T00:00:00")
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"上线时间格式不正确: {body.go_live_date}，需为 YYYY-MM-DDTHH:MM 或 YYYY-MM-DD",
            )
    # v3.4.1 修复 M-6：naive datetime 赋给 DateTime(timezone=True) 列偏差 8 小时，
    # 与修改项目上线时间处的兜底保持一致（下游 :756/:820/:889 均复用该值）
    if parsed_go_live.tzinfo is None:
        parsed_go_live = parsed_go_live.replace(tzinfo=timezone.utc)

    # 4. 更新项目状态
    now = datetime.now(timezone.utc)
    project.status = "archived"
    project.go_live_date = parsed_go_live
    project.archived_at = now
    project.archived_by = current_user.username

    # 5. 收集所有 FP 和依赖（用于快照和 summary 同步）
    req_ids = [r.id for r in requirements]
    fp_result = await session.execute(
        select(FunctionalPoint).where(FunctionalPoint.requirement_id.in_(req_ids))
    )
    all_fps = fp_result.scalars().all()
    fp_ids = [f.id for f in all_fps]
    fp_name_map = {f.id: f.name for f in all_fps}

    dep_result = await session.execute(
        select(FPDependency).where(
            (FPDependency.source_fp_id.in_(fp_ids)) | (FPDependency.target_fp_id.in_(fp_ids))
        )
    )
    all_deps = dep_result.scalars().all()

    # 6. 同步 FP.summary 和 relations_summary，更新 FP.status
    for f in all_fps:
        # summary 从 srr_json.sub_srr.summary 同步
        if isinstance(f.srr_json, dict):
            sub_srr = f.srr_json.get("sub_srr", {})
            if isinstance(sub_srr, dict) and sub_srr.get("summary"):
                f.summary = sub_srr["summary"]
            elif f.srr_json.get("requirement", {}).get("summary"):
                f.summary = f.srr_json["requirement"]["summary"]
        # relations_summary 聚合依赖描述
        f.relations_summary = await _build_fp_relations_summary(f.id, all_deps, fp_name_map)
        if f.status != "archived":
            f.status = "archived"

    # 7. 生成版本号（同项目重新归档时后缀递增 -v2/-v3）
    # v2.45 修复：version_id 是全局唯一约束，需全局查重（非仅当前项目），
    # 避免不同产品下同名同版本号项目生成相同 version_id 导致 commit 时冲突。
    base_version_id = f"{project.version}-{project.name}"
    existing_versions = await session.execute(
        select(FPDependencyVersion.version_id).where(
            FPDependencyVersion.version_id.like(f"{base_version_id}%")
        )
    )
    existing_ids = {r[0] for r in existing_versions.all()}
    version_id = base_version_id
    suffix = 2
    while version_id in existing_ids:
        version_id = f"{base_version_id}-v{suffix}"
        suffix += 1

    # 8. 创建版本快照
    version = FPDependencyVersion(
        version_id=version_id,
        project_id=proj_id,
        project_version=project.version,
        project_name=project.name,
        go_live_date=parsed_go_live,
        snapshot_at=now,
        is_active=True,
        archived_by=current_user.username,
        requirement_count=len(requirements),
        fp_count=len(all_fps),
        dependency_count=len(all_deps),
        change_summary=body.change_summary,
    )
    session.add(version)
    await session.flush()  # 获取 version.id

    # 9. 快照所有依赖到 version_items
    req_title_map = {r.id: r.title or "" for r in requirements}
    fp_req_map = {f.id: f.requirement_id for f in all_fps}
    for d in all_deps:
        item = FPDependencyVersionItem(
            version_pk=version.id,
            source_fp_id=d.source_fp_id,
            source_fp_name=fp_name_map.get(d.source_fp_id, d.source_fp_id),
            target_fp_id=d.target_fp_id,
            target_fp_name=fp_name_map.get(d.target_fp_id, d.target_fp_id),
            relation_type=d.relation_type,
            description=d.description or "",
            review_status=d.review_status,
            source_req_id=fp_req_map.get(d.source_fp_id, ""),
            source_req_title=req_title_map.get(fp_req_map.get(d.source_fp_id, ""), ""),
            # v2.45 修复：补充 target 侧需求字段，支持跨需求双向追溯
            target_req_id=fp_req_map.get(d.target_fp_id, ""),
            target_req_title=req_title_map.get(fp_req_map.get(d.target_fp_id, ""), ""),
        )
        session.add(item)

    # 10. 保留依赖边生效（不冻结 is_active），仅关联最新版本快照用于追溯。
    # v3.1.3 修复：依赖边应跟随功能点状态——功能点随项目归档置 archived 后，
    #        其依赖关系客观存在，应继续参与影响分析与引用统计（is_active 保持生效）。
    #        原 v2.45 在此把 is_active 置 False「冻结」，导致已归档依赖边对影响分析
    #        BFS 与 fan_in(is_active==True 过滤)完全失效，出现「引用全 0、跨需求影响漏检」
    #        （如 REQ-1787730895940-3104：权限回收 -> 权限控制(跨需求) 边 approved 但 inactive）。
    #        版本追溯仍由 FPDependencyVersionItem 快照 + version_pk 关联保证，不依赖冻结。
    for d in all_deps:
        old_is_active = d.is_active
        old_version_pk = d.version_pk
        d.version_pk = version.id
        session.add(FPDependencyHistory(
            dependency_id=d.id,
            source_fp_id=d.source_fp_id,
            target_fp_id=d.target_fp_id,
            relation_type=d.relation_type,
            description=d.description or "",
            action="archived",
            old_review_status=d.review_status,
            new_review_status=d.review_status,
            old_is_active=old_is_active,
            new_is_active=d.is_active,
            old_version_pk=old_version_pk,
            new_version_pk=version.id,
            changed_by=current_user.username,
            change_reason=f"项目归档: {body.change_summary}" if body.change_summary else "项目归档",
        ))

    # 11. 更新所有需求状态
    for r in requirements:
        r.status = "archived"
        r.go_live_date = parsed_go_live
        r.archived_at = now

    # 11a. v4.1.9：向量写入意图**同事务入出箱**（替代 v2.8.58 的 celery 派发）。
    #
    # 旧链路：commit → send_task(Redis) → worker 异步 embed → 第二个 commit 回写状态。
    # 三个缺口：① 消息丢失（Redis 重启 / worker 崩溃且未重投）后需求停 pending；
    #          ② FP 向量**无任何对账**，丢一条就永久缺失且无人知晓；
    #          ③ 状态回写是独立 commit、失败只记 warning，向量与状态可能各说各话。
    # 新链路：业务行（archived）+ status=pending + 出箱事件**同一事务**提交——
    # 要么一起生效、要么一起回滚；relay 至少一次投递（Milvus 幂等 upsert），
    # 投递结果按 payload.status_writeback 回写状态（见 vector_outbox.status_writeback_req_id）。
    from app.core.vector_outbox import enqueue as _outbox_enqueue

    project_version = project.version
    kb_count = 0
    kb_fp_count = 0
    # v2.45 优化：按 requirement_id 分组 FP，避免嵌套循环 O(需求数 × FP数) → O(需求数 + FP数)
    fp_by_req: dict[str, list] = {}
    for f in all_fps:
        fp_by_req.setdefault(f.requirement_id, []).append(f)

    _rev = int(now.timestamp() * 1000)  # 变更时刻（epoch 毫秒），出箱 revision 语义
    for r in requirements:
        milvus_meta = {
            "type": "requirement",
            "product_id": r.product_id,
            "project_id": proj_id,  # v2.45: 新增 project_id，支持按项目过滤
            "modules": ",".join(r.modules or []),
            "priority": r.priority,
            "status": "archived",
            "go_live_date": parsed_go_live.isoformat(),
            "project_version": project_version,
        }
        r.vectorization_status = "pending"
        r.vectorization_error = None
        _outbox_enqueue(
            session,
            aggregate_type="requirement",
            aggregate_id=r.id,
            action="upsert",
            revision=_rev,
            payload={
                "requirement_json": r.srr_json or {},
                "metadata": milvus_meta,
                # 显式声明：此事件的投递结果回写 requirements.vectorization_status
                "status_writeback": {"req_id": r.id},
            },
            created_by="archive_project",
        )
        kb_count += 1

        # FP 向量（复用 requirement 类型 + "FP-" 前缀 ID，与孤儿扫描/reconcile 约定一致）
        for f in fp_by_req.get(r.id, []):
            fp_meta = {
                "type": "functional_point",
                "requirement_id": r.id,
                "project_id": proj_id,  # v2.45: 新增
                "fp_id": f.id or "",
                "fp_name": f.name or "",
                "modules": ",".join(f.modules or []) if f.modules else (f.module_id or ""),
                "module_id": f.module_id or "",
                "fp_type": f.fp_type,
                "product_id": r.product_id,
                "status": "archived",
            }
            embed_srr = dict(f.srr_json) if isinstance(f.srr_json, dict) else {}
            embed_srr.setdefault("requirement", {})
            embed_srr["requirement"]["title"] = f.name
            if f.summary:
                embed_srr["requirement"]["summary"] = f.summary
            # FP 行没有 vectorization_status 列，不挂 status_writeback
            _outbox_enqueue(
                session,
                aggregate_type="requirement",
                aggregate_id=f"FP-{f.id}",
                action="upsert",
                revision=_rev,
                payload={"requirement_json": embed_srr, "metadata": fp_meta},
                created_by="archive_project",
            )
            kb_fp_count += 1

    # 11b. v3.3（N1 治理 · 用例库与需求库口径对齐）：随项目归档**固化用例**。
    #
    # 背景：KB 问答的需求检索只取 `status="archived"` 的需求（"只信已归档"），
    # 但用例检索**没有任何归档过滤**，导致尚未定稿/仍在审核的用例也会被当作依据
    # （N1「答案依据 AI 生成用例」的直接原因之一）。
    #
    # 用例侧的落点：`test_cases.archived_at` 列**早已存在但从未写入过**
    # （实测 1704 条全为 NULL）。此处按与需求侧相同的口径固化：
    # 归档时把本项目下**已审核通过**的用例标记 archived_at，
    # 与第 1423 行「用 status=='approved' 的用例构建 functional_point_cases
    # 固化集」的口径一致。
    req_ids_for_cases = [r.id for r in requirements]
    if req_ids_for_cases:
        case_res = await session.execute(
            update(TestCase)
            .where(
                TestCase.req_id.in_(req_ids_for_cases),
                TestCase.status == "approved",
                TestCase.archived_at.is_(None),   # 幂等：重复归档不覆盖首次固化时间
            )
            .values(archived_at=now)
        )
        logger.info(
            "[ArchiveProject] 固化用例: 项目={}, 标记 archived_at 的 approved 用例 {} 条",
            proj_id, case_res.rowcount or 0,
        )

    try:
        await session.commit()
    except sa_exc.IntegrityError as e:
        await session.rollback()
        # v2.45: 使用辅助函数检测 version_id 唯一约束冲突（避免宽泛字符串匹配误判）
        if _is_unique_violation(e, "fp_dependency_versions_version_id_key"):
            raise HTTPException(
                status_code=409,
                detail=f"版本号 '{version_id}' 已存在（可能不同产品下有同名同版本号项目），请修改项目名称或版本后重试"
            )
        raise HTTPException(status_code=409, detail=f"归档失败（数据冲突）: {str(e)[:200]}")

    # 12. 向量写入已前移到第 11a 步（同事务入出箱，v4.1.9）。
    # 此处不再派发 celery、不再二次回写状态——状态与事件都随第 9 段的主事务提交。
    kb_failed: list[dict] = []  # 出箱路径下入箱失败会整体回滚（无逐条失败），保留字段兼容响应

    # v2.8.55 Bug A-2: 失效该产品的 KB 检索缓存，防止 24h 内 augmentation 使用归档前旧数据
    product_id_for_cache = requirements[0].product_id if requirements else ""
    if product_id_for_cache:
        try:
            from app.db.kb_cache import kb_cache
            await kb_cache.invalidate_by_product(product_id_for_cache)
        except Exception as cache_e:
            logger.warning("[ArchiveProject] KB 缓存失效失败（不阻断）: {}", str(cache_e)[:150])

    # v3.2: 固化各功能点的 CRSR（权威完整版 SRR）
    # 项目归档是版本定稿时刻，此处把同名功能点跨版本聚合固化到 functional_point_crsr，
    # 供依赖图谱节点点击 / 影响分析展开直读，并追加 version_refs 追溯链。
    # 失败不阻断归档主流程（CRSR 可随时重算重建），但记录失败列表供响应提示。
    crsr_failed: list[dict] = []
    crsr_count = 0
    if product_id_for_cache:
        try:
            from app.core.fp_crsr import consolidate_crsr
            fp_names = sorted({f.name for f in all_fps if f.name})
            for fp_name in fp_names:
                ok = await consolidate_crsr(
                    session, product_id_for_cache, fp_name, version_id=version_id,
                )
                if ok:
                    crsr_count += 1
                else:
                    crsr_failed.append({"fp_name": fp_name})
            await session.commit()
        except Exception as crsr_e:
            await session.rollback()
            logger.error("[ArchiveProject] CRSR 固化失败（不阻断）: {}", str(crsr_e)[:200])
            crsr_failed.append({"error": str(crsr_e)[:200]})

    # v2.48: 跨功能点共享业务规则/全局约束逐条编译（与 CRSR 对称）。
    # 背景：CFI 里这些"一条一个数值"的规则此前只作为"整块拼接文本"混进需求级向量，
    # 单条语义被稀释（实测相似度 0.49~0.53）→ 低于 KB_QA_MIN_SIMILARITY 0.50 被滤，
    # 问答答"未找到/未提及"（如"登录连续失败5次锁定30分钟""事件循环采样间隔100ms"）。
    # 此处用 reconcile 对账：按"本项目 + 产品下其他已归档需求"的规则并集重算，
    # 并删除已消失规则的过期条目。失败不阻断归档主流程（可重算重建）。
    shared_rule_count = 0
    shared_rule_deleted = 0
    shared_rule_failed: list[dict] = []
    if product_id_for_cache:
        try:
            from app.core.shared_rules import reconcile_shared_rules
            # 期望集合要覆盖产品下**所有**仍归档的需求（含本次归档的），
            # 否则会把其他项目的规则当过期删掉。
            _archived_rows = await session.execute(
                select(Requirement).where(
                    Requirement.product_id == product_id_for_cache,
                    Requirement.status == "archived",
                )
            )
            _archived_reqs = list(_archived_rows.scalars().all())
            _st = await reconcile_shared_rules(
                session,
                product_id=product_id_for_cache,
                archived_requirements=_archived_reqs,
                version_id=version_id,
            )
            shared_rule_count = _st.get("upserted", 0)
            shared_rule_deleted = _st.get("deleted", 0)
            if _st.get("failed"):
                shared_rule_failed.append({"failed": _st["failed"]})
            await session.commit()
        except Exception as sr_e:
            await session.rollback()
            logger.error("[ArchiveProject] 共享规则编译失败（不阻断）: {}", str(sr_e)[:200])
            shared_rule_failed.append({"error": str(sr_e)[:200]})

    # v3.4: 功能点最新版用例集固化（与 CRSR 对称）——各 FP 聚合最新线获批用例，
    # 供受影响功能点回归时直接取用（回归测试选择 RTS）。失败不阻断归档。
    fpc_count = 0
    fpc_failed: list[dict] = []
    if product_id_for_cache:
        try:
            from app.core.fp_cases import consolidate_fp_cases
            fp_records = sorted(
                (f for f in all_fps if f.name),
                key=lambda f: (f.name, f.id),
            )
            seen_fp_ids: set[str] = set()
            for fp in fp_records:
                if fp.id in seen_fp_ids:
                    continue
                seen_fp_ids.add(fp.id)
                ok = await consolidate_fp_cases(
                    session,
                    product_id_for_cache,
                    fp.name,
                    version_id=version_id,
                    fp_id=fp.id,
                )
                if ok:
                    fpc_count += 1
                else:
                    fpc_failed.append({"fp_name": fp.name})
            await session.commit()
        except Exception as fpc_e:
            await session.rollback()
            logger.error("[ArchiveProject] FP 用例集固化失败（不阻断）: {}", str(fpc_e)[:200])
            fpc_failed.append({"error": str(fpc_e)[:200]})

    logger.info(
        "[ArchiveProject] 项目归档成功: proj_id={}, version_id={}, 需求={}, FP={}, 依赖={}, 出箱事件={}(+FP {}), 失败={}, crsr={}, fpc={}",
        proj_id, version_id, len(requirements), len(all_fps), len(all_deps), kb_count, kb_fp_count,
        len(kb_failed), crsr_count, fpc_count,
    )
    # v4.1.9: 出箱路径——事件与业务行同事务提交，投递由 relay 后台完成（至少一次）
    msg = f"项目已归档，版本号: {version_id}"
    msg += f"（向量写入已入出箱：{kb_count} 条需求 + {kb_fp_count} 个功能点后台投递中）"
    return ApiResponse(
        success=True,
        message=msg,
        data={
            "project_id": proj_id,
            "status": "archived",
            "version_id": version_id,
            "requirement_count": len(requirements),
            "fp_count": len(all_fps),
            "dependency_count": len(all_deps),
            "kb_written": kb_count,
            "kb_fp_written": kb_fp_count,
            "kb_failed": kb_failed,  # 出箱路径下恒为空（入箱失败会整体回滚）；保留字段兼容响应
            "vectorization_mode": "outbox",  # v4.1.9: 取代 async/sync 二态
            "go_live_date": parsed_go_live.isoformat(),
            # v3.2: CRSR 固化结果
            "crsr_consolidated": crsr_count,
            "crsr_failed": crsr_failed,
            # v3.4: FP 最新用例集固化结果
            "fpc_consolidated": fpc_count,
            "fpc_failed": fpc_failed,
            # v2.48: 共享业务规则/全局约束逐条编译结果
            "shared_rule_consolidated": shared_rule_count,
            "shared_rule_deleted": shared_rule_deleted,
            "shared_rule_failed": shared_rule_failed,
            # v3.5.7（P1-C）：归档时点待投递的固化任务数——>0 表示知识固化仍在
            # 异步进行中，此窗口内 KB 问答可能 miss 最新归档内容（前端可提示）。
            "kb_pending_count": await _count_pending_outbox(session),
        },
    )


async def _count_pending_outbox(session) -> int:
    """v3.5.7（P1-C）：统计 vector_outbox 中待投递（pending）条数。

    归档 → 固化 → 出箱 → 异步 relay → Milvus 可检索存在时间窗口；
    pending 数即「尚未进入 Milvus 的固化任务数」，供调用方轮询等待。
    """
    from sqlalchemy import func as _func
    from app.db.models import VectorOutbox as _Outbox
    return int((await session.execute(
        select(_func.count()).select_from(_Outbox).where(_Outbox.status == "pending")
    )).scalar() or 0)


# ============================================================================
# 项目状态机——归档 / 恢复活动
# v2.47: 移除 pending_archive 中间态（前端从未使用、且与 archive 端点构成死锁），
#        状态机收敛为干净的两态：active ⇄ archived。
# ============================================================================

@router.post("/projects/{proj_id}/reactivate", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def reactivate_project(
    proj_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """项目恢复活动（archived → active）。

    规则：
    - 仅 archived 可恢复活动
    - 删除该项目所有需求向量 + FP 向量（按 project_id 批量）
    - 对应版本 fp_dependency_versions.is_active → False
    - FPDependency.is_active → True（当前边重新启用）
    - Requirement.status: archived → active
    - FP.status: archived → pending_review（v2.45：需重新审核，避免跳过审核流程）
    - 清空 go_live_date / archived_at
    """
    # 1. 查询项目
    project = await session.get(Project, proj_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"项目 {proj_id} 不存在")
    if project.status == "active":
        raise HTTPException(status_code=409, detail="项目已为活跃状态，无需恢复")
    if project.status != "archived":
        raise HTTPException(
            status_code=409,
            detail=f"项目状态为 {project.status}，仅 archived 可恢复活动"
        )

    # archived → active 的完整路径（保留 v2.45 原有逻辑）

    # 2. 查询项目下所有需求
    req_result = await session.execute(
        select(Requirement).where(Requirement.project_id == proj_id)
    )
    requirements = req_result.scalars().all()
    req_ids = [r.id for r in requirements]

    # 3. 查询所有 FP 和依赖
    fp_result = await session.execute(
        select(FunctionalPoint).where(FunctionalPoint.requirement_id.in_(req_ids))
    )
    all_fps = fp_result.scalars().all()

    dep_result = await session.execute(
        select(FPDependency).where(
            (FPDependency.source_fp_id.in_([f.id for f in all_fps]))
            | (FPDependency.target_fp_id.in_([f.id for f in all_fps]))
        )
    )
    all_deps = dep_result.scalars().all()

    # 4. 恢复状态
    project.status = "active"
    project.go_live_date = None
    project.archived_at = None
    project.archived_by = ""

    for r in requirements:
        r.status = "active"
        r.go_live_date = None
        r.archived_at = None

    # 4b. v3.3（与 11b 对称）：取消归档时**同步解除用例固化**。
    # 否则用例会一直停留在"已归档"状态，而所属需求已回到 active——
    # 出现"权威用例挂在不存在的版本上"的不一致。
    _req_ids_unarch = [r.id for r in requirements]
    if _req_ids_unarch:
        await session.execute(
            update(TestCase)
            .where(TestCase.req_id.in_(_req_ids_unarch))
            .values(archived_at=None)
        )

    for f in all_fps:
        # v2.45: 恢复为 pending_review（需重新审核），而非直接 approved（避免跳过审核流程）
        if f.status == "archived":
            f.status = "pending_review"

    for d in all_deps:
        # v2.45 修复：同步写入 FPDependencyHistory 审计记录，追踪解冻操作
        old_is_active = d.is_active
        old_version_pk = d.version_pk
        d.is_active = True
        d.version_pk = None
        session.add(FPDependencyHistory(
            dependency_id=d.id,
            source_fp_id=d.source_fp_id,
            target_fp_id=d.target_fp_id,
            relation_type=d.relation_type,
            description=d.description or "",
            action="modified",
            old_review_status=d.review_status,
            new_review_status=d.review_status,
            old_is_active=old_is_active,
            new_is_active=True,
            old_version_pk=old_version_pk,
            new_version_pk=None,
            changed_by=current_user.username,
            change_reason="项目恢复活动",
        ))

    # 5. 对应版本 is_active → False
    versions_result = await session.execute(
        select(FPDependencyVersion).where(
            FPDependencyVersion.project_id == proj_id,
            FPDependencyVersion.is_active == True,  # noqa: E712
        )
    )
    active_versions = versions_result.scalars().all()
    for v in active_versions:
        v.is_active = False

    await session.commit()

    # 6. 删除向量库（按 project_id 批量，含需求向量 + FP 向量 + 用例向量）
    # v2.45 修复：原循环单条删除性能差且遗漏用例向量，改用批量方法
    # v4.1.9 说明：这里**刻意保留同步直删**，没有改走 scope 出箱。原因是第 7 步
    # 的 shared_rule 对账依赖「本项目的向量已经删干净」这个前提——改成异步后，
    # 对账会在向量仍在时计算期望集合，把仍被其他项目引用的规则误判为过期。
    # 若要改造，必须把对账也移到 relay 之后，属于独立改动。
    try:
        from app.db.milvus_store import vector_store
        deleted_reqs = await asyncio.to_thread(vector_store.delete_requirement_by_project, proj_id)
        deleted_cases = await asyncio.to_thread(vector_store.delete_test_case_by_project, proj_id)
        logger.info(
            "[ReactivateProject] 向量库清理完成: proj_id={}, 需求/FP向量={}, 用例向量={}",
            proj_id, deleted_reqs, deleted_cases,
        )
    except Exception as e:
        logger.warning("[ReactivateProject] 批量向量清理失败，降级为单条清理: {}", str(e)[:200])
        # 退化策略：按 req_id 逐条清理
        for r in requirements:
            try:
                await asyncio.to_thread(vector_store.delete_requirement_from_kb, r.id)
            except Exception as inner_e:
                logger.warning("[ReactivateProject] 单条需求向量清理失败 req_id={}: {}", r.id, str(inner_e)[:100])
        for f in all_fps:
            try:
                await asyncio.to_thread(vector_store.delete_requirement_from_kb, f"FP-{f.id}")
            except Exception as inner_e:
                logger.warning("[ReactivateProject] 单条FP向量清理失败 fp_id={}: {}", f.id, str(inner_e)[:100])

    # 7. v2.48: 共享业务规则/全局约束向量自愈。
    # 上一步按 project_id 批量删向量会连带删掉本项目贡献的 shared_rule 条目——
    # 而一个规则可能被产品下多个项目引用（向量 ID 只按 product+规则文本），
    # 直接删会误伤其他仍归档的项目。这里按「产品下仍处于 archived 的需求」重算对账：
    #   - 其他归档项目仍在引用的规则 → 重新补齐；
    #   - 只由本项目引用的规则 → 保持删除（期望集合里已无它）。
    _reactivate_pid = getattr(project, "product_id", "") or ""
    if _reactivate_pid:
        try:
            from app.core.shared_rules import reconcile_shared_rules
            _rows = await session.execute(
                select(Requirement).where(
                    Requirement.product_id == _reactivate_pid,
                    Requirement.status == "archived",
                )
            )
            _still = list(_rows.scalars().all())
            _st = await reconcile_shared_rules(
                session, product_id=_reactivate_pid, archived_requirements=_still,
            )
            await session.commit()
            logger.info(
                "[ReactivateProject] 共享规则向量已对账: proj_id={}, 期望={}, upsert={}, 删除过期={}",
                proj_id, _st.get("desired"), _st.get("upserted"), _st.get("deleted"),
            )
        except Exception as sr_e:
            await session.rollback()
            logger.warning("[ReactivateProject] 共享规则对账失败（不阻断）: {}", str(sr_e)[:200])

    logger.info(
        "[ReactivateProject] 项目恢复活动: proj_id={}, 需求={}, FP={}, 依赖={}, 版本停用={}",
        proj_id, len(requirements), len(all_fps), len(all_deps), len(active_versions)
    )
    return ApiResponse(
        success=True,
        message="项目已恢复活动状态，向量库已清理",
        data={
            "project_id": proj_id,
            "status": "active",
            "requirement_count": len(requirements),
            "fp_count": len(all_fps),
            "versions_deactivated": len(active_versions),
        },
    )


# ============================================================================
# v3.2.13: 项目级「修改上线时间」+ 知识库元数据重整合
# ============================================================================

class ProjectGoLiveUpdateRequest(BaseModel):
    """项目级上线时间修改请求模型。"""
    go_live_date: str = Field(..., description="上线时间 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM")
    reason: str = Field(default="", max_length=512, description="变更原因（可选，写入审计日志）")


@router.patch("/projects/{proj_id}/go-live-date", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def update_project_go_live_date(
    proj_id: str,
    body: ProjectGoLiveUpdateRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """修改已归档项目的上线时间，并同步重建知识库派生数据（v3.2.13）。

    背景：上线时间在系统中承担三个职责——
      ① 知识库检索的时间衰减因子（越新排越前）
      ② 同 req_id 多版本去重时的版本判据（保留最新）
      ③ 归档时写入向量元数据的锚点

    因此上线时间变更不能只改 PG 字段，必须同步以下三层，否则检索结果
    与"一项目多时间"的自相矛盾：
      - Project.go_live_date（项目本体）
      - Requirement.go_live_date（项目下所有需求）
      - FPDependencyVersion.go_live_date（版本快照，保持可追溯一致）

    向量库侧（Milvus metadata）：
      - 批量更新该项目下所有需求向量 + FP 向量的 go_live_date
      - 失效知识库检索缓存（否则仍返回旧排序）
      - 不重算 embedding（日期在 metadata，正文未变）

    派生整合产物（v3.2.13b）：
      - functional_point_cases（功能点最新版用例集）的"最新版"口径为
        「项目 go_live_date 最大者」，日期变更可能改变归属 → 对受影响 FP 重新聚合
      - functional_point_crsr（权威完整版 SRR）按 req_version/created_at 排序，
        不依赖 go_live_date → 无需重算

    仅允许 archived 项目（活跃项目无归档上线时间语义）。

    Args:
        proj_id: 项目 ID
        body: 含 go_live_date 与可选 reason
    """
    # 1. 查询项目
    project = await session.get(Project, proj_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"项目 {proj_id} 不存在")
    if project.status != "archived":
        raise HTTPException(
            status_code=409,
            detail=f"仅已归档项目可修改上线时间（当前状态: {project.status}）。活跃项目的上线时间在归档时设置。",
        )

    # 2. 解析上线时间
    try:
        parsed_go_live = datetime.fromisoformat(body.go_live_date)
    except ValueError:
        try:
            parsed_go_live = datetime.fromisoformat(body.go_live_date + "T00:00:00")
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"上线时间格式不正确: {body.go_live_date}，需为 YYYY-MM-DDTHH:MM 或 YYYY-MM-DD",
            )
    if parsed_go_live.tzinfo is None:
        parsed_go_live = parsed_go_live.replace(tzinfo=timezone.utc)

    old_go_live = project.go_live_date
    # 无变化直接返回，避免无意义的向量重建
    if old_go_live and old_go_live == parsed_go_live:
        return ApiResponse(
            success=True,
            message="上线时间未变化，无需同步",
            data={
                "project_id": proj_id,
                "go_live_date": parsed_go_live.isoformat(),
                "changed": False,
                "requirement_count": 0,
                "milvus_updated": False,
            },
        )

    # 3. 事务内同步三层 PG 数据
    project.go_live_date = parsed_go_live

    req_result = await session.execute(
        select(Requirement).where(Requirement.project_id == proj_id)
    )
    requirements = req_result.scalars().all()
    for r in requirements:
        r.go_live_date = parsed_go_live

    ver_result = await session.execute(
        select(FPDependencyVersion).where(FPDependencyVersion.project_id == proj_id)
    )
    versions = ver_result.scalars().all()
    for v in versions:
        v.go_live_date = parsed_go_live

    fp_result = await session.execute(
        select(FunctionalPoint.id, FunctionalPoint.name).where(
            FunctionalPoint.requirement_id.in_([r.id for r in requirements])
        )
    )
    fp_rows = fp_result.fetchall()
    fp_ids = [row[0] for row in fp_rows]
    # v3.2.13b: 收集受影响的功能点名（用于重整合「最新版用例集」）
    affected_fp_names: set[str] = {row[1] for row in fp_rows if row[1]}

    # 补充：本项目 approved 用例中声明的 related_fp 名称（覆盖名称与 FP 表不完全一致的边缘情况）
    if requirements:
        tc_result = await session.execute(
            select(TestCase.case_json).where(
                TestCase.req_id.in_([r.id for r in requirements]),
                TestCase.status == "approved",
            )
        )
        for (cjson,) in tc_result.fetchall():
            if isinstance(cjson, dict):
                name = cjson.get("related_fp") or cjson.get("functional_point") or ""
                if name:
                    affected_fp_names.add(name)

    await session.commit()

    logger.info(
        "[UpdateProjectGoLive] 项目上线时间变更: proj_id={}, {} → {}, 需求={}, 版本快照={}, FP={}, user={}, reason={}",
        proj_id,
        old_go_live.isoformat() if old_go_live else "(空)",
        parsed_go_live.isoformat(),
        len(requirements), len(versions), len(fp_ids),
        current_user.username, body.reason or "(未填写)",
    )

    # 4. 知识库侧重整合：更新向量 metadata + 失效缓存
    milvus_updated = False
    milvus_error = ""
    milvus_count = 0
    cache_invalidated = 0
    try:
        from app.db.milvus_store import vector_store

        new_date_iso = parsed_go_live.isoformat()
        # 4.1 批量更新需求向量元数据（含兜底：单条失败不阻断整批）
        for r in requirements:
            try:
                await asyncio.to_thread(
                    vector_store.update_requirement_metadata,
                    r.id,
                    {"go_live_date": new_date_iso},
                )
                milvus_count += 1
            except Exception as inner_e:
                logger.warning(
                    "[UpdateProjectGoLive] 需求向量元数据更新失败(不阻断): req_id={}, error={}",
                    r.id, str(inner_e)[:100],
                )
        # 4.2 批量更新 FP 向量元数据
        for fp_id in fp_ids:
            try:
                await asyncio.to_thread(
                    vector_store.update_requirement_metadata,
                    f"FP-{fp_id}",
                    {"go_live_date": new_date_iso},
                )
                milvus_count += 1
            except Exception as inner_e:
                logger.warning(
                    "[UpdateProjectGoLive] FP 向量元数据更新失败(不阻断): fp_id={}, error={}",
                    fp_id, str(inner_e)[:100],
                )
        milvus_updated = True

        # 4.3 失效知识库检索缓存（按产品维度清除，确保新排序立即生效）
        try:
            from app.db.kb_cache import kb_cache
            cache_invalidated = await kb_cache.invalidate_by_product(project.product_id)
            logger.info(
                "[UpdateProjectGoLive] KB 缓存已失效: product_id={}, cleared={}",
                project.product_id, cache_invalidated,
            )
        except Exception as cache_e:
            logger.warning(
                "[UpdateProjectGoLive] KB 缓存失效失败(不阻断，缓存将在 TTL 后自然过期): error={}",
                str(cache_e)[:200],
            )
    except Exception as e:
        milvus_error = str(e)[:300]
        logger.warning(
            "[UpdateProjectGoLive] Milvus 元数据更新失败（不阻断，PG 已落库）: proj_id={}, error={}",
            proj_id, milvus_error[:200],
        )

    # 5. 重整合「功能点最新版用例集」（v3.2.13b）
    # functional_point_cases 判定「最新版」的口径是「项目 go_live_date 最大者」，
    # 上线时间变更可能改变归属，必须对受影响 FP 重新聚合，否则回归基线会指向旧版本。
    # 注：functional_point_crsr（权威完整版 SRR）按 req_version/created_at 排序，
    #     不依赖 go_live_date，无需重算。
    fpcase_refreshed = 0
    fpcase_failed: list[str] = []
    if affected_fp_names and project.product_id:
        try:
            from app.core.fp_cases import consolidate_fp_cases
            for fp_name in sorted(affected_fp_names):
                try:
                    # version_id 传 None：本次是"日期纠正"而非重新归档，不追加 version_refs 追溯链
                    ok = await consolidate_fp_cases(
                        session, project.product_id, fp_name, version_id=None,
                    )
                    if not ok:
                        fpcase_failed.append(fp_name)
                except Exception as inner_e:
                    fpcase_failed.append(fp_name)
                    logger.warning(
                        "[UpdateProjectGoLive] FP 用例集重整合失败(不阻断): fp={}, error={}",
                        fp_name, str(inner_e)[:150],
                    )
            await session.commit()

            # consolidate_fp_cases 对"无同名用例"也返回 True，故按实际落库记录数统计，
            # 避免把空操作计入"已重整合"而虚高。
            from app.db.models import FunctionalPointCases
            actual_rows = (await session.execute(
                select(func.count()).select_from(FunctionalPointCases).where(
                    FunctionalPointCases.product_id == project.product_id,
                    FunctionalPointCases.fp_name.in_(sorted(affected_fp_names)),
                )
            )).scalar() or 0
            fpcase_refreshed = int(actual_rows)
            logger.info(
                "[UpdateProjectGoLive] FP 用例集重整合: product={}, 受影响 FP={}, 实际落库={}, 失败={}",
                project.product_id, len(affected_fp_names), fpcase_refreshed, len(fpcase_failed),
            )
        except Exception as fpc_e:
            await session.rollback()
            logger.warning(
                "[UpdateProjectGoLive] FP 用例集重整合异常(不阻断): error={}", str(fpc_e)[:200],
            )

    return ApiResponse(
        success=True,
        message=(
            f"项目上线时间已更新，同步 {milvus_count} 条知识库向量元数据"
            + (f"，失效 {cache_invalidated} 条检索缓存" if cache_invalidated else "")
            + (f"，重整合 {fpcase_refreshed} 个功能点的最新用例集" if fpcase_refreshed else "")
            if milvus_updated
            else "项目上线时间已更新，但向量库元数据同步失败（可稍后重试）"
        ),
        data={
            "project_id": proj_id,
            "go_live_date": parsed_go_live.isoformat(),
            "changed": True,
            "old_go_live_date": old_go_live.isoformat() if old_go_live else "",
            "requirement_count": len(requirements),
            "version_count": len(versions),
            "fp_count": len(fp_ids),
            "milvus_updated": milvus_updated,
            "milvus_count": milvus_count,
            "milvus_error": milvus_error if not milvus_updated else "",
            "cache_invalidated": cache_invalidated,
            "fp_case_sets_refreshed": fpcase_refreshed,
            "fp_case_sets_failed": fpcase_failed,
        },
    )


# ============================================================================
# v2.45: 依赖版本查询端点
# ============================================================================

@router.get("/{pid}/fp/dependency-versions", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_dependency_versions_by_product(
    pid: str,
    session: AsyncSession = Depends(get_session),
):
    """查询产品下所有依赖版本快照列表。"""
    result = await session.execute(
        select(FPDependencyVersion, Project)
        .join(Project, FPDependencyVersion.project_id == Project.id)
        .where(Project.product_id == pid)
        .order_by(FPDependencyVersion.snapshot_at.desc())
    )
    rows = result.all()
    versions = []
    for v, p in rows:
        versions.append({
            "id": v.id,
            "version_id": v.version_id,
            "project_id": v.project_id,
            "project_name": v.project_name,
            "project_version": v.project_version,
            "go_live_date": v.go_live_date.isoformat() if v.go_live_date else "",
            "snapshot_at": v.snapshot_at.isoformat() if v.snapshot_at else "",
            "is_active": v.is_active,
            "archived_by": v.archived_by,
            "requirement_count": v.requirement_count,
            "fp_count": v.fp_count,
            "dependency_count": v.dependency_count,
            "change_summary": v.change_summary,
        })
    return ApiResponse(success=True, message="ok", data=versions)


@router.get("/projects/{proj_id}/fp/dependency-versions", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_dependency_versions_by_project(
    proj_id: str,
    session: AsyncSession = Depends(get_session),
):
    """查询项目的所有依赖版本历史。"""
    result = await session.execute(
        select(FPDependencyVersion)
        .where(FPDependencyVersion.project_id == proj_id)
        .order_by(FPDependencyVersion.snapshot_at.desc())
    )
    rows = result.scalars().all()
    versions = [{
        "id": v.id,
        "version_id": v.version_id,
        "project_id": v.project_id,
        "project_version": v.project_version,
        "project_name": v.project_name,
        "go_live_date": v.go_live_date.isoformat() if v.go_live_date else "",
        "snapshot_at": v.snapshot_at.isoformat() if v.snapshot_at else "",
        "is_active": v.is_active,
        "archived_by": v.archived_by,
        "requirement_count": v.requirement_count,
        "fp_count": v.fp_count,
        "dependency_count": v.dependency_count,
        "change_summary": v.change_summary,
    } for v in rows]
    return ApiResponse(success=True, message="ok", data=versions)


@router.get("/fp/dependency-versions/{version_pk}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_dependency_version_detail(
    version_pk: int,
    session: AsyncSession = Depends(get_session),
):
    """查询版本详情（含快照明细）。"""
    version = await session.get(FPDependencyVersion, version_pk)
    if not version:
        raise HTTPException(status_code=404, detail=f"版本 {version_pk} 不存在")

    items_result = await session.execute(
        select(FPDependencyVersionItem)
        .where(FPDependencyVersionItem.version_pk == version_pk)
        .order_by(FPDependencyVersionItem.source_fp_name)
    )
    items = items_result.scalars().all()

    return ApiResponse(success=True, message="ok", data={
        "id": version.id,
        "version_id": version.version_id,
        "project_id": version.project_id,
        "project_version": version.project_version,
        "project_name": version.project_name,
        "go_live_date": version.go_live_date.isoformat() if version.go_live_date else "",
        "snapshot_at": version.snapshot_at.isoformat() if version.snapshot_at else "",
        "is_active": version.is_active,
        "archived_by": version.archived_by,
        "requirement_count": version.requirement_count,
        "fp_count": version.fp_count,
        "dependency_count": version.dependency_count,
        "change_summary": version.change_summary,
        "items": [{
            "id": it.id,
            "source_fp_id": it.source_fp_id,
            "source_fp_name": it.source_fp_name,
            "target_fp_id": it.target_fp_id,
            "target_fp_name": it.target_fp_name,
            "relation_type": it.relation_type,
            "description": it.description,
            "review_status": it.review_status,
            "source_req_id": it.source_req_id,
            "source_req_title": it.source_req_title,
            # v2.45 修复：返回 target 侧需求字段，前端可显示跨需求追溯信息
            "target_req_id": it.target_req_id,
            "target_req_title": it.target_req_title,
        } for it in items],
    })


@router.get("/fp/dependency-versions/{v1}/diff/{v2}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def diff_dependency_versions(
    v1: int,
    v2: int,
    session: AsyncSession = Depends(get_session),
):
    """对比两个版本的依赖差异。"""
    ver1 = await session.get(FPDependencyVersion, v1)
    ver2 = await session.get(FPDependencyVersion, v2)
    if not ver1 or not ver2:
        raise HTTPException(status_code=404, detail="版本不存在")
    # v4.1.9：路由级守卫只解析了路径里的 v1，此处补 v2 的同产品校验，
    # 否则可见产品 A 的用户可以用 v1=A、v2=B 读取产品 B 的版本差异。
    _p1 = await product_id_of_project(session, ver1.project_id)
    _p2 = await product_id_of_project(session, ver2.project_id)
    if _p1 != _p2:
        raise HTTPException(status_code=404, detail="版本不存在")

    items1_result = await session.execute(
        select(FPDependencyVersionItem).where(FPDependencyVersionItem.version_pk == v1)
    )
    items2_result = await session.execute(
        select(FPDependencyVersionItem).where(FPDependencyVersionItem.version_pk == v2)
    )
    items1 = items1_result.scalars().all()
    items2 = items2_result.scalars().all()

    def _edge_key(it):
        # v2.45 修复：使用 FP ID 而非名称，避免跨需求 FP 重名导致对比错误
        return (it.source_fp_id, it.target_fp_id, it.relation_type)

    edges1 = {_edge_key(it): it for it in items1}
    edges2 = {_edge_key(it): it for it in items2}

    added = []
    removed = []
    modified = []
    for key, it2 in edges2.items():
        if key not in edges1:
            added.append({
                "source_fp_name": it2.source_fp_name,
                "target_fp_name": it2.target_fp_name,
                "relation_type": it2.relation_type,
                "description": it2.description,
            })
        else:
            it1 = edges1[key]
            if (it1.description or "") != (it2.description or "") or it1.review_status != it2.review_status:
                modified.append({
                    "source_fp_name": it2.source_fp_name,
                    "target_fp_name": it2.target_fp_name,
                    "relation_type": it2.relation_type,
                    "old_description": it1.description or "",
                    "new_description": it2.description or "",
                    "old_review_status": it1.review_status,
                    "new_review_status": it2.review_status,
                })
    for key, it1 in edges1.items():
        if key not in edges2:
            removed.append({
                "source_fp_name": it1.source_fp_name,
                "target_fp_name": it1.target_fp_name,
                "relation_type": it1.relation_type,
                "description": it1.description,
            })

    return ApiResponse(success=True, message="ok", data={
        "version_1": {"id": ver1.id, "version_id": ver1.version_id, "snapshot_at": ver1.snapshot_at.isoformat() if ver1.snapshot_at else ""},
        "version_2": {"id": ver2.id, "version_id": ver2.version_id, "snapshot_at": ver2.snapshot_at.isoformat() if ver2.snapshot_at else ""},
        "added": added,
        "removed": removed,
        "modified": modified,
        "summary": {
            "added_count": len(added),
            "removed_count": len(removed),
            "modified_count": len(modified),
            "unchanged_count": len(edges1) - len(removed) - len(modified),
        },
    })


# ============================================================================
# v2.45: 知识库按项目展示端点
# ============================================================================

@router.get("/{pid}/kb/projects", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_kb_projects(
    pid: str,
    session: AsyncSession = Depends(get_session),
):
    """知识库列表按项目展示（v2.45）。

    返回产品下所有已归档项目，点击项目后展示该项目的需求和用例向量列表。
    """
    result = await session.execute(
        select(Project).where(
            Project.product_id == pid,
            Project.status == "archived",
        ).order_by(Project.archived_at.desc())
    )
    projects = result.scalars().all()

    # 查询每个项目下的需求数量和用例数量（v2.8.56 修复：仅统计知识库内 status='archived' 的需求，
    # 避免从知识库删除后恢复 active 的需求仍被计入）
    proj_ids = [p.id for p in projects]
    req_counts: dict[str, int] = {}
    tc_counts: dict[str, int] = {}
    if proj_ids:
        req_result = await session.execute(
            select(Requirement.project_id, func.count(Requirement.id))
            .where(
                Requirement.project_id.in_(proj_ids),
                Requirement.status == "archived",
            )
            .group_by(Requirement.project_id)
        )
        req_counts = {row[0]: row[1] for row in req_result.all()}

        # 用例走物理删除（delete_kb_entry 直接 delete），但需求恢复 active 后
        # 其历史用例可能仍存在；为保持口径一致，只统计已归档需求下的用例
        tc_result = await session.execute(
            select(Requirement.project_id, func.count(TestCase.id))
            .join(TestCase, TestCase.req_id == Requirement.id)
            .where(
                Requirement.project_id.in_(proj_ids),
                Requirement.status == "archived",
            )
            .group_by(Requirement.project_id)
        )
        tc_counts = {row[0]: row[1] for row in tc_result.all()}

    data = [{
        "id": p.id,
        "name": p.name,
        "version": p.version,
        "description": p.description,
        "status": p.status,
        "go_live_date": p.go_live_date.isoformat() if p.go_live_date else "",
        "archived_at": p.archived_at.isoformat() if p.archived_at else "",
        # v2.45 修复：补充 archived_by 字段，与 list_projects 返回一致
        "archived_by": p.archived_by or "",
        "requirement_count": req_counts.get(p.id, 0),
        "test_case_count": tc_counts.get(p.id, 0),
    } for p in projects]

    return ApiResponse(success=True, message="ok", data=data)


@router.get("/projects/{proj_id}/kb/entries", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_kb_entries_by_project(
    proj_id: str,
    session: AsyncSession = Depends(get_session),
):
    """查看项目内的知识和用例向量列表（v2.45）。

    返回该项目下的需求列表和测试用例列表，用于知识库项目展开查看。
    """
    project = await session.get(Project, proj_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"项目 {proj_id} 不存在")

    # 查询需求（v2.8.56 修复：仅返回知识库内 status='archived' 的需求，
    # 避免从知识库删除后恢复 active 的需求仍展示在项目详情页）
    req_result = await session.execute(
        select(Requirement.id, Requirement.title, Requirement.status, Requirement.go_live_date, Requirement.priority)
        .where(
            Requirement.project_id == proj_id,
            Requirement.status == "archived",
        )
        .order_by(Requirement.created_at)
    )
    req_rows = req_result.all()
    req_ids = [row[0] for row in req_rows]

    # 查询用例
    tc_list = []
    if req_ids:
        tc_result = await session.execute(
            select(TestCase.id, TestCase.title, TestCase.req_id, TestCase.priority)
            .where(TestCase.req_id.in_(req_ids))
            .order_by(TestCase.created_at)
        )
        for row in tc_result.all():
            tc_list.append({
                "id": row[0],
                "title": row[1],
                "req_id": row[2],
                "priority": row[3] or "P2",
            })

    requirements = [{
        "id": row[0],
        "title": row[1] or "",
        "status": row[2] or "active",
        "go_live_date": row[3].isoformat() if row[3] else "",
        "priority": row[4] or "P2",
    } for row in req_rows]

    return ApiResponse(success=True, message="ok", data={
        "project": {
            "id": project.id,
            "name": project.name,
            "version": project.version,
            "status": project.status,
            "go_live_date": project.go_live_date.isoformat() if project.go_live_date else "",
            "archived_at": project.archived_at.isoformat() if project.archived_at else "",
        },
        "requirements": requirements,
        "test_cases": tc_list,
    })


# ============================================================================
# 3. 模块 CRUD
# ============================================================================

@router.get("/{pid}/modules", response_model=ApiResponse)
async def list_modules(pid: str, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Module).where(Module.product_id == pid).order_by(Module.created_at.desc())
    )
    rows = result.scalars().all()
    return ApiResponse(success=True, message="ok", data=[{
        "id": r.id, "product_id": r.product_id, "name": r.name,
        "description": r.description,
        "created_at": r.created_at.isoformat() if r.created_at else "",
    } for r in rows])


@router.post("/{pid}/modules", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def create_module(pid: str, body: CreateModuleRequest, session: AsyncSession = Depends(get_session)):
    """创建模块。模块 ID 自动生成。"""
    import time, random
    ts = int(time.time() * 1000) % 100000
    rand_suffix = random.randint(100, 999)
    mod_id = f"MOD-{pid.split('-')[-1]}-{ts:05d}-{rand_suffix}"

    record = Module(id=mod_id, product_id=pid, name=body.name, description=body.description)
    session.add(record)
    try:
        await session.commit()
    except sa_exc.IntegrityError as e:
        await session.rollback()
        if _is_unique_violation(e, "uq_module_product_name"):
            raise HTTPException(status_code=409, detail=f"该模块名称「{body.name}」在当前产品下已存在")
        raise HTTPException(status_code=500, detail="创建模块失败")
    return ApiResponse(success=True, message="模块已创建", data={"id": mod_id, "name": body.name})


@router.delete("/modules/{mod_id}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def delete_module(mod_id: str, session: AsyncSession = Depends(get_session)):
    """删除模块及关联的依赖边（级联）。

    PostgreSQL 原生支持外键约束（ModuleDependency 的 ondelete=CASCADE），
    但为确保依赖图谱完整性，仍显式删除指向该模块的依赖边。
    """
    await session.execute(
        delete(ModuleDependency).where(
            (ModuleDependency.source_module_id == mod_id)
            | (ModuleDependency.target_module_id == mod_id)
        )
    )
    await session.execute(delete(Module).where(Module.id == mod_id))
    await session.commit()
    logger.info("[API/products] 模块已删除: id={}", mod_id)
    return ApiResponse(success=True, message="已删除")


@router.put("/modules/{mod_id}/position", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def update_module_position(
    mod_id: str,
    body: dict,
    session: AsyncSession = Depends(get_session),
):
    """更新模块在图谱中的位置。"""
    stmt = select(Module).where(Module.id == mod_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(404, f"模块 {mod_id} 不存在")
    record.pos_x = body.get("x", record.pos_x)
    record.pos_y = body.get("y", record.pos_y)
    await session.commit()
    return ApiResponse(success=True, message="位置已更新")


@router.put("/modules/{mod_id}", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def update_module(
    mod_id: str,
    body: dict,
    session: AsyncSession = Depends(get_session),
):
    """更新模块名称和描述。"""
    stmt = select(Module).where(Module.id == mod_id)
    result = await session.execute(stmt)
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(404, f"模块 {mod_id} 不存在")
    if body.get("name"):
        record.name = body["name"]
    if "description" in body:
        record.description = body.get("description", "")
    await session.commit()
    return ApiResponse(success=True, message="模块已更新", data={"id": mod_id, "name": record.name})


# ============================================================================
# 4. 依赖图谱
# ============================================================================

@router.get("/dependency-graph", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_dependency_graph(
    product_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """获取依赖图谱数据和布局。

    返回 nodes（有依赖关系的模块）和 edges（依赖关系）。
    注意：只有参与依赖关系的模块才会出现在图谱中，新建的无关联模块不会自动显示。
    """
    # v4.1.9：product_id 是 query 参数，路由级守卫取不到，此处显式校验。
    # 不传时必须按可见集合过滤——原实现会返回**全部产品**的模块与依赖边。
    if product_id:
        await assert_product_visible(session, user, product_id)
    visible_ids = await get_visible_product_ids(session, user)

    nodes: list[dict] = []
    edges: list[dict] = []

    # 1. 查询该产品下的所有模块 ID
    mod_conditions: list = []
    if product_id:
        mod_conditions.append(Module.product_id == product_id)
    apply_visible_product_filter(mod_conditions, visible_ids, Module.product_id)

    mod_stmt = select(Module.id, Module.name, Module.product_id, Module.description, Module.pos_x, Module.pos_y)
    if mod_conditions:
        mod_stmt = mod_stmt.where(*mod_conditions)
    mod_result = await session.execute(mod_stmt)
    module_rows = mod_result.all()
    product_module_ids = {row[0] for row in module_rows}
    module_map = {row[0]: row for row in module_rows}

    if not product_module_ids:
        return ApiResponse(success=True, message="ok", data={"nodes": [], "edges": []})

    # 2. 查询所有依赖关系，只保留两端都属于该产品的边
    dep_result = await session.execute(select(ModuleDependency))
    all_deps = dep_result.scalars().all()
    linked_ids: set[str] = set()
    for d in all_deps:
        if d.source_module_id in product_module_ids and d.target_module_id in product_module_ids:
            linked_ids.add(d.source_module_id)
            linked_ids.add(d.target_module_id)
            edges.append({
                "source": d.source_module_id,
                "target": d.target_module_id,
                "type": d.relation_type,
                "strength": d.strength,
            })

    # 3. 返回所有产品模块（不仅是有依赖关系的）
    for mid in product_module_ids:
        row = module_map[mid]
        nodes.append({
            "id": row[0], "name": row[1], "type": "module",
            "product_id": row[2],
            "description": row[3] or "",
            "position": {"x": row[4], "y": row[5]},
        })

    return ApiResponse(success=True, message="ok", data={"nodes": nodes, "edges": edges})


@router.post("/dependency-graph/edges", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def add_dependency_edge(
    body: AddDependencyEdgeRequest,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """添加依赖连线。

    v2.2: 校验 source 和 target 属于同一产品，防止跨产品依赖污染影响分析。
    """
    source, target = body.source, body.target
    relation_type, strength = body.relation_type, body.strength
    if source == target:
        raise HTTPException(400, "不能添加自环依赖")
    if relation_type not in ("depends_on", "interdependent"):
        raise HTTPException(400, "relation_type 仅支持 depends_on（依赖）或 interdependent（相互依赖）")
    if strength not in ("strong", "medium", "weak"):
        raise HTTPException(400, "strength 仅支持 strong / medium / weak")

    # 查询 source 和 target 模块是否存在
    from app.db.models import Module as ModuleModel
    stmt = select(ModuleModel).where(ModuleModel.id.in_([source, target]))
    result = await session.execute(stmt)
    modules = {m.id: m for m in result.scalars().all()}

    source_mod = modules.get(source)
    target_mod = modules.get(target)

    if not source_mod:
        raise HTTPException(404, f"源模块 {source} 不存在")
    if not target_mod:
        raise HTTPException(404, f"目标模块 {target} 不存在")

    # v4.1.9：source/target 在 body 里，路由级守卫取不到，此处显式校验产品可见性
    await assert_product_visible(session, user, source_mod.product_id)

    # v2.2: 校验同一产品
    if source_mod.product_id != target_mod.product_id:
        raise HTTPException(
            400,
            f"模块 {source}（产品 {source_mod.product_id}）与 "
            f"{target}（产品 {target_mod.product_id}）不属于同一产品，不能创建跨产品依赖",
        )

    # 检查是否已存在相同依赖（防止重复添加）
    existing = await session.execute(
        select(ModuleDependency).where(
            ModuleDependency.source_module_id == source,
            ModuleDependency.target_module_id == target,
            ModuleDependency.relation_type == relation_type,
        )
    )
    if existing.scalars().first():
        raise HTTPException(409, "该依赖关系已存在")

    record = ModuleDependency(
        source_module_id=source,
        target_module_id=target,
        relation_type=relation_type,
        strength=strength,
    )
    session.add(record)
    await session.commit()
    return ApiResponse(success=True, message="依赖已添加")


@router.delete("/dependency-graph/edges", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def remove_dependency_edge(
    source: str,
    target: str,
    session: AsyncSession = Depends(get_session),
):
    """删除依赖连线。（删除所有匹配的记录，包括重复边）"""
    result = await session.execute(
        select(ModuleDependency).where(
            ModuleDependency.source_module_id == source,
            ModuleDependency.target_module_id == target,
        )
    )
    edges = result.scalars().all()
    if not edges:
        raise HTTPException(404, "依赖关系不存在")
    for edge in edges:
        await session.delete(edge)
    await session.commit()
    logger.info("[API/products] 依赖已删除: source={}, target={}, count={}", source, target, len(edges))
    return ApiResponse(success=True, message=f"已删除 {len(edges)} 条依赖")


# ============================================================================
# 4b. 功能点（FP）依赖图谱（跨需求）
# ============================================================================

@router.get("/{product_id}/fp/dependency-graph", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_fp_dependency_graph(
    product_id: str,
    module_id: str | None = Query(default=None, description="v2.3: 按模块筛选，仅返回该模块内 FP 及其依赖"),
    review_status: str | None = Query(default=None, description="v2.3: 按审核状态筛选 (pending/approved/rejected)"),
    include_inactive: bool = Query(default=True, description="v2.45: 是否包含项目归档后的历史冻结边（is_active=false）"),
    session: AsyncSession = Depends(get_session),
):
    """获取产品下所有功能点及其依赖关系（跨需求）。

    返回 nodes（功能点）和 edges（FP 依赖关系，含跨需求边）。
    只返回该产品下所有需求的功能点；边的两端必须都在该产品功能点集合内。

    v2.3 新增筛选参数：
    - module_id: 按模块筛选，仅返回该模块内 FP 及其内部依赖关系
    - review_status: 按审核状态筛选依赖边 (pending/approved/rejected)
    """
    # 1. 查询该产品下所有需求的 ID + title + status + version（用于同名合并节点判断最新版本）
    req_stmt = select(
        Requirement.id, Requirement.title, Requirement.status, Requirement.version,
        Requirement.created_at,
    ).where(
        Requirement.product_id == product_id
    )
    req_result = await session.execute(req_stmt)
    req_rows = req_result.all()
    req_id_set = {row[0] for row in req_rows}
    req_title_map = {row[0]: row[1] or "" for row in req_rows}
    req_status_map = {row[0]: row[2] or "active" for row in req_rows}
    req_version_map = {row[0]: int(row[3] or 1) for row in req_rows}
    req_created_map = {row[0]: row[4] for row in req_rows}

    if not req_id_set:
        return ApiResponse(success=True, message="ok", data={"nodes": [], "edges": [], "modules": []})

    # 2. 查询这些需求下的所有 FunctionalPoint
    # v2.3: 增加 module_id 字段查询，用于按模块筛选和返回模块信息
    # v2.45: 增加 summary / relations_summary 字段，用于节点点击查看详情
    fp_stmt = select(
        FunctionalPoint.id, FunctionalPoint.name, FunctionalPoint.fp_type,
        FunctionalPoint.requirement_id, FunctionalPoint.priority,
        FunctionalPoint.module_id, FunctionalPoint.modules,
        FunctionalPoint.summary, FunctionalPoint.relations_summary,
        FunctionalPoint.status,
    ).where(FunctionalPoint.requirement_id.in_(req_id_set))

    # v2.3: 按模块筛选
    if module_id:
        fp_stmt = fp_stmt.where(
            (FunctionalPoint.module_id == module_id)
            | FunctionalPoint.modules.contains([module_id])
        )

    fp_result = await session.execute(fp_stmt)
    fp_rows = fp_result.all()

    if not fp_rows:
        return ApiResponse(success=True, message="ok", data={"nodes": [], "edges": [], "modules": []})

    fp_id_set = {row[0] for row in fp_rows}

    # v2.3: 收集所有模块 ID（用于返回 modules 列表给前端筛选器）
    all_module_ids: set[str] = set()
    nodes = []
    for row in fp_rows:
        mod_id = row[5]
        mods = row[6] or []
        if mod_id:
            all_module_ids.add(mod_id)
        for m in mods:
            all_module_ids.add(m)
        nodes.append({
            "id": row[0],
            "name": row[1],
            "fp_type": row[2],
            "req_id": row[3],
            "req_title": req_title_map.get(row[3], ""),
            "req_status": req_status_map.get(row[3], "active"),
            "req_version": req_version_map.get(row[3], 1),
            "req_created_at": req_created_map.get(row[3]).isoformat() if req_created_map.get(row[3]) else "",
            "priority": row[4] or "P2",
            "module_id": mod_id,
            "modules": mods,
            # v2.45: 新增字段，用于节点点击查看详情
            "summary": row[7] or "",
            "relations_summary": row[8] or "",
            "fp_status": row[9] or "pending_review",
        })

    # 查询模块名称映射
    module_options: list[dict] = []
    if all_module_ids:
        from app.db.models import Module as ModuleModel
        mod_result = await session.execute(
            select(ModuleModel.id, ModuleModel.name).where(ModuleModel.id.in_(all_module_ids))
        )
        module_options = [{"id": row[0], "name": row[1]} for row in mod_result.fetchall()]

    # 3. 查询所有 FPDependency（source 或 target 在该产品 FP 集合内）
    # v2.3: 返回 review_status 字段，前端按审核状态区分颜色
    # v2.3: 支持 review_status 筛选
    # v2.45: 默认仅返回 is_active=True 的边（项目归档后的历史冻结边不展示）
    #         通过 include_inactive=true 可查看全部历史边
    dep_stmt = select(FPDependency).where(
        FPDependency.source_fp_id.in_(fp_id_set),
        FPDependency.target_fp_id.in_(fp_id_set),
    )
    if not include_inactive:
        dep_stmt = dep_stmt.where(FPDependency.is_active == True)  # noqa: E712
    if review_status:
        dep_stmt = dep_stmt.where(FPDependency.review_status == review_status)

    dep_result = await session.execute(dep_stmt)
    deps = dep_result.scalars().all()

    edges = [
        {
            "id": d.id,
            "source": d.source_fp_id,
            "target": d.target_fp_id,
            "relation_type": d.relation_type,
            "description": d.description or "",
            "review_status": d.review_status,
            "reviewed_by": d.reviewed_by or "",
            "reviewed_at": d.reviewed_at.isoformat() if d.reviewed_at else "",
            "created_by": d.created_by or "",
            "created_at": d.created_at.isoformat() if d.created_at else "",
            # v2.45: 暴露 is_active 字段，前端可区分活跃边和历史冻结边
            "is_active": d.is_active,
            "version_pk": d.version_pk,
        }
        for d in deps
    ]

    return ApiResponse(
        success=True,
        message="ok",
        data={"nodes": nodes, "edges": edges, "modules": module_options},
    )


# ============================================================================
# v1.5.1: FP 级影响分析（基于 FPDependency BFS 遍历）
# ============================================================================

@router.get("/{product_id}/fp/impact", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_fp_impact(
    product_id: str,
    fp_id: str = Query(..., description="目标功能点 ID"),
    max_depth: int = Query(3, ge=1, le=10, description="BFS 最大遍历深度"),
    session: AsyncSession = Depends(get_session),
):
    """基于功能点依赖关系做 BFS 影响分析。

    从目标 FP 出发，沿着 depends_on/data_flows/lifecycle_scenarios 边反向 BFS，
    找出所有受影响的功能点和模块。
    """
    # 1. 校验目标 FP 存在
    fp = await session.get(FunctionalPoint, fp_id)
    if not fp:
        raise HTTPException(status_code=404, detail="功能点不存在")

    # 2. 加载该产品下所有 FP 的依赖
    req_ids = await session.execute(
        select(Requirement.id).where(Requirement.product_id == product_id)
    )
    req_id_set = {r[0] for r in req_ids.fetchall()}
    if not req_id_set:
        return ApiResponse(success=True, message="该产品下无需求", data={"fp_id": fp_id, "affected": [], "total": 0})

    fp_ids_result = await session.execute(
        select(FunctionalPoint.id, FunctionalPoint.name, FunctionalPoint.module_id, FunctionalPoint.requirement_id).where(
            FunctionalPoint.requirement_id.in_(req_id_set)
        )
    )
    fp_info = {r[0]: {"name": r[1], "module_id": r[2], "req_id": r[3]} for r in fp_ids_result.fetchall()}
    fp_id_set = set(fp_info.keys())

    dep_result = await session.execute(
        select(FPDependency).where(
            FPDependency.review_status == "approved",  # v2.3: 仅 approved 依赖参与影响分析
            FPDependency.is_active == True,  # v2.45: 排除项目归档后的历史版本依赖
            FPDependency.source_fp_id.in_(fp_id_set) | FPDependency.target_fp_id.in_(fp_id_set)
        )
    )
    deps = dep_result.scalars().all()

    # 构建邻接表
    # depends_on: target 是被依赖方 → 修改 target 影响 source（反向传播）
    # data_flows: source 是来源 → 修改 source 影响 target（正向传播）
    # lifecycle_scenarios / conflicts_with / invariants: 双向关系，两个方向都传播
    adj: dict[str, set[str]] = {}
    for d in deps:
        if d.relation_type == "depends_on":
            adj.setdefault(d.target_fp_id, set()).add(d.source_fp_id)
        elif d.relation_type == "data_flows":
            adj.setdefault(d.source_fp_id, set()).add(d.target_fp_id)
        elif d.relation_type in ("lifecycle_scenarios", "conflicts_with", "invariants"):
            # v2.8.55 Bug B-3: 双向关系——两个方向都传播影响
            adj.setdefault(d.source_fp_id, set()).add(d.target_fp_id)
            adj.setdefault(d.target_fp_id, set()).add(d.source_fp_id)

    # 3. BFS 遍历
    from collections import deque
    visited: dict[str, int] = {fp_id: 0}  # fp_id → depth
    queue = deque([fp_id])
    while queue:
        cur = queue.popleft()
        cur_depth = visited[cur]
        if cur_depth >= max_depth:
            continue
        for neighbor in adj.get(cur, set()):
            if neighbor not in visited and neighbor in fp_id_set:
                visited[neighbor] = cur_depth + 1
                queue.append(neighbor)

    # 4. 构建结果
    affected_fps = [
        {
            "fp_id": fid,
            "name": fp_info.get(fid, {}).get("name", fid),
            "module_id": fp_info.get(fid, {}).get("module_id"),
            "depth": depth,
        }
        for fid, depth in visited.items()
        if fid != fp_id  # 排除自身
    ]
    affected_fps.sort(key=lambda x: x["depth"])

    # 聚合受影响模块
    affected_modules: dict[str, list[str]] = {}
    for afp in affected_fps:
        mod = afp["module_id"] or "_unassigned"
        if mod not in affected_modules:
            affected_modules[mod] = []
        affected_modules[mod].append(afp["name"])

    # 5. 生成影响摘要（供用例生成使用）
    source_name = fp_info.get(fp_id, {}).get("name", fp.name)
    impact_summary = f"功能点「{source_name}」变更，将影响 {len(affected_fps)} 个关联功能点："
    for afp in affected_fps[:5]:
        impact_summary += f"\n- [{afp['depth']} 级] {afp['name']}"
    if len(affected_fps) > 5:
        impact_summary += f"\n... 及其他 {len(affected_fps) - 5} 个功能点"

    return ApiResponse(success=True, message="ok", data={
        "source_fp_id": fp_id,
        "source_fp_name": source_name,
        "affected": affected_fps,
        "affected_modules": [
            {"module_id": mod, "fp_names": names}
            for mod, names in affected_modules.items()
        ],
        "total": len(affected_fps),
        "max_depth_reached": max(visited.values()) if visited else 0,
        "impact_summary": impact_summary,
    })


# ============================================================================
# v3.2: 功能点权威完整版 SRR（CRSR）— 依赖图谱节点点击 / 影响分析展开共用
# ============================================================================

@router.get("/fp-crsr/{fp_id}", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_fp_crsr(
    fp_id: str,
    session: AsyncSession = Depends(get_session),
):
    """获取功能点跨版本聚合的权威完整版 SRR（最新最完整内容）。

    返回：
        - crsr: 聚合后整体 SRR（标量取最新、数组去重并集、冲突以最新为主）
        - versions: 各版本归属（最新在前，含所属需求/版本号/归档态）
        - version_count: 版本数
        - stored_version_refs: 归档固化时的版本追溯链（仅已固化时有值）
    """
    from app.core.fp_crsr import get_crsr_view

    try:
        view = await get_crsr_view(session, fp_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("[API/products] CRSR 查询失败 fp_id={}: {}", fp_id, str(e)[:200])
        raise HTTPException(status_code=500, detail=f"CRSR 查询失败: {str(e)[:200]}")

    return ApiResponse(success=True, message="ok", data=view)

# ============================================================================
# 5. 影响分析（面向模块的 BFS 遍历）
# ============================================================================

@router.get("/projects/{proj_id}/impact", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def analyze_project_impact(
    proj_id: str,
    module_ids: str = "",
    session: AsyncSession = Depends(get_session),
):
    """分析模块变更影响范围（BFS 遍历依赖图）。

    输入：proj_id（项目 ID）+ module_ids（逗号分隔的模块 ID 列表）
    输出：直接影响模块 / 间接影响模块 / 受影响需求 / 回归用例
    """
    from collections import deque

    target_modules = [m.strip() for m in module_ids.split(",") if m.strip()]
    if not target_modules:
        raise HTTPException(400, "请至少指定一个模块（module_ids）")

    logger.info("[API/impact] 分析项目影响: proj_id={}, modules={}", proj_id, target_modules)

    # 查询所有边
    dep_result = await session.execute(
        select(ModuleDependency)
    )
    edges = dep_result.scalars().all()

    # 构建邻接表
    in_edges: dict[str, list] = {}
    out_edges: dict[str, list] = {}
    for e in edges:
        in_edges.setdefault(e.target_module_id, []).append((e.source_module_id, e.relation_type))
        out_edges.setdefault(e.source_module_id, []).append((e.target_module_id, e.relation_type))

    # BFS 遍历（含环检测）
    visited = set(target_modules)
    queue: deque = deque((m, 0) for m in target_modules)
    indirect: list[dict] = []
    max_depth = 3

    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue

        # 反向边：依赖当前模块的其他模块（当前模块变更会影响它们）
        for neighbor, rtype in in_edges.get(current, []):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, depth + 1))
                if neighbor not in target_modules:
                    indirect.append({
                        "module_id": neighbor,
                        "reason": f"反向依赖 ({rtype})",
                        "depth": depth + 1,
                    })

        # 正向边：仅相互依赖（interdependent）需要正向传播。
        # depends_on 是单向依赖（A depends_on B 时 B 变更影响 A，但 A 变更不影响 B）；
        # interdependent 双向影响，A 变更会正向波及 B。
        # 注：原代码匹配 related_to，但创建接口只允许 depends_on/interdependent（见 add_dependency_edge），
        # related_to 边无法经 API 创建，属死代码，已改为 interdependent。
        for neighbor, rtype in out_edges.get(current, []):
            if rtype == "interdependent" and neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, depth + 1))
                if neighbor not in target_modules:
                    indirect.append({
                        "module_id": neighbor,
                        "reason": f"相互依赖 ({rtype})",
                        "depth": depth + 1,
                    })

    # 查找受影响的需求和用例（v2.4: 修复 pid 未定义 NameError；改用 modules[] 在 Python 层匹配）
    all_modules = list(visited)
    affected_reqs: list[dict] = []
    # v2.8.69: 前端类型 ProjectImpact 分 direct_requirements / regression_requirements
    #   - direct_requirements: 项目自身需求中命中受影响模块的（直接受影响）
    #   - regression_requirements: 其他产品内项目命中的（跨项目回归）
    direct_reqs: list[dict] = []
    regression_reqs: list[dict] = []

    # v2.8.69: 加载模块名称映射（模块信息为空的根因——原返回只给 module_id）
    mod_name_lookup: dict[str, str] = {}
    all_mod_ids = set(all_modules) | set(target_modules) | {m.get("module_id", "") for m in indirect if m.get("module_id")}
    if all_mod_ids:
        all_mod_ids.discard("")
        mod_q = await session.execute(
            select(Module.id, Module.name).where(Module.id.in_(list(all_mod_ids)))
        )
        for mid, mname in mod_q.all():
            mod_name_lookup[mid] = mname or mid

    if all_modules:
        # 通过 proj_id 查询所属 product_id，用于隔离查询范围（原代码误用未定义的 pid 导致 NameError）
        proj_result = await session.execute(
            select(Project.id, Project.name, Project.version, Project.product_id).where(Project.id == proj_id)
        )
        proj_row = proj_result.first()
        project_info: dict = {"id": proj_id, "name": "", "version": "1.0", "product_id": ""}
        product_id = ""
        if proj_row:
            project_info["id"] = proj_row[0] or proj_id
            project_info["name"] = proj_row[1] or ""
            project_info["version"] = proj_row[2] or "1.0"
            product_id = proj_row[3] or ""
        if product_id:
            # 仅按 product_id 隔离；module 字段（单值，已废弃 v2.2，仅存首模块）不再作为 DB 过滤条件，
            # 改在 Python 层用 modules[] 数组匹配，避免漏掉多模块关联的需求
            req_result = await session.execute(
                select(Requirement.id, Requirement.title, Requirement.modules, Requirement.project_id).where(
                    Requirement.product_id == product_id,
                ).order_by(Requirement.created_at.desc())
            )
            for row in req_result.all():
                rid, rtitle, rmodules, rproj_id = row[0], row[1], row[2], row[3]
                req_mods = set(rmodules or [])
                matched = list(req_mods & set(all_modules))
                if not matched:
                    continue
                entry_direct: dict = {
                    "id": rid,
                    "title": rtitle or "",
                    "modules": list(rmodules or []),
                }
                entry_regression: dict = {
                    "id": rid,
                    "title": rtitle or "",
                    "matched_modules": matched,
                }
                affected_reqs.append({
                    "id": rid, "title": rtitle or "", "matched_modules": matched,
                })
                # 区分: 项目自身需求 → direct_reqs；跨项目需求 → regression_reqs
                if rproj_id and rproj_id == proj_id:
                    direct_reqs.append(entry_direct)
                else:
                    regression_reqs.append(entry_regression)

    # v2.8.69: 与前端 ProjectImpact 类型对齐
    #   direct_modules: {id, name}[] — 前端展示模块名，不能只有字符串 mid
    #   indirect_modules: {module_id, name, relation_type, depth}[]
    #   project / direct_requirements / regression_requirements 全部补齐
    direct_modules_out: list[dict] = [
        {"id": mid, "name": mod_name_lookup.get(mid, mid)}
        for mid in target_modules
    ]
    indirect_modules_out: list[dict] = [
        {
            "module_id": m.get("module_id", ""),
            "name": mod_name_lookup.get(m.get("module_id", ""), m.get("module_id", "")),
            "relation_type": m.get("reason", ""),
            "depth": m.get("depth", 1),
        }
        for m in indirect
    ]

    return ApiResponse(success=True, message="ok", data={
        "project": {
            "id": project_info.get("id", proj_id),
            "name": project_info.get("name", ""),
            "version": project_info.get("version", "1.0"),
        },
        "direct_modules": direct_modules_out,
        "indirect_modules": indirect_modules_out,
        "all_affected_modules": [
            {"id": mid, "name": mod_name_lookup.get(mid, mid)} for mid in all_modules
        ],
        "direct_requirements": direct_reqs,
        "regression_requirements": regression_reqs,
        "affected_requirements": affected_reqs,
        "affected_requirement_count": len(affected_reqs),
    })


# ============================================================================
# 管理后台首页（v2.12）
# ============================================================================

@router.get("/admin/dashboard", response_model=ApiResponse)
async def admin_dashboard(
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """管理后台首页：当前用户可访问的产品列表 + 统计。"""
    from app.core.auth import CurrentUser
    result = await session.execute(select(Product).order_by(Product.name))
    products = result.scalars().all()

    is_admin = user.role == "admin"
    accessible = []
    for p in products:
        vids = p.visible_to_user_ids or []
        if is_admin or not vids or user.user_id in vids:
            # 统计产品下的项目数、需求数
            proj_count = await session.execute(
                select(Project).where(Project.product_id == p.id)
            )
            req_count = 0
            for proj in proj_count.scalars().all():
                r = await session.execute(select(Requirement).where(Requirement.project_id == proj.id))
                req_count += len(r.scalars().all())
            accessible.append({
                "id": p.id, "name": p.name, "description": p.description,
                "project_count": 0, "requirement_count": req_count,
            })

    # 用户总数
    user_count = await session.execute(select(User))
    user_total = len(user_count.scalars().all())

    return ApiResponse(success=True, message="ok", data={
        "products": accessible,
        "total_products": len(products),
        "total_users": user_total,
    })
