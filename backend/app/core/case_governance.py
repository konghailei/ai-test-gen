"""用例库治理扫描（docs/14 方向 B / B4）。

## ⚠️ 先说结论：三条判据在当前数据下**全部命中 0 条**

实测（2026-09-15，1704 条用例）：

| 判据（设计 §B4） | 实测命中 | 原因 |
|---|---|---|
| `weight < 0.3` 且 `execution_count = 0` 的 approved | **0 条** | approved 用例的 weight 最低也在 0.3 以上（公式 4 个输入中 2 个尚无数据，权重集中在 0.5 档） |
| `execution_count = 0` 且入库超 N 月 | **0 条** | **全部用例入库不足 1 个月**（最早 2026-08-28） |
| 语义相似度 > 0.9 的近似重复对 | **0 条** | 实测「最像的另一条用例」相似度 **max=0.890**，`>=0.90` 为 0；p50 仅 0.826 |

**因此本模块当前的作用是"能力就位 + 如实报告空结果"，而不是"产出一份有内容的清单"。**
不硬造一个看起来有用、实际全空的功能——那正是本项目反复纠正的"空洞通过"。

## 三条判据何时才会真正有数据

- `weight < 0.3`：需 `last_pass_rate` / `execution_count` 有数据后权重才会拉开差距
  （见 `app/utils/case_weight.py` 的校准说明）
- 入库超 N 月：需用例库自然积累（当前库龄 < 1 月）
- 近似重复：阈值须**按届时的实测分布重新标定**，不能沿用 0.9

## 阈值取值说明（不拍脑袋）

`DUP_SIMILARITY` 取 **0.92**（高于设计建议的 0.9），依据：
实测同一产品内「最像的另一条用例」相似度 **p50=0.826、max=0.890**——
同产品用例本就话题相近，0.9 以下几乎无区分力。
取 0.92 意味着"比实测最高值还高"，即**当前宁可不报，也不报噪声**；
待库增长、分布右移后再按分位数重新标定。

## 安全边界

本模块**只扫描、只报告**，不做任何写入。
归档（写 `archived_at`）会使用例**退出 KB 检索**（受 `KB_CASES_ARCHIVED_ONLY` 约束），
属不可逆的效果变更，必须由人显式确认后另行执行——不在此模块内自动做。
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

# ============================================================================
# 阈值（均经实测校准，改前请先看模块 docstring 的依据）
# ============================================================================
# 低权重阈值：设计建议 0.3。保留该值——它是对"该复核了"的合理定义，
# 只是当前 approved 用例都高于它（不是阈值错，是数据还没分化）。
LOW_WEIGHT = 0.3

# 长期未命中判定：入库超过该月数且从未被复用
STALE_MONTHS = 6

# 近似重复的相似度阈值（实测校准，见 docstring）
DUP_SIMILARITY = 0.92

# 扫描时抽样多少条做近似重复检测（全量两两比对成本过高）
DUP_SAMPLE = 80


async def scan_case_library(
    session=None,
    *,
    product_id: str | None = None,
    low_weight: float = LOW_WEIGHT,
    stale_months: int = STALE_MONTHS,
    dup_similarity: float = DUP_SIMILARITY,
    dup_sample: int = DUP_SAMPLE,
    include_dup_scan: bool = False,
) -> dict[str, Any]:
    """扫描用例库，返回三个维度的治理候选（**只读**）。

    Args:
        include_dup_scan: 是否做近似重复检测。默认 False——
            该检测需对每条样本调用一次向量检索，**耗时较长**
            （实测 60 条约 10s），不适合放进周期任务。

    Returns:
        dict: 含三个维度候选与**如实标注的命中情况说明**。
    """
    from sqlalchemy import func, select, text

    from app.db.database import async_session
    from app.db.models import TestCase

    async def _run(s) -> dict[str, Any]:
        def _f(stmt):
            return stmt.where(TestCase.product_id == product_id) if product_id else stmt

        # ① 低权重且从未复用
        low_weight_cases = [
            {"id": r[0], "title": r[1], "weight": round(float(r[2] or 0), 4),
             "execution_count": int(r[3] or 0)}
            for r in (await s.execute(
                _f(select(TestCase.id, TestCase.title, TestCase.weight,
                          TestCase.execution_count))
                .where(TestCase.status == "approved",
                       TestCase.weight < low_weight,
                       TestCase.execution_count == 0)
                .limit(100)
            )).all()
        ]

        # ② 长期未命中（入库超 N 月且从未复用）
        stale_cases = [
            {"id": r[0], "title": r[1], "created_at": str(r[2])}
            for r in (await s.execute(
                _f(select(TestCase.id, TestCase.title, TestCase.created_at))
                .where(TestCase.status == "approved",
                       TestCase.execution_count == 0,
                       TestCase.created_at < func.now() - text(
                           f"interval '{int(stale_months)} months'"))
                .limit(100)
            )).all()
        ]

        # 库龄（用于解释"为什么长期未命中会是空"）
        age = (await s.execute(
            _f(select(func.min(TestCase.created_at), func.max(TestCase.created_at)))
        )).first()
        total = (await s.execute(
            _f(select(func.count(TestCase.id)))
        )).scalar() or 0

        return {
            "total_cases": total,
            "library_age": {"earliest": str(age[0]) if age and age[0] else "",
                            "latest": str(age[1]) if age and age[1] else ""},
            "low_weight": {"threshold": low_weight, "count": len(low_weight_cases),
                           "cases": low_weight_cases},
            "stale": {"months": stale_months, "count": len(stale_cases),
                      "cases": stale_cases},
        }

    if session is not None:
        report = await _run(session)
    else:
        async with async_session() as s:
            report = await _run(s)

    # ③ 近似重复（可选，耗时）
    if include_dup_scan:
        report["duplicates"] = await _scan_duplicates(
            product_id=product_id, threshold=dup_similarity, sample=dup_sample
        )
    else:
        report["duplicates"] = {
            "skipped": True,
            "threshold": dup_similarity,
            "note": "近似重复检测默认跳过（每条样本需一次向量检索，耗时较长）；"
                    "需要时传 include_dup_scan=True。",
        }

    # 如实标注：当前命中为空时，说明原因而不是让用户以为"库很健康"
    empties = []
    if report["low_weight"]["count"] == 0:
        empties.append(
            f"低权重（weight<{low_weight}）候选为 0：approved 用例的权重集中在 0.5 档"
            "（公式 4 个输入中 2 个尚无数据），尚未分化出低权重群体"
        )
    if report["stale"]["count"] == 0:
        empties.append(
            f"长期未命中（超 {stale_months} 月且从未复用）候选为 0："
            f"用例库最早入库 {report['library_age']['earliest']}，**库龄不足**"
        )
    report["empty_reason"] = empties
    report["_disclaimer"] = (
        "本报告为空**不代表用例库健康**，可能只是库龄不足或阈值尚未适配当前分布。"
        "详见 app/core/case_governance.py 模块说明。"
    )

    logger.info(
        "[CaseGovernance] 扫描完成: total={}, 低权重={}, 长期未命中={}",
        report["total_cases"], report["low_weight"]["count"], report["stale"]["count"],
    )
    return report


async def _scan_duplicates(
    *, product_id: str | None, threshold: float, sample: int,
) -> dict[str, Any]:
    """近似重复检测：抽样已核准用例，找「最像的另一条」并比对阈值。

    ⚠️ 实测「最像的另一条用例」相似度 p50=0.826、max=0.890 ——
    同产品用例本就话题相近，**相似度对"重复"的区分力有限**。
    因此该结果只作**线索**，不应作为自动合并的依据。
    """
    from sqlalchemy import select, text

    from app.db.database import async_session
    from app.db.models import TestCase

    async with async_session() as s:
        stmt = select(TestCase.id, TestCase.title, TestCase.case_json).where(
            TestCase.status == "approved"
        )
        if product_id:
            stmt = stmt.where(TestCase.product_id == product_id)
        rows = (await s.execute(stmt.limit(sample))).all()

    from app.db.milvus_store import vector_store
    # v4.0.1（S1 从根治理）：同步 Milvus 调用一律不得在事件循环里直调。
    # 首次调用会真正建连 + `_ensure_collections()`（RPC），故同样隔离。
    await asyncio.to_thread(vector_store._ensure_connected)

    pairs, sims = [], []
    for cid, title, cj in rows:
        cj = cj if isinstance(cj, dict) else {}
        q = (str(title or "") + " " + " ".join(
            str(st.get("action", "")) + str(st.get("expected", ""))
            for st in (cj.get("steps") or [])[:3]
        )).strip()
        if not q:
            continue
        try:
            res = await asyncio.to_thread(
                vector_store.search_similar_cases, q, top_k=3,
                product_id=product_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[CaseGovernance] 相似检索失败: {}", str(e)[:120])
            continue
        others = [r for r in res if str(r.get("id")) != str(cid)]
        if not others:
            continue
        sim = float(others[0].get("similarity") or 0)
        sims.append(sim)
        if sim >= threshold:
            pairs.append({"similarity": round(sim, 4), "id": cid,
                          "title": str(title or ""),
                          "other_id": str(others[0].get("id") or "")})

    sims.sort()
    return {
        "threshold": threshold,
        "sampled": len(rows),
        "compared": len(sims),
        "pair_count": len(pairs),
        "similarity_stats": {
            "min": round(sims[0], 4) if sims else None,
            "p50": round(sims[len(sims) // 2], 4) if sims else None,
            "max": round(sims[-1], 4) if sims else None,
        },
        "pairs": pairs[:50],
        "note": (
            "同产品用例话题相近，相似度对『重复』的区分力有限"
            "（实测 p50≈0.83、max≈0.89）。本结果**只作线索**，"
            "不应作为自动合并依据——合并用例需人工判断。"
        ),
    }
