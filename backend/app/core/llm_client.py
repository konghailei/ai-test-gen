"""LLM 客户端封装。

统一封装 LLM 调用，提供故障转移、重试、断路器三大稳定性保障。
所有 Agent 通过此模块调用 LLM，避免散落的稳定性逻辑。

设计依据：docs/03-case-generation-flow.md §6.2-6.4
- §6.2 断路器（三状态机：CLOSED → OPEN → HALF_OPEN → CLOSED）
- §6.3 指数退避 + 全抖动重试（sleep = random(0, min(cap, base * 2^attempt))）
- §6.4 多提供商故障转移（连续失败3次 → 临时禁用10分钟）

技术选型：基于 LiteLLM 统一接口，所有提供商输出格式一致。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections import deque
from typing import Any

import litellm

from app.config import settings
from app.core.exceptions import (
    LLMAuthenticationError,
    LLMBreakerOpenError,
    LLMJsonParseError,
    LLMNoProviderAvailable,
    LLMRetryExhausted,
)
from app.utils.logger import logger

# LiteLLM 全局配置：关闭非必要遥测，降低噪声
litellm.set_verbose = False
litellm.suppress_debug_info = True


# ============================================================================
# 断路器（Circuit Breaker）— 对应 docs/03 §6.2
# ============================================================================

class CircuitBreaker:
    """三状态断路器。

    状态机：
        CLOSED（正常）──失败率超阈值──▶ OPEN（快速失败）
        OPEN ──冷却超时──▶ HALF_OPEN（探测）
        HALF_OPEN ──成功 N 次──▶ CLOSED
        HALF_OPEN ──失败──▶ OPEN

    LLM 特有触发条件（docs/03 §6.2）：
    - 错误率 > 20% over 60s 窗口
    - 冷却 30s 后进入半开探测
    - 半开态连续成功 3 次恢复闭合

    Note:
        本实现为单进程内存版（MVP）。分布式场景需迁移到 Redis 后端。
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_rate_threshold: float = 0.20,
        window_seconds: int = 60,
        cooldown_seconds: int = 30,
        half_open_success_threshold: int = 3,
        min_requests: int = 5,
    ) -> None:
        """初始化断路器。

        Args:
            failure_rate_threshold: 失败率阈值（0-1），超过则断开。默认 0.20 对应 docs/03 §6.2
            window_seconds: 统计窗口时长（秒）。默认 60s
            cooldown_seconds: OPEN 态冷却时长（秒）。默认 30s
            half_open_success_threshold: HALF_OPEN 态恢复 CLOSED 所需连续成功数。默认 3
            min_requests: 窗口内最少请求数，不足时不计算失败率（避免冷启动误判）
        """
        self._failure_rate_threshold = failure_rate_threshold
        self._window_seconds = window_seconds
        self._cooldown_seconds = cooldown_seconds
        self._half_open_success_threshold = half_open_success_threshold
        self._min_requests = min_requests

        self._state = self.CLOSED
        self._opened_at: float = 0.0
        self._half_open_successes = 0
        # 滑动窗口：记录 (timestamp, is_success)
        self._events: deque[tuple[float, bool]] = deque()

    @property
    def state(self) -> str:
        """当前断路器状态。"""
        return self._state

    def allow_request(self) -> bool:
        """判断当前是否允许放行请求。

        Returns:
            bool: True 表示允许调用 LLM；False 表示断路器开启，应快速失败或走降级

        State Transitions:
            CLOSED → 允许
            OPEN → 若冷却已过则转 HALF_OPEN 并允许；否则拒绝
            HALF_OPEN → 允许（探测请求）
        """
        now = time.time()
        if self._state == self.OPEN:
            if now - self._opened_at >= self._cooldown_seconds:
                self._state = self.HALF_OPEN
                self._half_open_successes = 0
                logger.warning("断路器 OPEN → HALF_OPEN，开始探测")
                return True
            return False
        return True

    def record_success(self) -> None:
        """记录一次成功调用。"""
        now = time.time()
        self._events.append((now, True))
        self._evict_expired(now)

        if self._state == self.HALF_OPEN:
            self._half_open_successes += 1
            if self._half_open_successes >= self._half_open_success_threshold:
                self._state = self.CLOSED
                self._events.clear()
                logger.info("断路器 HALF_OPEN → CLOSED，服务恢复")

    def record_failure(self) -> None:
        """记录一次失败调用。"""
        now = time.time()
        self._events.append((now, False))
        self._evict_expired(now)

        if self._state == self.HALF_OPEN:
            # 探测失败，立即重新断开
            self._trip_open(now)
            logger.warning("断路器 HALF_OPEN → OPEN，探测失败")
            return

        if self._state == self.CLOSED:
            failure_rate = self._failure_rate()
            if failure_rate is not None and failure_rate >= self._failure_rate_threshold:
                self._trip_open(now)
                logger.warning(
                    "断路器 CLOSED → OPEN，失败率 {:.0%} ≥ {}",
                    failure_rate,
                    self._failure_rate_threshold,
                )

    def _trip_open(self, now: float) -> None:
        """转入 OPEN 态。"""
        self._state = self.OPEN
        self._opened_at = now

    def _evict_expired(self, now: float) -> None:
        """清除滑动窗口外的过期事件。"""
        cutoff = now - self._window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _failure_rate(self) -> float | None:
        """计算当前窗口失败率。

        Returns:
            float | None: 失败率（0-1）；窗口内请求不足时返回 None
        """
        if len(self._events) < self._min_requests:
            return None
        failures = sum(1 for _, ok in self._events if not ok)
        return failures / len(self._events)


# ============================================================================
# LLM 客户端
# ============================================================================

class LLMClient:
    """LLM 统一客户端。

    封装 LiteLLM 调用，提供：
    1. 多提供商故障转移（主 → 备，连续失败 3 次临时禁用 10 分钟）
    2. 指数退避 + 全抖动重试（最多 5 次）
    3. 断路器保护（错误率 > 20% 降级）

    Usage:
        from app.core.llm_client import llm_client
        text = await llm_client.call("请解析以下需求...", temperature=0.3)
        data = await llm_client.call_json("返回 JSON: {...}", schema_hint={"id": str})
    """

    # 重试配置（docs/03 §6.3）
    MAX_ATTEMPTS = 5
    BASE_DELAY = 1.0
    MAX_DELAY = 60.0
    # v3.1.3 流式空闲超时（秒）：两个 chunk 之间超过该时长无数据视为链路死亡。
    # 正常生成时 chunk 间隔通常 < 30s；首响应到达后总生成时长不设上限
    # （大 prompt 可达 10-30 分钟）。
    STREAM_IDLE_TIMEOUT = 180

    # v3.3 首次响应超时（秒）默认值（N4 根治）。实际取值优先读
    # settings.LLM_FIRST_RESPONSE_TIMEOUT，便于用环境变量调整而无需改代码。
    # 语义：流式调用下「首个有效 chunk」的等待上限；正文 chunk 或思考
    # （reasoning_content）chunk 都算「有响应」。超时即判定模型异常，
    # **跳过剩余重试直接切换备用提供商**，不乘 MAX_ATTEMPTS。
    DEFAULT_FIRST_RESPONSE_TIMEOUT = 180

    # 故障转移配置（docs/03 §6.4）
    PROVIDER_FAILURE_THRESHOLD = 3
    PROVIDER_DISABLE_SECONDS = 600  # 10 分钟

    # v3.4.2：单次 call() 的总墙钟预算（秒），跨 provider × 跨重试累计。
    # 供应商全挂时应快速失败并提示更换供应商，而不是逐家等满 180s 首响应超时
    # （3 家 × 180s ≈ 9 分钟，再叠加重试可达数十分钟）。优先读
    # settings.LLM_CALL_TOTAL_BUDGET。
    DEFAULT_CALL_TOTAL_BUDGET = 240

    def __init__(self) -> None:
        """初始化客户端。"""
        self._breaker = CircuitBreaker()
        # 提供商连续失败计数：{provider_key: failures}
        self._provider_failures: dict[str, int] = {}
        # 提供商禁用截止时间：{provider_key: disabled_until_timestamp}
        self._provider_disabled_until: dict[str, float] = {}
        # 最近一次调用使用的模型名（用于错误报告）
        self._last_model: str = ""
        # 优化 4：LLM 响应缓存（进程内 LRU，仅缓存 temperature=0 的确定性调用）
        # v3.1.4 修复报告 §8.7：缓存条目带时间戳 + TTL 过期（防止跨轮次拿到陈旧响应）；
        # key 中加入 agent_name，不同 Agent 即使 prompt 文本相同也互不串缓存。
        # 注意：缓存键已包含完整 prompt+system_prompt，需求文本/KB 上下文不同则键必不同，
        # 跨需求命中仅在 prompt 完全一致（确定性等价输入）时发生，语义安全。
        self._response_cache: dict[str, tuple[str, float]] = {}
        self._CACHE_MAX_SIZE = 100
        self._CACHE_TTL_SECONDS = 600  # 10 分钟：覆盖单次流水线时长即可，避免陈旧响应长期存活

    def _cache_get(self, key: str) -> str | None:
        """读取缓存条目，带 TTL 过期检查；过期即删除并返回 None。"""
        entry = self._response_cache.get(key)
        if entry is None:
            return None
        content, ts = entry
        if time.time() - ts > self._CACHE_TTL_SECONDS:
            self._response_cache.pop(key, None)
            return None
        return content

    def _cache_put(self, key: str, content: str) -> None:
        """写入缓存；先清理过期条目，再按 FIFO 淘汰超限条目。"""
        if len(self._response_cache) >= self._CACHE_MAX_SIZE:
            # 先尝试清过期（腾位而不误杀活跃条目）
            now = time.time()
            expired = [k for k, (_, ts) in self._response_cache.items()
                       if now - ts > self._CACHE_TTL_SECONDS]
            for k in expired:
                self._response_cache.pop(k, None)
        while len(self._response_cache) >= self._CACHE_MAX_SIZE:
            self._response_cache.pop(next(iter(self._response_cache)))
        self._response_cache[key] = (content, time.time())

    async def call(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.3,
        response_format: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        provider: str | None = None,
        agent_name: str = "",
        return_usage: bool = False,
        _skip_log: bool = False,
    ) -> str | tuple:
        """调用 LLM 生成文本（核心入口）。

    支持多供应商故障转移、响应缓存、限流、重试与可观测性日志。
    返回 (content, usage, model_used, provider_key) 或仅 content（依 return_usage）。
        """
        """调用 LLM 生成文本（主入口）。

        Args:
            prompt: 用户提示词
            system_prompt: 系统指令（可选）。若提供则作为 system message
            temperature: 采样温度，0-1。默认 0.3（偏稳定）
            response_format: 响应格式约束。"json" 表示强制 JSON 输出；None 表示自由文本
            model: 指定模型名（覆盖配置）。默认用 settings 中的模型
            max_tokens: 最大生成 token 数。None 表示不限
            provider: 指定供应商名称（如 "deepseek"/"openai"）。若指定且可用，
                      仅使用该供应商；不可用时回退到正常候选列表。
            agent_name: 调用方 Agent 名称，用于 LLM 调用日志（仅直接调用 call 时记录；
                        call_json 会自行记录并传 _skip_log=True 避免重复）。
            return_usage: True 时返回 (content, usage_dict, model_used, provider_key) 四元组，
                          供 call_json 获取 token 用量；默认 False 返回 content 字符串。
            _skip_log: True 时跳过本方法的日志记录（由调用方 call_json 统一记录）。

        Returns:
            str | tuple: 默认返回 LLM 生成的文本；return_usage=True 时返回
                         (文本, token用量字典, 模型名, 供应商 key) 元组。

        Note（v3.4.1 修复 P-7）:
            本方法参数 provider 会被下方 `for provider in providers` 循环变量遮蔽
            （循环内它是 dict）。循环体内请勿把 provider 当作入参字符串使用。修正
            docstring 与实际返回值不一致的问题（原记 3 元，实际 4 元）。

        Raises:
            RuntimeError: 所有提供商均不可用或重试耗尽

        Note:
            故障转移顺序：主提供商 -> 备用提供商（若配置）。
            每个提供商内部执行全抖动重试；提供商间为故障转移关系。
        """
        # 断路器检查
        if not self._breaker.allow_request():
            err = LLMBreakerOpenError("LLM 断路器开启，服务暂不可用（错误率超阈值）")
            if not _skip_log:
                await self._log_call(
                    agent_name, system_prompt or "", prompt, "", None,
                    temperature, 0, {}, False, str(err)[:500],
                )
            raise err

        # 优化 4：LLM 响应缓存（仅缓存 temperature=0 的确定性调用）
        import hashlib
        # v3.4.1 修复 M-14：key 补入 response_format / max_tokens——此前 call（自由文本）
        # 与 call_json（JSON 约束）在同一 prompt、同 agent 下共用同一缓存键，
        # temperature=0 时互相污染。
        cache_key = hashlib.md5(
            f"{agent_name}|{prompt}|{system_prompt or ''}|{temperature}|{model or ''}"
            f"|{response_format or ''}|{max_tokens or ''}".encode()
        ).hexdigest()
        cached = self._cache_get(cache_key) if temperature == 0 else None
        if cached is not None:
            logger.debug("[LLM] 缓存命中: key={}", cache_key[:12])
            # 缓存命中非真实 LLM 调用，不写日志
            if return_usage:
                return cached, {}, "", ""
            return cached

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # 构建候选提供商列表（主 -> 备）
        providers = self._candidate_providers(model, provider=provider)
        if not providers:
            # v3.4.2：区分"一家都没配"与"全都被禁用"——后者是典型的供应商全挂，
            # 必须给出"更换供应商"的明确指引（此前只报"未配置 API Key"，误导排查方向）。
            now_ts = time.time()
            disabled = [
                k for k, until in self._provider_disabled_until.items() if until > now_ts
            ]
            if disabled:
                err = LLMNoProviderAvailable(
                    f"所有 LLM 提供商均不可用（{len(disabled)} 家处于禁用期："
                    f"{'、'.join(disabled[:4])}）。"
                    "请到「系统设置 → LLM 供应商」更换可用供应商，或检查 API Key / 额度。"
                )
            else:
                err = LLMNoProviderAvailable(
                    "无可用 LLM 提供商（未配置 API Key）。"
                    "请到「系统设置 → LLM 供应商」添加并启用供应商。"
                )
            if not _skip_log:
                await self._log_call(
                    agent_name, system_prompt or "", prompt, "", None,
                    temperature, 0, {}, False, str(err)[:500],
                )
            raise err

        start_ts = time.time()
        # v3.4.2：单次调用的总墙钟预算（跨 provider × 跨重试累计）。
        # 供应商全挂时立即失败并提示更换供应商，不再逐家等满 180s 首响应超时。
        budget = int(getattr(settings, "LLM_CALL_TOTAL_BUDGET", 0) or self.DEFAULT_CALL_TOTAL_BUDGET)
        deadline = start_ts + budget
        last_error: Exception | None = None
        last_provider_key = ""
        failure_notes: list[str] = []
        attempted = 0

        for prov in providers:
            remaining = deadline - time.time()
            if remaining <= 3:
                logger.warning(
                    "[LLM] 调用总预算 {}s 已耗尽，跳过剩余 {} 家提供商（已尝试 {} 家）",
                    budget, len(providers) - attempted, attempted,
                )
                break
            attempted += 1
            last_provider_key = prov["key"]
            try:
                content, usage, model_used = await self._call_with_retry(
                    prov, messages, temperature, response_format, max_tokens, deadline=deadline
                )
                self._breaker.record_success()
                self._provider_failures.pop(prov["key"], None)
                # 优化 4：写入缓存（仅 temperature=0 的确定性调用）
                if temperature == 0:
                    self._cache_put(cache_key, content)
                    logger.debug("[LLM] 缓存写入: key={}", cache_key[:12])
                duration_ms = int((time.time() - start_ts) * 1000)
                if not _skip_log:
                    await self._log_call(
                        agent_name, system_prompt or "", prompt, content, None,
                        temperature, duration_ms, usage, True, "",
                        model_used=model_used,
                        provider_used=prov["key"],
                    )
                if return_usage:
                    return content, usage, model_used, prov["key"]
                return content
            except _FirstResponseTimeoutError as e:
                # v3.3（N4 根治）：模型在首响应预算内无任何输出（含思考内容）
                # → 直接切换备用提供商，不重试同一模型
                last_error = e
                self._record_provider_failure(prov)
                failure_notes.append(f"{prov['key']} 首响应超时")
                logger.warning(
                    "提供商 {} 首响应超时，切换备用: {}",
                    prov["key"],
                    str(e)[:200],
                )
            except _RetryableError as e:
                # 重试耗尽 → 记录失败，尝试下一提供商
                last_error = e
                self._record_provider_failure(prov)
                failure_notes.append(f"{prov['key']} 重试耗尽")
                logger.warning(
                    "提供商 {} 重试耗尽，尝试故障转移: {}",
                    prov["key"],
                    str(e)[:200],
                )
            except _NonRetryableError as e:
                # 4xx 等不可重试错误 → 直接故障转移（docs/03 §6.3：4xx 不重试）
                # v3.4.2：额度耗尽/认证失败属"硬失败"，立即禁用该提供商（不等累计 3 次），
                # 使当前调用马上换下一家、后续调用直接跳过它。
                last_error = e
                self._disable_provider_now(prov, reason=str(e)[:80])
                failure_notes.append(f"{prov['key']} {str(e)[:60]}")
                logger.warning("提供商 {} 不可重试错误，故障转移: {}", prov["key"], str(e)[:200])

        # 所有提供商均失败（或总预算耗尽）
        self._breaker.record_failure()
        duration_ms = int((time.time() - start_ts) * 1000)
        detail = "；".join(failure_notes[:4]) if failure_notes else str(last_error or "")
        hint = "请到「系统设置 → LLM 供应商」更换可用供应商，或检查 API Key / 额度。"
        if attempted == 0:
            err = LLMNoProviderAvailable(
                f"所有 LLM 提供商均不可用（候选 {len(providers)} 家当前均处于禁用期）。{hint}"
            )
        else:
            err = LLMNoProviderAvailable(
                f"所有 LLM 提供商均不可用（已尝试 {attempted} 家，耗时 {duration_ms // 1000}s）。{hint}"
                + (f" 明细：{detail}" if detail else "")
            )
        if not _skip_log:
            await self._log_call(
                agent_name, system_prompt or "", prompt, "", None,
                temperature, duration_ms, {}, False, str(err)[:500],
                provider_used=last_provider_key,
            )
        raise err

    async def call_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        schema_hint: dict | None = None,
        temperature: float = 0.2,
        model: str | None = None,
        max_tokens: int | None = None,
        provider: str | None = None,
        allow_array: bool = False,
        agent_name: str = "",
        task_id: str = "",
        req_id: str = "",
    ) -> dict | list:
        """调用 LLM 并以容错方式解析 JSON（call + _parse_json）。

    返回解析后的 dict/list；解析失败时抛 LLMJsonParseError。
        """
        """调用 LLM 并解析为 JSON 对象或数组。

        Args:
            allow_array: True 时不启用 response_format="json"（避免 json_object 约束
                         阻止 LLM 输出 JSON 数组）。调用方 prompt 应自行约束输出格式。
            agent_name: 调用方 Agent 名称（如 ReqAgent/ClarifyAgent/GenAgent/ReviewAgent/FpAgent），
                        用于 LLM 调用日志。不传则记为 "unknown"。
            task_id: 关联的异步任务 ID（可选）。
            req_id: 关联的需求 ID（可选）。

        Note:
            日志记录在 call + _parse_json 两步完成后统一写入 llm_call_logs，
            通过 _skip_log=True 避免 call 内部重复记录。日志失败不阻断主流程。
        """
        formatted_prompt = prompt
        if schema_hint:
            keys = ", ".join(schema_hint.keys())
            formatted_prompt += f"\n\n请严格返回 JSON 对象，包含以下字段：{keys}。不要返回任何额外文本。"
        elif allow_array:
            pass  # 不追加格式约束，由调用方 prompt 控制
        else:
            formatted_prompt += "\n\n请严格返回 JSON，不要返回任何额外文本。"

        start_ts = time.time()
        text = ""
        usage: dict = {}
        model_used = ""
        provider_key = ""
        parsed: Any = None
        try:
            text, usage, model_used, provider_key = await self.call(
                formatted_prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                response_format=None if allow_array else "json",
                model=model,
                max_tokens=max_tokens,
                provider=provider,
                agent_name=agent_name,
                return_usage=True,
                _skip_log=True,
            )
            parsed = self._parse_json(text, schema_hint)
            duration_ms = int((time.time() - start_ts) * 1000)
            await self._log_call(
                agent_name or "unknown", system_prompt or "", formatted_prompt, text,
                parsed, temperature, duration_ms, usage, True, "",
                task_id=task_id, req_id=req_id, model_used=model_used,
                provider_used=provider_key,
            )
            return parsed
        except Exception as e:
            duration_ms = int((time.time() - start_ts) * 1000)
            await self._log_call(
                agent_name or "unknown", system_prompt or "", formatted_prompt, text,
                parsed, temperature, duration_ms, usage, False, str(e)[:500],
                task_id=task_id, req_id=req_id, model_used=model_used,
                provider_used=provider_key,
            )
            raise

    async def call_vision(
        self,
        image_base64: str,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        provider: str | None = None,
        mime_type: str = "image/png",
    ) -> str:
        """调用视觉模型（多模态输入）。
        """
        """调用多模态 LLM 识别图片内容（流程图/架构图/表格图等）。

        v2.18: 新增多模态能力。图片以 base64 编码传入，LLM 返回结构化文本描述。
        支持的供应商需提供视觉模型（如 qwen-vl-max、gpt-4o、claude-3-5-sonnet）。
        若未配置视觉供应商，回退到主 LLM（需主模型支持视觉，否则会报错）。

        Args:
            image_base64: 图片的 base64 编码字符串（不含 data: 前缀）
            prompt: 图片识别提示词（如"请描述这张流程图的内容..."）
            system_prompt: 系统指令（可选）
            temperature: 采样温度，默认 0.2（偏稳定）
            max_tokens: 最大生成 token 数
            provider: 指定供应商名称。None 时使用视觉专用供应商（若配置）或主供应商
            mime_type: 图片 MIME 类型，默认 image/png

        Returns:
            str: LLM 生成的图片内容描述

        Raises:
            LLMNoProviderAvailable: 无可用视觉供应商
            RuntimeError: 调用失败
        """
        if not self._breaker.allow_request():
            raise LLMBreakerOpenError("LLM 断路器开启，服务暂不可用（错误率超阈值）")

        # 构造多模态消息（OpenAI Vision API 格式）
        data_url = f"data:{mime_type};base64,{image_base64}"
        content: list[dict[str, Any]] = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": content})

        # 构建候选提供商列表
        providers = self._candidate_providers(None, provider=provider)
        if not providers:
            raise LLMNoProviderAvailable("无可用多模态 LLM 提供商（未配置 API Key）")

        last_error: Exception | None = None
        for prov in providers:
            try:
                result = await self._call_vision_with_retry(
                    prov, messages, temperature, max_tokens
                )
                self._breaker.record_success()
                self._provider_failures.pop(prov["key"], None)
                return result
            except _RetryableError as e:
                last_error = e
                self._record_provider_failure(prov)
                logger.warning(
                    "视觉提供商 {} 重试耗尽，尝试故障转移: {}",
                    prov["key"],
                    str(e)[:200],
                )
            except _NonRetryableError as e:
                last_error = e
                self._record_provider_failure(prov)
                logger.warning("视觉提供商 {} 不可重试错误，故障转移: {}", prov["key"], str(e)[:200])

        self._breaker.record_failure()
        raise LLMNoProviderAvailable(f"所有多模态 LLM 提供商均不可用: {last_error}")

    async def _call_vision_with_retry(
        self,
        provider: dict,
        messages: list[dict],
        temperature: float,
        max_tokens: int | None,
    ) -> str:
        """对单个视觉提供商执行全抖动重试调用（多模态格式）。"""
        last_error: Exception | None = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": provider["model"],
                    "messages": messages,
                    "api_key": provider["api_key"],
                    "api_base": provider["base_url"],
                    "custom_llm_provider": "openai",
                    "temperature": temperature,
                    "timeout": settings.LLM_TIMEOUT,
                }
                # 视觉调用不强制 json 格式，让模型自由描述图片内容
                if max_tokens:
                    kwargs["max_tokens"] = max_tokens
                else:
                    kwargs["max_tokens"] = 4096

                # v2.37: 思考模式开关 — 优先使用自定义参数，否则使用内置规则
                if provider.get("enable_thinking"):
                    custom_params = self._parse_thinking_params(provider.get("thinking_params", ""))
                    if custom_params:
                        kwargs.update(custom_params)
                    else:
                        thinking_kwargs = self._build_thinking_kwargs(provider["model"])
                        kwargs.update(thinking_kwargs)

                # 多模态卡死防御（同 838 修复）
                import asyncio as _aio_llm_v
                try:
                    response = await _aio_llm_v.wait_for(
                        _aio_llm_v.to_thread(lambda: _aio_llm_v.run(litellm.acompletion(**kwargs))),
                        timeout=provider.get("timeout", 300),
                    )
                except (AttributeError, NotImplementedError, RuntimeError):
                    response = await _aio_llm_v.wait_for(litellm.acompletion(**kwargs), timeout=provider.get("timeout", 300))
                content = response.choices[0].message.content
                if not content:
                    raise _RetryableError("多模态 LLM 返回空内容")
                logger.debug("多模态 LLM 调用成功 [provider={}, attempt={}]", provider["key"], attempt)
                return content

            except _NonRetryableError:
                raise
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                if self._is_non_retryable(e, err_str):
                    raise _NonRetryableError(str(e)) from e
                if attempt < self.MAX_ATTEMPTS:
                    delay = self._jitter_delay(attempt)
                    logger.debug(
                        "多模态 LLM 调用失败 [attempt={}/{}], {} 后重试: {}",
                        attempt,
                        self.MAX_ATTEMPTS,
                        f"{delay:.2f}s",
                        str(e)[:120],
                    )
                    await asyncio.sleep(delay)

        raise _RetryableError(f"重试 {self.MAX_ATTEMPTS} 次后仍失败: {last_error}")

    # ========================================================================
    # 内部实现
    # ========================================================================

    @staticmethod
    def _normalize_usage(usage_obj) -> dict:
        """归一化 usage 字典（P1-2）：标准三字段 + 供应商侧 prompt cache 命中观测。

        缓存字段随供应商不同：
        - OpenAI 兼容（dashscope/qwen、deepseek 等）: prompt_tokens_details.cached_tokens
        - Anthropic: cache_read_input_tokens / cache_creation_input_tokens
        - litellm 统一透传: prompt_cache_hit_tokens
        前置条件：system prompt 字节级稳定（提示词管理保证）+ 工具定义顺序固定，
        否则前缀缓存全量失效——动态内容一律放 user 消息（本书 2.3 原则）。
        """
        if usage_obj is None:
            return {}
        u = {
            "prompt_tokens": getattr(usage_obj, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage_obj, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage_obj, "total_tokens", 0) or 0,
        }
        for attr in ("cache_read_input_tokens", "cache_creation_input_tokens", "prompt_cache_hit_tokens"):
            v = getattr(usage_obj, attr, None)
            if v:
                u["cache_hit_tokens"] = v
                break
        details = getattr(usage_obj, "prompt_tokens_details", None)
        if details is not None and "cache_hit_tokens" not in u:
            cached = None
            if isinstance(details, dict):
                cached = details.get("cached_tokens")
            else:
                cached = getattr(details, "cached_tokens", None)
            if cached:
                u["cache_hit_tokens"] = cached
        return u

    def _candidate_providers(self, model: str | None, provider: str | None = None) -> list[dict]:
        """构建候选提供商列表（跳过被禁用的）。

        优先使用 settings._all_provider_configs（多供应商持久化配置），
        回退到 settings.llm_config（传统 deepseek/openai 单例）。

        Args:
            model: 指定模型（若提供则只用主提供商）
            provider: 指定供应商名称。若指定且可用，仅返回该供应商配置；
                      不可用时回退到正常候选列表。

        Returns:
            list[dict]: 可用提供商配置列表，每项含 key/api_key/base_url/model
        """
        now = time.time()

        # 优先使用多供应商配置
        all_configs: dict = getattr(settings, "_all_provider_configs", {})
        primary_provider = getattr(settings, "LLM_PROVIDER", "deepseek")

        # 若指定了 provider，优先返回该供应商，并追加其他可用供应商作为故障转移候选
        # v2.27: 旧版指定 provider 时仅返回该供应商，若其调用失败（如 403/额度耗尽）
        # 无故障转移目标，直接报"所有 LLM 提供商均不可用"。现改为追加备用供应商，
        # 确保指定供应商失败后能自动回退到其他可用供应商。
        if provider and all_configs and provider in all_configs:
            cfg = all_configs[provider]
            if cfg.get("api_key"):
                key = f"{provider}:{cfg.get('model', '')}"
                if self._provider_disabled_until.get(key, 0) < now:
                    primary_candidate = {
                        "key": key,
                        "api_key": cfg["api_key"],
                        "base_url": cfg.get("base_url", ""),
                        "model": model or cfg.get("model", ""),
                        "enable_thinking": cfg.get("enable_thinking", False),
                        "thinking_params": cfg.get("thinking_params", ""),
                    }
                    # 追加备用供应商作为故障转移候选（仅当未指定 model 时）
                    backup_candidates: list[dict] = []
                    if not model:
                        for p_name, p_cfg in all_configs.items():
                            if p_name == provider:
                                continue
                            if not p_cfg.get("api_key"):
                                continue
                            b_key = f"{p_name}:{p_cfg.get('model', '')}"
                            if self._provider_disabled_until.get(b_key, 0) < now:
                                backup_candidates.append({
                                    "key": b_key,
                                    "api_key": p_cfg["api_key"],
                                    "base_url": p_cfg.get("base_url", ""),
                                    "model": p_cfg.get("model", ""),
                                    "enable_thinking": p_cfg.get("enable_thinking", False),
                                    "thinking_params": p_cfg.get("thinking_params", ""),
                                })
                    return [primary_candidate] + backup_candidates
            # 指定 provider 不可用（被禁用或无 api_key）-> 回退到正常逻辑（不 return）

        candidates: list[dict] = []

        if all_configs:
            # 主供应商
            if primary_provider in all_configs:
                cfg = all_configs[primary_provider]
                if cfg.get("api_key"):
                    key = f"{primary_provider}:{cfg.get('model', '')}"
                    if self._provider_disabled_until.get(key, 0) < now:
                        candidates.append({
                            "key": key,
                            "api_key": cfg["api_key"],
                            "base_url": cfg.get("base_url", ""),
                            "model": model or cfg.get("model", ""),
                            "enable_thinking": cfg.get("enable_thinking", False),
                            "thinking_params": cfg.get("thinking_params", ""),
                        })

            # 备用供应商（仅当没有指定 model 时）
            if not model:
                for p_name, p_cfg in all_configs.items():
                    if p_name == primary_provider:
                        continue  # 主供应商已添加
                    if not p_cfg.get("api_key"):
                        continue
                    key = f"{p_name}:{p_cfg.get('model', '')}"
                    if self._provider_disabled_until.get(key, 0) < now:
                        candidates.append({
                            "key": key,
                            "api_key": p_cfg["api_key"],
                            "base_url": p_cfg.get("base_url", ""),
                            "model": p_cfg.get("model", ""),
                            "enable_thinking": p_cfg.get("enable_thinking", False),
                            "thinking_params": p_cfg.get("thinking_params", ""),
                        })

        # 回退到传统单例模式
        if not candidates:
            primary = settings.llm_config
            fallback = settings.fallback_llm_config

            # 主提供商
            if primary["api_key"]:
                primary_key = f"{settings.LLM_PROVIDER}:{primary['model']}"
                if self._provider_disabled_until.get(primary_key, 0) < now:
                    candidates.append({
                        "key": primary_key,
                        "api_key": primary["api_key"],
                        "base_url": primary["base_url"],
                        "model": model or primary["model"],
                        "enable_thinking": False,  # 传统单例模式不支持思考开关
                        "thinking_params": "",     # 传统单例模式不支持自定义参数
                    })

            # 备用提供商
            if fallback and fallback["api_key"] and not model:
                fallback_provider = "openai" if settings.LLM_PROVIDER == "deepseek" else "deepseek"
                fallback_key = f"{fallback_provider}:{fallback['model']}"
                if self._provider_disabled_until.get(fallback_key, 0) < now:
                    candidates.append({
                        "key": fallback_key,
                        "api_key": fallback["api_key"],
                        "base_url": fallback["base_url"],
                        "model": fallback["model"],
                        "enable_thinking": False,  # 传统单例模式不支持思考开关
                        "thinking_params": "",     # 传统单例模式不支持自定义参数
                    })

        return candidates

    @staticmethod
    def _parse_thinking_params(thinking_params: str) -> dict[str, Any]:
        """v2.37: 解析自定义思考参数 JSON 字符串。

        支持用户在设置页面输入自定义思考参数，灵活接入各种思考模式。
        参数格式示例：
            {"thinking": {"type": "enabled"}}                    # 腾讯混元/GLM/DeepSeek-V4
            {"thinking": {"type": "enabled", "budget_tokens": 8000}}  # Claude
            {"enable_thinking": true}                             # Qwen3
            {"reasoning_effort": "high"}                          # OpenAI o 系列

        Args:
            thinking_params: JSON 字符串（可能为空或非法）

        Returns:
            dict: 解析后的参数字典（空字典表示无自定义参数或解析失败）
        """
        if not thinking_params or not thinking_params.strip():
            return {}
        try:
            params = json.loads(thinking_params.strip())
            if isinstance(params, dict):
                return params
            logger.warning("[LLMClient] 自定义思考参数非 JSON 对象: {}", thinking_params[:80])
            return {}
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("[LLMClient] 自定义思考参数 JSON 解析失败: {} | 原文: {}",
                           str(e), thinking_params[:80])
            return {}

    @staticmethod
    def _build_thinking_kwargs(model: str) -> dict[str, Any]:
        """v2.36: 根据模型名称构建思考模式参数。

        不同供应商/模型对思考模式的参数支持不同（参考腾讯云 TokenHub 官方文档）：
        - DeepSeek R1 系列 / QwQ / reasoner：模型本身即推理模型，无需额外参数
        - 腾讯混元 Hy3 / Hy3-preview：支持 thinking={"type": "enabled"}（默认 disabled）
        - GLM 4.5+ / GLM-Z1 / GLM-5 系列：支持 thinking={"type": "enabled"}
        - Qwen3 系列（非推理型号）：支持 enable_thinking=True
        - Claude 3.5+ / Claude 4：支持 thinking={"type": "enabled", "budget_tokens": N}

        TokenHub 平台统一采用 OpenAI 兼容的 thinking 参数格式：
            开启：{"thinking": {"type": "enabled"}}
            关闭：{"thinking": {"type": "disabled"}}

        策略：对已知支持思考的模型系列返回相应参数，由 litellm 负责转换。
        对不支持思考的模型传参不会报错（会被忽略），但为避免歧义，
        此方法仅对已知支持思考的模型系列返回参数。

        Args:
            model: 模型名称（如 "hy3", "glm-4.5", "qwen3-reasoning", "deepseek-r1"）

        Returns:
            dict: 思考模式参数字典（空字典表示该模型无需额外参数）
        """
        model_lower = (model or "").lower()

        # DeepSeek R1 系列 / QwQ / reasoner：模型本身即推理模型，无需额外参数
        if "r1" in model_lower or "qwq" in model_lower or "reasoner" in model_lower:
            return {}

        # 腾讯混元 Hy3 / Hy3-preview：支持 thinking 参数（默认 disabled，需显式开启）
        if "hy3" in model_lower or "hunyuan" in model_lower:
            return {"thinking": {"type": "enabled"}}

        # GLM 4.5+ / GLM-Z1 / GLM-5 系列：支持 thinking 参数
        if "glm" in model_lower and (
            "4.5" in model_lower or "z1" in model_lower
            or "-thinking" in model_lower
            or "glm-5" in model_lower or "glm5" in model_lower
        ):
            return {"thinking": {"type": "enabled"}}

        # Qwen3 系列（非推理型号）：支持 enable_thinking 参数
        if "qwen3" in model_lower and "reasoning" not in model_lower and "-reasoner" not in model_lower:
            return {"enable_thinking": True}

        # Claude 3.5+ / Claude 4：支持 extended thinking
        if "claude" in model_lower and ("3-5" in model_lower or "3.5" in model_lower or "-4" in model_lower):
            return {"thinking": {"type": "enabled", "budget_tokens": 10000}}

        # 其他模型：不传思考参数（避免不支持的模型报错）
        return {}

    async def _call_with_retry(
        self,
        provider: dict,
        messages: list[dict[str, str]],
        temperature: float,
        response_format: str | None,
        max_tokens: int | None,
        deadline: float | None = None,
    ) -> str:
        """对单个提供商执行全抖动重试调用。

        Args:
            provider: 提供商配置
            messages: 消息列表
            temperature: 采样温度
            response_format: "json" 或 None
            max_tokens: 最大 token 数
            deadline: 调用方给定的总预算截止时间戳（time.time() 口径）。
                v3.4.2：到点即不再重试、且首响应等待不超过剩余预算，
                避免供应商全挂时单次调用悬挂数十分钟。

        Returns:
            tuple[str, dict, str]: (生成文本, token 使用量字典, 实际使用的模型名)

        Raises:
            _RetryableError: 重试耗尽（可故障转移）
            _NonRetryableError: 不可重试错误（4xx）
            _FirstResponseTimeoutError: 首响应预算内无任何输出（含思考内容），
                不重试，直接由调用方切换备用提供商
        """
        last_error: Exception | None = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            # v3.4.2：调用方总预算耗尽则立即停止重试（避免"全挂"场景把时间耗在重试上）
            if deadline is not None and time.time() >= deadline - 2:
                raise _RetryableError(
                    f"LLM 调用总预算耗尽（第 {attempt}/{self.MAX_ATTEMPTS} 次尝试前）: {last_error}"
                )
            try:
                kwargs: dict[str, Any] = {
                    "model": provider["model"],
                    "messages": messages,
                    "api_key": provider["api_key"],
                    "api_base": provider["base_url"],
                    "custom_llm_provider": "openai",
                    "temperature": temperature,
                    "timeout": settings.LLM_TIMEOUT,
                }
                if response_format == "json":
                    kwargs["response_format"] = {"type": "json_object"}
                if max_tokens:
                    kwargs["max_tokens"] = max_tokens
                else:
                    # v2.24: 默认 max_tokens=32768，防止长 SRR JSON 输出被截断
                    # （16384 token 约 32K-49K 字符，部分复杂 SRR 会超出导致 JSON 解析失败）
                    kwargs["max_tokens"] = 32768

                # v2.37: 思考模式开关 — 优先使用自定义参数，否则使用内置规则
                if provider.get("enable_thinking"):
                    custom_params = self._parse_thinking_params(provider.get("thinking_params", ""))
                    if custom_params:
                        # 用户自定义参数优先
                        kwargs.update(custom_params)
                    else:
                        # 回退到内置规则
                        thinking_kwargs = self._build_thinking_kwargs(provider["model"])
                        kwargs.update(thinking_kwargs)

                # v3.1.3 流式优先；v3.3 首响应超时固定化（N4 根治）：
                # 非流式整体响应要求服务端完整生成全部输出后才返回，大 prompt（116K 字符）
                # + 32K max_tokens 的总生成时长可达 10-30 分钟，任何固定总时长超时（如 300s）
                # 都会在生成完成前掐断连接——表现为任务长期 running、API 平台无完成调用记录、
                # 固定 5 分钟间隔反复超时重试。流式模式下超时语义拆成两段：
                # - 首响应等待（连接 + prefill）: 固定 LLM_FIRST_RESPONSE_TIMEOUT（默认 180s）。
                #   本项目所有调用均为流式，**180s 内收到任意响应（正文或思考内容）即视为
                #   模型存活**；超时即判定模型异常 → 跳过剩余重试直接切换备用提供商。
                #   原实现按 prompt 长度把该预算放宽到 1800s，使 prefill 停滞可挂起 30 分钟。
                # - 首响应到达后：仅施加空闲超时（STREAM_IDLE_TIMEOUT，无新数据=链路死亡），
                #   总生成时长不设上限（大 prompt 可达 10-30 分钟，属正常）。
                import asyncio as _aio_llm
                _prompt_len = sum(
                    len(m.get("content", "") or "") if isinstance(m, dict) else 0
                    for m in messages
                )
                _base_timeout = provider.get("timeout", 300)
                _first_response_timeout = int(
                    getattr(settings, "LLM_FIRST_RESPONSE_TIMEOUT", 0)
                    or self.DEFAULT_FIRST_RESPONSE_TIMEOUT
                )
                # v3.4.2：首响应等待也不得超过调用总预算的剩余时间——
                # 供应商全挂时避免"每家都等满 180s"把单次调用拖到数十分钟。
                if deadline is not None:
                    _remain = int(deadline - time.time())
                    if _remain > 0:
                        _first_response_timeout = min(_first_response_timeout, _remain)
                # 非流式回退专用预算：该路径没有中间 chunk 信号，只能按「总时长」兜底，
                # 故仍按 prompt 长度放宽（上限 1800s），避免长输出被误杀。
                _fallback_total_timeout = _base_timeout
                if _prompt_len > 30000:
                    _fallback_total_timeout = max(
                        _base_timeout, min(1800, 300 + _prompt_len // 100)
                    )

                content = ""
                usage: dict = {}
                _finish_reason = None
                _stream_done = False
                _gen = None
                _got_first_response = False
                try:
                    _stream_kwargs = dict(kwargs)
                    _stream_kwargs["stream"] = True
                    _stream_kwargs["stream_options"] = {"include_usage": True}
                    # v3.1.3 修复：await litellm.acompletion(stream=True) 取得流式对象本身
                    # 会等待服务端 TTFB（首 token 前的 prefill）。大 prompt（如 158K 复核
                    # prompt）prefill 极慢，若提供商短暂停滞，该 await 若无超时保护会永久
                    # 挂起——表现为任务长期 running、无任何 LLM 日志/心跳/进度、重试机制
                    # 永不触发（用户的"卡死/死循环"）。此处施加与下方 __anext__ 首响应
                    # 完全一致的超时预算，确保 TTFB 停滞最坏在预算内触发故障转移。
                    _gen = await _aio_llm.wait_for(
                        litellm.acompletion(**_stream_kwargs),
                        timeout=_first_response_timeout,
                    )
                    _parts: list[str] = []
                    _t_stream_start = time.time()
                    _t_first_response: float | None = None
                    _last_progress_log = time.time()
                    while True:
                        try:
                            _chunk = await _aio_llm.wait_for(
                                _gen.__anext__(),
                                timeout=(
                                    self.STREAM_IDLE_TIMEOUT if _got_first_response
                                    else _first_response_timeout
                                ),
                            )
                        except StopAsyncIteration:
                            break
                        # include_usage 模式下最后一个 chunk 携带累计用量
                        if getattr(_chunk, "usage", None):
                            usage = self._normalize_usage(_chunk.usage)
                        _piece = None
                        _think_piece = None
                        if getattr(_chunk, "choices", None):
                            _delta = getattr(_chunk.choices[0], "delta", None)
                            if _delta is not None:
                                _piece = getattr(_delta, "content", None)
                                # v3.3：思考/推理内容同样算「模型有响应」——推理模型
                                # 常先吐 reasoning_content 再吐正文。若不计数，一次
                                # 纯思考流会被误判为「无响应」并在首响应预算到期时
                                # 被切换掉（也可能反复空等到超时）。
                                _think_piece = getattr(_delta, "reasoning_content", None) or getattr(
                                    _delta, "reasoning", None
                                )
                                if not _think_piece:
                                    _extra = getattr(_delta, "model_extra", None)
                                    if isinstance(_extra, dict):
                                        _think_piece = _extra.get("reasoning_content")
                            if _piece:
                                _parts.append(_piece)
                            _fr = getattr(_chunk.choices[0], "finish_reason", None)
                            if _fr:
                                _finish_reason = _fr
                        # 首个有效响应（正文或思考）到达 → 转入空闲超时语义
                        if not _got_first_response and (_piece or _think_piece):
                            _got_first_response = True
                            _t_first_response = time.time()
                            logger.debug(
                                "[LLM] 首响应到达: provider={}, ttfb={:.1f}s, kind={}",
                                provider["key"],
                                _t_first_response - _t_stream_start,
                                "content" if _piece else "thinking",
                            )
                        # 心跳日志：证明生成仍在进行，避免"看起来没调 LLM"的误判
                        if time.time() - _last_progress_log > 30:
                            logger.debug(
                                "[LLM] 流式生成中: provider={}, chars={}, elapsed={:.0f}s",
                                provider["key"], sum(map(len, _parts)),
                                time.time() - _t_stream_start,
                            )
                            _last_progress_log = time.time()
                    content = "".join(_parts)
                    _stream_done = True
                    logger.debug(
                        "LLM 流式调用成功 [provider={}, attempt={}, chars={}, elapsed={:.0f}s, ttfb={}]",
                        provider["key"], attempt, len(content),
                        time.time() - _t_stream_start,
                        "-" if _t_first_response is None
                        else f"{_t_first_response - _t_stream_start:.1f}s",
                    )
                except Exception as _stream_err:
                    # 清理未消费完的流，避免连接泄漏
                    try:
                        if _gen is not None:
                            await _gen.aclose()
                    except Exception:
                        pass
                    # v3.3 首响应超时：预算内没有收到任何响应（含思考内容）→ 判定该模型
                    # 异常，直接上抛专用异常，**跳过剩余重试**交给上层切换备用提供商。
                    # 重试同一个"完全不吐字"的模型无意义，只会把挂起时间乘以 MAX_ATTEMPTS。
                    if (
                        isinstance(_stream_err, (TimeoutError, asyncio.TimeoutError))
                        and not _got_first_response
                    ):
                        logger.warning(
                            "[LLM] 首响应超时: provider={}, {}s 内无任何响应（含思考内容），切换备用提供商",
                            provider["key"], _first_response_timeout,
                        )
                        raise _FirstResponseTimeoutError(
                            f"首响应超时（{_first_response_timeout}s 内无任何响应，含思考内容）"
                        ) from _stream_err
                    _se = str(_stream_err).lower()
                    _unsupported = "stream" in _se and any(
                        k in _se for k in ("support", "invalid", "unknown", "not found")
                    )
                    if not _unsupported:
                        raise
                    logger.warning(
                        "[LLM] 提供商疑似不支持流式（{}），回退非流式调用",
                        str(_stream_err)[:120],
                    )
                if not _stream_done:
                    # 非流式回退：保留 to_thread 卡死防御 + 按 prompt 长度放宽的总时长预算
                    try:
                        response = await _aio_llm.wait_for(
                            _aio_llm.to_thread(lambda: _aio_llm.run(litellm.acompletion(**kwargs))),
                            timeout=_fallback_total_timeout,
                        )
                    except (AttributeError, NotImplementedError, RuntimeError):
                        # 极老 Python / 嵌套 asyncio.run 失败时回退旧逻辑
                        response = await _aio_llm.wait_for(
                            litellm.acompletion(**kwargs),
                            timeout=_fallback_total_timeout,
                        )
                    content = response.choices[0].message.content
                    _finish_reason = getattr(response.choices[0], "finish_reason", None)
                    # 提取 token 使用量（可能为 None）
                    usage_obj = getattr(response, "usage", None)
                    if usage_obj:
                        usage = self._normalize_usage(usage_obj)
                if not content:
                    raise _RetryableError("LLM 返回空内容")
                # v2.24: 检测输出是否因 max_tokens 截断
                if _finish_reason == "length":
                    logger.warning(
                        "[LLM] 输出被 max_tokens={} 截断 (content_len={})，JSON 解析可能失败",
                        kwargs.get("max_tokens"), len(content),
                    )
                logger.debug("LLM 调用成功 [provider={}, attempt={}]", provider["key"], attempt)
                return content, usage, provider["model"]

            except _NonRetryableError:
                raise
            except _FirstResponseTimeoutError:
                # v3.3：首响应预算内无任何输出（含思考内容）→ 不消耗剩余重试次数，
                # 直接交给 call() 切换备用提供商
                raise
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                # 4xx 错误不重试（docs/03 §6.3），但认证错误可故障转移
                if self._is_non_retryable(e, err_str):
                    raise _NonRetryableError(str(e)) from e
                if attempt < self.MAX_ATTEMPTS:
                    delay = self._jitter_delay(attempt)
                    # TimeoutError 的 str() 为空串，用 repr 兜底保证超时可诊断
                    _err_brief = str(e)[:120] or repr(e)[:120]
                    logger.debug(
                        "LLM 调用失败 [attempt={}/{}], {} 后重试: {}",
                        attempt,
                        self.MAX_ATTEMPTS,
                        f"{delay:.2f}s",
                        _err_brief,
                    )
                    await asyncio.sleep(delay)

        raise _RetryableError(f"重试 {self.MAX_ATTEMPTS} 次后仍失败: {last_error}")

    def _jitter_delay(self, attempt: int) -> float:
        """计算全抖动退避延迟。

        公式（docs/03 §6.3）：sleep = random(0, min(cap, base * 2^attempt))

        Args:
            attempt: 当前尝试次数（从 1 开始）

        Returns:
            float: 延迟秒数
        """
        ceiling = min(self.MAX_DELAY, self.BASE_DELAY * (2 ** attempt))
        return random.uniform(0, ceiling)

    def _is_non_retryable(self, error: Exception, err_str: str) -> bool:
        """判断是否为不可重试错误。

        Args:
            error: 异常对象
            err_str: 异常消息小写形式

        Returns:
            bool: True 表示不可重试（4xx 类错误，非 429）

        Note:
            docs/03 §6.3：4xx 不重试；429 读 Retry-After 头重试。
            认证失败(401/403)归类为不可重试，但会触发故障转移。
        """
        # LiteLLM 通常会包装异常，检查状态码
        status_code = getattr(error, "status_code", None)
        if status_code is not None:
            if status_code == 429:
                return False  # 限流可重试
            if 400 <= status_code < 500:
                return True  # 其他 4xx 不重试
            return False  # 5xx 可重试
        # 无状态码时按文本判断
        if "authentication" in err_str or "unauthorized" in err_str or "invalid api key" in err_str:
            return True
        return False

    def _record_provider_failure(self, provider: dict) -> None:
        """记录提供商失败，达阈值则临时禁用。

        Args:
            provider: 失败的提供商配置
        """
        key = provider["key"]
        self._provider_failures[key] = self._provider_failures.get(key, 0) + 1
        if self._provider_failures[key] >= self.PROVIDER_FAILURE_THRESHOLD:
            self._provider_disabled_until[key] = time.time() + self.PROVIDER_DISABLE_SECONDS
            logger.warning(
                "提供商 {} 连续失败 {} 次，临时禁用 {} 秒",
                key,
                self.PROVIDER_FAILURE_THRESHOLD,
                self.PROVIDER_DISABLE_SECONDS,
            )

    def _disable_provider_now(self, provider: dict, reason: str = "") -> None:
        """立即禁用某提供商（不等累计阈值）。

        v3.4.2：适用于"硬失败"——额度耗尽（403 Free quota exhausted）、认证失败（401）、
        模型不存在等。这类错误**重试与等待都无意义**，立即禁用 10 分钟可让当前调用
        立刻跳到下一家、后续调用直接跳过它，从而把"全挂"的判断从数十分钟压到一次探测。
        """
        key = provider["key"]
        self._provider_failures[key] = max(
            self._provider_failures.get(key, 0), self.PROVIDER_FAILURE_THRESHOLD
        )
        self._provider_disabled_until[key] = time.time() + self.PROVIDER_DISABLE_SECONDS
        logger.warning(
            "提供商 {} 立即禁用 {} 秒（{}）", key, self.PROVIDER_DISABLE_SECONDS, reason or "硬失败"
        )

    async def _log_call(
        self,
        agent_name: str,
        system_prompt: str,
        user_prompt: str,
        response: str,
        parsed_json: Any,
        temperature: float,
        duration_ms: int,
        token_usage: dict,
        success: bool,
        error_message: str = "",
        task_id: str = "",
        req_id: str = "",
        model_used: str = "",
        provider_used: str = "",
    ) -> None:
        """记录 LLM 调用到数据库（不阻断主流程）。

        使用独立 DB session（async_session），避免依赖调用方的 session——
        Agent 多在异步上下文/后台协程中运行，调用方 session 可能已关闭。

        任何异常只 logger.warning，不向上传播，确保日志失败不影响主流程。

        Args:
            agent_name: 调用方 Agent 名称
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            response: LLM 返回的完整响应文本
            parsed_json: 解析后的 JSON（dict/list，失败时为 None）
            temperature: 温度参数
            duration_ms: 调用耗时（毫秒）
            token_usage: token 使用量字典
            success: 是否成功
            error_message: 失败时的错误信息
            task_id: 关联的异步任务 ID
            req_id: 关联的需求 ID
            model_used: 实际使用的模型名
            provider_used: 实际使用的供应商 key（格式 "provider:model"）。
                          v2.27: 旧版用 settings.LLM_PROVIDER（全局主供应商），
                          导致日志无法区分实际使用的供应商。现改为传入实际值。
        """
        try:
            from app.db.database import async_session
            from app.db.models import LLMCallLog
            # P3-6: task_id/req_id 未显式传入时，从环境上下文兜底（编排器入口 set）
            if not task_id or not req_id:
                from app.core.task_context import get_task_id as _gtid, get_req_id as _grid
                task_id = task_id or _gtid()
                req_id = req_id or _grid()
            # settings 已在顶部导入，无需重复导入

            # v2.27: 优先使用实际供应商 key，回退到全局主供应商
            # provider_used 格式为 "provider_name:model_name"，提取供应商名
            if provider_used and ":" in provider_used:
                provider_name = provider_used.split(":", 1)[0]
            elif provider_used:
                provider_name = provider_used
            else:
                provider_name = getattr(settings, "LLM_PROVIDER", "") or ""
            log_entry = LLMCallLog(
                agent_name=agent_name or "unknown",
                task_id=task_id or None,
                req_id=req_id or None,
                provider=provider_name,
                model=model_used or "",
                system_prompt=system_prompt or "",
                user_prompt=user_prompt or "",
                response=response or "",
                parsed_json=parsed_json if isinstance(parsed_json, (dict, list)) else None,
                temperature=temperature,
                duration_ms=duration_ms,
                token_usage=token_usage if token_usage else None,
                success=success,
                error_message=error_message or None,
            )
            async with async_session() as session:
                session.add(log_entry)
                await session.commit()
        except Exception as e:
            logger.warning("[LLM/Log] 日志记录失败（不阻断）: {}", str(e)[:200])

    @staticmethod
    def _parse_json(text: str, schema_hint: dict | None) -> dict:
        """解析 LLM 输出为 JSON，容忍常见格式问题。

        Args:
            text: LLM 输出文本
            schema_hint: 期望字段提示（可选，用于校验）

        Returns:
            dict: 解析后的 JSON 对象

        Raises:
            LLMJsonParseError: 解析失败或字段校验不通过
        """
        # 预处理：去除 BOM、首尾空白、零宽字符
        cleaned = text.strip().lstrip("\ufeff").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")

        # 尝试直接解析
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e1:
            # v3.3：除首尾片段外，额外打印**出错位置附近**的窗口——
            # 仅凭 line/column 与首尾 80 字符无法定位语法错误（如第 25 行的
            # 多余逗号 / 缺逗号），导致「为什么修复函数也没救回来」无法排查。
            _pos = getattr(e1, "pos", 0) or 0
            _win = cleaned[max(0, _pos - 120): _pos + 120]
            logger.debug("[LLM/parse] 直接解析失败: {}, text_len={}, first80={!r}, last80={!r}, "
                         "err_pos={}, 附近片段={!r}",
                         str(e1)[:100], len(cleaned), cleaned[:80],
                         cleaned[-80:] if len(cleaned) > 80 else "", _pos, _win)
            # 容忍：提取首个 {...} 或 [...] 块
            data = _extract_json_block(cleaned)
            if data is None:
                # 最后尝试：修复常见 JSON 问题（尾随逗号、单引号、截断补全）
                data = _try_repair_json(cleaned)
            if data is None:
                # v2.24: 输出超长时附加截断提示
                trunc_hint = ""
                if len(cleaned) > 20000:
                    trunc_hint = "（输出可能被 max_tokens 截断，请检查 LLM 配置）"
                raise LLMJsonParseError(
                    f"LLM 输出无法解析为 JSON (len={len(cleaned)}){trunc_hint}: {cleaned[:200]}"
                )

        if not isinstance(data, dict):
            # 容忍 LLM 返回 list（如 GenAgent 的 [{"test_suite": [...]}] 或直接用例数组）
            # 调用方（req_agent/gen_agent）有自己的类型防御逻辑
            if isinstance(data, list):
                elem_types = [type(e).__name__ for e in data[:3]]
                logger.debug("[LLM/parse] LLM 返回 list: len={}, elem_types={}", len(data), elem_types)
            else:
                raise LLMJsonParseError(f"LLM 输出非 JSON 对象/数组: {type(data).__name__}")

        # schema_hint 校验：仅对 dict 检查键是否存在
        if schema_hint and isinstance(data, dict):
            missing = [k for k in schema_hint if k not in data]
            if missing:
                logger.warning("JSON 输出缺失字段: {}", missing)

        return data


def parse_llm_json(text: str, schema_hint: dict | None = None):
    """公共容错 JSON 解析入口（v3.2.2 收敛重复）。

    复用 llm_client._parse_json 的完整容错（围栏/前后缀/尾随逗号/截断补全），
    但失败时返回 None 而非抛异常，供各 Agent 替代裸 json.loads 直接调用。
    """
    try:
        return llm_client._parse_json(text, schema_hint)
    except Exception:
        return None


# ============================================================================
# 异常类型
# ============================================================================

class _RetryableError(Exception):
    """可重试错误（5xx、超时、空响应等）。"""


class _NonRetryableError(Exception):
    """不可重试错误（4xx 非限流、认证失败等）。"""


class _FirstResponseTimeoutError(Exception):
    """流式调用在首响应预算内未收到任何输出（正文与思考内容都没有）。

    v3.3（N4 根治）：此异常**不参与重试**，直接触发提供商故障转移。
    依据：本项目所有 LLM 调用均为流式，若一个模型连续
    `settings.LLM_FIRST_RESPONSE_TIMEOUT`（默认 180s）都吐不出任何 chunk，
    重试同一模型只会把挂起时间乘以 MAX_ATTEMPTS，无任何收益。
    """


# ============================================================================
# 辅助函数
# ============================================================================

def _try_parse_json(text: str) -> Any:
    """尝试解析 JSON，失败返回 None（不抛异常）。

    用于容错解析链中多处尝试，避免重复 try/except 样板。
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _find_first_json_start(text: str) -> int:
    """返回文本中首个 { 或 [ 的位置（取更靠前者）；都不存在返回 -1。"""
    i_obj = text.find("{")
    i_arr = text.find("[")
    candidates = [i for i in (i_obj, i_arr) if i != -1]
    return min(candidates) if candidates else -1


def _find_matching_bracket(text: str, start: int) -> int:
    """从 start（须为 { 或 [）做括号配对，返回匹配的右括号位置；找不到返回 -1。

    正确处理字符串与转义字符，避免把字符串字面量内的括号误判为结构括号。
    """
    if start < 0 or start >= len(text) or text[start] not in ("{", "["):
        return -1
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1


def _extract_json_block(text: str) -> dict | list | None:
    """从文本中提取首个 JSON 对象或数组块。

    用于 LLM 输出包含额外说明文字、markdown 代码块包裹、或输出被截断时容错解析。
    同时兼容对象 {...} 与数组 [...] 两种形态（GenAgent 允许数组输出）。

    Args:
        text: 可能包含 JSON 的文本

    Returns:
        dict | list | None: 解析成功返回对象或数组；否则 None
    """
    import re

    # 1. 优先提取 ```json ... ``` 或 ``` ... ``` 代码块（兼容 [ 与 { 开头）
    # v3.x fix: 兼容 ```json+language / ```json5 / ```lang 等变体，
    #           即 fence 上 json 后紧跟 +/5/_/-/数字或其他语言后缀（没有空白分隔）时，
    #           原来 (?:json)? 吃掉 "json" 后留下 "+" / "5" 开头字符，导致 block 非 JSON。
    code_block = re.search(r"```(?:json[0-9a-zA-Z_\-+]*|[\-\+\.A-Za-z0-9_]+)?\s*([\s\S]*?)```", text, re.DOTALL)
    if code_block:
        block = code_block.group(1).strip()
        # 再兜底：如果 block 不是以 { / [ 开头，向里找第一个 { 或 [ 做截断，
        # 兼容 ```foo json {\n ...} 这种混合标注或多余前导字符。
        first_ob = min(
            [i for i in (block.find("{"), block.find("[")) if i >= 0],
            default=-1,
        )
        if first_ob > 0:
            block = block[first_ob:]
        elif first_ob == -1:
            pass  # 无括号，留给 parse 层直接报错
        data = _try_parse_json(block)
        if data is not None:
            return data
        # 代码块内 JSON 损坏（如被截断）→ 尝试修复块内内容
        repaired = _try_repair_json(block)
        if repaired is not None:
            return repaired

    # 2. 回退：括号配对提取最外层 { 或 [ 块（v3.1 修复数组解析缺陷）
    start = _find_first_json_start(text)
    if start == -1:
        return None
    end = _find_matching_bracket(text, start)
    if end == -1:
        # 截断场景：从 start 到文本末尾尝试修复补全缺失括号
        return _try_repair_json(text[start:])
    block = text[start : end + 1]
    data = _try_parse_json(block)
    if data is not None:
        return data
    # 括号内 JSON 损坏（如尾随逗号）→ 尝试修复
    return _try_repair_json(block)


def _normalize_code_expressions(text: str) -> str:
    """预处理 LLM 误用的代码表达式为 JSON 字面量。

    LLM 偶尔会在 JSON 输出中混入 JavaScript/Python 代码表达式（如
    `"a".repeat(50)`、`"x" * 100`），导致 JSON 解析失败。本函数将这些
    表达式替换为对应的字面量字符串，最大化容错能力。

    支持的模式：
        1. "str".repeat(N)        → "strstr...str" (N 次)
        2. "str".repeat(N, "sep") → "strsepstr...str"
        3. "str" * N              → "strstr...str" (Python 风格)
        4. 超长重复字符串压缩      → "aaa...(N个a)" (防止 LLM 生成万级重复字符导致 JSON 结构损坏)

    Args:
        text: 可能含代码表达式的文本

    Returns:
        str: 替换代码表达式后的文本
    """
    import re

    def _escape_for_json(s: str, quote: str) -> str:
        """转义字符串内容以安全嵌入 JSON 字符串字面量。"""
        return s.replace("\\", "\\\\").replace(quote, "\\" + quote)

    def _build_replacement(quote: str, body: str, count: int, sep: str = "") -> str:
        """构造替换字符串：quote + body * count + quote。"""
        if count <= 0:
            return f"{quote}{quote}"
        if count > 10000:
            count = 10000  # 防止内存爆炸
        escaped = _escape_for_json(body, quote)
        if sep:
            # 带分隔符：str + sep + str + sep + ... + str（共 count 个 str，count-1 个 sep）
            sep_esc = _escape_for_json(sep, quote)
            return f"{quote}{(escaped + sep_esc) * (count - 1) + escaped}{quote}"
        return f"{quote}{escaped * count}{quote}"

    # 1. 处理 "str".repeat(N) 和 "str".repeat(N, "sep")
    #    分双引号和单引号两种情况独立匹配，避免反向引用问题
    for quote_char in ('"', "'"):
        # 双引号版本：匹配 "body".repeat(N) 或 "body".repeat(N, "sep")
        # body 内部允许 \" 转义和除 quote 外的任意字符
        pattern = re.compile(
            re.escape(quote_char) +
            r'((?:\\' + re.escape(quote_char) + r'|[^' + re.escape(quote_char) + r'\\])*)' +
            re.escape(quote_char) +
            r'\.repeat\(\s*(\d+)\s*(?:,\s*' +
            re.escape(quote_char) +
            r'((?:\\' + re.escape(quote_char) + r'|[^' + re.escape(quote_char) + r'\\])*)' +
            re.escape(quote_char) +
            r'\s*)?\)'
        )

        # 使用默认参数固定 quote_char，避免闭包陷阱
        def _replace_repeat(match: re.Match, qc=quote_char) -> str:
            body = match.group(1) or ""
            count = int(match.group(2))
            sep = match.group(3) or ""
            return _build_replacement(qc, body, count, sep)

        text = pattern.sub(_replace_repeat, text)

    # 2. 处理 "str" * N（Python 风格字符串重复）
    for quote_char in ('"', "'"):
        pattern_mul = re.compile(
            re.escape(quote_char) +
            r'((?:\\' + re.escape(quote_char) + r'|[^' + re.escape(quote_char) + r'\\])*)' +
            re.escape(quote_char) +
            r'\s*\*\s*(\d+)'
        )

        # 使用默认参数固定 quote_char
        def _replace_mul(match: re.Match, qc=quote_char) -> str:
            body = match.group(1) or ""
            count = int(match.group(2))
            return _build_replacement(qc, body, count)

        text = pattern_mul.sub(_replace_mul, text)

    # 3. 压缩超长重复字符串（LLM 生成边界值测试时可能误写万级重复字符）
    #    场景：LLM 生成 "password": "aaaa...（22412 个 a）..." 导致输出过长，
    #    LLM 在超长字符串后遗漏 JSON 闭合括号，JSON 解析失败。
    #    策略：检测 JSON 字符串字面量内连续重复字符 ≥ 200 的情况，压缩为短表示。
    #    阈值 200 既能覆盖问题场景，又不影响正常短字符串。
    text = _compress_long_repeated_chars(text, threshold=200)

    return text


def _compress_long_repeated_chars(text: str, threshold: int = 200) -> str:
    """压缩 JSON 字符串字面量中的超长重复字符序列。

    当 LLM 生成边界值测试数据时（如 128 位密码），可能误将 "128 个 a" 实际写出
    上万个 'a' 字符。这会导致：
    1. 输出 token 大量浪费
    2. LLM 在超长字符串后"忘记"闭合 JSON 对象括号，导致解析失败

    本函数扫描 JSON 字符串字面量（双引号包围、正确处理转义），将连续重复字符
    超过 threshold 的部分压缩为 "xxx...(N个x)" 格式。

    Args:
        text: 可能包含超长重复字符串的 JSON 文本
        threshold: 触发压缩的最小连续重复字符数（默认 200）

    Returns:
        str: 压缩后的文本
    """
    import re

    # 匹配 JSON 字符串字面量：双引号包围，内部允许 \" 转义和其他任意字符
    # 使用非贪婪匹配 + 负向断言确保正确识别字符串边界
    string_pattern = re.compile(r'"((?:\\.|[^"\\])*)"')

    def _compress_match(match: re.Match) -> str:
        """处理单个字符串字面量匹配，压缩内部超长重复字符。"""
        content = match.group(1)
        # 快速检查：字符串较短或无长重复序列时直接返回
        if len(content) < threshold:
            return match.group(0)

        # 检测连续重复字符：用正则找到所有 (char){n,} 其中 n >= threshold
        # 注意：需要处理转义字符，不压缩转义序列（如 \n \t）
        def _replace_repeated(m: re.Match) -> str:
            char = m.group(1)
            count = len(m.group(0)) // len(char) if len(char) > 0 else 0
            if count >= threshold:
                # 保留前 5 个字符 + 省略描述 + 后 3 个字符
                return f"{char * 5}...({count}个{char})...{char * 3}"
            return m.group(0)

        # 匹配连续重复的同一非转义字符（排除反斜杠开头的转义序列）
        # 使用 (.) 捕获一个字符，然后 \1+ 匹配后续相同字符
        repeated_pattern = re.compile(r'([^\n\\])\1{')
        # 上述正则不完整，改用直接匹配方式
        # 匹配同一字符连续出现 threshold 次以上
        # 注意：不匹配反斜杠（避免破坏转义序列）
        result = []
        i = 0
        n = len(content)
        while i < n:
            ch = content[i]
            # 跳过转义序列（反斜杠 + 下一字符）
            if ch == "\\" and i + 1 < n:
                result.append(content[i:i + 2])
                i += 2
                continue
            # 统计当前字符的连续出现次数
            j = i + 1
            while j < n and content[j] == ch:
                j += 1
            run_len = j - i
            if run_len >= threshold:
                # 压缩：保留前 5 + 描述 + 后 3
                result.append(f"{ch * 5}...({run_len}个{ch})...{ch * 3}")
            else:
                result.append(content[i:j])
            i = j

        compressed = "".join(result)
        # 转义压缩后的内容中的双引号和反斜杠（虽然占位符不太可能包含这些，但安全起见）
        # 注意：原始 content 已经是去掉了外层引号的字符串字面量内容，转义状态保持不变
        # 压缩后的 ...(N个x)... 部分不含特殊字符，不需要额外转义
        return f'"{compressed}"'

    return string_pattern.sub(_compress_match, text)


def _try_repair_json(text: str) -> dict | None:
    """尝试修复常见 JSON 格式问题后解析。

    修复项：
    1. LLM 误用的代码表达式（"a".repeat(50) → "aaa..."）
    2. 尾随逗号（}, ] 前的逗号）
    3. 单引号 → 双引号
    4. 截断的 JSON（补全缺失的右括号，按 LIFO 顺序）

    Args:
        text: 可能可修复的 JSON 文本

    Returns:
        dict | None: 修复后解析成功返回 dict；否则 None
    """
    import re

    # 0. 预处理：替换 LLM 误用的代码表达式（如 "a".repeat(50)）
    repaired = _normalize_code_expressions(text)

    # 1. 去除尾随逗号
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)

    # 2. 尝试解析
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # 3. 补全截断的 JSON（LLM 输出被 max_tokens 截断时常见）
    # 用栈记录打开的括号类型，按 LIFO 顺序补全对应的右括号
    stack: list[str] = []  # 记录 '{' 或 '['
    in_string = False
    escape = False
    for ch in repaired:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            stack.append("{")
        elif ch == "}":
            if stack and stack[-1] == "{":
                stack.pop()
        elif ch == "[":
            stack.append("[")
        elif ch == "]":
            if stack and stack[-1] == "[":
                stack.pop()

    if stack:
        # 截断在字符串内部时，先闭合字符串
        if in_string:
            repaired += '"'
            # 字符串闭合后可能需要补一个逗号分隔符的容错（不影响后续括号闭合）
        # 按 LIFO 顺序补全右括号
        closing = {"{": "}", "[": "]"}
        repaired += "".join(closing[c] for c in reversed(stack))
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            # 进一步容错：字符串闭合后可能缺少值结尾，尝试在字符串后补 }
            # 再试一次：截断在对象某个字段值中间时，丢弃最后一个不完整的字段
            pass

    # 4. 终极容错：截断在对象字段值中间，尝试丢弃最后一个不完整的键值对
    # 找到最后一个完整的 } 或 ]，截断到那里
    last_complete = max(repaired.rfind("}"), repaired.rfind("]"))
    if last_complete > 0:
        truncated = repaired[: last_complete + 1]
        # 重新用栈补全
        stk2: list[str] = []
        in_str2 = False
        esc2 = False
        for ch in truncated:
            if esc2:
                esc2 = False
                continue
            if ch == "\\":
                esc2 = True
                continue
            if ch == '"':
                in_str2 = not in_str2
                continue
            if in_str2:
                continue
            if ch == "{":
                stk2.append("{")
            elif ch == "}":
                if stk2 and stk2[-1] == "{":
                    stk2.pop()
            elif ch == "[":
                stk2.append("[")
            elif ch == "]":
                if stk2 and stk2[-1] == "[":
                    stk2.pop()
        if stk2:
            # ⚠️ closing 在上方 `if stack:` 块内定义；此处需独立定义，
            # 否则当 stack 为空但 stk2 非空时会触发 NameError
            closing2 = {"{": "}", "[": "]"}
            truncated += "".join(closing2[c] for c in reversed(stk2))
        try:
            return json.loads(truncated)
        except json.JSONDecodeError:
            pass

    # 5. 逐对象截断修复：适用于数组中某个对象因缺失闭合括号而损坏的场景
    #    典型场景：LLM 生成超长字符串（如 22412 个 'a'）后，遗漏了 JSON 对象的
    #    闭合 }，导致后续对象被解析器误认为前一个对象的属性，JSON 解析失败。
    #    策略：遍历所有 } 位置，对每个位置截断并用栈补全剩余括号，尝试解析。
    #    这样即使中间缺失 }，也能通过截断+补全恢复可解析的 JSON。
    stripped = repaired.lstrip()
    if stripped.startswith("["):
        # 找到所有 } 的位置（在字符串外部的）
        brace_positions: list[int] = []
        stk3: list[str] = []
        in_str3 = False
        esc3 = False
        for i, ch in enumerate(repaired):
            if esc3:
                esc3 = False
                continue
            if ch == "\\":
                esc3 = True
                continue
            if ch == '"':
                in_str3 = not in_str3
                continue
            if in_str3:
                continue
            if ch == "{":
                stk3.append("{")
            elif ch == "}":
                if stk3 and stk3[-1] == "{":
                    stk3.pop()
                    brace_positions.append(i)
                # 不匹配的 } 也记录（可能帮助截断）
                elif stk3:
                    brace_positions.append(i)
        # 从后往前，在每个 } 位置尝试截断+栈补全
        closing_map = {"{": "}", "[": "]"}
        for pos in reversed(brace_positions):
            truncated3 = repaired[: pos + 1]
            # 重新计算截断后文本的栈状态
            stk4: list[str] = []
            in_str4 = False
            esc4 = False
            for ch in truncated3:
                if esc4:
                    esc4 = False
                    continue
                if ch == "\\":
                    esc4 = True
                    continue
                if ch == '"':
                    in_str4 = not in_str4
                    continue
                if in_str4:
                    continue
                if ch == "{":
                    stk4.append("{")
                elif ch == "}":
                    if stk4 and stk4[-1] == "{":
                        stk4.pop()
                elif ch == "[":
                    stk4.append("[")
                elif ch == "]":
                    if stk4 and stk4[-1] == "[":
                        stk4.pop()
            # 补全缺失的闭合括号
            if stk4:
                truncated3 += "".join(closing_map[c] for c in reversed(stk4))
            try:
                data3 = json.loads(truncated3)
                if isinstance(data3, list) and len(data3) > 0:
                    logger.debug(
                        "[LLM/repair] 逐对象截断成功: 保留 {} 个对象（丢弃末尾损坏对象）",
                        len(data3),
                    )
                    return data3
            except json.JSONDecodeError:
                continue

    return None


# ============================================================================
# 全局单例
# ============================================================================

llm_client = LLMClient()
"""LLM 客户端全局单例。所有 Agent 通过此实例调用 LLM。"""
