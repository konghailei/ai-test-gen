# -*- coding: utf-8 -*-
"""静默降级扫描器：找出「捕获异常后只记日志、不上抛、也无降级标记」的位置。

## 为什么需要

N7（澄清语义层静默归零）与 N8（审核 LLM 失败静默降级）暴露了一类共性问题：

> **能力静默缺失** —— 某个环节实际没干活，但结果数据看起来"一切正常"
> （N7 是 `overall_score=0.91`，N8 是 `passed_cases` 看起来像审核通过），
> 用户与质量分都无法察觉。

这类问题的**失败路径往往比想象的多，且分散在不同层**（N7 就有两条：
一条"不抛异常返回残缺结果"、一条"抛异常被上层吞掉"）。
所以需要机械扫描，而不是靠人工翻代码。

## 判定规则

命中即报告：
- 捕获块类型为 `Exception` / `BaseException`（宽泛捕获）
- 块体内**没有** `raise`（不上抛）
- 块体内**有** `logger.warning/error/exception`（即"只记日志"）

再按块体内是否出现降级标记关键字（`degraded` / `skip_reason` 等）分为：
- 有标记 → 已可观测（正常）
- **无标记 → ⚠️ 需人工判定**是否属"能力静默缺失"

## 用法

    python scripts/scan_silent_degradation.py                # 扫描重点文件
    python scripts/scan_silent_degradation.py --all           # 扫描 app 全量
    python scripts/scan_silent_degradation.py --root backend/app

> ⚠️ 扫描结果**不是缺陷清单**，而是**待判定清单**。很多命中是合理的
> （如"向量写入失败不阻断"——数据仍在 PG，缺失由对账任务兜住）。
> 需按「是否让产出质量看起来正常而实际缺失」逐条判定。
"""
from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

# 能力可能静默缺失的关键路径（优先看这些）
HOT_FILES = [
    "agents/req_agent.py", "agents/fp_agent.py", "agents/gen_agent.py",
    "agents/review_agent.py", "agents/impact_agent.py",
    "agents/clarify_agent.py", "agents/clarify_question_gen.py",
    "agents/clarify_kb.py", "agents/clarify_detect.py",
    "agents/clarify_score.py",
    "core/orchestrator.py", "core/quality_gate.py",
]

DEGRADE_HINTS = ("degraded", "degraded_flags", "semantic_degraded",
                 "review_degraded", "skip_reason", "degrade")


def scan_file(path: Path) -> list[dict]:
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except Exception:  # noqa: BLE001
        return []

    def seg(node: ast.AST) -> str:
        return ast.get_source_segment(src, node) or ""

    out: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for h in node.handlers:
            if h.type is None:
                continue
            tname = getattr(h.type, "id", None) or getattr(h.type, "attr", None)
            if tname not in ("Exception", "BaseException"):
                continue
            body = "\n".join(seg(s) for s in h.body)
            if re.search(r"\braise\b", body):
                continue
            if not re.search(r"logger\.(warning|error|exception)", body):
                continue
            m = re.search(
                r"logger\.(?:warning|error|exception)\(\s*[\"']([^\"']{0,120})", body
            )
            out.append({
                "line": h.lineno,
                "log": (m.group(1) if m else "").strip(),
                "has_degrade_flag": any(k in body for k in DEGRADE_HINTS),
            })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None,
                    help="代码根目录，默认自动探测 backend/app")
    ap.add_argument("--all", action="store_true",
                    help="扫描根目录下全部 .py（默认只扫 HOT_FILES）")
    args = ap.parse_args()

    if args.root:
        root = Path(args.root)
    else:
        root = Path("backend/app") if Path("backend/app").is_dir() else Path("/app/app")

    targets = (sorted(root.rglob("*.py")) if args.all
               else [root / f for f in HOT_FILES])

    total = risky = 0
    for p in targets:
        if not p.exists():
            continue
        items = scan_file(p)
        if not items:
            continue
        print(f"\n=== {p.relative_to(root)} （{len(items)} 处静默捕获）===")
        for it in items:
            total += 1
            mark = "  " if it["has_degrade_flag"] else "⚠️"
            if not it["has_degrade_flag"]:
                risky += 1
            print(f"  {mark} L{it['line']:<5} degrade={str(it['has_degrade_flag']):<5} "
                  f"{it['log'][:80]}")

    print(f"\n合计静默捕获 {total} 处；其中**无降级标记** {risky} 处"
          f"（需人工判定是否属能力静默缺失）")


if __name__ == "__main__":
    main()
