# -*- coding: utf-8 -*-
"""评估统计口径（P2-3，依据《深入理解 AI Agent》6.4/6.7）。

三个口径，各答一个问题：
- pass_at_k(n, c, k)   —— "k 次机会能不能做到"（探索性任务用）
- pass_hat_k(n, c, k)  —— "k 次是否全部稳定成功"（回归测试必须用这个）
- mcnemar_exact(b, c)  —— 两个配置配对比较的显著性（比独立相减灵敏得多）
- wilson_ci(p, n)      —— 单次成功率的置信区间（分差小于噪声带宽不做决策）

纯 python 实现（math.comb），无 scipy 依赖。
"""
from __future__ import annotations

from math import comb, sqrt


def pass_at_k(n: int, c: int, k: int) -> float:
    """Pass@k 无偏估计（Codex 论文口径）。

    Args:
        n: 总采样次数
        c: 成功次数
        k: 抽取次数（1<=k<=n）

    Returns:
        float: k 次采样中至少一次成功的概率估计
    """
    if n <= 0 or k <= 0:
        raise ValueError("n 和 k 必须为正整数")
    if c < 0 or c > n:
        raise ValueError("c 必须在 [0, n] 内")
    k = min(k, n)
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    """Pass^k 估计：k 次采样全部成功的概率（稳定性/回归口径）。

    单次 60% 时 Pass^5 仅 7.8%——回归测试看这个指标，不看 Pass@k。
    """
    if n <= 0 or k <= 0:
        raise ValueError("n 和 k 必须为正整数")
    if c < 0 or c > n:
        raise ValueError("c 必须在 [0, n] 内")
    k = min(k, n)
    if c < k:
        return 0.0
    return comb(c, k) / comb(n, k)


def mcnemar_exact(b: int, c: int) -> float:
    """McNemar 配对精确检验（双侧 p 值）。

    配对比较两配置在同一批用例上的结果：
        b = A 成功且 B 失败的用例数
        c = B 成功且 A 失败的用例数
    两者越接近 p 越大（无显著差异）。p < 0.05 才值得做切换决策。

    比独立成功率相减灵敏得多：扣除了题目难易的共同噪声，
    只看两配置结果不一致的用例。
    """
    n = b + c
    if n == 0:
        return 1.0
    if b < 0 or c < 0:
        raise ValueError("b/c 必须非负")
    tail = sum(comb(n, i) for i in range(0, min(b, c) + 1)) / (2 ** n)
    return min(1.0, 2.0 * tail)


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 分数区间（比正态近似在小样本下更稳）。

    返回 (lower, upper)。用于判断"分差是否小于噪声带宽"：
    两配置的 Wilson 区间重叠 → 差异不可作为决策依据。
    """
    if n <= 0:
        raise ValueError("n 必须为正整数")
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def cohens_kappa(rater_a: list, rater_b: list) -> float:
    """Cohen's kappa：两位评分者在同一批样本上的一致率（剔除随机一致）。

    用于 LLM-as-Judge 校准（P2-4）：先建 100-200 条金标（人工标注），
    kappa > 0.7 才允许 judge 用于大规模评审；judge 模型或 rubric 更新后须重校。
    """
    if len(rater_a) != len(rater_b) or not rater_a:
        raise ValueError("两位评分者的样本必须等长且非空")
    n = len(rater_a)
    po = sum(1 for a, b in zip(rater_a, rater_b) if a == b) / n
    cats = set(rater_a) | set(rater_b)
    pe = sum(
        (sum(1 for x in rater_a if x == cat) / n) * (sum(1 for x in rater_b if x == cat) / n)
        for cat in cats
    )
    if pe >= 1.0:
        return 1.0 if po == 1.0 else 0.0
    return (po - pe) / (1 - pe)
