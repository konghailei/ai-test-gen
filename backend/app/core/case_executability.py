# -*- coding: utf-8 -*-
"""用例可执行性分级（v2.49，R4 实测驱动）。

## 为什么分级而不是"一刀切强约束"

R4 实测：需求文档提供 API 路径 44 处，但 UI 页面/菜单/入口仅 7 处、元素定位 0 处；
而用例界面型步骤占 45%。**需求没给的信息，强约束只能逼模型编造**——编造出的
"具体"比诚实标注的"模糊"更危险（测试人员照着找不到会误判为系统缺陷）。

因此本模块按**证据**分级，而不是按"具体性"打分：

| 级别 | 判定 | 处置 |
|---|---|---|
| A 可直接执行 | 无缺口标注、无编造元素、断言可观测、步骤可定位、有数据 | 可交付执行 |
| B 需补细节 | 无编造，但断言抽象 / 界面步骤无定位 / 缺 test_data | 人工补 1-2 处 |
| C 待需求澄清 | 显式标注缺口，或检出**编造元素**（路径无依据等） | 回喂澄清，不要求可执行 |

## 编造率（硬指标）

`fabricated_rate` = 含编造元素的用例数 / 受检用例数。目标为 0：
宁可标 C 级，也不产出"看起来能跑但没依据"的用例。

全部为确定性规则（正则 + 契约比对），不调 LLM，可单测。
"""
from __future__ import annotations

import json
import re

from app.core.case_path_lint import _clean_path, _iter_case_paths, allowed_path_patterns

GRADE_A, GRADE_B, GRADE_C = "A", "B", "C"

# 缺口标注（生成侧被要求写入，见 prompts/gen_agent.py「信息不足时禁止臆造」）
GAP_MARKERS = ("缺口:", "缺口：", "待补充", "待确认", "TODO", "信息不足")

# 抽象命名空间（断言 target 顶层），只有 ≤2 段时视为"不可观测"
ABSTRACT_NS = {
    "response", "http", "request", "db", "ui", "system", "api", "page",
    "query", "result", "data",
}

UI_SIG = re.compile(r"(点击|输入|选择|拖拽|页面|按钮|弹窗|上传|下拉|勾选|导航|刷新)")
LOCATOR = re.compile(
    r"[「\"'《]([^」\"'》]{2,30})[」\"'》]"
    r"|(?:页面|按钮|输入框|输入栏|下拉框|菜单|标签页|弹窗|对话框|字段|复选框|单选框|开关|图标|列表项)"
)

# 错误码样式（大写枚举 / 4xx / 5xx）；仅记录，不单独判 C（可能是通识）
ERRCODE = re.compile(r"\b([A-Z][A-Z0-9_]{3,}|[45]\d\d)\b")


def _case_blob(case: dict) -> str:
    parts = [str(case.get("title") or ""), str(case.get("expected") or ""), str(case.get("notes") or "")]
    for s in case.get("steps") or []:
        if isinstance(s, dict):
            parts.append(str(s.get("action") or ""))
            parts.append(str(s.get("expected") or ""))
        elif isinstance(s, str):
            parts.append(s)
    return " ".join(parts)


def _has_gap_marker(blob: str) -> bool:
    return any(m in blob for m in GAP_MARKERS)


def _abstract_targets(case: dict) -> tuple[int, int]:
    """返回 (抽象断言数, 断言总数)。抽象 = 顶层属抽象命名空间且段数 ≤2。"""
    abstract = total = 0
    for a in case.get("assertions") or []:
        if not isinstance(a, dict):
            continue
        total += 1
        t = str(a.get("target") or "").strip()
        top = t.split(".")[0].lower()
        if top in ABSTRACT_NS and len([x for x in t.split(".") if x]) <= 2:
            abstract += 1
    return abstract, total


def _unlocated_ui_steps(case: dict) -> int:
    n = 0
    for s in case.get("steps") or []:
        action = str((s or {}).get("action") or "") if isinstance(s, dict) else str(s or "")
        if UI_SIG.search(action) and not LOCATOR.search(action):
            n += 1
    return n


def grade_case(case: dict, allowed_patterns: list[re.Pattern], source_blob: str) -> dict:
    """单条用例分级。返回 {"grade", "reasons", "fabricated"}。"""
    reasons: list[str] = []
    fabricated = False
    grade = GRADE_A
    blob = _case_blob(case)

    # 1) 缺口标注 → C（这是"诚实降级"，不是缺陷）
    if _has_gap_marker(blob):
        grade = GRADE_C
        reasons.append("已标注信息缺口（needs_requirement_detail）")

    # 2) 编造路径 → C + fabricated（无可信集合时 fail-open：不判编造）
    bad_paths = []
    if allowed_patterns:
        for p in _iter_case_paths(case):
            if not any(pat.match(_clean_path(p)) for pat in allowed_patterns):
                bad_paths.append(p)
    if bad_paths:
        grade = GRADE_C
        fabricated = True
        reasons.append(f"接口路径无需求依据: {bad_paths[:3]}")

    # 3) 错误码留痕（仅记录，不判级——错误码可能来自通识/约定）
    unknown_codes = sorted({
        c for c in ERRCODE.findall(blob)
        if c not in source_blob and c not in ("200", "201", "204", "400", "401", "403", "404", "409", "422", "429", "500", "502", "503")
    })
    if unknown_codes:
        reasons.append(f"错误码未在需求出现（待核）: {unknown_codes[:3]}")

    # 4) 断言可观测性 → B
    abstract, total = _abstract_targets(case)
    if total and abstract == total:
        if grade == GRADE_A:
            grade = GRADE_B
        reasons.append(f"断言目标均为抽象命名空间（{abstract}/{total}）")

    # 5) 界面步骤定位 → B
    unloc = _unlocated_ui_steps(case)
    if unloc:
        if grade == GRADE_A:
            grade = GRADE_B
        reasons.append(f"{unloc} 个界面步骤无定位元素")

    # 6) 数据缺失 → B
    if not case.get("test_data"):
        if grade == GRADE_A:
            grade = GRADE_B
        reasons.append("缺 test_data")

    if grade == GRADE_A:
        reasons = ["结构/断言/定位/数据齐备"]

    return {"grade": grade, "reasons": reasons, "fabricated": fabricated}


def grade_cases(cases: list[dict], srr: dict, raw_text: str = "") -> dict:
    """批量分级（就地写 case["_executability"]），返回统计。

    Returns:
        {"checked", "A", "B", "C", "fabricated", "fabricated_rate",
         "reasons_top": {原因: 次数}, "details": [{case_id, grade, reasons}]}
    """
    stats: dict = {"checked": 0, GRADE_A: 0, GRADE_B: 0, GRADE_C: 0,
                   "fabricated": 0, "fabricated_rate": 0.0,
                   "reasons_top": {}, "details": [], "skipped": False}
    if not isinstance(cases, list):
        return stats

    allowed = allowed_path_patterns(srr, raw_text)
    text = raw_text or str((srr or {}).get("_raw_text") or "")
    source_blob = text + " " + json.dumps(srr or {}, ensure_ascii=False)
    # 无可信集合时无法判编造（fail-open），仍可判其他维度
    stats["skipped"] = not bool(allowed)

    reason_counts: dict[str, int] = {}
    for case in cases:
        if not isinstance(case, dict):
            continue
        stats["checked"] += 1
        r = grade_case(case, allowed, source_blob)
        stats[r["grade"]] += 1
        if r["fabricated"]:
            stats["fabricated"] += 1
        case["_executability"] = {"grade": r["grade"], "reasons": r["reasons"]}
        stats["details"].append({"case_id": case.get("id") or case.get("case_id"),
                                 "grade": r["grade"], "reasons": r["reasons"]})
        for reason in r["reasons"]:
            key = re.sub(r"[：:].*$", "", reason)
            reason_counts[key] = reason_counts.get(key, 0) + 1

    if stats["checked"]:
        stats["fabricated_rate"] = round(stats["fabricated"] / stats["checked"], 4)
    stats["reasons_top"] = dict(sorted(reason_counts.items(), key=lambda kv: -kv[1])[:8])
    return stats
