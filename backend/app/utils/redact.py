"""敏感信息脱敏（v4.0.1 / 缺陷 S9）。

背景（docs/TEST_PLAN.md §F S9）：
    需求非功能条款要求「检索结果中的敏感信息（密钥、token）必须脱敏后展示」，
    实测知识问答会把知识库原文/模型回答里的敏感串**原样回显**（如手机号
    13800138000），既违反需求也构成数据外泄面。

设计：
    - **密钥 / token 类**（需求硬要求）：sk-*、Bearer 令牌、JWT、`key=value` 形式的
      api_key/token/secret/password 等。命中即整体掩蔽。
    - **个人隐私类（PII，超集）**：中国大陆手机号、邮箱、身份证号，掩蔽中间段。
      需求条款只点名「密钥、token」，但实测泄露样本是手机号（§I #35），
      故一并覆盖；可用 `include_pii=False` 关闭（保留仅密钥/token 脱敏）。

调用约定：
    - 回答正文、追问回答、以及落库的问答记录，都必须先脱敏再展示/持久化，
      避免「展示已脱敏、库内仍是明文」的口径分裂。
    - 参考来源的 summary 同样需脱敏（它会被前端展示）。

注意：`re.sub` 的替换串里 `\\1` 是反向引用，数字/点号等字面量无需转义；
掩蔽段统一用 `*`，不使用 NUL 等控制字符，避免污染下游 JSON/Markdown 渲染。
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# 密钥 / token 类（需求硬要求）
# ---------------------------------------------------------------------------
_SECRET_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    # OpenAI / DeepSeek 风格密钥：sk-xxxx、sk-proj-xxxx
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}\b"), "sk-********"),
    # JWT（三段 base64url，以 eyJ 开头）
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{4,}\b"), "***"),
    # Bearer <token>（Authorization 头）
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{6,}"), r"\1 ***"),
    # key=value / key: value 形式的敏感键
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|auth[_-]?token"
            r"|client[_-]?secret|app[_-]?secret|secret[_-]?key|password|passwd|pwd)"
            r"(\s*[:=]\s*)[\"']?([A-Za-z0-9._\-/+=]{4,})[\"']?"
        ),
        r"\1\2***",
    ),
]

# ---------------------------------------------------------------------------
# 个人隐私类（PII，超集；可用 include_pii=False 关闭）
# ---------------------------------------------------------------------------
_PII_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    # 中国大陆手机号：1[3-9] 开头 11 位；前后不得紧邻数字（避免误伤长数字串）
    (re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)"), r"\1****\2"),
    # 身份证号：6 位地区 + 8 位生日 + 3 位顺序码 + 1 位校验
    (re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[0-9Xx])(?!\d)"), r"\1********\2"),
    # 邮箱：仅保留首字符 + 域名
    (
        re.compile(r"\b([A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]*@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b"),
        r"\1***@\2",
    ),
]


def redact(text: str, *, include_pii: bool = True) -> str:
    """对单段文本做敏感信息脱敏（幂等：已脱敏的串不会被二次改写）。"""
    if not text or not isinstance(text, str):
        return text
    out = text
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    if include_pii:
        for pattern, repl in _PII_PATTERNS:
            out = pattern.sub(repl, out)
    return out


def redact_sources(
    sources: list[dict] | None, *, include_pii: bool = True
) -> list[dict] | None:
    """脱敏参考来源中会被前端展示的文本字段（title / summary）。

    只处理展示字段，不动 id/type/similarity 等结构化字段，
    避免破坏 `_enrich_sources` 与前端 `KbQAReference` 的既有契约。
    """
    if not sources:
        return sources
    cleaned: list[dict] = []
    for s in sources:
        if not isinstance(s, dict):
            cleaned.append(s)
            continue
        item = dict(s)
        for key in ("title", "summary"):
            val = item.get(key)
            if isinstance(val, str) and val:
                item[key] = redact(val, include_pii=include_pii)
        cleaned.append(item)
    return cleaned


def redact_claims(
    claims: list[dict] | None, *, include_pii: bool = True
) -> list[dict] | None:
    """脱敏证据引用（claims）中的结论文本。"""
    if not claims:
        return claims
    cleaned: list[dict] = []
    for c in claims:
        if not isinstance(c, dict):
            cleaned.append(c)
            continue
        item = dict(c)
        claim = item.get("claim")
        if isinstance(claim, str) and claim:
            item["claim"] = redact(claim, include_pii=include_pii)
        cleaned.append(item)
    return cleaned


def redact_enabled() -> bool:
    """脱敏总开关（可配置回退；默认开启）。"""
    try:
        from app.config import settings

        return bool(getattr(settings, "KB_REDACT_SENSITIVE", True))
    except Exception:  # noqa: BLE001
        return True


def redact_pii_enabled() -> bool:
    """PII（手机号/邮箱/身份证）是否一并脱敏；默认开启。"""
    try:
        from app.config import settings

        return bool(getattr(settings, "KB_REDACT_PII", True))
    except Exception:  # noqa: BLE001
        return True
