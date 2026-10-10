"""跨 FP 共享规则数值保真校验（v3.5.7，docs/25 P2-B）。

问题（docs/24 O-1，两轮实证）：shared_business_rules / global_constraints 中的
具体数值（锁定 30 分钟、令牌 8 小时、40 条封顶、60 RPM…）在生成的用例中以
「间接形式」出现甚至完全缺失，且丢失对用户不可见——生成出口无任何命中率指标。

设计（对齐 docs/25 §2.7）：
  1. 提取器 extract_shared_values：从总 SRR 的共享规则中提取「数值+单位」原子
     （归一化：全角→半角、去空白；断言按数值原子本身判定，不匹配 LLM 措辞）；
  2. 校验器 check_value_fidelity：用例集全文匹配数值原子 → 规则粒度命中率
     shared_value_coverage（规则内任一原子命中即算该规则命中）；
  3. 透出：生成完成出口写入 result_json.shared_value_coverage（>0 规则时），
     低于阈值（默认 0.6，v1.0 轮定标基线 0.5 之上）附 value_fidelity_warning。

定标记录（2026-10-10，v1.0 正式轮 99 条用例）：
  数值规则 2 条，命中 1（30分钟/5次），未命中 1（8小时令牌）→ 基线 0.500。
  阈值 0.6 = 基线之上 + 保守起点，先观察一轮再评估是否进入发布门禁。
"""
from __future__ import annotations

import json
import re

from app.utils.logger import logger

# 数值原子正则：数字 + 常见业务单位（覆盖 docs/24 观察到的全部形态）
_VALUE_RE = re.compile(
    r"(\d+(?:\.\d+)?)(\s*)"
    r"(个|条|次|分钟|小时|秒|天|周|月|MB|GB|KB|TB|RPM|%|字符|节点|问|层|级)"
)

# 全角 → 半角归一化表
_FULLWIDTH = str.maketrans("０１２３４５６７８９（）：，％", "0123456789():,%")

# 结果缺省阈值（定标基线 0.5 之上，保守起点）
SHARED_VALUE_COVERAGE_THRESHOLD = 0.6


def _normalize(text: str) -> str:
    """全角→半角 + 去空白（数值原子匹配前的归一化）。"""
    return (text or "").translate(_FULLWIDTH).replace(" ", "").replace("\u3000", "")


def extract_shared_values(srr: dict) -> dict[str, list[str]]:
    """从总 SRR 的共享规则中提取数值原子清单。

    覆盖字段：cross_fp_interactions.shared_business_rules[].rule、
    cross_fp_interactions.global_constraints[].constraints/field。

    Returns:
        dict: {规则键: [数值原子, ...]}，规则键形如 "SBR:{id}" / "GC:{id}"；
              无数值规则时返回空 dict（调用方据此跳过校验）。
    """
    if not isinstance(srr, dict):
        return {}
    cfi = srr.get("cross_fp_interactions") or {}
    if not isinstance(cfi, dict):
        return {}
    rule_values: dict[str, list[str]] = {}
    for r in cfi.get("shared_business_rules") or []:
        if isinstance(r, dict):
            vals = sorted({m for m in _VALUE_RE.findall(_normalize(str(r.get("rule", ""))))
                           for m in [f"{m[0]}{m[2]}"]})
            if vals:
                rule_values[f"SBR:{r.get('id', '')}"] = vals
    for g in cfi.get("global_constraints") or []:
        if isinstance(g, dict):
            vals = sorted({m for m in _VALUE_RE.findall(
                _normalize(str(g.get("constraints", "")) + str(g.get("field", ""))))
                for m in [f"{m[0]}{m[2]}"]})
            if vals:
                rule_values[f"GC:{g.get('id', '')}"] = vals
    return rule_values


def check_value_fidelity(srr: dict, cases_json: list[dict]) -> dict:
    """校验用例集对共享规则数值的覆盖率。

    Args:
        srr: 总 SRR 字典
        cases_json: 用例 case_json 列表（全文参与匹配）

    Returns:
        dict: {
            "shared_value_rules": 规则数,
            "shared_value_hits": 命中规则数,
            "shared_value_coverage": 命中率（无数值规则时为 None）,
            "missed_rules": [{key, values}]（未命中的规则明细）,
        }
    """
    rule_values = extract_shared_values(srr)
    out: dict = {
        "shared_value_rules": len(rule_values),
        "shared_value_hits": 0,
        "shared_value_coverage": None,
        "missed_rules": [],
    }
    if not rule_values:
        return out

    # 用例全文一次拼接（数值原子匹配）
    corpus = _normalize("\n".join(
        json.dumps(c, ensure_ascii=False) if isinstance(c, dict) else str(c)
        for c in (cases_json or [])
    ))

    hits = 0
    missed: list[dict] = []
    for key, vals in rule_values.items():
        hit_vals = [v for v in vals if v in corpus]
        if hit_vals:
            hits += 1
        else:
            missed.append({"key": key, "values": vals})
    out["shared_value_hits"] = hits
    out["shared_value_coverage"] = round(hits / len(rule_values), 3)
    out["missed_rules"] = missed
    return out


def apply_value_fidelity(result: dict, srr: dict, cases_json: list[dict]) -> None:
    """生成完成出口调用：计算并写入 result_json（就地修改）。

    - 无数值规则：不写任何字段（保持 result_json 精简）；
    - 有数值规则：写 shared_value_coverage / shared_value_hits / shared_value_rules；
    - 低于阈值：追加 value_fidelity_warning + missed_rules（不阻断，先观察）。
    """
    try:
        report = check_value_fidelity(srr, cases_json)
        if report["shared_value_rules"] <= 0:
            return
        result["shared_value_coverage"] = report["shared_value_coverage"]
        result["shared_value_hits"] = report["shared_value_hits"]
        result["shared_value_rules"] = report["shared_value_rules"]
        if report["shared_value_coverage"] < SHARED_VALUE_COVERAGE_THRESHOLD:
            result["value_fidelity_warning"] = (
                f"跨 FP 共享规则数值命中率偏低"
                f"（{report['shared_value_hits']}/{report['shared_value_rules']}"
                f" = {report['shared_value_coverage']:.0%} < "
                f"{SHARED_VALUE_COVERAGE_THRESHOLD:.0%}），"
                "以下规则数值未在用例中原值出现："
                + "; ".join(f"{m['key']}({','.join(m['values'])})" for m in report["missed_rules"])
            )
            logger.warning("[ValueFidelity] {}", result["value_fidelity_warning"])
    except Exception as e:
        # 校验失败不阻断生成（保护型指标）
        logger.warning("[ValueFidelity] 数值保真校验失败（不阻断）: {}", str(e)[:150])
