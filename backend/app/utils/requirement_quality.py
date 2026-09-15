"""需求结构完整度评估（docs/14 二期 A2）。

## 目标

把"需求有没有写全"变成**可量化、可解释、可复现**的分数，供：
- **A3**：`missing` 直接作为澄清问题的种子（缺口驱动，而非等 LLM 发挥）
- **A4**：门禁引用该分数（先只做 Warning）

## 口径（依据 2026-09-15 真实数据实测校准，非照搬方案原表）

实测样本：17 条有 SRR 的需求 + 60 条 FP 子 SRR。**方案原表有 4 处需修正**：

| 原表字段 | 实测 | 修正 |
|---|---|---|
| `business_rules`（顶层，10%） | 顶层仅 **1/17**；`cross_fp_interactions.shared_business_rules` **14/17**；FP 子 SRR `business_rules` **60/60** | 改评 **FP 层 + cross_fp** |
| `data_constraints`（顶层，10%） | 顶层 **0/17**；FP 子 SRR **60/60** | 改评 **FP 层**（`assumptions` 作兜底） |
| `acceptance_criteria`（顶层，10%） | 顶层 **0/17**，且 **FP 子 SRR 也无此字段** | **剔除**（两处都取不到，计入会恒判缺失） |
| `api_definitions`（顶层，10%） | 顶层仅 **3/17** | 保留但**加适用性判断** + 降权 |
| `dependencies` 3/17、`ui_rules` 2/17 | 命中率极低 | `dependencies` 降权；**`ui_rules` 只报告不计分** |

> 为什么必须实测：方案原表若照抄，30% 权重恒判缺失（3 个字段）+ 多个字段普遍扣分，
> 会产出"所有需求都很差"的分数与一堆**伪澄清缺口**。
> 这正是 docs/14 §8.3 列的二期前置条件。

## 分层

- **核心档（70 分）**：恒适用，缺失即扣——需求的主体信息
- **补充档（30 分）**：低权重；`api_definitions` 支持**适用性豁免**（不涉及系统交互时不扣分）

`score = 已填权重 / 适用权重`，范围 0~1。

## 校准结果（2026-09-15，21 条真实需求）

```
分布：最低 0.208 / 最高 0.960 / 均值 0.729
< 0.35: 1/21 (5%)      ← 该条是"知识问答纠正"短记录，非完整需求，判低分合理
< 0.60: 3/21 (14%)
< 0.80: 13/21 (62%)
缺失频次：dependencies 86% / api_definitions 76% / preconditions 71% /
          non_functional 62% / data_model 57% / alternate_flows 43% / user_roles 38%
核心档缺失率：happy_path_steps 5% / business_rules 5%   ← 核心信息基本都填了
```

**分布健康**：均值 0.729 说明"需求普遍很简略"的担忧不成立；
分数确实能区分（0.208 的短问答记录 vs 0.960 的完整迭代文档）。

> ⚠️ **`dependencies`(86%) 与 `api_definitions`(76%) 缺失率过高，属"普遍不适用"而非"普遍漏写"**。
> 它们权重低（2 / 4）对分数影响有界，但**直接拿去驱动澄清会产生伪问题**。
> 因此 **A3 取缺口时只取 `weight >= 5` 的项**（排除这两项），并限制条数——
> 这个过滤规则写在 A3 的调用处，不在本模块里，以保持本模块"纯评估"的职责。
"""

from __future__ import annotations

from typing import Any

# ============================================================================
# 维度定义
# ============================================================================
# 每项：(key, 中文名, 权重, 取数路径说明)
# 取数由 _DIMENSION_PROBES 中的函数完成（支持"任一满足"的多路径）
CORE_DIMENSIONS: list[tuple[str, str, int]] = [
    ("title", "需求标题", 10),
    ("summary", "需求摘要", 10),
    ("happy_path_steps", "主流程步骤", 15),
    ("alternate_flows", "异常/分支流程", 10),
    ("business_rules", "业务规则", 15),
    ("non_functional", "非功能需求", 10),
]

SUPPLEMENT_DIMENSIONS: list[tuple[str, str, int]] = [
    ("preconditions", "前置条件", 6),
    ("data_constraints", "数据约束", 8),
    ("user_roles", "用户角色", 5),
    ("data_model", "数据模型", 5),
    ("api_definitions", "接口定义", 4),      # 支持适用性豁免
    ("dependencies", "依赖关系", 2),
]

# 只报告、不计分（命中率过低，计分会引入噪声）
REPORT_ONLY_DIMENSIONS: list[tuple[str, str]] = [
    ("ui_rules", "UI 规则"),
    ("assumptions", "假设与前提"),
    ("legacy_data_impact", "存量数据影响"),
]


def _nonempty(v: Any) -> bool:
    """非空判定：None / 空串 / 空容器 / 全空容器 均视为空。"""
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple, set)):
        return len(v) > 0
    if isinstance(v, dict):
        return any(_nonempty(x) for x in v.values())
    return True


def _any_fp_has(fp_srrs: list[dict], key: str) -> bool:
    return any(_nonempty((f or {}).get(key)) for f in (fp_srrs or []))


def _dig(obj: dict, path: str) -> Any:
    cur: Any = obj
    for p in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _probe(key: str, total: dict, fp_srrs: list[dict]) -> tuple[bool, str]:
    """返回 (是否已填, 取数位置说明)。"""
    if key == "title":
        return _nonempty(_dig(total, "requirement.title")), "requirement.title"
    if key == "summary":
        return _nonempty(_dig(total, "requirement.summary")), "requirement.summary"
    if key == "happy_path_steps":
        return _nonempty(_dig(total, "flows.happy_path.steps")), "flows.happy_path.steps"
    if key == "alternate_flows":
        return _nonempty(_dig(total, "flows.alternate_flows")), "flows.alternate_flows"
    if key == "business_rules":
        # 实测：FP 子 SRR 60/60 覆盖；总 SRR 侧迁到 cross_fp_interactions.shared_business_rules
        hit = (_any_fp_has(fp_srrs, "business_rules")
               or _nonempty(_dig(total, "cross_fp_interactions.shared_business_rules")))
        return hit, "FP[*].business_rules 或 cross_fp_interactions.shared_business_rules"
    if key == "non_functional":
        return _nonempty(total.get("non_functional")), "non_functional"
    if key == "preconditions":
        return _nonempty(total.get("preconditions")), "preconditions"
    if key == "data_constraints":
        # 实测：顶层 0/17；FP 子 SRR 60/60；assumptions 作兜底
        hit = _any_fp_has(fp_srrs, "data_constraints") or _nonempty(total.get("assumptions"))
        return hit, "FP[*].data_constraints 或 assumptions"
    if key == "user_roles":
        return _nonempty(total.get("user_roles")), "user_roles"
    if key == "data_model":
        return _nonempty(total.get("data_model")), "data_model"
    if key == "api_definitions":
        return _nonempty(total.get("api_definitions")), "api_definitions"
    if key == "dependencies":
        return _nonempty(total.get("dependencies")), "dependencies"
    return False, key


def _api_applicable(total: dict) -> tuple[bool, str]:
    """`api_definitions` 的适用性判断。

    实测命中率仅 3/17——若恒要求，会把"不涉及系统交互"的需求误判为不完整。
    判定信号（简单、可解释）：
      - `legacy_data_impact` 非空（涉及存量数据迁移/兼容 → 通常有接口）
      - `cross_fp_interactions` 非空（跨功能点交互 → 通常有接口）
      - `api_definitions` 本身非空（已填 → 自然适用）
    """
    if _nonempty(total.get("api_definitions")):
        return True, "已填写 api_definitions"
    if _nonempty(total.get("legacy_data_impact")):
        return True, "存在 legacy_data_impact（涉及存量数据/系统交互）"
    if _nonempty(total.get("cross_fp_interactions")):
        return True, "存在 cross_fp_interactions（涉及跨功能点交互）"
    return False, "未发现系统交互信号（legacy_data_impact / cross_fp_interactions 均空）"


def calc_structure_score(
    total_srr: dict | None,
    fp_srrs: list[dict] | None = None,
) -> dict:
    """计算需求结构完整度。

    Args:
        total_srr: 总 SRR（顶层），评结构性字段
        fp_srrs: 功能点子 SRR 列表，评业务规则类字段（`business_rules`/`data_constraints`
            实测只在 FP 层 100% 存在，见模块 docstring）

    Returns:
        dict:
            - `score` (float 0~1)：已填权重 / 适用权重
            - `filled` / `missing`：**`missing` 直接供 A3 澄清使用**
            - `not_applicable`：因不适用而豁免的维度
            - `report_only`：只报告不计分的维度命中情况
            - `detail`：逐维度明细（供前端/报告展示）
    """
    total = total_srr if isinstance(total_srr, dict) else {}
    fps = [f for f in (fp_srrs or []) if isinstance(f, dict)]

    filled: list[dict] = []
    missing: list[dict] = []
    not_applicable: list[dict] = []
    detail: dict[str, dict] = {}

    api_ok, api_reason = _api_applicable(total)

    for key, label, weight in CORE_DIMENSIONS + SUPPLEMENT_DIMENSIONS:
        if key == "api_definitions" and not api_ok:
            not_applicable.append({"key": key, "label": label, "weight": weight,
                                   "reason": api_reason})
            detail[key] = {"hit": None, "weight": weight, "applicable": False,
                           "label": label, "reason": api_reason}
            continue
        hit, where = _probe(key, total, fps)
        detail[key] = {"hit": hit, "weight": weight, "applicable": True,
                       "label": label, "where": where}
        if hit:
            filled.append({"key": key, "label": label, "weight": weight})
        else:
            missing.append({"key": key, "label": label, "weight": weight,
                            "hint": where})

    applicable_weight = sum(d["weight"] for d in detail.values() if d["applicable"])
    filled_weight = sum(d["weight"] for d in detail.values()
                        if d["applicable"] and d["hit"])
    score = round(filled_weight / applicable_weight, 4) if applicable_weight else 0.0

    report_only = []
    for key, label in REPORT_ONLY_DIMENSIONS:
        hit = _nonempty(total.get(key))
        report_only.append({"key": key, "label": label, "hit": hit})

    # missing 按权重降序 —— 澄清时优先问影响最大的
    missing.sort(key=lambda x: -x["weight"])

    return {
        "score": score,
        "filled_weight": filled_weight,
        "applicable_weight": applicable_weight,
        "filled": filled,
        "missing": missing,
        "not_applicable": not_applicable,
        "report_only": report_only,
        "detail": detail,
    }


def summarize_structure(score_info: dict, max_items: int = 3) -> str:
    """生成一句话摘要（供日志/前端提示）。"""
    if not score_info:
        return ""
    pct = int(round(score_info.get("score", 0) * 100))
    miss = score_info.get("missing") or []
    if not miss:
        return f"结构完整度 {pct}%（无缺口）"
    top = "、".join(m["label"] for m in miss[:max_items])
    more = f" 等 {len(miss)} 项" if len(miss) > max_items else ""
    return f"结构完整度 {pct}%，建议补充：{top}{more}"
