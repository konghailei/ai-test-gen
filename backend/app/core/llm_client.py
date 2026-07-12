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

    # 故障转移配置（docs/03 §6.4）
    PROVIDER_FAILURE_THRESHOLD = 3
    PROVIDER_DISABLE_SECONDS = 600  # 10 分钟

    def __init__(self) -> None:
        """初始化客户端。"""
        self._breaker = CircuitBreaker()
        # 提供商连续失败计数：{provider_key: failures}
        self._provider_failures: dict[str, int] = {}
        # 提供商禁用截止时间：{provider_key: disabled_until_timestamp}
        self._provider_disabled_until: dict[str, float] = {}
        # 最近一次调用使用的模型名（用于错误报告）
        self._last_model: str = ""

    async def call(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.3,
        response_format: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """调用 LLM 生成文本（主入口）。

        Args:
            prompt: 用户提示词
            system_prompt: 系统指令（可选）。若提供则作为 system message
            temperature: 采样温度，0-1。默认 0.3（偏稳定）
            response_format: 响应格式约束。"json" 表示强制 JSON 输出；None 表示自由文本
            model: 指定模型名（覆盖配置）。默认用 settings 中的模型
            max_tokens: 最大生成 token 数。None 表示不限

        Returns:
            str: LLM 生成的文本内容

        Raises:
            RuntimeError: 所有提供商均不可用或重试耗尽

        Note:
            故障转移顺序：主提供商 → 备用提供商（若配置）。
            每个提供商内部执行全抖动重试；提供商间为故障转移关系。
        """
        # 断路器检查
        if not self._breaker.allow_request():
            raise LLMBreakerOpenError("LLM 断路器开启，服务暂不可用（错误率超阈值）")

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # 构建候选提供商列表（主 → 备）
        providers = self._candidate_providers(model)
        if not providers:
            raise LLMNoProviderAvailable("无可用 LLM 提供商（未配置 API Key）")

        last_error: Exception | None = None
        for provider in providers:
            try:
                result = await self._call_with_retry(
                    provider, messages, temperature, response_format, max_tokens
                )
                self._breaker.record_success()
                self._provider_failures.pop(provider["key"], None)
                return result
            except _RetryableError as e:
                # 重试耗尽 → 记录失败，尝试下一提供商
                last_error = e
                self._record_provider_failure(provider)
                logger.warning(
                    "提供商 {} 重试耗尽，尝试故障转移: {}",
                    provider["key"],
                    str(e)[:200],
                )
            except _NonRetryableError as e:
                # 4xx 等不可重试错误 → 直接故障转移（docs/03 §6.3：4xx 不重试）
                last_error = e
                self._record_provider_failure(provider)
                logger.warning("提供商 {} 不可重试错误，故障转移: {}", provider["key"], str(e)[:200])

        # 所有提供商均失败
        self._breaker.record_failure()
        raise LLMNoProviderAvailable(f"所有 LLM 提供商均不可用: {last_error}")

    async def call_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        schema_hint: dict | None = None,
        temperature: float = 0.2,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> dict | list:
        """调用 LLM 并解析为 JSON 对象或数组。

        Args:
            prompt: 用户提示词（应明确要求返回 JSON）
            system_prompt: 系统指令（可选）
            schema_hint: 期望的 JSON 结构提示（用于校验关键字段存在性）。如 {"id": str, "steps": list}
            temperature: 采样温度。默认 0.2（JSON 输出需更稳定）
            model: 指定模型名
            max_tokens: 最大生成 token 数。None 表示不限（长 JSON 建议设置以防止 LLM 退化）

        Returns:
            dict: 解析后的 JSON 对象

        Raises:
            RuntimeError: LLM 调用失败或 JSON 解析失败

        Note:
            自动在 prompt 末尾追加 JSON 格式约束提示。
            若 schema_hint 提供，校验其键是否存在于结果中。
        """
        formatted_prompt = prompt
        if schema_hint:
            keys = ", ".join(schema_hint.keys())
            formatted_prompt += f"\n\n请严格返回 JSON 对象，包含以下字段：{keys}。不要返回任何额外文本。"
        else:
            formatted_prompt += "\n\n请严格返回 JSON 对象，不要返回任何额外文本。"

        text = await self.call(
            formatted_prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            response_format="json",
            model=model,
            max_tokens=max_tokens,
        )
        return self._parse_json(text, schema_hint)

    # ========================================================================
    # 内部实现
    # ========================================================================

    def _candidate_providers(self, model: str | None) -> list[dict]:
        """构建候选提供商列表（跳过被禁用的）。

        优先使用 settings._all_provider_configs（多供应商持久化配置），
        回退到 settings.llm_config（传统 deepseek/openai 单例）。

        Args:
            model: 指定模型（若提供则只用主提供商）

        Returns:
            list[dict]: 可用提供商配置列表，每项含 key/api_key/base_url/model
        """
        now = time.time()

        # 优先使用多供应商配置
        all_configs: dict = getattr(settings, "_all_provider_configs", {})
        primary_provider = getattr(settings, "LLM_PROVIDER", "deepseek")

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
                    })

        return candidates

    async def _call_with_retry(
        self,
        provider: dict,
        messages: list[dict[str, str]],
        temperature: float,
        response_format: str | None,
        max_tokens: int | None,
    ) -> str:
        """对单个提供商执行全抖动重试调用。

        Args:
            provider: 提供商配置
            messages: 消息列表
            temperature: 采样温度
            response_format: "json" 或 None
            max_tokens: 最大 token 数

        Returns:
            str: 生成文本

        Raises:
            _RetryableError: 重试耗尽（可故障转移）
            _NonRetryableError: 不可重试错误（4xx）
        """
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
                if response_format == "json":
                    kwargs["response_format"] = {"type": "json_object"}
                if max_tokens:
                    kwargs["max_tokens"] = max_tokens

                response = await litellm.acompletion(**kwargs)
                content = response.choices[0].message.content
                if not content:
                    raise _RetryableError("LLM 返回空内容")
                logger.debug("LLM 调用成功 [provider={}, attempt={}]", provider["key"], attempt)
                return content

            except _NonRetryableError:
                raise
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                # 4xx 错误不重试（docs/03 §6.3），但认证错误可故障转移
                if self._is_non_retryable(e, err_str):
                    raise _NonRetryableError(str(e)) from e
                if attempt < self.MAX_ATTEMPTS:
                    delay = self._jitter_delay(attempt)
                    logger.debug(
                        "LLM 调用失败 [attempt={}/{}], {} 后重试: {}",
                        attempt,
                        self.MAX_ATTEMPTS,
                        f"{delay:.2f}s",
                        str(e)[:120],
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
            logger.debug("[LLM/parse] 直接解析失败: {}, text_len={}, first80={!r}, last80={!r}",
                         str(e1)[:100], len(cleaned), cleaned[:80], cleaned[-80:] if len(cleaned) > 80 else "")
            # 容忍：提取首个 {...} 或 [...] 块
            data = _extract_json_block(cleaned)
            if data is None:
                # 最后尝试：修复常见 JSON 问题（尾随逗号、单引号）
                data = _try_repair_json(cleaned)
            if data is None:
                raise LLMJsonParseError(f"LLM 输出无法解析为 JSON (len={len(cleaned)}): {cleaned[:200]}")

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


# ============================================================================
# 异常类型
# ============================================================================

class _RetryableError(Exception):
    """可重试错误（5xx、超时、空响应等）。"""


class _NonRetryableError(Exception):
    """不可重试错误（4xx 非限流、认证失败等）。"""


# ============================================================================
# 辅助函数
# ============================================================================

def _extract_json_block(text: str) -> dict | None:
    """从文本中提取首个 JSON 对象块。

    用于 LLM 输出包含额外说明文字时容错解析。

    Args:
        text: 可能包含 JSON 的文本

    Returns:
        dict | None: 解析成功返回 dict；否则 None
    """
    # 优先匹配 ```json ... ``` 代码块
    import re
    code_block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if code_block:
        try:
            return json.loads(code_block.group(1))
        except json.JSONDecodeError:
            pass

    # 回退：匹配首个 { 到最后一个 } 的范围
    start = text.find("{")
    if start == -1:
        return None
    end = text.rfind("}")
    if end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        # 尝试修复后解析
        return _try_repair_json(text[start : end + 1])


def _try_repair_json(text: str) -> dict | None:
    """尝试修复常见 JSON 格式问题后解析。

    修复项：
    1. 尾随逗号（}, ] 前的逗号）
    2. 单引号 → 双引号
    3. 截断的 JSON（补全缺失的右括号，按 LIFO 顺序）

    Args:
        text: 可能可修复的 JSON 文本

    Returns:
        dict | None: 修复后解析成功返回 dict；否则 None
    """
    import re

    # 1. 去除尾随逗号
    repaired = re.sub(r",\s*([}\]])", r"\1", text)

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
            truncated += "".join(closing[c] for c in reversed(stk2))
        try:
            return json.loads(truncated)
        except json.JSONDecodeError:
            pass

    return None


# ============================================================================
# 全局单例
# ============================================================================

llm_client = LLMClient()
"""LLM 客户端全局单例。所有 Agent 通过此实例调用 LLM。"""
