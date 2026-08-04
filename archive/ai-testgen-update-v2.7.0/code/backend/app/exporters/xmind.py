"""XMind 思维导图格式导出器（JSON 格式，兼容 XMind 2020+ / Zen / 2022+）。

输出 .xmind 文件（ZIP 压缩包），包含：
    content.json   - 工作表 JSON 数组（核心内容）
    manifest.json  - 文件清单
    metadata.json  - 创建者元信息

树结构（按需求→功能点→用例→步骤）：
    项目根
      └── REQ-001 需求标题
          └── [功能点] 登录流程
              └── [functional] 正常登录验证
                  ├── 步骤1: 输入账号密码 → 登录成功
                  └── 前置: 已部署测试环境
"""

from __future__ import annotations

import io, json, uuid, zipfile
from datetime import datetime, timezone


def _id() -> str:
    """生成 24 字符 hex ID（与 XMind 标准兼容，类 ObjectId 格式）。"""
    # uuid4 hex 是 32 字符，截取前 24 位即可
    return uuid.uuid4().hex[:24]


def _topic(title: str, tid: str | None = None) -> dict:
    """创建独立 topic dict（XMind JSON 格式）。"""
    return {
        "id": tid or _id(),
        "title": str(title),
    }


def _attach_children(parent: dict, kids: list[dict]) -> dict:
    """把 kids 作为 parent 的子节点（attached 类型）。

    若 parent 已有 children.attached，则追加；否则新建。
    """
    children = parent.setdefault("children", {})
    attached = children.setdefault("attached", [])
    attached.extend(kids)
    return parent


def _case_type_label(t: str) -> str:
    """用例类型转中文标签。"""
    labels = {
        "functional": "功能", "boundary": "边界", "exception": "异常",
        "security": "安全", "performance": "性能", "regression": "回归",
        "integration": "集成", "ui": "界面", "api": "接口", "data": "数据",
    }
    return labels.get(t, t)


def export_to_xmind(cases: list[dict], project_name: str = "", include_details: bool = True) -> bytes:
    """导出为 XMind 思维导图 JSON 格式。

    分组逻辑（v2.52: 对齐需求分析 XMind 结构）：
    1. 按需求 ID 分组
    2. 需求下先分"业务场景"节点（跨功能点/整体性用例），再按功能点分组
    3. 功能点下展示用例 + 步骤 + 前置条件

    结构示例：
        <root>
          └── 需求标题
              ├── 业务场景
              │   └── [性能] 整体性能测试
              ├── 登录流程（功能点）
              │   └── [功能] 正常登录验证
              │       ├── 步骤1: 输入账号密码 → 预期: 登录成功
              │       └── 前置条件: 已部署测试环境
              └── 注册流程（功能点）
                  └── [边界] 边界值测试

    Args:
        cases: 用例列表，每项含 req_id/req_title/title/type/functional_point/preconditions/steps
        project_name: 所属项目名称（作为根节点标题）
        include_details: 是否包含详细步骤和前置条件，False 时用例仅保留标题

    Returns:
        bytes: .xmind ZIP 二进制
    """
    root_title = project_name or "测试用例"

    # 1. 按需求分组
    req_groups: dict[str, dict] = {}
    for c in cases:
        rid = c.get("req_id") or c.get("related_requirement") or "未知"
        if rid not in req_groups:
            req_groups[rid] = {
                "title": c.get("req_title") or f"需求 {rid}",
                "project_name": c.get("project_name", ""),
                "fp_groups": {},
            }
        # 2. 按功能点分组：无功能点归属的用例归入"业务场景"
        fp = (c.get("functional_point") or c.get("related_fp") or "").strip()
        if not fp:
            fp = "业务场景"
        if fp not in req_groups[rid]["fp_groups"]:
            req_groups[rid]["fp_groups"][fp] = []
        req_groups[rid]["fp_groups"][fp].append(c)

    # 3. 构建 JSON 树
    root_topic = _topic(root_title)

    req_nodes: list[dict] = []
    for rid, rg in sorted(req_groups.items()):
        # 需求节点：项目名 - 需求标题（跨项目时区分）
        proj_name = rg.get("project_name", "")
        req_title = rg["title"]
        req_label = f"{proj_name} - {req_title}" if proj_name else req_title
        req_node = _topic(req_label)

        fp_nodes: list[dict] = []
        # "业务场景"排在最前（与需求分析 XMind 结构一致），其余功能点按字母序
        fp_names = sorted(rg["fp_groups"].keys(), key=lambda x: (x != "业务场景", x))
        for fp_name in fp_names:
            fp_cases = rg["fp_groups"][fp_name]
            fp_node = _topic(fp_name)

            case_nodes: list[dict] = []
            for c in fp_cases:
                # 用例类型使用中文标签
                type_label = _case_type_label(c.get("type", ""))
                case_title = f"[{type_label}] {c.get('title', '')}" if type_label else c.get("title", "")
                case_node = _topic(case_title)

                if include_details:
                    # 步骤作为子节点
                    steps = c.get("steps") or (c.get("case_json") or {}).get("steps", [])
                    step_nodes: list[dict] = []
                    for i, s in enumerate(steps):
                        if not isinstance(s, dict):
                            continue
                        action = s.get("action", "")
                        if not action:
                            continue
                        expected = s.get("expected") or s.get("expect", "")
                        step_title = f"步骤{i + 1}: {action}"
                        if expected:
                            step_title += f"  →  预期: {expected}"
                        step_nodes.append(_topic(step_title))
                    if step_nodes:
                        _attach_children(case_node, step_nodes)

                    # 前置条件（追加到同一 children.attached）
                    precond = (c.get("preconditions")
                               or (c.get("case_json") or {}).get("preconditions", []))
                    if precond:
                        precond_nodes = [_topic(f"前置条件: {p}") for p in precond if p]
                        if precond_nodes:
                            _attach_children(case_node, precond_nodes)

                case_nodes.append(case_node)

            if case_nodes:
                _attach_children(fp_node, case_nodes)
            fp_nodes.append(fp_node)

        if fp_nodes:
            _attach_children(req_node, fp_nodes)
        req_nodes.append(req_node)

    if req_nodes:
        _attach_children(root_topic, req_nodes)

    # 4. 构建 content.json（数组，含一个 sheet）
    sheet = {
        "id": _id(),
        "class": "sheet",
        "title": root_title,
        "rootTopic": root_topic,
        "fileVersion": "2.0",
        # XMind 2020+ 期望 creator 在 metadata.json 中，但 content.json 也可以含
    }
    content_json = [sheet]

    # 5. manifest.json - 必须列出包内所有文件
    manifest = {
        "file-entries": {
            "content.json": {},
            "metadata.json": {},
        }
    }

    # 6. metadata.json
    metadata = {
        "creator": {
            "name": "AI测试用例生成系统",
            "version": "1.0",
        },
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "case_count": len(cases),
    }

    # 7. ZIP 打包（XMind 期望文件顺序：content.json 在前）
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "content.json",
            json.dumps(content_json, ensure_ascii=False, separators=(",", ": ")),
        )
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False))
    return buf.getvalue()


# 兼容别名（验证脚本/旧代码可能用 export_xmind 而非 export_to_xmind）
export_xmind = export_to_xmind


# ============================================================================
# XMind 8 XML 导出（content.xml + XMAP 2.0 命名空间，兼容 XMind 8）
# ============================================================================

import uuid as _uuid
from xml.etree.ElementTree import Element, SubElement, tostring as _tostring


def _xid() -> str:
    """生成 32 字符 hex ID（XMAP topic id 格式）。"""
    return _uuid.uuid4().hex


def _xtopic(title: str, tid: str | None = None) -> Element:
    """创建 XMAP topic 元素。"""
    t = Element("topic", {"id": tid or _xid(), "timestamp": str(int(datetime.now().timestamp() * 1000))})
    te = SubElement(t, "title")
    te.text = str(title)
    return t


def _xchildren(parent: Element, kids: list[Element]) -> None:
    """把 kids 附加到 parent 的 <children><topics type=\"attached\">。"""
    ce = parent.find("children")
    if ce is None:
        ce = SubElement(parent, "children")
    te = ce.find("topics")
    if te is None:
        te = SubElement(ce, "topics", {"type": "attached"})
    for k in kids:
        te.append(k)


def export_to_xmind_xml(cases: list[dict], project_name: str = "", include_details: bool = True) -> bytes:
    """导出用例为 XMind 8 兼容的 XMAP 2.0 XML 格式。

    XMind 8 不支持 content.json 格式（仅支持 content.xml + XMAP 命名空间）。
    本函数构建与 export_to_xmind 相同的树结构，但输出为 XMAP 2.0 XML。

    树结构（需求→功能点→用例→步骤）：
        <root>
          └── 需求标题
              └── 登录流程（功能点）
                  └── [功能] 正常登录验证
                      ├── 步骤1: 输入账号密码 → 预期: 登录成功
                      └── 前置条件: 已部署测试环境
    """
    root_title = project_name or "测试用例"

    # 1. 分组（复用 JSON 版逻辑，v2.52: 无功能点归属的用例归入"业务场景"）
    req_groups: dict[str, dict] = {}
    for c in cases:
        rid = c.get("req_id") or c.get("related_requirement") or "未知"
        if rid not in req_groups:
            req_groups[rid] = {"title": c.get("req_title") or f"需求 {rid}", "project_name": c.get("project_name", ""), "fp_groups": {}}
        fp = (c.get("functional_point") or c.get("related_fp") or "").strip()
        if not fp:
            fp = "业务场景"
        if fp not in req_groups[rid]["fp_groups"]:
            req_groups[rid]["fp_groups"][fp] = []
        req_groups[rid]["fp_groups"][fp].append(c)

    # 2. 构建 XML 树
    root_topic = _xtopic(root_title)

    req_nodes: list[Element] = []
    for rid, rg in sorted(req_groups.items()):
        # 需求节点：项目名 - 需求标题（跨项目时区分）
        proj_name = rg.get("project_name", "")
        req_title = rg["title"]
        req_label = f"{proj_name} - {req_title}" if proj_name else req_title
        req_node = _xtopic(req_label)
        fp_nodes: list[Element] = []
        # "业务场景"排在最前（与需求分析 XMind 结构一致），其余功能点按字母序
        fp_names = sorted(rg["fp_groups"].keys(), key=lambda x: (x != "业务场景", x))
        for fp_name in fp_names:
            fp_cases = rg["fp_groups"][fp_name]
            fp_node = _xtopic(fp_name)
            case_nodes: list[Element] = []
            for c in fp_cases:
                # 用例类型使用中文标签
                type_label = _case_type_label(c.get("type", ""))
                case_title = f"[{type_label}] {c.get('title', '')}" if type_label else c.get("title", "")
                case_node = _xtopic(case_title)

                if include_details:
                    # 步骤
                    steps = c.get("steps") or (c.get("case_json") or {}).get("steps", [])
                    step_kids: list[Element] = []
                    for i, s in enumerate(steps):
                        if not isinstance(s, dict):
                            continue
                        action = s.get("action", "")
                        if not action:
                            continue
                        expected = s.get("expected") or s.get("expect", "")
                        text = f"步骤{i + 1}: {action}"
                        if expected:
                            text += f"  ->  预期: {expected}"
                        step_kids.append(_xtopic(text))
                    if step_kids:
                        _xchildren(case_node, step_kids)

                    # 前置条件
                    precond = c.get("preconditions") or (c.get("case_json") or {}).get("preconditions", [])
                    if precond:
                        pre_kids = [_xtopic(f"前置条件: {p}") for p in precond if p]
                        if pre_kids:
                            _xchildren(case_node, pre_kids)

                case_nodes.append(case_node)
            if case_nodes:
                _xchildren(fp_node, case_nodes)
            fp_nodes.append(fp_node)
        if fp_nodes:
            _xchildren(req_node, fp_nodes)
        req_nodes.append(req_node)
    if req_nodes:
        _xchildren(root_topic, req_nodes)

    # 3. 构建 XMAP 2.0 XML
    sheet = Element("sheet", {"id": _xid(), "timestamp": str(int(datetime.now().timestamp() * 1000))})
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

    xml_bytes = b"<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"no\"?>\n"
    xml_bytes += _tostring(xmap, encoding="unicode").encode("utf-8")

    # 4. ZIP 打包
    import json as _json
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.xml", xml_bytes)
        zf.writestr("manifest.json", _json.dumps({"file-entries": {"content.xml": {}, "metadata.json": {}}}))
        zf.writestr("metadata.json", _json.dumps({"creator": {"name": "AI测试用例生成系统", "version": "1.0"}}))
    return buf.getvalue()
