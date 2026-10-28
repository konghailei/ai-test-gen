"""生成任务自愈：API 启动后自动续跑被中断的生成任务（docs/15 §4.2 / §4.8）。

要解决的问题（任务迁出 API 进程的务实实现）：
    生成任务跑在 API 进程内，API 重启/升级会杀掉在飞任务——此前只能靠人工点续跑。
    完整的持久任务队列是架构级改造；而**自愈续跑**用既有机制就能交付 80% 的价值：
    API 启动时扫描近 24h 内被中断、且带可用断点的生成任务，自动 resume
    （断点续跑 + 输入指纹校验都是现成的，探针已验证）。

规则（宁可不续，不乱续）：
    - 只处理 `interrupted` 且 `result_json.checkpoint.fp_groups` 非空的任务；
    - 每个需求最多续跑一个（最近优先）；该需求已有活动任务则跳过（并发互斥语义）；
    - 需求状态非 confirmed → 跳过；
    - **输入指纹校验不过 → 跳过**（保留 interrupted，由人工决定 fallback_to_restart）——
      自愈绝不把旧产物拼到新输入上；
    - 新任务走 `task_guard.reserve_generation_slot` 原子占用，防启动并发重复。

失败语义：续跑失败把新任务置 failed（不动旧任务），日志可查；不抛出。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.utils.logger import logger

SELFHEAL_WINDOW_HOURS = 24
SELFHEAL_MAX_TASKS = 5

# v3.4.1 修复 H-5：后台任务强引用集合——CPython 事件循环只弱引用 task，
# 裸 create_task 的协程可能被 GC 静默丢弃，任务永远停在 running
_bg_tasks: set[asyncio.Task] = set()


def _create_bg_task(coro) -> asyncio.Task:
    """创建后台任务并持有强引用，结束后自动移出集合。"""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


async def find_selfheal_candidates(limit: int = SELFHEAL_MAX_TASKS) -> list[dict]:
    """扫描可自愈的中断任务：每个需求取最近一条，排除已有活动任务的。"""
    from app.db.database import async_session
    from app.db.models import AnalysisTask

    cutoff = datetime.now(timezone.utc) - timedelta(hours=SELFHEAL_WINDOW_HOURS)
    async with async_session() as s:
        rows = (await s.execute(
            select(AnalysisTask)
            .where(
                AnalysisTask.task_type == "generate_cases",
                AnalysisTask.status == "interrupted",
                AnalysisTask.updated_at > cutoff,
            )
            .order_by(AnalysisTask.updated_at.desc())
            .limit(50)
        )).scalars().all()

    seen_req: set[str] = set()
    candidates: list[dict] = []
    for t in rows:
        if t.req_id in seen_req:
            continue
        cp = dict(t.result_json or {}).get("checkpoint") or {}
        if not cp.get("fp_groups"):
            continue
        seen_req.add(t.req_id)
        candidates.append({
            "req_id": t.req_id,
            "product_id": t.product_id or "",
            "source_task_id": t.id,
            "checkpoint": cp,
        })
        if len(candidates) >= limit:
            break
    return candidates


async def _update_task(task_id: str, **fields) -> None:
    """自愈任务的状态更新（与 generate.py 同样的守卫语义：终态不可覆盖）。"""
    from sqlalchemy import update as sa_update

    from app.db.database import async_session
    from app.db.models import AnalysisTask
    try:
        async with async_session() as s:
            await s.execute(
                sa_update(AnalysisTask)
                .where(AnalysisTask.id == task_id,
                       AnalysisTask.status.in_(["pending", "running"]))
                .values(**fields)
            )
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("[SelfHeal] 更新任务状态失败: task={}, err={}", task_id, str(e)[:120])


async def _run_selfheal(task_id: str, req_id: str, srr_json: dict,
                        checkpoint: dict) -> None:
    """执行一次自愈续跑（独立协程；不抛出）。"""
    from datetime import datetime as _dt, timezone as _tz

    from app.core.publication_policy import quality_outcome
    from app.core.orchestrator import orchestrator

    logger.info("[SelfHeal] 开始自愈续跑: task={}, req={}", task_id, req_id)
    try:
        async for event in orchestrator.run_pipeline(
            "", srr_json=srr_json, existing_req_id=req_id,
            resume=True, resume_checkpoint=checkpoint, task_id=task_id,
        ):
            if event.get("stage") == "done":
                await _update_task(
                    task_id,
                    status="failed" if event.get("completion_status") == "failed" else "completed",
                    phase="done", progress=100,
                    message="生成结果待人工审核（自愈续跑）" if event.get("manual_action_required") else "用例生成完成（自愈续跑）",
                    completed_at=_dt.now(_tz.utc),
                    result_json={
                        "selfheal": True,
                        **quality_outcome(event),
                        "case_count": len(event.get("cases") or []),
                    },
                )
                logger.info("[SelfHeal] 自愈续跑完成: task={}, req={}", task_id, req_id)
            elif event.get("stage") == "error":
                await _update_task(
                    task_id, status="failed", phase="generate", progress=100,
                    message="自愈续跑失败: " + str(event.get("message", ""))[:150],
                    error=str(event.get("message", ""))[:500],
                    completed_at=_dt.now(_tz.utc),
                )
    except asyncio.CancelledError:
        # v4.1.8：取消（进程优雅停机）也必须在 DB 留痕。CancelledError 继承
        # BaseException，下面的 except Exception 捕获不到——不处理则任务永远停在
        # running，直到下次启动 init_db 的 stale 清理才改判；窗口期内该需求
        # 新建分析会被活动任务唯一索引挡成 409。_update_task 自带终态守卫，
        # 重复写/失败均无副作用。
        try:
            await _update_task(
                task_id, status="interrupted", phase="generate", progress=0,
                message="自愈续跑被中断（进程停机）",
                completed_at=_dt.now(_tz.utc),
            )
        except Exception:  # noqa: BLE001
            pass
        raise
    except Exception as e:  # noqa: BLE001
        logger.error("[SelfHeal] 自愈续跑异常: task={}, req={}, err={}", task_id, req_id, str(e)[:200])
        await _update_task(
            task_id, status="failed", phase="generate", progress=100,
            message="自愈续跑异常", error=str(e)[:500],
            completed_at=_dt.now(_tz.utc),
        )


async def selfheal_interrupted_generations(max_tasks: int = SELFHEAL_MAX_TASKS) -> list[dict]:
    """入口：扫描并**启动**自愈续跑（每个候选一个独立协程，不等待完成）。

    Returns:
        list[dict]: 每个候选的处理结果（resumed / skipped + 原因），供启动日志与探针断言。
    """
    from datetime import datetime as _dt, timezone as _tz

    from app.core.pipeline_fingerprint import (
        compare_fingerprints,
        load_current_fingerprint,
    )
    from app.core.task_guard import reserve_generation_slot
    from app.db.database import async_session
    from app.db.models import Requirement
    from app.utils.id_gen import gen_task_id

    results: list[dict] = []
    candidates = await find_selfheal_candidates(max_tasks)
    if not candidates:
        logger.info("[SelfHeal] 启动自愈扫描：无可续跑的中断任务")
        return results

    logger.info("[SelfHeal] 启动自愈扫描：发现 {} 个候选", len(candidates))
    for cand in candidates:
        req_id = cand["req_id"]
        entry: dict = {"req_id": req_id, "source_task": cand["source_task_id"], "action": "skipped"}
        try:
            async with async_session() as s:
                req = (await s.execute(
                    select(Requirement).where(Requirement.id == req_id)
                )).scalar_one_or_none()
            if req is None or req.status != "confirmed":
                entry["reason"] = f"需求状态 {getattr(req, 'status', '不存在')} 非 confirmed"
                results.append(entry)
                continue

            srr_json = req.srr_json or {}
            async with async_session() as s:
                current_fp = await load_current_fingerprint(s, req, srr_json)
            ok, diffs = compare_fingerprints(cand["checkpoint"].get("fingerprint"), current_fp)
            if not ok:
                entry["reason"] = "输入指纹不一致（保留 interrupted，等人工决定）"
                entry["diffs"] = [d["field"] for d in diffs]
                results.append(entry)
                continue

            new_task_id = gen_task_id()
            slot = await reserve_generation_slot(
                req_id=req_id, task_id=new_task_id,
                product_id=cand["product_id"], created_by="selfheal",
            )
            if slot["status"] != "created":
                entry["reason"] = f"并发互斥：{slot.get('active_task_id') or '槽位被占'}"
                results.append(entry)
                continue

            entry["action"] = "resumed"
            entry["task_id"] = new_task_id
            results.append(entry)
            # v3.4.1 修复 H-5：改用带强引用的 _create_bg_task，防 GC 静默丢弃
            _create_bg_task(
                _run_selfheal(new_task_id, req_id, srr_json, cand["checkpoint"])
            )
        except Exception as e:  # noqa: BLE001
            entry["reason"] = "自愈调度异常: " + str(e)[:150]
            results.append(entry)

    resumed = sum(1 for r in results if r.get("action") == "resumed")
    logger.info("[SelfHeal] 自愈调度完成: 候选={} 续跑={} 跳过={}",
                len(results), resumed, len(results) - resumed)
    return results

