"""需求 SRR → XMind 思维导图导出器（XMAP 2.0 XML）。

把需求 SRR 结构导出为 .xmind 文件，便于评审/分享/归档。

树结构（业务场景 与功能点平级为一级节点）：
    需求标题（根）
      ├── 业务场景（一级）
      │   ├── 基本信息（优先级 / 类型 / 摘要）
      │   ├── 主流程（Happy Path）
      │   │   └── 步骤1: action → expected
      │   ├── 异常分支（Alternate Flows）
      │   ├── 业务规则 / 边界条件 / 数据约束 / 前置条件
      │   └── 附加详情
      ├── [fp_type] 功能点1（一级）
      │   ├── 摘要
      │   └── 主流程 / 异常分支 / 业务规则 / 数据约束 / 非功能需求
      └── [fp_type] 功能点2（一级）
          └── ...

v2.48 变更：
- 移除 acceptance_criteria（验收标准）独立导出（已合并到业务规则）
- 移除 test_hints（测试提示）导出（不再生成）
- data_constraints.type 中文化（兼容英文历史数据映射为中文）
"""

from __future__ import annotations

import io, json, uuid, zipfile
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring


# ============================================================================
# v2.48: 数据约束字段类型英文 → 中文映射（兼容历史数据）
# ============================================================================
DATA_TYPE_CN: dict[str, str] = {
    "string": "字符串",
    "str": "字符串",
    "text": "文本",
    "integer": "整数",
    "int": "整数",
    "long": "长整数",
    "bigint": "长整数",
    "short": "短整数",
    "float": "小数",
    "double": "双精度小数",
    "decimal": "高精度小数",
    "number": "数字",
    "boolean": "布尔",
    "bool": "布尔",
    "date": "日期",
    "datetime": "日期时间",
    "timestamp": "时间戳",
    "time": "时间",
    "enum": "枚举",
    "array": "数组",
    "list": "数组",
    "object": "对象",
    "dict": "对象",
    "json": "JSON",
    "uuid": "UUID",
    "url": "URL",
    "email": "邮箱",
    "phone": "电话",
    "file": "文件",
    "image": "图片",
    "binary": "二进制",
    "blob": "二进制大对象",
}

# v2.48: 功能点类型英文 → 中文映射（与前端 FP_TYPE_LABELS 对齐）
FP_TYPE_CN: dict[str, str] = {
    "workflow": "业务流程",
    "constraint": "约束规则",
    "calculation": "计算规则",
    "api_behavior": "API 行为",
    "data_record": "数据记录",
    "config": "系统配置",
}

# v2.48: 业务规则类型英文 → 中文映射（与前端 BR_TYPE_LABELS 对齐）
BR_TYPE_CN: dict[str, str] = {
    "validation": "校验",
    "calculation": "计算",
    "workflow": "流程",
    "permission": "权限",
    "security": "安全",
    "session": "会话",
    "timeout": "超时",
    "rate_limit": "限流",
    "notification": "通知",
    "data_retention": "数据保留",
    "audit": "审计",
    "encryption": "加密",
    "integration": "集成",
    "business": "业务",
    "constraint": "约束",
    "data_integrity": "数据完整性",
    "field_validation": "字段校验",
    "access_control": "访问控制",
    "format": "格式",
    "duplication": "去重",
    "mandatory": "必填",
    "uniqueness": "唯一性",
    "relationship": "关联",
    "logic": "逻辑",
    "prerequisite": "前置条件",
    "authorization": "授权",
    "authentication": "认证",
    "presentation": "展示",
    "initialization": "初始化",
    "data_format": "数据格式",
    "range": "范围",
    "enumeration": "枚举",
}

# v2.8.53: 跨 FP 交互类型英文 → 中文映射（CrossFpFlow.interaction_type）
INTERACTION_TYPE_CN: dict[str, str] = {
    "sequence": "顺序调用",
    "data_flow": "数据流",
    "event": "事件",
    "dependency": "依赖",
}

# v2.8.53: 假设类型英文 → 中文映射（Assumption.type）
ASSUMPTION_TYPE_CN: dict[str, str] = {
    "assumption": "假设",
    "constraint": "约束",
    "out_of_scope": "范围外",
}


def _data_type_cn(dtype: str) -> str:
    """把数据约束 type 字段映射为中文显示。

    - 已是中文直接返回
    - 英文（不区分大小写）按映射表返回
    - 未知值原样返回
    """
    if not dtype:
        return ""
    if any("\u4e00" <= ch <= "\u9fff" for ch in dtype):
        return dtype
    return DATA_TYPE_CN.get(dtype.lower(), dtype)


def _fp_type_cn(fp_type: str) -> str:
    """把功能点类型映射为中文显示（已是中文则原样返回）。"""
    if not fp_type:
        return ""
    if any("\u4e00" <= ch <= "\u9fff" for ch in fp_type):
        return fp_type
    return FP_TYPE_CN.get(fp_type.lower(), fp_type)


def _br_type_cn(br_type: str) -> str:
    """把业务规则 type 字段映射为中文显示（已是中文则原样返回）。"""
    if not br_type:
        return ""
    if any("\u4e00" <= ch <= "\u9fff" for ch in br_type):
        return br_type
    return BR_TYPE_CN.get(br_type.lower(), br_type)


def _interaction_type_cn(itype: str) -> str:
    """把跨 FP 交互类型映射为中文显示（已是中文则原样返回）。"""
    if not itype:
        return ""
    if any("\u4e00" <= ch <= "\u9fff" for ch in itype):
        return itype
    return INTERACTION_TYPE_CN.get(itype.lower(), itype)


def _assumption_type_cn(atype: str) -> str:
    """把假设类型映射为中文显示（已是中文则原样返回）。"""
    if not atype:
        return ""
    if any("\u4e00" <= ch <= "\u9fff" for ch in atype):
        return atype
    return ASSUMPTION_TYPE_CN.get(atype.lower(), atype)


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
    - 数据约束：field (type 中文): constraints
    - 业务规则（v2.48 含原验收标准）：[类型中文] rule / description / criteria
    - 共享业务规则：[类型中文] rule（适用: applied_fps）
    - FP 调用顺序（cross_fp_flows）：[交互类型中文] source_fp → target_fp: description
    - FP 间契约（fp_contracts）：provider_fp → consumer_fp: contract_desc (data_fields)
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

    # 数据约束：field (type 中文): constraints  ← v2.48 中文化
    if label == "数据约束":
        field = _as_str(it.get("field"))
        dtype_cn = _data_type_cn(_as_str(it.get("type")))
        constraints = _as_str(it.get("constraints") or it.get("constraint"))
        required = it.get("required")
        parts = []
        if field:
            parts.append(field)
        if dtype_cn:
            parts.append(f"[{dtype_cn}]")
        if required is not None:
            parts.append("必填" if required else "选填")
        if constraints:
            parts.append(constraints)
        if parts:
            return " ".join(parts)

    # 业务规则 / 共享业务规则：[类型中文] rule / description  ← v2.48 类型中文化；v2.8.53 共享规则补充 applied_fps
    if label in ("业务规则", "共享业务规则"):
        br_type = _br_type_cn(_as_str(it.get("type")))
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
            if br_type:
                text = f"[{br_type}] {text}"
            # 共享规则补充适用 FP（兼容 applied_fps / applies_to 两种字段名）
            if label == "共享业务规则":
                applied = it.get("applied_fps") or it.get("applies_to") or []
                if applied and isinstance(applied, list):
                    text += f"（适用: {', '.join(str(a) for a in applied)}）"
            return text

    # FP 调用顺序（cross_fp_flows）：[交互类型中文] source_fp → target_fp: description
    # v2.8.53 修复K: 原逻辑落入通用匹配，丢失 source_fp/target_fp/interaction_type 关键字段
    if label == "FP 调用顺序":
        # 兼容 source_fp/from 与 target_fp/to 两种字段命名
        source_fp = _as_str(it.get("source_fp") or it.get("from"))
        target_fp = _as_str(it.get("target_fp") or it.get("to"))
        itype_cn = _interaction_type_cn(_as_str(it.get("interaction_type")))
        # 兼容 description/contract 两种字段命名
        desc = _as_str(it.get("description") or it.get("contract"))
        name = _as_str(it.get("name"))
        parts = []
        prefix = f"[{itype_cn}] " if itype_cn else ""
        # 主体：source → target
        if source_fp or target_fp:
            parts.append(f"{prefix}{source_fp or '?'} → {target_fp or '?'}")
        elif name:
            parts.append(f"{prefix}{name}")
        elif desc:
            parts.append(f"{prefix}{desc}")
        else:
            parts.append(f"{prefix}{json.dumps(it, ensure_ascii=False)}")
            return parts[0]
        # 描述作为补充信息
        if desc and f"{source_fp} → {target_fp}" != desc:
            parts.append(desc)
        elif name and not (source_fp or target_fp):
            pass  # name 已作为主体
        elif name:
            parts.append(name)
        return " | ".join(p for p in parts if p)

    # FP 间契约（fp_contracts）：provider_fp → consumer_fp: contract_desc (data_fields)
    # v2.8.53 修复L: 原逻辑无匹配字段，落入 JSON fallback，丢失 provider/consumer/data_fields
    if label == "FP 间契约":
        # 兼容 provider_fp/fp_a 与 consumer_fp/fp_b 两种字段命名
        provider = _as_str(it.get("provider_fp") or it.get("fp_a"))
        consumer = _as_str(it.get("consumer_fp") or it.get("fp_b"))
        # 兼容 contract_desc/contract/description 两种字段命名
        contract_desc = _as_str(it.get("contract_desc") or it.get("contract") or it.get("description"))
        data_fields = it.get("data_fields") or []
        parts = []
        if provider or consumer:
            parts.append(f"{provider or '?'} → {consumer or '?'}")
        if contract_desc:
            parts.append(contract_desc)
        if data_fields and isinstance(data_fields, list):
            parts.append(f"字段: {', '.join(str(f) for f in data_fields)}")
        if parts:
            return " | ".join(parts)

    # v2.8.53 修复M: 旧版 SRR 顶层字段（api_definitions/user_roles/dependencies/ui_rules/data_model/assumptions）
    # 原导出器未覆盖这些字段，导致静默丢失。下面为每个字段添加专用格式化分支。
    if label == "API 接口":
        method = _as_str(it.get("method")).upper()
        path = _as_str(it.get("path"))
        summary = _as_str(it.get("summary"))
        request_body = _as_str(it.get("request_body"))
        response_body = _as_str(it.get("response_body"))
        error_responses = it.get("error_responses") or []
        parts = []
        if method or path:
            parts.append(f"{method} {path}".strip())
        if summary:
            parts.append(summary)
        if request_body:
            parts.append(f"请求: {request_body}")
        if response_body:
            parts.append(f"响应: {response_body}")
        if error_responses and isinstance(error_responses, list):
            parts.append(f"错误: {', '.join(str(e) for e in error_responses)}")
        if parts:
            return " | ".join(parts)

    if label == "用户角色":
        role = _as_str(it.get("role"))
        description = _as_str(it.get("description"))
        permissions = it.get("permissions") or []
        parts = []
        if role:
            parts.append(role)
        if description:
            parts.append(description)
        if permissions and isinstance(permissions, list):
            parts.append(f"权限: {', '.join(str(p) for p in permissions)}")
        if parts:
            return " | ".join(parts)

    if label == "外部依赖":
        name = _as_str(it.get("name"))
        dtype = _as_str(it.get("type"))
        description = _as_str(it.get("description"))
        impact = _as_str(it.get("impact_if_unavailable"))
        parts = []
        if name:
            parts.append(name)
        if dtype:
            parts.append(f"[{dtype}]")
        if description:
            parts.append(description)
        if impact:
            parts.append(f"不可用时: {impact}")
        if parts:
            return " ".join(p for p in parts if p)

    if label == "界面规则":
        element = _as_str(it.get("element"))
        behavior = _as_str(it.get("behavior"))
        condition = _as_str(it.get("condition"))
        parts = []
        if element:
            parts.append(element)
        if behavior:
            parts.append(behavior)
        if condition:
            parts.append(f"触发: {condition}")
        if parts:
            return " | ".join(parts)

    if label == "数据模型":
        entity_name = _as_str(it.get("entity_name"))
        description = _as_str(it.get("description"))
        fields = it.get("fields") or []
        relationships = it.get("relationships") or []
        parts = []
        if entity_name:
            parts.append(entity_name)
        if description:
            parts.append(description)
        if fields and isinstance(fields, list):
            # fields 可能是 DataConstraint dict 列表，也可能是简单字符串
            field_strs = []
            for f in fields:
                if isinstance(f, dict):
                    fname = _as_str(f.get("field") or f.get("name"))
                    ftype = _data_type_cn(_as_str(f.get("type")))
                    if fname and ftype:
                        field_strs.append(f"{fname}({ftype})")
                    elif fname:
                        field_strs.append(fname)
                else:
                    field_strs.append(str(f))
            if field_strs:
                parts.append(f"字段: {', '.join(field_strs)}")
        if relationships and isinstance(relationships, list):
            parts.append(f"关系: {', '.join(str(r) for r in relationships)}")
        if parts:
            return " | ".join(parts)

    if label == "假设与约束":
        content = _as_str(it.get("content"))
        atype = _assumption_type_cn(_as_str(it.get("type")))
        if content:
            return f"[{atype}] {content}" if atype else content

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


def _impact_entry_title(e: dict) -> str:
    """影响分析条目标题：受影响 FP 标【影响功能】前缀；未确认加 🚩 标记（文字方案）。

    - 已确认受影响（confirmed_at + is_affected=True）：【影响功能】功能名
    - 未确认（无 confirmed_at）：🚩【影响功能】功能名（未确认）
    - 已确认不受影响（is_affected=False + confirmed_at）：不导出（在调用方过滤）
    """
    name = _as_str(e.get("fp_name") or e.get("fp_id") or "未知功能点")
    confirmed = bool(e.get("confirmed_at"))
    if confirmed:
        return f"【影响功能】{name}"
    return f"🚩【影响功能】{name}（未确认）"


def _filter_impact_entries(impact_list: list) -> list[dict]:
    """过滤影响分析条目：仅保留"确认受影响"与"未确认"条目，剔除"确认不受影响"。"""
    if not isinstance(impact_list, list):
        return []
    entries = []
    for e in impact_list:
        if not isinstance(e, dict):
            continue
        # 已确认不受影响：不导出
        if e.get("is_affected") is False and e.get("confirmed_at"):
            continue
        entries.append(e)
    return entries


def _build_impact_node(impact_list: list) -> Element | None:
    """构建影响功能分析节点（XML 版）：一级节点，子节点为各受影响 FP。"""
    entries = _filter_impact_entries(impact_list)
    if not entries:
        return None
    node = _topic(f"影响功能分析（{len(entries)} 个）")
    kids = []
    for e in entries:
        entry_node = _topic(_impact_entry_title(e))
        desc = _as_str(e.get("impact_description"))
        if desc:
            _children(entry_node, [_topic(f"影响说明: {desc}")])
        kids.append(entry_node)
    if kids:
        _children(node, kids)
    return node


def _jbuild_impact_node(impact_list: list) -> dict | None:
    """构建影响功能分析节点（JSON 版）：一级节点，子节点为各受影响 FP。"""
    entries = _filter_impact_entries(impact_list)
    if not entries:
        return None
    node = _jtopic(f"影响功能分析（{len(entries)} 个）")
    kids = []
    for e in entries:
        entry_node = _jtopic(_impact_entry_title(e))
        desc = _as_str(e.get("impact_description"))
        if desc:
            _jattach(entry_node, [_jtopic(f"影响说明: {desc}")])
        kids.append(entry_node)
    if kids:
        _jattach(node, kids)
    return node


def _build_fp_node(fp: dict) -> Element | None:
    """构建单个功能点节点。"""
    if not isinstance(fp, dict):
        return None
    name = _as_str(fp.get("name") or "未命名功能点")
    fp_type = _fp_type_cn(_as_str(fp.get("fp_type") or fp.get("type") or ""))  # v2.48 中文化
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

        # 3-4. 业务规则（v2.48 已合并验收标准） / 数据约束
        # v2.48: 移除 acceptance_criteria 独立导出（已合并到 business_rules）
        for field, label in [
            ("business_rules", "业务规则"),
            ("data_constraints", "数据约束"),
        ]:
            items = sub_srr.get(field) or []
            if isinstance(items, list) and items:
                node = _build_list_node(items, label)
                if node:
                    sub_nodes.append(node)

        # 5. 非功能需求
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

    # v2.48: 移除 test_hints（测试提示）导出 — 不再生成该字段

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

    # ===== 1. 业务场景（一级节点，含基本信息/流程/规则等二级节点）=====
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

    # 1c-v2.32. 历史数据影响分析（仅优化需求）；v2.40: 字段精简为 4 核心维度
    ldi = srr.get("legacy_data_impact")
    if isinstance(ldi, dict) and ldi:
        ldi_kids: list = []
        for k, label in [
            ("impact_scope", "影响范围"),
            ("migration_plan", "迁移方案"),
            ("rollback_and_validation", "验证与回滚"),
            ("user_clarification", "用户澄清"),
        ]:
            v = _as_str(ldi.get(k))
            if v.strip():
                ldi_kids.append(_topic(f"{label}: {v}"))
        # risks 仍为列表
        items = ldi.get("risks") or []
        if isinstance(items, list) and items:
            sub_node = _build_list_node(items, "风险与缓解")
            if sub_node:
                ldi_kids.append(sub_node)
        if ldi_kids:
            ldi_node = _topic("历史数据影响分析")
            _children(ldi_node, ldi_kids)
            total_srr_kids.append(ldi_node)

    # v2.40: 移除 additional_details 字段，不再导出"附加详情"节点

    # v2.8.53 修复M: 旧版 SRR 顶层字段（api_definitions/user_roles/dependencies/ui_rules/data_model/assumptions）
    # 原导出器未覆盖，导致静默丢失。统一聚合到"附加信息"二级节点下。
    extra_kids: list = []
    for field, label in [
        ("api_definitions", "API 接口"),
        ("user_roles", "用户角色"),
        ("dependencies", "外部依赖"),
        ("ui_rules", "界面规则"),
        ("data_model", "数据模型"),
        ("assumptions", "假设与约束"),
    ]:
        items = srr.get(field) or []
        if isinstance(items, list) and items:
            sub_node = _build_list_node(items, label)
            if sub_node:
                extra_kids.append(sub_node)
    if extra_kids:
        extra_node = _topic("附加信息")
        _children(extra_node, extra_kids)
        total_srr_kids.append(extra_node)

    if total_srr_kids:
        total_srr_node = _topic("业务场景")
        _children(total_srr_node, total_srr_kids)
        root_kids.append(total_srr_node)

    # ===== 1.5 影响功能分析（v3.4：受影响 FP 标【影响功能】前缀，未确认加 🚩 标记）=====
    impact_node = _build_impact_node(srr.get("impact_analysis") or [])
    if impact_node:
        root_kids.append(impact_node)

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
    fp_type = _fp_type_cn(_as_str(fp.get("fp_type") or fp.get("type") or ""))  # v2.48 中文化
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

        # 3-4. 业务规则（v2.48 已合并验收标准） / 数据约束
        # v2.48: 移除 acceptance_criteria 独立导出（已合并到 business_rules）
        for field, label in [
            ("business_rules", "业务规则"),
            ("data_constraints", "数据约束"),
        ]:
            items = sub_srr.get(field) or []
            if isinstance(items, list) and items:
                node = _jbuild_list(items, label)
                if node:
                    sub_nodes.append(node)

        # 5. 非功能需求
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

    # v2.48: 移除 test_hints（测试提示）导出 — 不再生成该字段

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

    # ===== 1. 业务场景（一级节点，含基本信息/流程/规则等二级节点）=====
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

    # 1c-v2.32. 历史数据影响分析（仅优化需求）；v2.40: 字段精简为 4 核心维度
    ldi = srr.get("legacy_data_impact")
    if isinstance(ldi, dict) and ldi:
        ldi_kids: list = []
        for k, label in [
            ("impact_scope", "影响范围"),
            ("migration_plan", "迁移方案"),
            ("rollback_and_validation", "验证与回滚"),
            ("user_clarification", "用户澄清"),
        ]:
            v = _as_str(ldi.get(k))
            if v.strip():
                ldi_kids.append(_jtopic(f"{label}: {v}"))
        # risks 仍为列表
        items = ldi.get("risks") or []
        if isinstance(items, list) and items:
            sub_node = _jbuild_list(items, "风险与缓解")
            if sub_node:
                ldi_kids.append(sub_node)
        if ldi_kids:
            ldi_node = _jtopic("历史数据影响分析")
            _jattach(ldi_node, ldi_kids)
            total_srr_kids.append(ldi_node)

    # v2.40: 移除 additional_details 字段，不再导出"附加详情"节点

    # v2.8.53 修复M: 旧版 SRR 顶层字段（api_definitions/user_roles/dependencies/ui_rules/data_model/assumptions）
    # 原导出器未覆盖，导致静默丢失。统一聚合到"附加信息"二级节点下。
    extra_kids: list = []
    for field, label in [
        ("api_definitions", "API 接口"),
        ("user_roles", "用户角色"),
        ("dependencies", "外部依赖"),
        ("ui_rules", "界面规则"),
        ("data_model", "数据模型"),
        ("assumptions", "假设与约束"),
    ]:
        items = srr.get(field) or []
        if isinstance(items, list) and items:
            sub_node = _jbuild_list(items, label)
            if sub_node:
                extra_kids.append(sub_node)
    if extra_kids:
        extra_node = _jtopic("附加信息")
        _jattach(extra_node, extra_kids)
        total_srr_kids.append(extra_node)

    if total_srr_kids:
        total_srr_node = _jtopic("业务场景")
        _jattach(total_srr_node, total_srr_kids)
        root_kids.append(total_srr_node)

    # ===== 1.5 影响功能分析（v3.4：受影响 FP 标【影响功能】前缀，未确认加 🚩 标记）=====
    impact_node = _jbuild_impact_node(srr.get("impact_analysis") or [])
    if impact_node:
        root_kids.append(impact_node)

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
