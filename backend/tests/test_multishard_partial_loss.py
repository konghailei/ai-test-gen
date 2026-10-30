"""多分片部分失败不得静默丢用例（v4.1.9）。

缺陷
----
要点数 > 40 的大 FP 会按要点类型二次分片（happy_path / alt_flow / rule / data）。
原实现里 `single_shard_failed` **只在 `len(shards) == 1` 时置位**：

    if len(shards) == 1:
        single_shard_failed = True
        break

于是多分片场景下某个子批失败时，该 FP 仍会走完成回调、被 checkpoint 记为
「已完成」——而 checkpoint 里只有成功子批的用例。续跑时 `completed_fps`
包含该 FP，失败子批的要点**永久不再重试**；UI 还会收到一条
`status=done, cases=<成功子批数>` 的事件，用户看到的是「生成完成：6 条」，
不知道本该是 8 条。

修复语义
--------
- 任一子批失败 → `failed_shard_count += 1`（无条件，与分片数无关）
- `should_write_fp_checkpoint(0) is True`，否则 False → 整 FP 不写 checkpoint
- 本次仍产出成功子批的用例（resolved_cases.extend），避免「4 个成功 3 个也全丢」

测试方式说明
------------
`_gen_one_fp` 是 `run_pipeline` 内的闭包，做行为级测试需要构造完整 PipelineState
并 mock 掉 gen_agent / checkpoint / SSE emitter，成本远高于收益。这里用：
1. 纯函数单测锁语义；
2. 源码结构断言锁**调用形态**（计数在条件之前、完成回调里既 extend 又 return）。
两条一起才能防住「改回条件置位」这种回退。
"""

from __future__ import annotations

import inspect

from app.core.orchestrator import should_write_fp_checkpoint


# ---------------------------------------------------------------------------
# 1. 纯函数语义
# ---------------------------------------------------------------------------

def test_zero_failed_shards_allows_checkpoint():
    assert should_write_fp_checkpoint(0) is True


def test_any_failed_shard_blocks_checkpoint():
    """多分片部分失败（1 个失败、3 个成功）也必须阻止写 checkpoint。"""
    assert should_write_fp_checkpoint(1) is False
    assert should_write_fp_checkpoint(3) is False


# ---------------------------------------------------------------------------
# 2. 调用形态哨兵（防回退成「只在单分片时置位」）
# ---------------------------------------------------------------------------

def test_failure_counter_is_unconditional():
    """计数必须发生在 `if len(shards) == 1` **之前**——即不受分片数限制。"""
    from app.core import orchestrator as orch

    src = inspect.getsource(orch)

    # 失败日志是这一段唯一的锚点
    anchor = src.index('生成失败"')
    seg = src[anchor: anchor + 1600]

    assert "failed_shard_count += 1" in seg, "失败分支必须累加计数"
    assert seg.index("failed_shard_count += 1") < seg.index("if len(shards) == 1:"), (
        "计数必须在分片数判断之前，否则多分片场景又不会置位"
    )


def test_partial_failure_still_emits_cases_but_returns():
    """部分失败分支：既要 extend 成功子批用例，又要 return（不写 checkpoint）。"""
    from app.core import orchestrator as orch

    src = inspect.getsource(orch)
    hook = src.index("if not should_write_fp_checkpoint(failed_shard_count):")
    seg = src[hook: hook + 900]

    assert "resolved_cases.extend(fp_cases_all)" in seg, (
        "部分失败时本次仍应产出成功子批的用例，避免整 FP 白跑"
    )
    assert "return" in seg, "必须 return，不能落到 checkpoint 写入"


def test_reactivate_vector_failure_has_outbox_fallback():
    """恢复活动时向量删除失败必须有补偿（scope 出箱），否则残留 archived 向量。"""
    from app.api import products

    src = inspect.getsource(products.reactivate_project)
    assert 'created_by="reactivate_fallback"' in src, (
        "批量 + 逐条清理都失败时，必须补一条 scope 出箱让 relay 重试"
    )
    assert "aggregate_type=\"scope\"" in src
