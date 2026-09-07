"""用例 XMind 导出回归测试（v3.2）。

背景：v3.2 之前导出存在三类问题——
  1. topic 缺官方字段 class='topic'、rootTopic 缺 structureClass（布局可能左右分叉）
  2. 导出树为 4 层（项目→需求→功能点→用例），前端思维导图为 5 层（多【应用类型】层），结构不一致
  3. 用例仅带类型+标题，缺优先级/描述/ID/状态，且脏数据会生成「需求 未知」和空标题节点

本文件锁定上述修复，防止回归。

运行：
    python backend/tests/test_xmind_export.py
    pytest backend/tests/test_xmind_export.py -q
"""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.exporters.xmind import export_to_xmind, export_to_xmind_xml
except ModuleNotFoundError:
    # xmind 导出器仅依赖标准库，若环境未装后端完整依赖（loguru 等），
    # 直接按文件路径加载，保证本用例在任何环境下都能独立运行。
    import importlib.util

    _path = os.path.join(os.path.dirname(__file__), "..", "app", "exporters", "xmind.py")
    _spec = importlib.util.spec_from_file_location("_xmind_exporter", _path)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    export_to_xmind = _mod.export_to_xmind
    export_to_xmind_xml = _mod.export_to_xmind_xml

LOGIC_RIGHT = "org.xmind.ui.logic.right"


def _case(cid, title, ctype="functional", fp="", pre=None, steps=None,
          priority="P1", status="draft", desc=""):
    return {
        "id": cid,
        "req_id": "REQ-001",
        "req_title": "用户登录需求",
        "project_name": "Demo 项目",
        "title": title,
        "type": ctype,
        "functional_point": fp,
        "priority": priority,
        "status": status,
        "description": desc,
        "preconditions": pre or [],
        "steps": steps or [],
    }


CASES = [
    _case("TC-1", "正常登录验证", "functional", "登录流程",
          ["已部署测试环境", "用户已注册"],
          [{"action": '输入账号 "admin" & 密码 <123>', "expected": "登录成功，跳转首页"},
           {"action": "点击退出", "expected": "返回登录页"}],
          priority="P0", status="approved", desc="验证标准登录主流程"),
    _case("TC-2", "密码错误提示", "exception", "登录流程",
          [], [{"action": "输入错误密码", "expected": "提示密码错误"}]),
    _case("TC-3", "空密码校验", "boundary", "",
          [], [{"action": "密码留空提交", "expected": "按钮置灰"}]),   # 无功能点 → 业务场景
    _case("TC-4", "整体性能压测", "performance", "", [], []),          # 无步骤
    {"id": "TC-5", "req_id": "REQ-002", "req_title": "订单需求",      # 另一需求
     "project_name": "Demo 项目", "title": "下单超时", "type": "ui",
     "functional_point": "下单流程", "priority": "P2", "status": "draft",
     "preconditions": [], "steps": []},
    {"id": "TC-6", "title": "", "type": "", "functional_point": "",   # 脏数据
     "preconditions": [], "steps": []},
]


def _topics(root):
    """深度优先展开所有 topic。"""
    out = [root]
    for kid in root.get("children", {}).get("attached", []):
        out.extend(_topics(kid))
    return out


def _titles(node):
    return [t.get("title", "") for t in _topics(node)]


def _kids(node):
    return [c.get("title", "") for c in node.get("children", {}).get("attached", [])]


def _read_zip(data, name):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return json.loads(zf.read(name).decode("utf-8"))


# ============================================================================
# XMind Zen/2020+（content.json）
# ============================================================================

def test_zip_structure():
    data = export_to_xmind(CASES, project_name="Demo 项目")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
    assert "content.json" in names
    assert "manifest.json" in names
    assert "metadata.json" in names
    # XMind 要求 content.json 位于包首
    assert names[0] == "content.json"


def test_official_fields():
    """v3.2: 每个 topic 必须有 class='topic'，根必须有 structureClass。"""
    content = _read_zip(export_to_xmind(CASES, project_name="Demo 项目"), "content.json")
    sheet = content[0]
    assert sheet["class"] == "sheet"
    root = sheet["rootTopic"]
    assert root["structureClass"] == LOGIC_RIGHT
    for t in _topics(root):
        assert t.get("class") == "topic", f"topic 缺 class: {t}"
        assert "id" in t and "title" in t


def test_layer_matches_frontend():
    """层级须与前端思维导图一致：项目→需求→功能点→【应用类型】→用例。"""
    content = _read_zip(export_to_xmind(CASES, project_name="Demo 项目"), "content.json")
    root = content[0]["rootTopic"]
    titles = _titles(root)

    assert root["title"] == "Demo 项目"
    assert "Demo 项目 - 用户登录需求" in titles

    req_node = next(t for t in _topics(root) if t["title"] == "Demo 项目 - 用户登录需求")
    fps = _kids(req_node)
    assert fps[0] == "业务场景"                       # 业务场景排首位
    assert "登录流程" in fps

    fp_node = next(c for c in req_node["children"]["attached"] if c["title"] == "登录流程")
    types = _kids(fp_node)
    assert "功能" in types and "异常" in types        # 应用类型层
    # 用例挂在类型节点下（5 层结构）
    type_node = next(c for c in fp_node["children"]["attached"] if c["title"] == "功能")
    assert any("[P0][功能] 正常登录验证" == k for k in _kids(type_node))


def test_case_detail_content():
    content = _read_zip(export_to_xmind(CASES, project_name="Demo 项目"), "content.json")
    root = content[0]["rootTopic"]
    case_node = next(t for t in _topics(root) if t["title"] == "[P0][功能] 正常登录验证")
    kids = _kids(case_node)

    assert any(k.startswith("描述: 验证标准登录主流程") for k in kids)
    assert any("步骤1: 输入账号 \"admin\" & 密码 <123>" in k and "预期: 登录成功" in k for k in kids)
    assert any(k.startswith("步骤2:") for k in kids)
    assert "前置条件: 已部署测试环境" in kids
    assert "前置条件: 用户已注册" in kids
    assert any("ID: TC-1" in k and "状态: 已通过" in k for k in kids)


def test_dirty_data_no_empty_node():
    """脏数据不得产生空标题节点或「需求 未知」。"""
    content = _read_zip(export_to_xmind(CASES, project_name="Demo 项目"), "content.json")
    titles = _titles(content[0]["rootTopic"])
    assert all(t.strip() for t in titles), f"存在空标题节点: {titles}"
    assert "未分类需求" in titles
    assert not any("未知" in t for t in titles)
    # 空标题用例回退为用例 ID
    assert "TC-6" in titles


def test_include_details_false():
    root = _read_zip(
        export_to_xmind(CASES, project_name="Demo 项目", include_details=False),
        "content.json",
    )[0]["rootTopic"]
    titles = _titles(root)
    assert not any(t.startswith("步骤") for t in titles)
    assert not any(t.startswith("前置条件") for t in titles)
    assert not any(t.startswith("描述") for t in titles)
    # 精简模式下优先级仍需保留（导出为执行清单时的排序依据）
    assert any("[P0]" in t for t in titles)


# ============================================================================
# XMind 8（content.xml）
# ============================================================================

def test_xmind8_xml():
    data = export_to_xmind_xml(CASES, project_name="Demo 项目")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert "content.xml" in zf.namelist()
        xml = zf.read("content.xml").decode("utf-8")

    assert xml.startswith("<?xml")
    assert "urn:xmind:xmap:xmlns:content:2.0" in xml
    # 特殊字符必须转义
    assert "&amp;" in xml and "&lt;" in xml
    # 布局：逻辑图向右
    assert 'structure-class="%s"' % LOGIC_RIGHT in xml
    # 应用类型层 + 优先级
    assert "<title>功能</title>" in xml
    assert "[P0][功能] 正常登录验证" in xml
    # well-formed
    ET.fromstring(xml)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"[PASS] {fn.__name__}")
    print(f"\n全部通过（{len(fns)} 项）✓")
