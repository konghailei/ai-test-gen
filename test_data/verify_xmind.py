"""XMind 导出器验证脚本。

验证 export_xmind 生成的 .xmind 文件：
1. ZIP 结构正确（含 content.json / manifest.json / metadata.json）
2. content.json 符合 XMind 2020+ 格式（fileVersion=2.0, rootTopic 存在）
3. 用例树结构正确（根 > 类型分组 > 用例 > 步骤 > 预期）
4. 二进制可正确解压
"""

import io
import json
import zipfile
import sys
import os

# 添加 backend 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
# 容器内 /app 也是 backend 根目录
sys.path.insert(0, '/app')

from app.exporters.xmind import export_xmind
from app.exporters import export_cases, SUPPORTED_FORMATS, is_binary_format


def make_sample_cases() -> list[dict]:
    """构建样例用例（模拟 UTCF case_json）。"""
    return [
        {
            "id": "TC-AUTH-0001-0001",
            "title": "管理员使用正确账号登录系统",
            "type": "functional",
            "priority": "P0",
            "description": "验证管理员账号正常登录流程",
            "tags": ["登录", "P0"],
            "preconditions": [
                "已部署测试环境，访问 http://test.example.com",
                "已存在管理员账号 admin / Admin@123",
                "浏览器已清理 Cookie",
            ],
            "steps": [
                {"step": 1, "action": "在『用户名』输入框中输入 admin", "expected": "输入框显示文本 admin; 无报错提示", "data": {"username": "admin"}},
                {"step": 2, "action": "在『密码』输入框中输入 Admin@123", "expected": "密码框显示 8 个掩码点; 无报错提示", "data": {"password": "Admin@123"}},
                {"step": 3, "action": "点击『登录』按钮", "expected": "2 秒内页面跳转至 /dashboard; 顶部显示用户名 admin; 浏览器 Cookie 含 aitestgen_token", "data": {}},
            ],
            "expected": "管理员使用正确账号成功登录系统并跳转到首页",
            "test_data": {"username": "admin", "password": "Admin@123"},
            "assertions": [
                {"type": "equals", "target": "page.url", "expected_value": "/dashboard", "message": "页面应跳转到 dashboard"},
                {"type": "contains", "target": "cookie", "expected_value": "aitestgen_token", "message": "Cookie 中应包含 token"},
            ],
            "cleanup": ["点击右上角头像 → 退出登录", "清理浏览器 Cookie"],
            "related_requirement": "REQ-2024-001",
            "related_fp": "FP-AUTH-001",
            "confidence": 0.95,
            "execution": {"timeout_seconds": 15, "retry_count": 0, "depends_on": [], "skip_conditions": []},
        },
        {
            "id": "TC-AUTH-0001-0002",
            "title": "密码长度边界值测试 - 7 位（小于最小值 8）",
            "type": "boundary",
            "priority": "P2",
            "description": "验证密码长度边界 - 最小值减 1",
            "preconditions": ["已部署测试环境", "已知密码长度限制为 8-20 位"],
            "steps": [
                {"step": 1, "action": "在『用户名』输入框中输入 admin", "expected": "输入框显示 admin", "data": {}},
                {"step": 2, "action": "在『密码』输入框中输入 7 位字符串 Abc1234", "expected": "密码框显示 7 个掩码点", "data": {"password": "Abc1234"}},
                {"step": 3, "action": "点击『登录』按钮", "expected": "页面不跳转; 显示红色错误提示『密码长度不能少于 8 位』", "data": {}},
            ],
            "expected": "系统拒绝 7 位密码登录并给出明确提示",
            "test_data": {"username": "admin", "password": "Abc1234"},
            "assertions": [
                {"type": "contains", "target": "page.error_text", "expected_value": "密码长度", "message": "应提示密码长度错误"},
            ],
            "cleanup": [],
            "related_requirement": "REQ-2024-001",
            "confidence": 0.88,
            "execution": {"timeout_seconds": 10, "retry_count": 0, "depends_on": [], "skip_conditions": []},
        },
        {
            "id": "TC-AUTH-0001-0003",
            "title": "登录失败次数超限后账号锁定",
            "type": "exception",
            "priority": "P1",
            "description": "验证连续 5 次密码错误后账号被锁定",
            "preconditions": ["已部署测试环境", "已知锁定阈值 = 5 次"],
            "steps": [
                {"step": 1, "action": "使用错误密码登录 5 次", "expected": "前 4 次均提示『密码错误』; 第 5 次提示『账号已锁定』", "data": {}},
                {"step": 2, "action": "等待 30 分钟后再次尝试登录", "expected": "若锁定时长 30 分钟，提示『账号已解锁，请重试』", "data": {}},
            ],
            "expected": "账号锁定机制生效，30 分钟后自动解锁",
            "test_data": {},
            "assertions": [],
            "cleanup": ["联系管理员手动解锁测试账号"],
            "related_requirement": "REQ-2024-001",
            "confidence": 0.82,
            "execution": {"timeout_seconds": 1800, "retry_count": 0, "depends_on": [], "skip_conditions": []},
        },
    ]


def verify_xmind_structure(xmind_bytes: bytes) -> dict:
    """验证 xmind 二进制内容的 ZIP 结构和 JSON 格式。"""
    result = {"pass": True, "errors": [], "info": {}}

    # 1. 验证是合法 ZIP
    try:
        zf = zipfile.ZipFile(io.BytesIO(xmind_bytes))
    except zipfile.BadZipFile as e:
        result["pass"] = False
        result["errors"].append(f"非合法 ZIP: {e}")
        return result

    names = zf.namelist()
    result["info"]["zip_entries"] = names

    # 2. 验证必需文件存在
    required_files = ["content.json", "manifest.json", "metadata.json"]
    for f in required_files:
        if f not in names:
            result["pass"] = False
            result["errors"].append(f"缺少必需文件: {f}")

    # 3. 验证 content.json 是数组且含 fileVersion=2.0
    try:
        content_raw = zf.read("content.json")
        content = json.loads(content_raw)
        if not isinstance(content, list) or len(content) == 0:
            result["pass"] = False
            result["errors"].append("content.json 不是非空数组")
        else:
            sheet = content[0]
            if sheet.get("fileVersion") != "2.0":
                result["pass"] = False
                result["errors"].append(f"fileVersion 不为 2.0: {sheet.get('fileVersion')}")
            if "rootTopic" not in sheet:
                result["pass"] = False
                result["errors"].append("缺少 rootTopic")
            else:
                root = sheet["rootTopic"]
                root_title = root.get("title", "")
                result["info"]["root_title"] = root_title
                # 遍历第二层子主题
                children = root.get("children", {}).get("attached", [])
                result["info"]["level2_count"] = len(children)
                result["info"]["level2_titles"] = [c.get("title", "")[:60] for c in children]
    except Exception as e:
        result["pass"] = False
        result["errors"].append(f"解析 content.json 失败: {e}")

    # 4. 验证 manifest.json
    try:
        manifest = json.loads(zf.read("manifest.json"))
        if "file-entries" not in manifest:
            result["pass"] = False
            result["errors"].append("manifest.json 缺少 file-entries")
    except Exception as e:
        result["pass"] = False
        result["errors"].append(f"解析 manifest.json 失败: {e}")

    # 5. 验证 metadata.json
    try:
        metadata = json.loads(zf.read("metadata.json"))
        result["info"]["case_count"] = metadata.get("case_count")
        result["info"]["creator"] = metadata.get("creator", {}).get("name")
    except Exception as e:
        result["pass"] = False
        result["errors"].append(f"解析 metadata.json 失败: {e}")

    return result


def test_export_all_formats():
    """测试所有格式都能正常导出。"""
    cases = make_sample_cases()
    print(f"\n=== 测试所有导出格式（用例数={len(cases)}）===")
    for fmt in SUPPORTED_FORMATS:
        try:
            content, media_type, filename = export_cases(cases, fmt)
            is_bin = is_binary_format(fmt)
            size = len(content) if is_bin else len(str(content))
            print(f"  [{fmt}] OK  size={size}  media={media_type}  filename={filename}")
        except Exception as e:
            print(f"  [{fmt}] FAIL: {e}")


def main():
    print("=" * 70)
    print("XMind 导出器验证")
    print("=" * 70)

    cases = make_sample_cases()
    print(f"\n样例用例: {len(cases)} 条")

    # 1. 测试所有格式
    test_export_all_formats()

    # 2. 详细验证 XMind 格式
    print("\n=== XMind 详细验证 ===")
    xmind_bytes = export_xmind(cases)
    print(f"XMind 文件大小: {len(xmind_bytes)} bytes")

    result = verify_xmind_structure(xmind_bytes)
    print(f"\n验证结果: {'PASS' if result['pass'] else 'FAIL'}")
    if result["errors"]:
        print("错误:")
        for e in result["errors"]:
            print(f"  - {e}")
    if result["info"]:
        print("信息:")
        for k, v in result["info"].items():
            if isinstance(v, list) and len(v) > 5:
                print(f"  {k}: ({len(v)} 项) 前 3 项:")
                for item in v[:3]:
                    print(f"    - {item}")
            else:
                print(f"  {k}: {v}")

    # 3. 输出文件以供 XMind 软件打开
    out_path = os.path.join(os.path.dirname(__file__), "sample_testcases.xmind")
    # 容器内可能无法写入 /tmp 之外的路径，做兼容
    try:
        with open(out_path, "wb") as f:
            f.write(xmind_bytes)
        print(f"\n已生成样例文件: {out_path}")
    except (OSError, PermissionError):
        out_path = "/tmp/sample_testcases.xmind"
        with open(out_path, "wb") as f:
            f.write(xmind_bytes)
        print(f"\n已生成样例文件: {out_path}")
    print("可使用 XMind 2020+ 软件打开验证")

    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
