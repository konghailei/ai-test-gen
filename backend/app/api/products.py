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

from fastapi import APIRouter, Depends, HTTPException, Query
from app.core.auth import require_admin, require_write, get_current_user
from pydantic import BaseModel, Field
from sqlalchemy import select, delete, exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.db.models import (
    Product, Project, Module, Requirement, ModuleDependency, TestCase, User,
    FunctionalPoint, FPDependency, AnalysisTask, GenerationBatch, ReqDependency,
    Traceability,
)
from app.models.api_schemas import ApiResponse
from app.utils.logger import logger

router = APIRouter()


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

@router.get("/products", response_model=ApiResponse)
async def list_products(
    session: AsyncSession = Depends(get_session),
    user: "CurrentUser" = Depends(get_current_user),
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


@router.post("/products", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def create_product(body: CreateProductRequest, session: AsyncSession = Depends(get_session)):
    """创建产品。产品 ID 自动生成（时间戳后缀 + 随机数防冲突）。"""
    if body.id:
        pid = body.id
    else:
        # 使用时间戳 + 随机后缀，避免全局计数器重启后重复
        ts = int(time.time() * 1000) % 100000
        rand_suffix = random.randint(100, 999)
        pid = f"PROD-{ts:05d}-{rand_suffix}"

    record = Product(id=pid, name=body.name, description=body.description)
    session.add(record)
    try:
        await session.commit()
    except sa_exc.IntegrityError as e:
        await session.rollback()
        if _is_unique_violation(e, "uq_product_name"):
            raise HTTPException(status_code=409, detail=f"产品名称「{body.name}」已存在，请使用其他名称")
        raise HTTPException(status_code=500, detail="创建产品失败")
    logger.info("[API/products] 产品已创建: id={}, name={}", pid, body.name)
    return ApiResponse(success=True, message="产品已创建", data={"id": pid, "name": body.name})


@router.put("/products/{pid}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
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
    return ApiResponse(success=True, message="已更新")


@router.delete("/products/{pid}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def delete_product(pid: str, session: AsyncSession = Depends(get_session)):
    """删除产品及关联的项目、模块（级联）。"""
    # 查该产品下所有需求，用于级联删除与清理向量库
    req_result = await session.execute(
        select(Requirement.id).where(Requirement.product_id == pid)
    )
    req_ids = [row[0] for row in req_result.all()]

    # 应用层级联删除关联数据（先子表后父表，避免外键冲突）
    # 必须先删引用 Project/Requirement 的子表，最后再删 Project 与 Product 本身
    await session.execute(delete(Module).where(Module.product_id == pid))
    await _cascade_delete_requirements(session, req_ids)
    await session.execute(delete(Project).where(Project.product_id == pid))
    await session.execute(delete(Product).where(Product.id == pid))
    await session.commit()

    # 同步删除向量库中该产品所有需求的记录
    for req_id in req_ids:
        try:
            from app.db.milvus_store import vector_store
            await asyncio.to_thread(vector_store.delete_requirement_from_kb, req_id)
        except Exception as e:
            logger.warning("[API/products] 向量库删除失败(不阻断): req_id={}, error={}", req_id, str(e)[:100])

    logger.info("[API/products] 产品已删除: id={}, 关联需求 {} 条已清理", pid, len(req_ids))
    return ApiResponse(success=True, message="已删除")


# ============================================================================
# 2. 项目 CRUD
# ============================================================================

@router.get("/products/{pid}/projects", response_model=ApiResponse)
async def list_projects(pid: str, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Project).where(Project.product_id == pid).order_by(Project.created_at.desc())
    )
    rows = result.scalars().all()
    return ApiResponse(success=True, message="ok", data=[{
        "id": r.id, "product_id": r.product_id, "name": r.name,
        "description": r.description, "version": r.version or "1.0",
        "created_at": r.created_at.isoformat() if r.created_at else "",
    } for r in rows])


@router.post("/products/{pid}/projects", response_model=ApiResponse, dependencies=[Depends(require_write)])
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
    顺序：Traceability（同时引用 test_cases 与 requirements，须最先）→ TestCase →
    AnalysisTask / GenerationBatch / ReqDependency；Requirement 最后删除，
    FunctionalPoint 由 DB ondelete=CASCADE 自动级联。
    """
    if not req_ids:
        return
    # 收集这批需求关联的测试用例 ID，用于清理追溯矩阵（test_case_id 维度）
    tc_rows = await session.execute(select(TestCase.id).where(TestCase.req_id.in_(req_ids)))
    tc_ids = [r[0] for r in tc_rows.all()]

    # 1) 追溯矩阵：同时引用 test_cases 与 requirements，必须在 TestCase 之前删除
    trace_cond = Traceability.requirement_id.in_(req_ids)
    if tc_ids:
        trace_cond = trace_cond | Traceability.test_case_id.in_(tc_ids)
    await session.execute(delete(Traceability).where(trace_cond))
    # 2) 测试用例
    await session.execute(delete(TestCase).where(TestCase.req_id.in_(req_ids)))
    # 3) 其余引用需求的子表
    await session.execute(delete(AnalysisTask).where(AnalysisTask.req_id.in_(req_ids)))
    await session.execute(delete(GenerationBatch).where(GenerationBatch.req_id.in_(req_ids)))
    await session.execute(
        delete(ReqDependency).where(
            ReqDependency.source_req_id.in_(req_ids) | ReqDependency.target_req_id.in_(req_ids)
        )
    )
    # 4) 需求本身（FunctionalPoint 经 ondelete=CASCADE 自动级联）
    await session.execute(delete(Requirement).where(Requirement.id.in_(req_ids)))


@router.delete("/projects/{proj_id}", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def delete_project(proj_id: str, session: AsyncSession = Depends(get_session)):
    """删除项目及关联的需求、用例（级联），并清理向量库。

    所有引用 requirements.id 的外键均为 RESTRICT，故由 _cascade_delete_requirements
    显式按"先子表后父表"顺序清理，避免外键冲突。
    """
    # 先收该项目下所有需求 ID，用于级联删除与清理向量库
    req_result = await session.execute(
        select(Requirement.id).where(Requirement.project_id == proj_id)
    )
    req_ids = [r[0] for r in req_result.all()]

    # 应用层级联删除需求及其所有子表（analysis_tasks / traceability 等）
    await _cascade_delete_requirements(session, req_ids)
    await session.execute(delete(Project).where(Project.id == proj_id))
    await session.commit()

    # 同步清理向量库
    for req_id in req_ids:
        try:
            from app.db.milvus_store import vector_store
            await asyncio.to_thread(vector_store.delete_requirement_from_kb, req_id)
        except Exception as e:
            logger.warning("[API/products] 向量库删除失败(不阻断): req_id={}, error={}", req_id, str(e)[:100])

    logger.info("[API/products] 项目已删除: id={}, 关联需求 {} 条已清理", proj_id, len(req_ids))
    return ApiResponse(success=True, message="已删除")


# ============================================================================
# 3. 模块 CRUD
# ============================================================================

@router.get("/products/{pid}/modules", response_model=ApiResponse)
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


@router.post("/products/{pid}/modules", response_model=ApiResponse, dependencies=[Depends(require_write)])
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
):
    """获取依赖图谱数据和布局。

    返回 nodes（有依赖关系的模块）和 edges（依赖关系）。
    注意：只有参与依赖关系的模块才会出现在图谱中，新建的无关联模块不会自动显示。
    """
    nodes: list[dict] = []
    edges: list[dict] = []

    # 1. 查询该产品下的所有模块 ID
    mod_stmt = select(Module.id, Module.name, Module.product_id, Module.description, Module.pos_x, Module.pos_y)
    if product_id:
        mod_stmt = mod_stmt.where(Module.product_id == product_id)
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
    session: AsyncSession = Depends(get_session),
):
    """获取产品下所有功能点及其依赖关系（跨需求）。

    返回 nodes（功能点）和 edges（FP 依赖关系，含跨需求边）。
    只返回该产品下所有需求的功能点；边的两端必须都在该产品功能点集合内。
    """
    # 1. 查询该产品下所有需求的 ID + title + status
    req_stmt = select(Requirement.id, Requirement.title, Requirement.status).where(
        Requirement.product_id == product_id
    )
    req_result = await session.execute(req_stmt)
    req_rows = req_result.all()
    req_id_set = {row[0] for row in req_rows}
    req_title_map = {row[0]: row[1] or "" for row in req_rows}
    req_status_map = {row[0]: row[2] or "active" for row in req_rows}

    if not req_id_set:
        return ApiResponse(success=True, message="ok", data={"nodes": [], "edges": []})

    # 2. 查询这些需求下的所有 FunctionalPoint
    fp_stmt = select(
        FunctionalPoint.id, FunctionalPoint.name, FunctionalPoint.fp_type,
        FunctionalPoint.requirement_id, FunctionalPoint.priority,
    ).where(FunctionalPoint.requirement_id.in_(req_id_set))
    fp_result = await session.execute(fp_stmt)
    fp_rows = fp_result.all()

    if not fp_rows:
        return ApiResponse(success=True, message="ok", data={"nodes": [], "edges": []})

    fp_id_set = {row[0] for row in fp_rows}

    nodes = [
        {
            "id": row[0],
            "name": row[1],
            "fp_type": row[2],
            "req_id": row[3],
            "req_title": req_title_map.get(row[3], ""),
            "req_status": req_status_map.get(row[3], "active"),
            "priority": row[4] or "P2",
        }
        for row in fp_rows
    ]

    # 3. 查询所有 FPDependency（source 或 target 在该产品 FP 集合内）
    dep_stmt = select(FPDependency).where(
        FPDependency.source_fp_id.in_(fp_id_set),
        FPDependency.target_fp_id.in_(fp_id_set),
    )
    dep_result = await session.execute(dep_stmt)
    deps = dep_result.scalars().all()

    edges = [
        {
            "id": d.id,
            "source": d.source_fp_id,
            "target": d.target_fp_id,
            "relation_type": d.relation_type,
            "description": d.description or "",
        }
        for d in deps
    ]

    return ApiResponse(success=True, message="ok", data={"nodes": nodes, "edges": edges})


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
            FPDependency.source_fp_id.in_(fp_id_set) | FPDependency.target_fp_id.in_(fp_id_set)
        )
    )
    deps = dep_result.scalars().all()

    # 构建邻接表
    # depends_on: target 是被依赖方 → 修改 target 影响 source（反向传播）
    # data_flows / lifecycle_scenarios: source 是来源 → 修改 source 影响 target（正向传播）
    adj: dict[str, set[str]] = {}
    for d in deps:
        if d.relation_type == "depends_on":
            adj.setdefault(d.target_fp_id, set()).add(d.source_fp_id)
        elif d.relation_type in ("data_flows", "lifecycle_scenarios"):
            adj.setdefault(d.source_fp_id, set()).add(d.target_fp_id)

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
    if all_modules:
        # 通过 proj_id 查询所属 product_id，用于隔离查询范围（原代码误用未定义的 pid 导致 NameError）
        proj_result = await session.execute(
            select(Project.product_id).where(Project.id == proj_id)
        )
        product_id = proj_result.scalar_one_or_none()
        if product_id:
            # 仅按 product_id 隔离；module 字段（单值，已废弃 v2.2，仅存首模块）不再作为 DB 过滤条件，
            # 改在 Python 层用 modules[] 数组匹配，避免漏掉多模块关联的需求
            req_result = await session.execute(
                select(Requirement).where(
                    Requirement.product_id == product_id,
                ).order_by(Requirement.created_at.desc())
            )
            for req in req_result.scalars().all():
                req_mods = set(req.modules or [])
                matched = list(req_mods & set(all_modules))
                if matched:
                    affected_reqs.append({
                        "id": req.id,
                        "title": req.title,
                        "matched_modules": matched,
                    })

    return ApiResponse(success=True, message="ok", data={
        "direct_modules": target_modules,
        "indirect_modules": indirect,
        "all_affected_modules": all_modules,
        "affected_requirements": affected_reqs,
        "affected_requirement_count": len(affected_reqs),
    })


# ============================================================================
# 管理后台首页（v2.12）
# ============================================================================

@router.get("/admin/dashboard", response_model=ApiResponse)
async def admin_dashboard(
    session: AsyncSession = Depends(get_session),
    user: "CurrentUser" = Depends(get_current_user),
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
