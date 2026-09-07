"""阶段5 API 路由层测试脚本。

测试覆盖（实现计划 V3）：
    组1：健康检查与基础可达性
    组2：需求管理（mock req_agent / clarify_agent）
    组3：用例 CRUD（DB 夹具）
    组4：知识库统计与模式列表
    组5：LLM 配置管理
    组6：导出（JSON / Markdown / Gherkin / pytest 四种格式）
    组7：SSE 流式生成（mock orchestrator）

运行方式：python tests/test_phase5_api.py
（不通过 pytest 运行，避免 fixture 误判，与阶段3/4测试保持一致）
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
from unittest.mock import AsyncMock, patch, MagicMock

# 确保导入 backend 目录
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from app.core.auth import get_default_token
from app.db.database import async_session, engine
from app.db.models import AnalysisTask, Requirement, TestCase


# 动态创建的需求 ID（upload 异步模式下由接口生成，测试结束后统一清理）
DYNAMIC_REQ_IDS: list[str] = []


# ============================================================================
# 颜色与结果统计
# ============================================================================

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录并打印单个测试结果。"""
    results.append((name, ok, detail))
    mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not ok else ""))


def section(title: str) -> None:
    """打印分组标题。"""
    print(f"\n{YELLOW}━━ {title} ━━{RESET}")


# ============================================================================
# 测试夹具数据（真实结构，非模拟业务数据）
# ============================================================================

TEST_REQ_ID = "REQ-TEST-0001"
TEST_REQ_ID2 = "REQ-TEST-0002"
TEST_CASE_ID = "TC-TEST-0001-0001"

TEST_SRR = {
    "srr_version": "1.0",
    "requirement": {
        "id": TEST_REQ_ID,
        "title": "用户登录功能",
        "summary": "支持账号密码登录",
        "product_id": "PROD-TEST",
        "priority": "P2",
        "modules": ["auth"],
        "change_type": "new",
    },
    "flows": {
        "happy_path": {"steps": [{"action": "输入账号密码", "expect": "登录成功"}]},
        "alternate_flows": [],
    },
    "preconditions": ["用户已注册"],
    "business_rules": [],
    "data_constraints": [],
    "test_points_hint": [],
}

TEST_CASE_JSON = {
    "id": TEST_CASE_ID,
    "title": "正确账号密码登录成功",
    "type": "functional",
    "priority": "P1",
    "preconditions": ["用户已注册"],
    "steps": [{"action": "输入正确账号密码", "expected": "登录成功"}],
    "expected": "返回 token",
}


async def setup_fixtures() -> None:
    """插入测试夹具数据到 DB。

    为保证测试可重复运行，先清理可能残留的测试数据（上次异常退出时可能未清理）。
    """
    from sqlalchemy import delete

    async with async_session() as session:
        # 先清理残留：任务、用例、需求、mock 数据（幂等，无残留时不会删除任何记录）
        await session.execute(delete(AnalysisTask).where(AnalysisTask.req_id.in_([TEST_REQ_ID, TEST_REQ_ID2])))
        await session.execute(delete(TestCase).where(TestCase.req_id.in_([TEST_REQ_ID, TEST_REQ_ID2])))
        await session.execute(delete(TestCase).where(TestCase.id == TEST_CASE_ID))
        await session.execute(delete(Requirement).where(Requirement.id.in_([TEST_REQ_ID, TEST_REQ_ID2])))
        await session.execute(delete(Requirement).where(Requirement.id == "REQ-MOCK-001"))
        await session.commit()

    async with async_session() as session:
        # 插入需求 1（组2/3/4/5 使用；2.8 提交澄清后状态会转 analyzing）
        req = Requirement(
            id=TEST_REQ_ID,
            product_id="PROD-TEST",
            title="用户登录功能",
            priority="P2",
            module="auth",
            modules=["auth"],
            status="pending_review",
            srr_json=TEST_SRR,
            raw_text="用户登录功能：支持账号密码登录",
            source_type="markdown",
            created_by="test",
        )
        session.add(req)

        # 插入需求 2（组7 专用：完整走 pending_review → confirm → confirmed → generate）
        req2 = Requirement(
            id=TEST_REQ_ID2,
            product_id="PROD-TEST",
            title="订单查询功能",
            priority="P2",
            module="order",
            modules=["order"],
            status="pending_review",
            srr_json=TEST_SRR,
            raw_text="订单查询功能：支持按订单号查询",
            source_type="markdown",
            created_by="test",
        )
        session.add(req2)

        # 插入用例
        case = TestCase(
            id=TEST_CASE_ID,
            req_id=TEST_REQ_ID,
            product_id="PROD-TEST",
            title="正确账号密码登录成功",
            type="functional",
            priority="P1",
            status="draft",
            case_json=TEST_CASE_JSON,
            source="ai_generated",
            gen_batch_id="BATCH-TEST-001",
            confidence=0.85,
        )
        session.add(case)
        await session.commit()


async def cleanup_fixtures() -> None:
    """清理测试夹具数据。"""
    async with async_session() as session:
        from sqlalchemy import delete
        req_ids = [TEST_REQ_ID, TEST_REQ_ID2, *DYNAMIC_REQ_IDS]
        await session.execute(delete(AnalysisTask).where(AnalysisTask.req_id.in_(req_ids)))
        await session.execute(delete(TestCase).where(TestCase.req_id == TEST_REQ_ID))
        await session.execute(delete(Requirement).where(Requirement.id.in_(req_ids)))
        await session.commit()


# ============================================================================
# 组1：健康检查与基础可达性
# ============================================================================

def test_health(client: TestClient) -> None:
    section("组1：健康检查与基础可达性")

    # 1.1 健康检查
    resp = client.get("/api/health")
    check("GET /api/health 返回 200", resp.status_code == 200, f"status={resp.status_code}")
    data = resp.json()
    check("健康检查 success=True", data.get("success") is True)
    check("健康检查含 version", "version" in data.get("data", {}))

    # 1.2 OpenAPI 文档可访问
    resp = client.get("/openapi.json")
    check("GET /openapi.json 返回 200", resp.status_code == 200)
    spec = resp.json()
    check("OpenAPI 含 /api/generate 路径", "/api/generate" in spec.get("paths", {}))
    check("OpenAPI 含 /api/requirements/upload 路径",
          "/api/requirements/upload" in spec.get("paths", {}))


# ============================================================================
# 组2：需求管理
# ============================================================================

def test_requirements(client: TestClient) -> None:
    section("组2：需求管理")

    # 2.1 需求列表
    resp = client.get("/api/requirements")
    check("GET /api/requirements 返回 200", resp.status_code == 200)
    data = resp.json()
    check("需求列表 success=True", data.get("success") is True)
    check("需求列表含 items", "items" in data.get("data", {}))
    check("需求列表含 total", "total" in data.get("data", {}))

    # 2.2 需求列表筛选
    resp = client.get("/api/requirements", params={"product_id": "PROD-TEST"})
    check("GET /api/requirements?product_id 筛选", resp.status_code == 200)
    items = resp.json().get("data", {}).get("items", [])
    check("筛选结果含测试需求", any(i["id"] == TEST_REQ_ID for i in items))

    # 2.3 需求详情
    resp = client.get(f"/api/requirements/{TEST_REQ_ID}")
    check("GET /api/requirements/{id} 返回 200", resp.status_code == 200)
    data = resp.json().get("data", {})
    check("详情含 srr_json", "srr_json" in data)
    check("详情含 raw_text", "raw_text" in data)

    # 2.4 需求详情 - 不存在
    resp = client.get("/api/requirements/REQ-NOT-EXIST")
    check("GET 不存在的需求返回 404", resp.status_code == 404)

    # 2.5 上传需求 - 空文本（Pydantic min_length 校验 → 422）
    resp = client.post("/api/requirements/upload", json={"raw_text": ""})
    check("POST upload 空文本返回 422", resp.status_code == 422)

    # 2.6 上传需求 - 异步模式（v2.15）：立即返回 task_id/req_id，后台协程分析
    # mock 后台协程，避免真实 LLM 调用
    with patch("app.api.requirements._run_requirement_analysis", new_callable=AsyncMock):
        resp = client.post("/api/requirements/upload", json={
            "raw_text": "测试需求文本",
            "product_id": "PROD-TEST",
        })
    check("POST upload（异步）返回 200", resp.status_code == 200, f"status={resp.status_code}")
    data = resp.json().get("data", {})
    check("upload 返回 task_id", bool(data.get("task_id")))
    check("upload 返回 req_id", bool(data.get("req_id")))
    check("upload 返回 analyzing 状态", data.get("status") == "analyzing")
    if data.get("req_id"):
        DYNAMIC_REQ_IDS.append(data["req_id"])

    # 2.7 获取澄清问题（读 DB 中的澄清状态，不实时调用 agent）
    resp = client.get(f"/api/requirements/{TEST_REQ_ID}/clarify/questions")
    check("GET clarify/questions 返回 200", resp.status_code == 200)
    data = resp.json().get("data", {})
    check("clarify 返回 questions 列表", isinstance(data.get("questions"), list))
    check("clarify 返回 status", "status" in data)

    # 2.8 提交澄清答案 - 异步模式（v2.15）：返回 task_id，后台协程重新解析
    # mock 后台协程，避免真实 LLM 调用
    with patch("app.api.requirements._run_clarify_reparse", new_callable=AsyncMock):
        resp = client.post(f"/api/requirements/{TEST_REQ_ID}/clarify", json={
            "answers": {"requirement.summary": "支持手机号验证码登录"}
        })
    check("POST clarify 提交答案返回 200", resp.status_code == 200)
    check("clarify 返回 task_id", bool(resp.json().get("data", {}).get("task_id")))

    # 2.9 提交澄清答案 - 空答案
    resp = client.post(f"/api/requirements/{TEST_REQ_ID}/clarify", json={"answers": {}})
    check("POST clarify 空答案返回 400", resp.status_code == 400)


async def _cleanup_mock_req(req_id: str) -> None:
    """清理 mock 上传的需求。"""
    async with async_session() as session:
        from sqlalchemy import delete
        await session.execute(delete(Requirement).where(Requirement.id == req_id))
        await session.commit()


# ============================================================================
# 组3：用例 CRUD
# ============================================================================

def test_testcases(client: TestClient) -> None:
    section("组3：用例 CRUD")

    # 3.1 用例列表
    resp = client.get("/api/testcases")
    check("GET /api/testcases 返回 200", resp.status_code == 200)
    data = resp.json().get("data", {})
    check("用例列表含 items", "items" in data)
    check("用例列表含 total", "total" in data)

    # 3.2 用例列表筛选
    resp = client.get("/api/testcases", params={"req_id": TEST_REQ_ID})
    check("GET /api/testcases?req_id 筛选", resp.status_code == 200)
    items = resp.json().get("data", {}).get("items", [])
    check("筛选结果含测试用例", any(i["id"] == TEST_CASE_ID for i in items))

    # 3.3 用例详情
    resp = client.get(f"/api/testcases/{TEST_CASE_ID}")
    check("GET /api/testcases/{id} 返回 200", resp.status_code == 200)
    data = resp.json().get("data", {})
    check("详情含 case_json", "case_json" in data)
    check("详情含 confidence", "confidence" in data)

    # 3.4 用例详情 - 不存在
    resp = client.get("/api/testcases/TC-NOT-EXIST")
    check("GET 不存在的用例返回 404", resp.status_code == 404)

    # 3.5 编辑用例
    resp = client.put(f"/api/testcases/{TEST_CASE_ID}", json={
        "title": "更新后的标题",
        "priority": "P0",
    })
    check("PUT /api/testcases/{id} 返回 200", resp.status_code == 200)
    data = resp.json().get("data", {})
    check("更新后 title 正确", data.get("title") == "更新后的标题")
    check("更新后 status=reviewed", data.get("status") == "reviewed")

    # 3.6 编辑用例 - 不存在
    resp = client.put("/api/testcases/TC-NOT-EXIST", json={"title": "x"})
    check("PUT 不存在的用例返回 404", resp.status_code == 404)

    # 3.7 反馈回流 - 采纳
    resp = client.post(f"/api/testcases/{TEST_CASE_ID}/feedback", json={
        "accepted": True,
        "comments": "用例质量良好",
    })
    check("POST feedback 采纳返回 200", resp.status_code == 200)
    check("反馈后 status=approved", resp.json().get("data", {}).get("new_status") == "approved")

    # 3.8 反馈回流 - 拒绝
    resp = client.post(f"/api/testcases/{TEST_CASE_ID}/feedback", json={
        "accepted": False,
        "comments": "步骤不完整",
    })
    check("POST feedback 拒绝返回 200", resp.status_code == 200)
    check("反馈后 status=rejected", resp.json().get("data", {}).get("new_status") == "rejected")

    # 3.9 反馈 - 不存在
    resp = client.post("/api/testcases/TC-NOT-EXIST/feedback", json={"accepted": True})
    check("POST feedback 不存在返回 404", resp.status_code == 404)

    # 3.10 删除用例 - 不存在
    resp = client.delete("/api/testcases/TC-NOT-EXIST")
    check("DELETE 不存在的用例返回 404", resp.status_code == 404)


# ============================================================================
# 组4：知识库统计与模式列表
# ============================================================================

def test_knowledge(client: TestClient) -> None:
    section("组4：知识库统计与模式列表")

    # 4.1 知识库统计
    resp = client.get("/api/knowledge/stats")
    check("GET /api/knowledge/stats 返回 200", resp.status_code == 200)
    data = resp.json().get("data", {})
    check("统计含 total_requirements", "total_requirements" in data)
    check("统计含 total_test_cases", "total_test_cases" in data)
    check("统计含 total_patterns", "total_patterns" in data)
    check("统计含 kb_empty", "kb_empty" in data)

    # 4.2 模式列表
    resp = client.get("/api/knowledge/patterns")
    check("GET /api/knowledge/patterns 返回 200", resp.status_code == 200)
    data = resp.json().get("data", {})
    check("模式列表含 items", "items" in data)
    # 种子数据应有 8 个模式
    check("种子模式数 >= 8", data.get("total", 0) >= 8, f"total={data.get('total')}")

    # 4.3 模式列表 - 按类别筛选
    resp = client.get("/api/knowledge/patterns", params={"category": "crud"})
    check("GET patterns?category=crud 筛选", resp.status_code == 200)
    items = resp.json().get("data", {}).get("items", [])
    check("筛选结果全为 crud 类别", all(i["category"] == "crud" for i in items))


# ============================================================================
# 组5：LLM 配置管理
# ============================================================================

def test_settings(client: TestClient) -> None:
    section("组5：LLM 配置管理")

    # 5.0 DB 级快照（asyncpg 原生连接，独立于应用 engine，测试后原样恢复
    # 防止占位 Key 污染真实环境；应用 engine 绑定 TestClient 事件循环，不可在此复用）
    import asyncpg

    from app.config import settings as _app_settings

    _pg_dsn = _app_settings.pg_async_url.replace("postgresql+asyncpg", "postgresql")

    async def _snapshot_llm_config() -> list[dict]:
        conn = await asyncpg.connect(_pg_dsn)
        try:
            return [dict(r) for r in await conn.fetch("SELECT * FROM llm_config ORDER BY id")]
        finally:
            await conn.close()

    async def _restore_llm_config(rows: list[dict]) -> None:
        conn = await asyncpg.connect(_pg_dsn)
        try:
            await conn.execute("DELETE FROM llm_config")
            if rows:
                cols = list(rows[0].keys())
                stmt = "INSERT INTO llm_config ({}) VALUES ({})".format(
                    ", ".join(cols), ", ".join(f"${i + 1}" for i in range(len(cols)))
                )
                await conn.executemany(stmt, [tuple(r[c] for c in cols) for r in rows])
        finally:
            await conn.close()

    snapshot_rows = asyncio.run(_snapshot_llm_config())
    try:
        # 5.1 获取 LLM 配置
        resp = client.get("/api/settings/llm")
        check("GET /api/settings/llm 返回 200", resp.status_code == 200)
        data = resp.json().get("data", {})
        check("配置含 providers 列表", "providers" in data)
        check("配置含 builtin_providers", "builtin_providers" in data)

        # 5.2 更新配置 - 重复提供商
        resp = client.put("/api/settings/llm", json={
            "providers": [
                {"provider": "deepseek", "api_key": "sk-test1234567890"},
                {"provider": "deepseek", "api_key": "sk-test1234567890"},
            ]
        })
        check("PUT 重复提供商返回 400", resp.status_code == 400)

        # 5.3 更新配置 - 空 providers 列表
        resp = client.put("/api/settings/llm", json={"providers": []})
        check("PUT 空 providers 返回 400", resp.status_code == 400)

        # 5.4 更新配置 - 有效（mock，不实际调用 LLM）
        resp = client.put("/api/settings/llm", json={
            "providers": [{
                "provider": "deepseek",
                "api_key": "sk-test1234567890",
                "model": "deepseek-chat",
                "is_primary": True,
            }]
        })
        check("PUT 有效配置返回 200", resp.status_code == 200)
        data = resp.json().get("data", {})
        providers = data.get("providers", [])
        check("更新后 providers 非空", len(providers) > 0)
        primary = next((p for p in providers if p.get("is_primary")), providers[0] if providers else {})
        check("更新后 provider=deepseek", primary.get("provider") == "deepseek")
        check("Key 已脱敏", "***" in primary.get("api_key", ""))

        # 5.5 测试连通性 - mock litellm.acompletion
        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_completion:
            mock_completion.return_value = MagicMock(
                choices=[MagicMock(message=MagicMock(content="ok"))]
            )
            resp = client.post("/api/settings/llm/test")
        check("POST /api/settings/llm/test（mock）返回 200", resp.status_code == 200)
        data = resp.json().get("data", {})
        check("连通测试 success=True", data.get("success") is True)
        check("连通测试含 latency_ms", "latency_ms" in data)
    finally:
        asyncio.run(_restore_llm_config(snapshot_rows))
        check("已恢复原 LLM 配置（环境保护）", True)


# ============================================================================
# 组6：导出
# ============================================================================

def test_export(client: TestClient) -> None:
    section("组6：导出")

    # 6.1 未指定 case_ids → 400（防止批量数据泄露的契约校验）
    resp = client.post("/api/export", json={"format": "json"})
    check("POST /api/export 无 case_ids 返回 400", resp.status_code == 400)

    # 6.2 指定 case_ids JSON 导出
    resp = client.post("/api/export", json={
        "format": "json",
        "case_ids": [TEST_CASE_ID],
    })
    check("POST /api/export JSON 返回 200", resp.status_code == 200,
          f"status={resp.status_code}")
    check("JSON 导出 Content-Type 正确",
          "application/json" in resp.headers.get("content-type", ""))
    try:
        cases = resp.json()
        check("JSON 导出返回数组", isinstance(cases, list))
        check("JSON 导出非空", len(cases) > 0)
    except Exception as e:
        check("JSON 导出可解析", False, str(e))

    # 6.3 Markdown 导出
    resp = client.post("/api/export", json={
        "format": "markdown",
        "case_ids": [TEST_CASE_ID],
    })
    check("POST /api/export markdown 返回 200", resp.status_code == 200,
          f"status={resp.status_code}")
    check("Markdown Content-Type 正确",
          "text/markdown" in resp.headers.get("content-type", ""))
    md_content = resp.text
    check("Markdown 含标题", "# 测试用例文档" in md_content)
    check("Markdown 含覆盖率概览", "覆盖率概览" in md_content)
    check("Markdown 含用例 ID", TEST_CASE_ID in md_content)
    check("Markdown 含追溯矩阵", "追溯矩阵" in md_content)
    check("Markdown 含步骤表格", "| 步骤 | 操作 | 预期结果 |" in md_content)

    # 6.4 Gherkin 导出
    resp = client.post("/api/export", json={
        "format": "gherkin",
        "case_ids": [TEST_CASE_ID],
    })
    check("POST /api/export gherkin 返回 200", resp.status_code == 200,
          f"status={resp.status_code}")
    gherkin_content = resp.text
    check("Gherkin 含 Feature", gherkin_content.startswith("Feature:"))
    check("Gherkin 含 Scenario", "Scenario:" in gherkin_content)
    check("Gherkin 含 Given/When/Then", "Given" in gherkin_content or "When" in gherkin_content)

    # 6.5 pytest 导出
    resp = client.post("/api/export", json={
        "format": "pytest",
        "case_ids": [TEST_CASE_ID],
    })
    check("POST /api/export pytest 返回 200", resp.status_code == 200,
          f"status={resp.status_code}")
    check("pytest Content-Type 正确",
          "text/x-python" in resp.headers.get("content-type", ""))
    py_content = resp.text
    check("pytest 含 import pytest", "import pytest" in py_content)
    check("pytest 含测试函数", "def test_tc_" in py_content)
    check("pytest 含 mark 装饰器", "@pytest.mark." in py_content)


# ============================================================================
# 组7：SSE 流式生成
# ============================================================================

def test_generate_sse(client: TestClient) -> None:
    section("组7：SSE 流式生成")

    # 7.1 生成 - 需求不存在
    resp = client.post("/api/generate", json={"req_id": "REQ-NOT-EXIST"})
    check("POST /api/generate 不存在需求返回 404", resp.status_code == 404)

    # 7.2 生成 - 非 confirmed 状态（业务规则：仅确认完成的需求可生成）
    resp = client.post("/api/generate", json={"req_id": TEST_REQ_ID2})
    check("非 confirmed 状态生成返回 409", resp.status_code == 409,
          f"status={resp.status_code}")

    # 7.3 需求确认（真实流程：pending_review → confirmed）
    resp = client.post(f"/api/requirements/{TEST_REQ_ID2}/confirm")
    check("POST confirm 返回 200", resp.status_code == 200,
          f"status={resp.status_code} body={str(resp.json())[:120]}")
    resp = client.get(f"/api/requirements/{TEST_REQ_ID2}")
    check("确认后状态为 confirmed",
          resp.json().get("data", {}).get("status") == "confirmed")

    # 7.4 生成 - mock orchestrator，验证 SSE 流
    async def mock_run_pipeline(*args, **kwargs):
        """模拟 orchestrator 产出 SSE 事件流。"""
        yield {"stage": "parse", "message": "解析中", "progress": 20, "req_id": TEST_REQ_ID2}
        yield {"stage": "select_channel", "message": "通道选择", "progress": 25, "channel": "FULL"}
        yield {"stage": "gate", "message": "门禁检查", "progress": 40, "gate_passed": True}
        yield {"stage": "generate", "message": "生成中", "progress": 75, "cases_count": 3}
        yield {"stage": "review", "message": "审核中", "progress": 90, "review_passed": 3}
        yield {
            "stage": "done", "message": "完成", "progress": 100,
            "req_id": TEST_REQ_ID2, "channel": "FULL",
            "cases": [{"id": "TC-1"}, {"id": "TC-2"}, {"id": "TC-3"}],
            "review_report": {"total_cases": 3, "passed_cases": 3},
        }

    with patch("app.api.generate.orchestrator.run_pipeline", side_effect=mock_run_pipeline):
        resp = client.post("/api/generate", json={"req_id": TEST_REQ_ID2})

    check("POST /api/generate（mock）返回 200", resp.status_code == 200,
          f"status={resp.status_code}")
    check("SSE Content-Type 正确",
          "text/event-stream" in resp.headers.get("content-type", ""))

    # 解析 SSE 流
    events = []
    for line in resp.iter_lines():
        if line.startswith("data: "):
            try:
                event = json.loads(line[6:])
                events.append(event)
            except json.JSONDecodeError:
                pass
    check("SSE 流产生事件", len(events) > 0, f"events={len(events)}")
    check("SSE 含 done 事件", any(e.get("stage") == "done" for e in events),
          f"stages={[e.get('stage') for e in events]}")
    check("SSE done 含 cases", any(
        e.get("stage") == "done" and len(e.get("cases", [])) == 3
        for e in events
    ))

    # 7.5 生成 - mock orchestrator 抛异常
    async def mock_run_pipeline_error(*args, **kwargs):
        yield {"stage": "parse", "message": "解析中", "progress": 20}
        raise RuntimeError("模拟 LLM 调用失败")

    with patch("app.api.generate.orchestrator.run_pipeline",
               side_effect=mock_run_pipeline_error):
        resp = client.post("/api/generate", json={"req_id": TEST_REQ_ID2})

    check("SSE 异常流返回 200", resp.status_code == 200)
    events = []
    for line in resp.iter_lines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    check("SSE 异常流含 error 事件",
          any(e.get("stage") == "error" for e in events),
          f"stages={[e.get('stage') for e in events]}")


# ============================================================================
# 主入口
# ============================================================================

def main() -> int:
    print(f"{YELLOW}╔══════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{YELLOW}║  阶段5 API 路由层测试                                          ║{RESET}")
    print(f"{YELLOW}╚══════════════════════════════════════════════════════════════╝{RESET}")

    from app.db.database import init_db

    async def prepare() -> None:
        await init_db()
        await setup_fixtures()
        await engine.dispose(close=False)

    async def cleanup() -> None:
        await engine.dispose(close=False)
        await cleanup_fixtures()
        await engine.dispose(close=False)

    asyncio.run(prepare())

    try:
        with TestClient(app) as client:
            client.headers.update({"Authorization": f"Bearer {get_default_token()}"})
            test_health(client)
            test_requirements(client)
            test_testcases(client)
            test_knowledge(client)
            test_settings(client)
            test_export(client)
            test_generate_sse(client)
    finally:
        asyncio.run(cleanup())

    # 汇总
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed

    print(f"\n{YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    if failed == 0:
        print(f"{GREEN}✓ 全部 {total} 项测试通过{RESET}")
    else:
        print(f"{RED}✗ {failed}/{total} 项测试失败：{RESET}")
        for name, ok, detail in results:
            if not ok:
                print(f"  {RED}✗{RESET} {name} — {detail}")
    print(f"{YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    from app.main import app
    sys.exit(main())
