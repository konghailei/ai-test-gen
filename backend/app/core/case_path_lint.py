# -*- coding: utf-8 -*-
"""用例接口路径真实性校验（v2.49，R4 缺陷的落库前防线）。

## 背景（自证第四轮实测）

R4 抽验 754 条用例发现 10 条引用了系统中不存在的接口路径。根因追查：
- 解析层（ReqAgent）把「熔断」「检索降级」等**能力描述**臆造成 HTTP 接口
  （`/api/llm/fallback`、`/api/retrieval/status`）；
- 生成层（GenAgent）在功能点无对应接口定义时**自行发明路径**
  （`/api/v1/requirements`、`/api/task/{id}` 等）；
- 全链路无任何接口真实性校验 → 幻觉直达产物。

前两层已分别加约束（解析层原文校验 + 生成层提示约束），本模块是**最后防线**：
用例里出现的 `/api/...` 路径必须能追溯到
「本需求 SRR 的 api_definitions」或「需求原文」。

## 设计原则

- **只标记、不删除**：误杀成本高于漏网。标记写入 `case["_path_check"]`
  （下划线前缀 = 审计字段，不进正式 Schema），随用例落库可审计、可展示。
- **占位段通配**：契约写 `/api/requirements/{req_id}/versions`、用例写
  `/api/requirements/REQ-1/versions` 属同一条路径，按段位通配匹配（避免误报）。
- **fail-open**：无可信集合（SRR 无 api_definitions 且无原文）时**不标记**——
  避免把"契约缺失"误判为"路径错误"（后者会让整批用例被误标）。
"""
from __future__ import annotations

import re

# 用例文本中的 HTTP 路径（/api 起头）
PATH_RE = re.compile(r"/api/[A-Za-z0-9_\-/{}.$:]+")


def _clean_path(p: str) -> str:
    """去掉查询串与末尾斜杠（保留参数段供正则通配）。"""
    p = str(p).split("?")[0].strip()
    return p.rstrip("/")


def _path_pattern(p: str) -> re.Pattern | None:
    """把路径编译为同构正则：`{xxx}` 占位段匹配任意非空段。

    这样契约 `/api/requirements/{req_id}/versions` 能匹配用例中的
    `/api/requirements/REQ-1792-6157/versions`（具体值），不误报。
    """
    p = _clean_path(p)
    if not p:
        return None
    segs = []
    for seg in p.split("/"):
        if seg.startswith("{") and seg.endswith("}"):
            segs.append(r"[^/]+")
        else:
            segs.append(re.escape(seg))
    return re.compile("^" + "/".join(segs) + "$")


def allowed_path_patterns(srr: dict, raw_text: str = "") -> list[re.Pattern]:
    """可信路径集合（编译后）：api_definitions 的 path + 需求原文中出现的路径。"""
    pats: list[re.Pattern] = []
    seen: set[str] = set()
    for it in (srr or {}).get("api_definitions") or []:
        if isinstance(it, dict) and it.get("path"):
            key = _clean_path(it["path"])
            if key and key not in seen:
                seen.add(key)
                pat = _path_pattern(key)
                if pat:
                    pats.append(pat)
    text = raw_text or str((srr or {}).get("_raw_text") or "")
    for p in PATH_RE.findall(text or ""):
        key = _clean_path(p)
        if key and key not in seen:
            seen.add(key)
            pat = _path_pattern(key)
            if pat:
                pats.append(pat)
    return pats


def _iter_case_paths(case: dict) -> list[str]:
    """收集用例中出现的路径（步骤/预期/断言），去重保序。"""
    chunks: list[str] = [str(case.get("title") or ""), str(case.get("expected") or "")]
    for st in case.get("steps") or []:
        if isinstance(st, dict):
            chunks.append(str(st.get("action") or ""))
            chunks.append(str(st.get("expected") or ""))
        elif isinstance(st, str):
            chunks.append(st)
    for a in case.get("assertions") or []:
        if isinstance(a, dict):
            chunks.append(str(a.get("message") or ""))
            chunks.append(str(a.get("target") or ""))
    seen: set[str] = set()
    out: list[str] = []
    for c in chunks:
        for m in PATH_RE.findall(c):
            p = m.rstrip(".,;:）)")
            if p and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def lint_case_paths(cases: list[dict], srr: dict, raw_text: str = "") -> dict:
    """就地标记用例的 `_path_check.unverified_paths`，返回统计。

    Args:
        cases: 用例 dict 列表（会被就地修改：命中时写入 `_path_check`）。
        srr: 当前需求的 SRR（取 api_definitions 与 _raw_text）。
        raw_text: 需求原文（优先于 SRR 内的 _raw_text）。

    Returns:
        dict: {"checked", "flagged", "flagged_cases": [...],
               "unverified_paths": {path: [case_id, ...]}, "skipped": bool}
    """
    stats: dict = {"checked": 0, "flagged": 0, "flagged_cases": [],
                   "unverified_paths": {}, "skipped": False}
    if not isinstance(cases, list):
        return stats

    patterns = allowed_path_patterns(srr, raw_text)
    # fail-open：无可信集合时不标记（契约缺失 ≠ 路径错误）
    if not patterns:
        stats["skipped"] = True
        return stats

    for case in cases:
        if not isinstance(case, dict):
            continue
        stats["checked"] += 1
        paths = _iter_case_paths(case)
        if not paths:
            continue
        bad = [
            p for p in paths
            if not any(pat.match(_clean_path(p)) for pat in patterns)
        ]
        if not bad:
            continue
        stats["flagged"] += 1
        cid = str(case.get("id") or "")
        stats["flagged_cases"].append(cid)
        case["_path_check"] = {"unverified_paths": bad}
        for p in bad:
            stats["unverified_paths"].setdefault(p, []).append(cid)
    return stats
