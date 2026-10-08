# docs/22 — FP 子批并发生成方案（方案①）

> 状态：**已实施（v3.5.5）**——2026-10-08 完成编码 + 守卫测试 + 回归。
> 大哥拍板：并发做成设置页开关（kb_settings KV，运行时动态读取，改后立即生效）。
> 实施记录见 §4；待办：dogfood 真实 E2E 复测（与 D-3/D-5 合并）。
> 来源：docs/21 自举验证的耗时基线（生成阶段 ~50min 串行）+ 用户加速需求

## 0. 背景与收益

自举验证实测（docs/21 §单轮耗时）：

- 生成阶段 ~50min：FP1 LLM 14.5min + 批内去重 3.7min + FP2 14min + Review 10min + 非LLM空隙 8min
- 全流程 1.5-2h（LLM 正常时）

FP 之间无数据依赖（各生成各的，汇合点统一去重），完全串行是纯浪费。
并发化（N=3）预期：生成阶段 ~50min → ~20min，全流程 1.5-2h → ~1h。

## 1. 可行性验证结论（2026-10-08 实测）

### 1.1 数据依赖审计 —— ✅ 无跨 FP 数据依赖

`_gen_node`（orchestrator.py L1077 起）FP 循环体共享状态逐项审计：

| 状态 | 并发处理方式 |
|---|---|
| `resolved_cases`（用例汇总） | 各任务局部收集 → 完成后合并（顺序无关，汇合点有全量去重） |
| `checkpoint_groups` | 完成回调内 append + 全量覆盖写（天然收敛，见 1.3） |
| `degraded_flags` | append 有 `not in` 守卫；asyncio 单线程事件循环无竞态 |
| `fp_subtasks`（进度卡片） | `_emit_gen` 按 key 移除旧卡片再 append，全量快照下发 |
| `_gen_queue`（SSE 事件队列） | ContextVar 隔离 |
| GenAgent 实例 | 无实例级可变状态（唯一属性 `self._llm`），可复用单实例 |
| GenAgent 内部 few-shot 检索 | 每次调用独立 Milvus/PG 会话；`execution_count` 自增是 DB 原子操作 |

关键确认：主汇合点（L1434-1483）有**同名去重 → 3-gram 近重复去重 → embedding 语义去重（0.92）**三道兜底，FP 完成顺序不影响最终去重效果。

### 1.2 LLM 供应商并发实测 —— ✅ 无限流、无降速

探针脚本（绕开响应缓存，每路不同 prompt，火山 glm-5.3-flash 主供应商）：

| 梯度 | 结果 |
|---|---|
| N=3 | 3/3 成功，单路 2.5-2.8s |
| N=5 | 5/5 成功，单路 4.3-4.8s |
| N=8 | 8/8 成功，最慢单路 5.3s，无 429 |

供应商侧无并发上限限制。大 prompt（GenAgent 真实场景 140-150s/次）并发吞吐属落地后监控项，不构成可行性阻塞。

### 1.3 checkpoint 语义 —— ✅ 全量覆盖写天然收敛，无需改动

`_save_gen_checkpoint`（L405-436）每次写入都是全量覆盖：
`result_json.checkpoint.fp_groups = list(fp_groups)`。

并发场景推演：A 完成回调 `append(gA); await save(含 gA)`，B 完成回调 `append(gB); await save(含 gA+gB)`。任意交织下最终状态一致（每步全量覆盖、单调递增），**无需加锁**。

唯一纪律：append 与 save 必须在同一个完成回调内成对出现（保证时序一致）。

### 1.4 进度事件契约 —— ✅ 前端按 key 刷新，乱序安全

`_emit_gen`（L1138-1165）：subtask 唯一 key = fp_id，更新时先按 key 移除旧卡片再 append，事件携带全量快照（`subtasks=snap`）。FP 完成序乱序不影响展示正确性。**前端零改动**。

### 1.5 LLM 总预算机制 —— ✅ 并发安全

`LLM_CALL_TOTAL_BUDGET`（240s）是**单次 call 内的墙钟 deadline**（`deadline = start_ts + budget`，每次调用独立起算），非共享计数器，并发各路独立预算。

### 1.6 GenAgent 内部检查 —— ✅ 安全

- few-shot 检索独立会话；`execution_count = execution_count + 1` DB 原子自增
- 四级去重（含 L2 embedding）每次调用独立；bge 同步推理经 `asyncio.to_thread` 包装，默认线程池（min(32, cpu+4)）配额足够

## 2. 实施方案

### 2.1 改动清单

| # | 文件 | 改动 | 规模 |
|---|---|---|---|
| 1 | `backend/app/core/orchestrator.py` | `_gen_node` FP 循环 → gather + Semaphore + 完成即写 checkpoint | 中 |
| 2 | `backend/app/config.py` | 新增 `GEN_FP_CONCURRENCY: int = 3`（0/1 = 串行回退） | 小 |
| 3 | `backend/tests/test_fp_concurrency.py` | 新建守卫测试 | 新建 |
| 4 | `backend/app/VERSION` + `frontend/package.json` | v3.5.4 → v3.5.5 | 小 |
| 5 | 回归 phase4/phase5 + 真实 E2E | 验证 | — |

### 2.2 核心改动设计（orchestrator.py）

```python
# _gen_node 内（替换原 for fp in fp_list 串行循环）：

# v3.5.5（方案①）：FP 间无数据依赖（各生成各的，汇合点统一三道去重），
# 串行循环是纯浪费。改为 Semaphore 限流的受控并发。
# 关键语义保持：
#   a) checkpoint 完成即写（全量覆盖，天然收敛无竞态）
#   b) 进度事件按 fp_id key 刷新（前端零改动）
#   c) 单 FP 失败不阻断其它 FP（不阻断语义保持）
#   d) D-5 大 FP 子批在单 FP 任务内仍串行（共享该 FP 的要点上下文）
_fp_conc = int(getattr(settings, "GEN_FP_CONCURRENCY", 3) or 0)

async def _gen_one_fp(fp: dict) -> None:
    fid, fname = fp.get("id", ""), fp.get("name", "")
    # ... D-5 分片（_split_big_fp，逻辑不变）...
    fp_cases_all: list[dict] = []
    shards_failed = 0
    async with _sem:  # Semaphore(_fp_conc)，_fp_conc<=1 时不并发
        for _si, shard in enumerate(shards, 1):
            await _emit_gen(... "generating" ...)
            try:
                fp_cases = await gen_agent.generate(
                    ..., functional_points=[shard], fp_scope=shard, ...,
                )
            except Exception as e:
                fp_cases = []
                shards_failed += 1
                if "fp_generate_failed" not in degraded_flags:
                    degraded_flags.append("fp_generate_failed")
                await _emit_gen(... "failed" ...)
                if len(shards) == 1:
                    break
            for c in fp_cases:
                c.setdefault("related_fp", fid)
            fp_cases_all.extend(fp_cases)
    # 合并 + 完成即写（主事件循环串行执行，全量覆盖收敛）
    resolved_cases.extend(fp_cases_all)
    checkpoint_groups.append({
        "fp_id": fid, "fp_name": fname,
        "case_count": len(fp_cases_all), "cases": list(fp_cases_all),
    })
    await _emit_gen(... "done" ...)
    try:
        await self._save_gen_checkpoint(task_id, checkpoint_groups,
                                        fingerprint=checkpoint_fingerprint)
    except Exception:
        pass  # 与原语义一致：写失败不阻断

pending = [fp for fp in fp_list if fp.get("id") not in completed_fps]
if _fp_conc <= 1:
    for fp in pending:  # 串行回退（行为与 v3.5.4 完全一致）
        await _gen_one_fp(fp)
else:
    _sem = asyncio.Semaphore(_fp_conc)
    results = await asyncio.gather(*[_gen_one_fp(fp) for fp in pending],
                                   return_exceptions=True)
    for fp, r in zip(pending, results):
        if isinstance(r, Exception):
            # gather 兜底：任务级异常视为该 FP 失败（不阻断，降级标记，续跑重试）
            if "fp_generate_failed" not in degraded_flags:
                degraded_flags.append("fp_generate_failed")
            failed_msg = ...  # 记录 fp 名
```

### 2.3 守卫测试（test_fp_concurrency.py）

1. **无失败放大**：FakeGenAgent 对指定 FP 抛错，其余 FP 正常完成；结果集含其余 FP 用例，degraded_flags 只含 fp_generate_failed 一项
2. **checkpoint 完成即写**：记录 _save_gen_checkpoint 调用次数 ≥ FP 数；每次调用的 fp_groups 单调递增
3. **进度事件 key 去重**：并发发射交错事件后 fp_subtasks 无重复 key；done 事件后 covered==total
4. **串行回退**：GEN_FP_CONCURRENCY=0/1 时走串行路径（行为断言）
5. **D-5 子批仍串行**：单 FP 多子批时，子批间无并发（FakeGenAgent 记录并发峰值）
6. **合并结果顺序**：resolved_cases 按原 fp_list 顺序重排（展示序一致）

### 2.4 验证计划

1. 单测：test_fp_concurrency.py 全绿
2. 回归：phase4（141）+ phase5（112）必跑；phase2/3/6 抽查（orchestrator 是共享模块）
3. 真实 E2E：dogfood v2.0 重跑一次生成，断言：
   - 耗时：生成阶段 ~50min → ≤30min
   - 质量：用例条数、覆盖率与串行版持平（±10%）
   - checkpoint：中断后续跑可用（可模拟 kill 生成任务后 resume）
4. 发布：v3.5.5（VERSION + package.json + commit + tag + push）

### 2.5 风险与对策

| 风险 | 对策 |
|---|---|
| 大 prompt 并发触发供应商限流 | 小 prompt 8 路实测全过；默认并发 3 起步，监控 llm_call_logs 429/超时率，异常降至 2 |
| 多路同时触发 403/401 → 供应商禁用 600s 波及全部路 | 既有机制；并发不引入新风险，但失败概率会叠加——监控即可 |
| bge embedding 并发 CPU 打满 | to_thread 默认线程池配额足够；实测若打满，L2 去重外套 Semaphore |
| 用例顺序变化影响 diff 查看习惯 | 合并时按原 fp_list 顺序重排 |

## 3. 待拍板项（已拍板 2026-10-08）

1. ~~并发度默认值~~ → **3**（小 prompt 8 路实测全过；设置页可随时调 1-5）
2. ~~复测合并~~ → **合并**（与 D-3/D-5 复测共用一次 dogfood E2E）

## 4. 实施记录（2026-10-08，v3.5.5）

### 4.1 改动清单

| 文件 | 改动 |
|---|---|
| `backend/app/config.py` | 新增 `GEN_FP_CONCURRENCY: int = 3`（默认值兜底） |
| `backend/app/api/settings.py` | ① 新增 `get_gen_fp_concurrency(session)`（kb_settings KV，key=`generation_fp_concurrency`，非法值回退默认）；② 新增 GET/PUT `/api/settings/generation`（GET 登录即可读，PUT 需管理员；Pydantic Field ge=1 le=5 边界校验） |
| `backend/app/core/orchestrator.py` | `_gen_node`：FP 串行循环 → `_gen_one_fp(fp)`（局部收集 + 完成回调合并/checkpoint）+ `asyncio.gather` + `Semaphore(并发度)`；并发度从 kb_settings 动态读取；`<=1 或单 FP` 走串行回退；gather 兜底捕获任务级异常 |
| `frontend/src/api/client.ts` | `settings.getGeneration / setGeneration` |
| `frontend/src/pages/Settings.tsx` | 「生成性能」卡片（下拉 1-5，1 显示"串行，最稳"，≥4 显示限流警告） |
| `backend/tests/test_fp_concurrency.py` | 8 项守卫测试（新建） |

### 4.2 守卫测试挖出的真实语义回归（已修复）

初版 `_gen_one_fp` 完成回调**无条件执行**——单分片 FP 生成失败后仍会 append checkpoint + 发 done，
失败 FP 被记成「完成 0 条」→ 续跑时被 `completed_fps` 跳过 → **永不重试**（v3.5.4 原语义：
单分片失败 break → 不写 checkpoint、不发 done）。修复：`single_shard_failed` 标记 +
完成回调入口 `if single_shard_failed: return`。这正是「失败路径用注入异常验证」纪律的价值。

### 4.3 验证结果

| 项 | 结果 |
|---|---|
| `test_fp_concurrency.py` | **8/8 全绿**（无失败放大 / checkpoint 完成即写单调递增 / 事件 key 去重 / 串行回退峰值=1 / Semaphore 限流峰值≤2 / 乱序合并 related_fp 兜底标注 / gather 隔离 / degraded_flags 去重） |
| 相邻单测（去重/发布策略/D-5/D-6） | 56 passed |
| phase4 回归（脚本式） | **186/186 通过** |
| phase5 回归（脚本式） | **112/112 通过** |
| 新端点全链路探针（ASGITransport） | GET 200 / PUT(2) 落库 / PUT(6) 422 / PUT(1) 串行模式提示 / orchestrator 同源读取一致 |
| `tsc --noEmit` + `npm run build` | 零错误，构建成功 |

### 4.4 待办

- [ ] dogfood v2.0 真实 E2E 复测（与 D-3/D-5 合并）：断言生成阶段 ≤30min、覆盖率提升、auto_allowed 生效、并发生成用例质量与串行持平
- [ ] git 提交（本批 + 此前三批修复，建议分 commit）→ 发版 v3.5.5
