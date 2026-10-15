"""生成入口资源准入门接线验证（v4.0.2）。

## 为什么单测之外还要这个脚本

`tests/test_resource_guard.py` 只验 `GenerationGate` **本身**的语义。但真正会
咬人的是**接线**：`api/generate.py` 里"拿槽位 → 跑流水线 → 归还槽位"这条链上
任何一个环节写错，表现都是**静默的**——

  - 忘了归还：第一个任务跑完后槽位永不释放，**后续所有生成无限排队**；
  - 归还过早/重复：并发超额，CPU 照样打穿；
  - 排队事件不发：用户对着静止的页面，以为系统卡死。

这些用单测覆盖不到（需要真实 ASGI + SSE + 并发请求），故用脚本打桩
`orchestrator.run_pipeline` 做端到端验证。运行：`python tests/test_generate_resource_gate.py`

## 验什么

1. 限额内并发请求全部放行（无回归：单用户场景零等待）；
2. 超限请求收到 `stage=queued` 的 SSE 事件，并真的**阻塞**到有资源；
3. 前序任务完成后排队者自动继续（等待 → 获得资源 → 跑完）；
4. **槽位全部归还**（active 归零）——最重要的一条，防"永久堵死"；
5. 排队者最终能正常跑完整个流水线（门控不破坏 done 链路）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx  # noqa: E402

from app.core.auth import get_default_token  # noqa: E402
from app.core import resource_guard as rg_module  # noqa: E402
from app.db.database import async_session  # noqa: E402
from app.db.models import AnalysisTask, Requirement  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402

GREEN, RED, YELLOW, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {GREEN if ok else RED}{'✓' if ok else '✗'}{RESET} {name}"
          + (f" — {detail}" if detail and not ok else ""), flush=True)


def section(title: str) -> None:
    print(f"\n{YELLOW}━━ {title} ━━{RESET}", flush=True)


# 并发请求必须用**不同** req_id：reserve_generation_slot 是"同一需求互斥"
REQ_IDS = [f"REQ-GATE-{i:03d}" for i in range(4)]
PROD = "PROD-GATE-TEST"
SRR = {
    "requirement": {
        "title": "资源门控测试需求",
        "product_name": "门控测试产品",
        "description": "用于验证生成入口资源准入的接线",
        "modules": ["gate"],
    },
}
CEILING = 3  # 把并发上限钉死，测试结果不随机器核数波动


class FakePipeline:
    """打桩流水线：进入即占住槽位，直到外部放行；之后走完正常 done 链路。"""

    def __init__(self) -> None:
        self.entered: list[str] = []
        self.release_evt = asyncio.Event()
        self.all_entered = asyncio.Event()

    async def run_pipeline(self, **kwargs):
        rid = kwargs.get("existing_req_id", "")
        self.entered.append(rid)
        if len(self.entered) >= CEILING:
            self.all_entered.set()
        await self.release_evt.wait()
        yield {"stage": "parse", "message": "解析中", "progress": 20, "req_id": rid}
        yield {"stage": "generate", "message": "生成中", "progress": 75, "cases_count": 1}
        yield {
            "stage": "done", "message": "完成", "progress": 100,
            "req_id": rid,
            "cases": [{"id": "TC-GATE-1", "title": "门控用例", "type": "functional"}],
            "review_report": {"total_cases": 1, "passed_cases": 1},
        }


async def read_until_terminal(resp: httpx.Response, sink: list[dict]) -> None:
    """读 SSE 流直到 done/error（或流结束）。"""
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            sink.append(json.loads(payload))
        except Exception:  # noqa: BLE001
            continue
        if any(e.get("stage") in ("done", "error") for e in sink):
            return


async def open_batch(client: httpx.AsyncClient, headers: dict, req_ids: list[str],
                     fake: FakePipeline) -> tuple[list[asyncio.Task], list[dict]]:
    """并发打开 len(req_ids) 条 SSE 流，返回 (读取任务, 各流事件收集器)。"""
    tasks: list[asyncio.Task] = []
    sinks: list[dict] = []
    for rid in req_ids:
        resp = await client.send(
            client.build_request("POST", "/api/generate", json={"req_id": rid}, headers=headers),
            stream=True,
        )
        sink: list[dict] = []
        sinks.append(sink)
        tasks.append(asyncio.create_task(read_until_terminal(resp, sink), name=f"sse-{rid}"))
    return tasks, sinks


async def setup_fixtures() -> None:
    from sqlalchemy import delete

    async with async_session() as s:
        await s.execute(delete(AnalysisTask).where(AnalysisTask.req_id.in_(REQ_IDS)))
        await s.execute(delete(Requirement).where(Requirement.id.in_(REQ_IDS)))
        for rid in REQ_IDS:
            s.add(Requirement(
                id=rid, product_id=PROD, title=f"门控测试需求 {rid}",
                priority="P2", module="gate", modules=["gate"],
                status="confirmed", srr_json=SRR,
                raw_text=f"门控测试需求 {rid}", source_type="markdown", created_by="test",
            ))
        await s.commit()


async def cleanup_fixtures() -> None:
    from sqlalchemy import delete

    async with async_session() as s:
        await s.execute(delete(AnalysisTask).where(AnalysisTask.req_id.in_(REQ_IDS)))
        await s.execute(delete(Requirement).where(Requirement.id.in_(REQ_IDS)))
        await s.commit()


async def main() -> int:
    gate = rg_module.resource_guard.gate
    token = get_default_token()
    headers = {"Authorization": f"Bearer {token}"}
    orig_ceiling = rg_module.task_ceiling
    # 钉死并发上限：不随机器核数波动（本容器推导值也是 3，但别依赖它）
    rg_module.task_ceiling = lambda snap=None: CEILING

    await setup_fixtures()
    # 清掉可能残留的槽位，保证从干净状态开始
    for tid in list(getattr(gate, "_active", [])):
        gate.release(tid)

    transport = httpx.ASGITransport(app=fastapi_app)
    client = httpx.AsyncClient(transport=transport, base_url="http://t", timeout=60.0)
    try:
        # ================= 场景 1：限额内并发全部放行 =================
        section(f"场景 1：{CEILING} 个并发请求全部放行（单用户场景无回归）")
        fake = FakePipeline()
        with patch("app.api.generate.orchestrator.run_pipeline", side_effect=fake.run_pipeline):
            tasks, _ = await open_batch(client, headers, REQ_IDS[:CEILING], fake)
            try:
                await asyncio.wait_for(fake.all_entered.wait(), timeout=25.0)
                check(f"{CEILING} 个并发请求都进入了流水线（拿到资源）",
                      len(fake.entered) == CEILING, f"实际进入 {len(fake.entered)}: {fake.entered}")
                check(f"闸门 active = {CEILING}", gate.active_tasks == CEILING,
                      f"实际 {gate.active_tasks}")
                check("闸门 waiting = 0（没人排队）", gate.waiting_count == 0)
            finally:
                fake.release_evt.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=40.0)
        check("场景 1 结束后 active = 0（槽位已归还）", gate.active_tasks == 0,
              f"实际 {gate.active_tasks}，残留 {getattr(gate, '_active', [])}")

        # ================= 场景 2：超限请求排队 → 释放后自动继续 =================
        section(f"场景 2：第 {CEILING + 1} 个请求排队，收到 queued 事件，释放后自动继续")
        fake2 = FakePipeline()
        with patch("app.api.generate.orchestrator.run_pipeline", side_effect=fake2.run_pipeline):
            tasks2, sinks2 = await open_batch(client, headers, REQ_IDS[:CEILING], fake2)
            try:
                await asyncio.wait_for(fake2.all_entered.wait(), timeout=25.0)
                # 打开第 4 条：应当排队而非进入
                t4, sink4 = await open_batch(client, headers, [REQ_IDS[CEILING]], fake2)
                await asyncio.sleep(1.5)  # 留足时间进入等待队列并推出首个 queued 事件
                check(f"第 {CEILING + 1} 个请求未进入流水线（在排队）",
                      len(fake2.entered) == CEILING, f"entered={fake2.entered}")
                check("闸门 waiting = 1", gate.waiting_count == 1, f"实际 {gate.waiting_count}")
                check("排队者收到 queued SSE 事件",
                      any(e.get("stage") == "queued" for e in sink4),
                      f"stages={[e.get('stage') for e in sink4]}")
                q = next((e for e in sink4 if e.get("stage") == "queued"), {})
                check("queued 事件带排队位次与上限",
                      isinstance(q.get("queue_position"), int) and isinstance(q.get("ceiling"), int),
                      f"queue_position={q.get('queue_position')}, ceiling={q.get('ceiling')}")
            finally:
                fake2.release_evt.set()
        await asyncio.wait_for(asyncio.gather(*(tasks2 + t4)), timeout=60.0)
        check("排队者已进入流水线", REQ_IDS[CEILING] in fake2.entered,
              f"entered={fake2.entered}")
        check("排队者最终收到 done（门控不破坏 done 链路）",
              any(e.get("stage") == "done" for e in sink4),
              f"stages={[e.get('stage') for e in sink4]}")

        # ================= 场景 3：全部完成后槽位归零（防永久堵死） =================
        section("场景 3：全部完成后槽位归零（防永久堵死）")
        await asyncio.sleep(1.0)
        check("闸门 active = 0（无泄漏）", gate.active_tasks == 0,
              f"实际 {gate.active_tasks}，残留 {getattr(gate, '_active', [])}")
        check("闸门 waiting = 0", gate.waiting_count == 0, f"实际 {gate.waiting_count}")
        check("准入计数 = 4 / 拒绝 = 0",
              gate.snapshot()["admitted_total"] == 4 and gate.snapshot()["rejected_total"] == 0,
              f"{gate.snapshot()['admitted_total']}/{gate.snapshot()['rejected_total']}")

        # ================= 场景 4：等待超时必须明确失败 =================
        section("场景 4：等待超时返回明确错误（不无限挂起）")
        fake3 = FakePipeline()
        with patch("app.api.generate.orchestrator.run_pipeline", side_effect=fake3.run_pipeline):
            tasks3, _ = await open_batch(client, headers, REQ_IDS[:CEILING], fake3)
            try:
                await asyncio.wait_for(fake3.all_entered.wait(), timeout=25.0)
                # 把等待预算压到 0.3s，快速验证超时路径
                real_wait = rg_module._cfg
                rg_module._cfg = lambda name, default: (
                    0.3 if name == "GEN_RESOURCE_WAIT_TIMEOUT_S" else real_wait(name, default)
                )
                try:
                    t5, sink5 = await open_batch(client, headers, [REQ_IDS[CEILING]], fake3)
                    await asyncio.wait_for(asyncio.gather(t5), timeout=30.0)
                finally:
                    rg_module._cfg = real_wait
                errs = [e for e in sink5 if e.get("stage") == "error"]
                check("超时后收到 error 事件", len(errs) == 1,
                      f"stages={[e.get('stage') for e in sink5]}")
                check("error 带 resource_unavailable 标记",
                      bool(errs) and errs[0].get("error_kind") == "resource_unavailable",
                      f"error_kind={errs[0].get('error_kind') if errs else None}")
                check("超时未占用槽位（active 仍为 3）", gate.active_tasks == CEILING,
                      f"实际 {gate.active_tasks}")
            finally:
                fake3.release_evt.set()
        await asyncio.wait_for(asyncio.gather(*tasks3), timeout=60.0)
        await asyncio.sleep(1.0)
        check("场景 4 结束后 active = 0", gate.active_tasks == 0,
              f"实际 {gate.active_tasks}")
    finally:
        rg_module.task_ceiling = orig_ceiling
        # 兜底清理：取消残留 SSE 任务、归还残留槽位
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task() and not t.done() and (t.get_name() or "").startswith("sse-"):
                t.cancel()
        await asyncio.gather(*asyncio.all_tasks(), return_exceptions=True)
        for tid in list(getattr(gate, "_active", [])):
            gate.release(tid)
        await client.aclose()
        await cleanup_fixtures()

    ok = sum(1 for _, v, _ in results if v)
    print(f"\n{'=' * 60}\n门控接线验证: {ok}/{len(results)} 通过, {len(results) - ok} 失败\n{'=' * 60}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    t0 = time.time()
    try:
        code = asyncio.run(main())
    finally:
        print(f"总耗时 {time.time() - t0:.1f}s", flush=True)
    raise SystemExit(code)
