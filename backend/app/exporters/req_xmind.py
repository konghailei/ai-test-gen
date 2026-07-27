"""需求 SRR → XMind 思维导图导出器（XMAP 2.0 XML）。

把需求 SRR 结构导出为 .xmind 文件，便于评审/分享/归档。

树结构（总体SRR 与功能点平级为一级节点）：
    需求标题（根）
      ├── 总体SRR（一级）
      │   ├── 基本信息（优先级 / 类型 / 摘要）
      │   ├── 主流程（Happy Path）
      │   │   └── 步骤1: action → expected
      │   ├── 异常分支（Alternate Flows）
      │   ├── 业务规则 / 边界条件 / 数据约束 / 验收标准 / 前置条件
      │   └── 附加详情
      ├── [fp_type] 功能点1（一级）
      │   ├── 摘要
      │   ├── 主流程 / 异常分支 / 业务规则 / ...
      │   └── 测试提示
      └── [fp_type] 功能点2（一级）
          └── ...
"""

from __future__ import annotations

import io, json, uuid, zipfile
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring


def _id() -> str: return uuid.uuid4().hex


def _topic(title: str, tid: str | None = None) -> Element:
    """创建独立 topic 元素（不附加到任何父节点）。"""
    t = Element("topic", {"id": tid or _id(), "timestamp": str(int(datetime.now().timestamp() * 1000))})
    te = SubElement(t, "title"); te.text = str(title)
    return t


def _children(parent: Element, kids: list[Element]) -> None:
    """把 kids 作为 parent 的子节点（复用同一 <children> 块）。"""
    ce = parent.find("children")
    if ce is None:
        ce = SubElement(parent, "children")
    te = ce.find("topics")
    if te is None:
        te = SubElement(ce, "topics", {"type": "attached"})
    for k in kids:
        te.append(k)


def _as_str(v, default: str = "") -> str:
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _format_item_title(it: dict, label: str = "") -> str:
    """将 dict 格式化为可读的 title 字符串，避免直接显示 JSON。

    按标签类型选择最佳字段组合：
    - 边界条件：field + trigger → expect
    - 数据约束：field (type): constraints
    - 业务规则：rule / description
    - 验收标准：description / criteria
    - 通用：rule/condition/criteria/constraint → description → name → JSON
    """
    if not isinstance(it, dict):
        return _as_str(it)

    # 边界条件：field + trigger → expect
    if label == "边界条件":
        field = _as_str(it.get("field"))
        trigger = _as_str(it.get("trigger") or it.get("condition"))
        expect = _as_str(it.get("expect") or it.get("expected"))
        parts = []
        if field:
            parts.append(field)
        if trigger:
            parts.append(f"触发: {trigger}")
        if expect:
            parts.append(f"预期: {expect}")
        if parts:
            return " | ".join(parts)

    # 数据约束：field (type): constraints
    if label == "数据约束":
        field = _as_str(it.get("field"))
        dtype = _as_str(it.get("type"))
        constraints = _as_str(it.get("constraints") or it.get("constraint"))
        required = it.get("required")
        parts = []
        if field:
            parts.append(field)
        if dtype:
            parts.append(f"[{dtype}]")
        if required is not None:
            parts.append("必填" if required else "选填")
        if constraints:
            parts.append(constraints)
        if parts:
            return " ".join(parts)

    # 通用匹配
    text = (
        _as_str(it.get("rule") or it.get("condition") or it.get("criteria") or it.get("constraint"))
        or _as_str(it.get("description"))
        or _as_str(it.get("name"))
        or _as_str(it.get("title"))
    )
    if text:
        desc = _as_str(it.get("description") or it.get("detail") or it.get("note"))
        if desc and desc != text:
            text = f"{text}（{desc}）"
        return text

    # 最终 fallback
    return json.dumps(it, ensure_ascii=False)


def _build_steps_node(steps: list, label: str = "主流程") -> Element | None:
    """构建流程步骤节点：每步显示 action → expected。"""
    if not steps:
        return None
    node = _topic(f"{label}（{len(steps)} 步）")
    step_nodes = []
    for i, s in enumerate(steps, 1):
        if isinstance(s, dict):
            action = _as_str(s.get("action") or s.get("step") or "")
            expected = _as_str(s.get("expected") or s.get("result") or "")
            text = f"{i}. {action}"
            if expected:
                text += f"  →  {expected}"
        else:
            text = f"{i}. {_as_str(s)}"
        step_nodes.append(_topic(text))
    if step_nodes:
        _children(node, step_nodes)
    return node


def _build_list_node(items: list, label: str, field_name: str = "") -> Element | None:
    """构建列表型节点（业务规则/边界条件等）。"""
    if not items:
        return None
    node = _topic(f"{label}（{len(items)} 条）")
    kids = []
    for it in items:
        if isinstance(it, dict):
            text = _format_item_title(it, label)
        else:
            text = _as_str(it)
        kids.append(_topic(text))
    if kids:
        _children(node, kids)
    return node


def _build_alt_flows_node(alt_flows: list) -> Element | None:
    """构建异常分支节点：每个分支显示 name + description + steps。"""
    if not alt_flows:
        return None
    node = _topic(f"异常分支（{len(alt_flows)} 个）")
    kids = []
    for i, f in enumerate(alt_flows, 1):
        if isinstance(f, dict):
            name = _as_str(f.get("name") or f.get("scenario") or f"分支 {i}")
            desc = _as_str(f.get("description") or f.get("trigger") or "")
            title = name if not desc else f"{name}（{desc}）"
            flow_node = _topic(title)
            # 分支步骤
            steps = f.get("steps") or f.get("flow") or []
            if isinstance(steps, list) and steps:
                step_nodes = []
                for j, s in enumerate(steps, 1):
                    if isinstance(s, dict):
                        action = _as_str(s.get("action") or s.get("step") or "")
                        expected = _as_str(s.get("expected") or s.get("result") or "")
                        text = f"{j}. {action}"
                        if expected:
                            text += f"  →  {expected}"
                    else:
                        text = f"{j}. {_as_str(s)}"
                    step_nodes.append(_topic(text))
                if step_nodes:
                    _children(flow_node, step_nodes)
            kids.append(flow_node)
        else:
            kids.append(_topic(_as_str(f)))
    if kids:
        _children(node, kids)
    return node


def _build_fp_node(fp: dict) -> Element | None:
    """构建单个功能点节点。"""
    if not isinstance(fp, dict):
        return None
    name = _as_str(fp.get("name") or "未命名功能点")
    fp_type = _as_str(fp.get("fp_type") or fp.get("type") or "")
    title = f"[{fp_type}] {name}" if fp_type else name
    fp_node = _topic(title)

    sub_nodes: list[Element] = []

    # 子 SRR 中的字段（v2.28 精简为 6 个核心字段）
    sub_srr = fp.get("srr_json") or fp.get("sub_srr") or {}
    if isinstance(sub_srr, dict):
        # 1. summary（兼容旧格式 requirement.summary / fp 顶层 summary）
        summary = _as_str(sub_srr.get("summary") or (sub_srr.get("requirement") or {}).get("summary") or fp.get("summary"))
        if summary:
            sub_nodes.append(_topic(f"摘要: {summary}"))

        # 2. flows（主流程 + 异常分支）
        flows = sub_srr.get("flows") or {}
        if isinstance(flows, dict):
            hp_steps = (flows.get("happy_path") or {}).get("steps", []) if isinstance(flows.get("happy_path"), dict) else flows.get("happy_path", [])
            steps_node = _build_steps_node(hp_steps if isinstance(hp_steps, list) else [], "主流程")
            if steps_node:
                sub_nodes.append(steps_node)
            alt_node = _build_alt_flows_node(flows.get("alternate_flows") or [])
            if alt_node:
                sub_nodes.append(alt_node)

        # 3-5. 业务规则 / 数据约束 / 验收标准
        for field, label in [
            ("business_rules", "业务规则"),
            ("data_constraints", "数据约束"),
            ("acceptance_criteria", "验收标准"),
        ]:
            items = sub_srr.get(field) or []
            if isinstance(items, list) and items:
                node = _build_list_node(items, label)
                if node:
                    sub_nodes.append(node)

        # 6. 非功能需求
        nf = sub_srr.get("non_functional") or {}
        if isinstance(nf, dict) and nf:
            nf_items = []
            for k, label in [("performance", "性能"), ("security", "安全"), ("compatibility", "兼容性"), ("accessibility", "可访问性")]:
                v = _as_str(nf.get(k))
                if v:
                    nf_items.append(_topic(f"{label}: {v}"))
            if nf_items:
                nf_node = _topic("非功能需求")
                _children(nf_node, nf_items)
                sub_nodes.append(nf_node)

    # 测试提示
    hints = fp.get("test_hints") or []
    if isinstance(hints, list) and hints:
        hints_node = _topic(f"测试提示（{len(hints)} 条）")
        _children(hints_node, [_topic(_as_str(h)) for h in hints])
        sub_nodes.append(hints_node)

    if sub_nodes:
        _children(fp_node, sub_nodes)
    return fp_node


def export_requirement_to_xmind(
    srr: dict,
    req_title: str = "",
    req_id: str = "",
) -> bytes:
    """把需求 SRR 导出为 XMind 思维导图。

    Args:
        srr: 需求 SRR JSON（含 requirement/flows/cross_fp_interactions/non_functional/legacy_data_impact/...）
        req_title: 需求标题（fallback，srr.requirement.title 优先）
        req_id: 需求 ID（用于根节点标题展示）

    Returns:
        bytes: .xmind ZIP 文件
    """
    srr = srr or {}
    req_block = srr.get("requirement") or {}
    if not isinstance(req_block, dict):
        req_block = {}

    root_title = _as_str(req_block.get("title")) or req_title or req_id or "需求"
    if req_id and req_id not in root_title:
        root_title = f"{root_title}（{req_id}）"

    # 根节点
    root_topic = _topic(root_title)
    root_kids: list[Element] = []

    # ===== 1. 总体 SRR（一级节点，含基本信息/流程/规则等二级节点）=====
    total_srr_kids: list[Element] = []

    # 1a. 基本信息
    info_items = []
    if req_block.get("priority"):
        info_items.append(_topic(f"优先级: {_as_str(req_block.get('priority'))}"))
    if req_block.get("type"):
        info_items.append(_topic(f"类型: {_as_str(req_block.get('type'))}"))
    if req_block.get("summary"):
        info_items.append(_topic(f"摘要: {_as_str(req_block.get('summary'))}"))
    if req_block.get("version"):
        info_items.append(_topic(f"版本: {_as_str(req_block.get('version'))}"))
    if info_items:
        info_node = _topic("基本信息")
        _children(info_node, info_items)
        total_srr_kids.append(info_node)

    # 1b. 流程
    flows = srr.get("flows") or {}
    if not isinstance(flows, dict):
        flows = {}

    happy_path = flows.get("happy_path") or {}
    if isinstance(happy_path, dict):
        hp_steps = happy_path.get("steps") or []
    else:
        hp_steps = happy_path if isinstance(happy_path, list) else []

    steps_node = _build_steps_node(hp_steps, "主流程（Happy Path）")
    if steps_node:
        total_srr_kids.append(steps_node)

    alt_node = _build_alt_flows_node(flows.get("alternate_flows") or [])
    if alt_node:
        total_srr_kids.append(alt_node)

    # 1c. 前置条件（v2.32: 移除 boundary_conditions/data_constraints/acceptance_criteria，下沉到功能点 SRR）
    # v2.34: 总 SRR 不再导出顶层 business_rules（已迁移至 cross_fp_interactions.shared_business_rules，避免与功能点 SRR 重复）
    for field, label in [
        ("preconditions", "前置条件"),
    ]:
        items = srr.get(field) or []
        if isinstance(items, list) and items:
            node = _build_list_node(items, label)
            if node:
                total_srr_kids.append(node)

    # 1c-v2.32. 跨功能点交互规则（总 SRR 核心字段，含 v2.34 迁移过来的 shared_business_rules）
    cfi = srr.get("cross_fp_interactions") or {}
    if isinstance(cfi, dict) and cfi:
        cfi_kids: list = []
        for sub_field, sub_label in [
            ("cross_fp_flows", "FP 调用顺序"),
            ("shared_business_rules", "共享业务规则"),
            ("fp_contracts", "FP 间契约"),
            ("global_constraints", "全局约束"),
        ]:
            sub_items = cfi.get(sub_field) or []
            if isinstance(sub_items, list) and sub_items:
                sub_node = _build_list_node(sub_items, sub_label)
                if sub_node:
                    cfi_kids.append(sub_node)
        if cfi_kids:
            cfi_node = _topic("跨功能点交互规则")
            _children(cfi_node, cfi_kids)
            total_srr_kids.append(cfi_node)

    # 1c-v2.32. 历史数据影响分析（仅优化需求）
    ldi = srr.get("legacy_data_impact")
    if isinstance(ldi, dict) and ldi:
        ldi_kids: list = []
        for k, label in [
            ("impact_scope", "影响范围"),
            ("user_clarification", "用户澄清"),
            ("migration_strategy", "迁移策略"),
            ("compatibility_analysis", "兼容性分析"),
            ("rollback_strategy", "回滚策略"),
            ("verification_method", "验证方法"),
        ]:
            v = _as_str(ldi.get(k))
            if v.strip():
                ldi_kids.append(_topic(f"{label}: {v}"))
        for k, label in [
            ("data_inventory", "数据盘点"),
            ("field_mapping", "字段映射"),
            ("risks", "风险与缓解"),
        ]:
            items = ldi.get(k) or []
            if isinstance(items, list) and items:
                sub_node = _build_list_node(items, label)
                if sub_node:
                    ldi_kids.append(sub_node)
        if ldi_kids:
            ldi_node = _topic("历史数据影响分析")
            _children(ldi_node, ldi_kids)
            total_srr_kids.append(ldi_node)

    # 1d. 附加详情
    additional = srr.get("additional_details") or srr.get("additional_details_text") or ""
    if _as_str(additional).strip():
        add_node = _topic("附加详情")
        _children(add_node, [_topic(_as_str(additional))])
        total_srr_kids.append(add_node)

    if total_srr_kids:
        total_srr_node = _topic("总体SRR")
        _children(total_srr_node, total_srr_kids)
        root_kids.append(total_srr_node)

    # ===== 2. 功能点（每个功能点作为独立一级节点）=====
    fps = srr.get("functional_points") or srr.get("fps") or []
    if isinstance(fps, list) and fps:
        for fp in fps:
            if isinstance(fp, dict):
                node = _build_fp_node(fp)
                if node:
                    root_kids.append(node)

    if root_kids:
        _children(root_topic, root_kids)

    # ===== 构建 sheet =====
    sheet = Element("sheet", {"id": _id(), "timestamp": str(int(datetime.now().timestamp() * 1000))})
    SubElement(sheet, "title").text = root_title
    sheet.append(root_topic)

    xmap = Element("xmap-content", {
        "xmlns": "urn:xmind:xmap:xmlns:content:2.0",
        "xmlns:fo": "http://www.w3.org/1999/XSL/Format",
        "xmlns:svg": "http://www.w3.org/2000/svg",
        "xmlns:xhtml": "http://www.w3.org/1999/xhtml",
        "xmlns:xlink": "http://www.w3.org/1999/xlink",
        "version": "2.0",
    })
    xmap.append(sheet)

    xml_bytes = b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
    xml_bytes += tostring(xmap, encoding="unicode").encode("utf-8")

    # meta.xml — XMind 8 兼容必需的元数据文件
    meta_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        b'<meta xmlns="urn:xmind:xmap:xmlns:meta:2.0">\n'
        b'  <creator name="AI\xe6\xb5\x8b\xe8\xaf\x95\xe7\x94\xa8\xe4\xbe\x8b\xe7\x94\x9f\xe6\x88\x90\xe7\xb3\xbb\xe7\xbb\x9f" version="1.0"/>\n'
        + f'  <created value="{datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}"/>\n'.encode("utf-8") +
        b'</meta>\n'
    )

    # META-INF/manifest.xml — XMind 8 兼容必需的清单文件（XML 格式，非 JSON）
    manifest_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        b'<manifest xmlns="urn:xmind:xmap:xmlns:manifest:1.0">\n'
        b'  <file-entry full-path="content.xml" media-type="text/xml"/>\n'
        b'  <file-entry full-path="meta.xml" media-type="text/xml"/>\n'
        b'</manifest>\n'
    )

    # ZIP 打包 — 同时包含新旧两种 manifest 格式以最大化兼容性
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.xml", xml_bytes)
        zf.writestr("META-INF/manifest.xml", manifest_xml)
        zf.writestr("meta.xml", meta_xml)
        zf.writestr("manifest.json", json.dumps({"file-entries": {"content.xml": {}, "metadata.json": {}, "meta.xml": {}}}).encode("utf-8"))
        zf.writestr("metadata.json", json.dumps({"creator": {"name": "AI测试用例生成系统", "version": "1.0"}}, ensure_ascii=False).encode("utf-8"))
    return buf.getvalue()


# ============================================================================
# JSON 格式导出（XMind 2020+ / Zen / 2022+）
# ============================================================================

def _jid() -> str:
    """生成 24 字符 hex ID（与 XMind 标准兼容）。"""
    return uuid.uuid4().hex[:24]


def _jtopic(title: str, tid: str | None = None) -> dict:
    """创建独立 topic dict（XMind JSON 格式）。"""
    return {"id": tid or _jid(), "title": str(title)}


def _jattach(parent: dict, kids: list[dict]) -> dict:
    """把 kids 作为 parent 的子节点（attached 类型）。"""
    children = parent.setdefault("children", {})
    attached = children.setdefault("attached", [])
    attached.extend(kids)
    return parent


def _jbuild_steps(steps: list, label: str = "主流程") -> dict | None:
    """构建流程步骤节点（JSON 版）：每步显示 action → expected。"""
    if not steps:
        return None
    node = _jtopic(f"{label}（{len(steps)} 步）")
    step_nodes = []
    for i, s in enumerate(steps, 1):
        if isinstance(s, dict):
            action = _as_str(s.get("action") or s.get("step") or "")
            expected = _as_str(s.get("expected") or s.get("result") or "")
            text = f"{i}. {action}"
            if expected:
                text += f"  →  {expected}"
        else:
            text = f"{i}. {_as_str(s)}"
        step_nodes.append(_jtopic(text))
    if step_nodes:
        _jattach(node, step_nodes)
    return node


def _jbuild_list(items: list, label: str) -> dict | None:
    """构建列表型节点（JSON 版）：业务规则/边界条件等。"""
    if not items:
        return None
    node = _jtopic(f"{label}（{len(items)} 条）")
    kids = []
    for it in items:
        if isinstance(it, dict):
            text = _format_item_title(it, label)
        else:
            text = _as_str(it)
        kids.append(_jtopic(text))
    if kids:
        _jattach(node, kids)
    return node


def _jbuild_alt_flows(alt_flows: list) -> dict | None:
    """构建异常分支节点（JSON 版）：每个分支显示 name + description + steps。"""
    if not alt_flows:
        return None
    node = _jtopic(f"异常分支（{len(alt_flows)} 个）")
    kids = []
    for i, f in enumerate(alt_flows, 1):
        if isinstance(f, dict):
            name = _as_str(f.get("name") or f.get("scenario") or f"分支 {i}")
            desc = _as_str(f.get("description") or f.get("trigger") or "")
            title = name if not desc else f"{name}（{desc}）"
            flow_node = _jtopic(title)
            steps = f.get("steps") or f.get("flow") or []
            if isinstance(steps, list) and steps:
                step_nodes = []
                for j, s in enumerate(steps, 1):
                    if isinstance(s, dict):
                        action = _as_str(s.get("action") or s.get("step") or "")
                        expected = _as_str(s.get("expected") or s.get("result") or "")
                        text = f"{j}. {action}"
                        if expected:
                            text += f"  →  {expected}"
                    else:
                        text = f"{j}. {_as_str(s)}"
                    step_nodes.append(_jtopic(text))
                if step_nodes:
                    _jattach(flow_node, step_nodes)
            kids.append(flow_node)
        else:
            kids.append(_jtopic(_as_str(f)))
    if kids:
        _jattach(node, kids)
    return node


def _jbuild_fp(fp: dict) -> dict | None:
    """构建单个功能点节点（JSON 版）。"""
    if not isinstance(fp, dict):
        return None
    name = _as_str(fp.get("name") or "未命名功能点")
    fp_type = _as_str(fp.get("fp_type") or fp.get("type") or "")
    title = f"[{fp_type}] {name}" if fp_type else name
    fp_node = _jtopic(title)

    sub_nodes: list[dict] = []

    # 子 SRR 中的字段（v2.28 精简为 6 个核心字段）
    sub_srr = fp.get("srr_json") or fp.get("sub_srr") or {}
    if isinstance(sub_srr, dict):
        # 1. summary（兼容旧格式 requirement.summary / fp 顶层 summary）
        summary = _as_str(sub_srr.get("summary") or (sub_srr.get("requirement") or {}).get("summary") or fp.get("summary"))
        if summary:
            sub_nodes.append(_jtopic(f"摘要: {summary}"))

        # 2. flows（主流程 + 异常分支）
        flows = sub_srr.get("flows") or {}
        if isinstance(flows, dict):
            hp_steps = (flows.get("happy_path") or {}).get("steps", []) if isinstance(flows.get("happy_path"), dict) else flows.get("happy_path", [])
            steps_node = _jbuild_steps(hp_steps if isinstance(hp_steps, list) else [], "主流程")
            if steps_node:
                sub_nodes.append(steps_node)
            alt_node = _jbuild_alt_flows(flows.get("alternate_flows") or [])
            if alt_node:
                sub_nodes.append(alt_node)

        # 3-5. 业务规则 / 数据约束 / 验收标准
        for field, label in [
            ("business_rules", "业务规则"),
            ("data_constraints", "数据约束"),
            ("acceptance_criteria", "验收标准"),
        ]:
            items = sub_srr.get(field) or []
            if isinstance(items, list) and items:
                node = _jbuild_list(items, label)
                if node:
                    sub_nodes.append(node)

        # 6. 非功能需求
        nf = sub_srr.get("non_functional") or {}
        if isinstance(nf, dict) and nf:
            nf_items = []
            for k, label in [("performance", "性能"), ("security", "安全"), ("compatibility", "兼容性"), ("accessibility", "可访问性")]:
                v = _as_str(nf.get(k))
                if v:
                    nf_items.append(_jtopic(f"{label}: {v}"))
            if nf_items:
                nf_node = _jtopic("非功能需求")
                _jattach(nf_node, nf_items)
                sub_nodes.append(nf_node)

    hints = fp.get("test_hints") or []
    if isinstance(hints, list) and hints:
        hints_node = _jtopic(f"测试提示（{len(hints)} 条）")
        hint_kids = [_jtopic(_as_str(h)) for h in hints if h]
        if hint_kids:
            _jattach(hints_node, hint_kids)
            sub_nodes.append(hints_node)

    if sub_nodes:
        _jattach(fp_node, sub_nodes)
    return fp_node


def export_requirement_to_xmind_json(
    srr: dict,
    req_title: str = "",
    req_id: str = "",
) -> bytes:
    """把需求 SRR 导出为 XMind 思维导图 JSON 格式（兼容 XMind 2020+ / Zen / 2022+）。

    树结构与 :func:`export_requirement_to_xmind` 一致，区别仅在输出格式：
    - JSON 版：content.json（XMind 2020+ 标准）
    - XML 版：content.xml（XMAP 2.0，XMind 8 兼容）

    Args:
        srr: 需求 SRR JSON
        req_title: 需求标题（fallback）
        req_id: 需求 ID（用于根节点标题展示）

    Returns:
        bytes: .xmind ZIP 文件
    """
    srr = srr or {}
    req_block = srr.get("requirement") or {}
    if not isinstance(req_block, dict):
        req_block = {}

    root_title = _as_str(req_block.get("title")) or req_title or req_id or "需求"
    if req_id and req_id not in root_title:
        root_title = f"{root_title}（{req_id}）"

    root_topic = _jtopic(root_title)
    root_kids: list[dict] = []

    # ===== 1. 总体 SRR（一级节点，含基本信息/流程/规则等二级节点）=====
    total_srr_kids: list[dict] = []

    # 1a. 基本信息
    info_items = []
    if req_block.get("priority"):
        info_items.append(_jtopic(f"优先级: {_as_str(req_block.get('priority'))}"))
    if req_block.get("type"):
        info_items.append(_jtopic(f"类型: {_as_str(req_block.get('type'))}"))
    if req_block.get("summary"):
        info_items.append(_jtopic(f"摘要: {_as_str(req_block.get('summary'))}"))
    if req_block.get("version"):
        info_items.append(_jtopic(f"版本: {_as_str(req_block.get('version'))}"))
    if info_items:
        info_node = _jtopic("基本信息")
        _jattach(info_node, info_items)
        total_srr_kids.append(info_node)

    # 1b. 流程
    flows = srr.get("flows") or {}
    if not isinstance(flows, dict):
        flows = {}

    happy_path = flows.get("happy_path") or {}
    if isinstance(happy_path, dict):
        hp_steps = happy_path.get("steps") or []
    else:
        hp_steps = happy_path if isinstance(happy_path, list) else []

    steps_node = _jbuild_steps(hp_steps, "主流程（Happy Path）")
    if steps_node:
        total_srr_kids.append(steps_node)

    alt_node = _jbuild_alt_flows(flows.get("alternate_flows") or [])
    if alt_node:
        total_srr_kids.append(alt_node)

    # 1c. 前置条件（v2.32: 移除 boundary_conditions/data_constraints/acceptance_criteria，下沉到功能点 SRR）
    # v2.34: 总 SRR 不再导出顶层 business_rules（已迁移至 cross_fp_interactions.shared_business_rules，避免与功能点 SRR 重复）
    for field, label in [
        ("preconditions", "前置条件"),
    ]:
        items = srr.get(field) or []
        if isinstance(items, list) and items:
            node = _jbuild_list(items, label)
            if node:
                total_srr_kids.append(node)

    # 1c-v2.32. 跨功能点交互规则（总 SRR 核心字段，含 v2.34 迁移过来的 shared_business_rules）
    cfi = srr.get("cross_fp_interactions") or {}
    if isinstance(cfi, dict) and cfi:
        cfi_kids: list = []
        for sub_field, sub_label in [
            ("cross_fp_flows", "FP 调用顺序"),
            ("shared_business_rules", "共享业务规则"),
            ("fp_contracts", "FP 间契约"),
            ("global_constraints", "全局约束"),
        ]:
            sub_items = cfi.get(sub_field) or []
            if isinstance(sub_items, list) and sub_items:
                sub_node = _jbuild_list(sub_items, sub_label)
                if sub_node:
                    cfi_kids.append(sub_node)
        if cfi_kids:
            cfi_node = _jtopic("跨功能点交互规则")
            _jattach(cfi_node, cfi_kids)
            total_srr_kids.append(cfi_node)

    # 1c-v2.32. 历史数据影响分析（仅优化需求）
    ldi = srr.get("legacy_data_impact")
    if isinstance(ldi, dict) and ldi:
        ldi_kids: list = []
        for k, label in [
            ("impact_scope", "影响范围"),
            ("user_clarification", "用户澄清"),
            ("migration_strategy", "迁移策略"),
            ("compatibility_analysis", "兼容性分析"),
            ("rollback_strategy", "回滚策略"),
            ("verification_method", "验证方法"),
        ]:
            v = _as_str(ldi.get(k))
            if v.strip():
                ldi_kids.append(_jtopic(f"{label}: {v}"))
        for k, label in [
            ("data_inventory", "数据盘点"),
            ("field_mapping", "字段映射"),
            ("risks", "风险与缓解"),
        ]:
            items = ldi.get(k) or []
            if isinstance(items, list) and items:
                sub_node = _jbuild_list(items, label)
                if sub_node:
                    ldi_kids.append(sub_node)
        if ldi_kids:
            ldi_node = _jtopic("历史数据影响分析")
            _jattach(ldi_node, ldi_kids)
            total_srr_kids.append(ldi_node)

    # 1d. 附加详情
    additional = srr.get("additional_details") or srr.get("additional_details_text") or ""
    if _as_str(additional).strip():
        add_node = _jtopic("附加详情")
        _jattach(add_node, [_jtopic(_as_str(additional))])
        total_srr_kids.append(add_node)

    if total_srr_kids:
        total_srr_node = _jtopic("总体SRR")
        _jattach(total_srr_node, total_srr_kids)
        root_kids.append(total_srr_node)

    # ===== 2. 功能点（每个功能点作为独立一级节点）=====
    fps = srr.get("functional_points") or srr.get("fps") or []
    if isinstance(fps, list) and fps:
        for fp in fps:
            if isinstance(fp, dict):
                node = _jbuild_fp(fp)
                if node:
                    root_kids.append(node)

    if root_kids:
        _jattach(root_topic, root_kids)

    # 构建 content.json
    sheet = {
        "id": _jid(),
        "class": "sheet",
        "title": root_title,
        "rootTopic": root_topic,
        "fileVersion": "2.0",
    }
    content_json = [sheet]

    manifest = {
        "file-entries": {
            "content.json": {},
            "metadata.json": {},
        }
    }
    metadata = {
        "creator": {
            "name": "AI测试用例生成系统",
            "version": "1.0",
        },
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "content.json",
            json.dumps(content_json, ensure_ascii=False, separators=(",", ": ")),
        )
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False))
    return buf.getvalue()


__all__ = ["export_requirement_to_xmind", "export_requirement_to_xmind_json"]
