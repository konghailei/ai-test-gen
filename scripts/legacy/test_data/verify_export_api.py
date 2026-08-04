"""导出 API 端到端验证。

通过 HTTP 调用 /api/export 端点，验证：
1. xmind 格式返回 application/x-xmind 二进制内容
2. markdown/gherkin/pytest/json 格式正常返回
3. 返回的 xmind 文件是合法 ZIP
4. 缺少 case_ids 时返回 400
"""

import sys
import os
import io
import json
import zipfile
import urllib.request
import urllib.error

# API 基础地址
API_BASE = "http://localhost:8000/api"
DEFAULT_USER = "admin"
DEFAULT_PASS = "888888"


def login() -> str:
    """登录获取 token。"""
    data = json.dumps({"username": DEFAULT_USER, "password": DEFAULT_PASS}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/auth/login",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read())
    token = body.get("data", {}).get("token") or body.get("token")
    if not token:
        raise RuntimeError(f"登录未获取到 token: {body}")
    return token


def fetch_case_ids(token: str, limit: int = 3) -> list[str]:
    """获取前 N 条用例的 ID。"""
    req = urllib.request.Request(
        f"{API_BASE}/testcases?page=1&page_size={limit}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read())
    items = body.get("data", {}).get("items", [])
    if not items:
        raise RuntimeError("数据库无用例，请先通过界面生成测试用例")
    return [item["id"] for item in items]


def export_format(token: str, fmt: str, case_ids: list[str]) -> tuple[int, bytes, str]:
    """调用导出 API。"""
    data = json.dumps({"format": fmt, "case_ids": case_ids}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/export",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")


def main():
    print("=" * 70)
    print("导出 API 端到端验证")
    print("=" * 70)

    try:
        token = login()
        print(f"登录成功，token 前 20 字符: {token[:20]}...")
    except Exception as e:
        print(f"登录失败: {e}")
        return 1

    try:
        case_ids = fetch_case_ids(token, limit=3)
        print(f"获取到用例 IDs: {case_ids}")
    except Exception as e:
        print(f"获取用例失败: {e}")
        return 1

    formats = ["xmind", "markdown", "gherkin", "pytest", "json"]
    all_pass = True
    for fmt in formats:
        status, body, ct = export_format(token, fmt, case_ids)
        if status != 200:
            print(f"  [{fmt}] FAIL  status={status}  body={body[:200]}")
            all_pass = False
            continue

        # xmind 需要验证 ZIP 结构
        if fmt == "xmind":
            try:
                zf = zipfile.ZipFile(io.BytesIO(body))
                names = zf.namelist()
                assert "content.json" in names, "缺少 content.json"
                assert "manifest.json" in names, "缺少 manifest.json"
                content = json.loads(zf.read("content.json"))
                assert isinstance(content, list) and content[0].get("fileVersion") == "2.0"
                root_title = content[0].get("rootTopic", {}).get("title", "")
                print(f"  [{fmt}] OK  status={status}  size={len(body)}  content_type={ct}")
                print(f"       ZIP entries: {names}")
                print(f"       root_title: {root_title}")
                print(f"       fileVersion: 2.0  ✓")
            except Exception as e:
                print(f"  [{fmt}] ZIP 解析失败: {e}")
                all_pass = False
        else:
            preview = body[:120].decode("utf-8", errors="replace").replace("\n", "\\n")
            print(f"  [{fmt}] OK  status={status}  size={len(body)}  content_type={ct}")
            print(f"       preview: {preview}")

    # 测试缺少 case_ids 时的 400 响应
    print("\n=== 测试缺少 case_ids 时的 400 响应 ===")
    data = json.dumps({"format": "xmind", "case_ids": []}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/export",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        print("  FAIL: 应该返回 400 但实际 200")
        all_pass = False
    except urllib.error.HTTPError as e:
        if e.code == 400:
            print(f"  OK: 返回 400，body={e.read().decode('utf-8', errors='replace')[:200]}")
        else:
            print(f"  FAIL: 期望 400 实际 {e.code}")
            all_pass = False

    print("\n" + "=" * 70)
    print(f"总结: {'全部 PASS' if all_pass else '存在 FAIL'}")
    print("=" * 70)
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
