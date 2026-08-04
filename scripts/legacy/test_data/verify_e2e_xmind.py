"""端到端验证：优化的生成 prompt + xmind 导出。

流程：
1. 登录获取 token
2. 查询已有的 active 需求
3. 触发新的测试用例生成（使用优化的 prompt）
4. 等待生成完成，查询新用例
5. 验证用例质量（steps 数量、expected 可观测性、preconditions 完整性）
6. 导出为 xmind 验证
"""

import sys
import os
import io
import json
import time
import zipfile
import urllib.request
import urllib.error

API_BASE = "http://localhost:8000/api"
DEFAULT_USER = "admin"
DEFAULT_PASS = "888888"


def http_request(method: str, path: str, token: str = "", body: dict = None, stream: bool = False):
    """发起 HTTP 请求，支持 SSE 流式响应。"""
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    return urllib.request.urlopen(req, timeout=300 if stream else 30)


def login() -> str:
    data = json.dumps({"username": DEFAULT_USER, "password": DEFAULT_PASS}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/auth/login",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read())
    return body.get("data", {}).get("token") or body.get("token")


def list_active_requirements(token: str) -> list[dict]:
    """获取 active 状态的需求列表。"""
    with http_request("GET", "/requirements?page=1&page_size=20&status=active", token) as resp:
        body = json.loads(resp.read())
    return body.get("data", {}).get("items", [])


def generate_cases_sse(token: str, req_id: str):
    """通过 SSE 触发用例生成，返回最终事件。"""
    url = f"{API_BASE}/generate"
    data = json.dumps({"req_id": req_id}).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    final_event = None
    with urllib.request.urlopen(req, timeout=300) as resp:
        buffer = ""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buffer:
                event_str, buffer = buffer.split("\n\n", 1)
                event = {}
                for line in event_str.split("\n"):
                    if line.startswith("data: "):
                        try:
                            event = json.loads(line[6:])
                        except json.JSONDecodeError:
                            event = {"raw": line[6:]}
                if event:
                    if event.get("stage"):
                        print(f"    [{event['stage']}] {event.get('message', '')} ({event.get('progress', 0)}%)")
                    if event.get("event") in ("done", "complete", "error", "failed"):
                        final_event = event
    return final_event


def list_testcases(token: str, req_id: str) -> list[dict]:
    """获取指定需求的所有用例。"""
    with http_request("GET", f"/testcases?req_id={req_id}&page=1&page_size=100", token) as resp:
        body = json.loads(resp.read())
    return body.get("data", {}).get("items", [])


def get_testcase_detail(token: str, case_id: str) -> dict:
    """获取用例详情。"""
    with http_request("GET", f"/testcases/{case_id}", token) as resp:
        body = json.loads(resp.read())
    return body.get("data", {})


def export_xmind(token: str, case_ids: list[str]) -> bytes:
    """导出为 xmind。"""
    url = f"{API_BASE}/export"
    data = json.dumps({"format": "xmind", "case_ids": case_ids}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def analyze_case_quality(case: dict) -> dict:
    """分析用例质量。"""
    case_json = case.get("case_json", {}) or {}
    steps = case_json.get("steps", []) or []
    preconditions = case_json.get("preconditions", []) or []
    expected = case_json.get("expected", "")
    cleanup = case_json.get("cleanup", []) or []

    # 检查步骤是否原子化（每个 action 不应包含分号或多个动作词）
    atomic_violations = 0
    fuzzy_expected = 0
    for step in steps:
        action = step.get("action", "")
        exp = step.get("expected", "")
        # 简单启发式：action 不应包含 " 然后 " " 之后 " 等连接词
        if any(kw in action for kw in [" 然后 ", " 之后 ", " 并 "]):
            atomic_violations += 1
        # expected 不应使用模糊词
        if any(kw in exp for kw in ["成功", "正常", "失败", "正确"]):
            if not any(kw in exp for kw in ["成功登录", "正常显示", "失败提示", "正确显示"]):
                # 排除一些合法用法
                pass
            fuzzy_expected += 1

    return {
        "id": case.get("id", ""),
        "title": case.get("title", "")[:60],
        "type": case.get("type", ""),
        "priority": case.get("priority", ""),
        "confidence": case.get("confidence", 0),
        "steps_count": len(steps),
        "preconditions_count": len(preconditions),
        "has_expected": bool(expected),
        "cleanup_count": len(cleanup),
        "atomic_violations": atomic_violations,
        "fuzzy_expected": fuzzy_expected,
        "steps_preview": [
            {
                "action": s.get("action", "")[:80],
                "expected": s.get("expected", "")[:80],
            }
            for s in steps[:3]
        ],
    }


def main():
    print("=" * 70)
    print("端到端验证：优化 prompt + xmind 导出")
    print("=" * 70)

    # 1. 登录
    try:
        token = login()
        print(f"✓ 登录成功")
    except Exception as e:
        print(f"✗ 登录失败: {e}")
        return 1

    # 2. 查询 active 需求
    reqs = list_active_requirements(token)
    if not reqs:
        print("✗ 无 active 状态的需求，请先上传需求并完成分析")
        return 1
    print(f"✓ 找到 {len(reqs)} 个 active 需求")
    req = reqs[0]
    req_id = req["id"]
    print(f"  使用第一个需求: {req_id} — {req.get('title', '')[:60]}")

    # 3. 触发生成（使用优化的 prompt）
    print(f"\n▶ 触发用例生成 (req_id={req_id})...")
    print("  （使用包含 ICOAST + 三目标法 + 原子步骤规范的优化 prompt）")
    try:
        final = generate_cases_sse(token, req_id)
        if final and final.get("event") in ("error", "failed"):
            print(f"✗ 生成失败: {final}")
            return 1
        print(f"✓ 生成完成: {final}")
    except Exception as e:
        print(f"✗ 生成过程出错: {e}")
        return 1

    # 4. 查询新生成的用例
    time.sleep(2)  # 等待数据库写入
    cases = list_testcases(token, req_id)
    if not cases:
        print("✗ 未查询到用例")
        return 1
    print(f"\n✓ 查询到 {len(cases)} 个用例")

    # 5. 取前 5 条用例详情并分析质量
    print(f"\n=== 用例质量分析（前 5 条）===")
    sample_ids = [c["id"] for c in cases[:5]]
    quality_results = []
    for case_id in sample_ids:
        try:
            case_detail = get_testcase_detail(token, case_id)
            q = analyze_case_quality(case_detail)
            quality_results.append(q)
            print(f"\n  [{q['id']}] {q['title']}")
            print(f"    type={q['type']} priority={q['priority']} confidence={q['confidence']}")
            print(f"    steps={q['steps_count']} preconditions={q['preconditions_count']} cleanup={q['cleanup_count']}")
            if q['atomic_violations']:
                print(f"    ⚠ 原子性违规: {q['atomic_violations']}")
            if q['fuzzy_expected']:
                print(f"    ⚠ 模糊预期词: {q['fuzzy_expected']}")
            for i, s in enumerate(q['steps_preview'], 1):
                print(f"    步骤{i}: {s['action']}")
                print(f"          预期: {s['expected']}")
        except Exception as e:
            print(f"  ✗ 获取用例 {case_id} 详情失败: {e}")

    # 6. 整体质量统计
    if quality_results:
        print(f"\n=== 整体质量统计 ===")
        avg_steps = sum(q["steps_count"] for q in quality_results) / len(quality_results)
        avg_preconditions = sum(q["preconditions_count"] for q in quality_results) / len(quality_results)
        avg_conf = sum(q["confidence"] for q in quality_results) / len(quality_results)
        total_atomic = sum(q["atomic_violations"] for q in quality_results)
        total_fuzzy = sum(q["fuzzy_expected"] for q in quality_results)
        print(f"  平均步骤数: {avg_steps:.1f}（优化前约 3-4，预期 ≥ 3）")
        print(f"  平均前置条件数: {avg_preconditions:.1f}")
        print(f"  平均置信度: {avg_conf:.3f}")
        print(f"  原子性违规总数: {total_atomic}（预期 0）")
        print(f"  模糊预期词总数: {total_fuzzy}（预期 0 或接近 0）")

    # 7. 导出 xmind 验证
    print(f"\n=== XMind 导出验证 ===")
    all_case_ids = [c["id"] for c in cases]
    try:
        xmind_bytes = export_xmind(token, all_case_ids)
        # 验证 ZIP 结构
        zf = zipfile.ZipFile(io.BytesIO(xmind_bytes))
        names = zf.namelist()
        assert "content.json" in names
        assert "manifest.json" in names
        content = json.loads(zf.read("content.json"))
        assert isinstance(content, list) and content[0].get("fileVersion") == "2.0"
        root = content[0].get("rootTopic", {})
        children = root.get("children", {}).get("attached", [])
        # 第二层应包含概要 + 类型分组
        type_groups = [c for c in children if "(" in c.get("title", "")]
        print(f"  ✓ XMind 文件大小: {len(xmind_bytes)} bytes")
        print(f"  ✓ ZIP entries: {names}")
        print(f"  ✓ fileVersion: 2.0")
        print(f"  ✓ 根主题: {root.get('title', '')}")
        print(f"  ✓ 类型分组数: {len(type_groups)}")
        for tg in type_groups:
            print(f"    - {tg.get('title', '')}")
        # 保存到本地
        out_path = "test_data/latest_testcases.xmind"
        with open(out_path, "wb") as f:
            f.write(xmind_bytes)
        print(f"  ✓ 已保存: {out_path}")
    except Exception as e:
        print(f"  ✗ XMind 导出失败: {e}")
        import traceback
        traceback.print_exc()
        return 1

    print("\n" + "=" * 70)
    print("端到端验证完成")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
