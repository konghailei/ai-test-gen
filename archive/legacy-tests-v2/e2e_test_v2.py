"""e2e 全量功能测试脚本（v2.0）— 覆盖项目管理/模块管理拆分、需求历史版本、XMind 导出等新功能。

用法：
    python tests/e2e_test_v2.py [--base-url http://localhost:8000]

环境要求：
    - 后端 API 已启动（默认 http://localhost:8000）
    - 默认管理员账号 admin / 888888

测试覆盖：
    1. 健康检查 + 登录
    2. 产品/项目/模块 CRUD（验证拆分后的接口）
    3. 需求上传 + 解析
    4. 澄清问题 + 答案提交
    5. 需求历史版本管理（重新生成后查询历史版本）
    6. 需求 XMind 导出（新增功能）
    7. 测试用例生成（轻量）
    8. 测试用例列表
    9. 测试用例 XMind 导出
    10. 知识库归档 + 检索
    11. 依赖图谱查询
    12. 项目管理跳转参数（验证 /requirements?project_id=xxx 等查询参数可用）
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import zipfile
from typing import Any

import requests


BASE_URL = "http://localhost:8000"
ADMIN_USER = "admin"
ADMIN_PWD = "888888"

# 颜色输出（Windows 兼容）
class C:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    END = "\033[0m"


# 测试统计
_results: list[tuple[str, bool, str]] = []


def log(name: str, ok: bool, detail: str = "") -> None:
    status = f"{C.GREEN}PASS{C.END}" if ok else f"{C.RED}FAIL{C.END}"
    line = f"  [{status}] {name}"
    if detail:
        line += f"  {C.CYAN}{detail}{C.END}" if ok else f"  {C.RED}{detail}{C.END}"
    print(line)
    _results.append((name, ok, detail))


def section(title: str) -> None:
    print(f"\n{C.BOLD}{C.CYAN}=== {title} ==={C.END}")


def assert_true(name: str, cond: bool, detail: str = "") -> None:
    log(name, bool(cond), detail)


# ============================================================================
# API 客户端
# ============================================================================

class ApiClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.token: str | None = None
        self.session = requests.Session()

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def login(self, username: str, password: str) -> dict:
        r = self.session.post(
            f"{self.base_url}/api/auth/login",
            json={"username": username, "password": password},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"登录失败: {data}")
        self.token = data["data"]["token"]
        return data["data"]  # 含 token / user 字段

    def get(self, path: str, **kw) -> Any:
        r = self.session.get(f"{self.base_url}{path}", headers=self._headers(), timeout=60, **kw)
        return self._unwrap(r)

    def post(self, path: str, body: Any = None, **kw) -> Any:
        r = self.session.post(f"{self.base_url}{path}", json=body, headers=self._headers(), timeout=120, **kw)
        return self._unwrap(r)

    def patch(self, path: str, body: Any = None) -> Any:
        r = self.session.patch(f"{self.base_url}{path}", json=body, headers=self._headers(), timeout=60)
        return self._unwrap(r)

    def delete(self, path: str) -> Any:
        r = self.session.delete(f"{self.base_url}{path}", headers=self._headers(), timeout=60)
        return self._unwrap(r)

    def get_raw(self, path: str, **kw) -> requests.Response:
        return self.session.get(f"{self.base_url}{path}", headers=self._headers(), timeout=120, **kw)

    def _unwrap(self, r: requests.Response) -> Any:
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()
        if isinstance(data, dict) and "success" in data:
            if not data["success"]:
                raise RuntimeError(f"API error: {data.get('message') or data}")
            return data.get("data", data)
        return data


# ============================================================================
# 测试用例
# ============================================================================

def test_health_and_login(api: ApiClient) -> None:
    section("1. 健康检查 + 登录")
    h = api.get("/api/health")
    assert_true("健康检查可访问", isinstance(h, dict) and "version" in h, str(h)[:200])

    user = api.login(ADMIN_USER, ADMIN_PWD)
    u = user.get("user", {}) if isinstance(user, dict) else {}
    assert_true("管理员登录", bool(api.token), f"user={u.get('username')}, role={u.get('role')}")


def test_products_projects_modules(api: ApiClient, state: dict) -> None:
    section("2. 产品 / 项目 / 模块 CRUD（拆分管理）")
    # 产品（注意：products router 挂载在 /api/products 前缀下，路由内部又是 /products）
    products = api.get("/api/products/products")
    assert_true("产品列表", isinstance(products, list), f"数量={len(products) if isinstance(products, list) else 'N/A'}")

    if not products:
        # 创建一个测试产品
        created = api.post("/api/products/products", {"name": "E2E测试产品", "description": "v2.0 e2e 测试自动创建"})
        pid = created["id"]
        assert_true("创建测试产品", True, f"pid={pid}")
    else:
        pid = products[0]["id"]
        assert_true("使用已有产品", True, f"pid={pid}, name={products[0].get('name')}")

    state["product_id"] = pid

    # 项目（完整路径 /api/products/products/{pid}/projects）
    projs = api.get(f"/api/products/products/{pid}/projects")
    assert_true("项目列表", isinstance(projs, list), f"数量={len(projs) if isinstance(projs, list) else 'N/A'}")

    if not projs:
        new_proj = api.post(f"/api/products/products/{pid}/projects", {"name": "E2E测试项目", "description": "测试项目"})
        proj_id = new_proj["id"]
        assert_true("创建测试项目", True, f"proj_id={proj_id}")
    else:
        proj_id = projs[0]["id"]
        assert_true("使用已有项目", True, f"proj_id={proj_id}")

    state["project_id"] = proj_id

    # 模块（完整路径 /api/products/products/{pid}/modules）
    mods = api.get(f"/api/products/products/{pid}/modules")
    assert_true("模块列表", isinstance(mods, list), f"数量={len(mods) if isinstance(mods, list) else 'N/A'}")

    if not mods:
        try:
            new_mod = api.post(f"/api/products/products/{pid}/modules", {"name": "E2E测试模块", "description": "测试模块"})
            mod_id = new_mod["id"]
            assert_true("创建测试模块", True, f"mod_id={mod_id}")
        except Exception as e:
            assert_true("创建测试模块", False, str(e)[:200])
            mod_id = None
    else:
        mod_id = mods[0]["id"]
        assert_true("使用已有模块", True, f"mod_id={mod_id}")

    state["module_id"] = mod_id


def test_dependency_graph(api: ApiClient, state: dict) -> None:
    section("3. 依赖图谱")
    pid = state.get("product_id")
    if not pid:
        assert_true("依赖图谱", False, "无产品 ID")
        return
    try:
        g = api.get(f"/api/products/dependency-graph?product_id={pid}")
        # dependency-graph 端点可能返回 ApiResponse 包装
        if isinstance(g, dict) and "nodes" in g:
            assert_true("依赖图谱查询", True, f"nodes={len(g.get('nodes', []))}, edges={len(g.get('edges', []))}")
        else:
            assert_true("依赖图谱查询", isinstance(g, dict), f"keys={list(g.keys())[:5] if isinstance(g, dict) else 'N/A'}")
    except Exception as e:
        assert_true("依赖图谱查询", False, str(e)[:200])


def test_requirement_upload_clarify(api: ApiClient, state: dict) -> None:
    section("4. 需求上传 + 解析 + 澄清")
    pid = state.get("product_id")
    proj_id = state.get("project_id")
    if not pid or not proj_id:
        assert_true("需求上传", False, "无产品/项目 ID")
        return

    # 测试需求文本（轻量级，避免占用 LLM 太久）
    sample_req = """# 用户登录功能需求

## 1. 功能概述
用户通过账号密码登录系统，支持邮箱/手机号登录。

## 2. 主流程
1. 用户访问登录页
2. 输入账号和密码
3. 系统校验账号密码
4. 登录成功，跳转首页

## 3. 异常分支
- 账号不存在：提示"账号未注册"
- 密码错误：提示"密码错误"，连续 5 次锁定 30 分钟
- 账号被禁用：提示"账号已禁用"

## 4. 业务规则
- 密码连续错误 5 次锁定账号 30 分钟
- 同一账号 1 分钟内最多尝试 3 次登录
- 登录成功后生成 JWT token，有效期 2 小时

## 5. 数据约束
- 账号：3-32 字符，字母数字下划线
- 密码：8-32 字符，必须包含字母和数字

## 6. 验收标准
- 正确账号密码可登录成功
- 错误密码 5 次后账号锁定
- token 2 小时后失效
"""

    try:
        # 上传需求
        result = api.post("/api/requirements/upload", {
            "raw_text": sample_req,
            "source_type": "text",
            "product_id": pid,
            "project_id": proj_id,
            "title": "E2E测试-用户登录功能",
        })
        req_id = result.get("req_id")
        assert_true("需求上传并解析", bool(req_id), f"req_id={req_id}, status={result.get('status')}")
        state["req_id"] = req_id
        state["initial_srr"] = result.get("srr_json", {})

        # 等待解析完成（如果是异步，最长等 180s）
        for _ in range(90):
            detail = api.get(f"/api/requirements/{req_id}")
            status = detail.get("status")
            if status in ("draft", "active", "pending_review"):
                break
            time.sleep(2)
        else:
            assert_true("解析完成", False, "180s 内未完成解析")
            return

        assert_true("解析完成", True, f"status={status}")

        # 澄清问题（如有）
        try:
            qs = api.get(f"/api/requirements/{req_id}/clarify/questions")
            questions = qs.get("questions", []) if isinstance(qs, dict) else qs
            assert_true("获取澄清问题", True, f"问题数={len(questions)}")

            # 提交澄清答案（如有问题）
            if questions:
                answers = {q["id"]: "测试回答：" + q.get("question", "")[:30] for q in questions}
                api.post(f"/api/requirements/{req_id}/clarify", {"answers": answers})
                assert_true("提交澄清答案", True, f"答案数={len(answers)}")
        except Exception as e:
            assert_true("澄清流程", False, str(e)[:200])

    except Exception as e:
        assert_true("需求上传", False, str(e)[:300])


def test_requirement_history_versions(api: ApiClient, state: dict) -> None:
    section("5. 需求历史版本管理（重新生成）")
    req_id = state.get("req_id")
    if not req_id:
        assert_true("历史版本", False, "无需求 ID")
        return

    # 重新生成（触发历史版本保存）
    try:
        result = api.post(f"/api/requirements/{req_id}/regenerate", {"extra_notes": "补充：登录失败需记录日志，便于安全审计"})
        task_id = result.get("task_id")
        assert_true("触发重新生成", True, f"task_id={task_id}")

        # 轮询任务状态（最多 90s）
        if task_id:
            for _ in range(45):
                task = api.get(f"/api/requirements/tasks/{task_id}")
                t_status = task.get("status")
                if t_status in ("completed", "failed", "interrupted"):
                    break
                time.sleep(2)
            assert_true("重新生成完成", t_status == "completed", f"task_status={t_status}")
        else:
            # 同步模式：直接查询需求状态
            for _ in range(45):
                detail = api.get(f"/api/requirements/{req_id}")
                if detail.get("status") in ("active", "pending_review", "draft"):
                    break
                time.sleep(2)
            assert_true("重新生成完成", True, f"req_status={detail.get('status')}")

        # 查询历史版本列表
        versions = api.get(f"/api/requirements/{req_id}/versions")
        v_items = versions.get("items", []) if isinstance(versions, dict) else []
        assert_true("历史版本列表", isinstance(v_items, list), f"版本数={len(v_items)}")

        if v_items:
            # 查看首个版本详情
            v_idx = v_items[0].get("version_index", 1)
            v_detail = api.get(f"/api/requirements/{req_id}/versions/{v_idx}")
            has_srr = isinstance(v_detail, dict) and "srr" in v_detail
            assert_true("历史版本详情", has_srr, f"version_index={v_idx}, has_srr={has_srr}")
        else:
            assert_true("历史版本详情", False, "无历史版本")

    except Exception as e:
        assert_true("历史版本管理", False, str(e)[:300])


def test_requirement_xmind_export(api: ApiClient, state: dict) -> None:
    section("6. 需求 XMind 导出（v2.0 新功能）")
    req_id = state.get("req_id")
    if not req_id:
        assert_true("需求 XMind 导出", False, "无需求 ID")
        return

    try:
        resp = api.get_raw(f"/api/requirements/{req_id}/export/xmind")
        assert_true("XMind HTTP 响应", resp.status_code == 200, f"status={resp.status_code}")

        # 验证是合法的 ZIP 文件
        content = resp.content
        assert_true("返回内容非空", len(content) > 100, f"size={len(content)} bytes")

        # 解压验证 content.xml 存在
        buf = io.BytesIO(content)
        with zipfile.ZipFile(buf, "r") as zf:
            names = zf.namelist()
            has_xml = "content.xml" in names
            assert_true("ZIP 包含 content.xml", has_xml, f"files={names}")
            if has_xml:
                xml = zf.read("content.xml").decode("utf-8", errors="ignore")
                assert_true("XML 内容有效", "<xmap-content" in xml and "<topic" in xml, f"xml_len={len(xml)}")

        # 验证 Content-Disposition
        cd = resp.headers.get("Content-Disposition", "")
        assert_true("Content-Disposition", "filename=" in cd and ".xmind" in cd, cd[:200])

    except Exception as e:
        assert_true("需求 XMind 导出", False, str(e)[:300])


def test_testcase_generation_and_list(api: ApiClient, state: dict) -> None:
    section("7. 测试用例生成 + 列表")
    req_id = state.get("req_id")
    if not req_id:
        assert_true("用例生成", False, "无需求 ID")
        return

    try:
        # 触发用例生成（异步任务）
        result = api.post("/api/generate", {"req_id": req_id, "project_id": state.get("project_id")})
        task_id = result.get("task_id")
        assert_true("触发生成", True, f"task_id={task_id}")

        # 轮询任务状态（最多 180s，LLM 生成耗时）
        final_status = None
        if task_id:
            for _ in range(90):
                try:
                    # 用 SSE 流式端点或者 tasks 端点查询
                    task = api.get(f"/api/requirements/tasks/{task_id}")
                    final_status = task.get("status")
                    if final_status in ("completed", "failed", "interrupted"):
                        break
                except Exception:
                    pass
                time.sleep(2)
            assert_true("生成任务完成", final_status == "completed", f"final_status={final_status}")
        else:
            assert_true("生成任务完成", False, "未返回 task_id")

        # 查询用例列表
        cases = api.get(f"/api/testcases?req_id={req_id}&page=1&page_size=10")
        items = cases.get("items", []) if isinstance(cases, dict) else []
        assert_true("用例列表查询", isinstance(items, list), f"数量={len(items)}, total={cases.get('total') if isinstance(cases, dict) else 'N/A'}")

        if items:
            state["case_ids"] = [c["id"] for c in items[:5]]

    except Exception as e:
        assert_true("用例生成", False, str(e)[:300])


def test_testcase_xmind_export(api: ApiClient, state: dict) -> None:
    section("8. 测试用例 XMind 导出")
    case_ids = state.get("case_ids")
    if not case_ids:
        assert_true("用例 XMind 导出", False, "无用例 ID")
        return

    try:
        resp = api.session.post(
            f"{api.base_url}/api/export",
            json={"format": "xmind", "case_ids": case_ids},
            headers=api._headers(),
            timeout=60,
        )
        assert_true("用例 XMind HTTP 响应", resp.status_code == 200, f"status={resp.status_code}")

        content = resp.content
        assert_true("返回内容非空", len(content) > 100, f"size={len(content)} bytes")

        buf = io.BytesIO(content)
        with zipfile.ZipFile(buf, "r") as zf:
            names = zf.namelist()
            has_xml = "content.xml" in names
            assert_true("用例 ZIP 包含 content.xml", has_xml, f"files={names}")

    except Exception as e:
        assert_true("用例 XMind 导出", False, str(e)[:300])


def test_knowledge_base(api: ApiClient, state: dict) -> None:
    section("9. 知识库归档 + 检索")
    req_id = state.get("req_id")
    if not req_id:
        assert_true("知识库", False, "无需求 ID")
        return

    try:
        # 归档需求
        archive_date = "2026-07-20"
        result = api.post(f"/api/requirements/{req_id}/archive", {"go_live_date": archive_date})
        assert_true("需求归档", True, f"result={str(result)[:200]}")

        # 知识库列表（检查归档后是否能在 KB 中找到）
        kb_list = api.get("/api/knowledge/entries?page=1&page_size=20")
        items = kb_list.get("items", []) if isinstance(kb_list, dict) else []
        assert_true("知识库列表", isinstance(items, list), f"数量={len(items)}")

        # 知识库问答检索（SSE 流式响应，验证状态码即可）
        try:
            qa_resp = api.session.post(
                f"{api.base_url}/api/knowledge-qa/ask",
                json={"question": "用户登录功能如何实现？", "top_k": 3},
                headers=api._headers(),
                timeout=60,
                stream=True,
            )
            # 读取流式响应的前几个事件，验证非空
            chunks = []
            for line in qa_resp.iter_lines():
                if line:
                    chunks.append(line)
                    if len(chunks) >= 5:
                        break
            assert_true("KB 问答检索", qa_resp.status_code == 200 and len(chunks) > 0, f"status={qa_resp.status_code}, chunks={len(chunks)}")
        except Exception as e:
            # KB QA 可能因模型加载失败，不阻断测试
            assert_true("KB 问答检索", False, f"(非阻断) {str(e)[:150]}")

    except Exception as e:
        assert_true("知识库归档", False, str(e)[:300])


def test_project_navigation_params(api: ApiClient, state: dict) -> None:
    section("10. 项目/模块管理跳转参数验证")
    pid = state.get("product_id")
    proj_id = state.get("project_id")
    mod_id = state.get("module_id")

    # 按项目过滤需求列表
    if proj_id:
        try:
            r = api.get(f"/api/requirements?page=1&page_size=10&project_id={proj_id}")
            assert_true("按 project_id 过滤需求", isinstance(r, dict), f"total={r.get('total') if isinstance(r, dict) else 'N/A'}")
        except Exception as e:
            assert_true("按 project_id 过滤需求", False, str(e)[:150])

    # 按项目过滤用例列表
    if proj_id:
        try:
            r = api.get(f"/api/testcases?page=1&page_size=10&project_id={proj_id}")
            assert_true("按 project_id 过滤用例", isinstance(r, dict), f"total={r.get('total') if isinstance(r, dict) else 'N/A'}")
        except Exception as e:
            assert_true("按 project_id 过滤用例", False, str(e)[:150])

    # 模块管理跳转依赖图谱
    if pid:
        try:
            r = api.get(f"/api/products/dependency-graph?product_id={pid}&module_id={mod_id or ''}")
            assert_true("按 module_id 查询依赖图谱", isinstance(r, dict), f"keys={list(r.keys())[:3] if isinstance(r, dict) else 'N/A'}")
        except Exception as e:
            assert_true("按 module_id 查询依赖图谱", False, str(e)[:150])


# ============================================================================
# 主入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--skip-generation", action="store_true", help="跳过用例生成测试（耗时）")
    args = parser.parse_args()

    base_url = args.base_url

    print(f"{C.BOLD}E2E 全量功能测试{C.END}")
    print(f"  Base URL: {base_url}")
    print(f"  账号: {ADMIN_USER}")
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    api = ApiClient(base_url)
    state: dict[str, Any] = {}

    # 依次执行测试
    test_health_and_login(api)
    test_products_projects_modules(api, state)
    test_dependency_graph(api, state)
    test_requirement_upload_clarify(api, state)
    test_requirement_history_versions(api, state)
    test_requirement_xmind_export(api, state)
    if not args.skip_generation:
        test_testcase_generation_and_list(api, state)
        test_testcase_xmind_export(api, state)
    else:
        print(f"\n{C.YELLOW}  [SKIP] 测试用例生成（--skip-generation）{C.END}")
    test_knowledge_base(api, state)
    test_project_navigation_params(api, state)

    # 汇总
    section("测试汇总")
    total = len(_results)
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = total - passed
    print(f"  {C.BOLD}总计: {total}  通过: {C.GREEN}{passed}{C.END}  失败: {C.RED}{failed}{C.END}{C.BOLD}{C.END}")

    if failed:
        print(f"\n{C.RED}失败项:{C.END}")
        for name, ok, detail in _results:
            if not ok:
                print(f"  - {name}: {detail}")
        sys.exit(1)
    else:
        print(f"\n{C.GREEN}{C.BOLD}全部测试通过！{C.END}")


if __name__ == "__main__":
    main()
