"""用例质量权重（docs/14 二期 10/11，方向 B3）。

## 公式（设计 §B3）

```
weight = 0.35 × 人工采纳率        # feedback.accepted 的比例
       + 0.25 × 质量分            # 用例置信度（6 维审核的聚合结果）
       + 0.20 × 使用频率归一化      # execution_count 的 log 归一化
       + 0.20 × 通过率             # last_pass_rate（无数据时取中性值 0.5）
```

## ⚠️ 实测：当前数据下区分度很弱（2026-09-15，1704 条用例）

```
feedback.accepted：None 1287 / true 416 / false 1
execution_count > 0：仅 5 条
last_pass_rate > 0：0 条（从未写入——无执行结果回流）

按公式计算：min 0.225 / max 0.822 / mean 0.528 / **std 仅 0.058**
分布(0.1 桶)：{0.2:1, 0.4:152, **0.5:1133**, 0.6:410, 0.7:6, 0.8:2}
```

**4 个输入里有 2 个近似常量**（使用频率几乎全 0、通过率全为中性 0.5），
导致 66% 的用例挤在 0.5 桶——**区分度甚至低于 `confidence` 单独使用**。

## 因此：排序接入采用**候选集内归一化**，而不是直接用原始 weight

若直接算 `0.6×sim + 0.3×weight`，近常量的 weight 项只会给排序带来**噪声级扰动**，
同时把相似度的权重稀释到 0.6。改为**在候选集内 min-max 归一化**后：

- weight 近似常量时 → 归一化值 ≈ 0.5 常量 → **不扰动排序**（退化为 `sim + recency`）
- weight 有真实差异时 → 自动生效，无需人工开关

即**归一化本身就是方差守卫**，不需要额外的阈值判断。

## 何时会变得有区分度

- `last_pass_rate` 有数据（对接 CI/CD 或人工标记"此用例有效"）
- `execution_count` 积累（用例被检索命中注入 prompt 的次数）

两者都是**使用量驱动的**——系统用起来之后自然改善，无需改代码。
"""

from __future__ import annotations

import math

# 中性值：某输入无数据时使用，避免"没数据=差"的误判
NEUTRAL = 0.5

# 公式系数（设计 §B3，勿随意改——改了要重新做区分度校准）
W_ACCEPT = 0.35
W_QUALITY = 0.25
W_USAGE = 0.20
W_PASSRATE = 0.20

# 使用频率的**饱和基准**：被检索命中 50 次即视为"充分使用"，归一化到 1.0。
#
# 为什么用固定基准而不是"批次内最大值"：
#   用批次最大值会导致**同一条用例的 weight 随别人变化而变化**——
#   单条重算（如反馈后立即重算）与批量重算结果不一致，权重不可比较。
#   固定基准使 weight 成为**用例自身的属性**，任何时刻算都相同。
#
# ⚠️ 该值是**初始标定**：实测目前 execution_count>0 的仅 5 条（最高个位数），
#   尚无分布可依。待使用量积累后（有足够样本）应重新标定，方法与 §8.5 一致
#   （取分布分位数，而非拍脑袋）。
USAGE_SATURATION = 50


def _acceptance_rate(feedback: dict | None) -> tuple[float, str]:
    """人工采纳率。

    `feedback` 形如 `{"accepted": true, "comments": "...", "submitted_at": "..."}`。
    实测 1287/1704 条 feedback **没有 `accepted` 键**（只有评论），
    这类视为"无人工反馈"→ 中性 0.5，而不是 0（否则会被误判为"被否决"）。
    """
    if not isinstance(feedback, dict):
        return NEUTRAL, "无 feedback"
    if "accepted" not in feedback or feedback.get("accepted") is None:
        return NEUTRAL, "feedback 无 accepted 键"
    return (1.0 if feedback.get("accepted") else 0.0), "已人工反馈"


def _quality(confidence: float | None) -> float:
    """质量分：用例置信度（0~1）。无值取中性。"""
    try:
        c = float(confidence)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return NEUTRAL
    return c if c > 0 else NEUTRAL


def _usage_norm(execution_count: int | None, saturation: int = USAGE_SATURATION) -> float:
    """使用频率归一化：log(1+ec) / log(1+saturation)，上限截断到 1.0。

    用**固定饱和基准**而非批次最大值——见 `USAGE_SATURATION` 的说明
    （保证单条重算与批量重算结果一致，weight 是用例自身属性）。
    """
    ec = max(int(execution_count or 0), 0)
    if ec <= 0:
        return 0.0
    denom = math.log(1 + max(int(saturation), 1))
    return min(1.0, math.log(1 + ec) / denom) if denom > 0 else 0.0


def _pass_rate(last_pass_rate: float | None) -> float:
    """通过率：无数据（0 或 None）取中性 0.5。

    注意：**不能把"无数据"当作 0**——那会让所有尚未执行的用例被判为低质。
    """
    try:
        p = float(last_pass_rate)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return NEUTRAL
    return p if p > 0 else NEUTRAL


def calc_case_weight(
    feedback: dict | None,
    confidence: float | None,
    execution_count: int | None = 0,
    last_pass_rate: float | None = None,
) -> tuple[float, dict]:
    """计算单条用例的 weight（**只依赖该用例自身**，任何时刻算结果相同）。

    Returns:
        (weight, 明细) —— 明细含四个分项，便于排查"为什么这条权重低"
    """
    acc, acc_src = _acceptance_rate(feedback)
    qual = _quality(confidence)
    usage = _usage_norm(execution_count)
    pr = _pass_rate(last_pass_rate)

    weight = (W_ACCEPT * acc + W_QUALITY * qual
              + W_USAGE * usage + W_PASSRATE * pr)
    return round(weight, 4), {
        "acceptance": round(acc, 4), "acceptance_src": acc_src,
        "quality": round(qual, 4),
        "usage": round(usage, 4),
        "pass_rate": round(pr, 4),
    }


# 候选集内 weight 的**最小有效极差**。
#
# ⚠️ 为什么必须有这个守卫：min-max 归一化是**线性拉伸**，
# 拉伸倍数 = 1/极差。候选集常常是同质的（同一需求的用例置信度相近），
# 此时极差可能只有 0.02，归一化会把 0.01 的噪声差异放大 **50 倍**成满量程，
# 让噪声主导排序。
#   - 实测全局极差 0.597（0.225~0.822）→ 拉伸仅 1.68×，可接受
#   - 但同质候选集（如全为同一需求）极差可能 <0.05 → 必须抑制
#
# 取值 0.10 的依据：公式中人工反馈翻转（最明确的信号）带来 0.35 差异，
# 置信度跨一个档位（0.6→1.0）带来 0.10 差异——即 0.10 ≈ "一个可辨识的信号档"。
# 低于此值视为噪声，不让其影响排序。
MIN_WEIGHT_SPREAD = 0.10


def normalize_weights(
    weights: list[float], min_spread: float = MIN_WEIGHT_SPREAD,
) -> list[float]:
    """候选集内 min-max 归一化（带**方差守卫**）。

    当候选集的 weight 极差 < `min_spread` 时，视为"无有效差异"，
    返回全 0.5 常量 —— 使 weight 项**不影响排序**（退化为纯相似度排序），
    避免把噪声放大成满量程。

    Returns:
        与输入等长的归一化列表；无有效差异或为空时返回全 0.5。
    """
    if not weights:
        return []
    lo, hi = min(weights), max(weights)
    if hi - lo < max(min_spread, 1e-9):
        return [0.5] * len(weights)
    return [(w - lo) / (hi - lo) for w in weights]


def rank_score(
    similarity: float, weight_norm: float, recency: float = 0.5,
) -> float:
    """检索排序分（设计 §B3）。

    `rank_score = 0.6 × 语义相似度 + 0.3 × 归一化weight + 0.1 × 新近度`

    `weight_norm` 必须来自 `normalize_weights`（候选集内归一化），
    否则近常量的 weight 会稀释相似度而不带来有效区分。
    """
    return 0.6 * similarity + 0.3 * weight_norm + 0.1 * recency


async def recompute_all_weights(session=None) -> dict:
    """全量重算并回写 `test_cases.weight`。

    供人工调用或周期任务使用（设计 §B3 的"定期重算"）。

    Returns:
        dict: {total, updated, distribution, std, mean}
    """
    import statistics

    from sqlalchemy import select, text

    from app.db.database import async_session
    from app.db.models import TestCase

    async def _run(s) -> dict:
        rows = (await s.execute(
            select(TestCase.id, TestCase.feedback, TestCase.confidence,
                   TestCase.execution_count, TestCase.last_pass_rate)
        )).all()

        computed: list[tuple[str, float]] = []
        for cid, fb, conf, ec, lpr in rows:
            w, _ = calc_case_weight(fb, conf, ec, lpr)
            computed.append((cid, w))

        if computed:
            # 逐条更新（避免超长参数列表）；量大时可改批量 executemany
            for cid, w in computed:
                await s.execute(
                    text("UPDATE test_cases SET weight = :w WHERE id = :i"),
                    {"w": w, "i": cid},
                )
            await s.commit()

        ws = [w for _, w in computed]
        return {
            "total": len(ws),
            "updated": len(ws),
            "mean": round(statistics.mean(ws), 4) if ws else 0.0,
            "std": round(statistics.pstdev(ws), 4) if len(ws) > 1 else 0.0,
            "min": round(min(ws), 4) if ws else 0.0,
            "max": round(max(ws), 4) if ws else 0.0,
        }

    if session is not None:
        return await _run(session)
    async with async_session() as s:
        return await _run(s)
