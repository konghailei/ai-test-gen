"""知识库维护周期任务（v3.3 / N5-a 根治）。

## 为什么需要

N5 追查确认了三个机制缺陷，其中 **N5-c（无「PG 状态 vs Milvus 实际」对账机制）**
是"这类不一致能潜伏至今"的根本原因：

    N5-a 归档派发 Celery 向量化任务后无终态保证（有需求长期停留 pending）
    N5-b KB 纠错「删旧向量 → 重新嵌入」非原子，中断则永久丢向量且状态不回滚
    N5-c 无对账机制 → 上述不一致无法被自动发现

`kb_vector_reconcile` 提供了修复能力，但**之前只能手动跑**。
本模块把它变成周期任务，使系统能**自愈**，而不是等人发现。

## 做什么 / 不做什么

- ✅ **自动修复**：补写缺失向量、修正"卡住"的状态（只新增/改状态，不删任何数据）
- ✅ **只报告孤儿**：孤儿向量扫描结果写入日志与产物，**不自动删除**
  （删除不可逆，必须人工确认——见 `scripts/kb_orphan_gc.py --apply`）
- ❌ 不做别的清理、不碰业务数据

## 配置

`KB_MAINTENANCE_ENABLED`（默认 True）/ `KB_MAINTENANCE_INTERVAL`（默认 3600 秒）
"""

from __future__ import annotations

import asyncio

from loguru import logger

_loop_task: asyncio.Task | None = None


async def _run_once() -> None:
    """执行一轮对账自愈 + 孤儿扫描（只报告）。"""
    from app.core.kb_vector_reconcile import (
        reconcile_requirements,
        scan_orphan_vectors,
    )

    try:
        rep = await reconcile_requirements(repair=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("[KbMaint] 对账失败（下轮重试）: {}", str(e)[:200])
        return

    fixed = rep["repaired"] + rep["status_only_fixed"]
    if fixed or rep["repair_failed"]:
        logger.warning(
            "[KbMaint] 对账自愈: 补写向量 {} 条 / 修正状态 {} 条 / 失败 {} 条 "
            "（PG 需求 {}，Milvus 向量 {}）",
            rep["repaired"], rep["status_only_fixed"], len(rep["repair_failed"]),
            rep["pg_requirement_count"], rep["vector_count"],
        )
    else:
        logger.info(
            "[KbMaint] 对账一致: completed 且有向量 {} 条，无需修复",
            rep["completed_with_vector"],
        )

    try:
        orph = await scan_orphan_vectors("requirements")
        if orph["orphan_count"]:
            # 只报告，不自动删除——删除不可逆，需人工确认后跑 kb_orphan_gc.py --apply
            logger.warning(
                "[KbMaint] 发现孤儿向量 {} 条（占 {:.1%}），**未自动删除**；"
                "如需清理请人工确认后执行 scripts/kb_orphan_gc.py --apply。样例: {}",
                orph["orphan_count"],
                orph["orphan_count"] / max(orph["vector_count"], 1),
                orph["orphan_ids"][:5],
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("[KbMaint] 孤儿扫描失败: {}", str(e)[:200])


async def _maintenance_loop(interval: int) -> None:
    """维护主循环。任何异常都不能让循环退出（否则维护自己死了）。"""
    logger.info("[KbMaint] 知识库维护任务已启动: interval={}s", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            await _run_once()
        except asyncio.CancelledError:
            logger.info("[KbMaint] 知识库维护任务已停止")
            raise
        except Exception as e:  # noqa: BLE001 - 必须自愈
            logger.warning("[KbMaint] 维护循环异常（下轮重试）: {}", str(e)[:200])
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise


def start_kb_maintenance() -> asyncio.Task | None:
    """启动知识库维护任务（在 FastAPI lifespan 启动阶段调用）。"""
    global _loop_task

    from app.config import settings

    if not getattr(settings, "KB_MAINTENANCE_ENABLED", True):
        logger.info("[KbMaint] 知识库维护未启用（KB_MAINTENANCE_ENABLED=False）")
        return None
    if _loop_task is not None and not _loop_task.done():
        return _loop_task

    _loop_task = asyncio.create_task(
        _maintenance_loop(int(getattr(settings, "KB_MAINTENANCE_INTERVAL", 3600))),
        name="kb-maintenance",
    )
    return _loop_task


async def stop_kb_maintenance() -> None:
    """停止维护任务（应用关闭时调用）。"""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        _loop_task.cancel()
        try:
            await _loop_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _loop_task = None
