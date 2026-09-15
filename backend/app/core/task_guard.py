"""生成任务的并发互斥与幂等（docs/15 §4.3）。

要解决的问题（审查证据）：
    - `/api/generate` 先在 session A 查活动任务、再在 session B 插入任务 → **TOCTOU**：
      两个并发请求可能都"查到没有活动任务"，然后各插一条 → 同一需求两个活动生成任务，
      互相覆盖任务状态、重复写用例；
    - `resume=True` 直接绕过活动任务拒绝 → 续跑可以踩在正在跑的任务上；
    - 幂等键缺失：同一请求重试（客户端超时重发）会再建一个任务。

做法（三层，逐层兜底）：
    1. **事务内 advisory lock** `pg_advisory_xact_lock(hashtext(req_id||':generate'))`：
       同一 req_id 的"查活动任务 + 插入"串行化，从根上消掉 TOCTOU（锁随事务提交自动释放）；
    2. **partial unique index**：`(req_id) WHERE status IN ('pending','running') AND task_type IN (...)`。
       DB 层最终防线 —— 即使有人绕过本模块直接插任务，也拦得住。
       注意：仍是"状态字段一变就不再占用"，所以终态流转必须可靠（见 review/异常路径）；
    3. **僵尸兜底**：插入前把 `updated_at` 超过 6 小时的 pending/running 生成任务置 `interrupted`，
       避免服务重启残留把生成永久挡住（主回收方仍是 `core/task_watchdog.py`，每 120s 扫 60 分钟无更新）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.utils.logger import logger

# 参与互斥的生成类任务（上传需求 / 提交澄清等其它类型不受影响）
GENERATION_TASK_TYPES = ("generate_cases", "regenerate")
ACTIVE_STATUSES = ("pending", "running")

# 僵尸判定（兜底用；主回收方是 task_watchdog 的 60 分钟无更新）
ZOMBIE_HOURS = 6

# ⚠️ partial unique index 的 DDL 只有一份，在 `app/db/database.py` 的 init_db Step 3b
# （启动时执行）。这里不再重复实现，避免两份 DDL 漂移导致"以为建了其实没建"。


async def _reap_zombies(session, req_id: str) -> int:
    """把该需求超过 ZOMBIE_HOURS 没有更新的活动生成任务置 interrupted（同事务内）。"""
    from app.db.models import AnalysisTask

    cutoff = datetime.now(timezone.utc) - timedelta(hours=ZOMBIE_HOURS)
    rows = (await session.execute(
        select(AnalysisTask).where(
            AnalysisTask.req_id == req_id,
            AnalysisTask.status.in_(list(ACTIVE_STATUSES)),
            AnalysisTask.task_type.in_(list(GENERATION_TASK_TYPES)),
            AnalysisTask.updated_at < cutoff,
        )
    )).scalars().all()
    for task in rows:
        task.status = "interrupted"
        task.message = "任务长时间无状态更新，已被生成互斥守卫回收"
        logger.warning("[TaskGuard] 回收僵尸生成任务: task_id={}, req_id={}", task.id, req_id)
    return len(rows)


async def reserve_generation_slot(
    *,
    req_id: str,
    task_id: str,
    product_id: str,
    created_by: str = "api",
) -> dict:
    """原子地占用「该需求的活动生成槽位」并建任务。

    Returns:
        dict: {"status": "created" | "conflict", "task_id": ..., "active_task_id": ..., "active_status": ...}
    """
    from app.db.database import async_session
    from app.db.models import AnalysisTask

    async with async_session() as session:
        # ① 事务级 advisory lock：同 req_id 的并发请求在此串行（提交/回滚时自动释放）
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"{req_id}:generate"},
        )
        # ② 僵尸兜底
        await _reap_zombies(session, req_id)
        # ③ 活动任务检查（此时已与插入同处一个事务，不存在 TOCTOU 窗口）
        active = (await session.execute(
            select(AnalysisTask.id, AnalysisTask.status, AnalysisTask.created_at)
            .where(
                AnalysisTask.req_id == req_id,
                AnalysisTask.status.in_(list(ACTIVE_STATUSES)),
                AnalysisTask.task_type.in_(list(GENERATION_TASK_TYPES)),
            )
            .order_by(AnalysisTask.created_at.desc())
            .limit(1)
        )).first()
        if active is not None:
            await session.rollback()
            return {
                "status": "conflict",
                "task_id": task_id,
                "active_task_id": active[0],
                "active_status": active[1],
            }
        # ④ 插入
        session.add(AnalysisTask(
            id=task_id,
            task_type="generate_cases",
            req_id=req_id,
            product_id=product_id,
            status="running",
            phase="generate",
            progress=0,
            message="用例生成已启动...",
            created_by=created_by,
        ))
        try:
            await session.commit()
        except Exception as e:  # noqa: BLE001
            await session.rollback()
            # 区分"唯一索引拦下（真冲突）"与"插入失败（如外键不存在）"：
            # 把后者伪装成 409 会让人以为"有任务在跑"，排查方向全错。
            msg = str(e)
            is_dup = "uq_analysis_tasks_active_generation" in msg or "duplicate key" in msg.lower()
            if is_dup:
                logger.warning("[TaskGuard] 插入生成任务被唯一索引拦截: req_id={}, err={}", req_id, msg[:150])
                return {"status": "conflict", "task_id": task_id, "active_task_id": "", "active_status": ""}
            logger.error("[TaskGuard] ⚠️ 创建生成任务失败（非并发冲突，需排查）: req_id={}, err={}", req_id, msg[:300])
            raise
    return {"status": "created", "task_id": task_id}
