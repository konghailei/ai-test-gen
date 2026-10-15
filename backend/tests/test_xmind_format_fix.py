"""验证 _format_item_title 对 cross_fp_flows/fp_contracts/shared_business_rules 的处理。

运行：python tests/test_xmind_format_fix.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.exporters.req_xmind import _format_item_title, export_requirement_to_xmind, export_requirement_to_xmind_json


def test_cross_fp_flows():
    # 标准字段命名
    item = {
        "name": "登录后加载菜单",
        "source_fp": "用户登录",
        "target_fp": "菜单加载",
        "interaction_type": "sequence",
        "description": "登录成功后触发菜单加载，传递 user_id",
    }
    result = _format_item_title(item, "FP 调用顺序")
    print(f"[cross_fp_flows 标准] {result}")
    assert "用户登录" in result and "菜单加载" in result, "应包含 source/target FP"
    assert "顺序调用" in result, "应包含交互类型中文"
    assert "登录成功后触发菜单加载" in result, "应包含描述"
    assert "user_id" in result or "传递" in result, "应包含描述细节"

    # 兼容 from/to/contract 字段命名
    item2 = {"from": "登录验证", "to": "首页跳转", "contract": "登录成功后传递 session_token"}
    result2 = _format_item_title(item2, "FP 调用顺序")
    print(f"[cross_fp_flows 兼容] {result2}")
    assert "登录验证" in result2 and "首页跳转" in result2, "应包含 from/to FP"


def test_fp_contracts():
    # 标准字段命名
    item = {
        "provider_fp": "用户登录",
        "consumer_fp": "菜单加载",
        "data_fields": ["user_id", "token"],
        "contract_desc": "登录成功返回 user_id 和 token，菜单加载需携带 token 鉴权",
    }
    result = _format_item_title(item, "FP 间契约")
    print(f"[fp_contracts 标准] {result}")
    assert "用户登录" in result and "菜单加载" in result, "应包含 provider/consumer"
    assert "user_id" in result and "token" in result, "应包含 data_fields"
    assert "鉴权" in result, "应包含契约描述"
    assert result != "{}" and not result.startswith("{"), "不应落入 JSON fallback"

    # 兼容 fp_a/fp_b/contract 字段命名
    item2 = {"fp_a": "登录验证", "fp_b": "密码复杂度", "contract": "密码校验结果以布尔值回调"}
    result2 = _format_item_title(item2, "FP 间契约")
    print(f"[fp_contracts 兼容] {result2}")
    assert "登录验证" in result2 and "密码复杂度" in result2
    assert "布尔值" in result2


def test_shared_business_rules():
    # 带 type 和 applied_fps
    item = {
        "id": "SBR-01",
        "rule": "全链路 TLS 加密",
        "type": "security",
        "applied_fps": ["用户登录", "数据查询"],
    }
    result = _format_item_title(item, "共享业务规则")
    print(f"[shared_br 标准] {result}")
    assert "安全" in result, "应包含类型中文"
    assert "全链路 TLS 加密" in result, "应包含规则"
    assert "用户登录" in result and "数据查询" in result, "应包含 applied_fps"

    # 兼容 applies_to 字段命名
    item2 = {"rule": "统一编码规则", "applies_to": ["登录验证", "数据传输"]}
    result2 = _format_item_title(item2, "共享业务规则")
    print(f"[shared_br 兼容] {result2}")
    assert "统一编码规则" in result2
    assert "登录验证" in result2 and "数据传输" in result2


def test_legacy_top_fields():
    """修复M: 旧版 SRR 顶层字段应能正确格式化。"""
    # API 接口
    api = {"method": "POST", "path": "/api/login", "summary": "用户登录",
           "request_body": "username,password", "response_body": "token",
           "error_responses": ["401 未授权"]}
    r = _format_item_title(api, "API 接口")
    print(f"[api_definitions] {r}")
    assert "POST" in r and "/api/login" in r and "用户登录" in r

    # 用户角色
    role = {"role": "管理员", "description": "系统管理员", "permissions": ["read", "write"]}
    r = _format_item_title(role, "用户角色")
    print(f"[user_roles] {r}")
    assert "管理员" in r and "read" in r and "write" in r

    # 外部依赖
    dep = {"name": "短信服务", "type": "API", "description": "发送验证码",
           "impact_if_unavailable": "无法注册"}
    r = _format_item_title(dep, "外部依赖")
    print(f"[dependencies] {r}")
    assert "短信服务" in r and "API" in r and "无法注册" in r

    # 界面规则
    ui = {"element": "登录按钮", "behavior": "频繁点击限流", "condition": "3次/秒"}
    r = _format_item_title(ui, "界面规则")
    print(f"[ui_rules] {r}")
    assert "登录按钮" in r and "频繁点击限流" in r

    # 数据模型
    dm = {"entity_name": "User", "description": "用户实体",
          "fields": [{"field": "id", "type": "integer"}, {"field": "name", "type": "string"}],
          "relationships": ["hasOne:Profile"]}
    r = _format_item_title(dm, "数据模型")
    print(f"[data_model] {r}")
    assert "User" in r and "id(整数)" in r and "name(字符串)" in r

    # 假设与约束
    asm = {"content": "假设用户已注册", "type": "assumption"}
    r = _format_item_title(asm, "假设与约束")
    print(f"[assumptions] {r}")
    assert "假设" in r and "用户已注册" in r


def test_export_with_legacy_fields():
    """端到端验证：含旧版顶层字段的 SRR 能完整导出。"""
    import io, zipfile
    srr = {
        "requirement": {"title": "测试需求", "summary": "验证导出"},
        "cross_fp_interactions": {
            "cross_fp_flows": [
                {"source_fp": "FP_A", "target_fp": "FP_B",
                 "interaction_type": "sequence", "description": "A到B"}
            ],
            "shared_business_rules": [
                {"rule": "TLS加密", "type": "security", "applied_fps": ["FP_A", "FP_B"]}
            ],
            "fp_contracts": [
                {"provider_fp": "FP_A", "consumer_fp": "FP_B",
                 "data_fields": ["token"], "contract_desc": "A提供token给B"}
            ],
            "global_constraints": ["响应<200ms"],
        },
        "api_definitions": [{"method": "GET", "path": "/api/test", "summary": "测试接口"}],
        "user_roles": [{"role": "admin", "permissions": ["all"]}],
        "dependencies": [{"name": "Redis", "type": "DB", "description": "缓存"}],
        "ui_rules": [{"element": "按钮", "behavior": "防抖"}],
        "data_model": [{"entity_name": "User", "fields": [{"field": "id", "type": "integer"}]}],
        "assumptions": [{"content": "用户已登录", "type": "assumption"}],
        "functional_points": [],
    }
    # XML 版本（返回 ZIP 包，需解压读取 content.xml）
    xml_zip = export_requirement_to_xmind(srr, req_title="测试需求", req_id="REQ-001")
    assert isinstance(xml_zip, bytes) and len(xml_zip) > 0
    with zipfile.ZipFile(io.BytesIO(xml_zip)) as zf:
        xml_text = zf.read("content.xml").decode("utf-8")
    assert "FP_A" in xml_text and "FP_B" in xml_text, "XML 应含 FP 调用顺序"
    assert "TLS加密" in xml_text, "XML 应含共享业务规则"
    assert "附加信息" in xml_text, "XML 应含附加信息节点"
    assert "测试接口" in xml_text, "XML 应含 API 接口"
    print("[XML 导出] OK")

    # JSON 版本（返回 ZIP 包，需解压读取 content.json）
    json_zip = export_requirement_to_xmind_json(srr, req_title="测试需求", req_id="REQ-001")
    assert isinstance(json_zip, bytes) and len(json_zip) > 0
    with zipfile.ZipFile(io.BytesIO(json_zip)) as zf:
        json_text = zf.read("content.json").decode("utf-8")
    assert "FP_A" in json_text and "FP_B" in json_text, "JSON 应含 FP 调用顺序"
    assert "附加信息" in json_text, "JSON 应含附加信息节点"
    assert "测试接口" in json_text, "JSON 应含 API 接口"
    print("[JSON 导出] OK")


if __name__ == "__main__":
    test_cross_fp_flows()
    test_fp_contracts()
    test_shared_business_rules()
    test_legacy_top_fields()
    test_export_with_legacy_fields()
    print("\n所有测试通过 ✓")
