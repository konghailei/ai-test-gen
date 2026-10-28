"""内存 Token Bucket 限流中间件。

实现架构文档 docs/00 L1 防护的"RPM 限速"能力。
非分布式版本，适用于单实例部署；生产多实例需切换 Redis 版。

设计依据：
- docs/00-architecture-overview.md §2（L1 防护: RPM+TPM 双轴限速）
- docs/00-architecture-overview.md §9（Token Bucket 算法）

线程安全：使用 asyncio.Lock 保护计数器操作，避免异步协程竞争。

## v4.1.6：为什么限流会成为"刷新页面非常慢"的元凶

三个独立缺陷叠在一起：

1. **限流 key 用 `request.client.host`** —— 容器部署时请求来自 nginx，
   在 Docker 网桥里恒为 `172.18.0.x`（同一台机器就是同一个值）。于是**全站所有人共享
   一个桶**：前端 `Layout` 每 5s 轮询一次任务角标（12 次/分钟）、`Tasks` 页 3s 一次
   （20 次/分钟）、`UploadRequirementModal` 1.5s 一次（40 次/分钟）——
   光轮询就能把 `API_RPM_LIMIT=60` 打满，**任何人的业务请求都拿到 429**。
   nginx 早就透传了 `X-Real-IP` / `X-Forwarded-For`，是后端没读。

2. **轮询与业务共用一个桶** —— 轮询是"背景噪声"，却和"用户点了生成"抢同一批令牌。
   噪声把桶抽干，用户操作就被拒。二者必须分桶。

3. **一次 429 就等于一次失败** —— 前端 `client.ts` 没有退避重试，
   `Layout` 里还是 `catch {}` 静默吞掉。表现成"页面卡住/数据不刷新"，而不是报错。

本模块对应修 1、2；第 3 条在 `frontend/src/api/client.ts`。
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from app.config import settings
from app.utils.logger import logger


class TokenBucket:
    """内存 Token Bucket 限流器。

    每 `window_seconds` 秒补充 `limit` 个令牌。
    每个请求消耗 1 个令牌，令牌耗尽则拒绝。

    线程安全：使用 asyncio.Lock 保护计数器，避免 FastAPI 异步协程并发竞争。
    """

    def __init__(self, limit: int, window_seconds: int = 60):
        """初始化 Token Bucket。

        Args:
            limit: 窗口期内最大请求数
            window_seconds: 窗口大小（秒）
        """
        self.limit = limit
        self.window_seconds = window_seconds
        self._lock = asyncio.Lock()
        self._buckets: dict[str, dict] = defaultdict(lambda: {
            "tokens": limit,
            "last_refill": time.time(),
        })

    async def allow(self, key: str) -> bool:
        """检查是否允许请求通过（线程安全）。

        Args:
            key: 限流标识（通常是客户端 IP）

        Returns:
            bool: True 表示允许通过
        """
        async with self._lock:
            now = time.time()
            # 惰性清理：桶数量超阈值时，删除长期不活跃的桶，防止内存无限增长。
            # 每个唯一 IP 永久驻留会导致 OOM（IPv6/NAT 轮换/恶意伪造不同源 IP）。
            if len(self._buckets) > 10000:
                stale_cutoff = now - self.window_seconds * 10
                stale_keys = [
                    k for k, v in self._buckets.items()
                    if v["last_refill"] < stale_cutoff
                ]
                for k in stale_keys:
                    del self._buckets[k]
            bucket = self._buckets[key]

            # 补充令牌（v3.4.1 修复 M-4：改用连续补充模型）
            # 原实现在 elapsed < window 分支里执行 last_refill = now - (elapsed % window)，
            # 而 elapsed % window == elapsed，该行恒等于旧值（空操作）——即 last_refill
            # 永不推进；于是 refill 每轮都从同一个原点重复累加，同一段时间被反复入账。
            # 实测 limit=5/window=2s：现行实现 1 秒内放行 16 次，修正后 3 次（理论 2~3）。
            elapsed = now - bucket["last_refill"]
            if elapsed > 0:
                capped = min(elapsed, float(self.window_seconds))
                refill = capped / self.window_seconds * self.limit
                bucket["tokens"] = min(float(self.limit), bucket["tokens"] + refill)
                bucket["last_refill"] = now

            # 消耗令牌
            if bucket["tokens"] > 0:
                bucket["tokens"] -= 1
                return True
            return False

    def next_token_wait(self) -> int:
        """距下一个令牌可用的秒数（至少 1 秒）。

        v3.4.1 修复 M-4：Retry-After 此前返回整个窗口长度，远大于真实等待时间。
        """
        if not self.window_seconds or not self.limit:
            return max(1, int(self.window_seconds or 1))
        rate = self.limit / self.window_seconds
        return max(1, int(1 / rate) if rate > 0 else int(self.window_seconds))


# ============================================================================
# 分桶：轮询噪声 与 业务操作 必须分开计费
# ============================================================================
# 为什么分桶（v4.1.6）：
#   前端有 4 处定时器（Layout 5s / Tasks 3s / 上传弹窗 1.5s / Dashboard 10s）。
#   单开一个"分析任务详情页"，光轮询就 ~20 次/分钟；再开个上传弹窗能到 60+。
#   这些请求**不承载用户意图**，却和"点击生成"抢同一批令牌 → 用户操作被 429。
#   拆桶后：轮询打爆自己的桶只影响轮询自己（表现为角标延迟刷新，用户无感），
#   业务请求的桶始终有令牌可用。
#
# 判定方式：按**路径**而非 HTTP 方法（GET 也可能是重操作，如导出）。
# 这份清单只放"定时器会打的只读接口"，宁可少列：列错的代价是噪声没被隔离，
# 列漏的代价是业务请求被误判成噪声、失去限流保护。
#
# ⚠️ 路径必须与 `frontend/src/api/client.ts` 里的真实调用一一对齐
#    （v4.1.6 第一版按"看起来应该叫这个名字"猜了 4 条，实际全不对 ——
#     真实是 `/requirements/tasks` 而不是 `/api/tasks`）。
#    排序：精确路径在前，前缀路径在后，方便人眼核对。
_POLL_PATTERNS = (
    "/api/requirements/tasks",              # Layout 角标(5s) + Tasks 页(3s) + 澄清/上传进度(getTask 1.5s)
    "/api/observability/system-resources",  # Dashboard 资源条(10s)
    "/api/settings/notifications",          # 管理员未读数(60s)
)


def _is_poll_request(path: str, method: str) -> bool:
    """是否属于"前端定时器打的只读轮询"。

    判定三条（必须同时满足，缺一个就会把用户操作误判成噪声）：
      1. HTTP 方法必须是 GET —— 同一路径上的 POST（创建/取消任务）是用户操作；
      2. `path` 必须**精确等于**某个 pattern，或在其下**只多一个路径段**
         （`/api/requirements/tasks/{id}`）= 查单个任务，这是轮询的形态；
      3. 不能带"动作性"后缀（/cancel、/retry、/resume…）。
         这些是写操作，即便方法写成 GET 也不能放进轮询桶。

    为什么不能简单 `startswith(p + "/")`：`/api/requirements/tasks/{id}/cancel`
    也满足前缀匹配，用户点"取消"就会被塞进轮询桶、绕过业务限流。
    """
    if method.upper() != "GET":
        return False

    _ACTION_SUFFIXES = ("/cancel", "/retry", "/resume", "/rerun", "/start", "/submit", "/abort")
    low = path.rstrip("/").lower()
    for suf in _ACTION_SUFFIXES:
        if low.endswith(suf):
            return False

    for p in _POLL_PATTERNS:
        if path.rstrip("/") == p:
            return True
        # 只允许多一级：/api/requirements/tasks/{id}
        if path.startswith(p + "/"):
            rest = path[len(p) + 1:].rstrip("/")
            if rest and "/" not in rest:
                return True
    return False


# 业务桶：写操作 / 生成 / 导出等真实用户动作
_bucket = TokenBucket(limit=settings.API_RPM_LIMIT, window_seconds=settings.API_RATE_WINDOW)
# 轮询桶：单独计数，额度按"前端并发组件数 × 各自频率"给足余量。
#   默认 = 业务额度的 4 倍（60 → 240/分钟）。按最坏情况估算：
#   Layout 12 + Tasks 20 + Dashboard 6 + 上传弹窗 40 ≈ 78/分钟/标签页，
#   再留 3 倍余量给"多开几个标签页/多个用户共享同一出口 IP"。
_POLL_BUCKET_MULTIPLIER = 4
_poll_bucket = TokenBucket(
    limit=max(60, settings.API_RPM_LIMIT * _POLL_BUCKET_MULTIPLIER),
    window_seconds=settings.API_RATE_WINDOW,
)

# ⚠️ 单进程内存桶的**边界**（写在这里，避免以后误判）：
#   ① 多 worker（uvicorn --workers N）时每个进程各算一份 → 实际额度 = N × 配置值；
#   ② 多副本部署同理。本项目单 worker 单实例，故不引入 Redis；
#   ③ 生产若扩到多副本，必须换 Redis 版，否则限流形同虚设。
#   这不影响"容器化后全站共用一桶"的修复 —— 那条 bug 与 worker 数无关。


# ============================================================================
# 真实客户端 IP
# ============================================================================
# 为什么不能直接用 request.client.host（v4.1.6 实测）：
#   容器部署下请求链路是 浏览器 → nginx(frontend 容器) → backend 容器。
#   uvicorn 看到的对端恒为 Docker 网桥地址（172.18.0.x），**所有人都是同一个值**
#   → 限流退化成"全站共享一个桶"。
#   nginx 已按 `proxy_set_header X-Real-IP $remote_addr` 与
#   `X-Forwarded-For $proxy_add_x_forwarded_for` 透传，这里读出来即可。
#
# ⚠️ 安全边界：XFF 是客户端可伪造的。只有在**可信代理之后**才能采信。
#   本项目 nginx 用 `$proxy_add_x_forwarded_for` 追加而非覆盖，
#   所以取**最左**值会被客户端随意伪造（拿伪造 IP 就能白嫖新桶）。
#   正确做法是取**最右**（最接近本机的那一跳，由我们自己的 nginx 写入）。
#   若前面还有一层负载均衡，可在 .env 用 TRUSTED_PROXY_DEPTH 调整取第几跳。
def _client_ip(request: Request) -> str:
    """取真实客户端 IP（不可伪造优先）。"""
    import os

    depth = 1
    try:
        depth = max(1, int(os.environ.get("TRUSTED_PROXY_DEPTH", "1")))
    except ValueError:
        depth = 1

    xff = request.headers.get("x-forwarded-for") or ""
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    if hops:
        # 从右往左数第 depth 跳；XFF 里是我们 nginx 追加的，所以 right[-1] 最可信。
        idx = -depth if len(hops) >= depth else 0
        return hops[idx] or "unknown"

    real_ip = (request.headers.get("x-real-ip") or "").strip()
    if real_ip:
        return real_ip

    # 直连场景（本地开发 / 无 nginx）：退回对端地址
    return request.client.host if request.client else "unknown"


async def rate_limit_middleware(request: Request, call_next: Callable) -> Response:
    """限流中间件。

    对 /api/ 路径下的请求进行限流检查。
    健康检查、文档路径跳过限流。

    轮询类只读请求走独立的大桶（见 `_POLL_PATTERNS`），不与业务请求抢令牌。

    Usage:
        app.middleware("http")(rate_limit_middleware)
    """
    path = request.url.path

    if not path.startswith("/api/"):
        return await call_next(request)

    if path == "/api/health":
        return await call_next(request)

    client_ip = _client_ip(request)
    bucket = _poll_bucket if _is_poll_request(path, request.method) else _bucket

    if not await bucket.allow(client_ip):
        logger.warning(
            "[限流] 客户端 {} 请求频率超限: path={} bucket={}",
            client_ip, path, "poll" if bucket is _poll_bucket else "business",
        )
        return JSONResponse(
            status_code=429,
            content={
                "success": False,
                "message": "请求频率超限，请稍后再试",
                "data": {
                    "retry_after": bucket.next_token_wait(),
                    "limit": bucket.limit,
                    "bucket": "poll" if bucket is _poll_bucket else "business",
                },
            },
            headers={"Retry-After": str(bucket.next_token_wait())},
        )

    return await call_next(request)
