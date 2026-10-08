"""要点级覆盖工具（Point-level Coverage）。

为 GenAgent / ReviewAgent / Orchestrator 提供统一的 SRR 要点抽取、
要点 ID 识别、动态生成下限与覆盖映射率计算。

要点 ID 规则（与 prompts.gen_agent._build_coverage_checklist 完全一致）：
- 总 SRR：happy_path.{i} / alternate_flow.{i} / precondition.{i} / shared_rule.{i}
- FP 级：{fp_id}.happy_path.{i} / {fp_id}.alt_flow.{i} / {fp_id}.rule.{i} / {fp_id}.data.{i}
"""

from __future__ import annotations

import math
import re

# 用例文本中的要点 ID（方括号形式为权威格式）
# v3.4.5（缺陷 D）：白名单加入 5 类扩展要点类型——collect_points 已把它们纳入
# 分母，提取正则若不认这些 ID，用例标注了也提取不出来 → 恒判"未覆盖" →
# 假触发补生成（打标记必须到达消费方）。
# 注意 non_functional 有两种 ID 形态：dict 型是 `non_functional.{key}`（词尾），
# list 型是 `non_functional.{i}`（数字尾）→ 用 (?:\.[a-z_]+|\.\d+)? 兼容两态。
_POINT_ID_RE = re.compile(
    r"\[((?:[^\[\]]+?\.)?(?:happy_path|alt_flow|alternate_flow|rule|data|precondition|shared_rule|(?:non_functional(?:\.[a-z_]+|\.\d+)?)|api|role|dependency|ui_rule)(?:\.\d+)?)\]"
)
# 无方括号的兜底识别（如 notes 中写成 "fp_x.rule.2"）
_BARE_POINT_ID_RE = re.compile(
    r"(?<![\w\-\[.])((?:[\w\-]+\.)?(?:happy_path|alt_flow|alternate_flow|rule|data|precondition|shared_rule|(?:non_functional(?:\.[a-z_]+|\.\d+)?)|api|role|dependency|ui_rule)(?:\.\d+)?)(?![\w\-\].])"
)

_TOP_LEVEL_TYPES = ("happy_path", "alternate_flow", "precondition", "shared_rule")

# 显式标注率下限：低于此值不认为"映射口径"成立（此时只报已观测下界）
ANNOTATION_RATE_THRESHOLD = 0.30

# 启发式回填标记：回填 ID 只作线索展示，绝不充当已观测覆盖证据
HEURISTIC_MARKER = "heuristic"


def is_heuristic_mapping(case: dict) -> bool:
    """用例是否带启发式回填标记（case 级或 gen_metadata 级任一即可）。"""
    if not isinstance(case, dict):
        return False
    gm = case.get("gen_metadata")
    gm = gm if isinstance(gm, dict) else {}
    return case.get("mapping_source") == HEURISTIC_MARKER or gm.get("mapping_source") == HEURISTIC_MARKER


def _top_priority(srr: dict) -> str:
    req = srr.get("requirement", {}) if isinstance(srr, dict) else {}
    return (req or {}).get("priority", "") if isinstance(req, dict) else ""


def _fp_priority(fp: dict, srr: dict) -> str:
    prio = fp.get("priority", "") or ""
    if not prio:
        fp_srr = fp.get("srr_json", {}) if isinstance(fp.get("srr_json"), dict) else {}
        req = fp_srr.get("requirement", {}) if isinstance(fp_srr.get("requirement"), dict) else {}
        prio = req.get("priority", "") or ""
    return prio or _top_priority(srr)


def _add_extended_points(src: dict, add, prio: str, fp_id: str = "", fp_name: str = "") -> None:
    """抽取「非功能 / 接口 / 角色 / 依赖 / UI 规则」5 类要点（缺陷 D）。

    这 5 类字段此前**随完整 SRR 注入 prompt，却不参与任何覆盖度量**：
    LLM 为其生成了用例 → 不计分；完全没生成 → 不告警、不触发补生成、
    ``degraded_flags`` 不亮。结果是覆盖率和告警系统对它们完全失明。

    注意：**只纳入有内容的项**。``non_functional`` 是固定 4 键的 dict，
    未填写的维度是空串——把它们也算进分母会凭空造出「未覆盖」，
    那不是真实缺口，只是需求没写。同理空列表直接跳过。
    """
    prefix = f"{fp_id}." if fp_id else ""

    nf = src.get("non_functional")
    if isinstance(nf, dict):
        for key, val in nf.items():
            text = str(val or "").strip()
            if text:
                add(f"{prefix}non_functional.{key}", "non_functional",
                    f"{key}: {text}", prio, fp_id, fp_name)
    elif isinstance(nf, list):
        for i, val in enumerate(nf, 1):
            text = str(val or "").strip()
            if text:
                add(f"{prefix}non_functional.{i}", "non_functional", text, prio, fp_id, fp_name)

    for i, api in enumerate(src.get("api_definitions") or [], 1):
        if isinstance(api, dict):
            name = api.get("name") or api.get("path") or api.get("endpoint") or f"api_{i}"
            desc = f"{api.get('method', '')} {name}".strip()
        else:
            desc = str(api)
        if desc.strip():
            add(f"{prefix}api.{i}", "api", desc, prio, fp_id, fp_name)

    for i, role in enumerate(src.get("user_roles") or [], 1):
        if isinstance(role, dict):
            desc = str(role.get("role") or role.get("name") or "")
            perms = role.get("permissions") or []
            if perms:
                desc = f"{desc}: {'、'.join(str(p) for p in perms)}"
        else:
            desc = str(role)
        if desc.strip():
            add(f"{prefix}role.{i}", "role", desc, prio, fp_id, fp_name)

    for i, dep in enumerate(src.get("dependencies") or [], 1):
        if isinstance(dep, dict):
            desc = str(dep.get("name") or dep.get("target") or dep.get("description") or f"dependency_{i}")
        else:
            desc = str(dep)
        if desc.strip():
            add(f"{prefix}dependency.{i}", "dependency", desc, prio, fp_id, fp_name)

    for i, ui in enumerate(src.get("ui_rules") or [], 1):
        if isinstance(ui, dict):
            desc = "；".join(
                str(ui.get(k)) for k in ("element", "behavior", "trigger") if ui.get(k)
            )
        else:
            desc = str(ui)
        if desc.strip():
            add(f"{prefix}ui_rule.{i}", "ui_rule", desc, prio, fp_id, fp_name)


def collect_points(
    srr: dict,
    functional_points: list[dict],
    include_preconditions: bool = False,
) -> list[dict]:
    """从总 SRR + FP 子 SRR 抽取全部覆盖要点。

    Args:
        srr: 总 SRR JSON
        functional_points: 功能点列表（含 srr_json）
        include_preconditions: 是否包含前置条件要点（不参与覆盖统计分母）

    Returns:
        list[dict]: 每项 {id, ptype, desc, priority, fp_id, fp_name, field}
    """
    points: list[dict] = []
    srr = srr if isinstance(srr, dict) else {}
    top_prio = _top_priority(srr)

    def _add(pid: str, ptype: str, desc: str, prio: str, fp_id: str = "", fp_name: str = "", field: str = "") -> None:
        points.append({
            "id": pid,
            "ptype": ptype,
            "desc": str(desc or "")[:160],
            "priority": prio or "",
            "fp_id": fp_id,
            "fp_name": fp_name,
            "field": field,
        })

    flows = srr.get("flows", {}) if isinstance(srr.get("flows"), dict) else {}
    happy = flows.get("happy_path", {})
    hp_steps = happy.get("steps", []) if isinstance(happy, dict) else []
    for i, step in enumerate(hp_steps, 1):
        action = step.get("action", step.get("desc", "")) if isinstance(step, dict) else step
        _add(f"happy_path.{i}", "happy_path", action, top_prio)

    for i, af in enumerate(flows.get("alternate_flows", []) or [], 1):
        name = af.get("name", af.get("trigger", "")) if isinstance(af, dict) else af
        _add(f"alternate_flow.{i}", "alternate_flow", name, top_prio)

    if include_preconditions:
        for i, p in enumerate(srr.get("preconditions", []) or [], 1):
            _add(f"precondition.{i}", "precondition", p, top_prio)

    cfi = srr.get("cross_fp_interactions", {}) if isinstance(srr.get("cross_fp_interactions"), dict) else {}
    for i, r in enumerate(cfi.get("shared_business_rules", []) or [], 1):
        desc = r.get("rule", r.get("description", "")) if isinstance(r, dict) else r
        _add(f"shared_rule.{i}", "shared_rule", desc, top_prio)

    # v3.4.5（缺陷 D）：非功能/接口/角色/依赖/UI 规则——此前注入 prompt 但不计分
    _add_extended_points(srr, _add, top_prio)

    for fp in functional_points or []:
        fp_id = fp.get("id", "")
        fp_name = fp.get("name", fp_id)
        prio = _fp_priority(fp, srr)
        fp_srr = fp.get("srr_json", {}) if isinstance(fp.get("srr_json"), dict) else {}
        fp_flows = fp_srr.get("flows", {}) if isinstance(fp_srr.get("flows"), dict) else {}
        fp_hp = fp_flows.get("happy_path", {})
        fp_hp_steps = fp_hp.get("steps", []) if isinstance(fp_hp, dict) else []
        for i, step in enumerate(fp_hp_steps, 1):
            action = step.get("action", step.get("desc", "")) if isinstance(step, dict) else step
            _add(f"{fp_id}.happy_path.{i}", "happy_path", action, prio, fp_id, fp_name)
        for i, af in enumerate(fp_flows.get("alternate_flows", []) or [], 1):
            name = af.get("name", af.get("trigger", "")) if isinstance(af, dict) else af
            _add(f"{fp_id}.alt_flow.{i}", "alt_flow", name, prio, fp_id, fp_name)
        for i, r in enumerate(fp_srr.get("business_rules", []) or [], 1):
            desc = r.get("rule", r.get("description", r.get("condition", ""))) if isinstance(r, dict) else r
            _add(f"{fp_id}.rule.{i}", "rule", desc, prio, fp_id, fp_name)
        for i, d in enumerate(fp_srr.get("data_constraints", []) or [], 1):
            if isinstance(d, dict):
                field = d.get("field", "")
                cons = d.get("constraints", d.get("constraint", ""))
                desc = f"{field}: {cons}"
            else:
                field, desc = "", d
            _add(f"{fp_id}.data.{i}", "data", desc, prio, fp_id, fp_name, field=field)
        # v3.4.5（缺陷 D）：FP 子 SRR 的 5 类扩展要点
        _add_extended_points(fp_srr, _add, prio, fp_id, fp_name)

    if not include_preconditions:
        points = [p for p in points if p["ptype"] != "precondition"]
    return points


def extract_point_ids(case: dict) -> set[str]:
    """从单条用例的文本字段中提取其声称覆盖的要点 ID 集合。

    兼容结构化 cover_points / covered_points / covers_points / covered_point_ids、
    notes 方括号标注和标题/描述/步骤中的裸 ID，也读取 gen_metadata 中的标注。
    结构化字段中的未知 ID 原样保留，交由覆盖计算与需求清单校验。
    本函数只提取声明，不判断来源可信度，也不证明业务正确性。
    """
    ids: set[str] = set()

    def _collect_str(v: object) -> None:
        if v is None:
            return
        if isinstance(v, (list, tuple, set)):
            for it in v:
                _collect_str(it)
            return
        if isinstance(v, str):
            # 用 update 而非闭包内的 |=，避免把 ids 当作未初始化的局部变量。
            ids.update(token.strip("[]") for token in re.split(r"[,，;；\s]+", v) if token.strip("[]"))

    metadata = case.get("gen_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    for source in (case, metadata):
        for k in ("cover_points", "covered_points", "covers_points", "covered_point_ids"):
            _collect_str(source.get(k))

    texts: list[str] = [
        case.get("title", ""),
        case.get("description", ""),
        case.get("notes", ""),
        metadata.get("notes", ""),
    ]
    for s in case.get("steps", []) or []:
        if isinstance(s, dict):
            texts.append(s.get("action", ""))
            texts.append(s.get("expected", ""))
    blob = " ".join(str(t) for t in texts if t)
    ids |= {m.group(1) for m in _POINT_ID_RE.finditer(blob)}
    ids |= {m.group(1) for m in _BARE_POINT_ID_RE.finditer(blob)}
    return ids


def normalize_structured_point_ids(
    cases: list[dict],
    srr: dict,
    functional_points: list[dict],
) -> int:
    """规范化结构化 `covered_point_ids` 与 notes 覆盖段：修复清单外编号（就地修改）。

    背景（v3.4）：生成 prompt 要求 LLM 直接输出结构化 covered_point_ids；
    LLM 可能编造清单里没有的编号，而 calc_point_coverage 会把未知引用记为
    invalid_point_references → 直接阻断自动发布。这里在入库前把不在合法集合
    （**含前置条件**——合法 ID 集 ≠ 覆盖分母，见 calc_point_coverage 注释）中的
    编号剔除，notes 兜底与启发式回填逻辑不受影响。

    v3.5.1（自举验证实测缺陷，防线对称性修复）：normalize 原先只处理
    covered_point_ids 结构化字段，但 extract_point_ids 还会从 **notes 方括号
    标注**（POINT 正则）与标题/描述/步骤（BARE 正则）提取 ID——LLM 按 prompt
    示例在 notes 写裸形态 `[rule.1]`（清单里实际是 `REQ-xxx.rule.1`），这些裸
    ID 不在 known_ids → 依旧触发 invalid_point_references 全批阻断。
    修复分两步，优先挽救而非丢弃：
    1. **前缀补全**：裸 `rule.N`/`data.N` 等形态若 `related_fp + 裸ID` 在合法
       集合中（用例 related_fp 已指向 FP），改写为带前缀的合法 ID（结构化字段
       与 notes 覆盖段同步改写）；
    2. **剔除兜底**：无法映射的清单外编号从结构化字段与 notes 覆盖段中移除
       （正文文本不动——那是给用户读的描述）。

    Returns:
        int: 被修复（补前缀或剔除）编号的用例数量。
    """
    known_ids = {
        p["id"]
        for p in collect_points(srr, functional_points or [], include_preconditions=True)
    }

    # 裸 ID → 带前缀合法 ID 的映射（基于 related_fp）
    _BARE_ID_RE = re.compile(
        r"^(happy_path|alternate_flow|alt_flow|rule|data|precondition|shared_rule|"
        r"non_functional|api|role|dependency|ui_rule)\.\d+$"
    )

    def _normalize_one(bare_id: str, related_fp: str) -> str | None:
        """裸 ID 补前缀；成功返回合法 ID，失败返回 None。已是合法 ID 原样返回。"""
        if bare_id in known_ids:
            return bare_id
        if _BARE_ID_RE.match(bare_id) and related_fp:
            prefixed = f"{related_fp}.{bare_id}"
            if prefixed in known_ids:
                return prefixed
        return None

    fixed = 0
    for c in cases or []:
        if not isinstance(c, dict):
            continue
        related_fp = str(c.get("related_fp") or "")
        changed = False

        # 1) 结构化字段：逐条尝试补前缀，映射失败才剔除
        ids = c.get("covered_point_ids")
        if isinstance(ids, list):
            new_ids: list[str] = []
            for raw in ids:
                pid = str(raw)
                mapped = _normalize_one(pid, related_fp)
                if mapped is None:
                    changed = True  # 剔除清单外编号
                    continue
                if mapped != pid:
                    changed = True
                new_ids.append(mapped)
            if changed:
                c["covered_point_ids"] = new_ids

        # 2) notes 覆盖段：对「覆盖:」后方括号标注做同样的补前缀/剔除
        notes = c.get("notes")
        if isinstance(notes, str) and "覆盖" in notes:
            def _rewrite_note(m: re.Match) -> str:
                nonlocal changed
                pid = m.group(1)
                mapped = _normalize_one(pid, related_fp)
                if mapped is None:
                    changed = True
                    return ""  # 从 notes 覆盖段剔除
                if mapped != pid:
                    changed = True
                    return f"[{mapped}]"
                return m.group(0)

            new_notes = re.sub(r"\[([^\[\]]+?)\]", _rewrite_note, notes)
            new_notes = re.sub(r"覆盖:\s*(；|\s)*$", "", new_notes)   # 移除空覆盖段
            new_notes = re.sub(r"\s+([；;])", r"\1", new_notes)       # 剔除后残留空格
            new_notes = re.sub(r"\s{2,}", " ", new_notes).strip()
            if changed:
                c["notes"] = new_notes
                gm = c.get("gen_metadata")
                if isinstance(gm, dict) and isinstance(gm.get("notes"), str):
                    gm_notes = re.sub(r"\[([^\[\]]+?)\]", _rewrite_note, gm["notes"])
                    gm_notes = re.sub(r"覆盖:\s*(；|\s)*$", "", gm_notes)
                    gm_notes = re.sub(r"\s+([；;])", r"\1", gm_notes)
                    gm["notes"] = re.sub(r"\s{2,}", " ", gm_notes).strip()

        if changed:
            fixed += 1
    return fixed


def backfill_cases_point_ids(
    cases: list[dict],
    srr: dict,
    functional_points: list[dict],
    min_ratio: float = 0.35,
) -> int:
    """系统兜底：为无要点标注的用例回填最可能覆盖的要点 ID。

    当 LLM 未输出要点标注时，依据用例文本与要点描述的关键词命中率，
    将候选编号写回 notes 的 “覆盖: [...]” 段，仅供显示和人工核查。
    回填标记 mapping_source='heuristic'，不作为已观测覆盖证据；已有显式标注优先保留。

    仅回填明显种子（同 related_fp / 总需求要点 + 文本有实质命中），避免误配。

    Args:
        cases: 用例列表（就地补充 notes 标注）
        srr: 总 SRR
        functional_points: 功能点列表
        min_ratio: 用例文本与要点描述命中 3 字及 2 字关键词的最低比例阈值

    Returns:
        int: 回填的用例数量（用于统计）。

    TODO(说明): 若未来引入语义向量化，可用 Embedding 相似度替换关键词命中，
        提高映射精度。当前保持确定性关键词匹配，稳定且无需额外模型调用。
    """
    points = collect_points(srr, functional_points or [], include_preconditions=False)
    point_map: dict[str, dict] = {p["id"]: p for p in points}

    # 按 fp_id 建索引，便于快速取候选
    by_fp: dict[str, list[dict]] = {}
    orphan: list[dict] = []
    fp_id_by_name: dict[str, str] = {}
    for p in points:
        if p["fp_id"]:
            by_fp.setdefault(p["fp_id"], []).append(p)
            if p.get("fp_name"):
                fp_id_by_name[p["fp_name"]] = p["fp_id"]
        else:
            orphan.append(p)

    def _ngrams(s: str, size: int) -> set[str]:
        """提取指定长度的连续子串（去除非中文字符干扰），长度不足返回空集。"""
        import re as _re
        cleaned = _re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", s)
        if len(cleaned) < size:
            return set()
        return {cleaned[i:i + size] for i in range(len(cleaned) - size + 1)}

    def _keywords(text: str) -> tuple[set[str], set[str]]:
        """提取 3 字与 2 字 n-gram（3 字优先，2 字辅助）。"""
        return _ngrams(text, 3), _ngrams(text, 2)

    def _best_hits(text: str, cands: list[dict]) -> set[str]:
        """返回命中要点 ID 集合。

        判定：用例文本与要点描述同时命中至少 1 个 3 字词（显著特征），且
        要点描述中被命中的 3+2 字词累计覆盖比例 ≥ min_ratio。命中组合再筛。
        """
        t3, t2 = _keywords(text)
        if not t3:
            return set()
        hits: dict[str, float] = {}
        for p in cands:
            desc = str(p.get("desc", "") or "")
            if not desc:
                continue
            d3, d2 = _keywords(desc)
            if not d3:
                continue
            inter3 = t3 & d3
            inter2 = t2 & d2
            if not inter3:
                continue  # 必须命中 3 字词才视为实质匹配（防高频 2 字词误配）
            covered = len(inter3) + len(inter2)
            total = len(d3) + len(d2)
            ratio = covered / total if total else 0.0
            if ratio >= min_ratio:
                hits[p["id"]] = ratio * 100 + len(inter3)  # 3 字命中数作为次级排序
        if not hits:
            return set()
        # 取得分最高的若干要点（同分保留多个，避免一个用例标注过少）
        top_score = max(hits.values())
        picked = {k for k, v in hits.items() if v >= top_score - 30}
        return picked

    backfilled = 0
    for case in cases or []:
        if extract_point_ids(case):
            continue  # 已有标注，跳过
        text = " ".join(str(x) for x in [
            case.get("title", ""),
            case.get("description", ""),
            case.get("related_fp", ""),
        ] if x)
        for s in case.get("steps", []) or []:
            if isinstance(s, dict):
                text += " " + str(s.get("action", "")) + " " + str(s.get("expected", ""))
        if not text.strip():
            continue
        # 候选：related_fp 关联的要点 + 顶层要点；若 related_fp 为空，放宽到全量，
        # 并按 FP 名命中的 FP 子集优先（LLM 常漏填 related_fp，只能全量匹配反推）。
        rfp = str(case.get("related_fp", "") or "")
        cands: list[dict] = []
        if rfp:
            if rfp in by_fp:
                cands = by_fp[rfp] + orphan
            elif rfp in fp_id_by_name:
                cands = by_fp.get(fp_id_by_name[rfp], []) + orphan
            else:
                cands = points
        if not cands:
            cands = points
        picked = _best_hits(text, cands)
        # 仅在候选是子集（非全量）时，放宽到全量再查一次，避免漏掉 FP 间误归属
        if not picked and cands is not points:
            picked = _best_hits(text, points)
        if not picked:
            continue
        picked = {pid for pid in picked if pid in point_map}
        # 回填到 notes：保留原 notes 内容，追加覆盖标注
        note_str = str(case.get("notes") or "").strip()
        cover_tag = "覆盖: " + ", ".join(f"[{pid}]" for pid in sorted(picked))
        case["notes"] = (note_str + (" " if note_str else "") + cover_tag).strip()
        # 同步写入 gen_metadata，便于 review / KB 读取
        gm = case.get("gen_metadata")
        if not isinstance(gm, dict):
            gm = {}
            case["gen_metadata"] = gm
        existing = gm.get("notes", "")
        gm["notes"] = (str(existing) + (" " if existing else "") + cover_tag).strip()
        # 打标：回填 ID 是"线索"不是"证据"（calc_point_coverage 据此排除）
        case["mapping_source"] = HEURISTIC_MARKER
        gm["mapping_source"] = HEURISTIC_MARKER
        backfilled += 1
    return backfilled


def calc_min_cases(srr: dict, functional_points: list[dict]) -> tuple[int, int]:
    """按要点构成推算本批动态生成下限（业界风险驱动密度实践）。

    规则：主流程步骤按场景法合并（约 3 步/例）、每异常分支 1 例、
    每业务规则 1 例、数据约束按字段聚合 1 例；P0/P1 需求加权放大。

    Returns:
        tuple[int, int]: (min_cases, total_points)
    """
    points = collect_points(srr, functional_points or [])
    total = len(points)
    if total == 0:
        return 5, 0

    hp = [p for p in points if p["ptype"] == "happy_path"]
    alt = [p for p in points if p["ptype"] in ("alt_flow", "alternate_flow")]
    rules = [p for p in points if p["ptype"] in ("rule", "shared_rule")]
    data = [p for p in points if p["ptype"] == "data"]

    min_hp = math.ceil(len(hp) / 3)
    min_alt = len(alt)
    min_rules = len(rules)
    dc_fields = {p["field"] for p in data if p["field"]}
    min_data = len(dc_fields) if dc_fields else math.ceil(len(data) / 2)

    min_cases = min_hp + min_alt + min_rules + min_data
    if _top_priority(srr) in ("P0", "P1"):
        min_cases = max(min_cases, math.ceil(total * 0.6))
    min_cases = max(min_cases, 5)
    min_cases = min(min_cases, 40)
    return min_cases, total


def calc_point_coverage(srr: dict, functional_points: list[dict], cases: list[dict]) -> dict:
    """计算要点级覆盖映射——**只认已观测证据，不再用数量比冒充覆盖率**。

    两种口径（docs/15 §4.7）：

    - ``mapping_based=True``（``coverage_status='mapped'``）：显式标注率
      ≥ :data:`ANNOTATION_RATE_THRESHOLD` 时按 ID 精确映射（RTM 实践），
      ``coverage_rate`` 即实测映射率。
    - ``mapping_based=False``（``coverage_status='unknown'``）：只报**已观测下界**
      ``observed_coverage_rate`` = 已证实覆盖数 / 要点总数。旧的
      ``min(用例数, 要点数)/要点数`` 会被读成"覆盖良好"（用例多≠覆盖对），
      这正是"伪 100%"的来源，已移除。``estimated_coverage_rate`` 恒为 None。

    启发式回填（``mapping_source='heuristic'``）的 ID 单独登记在
    ``heuristic_point_ids``：既不进分子也不进分母，仅作人工核查线索。
    """
    points = collect_points(srr, functional_points or [])
    total = len(points)
    point_map = {p["id"]: p for p in points}
    # v3.4 修复（端到端实测发现）：`precondition.N` 是**合法要点类型**，但默认不参与
    # 覆盖分母（collect_points 会过滤掉）。若拿 point_map 当"合法 ID 全集"，
    # 用例标注 precondition 会被判成"引用了不存在的要点"→ invalid_point_references
    # → 直接阻断自动发布。这是**口径错位造成的假阻断**：分母可以排除前置条件，
    # 但合法性判定必须认它。
    known_ids = set(point_map.keys()) | {
        p["id"] for p in collect_points(srr, functional_points or [], include_preconditions=True)
        if p["ptype"] == "precondition"
    }
    case_list = [c for c in (cases or []) if isinstance(c, dict)]
    case_count = len(case_list)

    covered_ids: set[str] = set()
    heuristic_ids: set[str] = set()
    invalid_ids: list[str] = []
    cases_with_valid_ids = 0

    for c in case_list:
        ids = extract_point_ids(c)
        if not ids:
            continue
        unknown = ids - known_ids
        invalid_ids.extend(sorted(unknown))
        # 只有"既能算覆盖、又真实存在"的 ID 才进入覆盖分子（前置条件不进分母）
        valid = (ids - unknown) & point_map.keys()
        if is_heuristic_mapping(c):
            # 回填线索：只登记，不计入任何覆盖分子/分母
            heuristic_ids |= valid
            continue
        if valid:
            cases_with_valid_ids += 1
            covered_ids |= valid

    annotation_rate = round(cases_with_valid_ids / case_count, 3) if case_count else 0.0
    mapping_based = (
        total > 0 and cases_with_valid_ids > 0
        and annotation_rate >= ANNOTATION_RATE_THRESHOLD
    )
    observed_rate = round(len(covered_ids) / total, 3) if total else 0.0

    hp_points = [p for p in points if p.get("priority") in ("P0", "P1")]
    hp_covered = sum(1 for p in hp_points if p["id"] in covered_ids)
    hp_rate = round(hp_covered / len(hp_points), 3) if hp_points else 0.0

    uncovered = [p for p in points if p["id"] not in covered_ids]
    prio_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    uncovered.sort(key=lambda p: prio_rank.get(p.get("priority", ""), 9))

    # v3.4.5（缺陷 D）：新增 5 类，便于观测分母构成变化
    stats = {
        "happy_path": 0, "alt_flow": 0, "rule": 0, "data": 0, "shared_rule": 0,
        "non_functional": 0, "api": 0, "role": 0, "dependency": 0, "ui_rule": 0,
    }
    for p in points:
        key = "alt_flow" if p["ptype"] == "alternate_flow" else p["ptype"]
        if key in stats:
            stats[key] += 1

    return {
        "total_points": total,
        "covered_points": len(covered_ids),
        "coverage_rate": observed_rate,
        "coverage_status": "mapped" if mapping_based else "unknown",
        "coverage_basis": "observed" if mapping_based else "observed_lower_bound",
        "observed_coverage_rate": observed_rate,
        "estimated_coverage_rate": None,
        "mapping_based": mapping_based,
        "annotation_rate": annotation_rate,
        "cases_with_valid_ids": cases_with_valid_ids,
        "invalid_point_ids": list(dict.fromkeys(invalid_ids)),
        "covered_point_ids": sorted(covered_ids),
        "heuristic_point_ids": sorted(heuristic_ids),
        "high_priority_total": len(hp_points),
        "high_priority_covered": hp_covered,
        "high_priority_rate": hp_rate,
        # 全量返回（不截断）：截断会让"未覆盖清单"看起来比真实情况小
        "uncovered": [
            {"id": p["id"], "type": p["ptype"], "priority": p["priority"],
             "desc": p["desc"], "fp_id": p["fp_id"]}
            for p in uncovered
        ],
        "case_count": case_count,
        "point_stats": stats,
    }
