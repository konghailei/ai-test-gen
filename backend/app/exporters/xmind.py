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

import io, json, re, uuid, zipfile
from datetime import datetime, timezone


def _id() -> str:
    """生成 24 字符 hex ID（与 XMind 标准兼容，类 ObjectId 格式）。"""
    # uuid4 hex 是 32 字符，截取前 24 位即可
    return uuid.uuid4().hex[:24]


def _topic(title: str, tid: str | None = None) -> dict:
    """创建独立 topic dict（XMind JSON 格式）。

    v3.2: 补齐官方规范字段 class='topic'（XMind 官方 content.json 必备），
    缺失时部分客户端解析会降级或渲染异常。
    """
    return {
        "id": tid or _id(),
        "class": "topic",
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
    """用例类型转中文标签。

    v3.2: 与前端 XMind 编辑页（XMindEditPage.TYPE_LABELS）保持一致，
    保证导出的 .xmind 打开后与页面上的分组名完全相同。
    """
    labels = {
        "functional": "功能", "boundary": "边界值", "exception": "异常",
        "security": "安全", "performance": "性能", "regression": "回归",
        "integration": "集成", "ui": "UI", "api": "接口", "data": "数据",
        "exploratory": "探索性",  # v3.1.3 P2: 与前端 caseNote.ts 对齐
        "other": "其他",
    }
    return labels.get(t, t)


def _fallback_req_title(rid: str) -> str:
    """无需求标题时的兜底名称（导出里避免出现意义不明的「需求 未知」节点）。"""
    return "未分类需求" if rid in ("未知", "未分类", "") else f"需求 {rid}"


# 用例类型排序（功能→边界→异常→…→其他），与前端思维导图分组顺序一致
_TYPE_ORDER: tuple[str, ...] = (
    "functional", "boundary", "exception", "security", "performance",
    "regression", "integration", "ui", "api", "data", "exploratory", "other",
)

# 用例状态中文标签
_STATUS_LABELS: dict[str, str] = {
    "draft": "草稿", "reviewed": "已审核", "approved": "已通过", "rejected": "已驳回",
}

# XMind 逻辑图（全部向右）布局标识：用例树层级深，左右分叉的思维导图布局不可读
_LOGIC_RIGHT = "org.xmind.ui.logic.right"


def _type_sort_key(t: str) -> tuple[int, str]:
    """类型排序键：已知类型按 _TYPE_ORDER 顺序，未知类型排在最后（按字母序）。"""
    try:
        return (_TYPE_ORDER.index(t), "")
    except ValueError:
        return (len(_TYPE_ORDER), t)


def _build_case_node(c: dict, include_details: bool) -> dict:
    """构建单个用例节点（标题 + 描述 + 步骤 + 前置条件 + 元信息）。

    标题格式：[优先级][类型] 用例标题（优先级/类型缺失时自动省略对应标签）
    """
    title = (c.get("title") or "").strip() or (c.get("id") or "").strip() or "未命名用例"
    type_label = _case_type_label((c.get("type") or "").strip())
    priority = (c.get("priority") or "").strip()
    prefix = "".join(f"[{x}]" for x in (priority, type_label) if x)
    case_node = _topic(f"{prefix} {title}" if prefix else title)

    if not include_details:
        return case_node

    # 描述
    desc = (c.get("description") or "").strip()
    if desc:
        _attach_children(case_node, [_topic(f"描述: {desc}")])

    # 步骤
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

    # 前置条件
    precond = (c.get("preconditions")
               or (c.get("case_json") or {}).get("preconditions", []))
    if precond:
        precond_nodes = [_topic(f"前置条件: {p}") for p in precond if p]
        if precond_nodes:
            _attach_children(case_node, precond_nodes)

    # 元信息（用例 ID + 审核状态），放在末尾避免干扰主流程阅读
    meta: list[str] = []
    case_id = (c.get("id") or "").strip()
    if case_id:
        meta.append(f"ID: {case_id}")
    status_label = _STATUS_LABELS.get((c.get("status") or "").strip(), "")
    if status_label:
        meta.append(f"状态: {status_label}")
    if meta:
        _attach_children(case_node, [_topic(" ｜ ".join(meta))])

    return case_node


def _group_by_type(cases: list[dict]) -> list[tuple[str, list[dict]]]:
    """按用例类型分组，返回 [(type, cases)]，顺序与前端思维导图一致。"""
    groups: dict[str, list[dict]] = {}
    for c in cases:
        t = (c.get("type") or "").strip() or "other"
        groups.setdefault(t, []).append(c)
    return sorted(groups.items(), key=lambda kv: _type_sort_key(kv[0]))


def export_to_xmind(cases: list[dict], project_name: str = "", include_details: bool = True) -> bytes:
    """导出为 XMind 思维导图 JSON 格式。

    分组逻辑（v3.2: 与前端 XMind 编辑页层级完全对齐）：
    1. 按需求 ID 分组
    2. 需求下先分"业务场景"节点（跨功能点/整体性用例），再按功能点分组
    3. 功能点下按「应用类型」分组（顺序：功能→边界值→异常→安全→性能→回归→集成→UI→接口→数据→其他）
    4. 用例节点标题为「[优先级][类型] 标题」，子节点依次为 描述 / 步骤 / 前置条件 / 元信息(ID·状态)

    结构示例：
        <root>
          └── 需求标题
              ├── 业务场景
              │   └── 性能
              │       └── [P1][性能] 整体性能测试
              └── 登录流程（功能点）
                  └── 功能
                      └── [P0][功能] 正常登录验证
                          ├── 描述: 验证标准登录主流程
                          ├── 步骤1: 输入账号密码  →  预期: 登录成功
                          ├── 前置条件: 已部署测试环境
                          └── ID: TC-001 ｜ 状态: 已通过

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
                "title": c.get("req_title") or _fallback_req_title(rid),
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
    # 逻辑图（全部向右）布局：用例树层级深，默认思维导图布局会左右分叉
    root_topic["structureClass"] = _LOGIC_RIGHT

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

            # v3.2: 增加「应用类型」层，与前端思维导图（需求→功能点→类型→用例）对齐
            type_nodes: list[dict] = []
            for type_name, type_cases in _group_by_type(fp_cases):
                type_node = _topic(_case_type_label(type_name))
                _attach_children(
                    type_node,
                    [_build_case_node(c, include_details) for c in type_cases],
                )
                type_nodes.append(type_node)

            if type_nodes:
                _attach_children(fp_node, type_nodes)
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


# ============================================================================
# XMind 8 XML 导出（content.xml + XMAP 2.0 命名空间，兼容 XMind 8）
# ============================================================================

import uuid as _uuid
from xml.etree.ElementTree import Element, SubElement, tostring as _tostring


def _xid() -> str:
    """生成 32 字符 hex ID（XMAP topic id 格式）。"""
    return _uuid.uuid4().hex


_INVALID_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xml_safe(value) -> str:
    """过滤 XML 1.0 非法控制字符。

    v3.4.1 修复 L-3：LLM 文本夹带 \\x00-\\x08 等控制字符时，ElementTree 只转义
    & < > 而不会剥离它们，写进 content.xml 后整个导出文件变成非良构 XML，
    XMind／任何解析器都无法打开。
    """
    return _INVALID_XML_CHARS.sub("", str(value if value is not None else ""))


def _xtopic(title: str, tid: str | None = None, structure_class: str | None = None) -> Element:
    """创建 XMAP topic 元素。

    structure_class: XMind 8 布局标识（如 org.xmind.ui.logic.right 逻辑图向右）
    """
    attrs = {"id": tid or _xid(), "timestamp": str(int(datetime.now().timestamp() * 1000))}
    if structure_class:
        attrs["structure-class"] = structure_class
    t = Element("topic", attrs)
    te = SubElement(t, "title")
    te.text = _xml_safe(title)
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


def _build_xcase(c: dict, include_details: bool) -> Element:
    """构建单个用例 topic（XML 版），结构与 _build_case_node 保持一致。"""
    title = (c.get("title") or "").strip() or (c.get("id") or "").strip() or "未命名用例"
    type_label = _case_type_label((c.get("type") or "").strip())
    priority = (c.get("priority") or "").strip()
    prefix = "".join(f"[{x}]" for x in (priority, type_label) if x)
    case_node = _xtopic(f"{prefix} {title}" if prefix else title)

    if not include_details:
        return case_node

    desc = (c.get("description") or "").strip()
    if desc:
        _xchildren(case_node, [_xtopic(f"描述: {desc}")])

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

    precond = c.get("preconditions") or (c.get("case_json") or {}).get("preconditions", [])
    if precond:
        pre_kids = [_xtopic(f"前置条件: {p}") for p in precond if p]
        if pre_kids:
            _xchildren(case_node, pre_kids)

    meta: list[str] = []
    case_id = (c.get("id") or "").strip()
    if case_id:
        meta.append(f"ID: {case_id}")
    status_label = _STATUS_LABELS.get((c.get("status") or "").strip(), "")
    if status_label:
        meta.append(f"状态: {status_label}")
    if meta:
        _xchildren(case_node, [_xtopic(" ｜ ".join(meta))])

    return case_node


def export_to_xmind_xml(cases: list[dict], project_name: str = "", include_details: bool = True) -> bytes:
    """导出用例为 XMind 8 兼容的 XMAP 2.0 XML 格式。

    XMind 8 不支持 content.json 格式（仅支持 content.xml + XMAP 命名空间）。
    本函数构建与 export_to_xmind 相同的树结构，但输出为 XMAP 2.0 XML。

    树结构（需求→功能点→应用类型→用例→步骤，与 export_to_xmind 一致）：
        <root>
          └── 需求标题
              └── 登录流程（功能点）
                  └── 功能（应用类型）
                      └── [P0][功能] 正常登录验证
                          ├── 描述: 验证标准登录主流程
                          ├── 步骤1: 输入账号密码 -> 预期: 登录成功
                          ├── 前置条件: 已部署测试环境
                          └── ID: TC-001 ｜ 状态: 已通过
    """
    root_title = project_name or "测试用例"

    # 1. 分组（复用 JSON 版逻辑，v2.8.52: 无功能点归属的用例归入"业务场景"）
    req_groups: dict[str, dict] = {}
    for c in cases:
        rid = c.get("req_id") or c.get("related_requirement") or "未知"
        if rid not in req_groups:
            req_groups[rid] = {"title": c.get("req_title") or _fallback_req_title(rid), "project_name": c.get("project_name", ""), "fp_groups": {}}
        fp = (c.get("functional_point") or c.get("related_fp") or "").strip()
        if not fp:
            fp = "业务场景"
        if fp not in req_groups[rid]["fp_groups"]:
            req_groups[rid]["fp_groups"][fp] = []
        req_groups[rid]["fp_groups"][fp].append(c)

    # 2. 构建 XML 树
    root_topic = _xtopic(root_title, structure_class=_LOGIC_RIGHT)

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
            # v3.2: 「应用类型」层，与前端思维导图（需求→功能点→类型→用例）对齐
            type_nodes: list[Element] = []
            for type_name, type_cases in _group_by_type(fp_cases):
                type_node = _xtopic(_case_type_label(type_name))
                _xchildren(type_node, [_build_xcase(c, include_details) for c in type_cases])
                type_nodes.append(type_node)
            if type_nodes:
                _xchildren(fp_node, type_nodes)
            fp_nodes.append(fp_node)
        if fp_nodes:
            _xchildren(req_node, fp_nodes)
        req_nodes.append(req_node)
    if req_nodes:
        _xchildren(root_topic, req_nodes)

    # 3. 构建 XMAP 2.0 XML
    sheet = Element("sheet", {"id": _xid(), "timestamp": str(int(datetime.now().timestamp() * 1000))})
    SubElement(sheet, "title").text = _xml_safe(root_title)
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
