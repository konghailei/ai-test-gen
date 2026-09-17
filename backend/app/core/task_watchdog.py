"""分析任务看门狗（v3.3 新增，修 N4）。

## 为什么需要

LLM 流式调用自 v3.1.3 起 **「首响应到达后总生成时长不设上限」**：只施加「首响应超时」
与「chunk 空闲超时（`STREAM_IDLE_TIMEOUT=180s`）」，二者都防不住「上游以极慢但持续的
速率吐字」——此时单次调用可无限长。

> v3.3 已把**首响应**预算固定为 `LLM_FIRST_RESPONSE_TIMEOUT=180s`（原实现按 prompt
> 长度动态放宽到 1800s），且 180s 内收到任意响应——**正文或思考（reasoning_content）
> chunk 都算**——即视为模型存活，超时则跳过剩余重试直接切换备用提供商。因此
> 「prefill 完全停滞」这一类挂起已由 LLM 层兜住；本看门狗针对的是**首响应之后**
> 缓慢吐字或协程卡死的情形，仍是最后一道网。

而任务层（`requirement_tasks.py`）**没有任何超时或看门狗**，`grep watchdog|wait_for|超时`
零命中。唯一的恢复手段是：① 用户手动取消（依赖协程走到下一次状态更新才能生效，
挂起的协程永远走不到）② 进程重启时的 stale 清扫（`database.py:414`）。

结果：任务永久停在 `running`，需求永久停在 `analyzing`，前端一直转圈且无法重试。

## 本模块做什么

周期性扫描「长时间没有任何状态更新」的 running/pending 任务，标记为 `interrupted`
并回退卡死的 `analyzing` 需求。

**为什么用 interrupted 而不是 failed**：`requirement_tasks._update_task` 的 UPDATE 语句
带 `AnalysisTask.status != "interrupted"` 守卫（:950），所以即便僵尸协程后续恢复并
尝试写状态，也**无法**把任务改回 running/completed。若标记 failed 则会被覆盖，
导致状态反复横跳。

## 不做什么

不试图取消/杀死协程——当前没有协程注册表，强行引入改动面过大。
看门狗只保证「状态最终收敛到可解释的终态」，让用户能重试。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from loguru import logger

_watchdog_task: asyncio.Task | None = None


async def _sweep_once(stale_minutes: int) -> int:
    """执行一次扫描，返回被回收的任务数。"""
    from sqlalchemy import select, update

    from app.db.database import async_session
    from app.db.models import AnalysisTask, Requirement

    deadline = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    now = datetime.now(timezone.utc)
    reclaimed = 0

    async with async_session() as s:
        result = await s.execute(
            update(AnalysisTask)
            .where(
                AnalysisTask.status.in_(["pending", "running"]),
                AnalysisTask.updated_at < deadline,
            )
            .values(
                status="interrupted",
                error=f"任务无进展超过 {stale_minutes} 分钟，已被看门狗中断（N4 兜底）",
                completed_at=now,
            )
        )
        reclaimed = result.rowcount or 0
        # 与 database.py:427 的重启清扫保持一致：联动回退卡死需求，
        # 否则需求停在 analyzing 会导致澄清/确认/提交全部被拒。
        # 只回退那些确实没有其它活跃任务在跑的需求，避免误伤刚提交的重试。
        #
        # v3.4.1 修复 L-12：需求回退不再挂在 `if reclaimed:` 分支下。
        # 原实现要求"本轮确实回收了任务"才回退需求——若某需求停在 analyzing，而它的
        # 僵尸任务在更早一轮已被标记 interrupted，之后每轮 reclaimed 都是 0，该需求
        # 就永远不会被回退。现在每轮都幂等执行（无匹配行时 UPDATE 影响 0 行，无副作用）。
        active_req_ids = [
            row[0]
            for row in (
                await s.execute(
                    select(AnalysisTask.req_id).where(
                        AnalysisTask.status.in_(["pending", "running"])
                    )
                )
            ).fetchall()
            if row[0]
        ]
        stmt = update(Requirement).where(Requirement.status == "analyzing")
        if active_req_ids:
            stmt = stmt.where(Requirement.id.notin_(active_req_ids))
        req_result = await s.execute(stmt.values(status="draft"))
        await s.commit()
        if req_result.rowcount:
            logger.info(
                "[Watchdog] 联动回退卡死需求: {} 个 analyzing → draft",
                req_result.rowcount,
            )
        elif reclaimed:
            logger.info("[Watchdog] 回收僵尸任务 {} 个（无待回退需求）", reclaimed)

    return reclaimed


async def _watchdog_loop(stale_minutes: int, interval: int) -> None:
    """看门狗主循环。任何异常都不能让循环退出（否则看门狗自己死了）。"""
    logger.info(
        "[Watchdog] 分析任务看门狗已启动: stale={}min, interval={}s",
        stale_minutes,
        interval,
    )
    while True:
        try:
            await asyncio.sleep(interval)
            reclaimed = await _sweep_once(stale_minutes)
            if reclaimed:
                logger.warning(
                    "[Watchdog] 回收 {} 个无进展任务（超过 {} 分钟未更新状态）",
                    reclaimed,
                    stale_minutes,
                )
        except asyncio.CancelledError:
            logger.info("[Watchdog] 看门狗已停止")
            raise
        except Exception as e:  # noqa: BLE001 - 看门狗必须自愈
            logger.warning("[Watchdog] 巡检异常（下轮重试）: {}", str(e)[:200])
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise


def start_task_watchdog() -> asyncio.Task | None:
    """启动看门狗后台任务。在 FastAPI lifespan 启动阶段调用。

    Returns:
        创建的 asyncio.Task；未启用或已启动时返回 None。
    """
    global _watchdog_task

    from app.config import settings

    if not getattr(settings, "TASK_WATCHDOG_ENABLED", True):
        logger.info("[Watchdog] 看门狗未启用（TASK_WATCHDOG_ENABLED=False）")
        return None
    if _watchdog_task is not None and not _watchdog_task.done():
        return _watchdog_task

    _watchdog_task = asyncio.create_task(
        _watchdog_loop(
            stale_minutes=int(getattr(settings, "TASK_STALE_MINUTES", 60)),
            interval=int(getattr(settings, "TASK_WATCHDOG_INTERVAL", 120)),
        ),
        name="task-watchdog",
    )
    return _watchdog_task


async def stop_task_watchdog() -> None:
    """停止看门狗（应用关闭时调用）。"""
    global _watchdog_task
    if _watchdog_task is not None and not _watchdog_task.done():
        _watchdog_task.cancel()
        try:
            await _watchdog_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _watchdog_task = None
