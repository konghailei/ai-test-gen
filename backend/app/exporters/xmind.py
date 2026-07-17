"""XMind 思维导图格式导出器（XMAP 2.0 XML）。

输出 .xmind 文件，兼容 XMind 8 / 2020 / 2022+ 全系。

树结构（按项目→需求→功能点/类型→用例）：
    项目名称（根）
      ├── REQ-001 需求标题
      │   ├── [功能点] 登录流程
      │   │   ├── [functional] 正常登录验证
      │   │   │   ├── 步骤1: 输入账号密码 → 登录成功
      │   │   │   └── ...
      │   │   └── ...
      │   └── [未分类]
      └── REQ-002 需求标题
"""

from __future__ import annotations

import io, json, uuid, zipfile
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, tostring


def _id(): return uuid.uuid4().hex


def _topic(parent: Element, title: str, tid: str | None = None) -> Element:
    t = SubElement(parent, "topic", {"id": tid or _id(), "timestamp": str(int(datetime.now().timestamp() * 1000))})
    te = SubElement(t, "title"); te.text = str(title)
    return t


def _children(parent: Element, kids: list[Element]) -> None:
    ce = SubElement(parent, "children")
    te = SubElement(ce, "topics", {"type": "attached"})
    for k in kids: te.append(k)


def export_to_xmind(cases: list[dict], project_name: str = "") -> bytes:
    """导出为 XMind 思维导图 XML。

    分组逻辑（二级）：
    1. 按需求 ID 分组
    2. 需求下按功能点分组（有 FP 用 FP，无 FP 用类型兜底）
    3. 功能点下展示用例 + 步骤

    Args:
        cases: 用例列表，每项含 req_id/req_title/title/type/functional_point/preconditions/steps
        project_name: 所属项目名称（作为根节点标题）

    Returns:
        bytes: .xmind ZIP
    """
    root_title = project_name or "测试用例"

    # 1. 按需求分组
    req_groups: dict[str, dict] = {}
    for c in cases:
        rid = c.get("req_id", "") or "未知"
        if rid not in req_groups:
            req_groups[rid] = {
                "title": c.get("req_title", "") or f"需求 {rid}",
                "fp_groups": {},
            }
        # 2. 需求下按功能点分组
        fp = c.get("functional_point", "") or c.get("related_fp", "") or f"[{c.get('type', '用例')}]"
        if fp not in req_groups[rid]["fp_groups"]:
            req_groups[rid]["fp_groups"][fp] = []
        req_groups[rid]["fp_groups"][fp].append(c)

    # 3. 构建 XML 树
    sheet = Element("sheet", {"id": _id(), "timestamp": str(int(datetime.now().timestamp() * 1000))})
    SubElement(sheet, "title").text = root_title
    root_topic = _topic(sheet, root_title)

    req_nodes = []
    for rid, rg in sorted(req_groups.items()):
        req_node = Element("topic", {"id": _id(), "timestamp": str(int(datetime.now().timestamp() * 1000))})
        SubElement(req_node, "title").text = f"{rid} {rg['title']}"

        fp_nodes = []
        for fp_name, fp_cases in sorted(rg["fp_groups"].items()):
            fp_node = _topic(req_node, fp_name)
            case_nodes = []
            for c in fp_cases:
                case_node = _topic(req_node, f"[{c.get('type', '')}] {c.get('title', '')}")
                # 步骤作为子节点
                steps = c.get("steps") or (c.get("case_json") or {}).get("steps", [])
                if steps:
                    step_nodes = [_topic(req_node, f"{s.get('step', i+1)}. {s.get('action', '')}  →  {s.get('expected', '')}")
                                  for i, s in enumerate(steps) if isinstance(s, dict) and s.get('action')]
                    if step_nodes: _children(case_node, step_nodes)
                # 前置条件
                precond = c.get("preconditions") or (c.get("case_json") or {}).get("preconditions", [])
                if precond:
                    _children(case_node, [_topic(req_node, f"前置: {p}") for p in precond if p])
                case_nodes.append(case_node)
            if case_nodes: _children(fp_node, case_nodes)
            fp_nodes.append(fp_node)

        if fp_nodes: _children(req_node, fp_nodes)
        req_nodes.append(req_node)

    if req_nodes: _children(root_topic, req_nodes)

    # 4. 构建 content.xml
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

    # 5. ZIP 打包
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.xml", xml_bytes)
        zf.writestr("manifest.json", json.dumps({"file-entries": {"content.xml": {}, "metadata.json": {}}}).encode("utf-8"))
        zf.writestr("metadata.json", json.dumps({"creator": {"name": "AI-Test-Gen", "version": "1.0"}}).encode("utf-8"))
    return buf.getvalue()


def _case_type_label(t: str) -> str:
    return {"functional": "功能", "boundary": "边界", "exception": "异常", "security": "安全", "performance": "性能"}.get(t, t)
