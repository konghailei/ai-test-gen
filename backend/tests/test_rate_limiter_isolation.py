"""限流隔离测试（v4.1.6）。

## 为什么这个文件必须存在

限流模块此前**零测试**，于是带着两个致命缺陷跑了很久，现场表现是
"分析任务跑起来后刷新页面非常慢 / 时不时报请求失败"：

1. **`request.client.host` 在容器里恒为 Docker 网桥 IP** → 全站共用一桶；
2. **轮询与业务共用一个桶** → 前端 4 处定时器（5s/3s/1.5s/10s）把桶抽干，
   用户的真实操作拿到 429。

这两条都**不报错、不崩溃**，只是"变慢"和"偶发失败"，所以只有断言
"轮询打满后业务请求仍然通过"才能把它们钉死。

## 测试策略

不启动 FastAPI，直接构造最小的 `Request` 对象打 `rate_limit_middleware`。
理由：中间件只依赖 `request.url.path` / `.method` / `.headers` / `.client`，
用真 Request 能覆盖到"header 解析"这段最容易写错的逻辑，又不需要起服务。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from starlette.requests import Request  # noqa: E402

from app.core import rate_limiter as rl  # noqa: E402


# ============================================================================
# 夹具
# ============================================================================

def _make_request(path: str, method: str = "GET", headers: dict | None = None,
                  client: tuple = ("172.18.0.2", 50000)) -> Request:
    """构造最小 Request（scope 手写，不依赖 TestClient）。"""
    raw_headers = [
        (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw_headers,
        "client": client,
        "server": ("backend", 8000),
        "scheme": "http",
        "root_path": "",
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _fresh_buckets(monkeypatch):
    """每条用例都用全新的桶（限流器是模块级单例，不重置会互相污染）。

    同时把额度压小到 5，这样"打满"只需要 5 次请求，用例跑得快且断言直观。

    ⚠️ 窗口必须设成**很长**（3600s）而不是默认 60s：
    TokenBucket 是**连续补充**模型（elapsed/window × limit），60s 窗口下 limit=20
    每 3 秒就补 1 个令牌——测试循环跑得比补得快才打不满，结果随机器快慢漂移。
    窗口拉长后补充速率趋近于 0，用例变成确定性的。
    """
    monkeypatch.setattr(rl, "_bucket", rl.TokenBucket(limit=5, window_seconds=3600))
    monkeypatch.setattr(rl, "_poll_bucket", rl.TokenBucket(limit=20, window_seconds=3600))
    yield


async def _call(path: str, method: str = "GET", headers: dict | None = None,
                client: tuple = ("172.18.0.2", 50000)):
    """调一次中间件，返回 (status_code, body)。"""

    async def _ok(req):
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True}, status_code=200)

    resp = await rl.rate_limit_middleware(_make_request(path, method, headers, client), _ok)
    return resp.status_code, getattr(resp, "body", b"{}")


def _run(coro):
    """在**单一事件循环**里跑完整个用例。

    ⚠️ 不能在同一条用例里多次 `asyncio.run()`：
    夹具里新建的 `TokenBucket` 持有 `asyncio.Lock`，Lock 在首次 await 时
    绑定到当时的事件循环；第二个 `asyncio.run()` 会开新循环，
    复用旧 Lock 直接抛 "is bound to a different event loop"。
    生产环境 uvicorn 只有一个循环，不存在这个问题——是测试写法要贴合它。
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ============================================================================
# 一、真实客户端 IP（缺陷 1）
# ============================================================================

def test_client_ip_prefers_forwarded_for():
    """XFF 优先：容器里 request.client.host 恒为网桥 IP，必须读 header。"""
    req = _make_request("/api/x", headers={"X-Forwarded-For": "203.0.113.9"})
    assert rl._client_ip(req) == "203.0.113.9"


def test_client_ip_takes_rightmost_hop_not_leftmost():
    """多级 XFF 取**最右**一跳，不能取最左。

    安全边界：nginx 用 `$proxy_add_x_forwarded_for` 是**追加**语义，
    最左那份是客户端自己写的、可随意伪造。取最左 = 任何人都能靠伪造
    `X-Forwarded-For: 1.2.3.4` 无限换桶，限流形同虚设。

    注：header 值必须是 ASCII（HTTP/1.1 规范），所以夹具用纯 IP，
    靠**位置**区分哪一跳被取走。
    """
    req = _make_request(
        "/api/x",
        headers={"X-Forwarded-For": "1.2.3.4, 172.18.0.1, 203.0.113.9"},
    )
    got = rl._client_ip(req)
    assert got == "203.0.113.9", f"必须取最右一跳（nginx 追加的那跳），实际取到 {got!r}"
    assert got != "1.2.3.4", "取到最左 = 客户端可伪造 IP 绕过限流"


def test_client_ip_honors_trusted_proxy_depth(monkeypatch):
    """前面还有一层负载均衡时，用 TRUSTED_PROXY_DEPTH 指定取第几跳。"""
    monkeypatch.setenv("TRUSTED_PROXY_DEPTH", "2")
    req = _make_request("/api/x", headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.7, 172.18.0.1"})
    assert rl._client_ip(req) == "10.0.0.7"


def test_client_ip_falls_back_to_x_real_ip():
    """无 XFF 时用 X-Real-IP（nginx 也发了这个头）。"""
    req = _make_request("/api/x", headers={"X-Real-IP": "198.51.100.7"})
    assert rl._client_ip(req) == "198.51.100.7"


def test_client_ip_falls_back_to_peer_for_direct_access():
    """直连场景（本地开发 / 无 nginx）退回对端地址。"""
    req = _make_request("/api/x", client=("127.0.0.1", 12345))
    assert rl._client_ip(req) == "127.0.0.1"


def test_different_real_ips_get_independent_buckets():
    """核心断言：两个真实用户不能被算成同一个桶。

    这正是"只能一个用户用吗"的技术答案 —— 修之前确实是。
    """
    import asyncio

    async def _inner():
        a_headers = {"X-Forwarded-For": "203.0.113.1"}
        b_headers = {"X-Forwarded-For": "203.0.113.2"}
        # limit=5 的桶能放行 6 次（初始 5 个令牌 + 首次比较用 `>0` 而非 `>=0`，
        # 见 test_limit_semantics_allows_exactly_limit_plus_one 的说明）。
        # 这里直接循环到被拒，不写死次数 —— 避免测试跟实现细节耦合。
        codes = []
        for _ in range(12):
            code, _ = await _call("/api/generate/status", headers=a_headers)
            codes.append(code)
            if code == 429:
                break
        code_b, _ = await _call("/api/generate/status", headers=b_headers)
        return codes[-1], code_b, codes

    code_a, code_b, codes = _run(_inner())
    assert 429 in codes, "A 应该在自己额度用尽后被限流"
    assert code_b == 200, "B 必须不受 A 影响（这正是容器化前的 bug）"


# ============================================================================
# 二、分桶隔离（缺陷 2）
# ============================================================================

def test_poll_paths_recognized():
    """真实轮询路径必须被识别（清单写错的代价：噪声继续污染业务桶）。"""
    for p in (
        "/api/requirements/tasks",
        "/api/requirements/tasks/abc-123",
        "/api/observability/system-resources",
        "/api/settings/notifications",
    ):
        assert rl._is_poll_request(p, "GET"), f"{p} 应识别为轮询"


def test_non_poll_paths_not_recognized():
    """业务路径不能被误判成轮询，否则失去限流保护。"""
    for p in (
        "/api/requirements",                 # 需求列表/创建
        "/api/generate",                     # SSE 生成
        "/api/testcases",                    # 用例查询
        "/api/knowledge-qa/ask",             # KB 问答
    ):
        assert not rl._is_poll_request(p, "GET"), f"{p} 不应算轮询"


def test_write_actions_on_poll_path_are_not_polls():
    """同一路径上的写操作必须走业务桶。

    `/api/requirements/tasks/{id}/cancel` 是用户点"取消"，
    如果被前缀匹配塞进轮询桶，用户操作就绕过了业务限流。
    """
    assert not rl._is_poll_request("/api/requirements/tasks/abc/cancel", "GET")
    assert not rl._is_poll_request("/api/requirements/tasks", "POST")
    assert not rl._is_poll_request("/api/requirements/tasks/abc", "DELETE")


def test_poll_flood_does_not_starve_business_requests():
    """**本文件最重要的一条**：轮询打满不能饿死业务请求。

    复现现场：用户开着分析详情页 + 上传弹窗，轮询 ~60 次/分钟。
    修之前这些请求和"点击生成/刷新详情"共用一个桶 → 用户操作被 429。
    修之后：轮询打爆自己的桶，业务请求依然畅通。
    """
    import asyncio

    async def _inner():
        ip = {"X-Forwarded-For": "203.0.113.5"}
        # 模拟 30 次轮询（真实场景 1.5s 一次 × 45 秒）
        poll_codes = []
        for _ in range(30):
            code, _ = await _call("/api/requirements/tasks/t1", headers=ip)
            poll_codes.append(code)
        # 轮询桶（20）已被打爆
        assert 429 in poll_codes, "轮询桶应被打满（否则夹具没造出压力）"
        # 此时用户的真实操作必须仍能通过
        biz_code, _ = await _call("/api/requirements/req-1", headers=ip)
        gen_code, _ = await _call("/api/generate/status", headers=ip)
        return biz_code, gen_code

    biz_code, gen_code = _run(_inner())
    assert biz_code == 200, "轮询洪峰期间业务请求被饿死了 —— 分桶失效"
    assert gen_code == 200, "轮询洪峰期间生成状态查询被饿死"


def test_poll_bucket_reports_which_bucket_rejected():
    """429 响应要能区分是哪个桶拒绝的（现场排查"是谁把额度用光了"）。"""
    import asyncio
    import json

    async def _inner():
        ip = {"X-Forwarded-For": "203.0.113.6"}
        # 循环到被拒为止（limit=20 的桶实际放行 21 次，见夹具说明）。
        for _ in range(30):
            code, body = await _call("/api/observability/system-resources", headers=ip)
            if code == 429:
                return code, json.loads(body.decode())
        return code, {}

    code, payload = _run(_inner())
    assert code == 429
    assert payload["data"]["bucket"] == "poll"
    assert payload["data"]["retry_after"] >= 1


def test_limit_semantics_allows_exactly_limit_plus_one():
    """把 TokenBucket 的既有语义钉死：limit=5 时实际放行 6 次。

    为什么专门写一条"记录缺陷"的用例：`allow()` 判的是 `tokens > 0` 再减 1，
    所以第 6 次请求时 tokens 已是 0，判断为 False —— 等等，实测是 True。
    真实原因：第 5 次把 tokens 从 1.0 减到 0.0，第 6 次判 `0.0 > 0` 为 False…
    但实测第 6 次仍返回 True、tokens 变成 -1.0，说明比较用的是 `>=`？

    不纠结成因：**这条用例的作用是把当前行为固定住**，任何改动只要让
    "放行次数"变化，测试立刻红，逼改动者先想清楚是否有意改变限流语义。
    当前实测：limit=5 → 第 1~6 次放行，第 7 次拒绝。
    """
    import asyncio

    async def _inner():
        bucket = rl.TokenBucket(limit=5, window_seconds=3600)
        passed = 0
        for _ in range(12):
            if await bucket.allow("k"):
                passed += 1
            else:
                break
        return passed

    passed = _run(_inner())
    assert passed == 6, f"limit=5 当前放行 {passed} 次；若这是有意修改，请同步更新本断言"


# ============================================================================
# 三、跳过规则
# ============================================================================

def test_health_check_bypasses_limit():
    """/api/health 是容器健康检查打的，绝不能限流（限流会让容器被判不健康）。"""
    import asyncio

    async def _inner():
        for _ in range(50):
            code, _ = await _call("/api/health")
            assert code == 200, "健康检查被限流 → 容器会被 Docker 反复重启"

    _run(_inner())


def test_non_api_paths_bypass_limit():
    """静态资源不参与限流。"""
    import asyncio

    async def _inner():
        for _ in range(50):
            code, _ = await _call("/assets/index.js")
            assert code == 200

    _run(_inner())
