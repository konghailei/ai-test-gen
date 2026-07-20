"""需求 SRR → XMind 思维导图导出器（XMAP 2.0 XML）。

把需求 SRR 结构导出为 .xmind 文件，便于评审/分享/归档。

树结构（按 SRR 字段组织）：
    需求标题（根）
      ├── 基本信息（优先级 / 类型 / 摘要）
      ├── 主流程（Happy Path）
      │   └── 步骤1: action → expected
      ├── 异常分支（Alternate Flows）
      │   └── 分支1: ...
      ├── 业务规则
      ├── 边界条件
      ├── 数据约束
      ├── 验收标准
      ├── 前置条件
      └── 功能点（如有）
          └── FP1: ...
"""

from __future__ import annotations

import io, json, uuid, zipfile
from datetime import datetime
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
            # 常见字段名优先级
            text = (
                _as_str(it.get("rule") or it.get("condition") or it.get("criteria") or it.get("constraint"))
                or _as_str(it.get("description"))
                or _as_str(it.get("name"))
                or json.dumps(it, ensure_ascii=False)
            )
            # 附加说明（如有）
            desc = _as_str(it.get("description") or it.get("detail") or it.get("note"))
            if desc and desc != text:
                text = f"{text}（{desc}）"
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
    summary = _as_str(fp.get("summary"))
    if summary:
        sub_nodes.append(_topic(f"摘要: {summary}"))

    # 子 SRR 中的字段
    sub_srr = fp.get("srr_json") or fp.get("sub_srr") or {}
    if isinstance(sub_srr, dict):
        flows = sub_srr.get("flows") or {}
        if isinstance(flows, dict):
            hp_steps = (flows.get("happy_path") or {}).get("steps", []) if isinstance(flows.get("happy_path"), dict) else flows.get("happy_path", [])
            steps_node = _build_steps_node(hp_steps if isinstance(hp_steps, list) else [], "主流程")
            if steps_node:
                sub_nodes.append(steps_node)
            alt_node = _build_alt_flows_node(flows.get("alternate_flows") or [])
            if alt_node:
                sub_nodes.append(alt_node)

        for field, label in [
            ("business_rules", "业务规则"),
            ("boundary_conditions", "边界条件"),
            ("data_constraints", "数据约束"),
            ("acceptance_criteria", "验收标准"),
            ("preconditions", "前置条件"),
        ]:
            items = sub_srr.get(field) or []
            if isinstance(items, list) and items:
                node = _build_list_node(items, label)
                if node:
                    sub_nodes.append(node)

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
        srr: 需求 SRR JSON（含 requirement/flows/business_rules/...）
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

    # ===== 1. 基本信息 =====
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
        _children(root_topic, [_topic("基本信息")] + [])
        # 基本信息作为单独子节点
        info_node = _topic("基本信息")
        _children(info_node, info_items)
        # 替换上面的占位（重做：直接 append info_node）
        # 上面 _children(root_topic, [_topic("基本信息")]) 已经添加了一个空"基本信息"节点，
        # 这里改为先移除再添加，避免重复。简化做法：重新构建 root_topic 的 children。
        # 直接重新创建：
        root_topic = _topic(root_title)
        _children(root_topic, [info_node])

    # ===== 2. 流程 =====
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
        _children(root_topic, [steps_node])

    alt_node = _build_alt_flows_node(flows.get("alternate_flows") or [])
    if alt_node:
        _children(root_topic, [alt_node])

    # ===== 3. 业务规则 / 边界条件 / 数据约束 / 验收标准 / 前置条件 =====
    for field, label in [
        ("business_rules", "业务规则"),
        ("boundary_conditions", "边界条件"),
        ("data_constraints", "数据约束"),
        ("acceptance_criteria", "验收标准"),
        ("preconditions", "前置条件"),
    ]:
        items = srr.get(field) or []
        if isinstance(items, list) and items:
            node = _build_list_node(items, label)
            if node:
                _children(root_topic, [node])

    # ===== 4. 功能点（Functional Points）=====
    fps = srr.get("functional_points") or srr.get("fps") or []
    if isinstance(fps, list) and fps:
        fp_root = _topic(f"功能点（{len(fps)} 个）")
        fp_nodes = []
        for fp in fps:
            if isinstance(fp, dict):
                node = _build_fp_node(fp)
                if node:
                    fp_nodes.append(node)
        if fp_nodes:
            _children(fp_root, fp_nodes)
            _children(root_topic, [fp_root])

    # ===== 5. 附加详情 =====
    additional = srr.get("additional_details") or srr.get("additional_details_text") or ""
    if _as_str(additional).strip():
        add_node = _topic("附加详情")
        _children(add_node, [_topic(_as_str(additional))])
        _children(root_topic, [add_node])

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

    # ZIP 打包
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.xml", xml_bytes)
        zf.writestr("manifest.json", json.dumps({"file-entries": {"content.xml": {}, "metadata.json": {}}}).encode("utf-8"))
        zf.writestr("metadata.json", json.dumps({"creator": {"name": "AI测试用例生成系统", "version": "1.0"}}).encode("utf-8"))
    return buf.getvalue()


__all__ = ["export_requirement_to_xmind"]
