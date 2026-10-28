"""LLM 配置管理路由（多供应商 + 加密存储）。

配置存 database，重启不丢失。
API Key 使用 Fernet 对称加密后存入 PostgreSQL，防止明文泄露。

支持功能：
1. 多供应商配置（DeepSeek / OpenAI / 通义千问 / GLM / Claude / 自定义）
2. 内置供应商预设（自动填充 base_url 和 model）
3. API Key 加密存储（v2 安全升级）
4. 旧版明文 Key 自动迁移（首次读取时加密重写）
5. 连通性测试（带精确错误分类和用户友好提示）
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth import require_admin, require_write, CurrentUser, get_current_user
from app.db.database import get_session
from app.db.models import LLMConfig as LLMConfigModel, KBSetting
from app.models.api_schemas import (
    ApiResponse,
    BUILTIN_PROVIDERS,
    LLMConfigItem,
    LLMConfigRequest,
    LLMConfigResponse,
    LLMTestResponse,
)
from app.utils.crypto import decrypt_api_key, encrypt_api_key
from app.utils.logger import logger

def _sanitize_error(msg: str) -> str:
    """脱敏 LLM 错误信息中的 API Key 片段（v2.2 新增）。"""
    import re as _re
    # 匹配 sk-xxx 格式的 Key 片段
    msg = _re.sub(r'sk-[A-Za-z0-9]{10,}', 'sk-****', msg)
    # 匹配 Bearer 格式的 Key
    msg = _re.sub(r'Bearer\s+sk-[A-Za-z0-9]{10,}', 'Bearer sk-****', msg)
    return msg


class ReviewLLMSettingRequest(BaseModel):
    """评审专属 LLM 供应商设置。"""
    provider: str = Field(default="", max_length=64, description="供应商名（空=清除，跟随主力）")


# v3.5.5（docs/22 方案①）：FP 子批并发生成设置（kb_settings 通用 KV 复用）
GEN_CONCURRENCY_SETTING_KEY = "generation_fp_concurrency"
# 合法区间：1=串行回退（与 v3.5.4 行为一致）；上限 5 防误配打爆供应商
GEN_CONCURRENCY_MIN = 1
GEN_CONCURRENCY_MAX = 5


class GenerationSettingRequest(BaseModel):
    """生成性能设置。"""
    fp_concurrency: int = Field(
        ge=GEN_CONCURRENCY_MIN, le=GEN_CONCURRENCY_MAX,
        description="FP 分片生成并发度（1=串行回退，2-5=受控并发）",
    )


async def get_gen_fp_concurrency(session: AsyncSession) -> int:
    """读取 FP 子批并发度（kb_settings KV；未设置/非法值时回退 config 默认）。

    供 orchestrator 在每次生成任务启动时调用——设置页改完立即对下一次生成生效，
    无需重启。
    """
    try:
        result = await session.execute(
            select(KBSetting).where(KBSetting.key == GEN_CONCURRENCY_SETTING_KEY)
        )
        row = result.scalar_one_or_none()
        if row is not None and row.value:
            v = int(row.value)
            if GEN_CONCURRENCY_MIN <= v <= GEN_CONCURRENCY_MAX:
                return v
    except (ValueError, TypeError):
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[settings] 读取生成并发配置失败（用默认值）: {}", str(e)[:100])
    return int(getattr(settings, "GEN_FP_CONCURRENCY", 3) or 3)


router = APIRouter()


# ============================================================================
# 错误分类与用户友好提示
# ============================================================================

# 每个供应商的 API Key 预期前缀（用于前端输入校验提示）
PROVIDER_KEY_PREFIXES = {
    "deepseek": "sk-",
    "openai": "sk-",
    "qwen": "sk-",
    "glm": "",
    "claude": "sk-ant-",
}


def classify_llm_error(e: Exception, provider: str, model: str = "") -> str:
    """将 LLM 异常分类为中文可操作提示。

    返回格式: "错误分类: 用户可操作的修复提示"
    """
    err_str = str(e).lower()

    # 1. 认证错误（API Key 无效）
    if any(kw in err_str for kw in ("authentication", "unauthorized", "invalid api key",
                                     "invalid_key", "401", "403", "permission denied",
                                     "access denied", "forbidden")):
        hints = {
            "deepseek": "请检查 DeepSeek API Key 是否正确（格式: sk-开头），可到 https://platform.deepseek.com 获取",
            "openai": "请检查 OpenAI API Key 是否正确（格式: sk-开头），可到 https://platform.openai.com 获取",
            "qwen": "请检查通义千问 API Key 是否正确（格式: sk-开头），可到 https://dashscope.aliyun.com 获取",
            "glm": "请检查 GLM API Key 是否正确，可到 https://open.bigmodel.cn 获取",
            "claude": "请检查 Claude API Key 是否正确（格式: sk-ant-开头），可到 https://console.anthropic.com 获取",
        }
        hint = hints.get(provider, "请检查 API Key 是否正确，注意区分大小写和前后空格")
        return f"认证失败: {hint}"

    # 2. 模型不存在或不可用
    if any(kw in err_str for kw in ("model not found", "model_not_found", "not found",
                                     "no such model", "unknown model", "404")):
        return (
            f"模型不存在: 请检查「模型名称」字段是否填写正确。"
            f"当前填写的是「{model}」。"
            f"可查看供应商文档确认正确的模型名。"
        )

    # 3. API 地址错误
    if any(kw in err_str for kw in ("connection", "connect", "timeout", "refused",
                                     "eof", "name resolution", "dns", "no route",
                                     "resolve", "ssl", "certificate")):
        return (
            f"网络连接失败: 请检查「API 基础地址」是否正确。\n"
            f"- 是否能从当前网络访问该地址？\n"
            f"- 是否需要配置代理？\n"
            f"- 地址末尾是否有多余的空格或符号？"
        )

    # 4. 限流
    if any(kw in err_str for kw in ("rate limit", "rate_limit", "too many requests",
                                     "429", "quota exceeded", "insufficient_quota")):
        return (
            f"请求频率超限: API 调用过于频繁，请稍后重试。\n"
            f"- 检查账户是否有足够余额\n"
            f"- 检查 API 调用限额配置"
        )

    # 5. URL 格式错误
    if "invalid url" in err_str or "malformed" in err_str:
        return (
            f"API 地址格式错误: 请检查「API 基础地址」是否以 https:// 开头且格式完整。\n"
            f"示例: https://api.deepseek.com/v1"
        )

    # 6. 默认兜底
    short_msg = str(e)[:150]
    return (
        f"连接失败（{short_msg}）\n"
        f"请检查:\n"
        f"  1. API Key 是否填写正确（注意不要有多余空格）\n"
        f"  2. API 地址是否能从当前网络访问\n"
        f"  3. 模型名称是否在所选供应商下有效\n"
        f"  4. 网络是否需要代理才能访问外部 API"
    )


def _mask_api_key(api_key: str) -> str:
    """脱敏 API Key（显示前3后4位）。"""
    if not api_key or len(api_key) < 8:
        return "***" if api_key else ""
    return f"{api_key[:3]}***{api_key[-4:]}"


def _is_encrypted(value: str) -> bool:
    """判断字符串是否为 Fernet 加密格式（gAAAAA 开头）。"""
    return value.startswith("gAAAA") if value else False


def _build_item(row: LLMConfigModel) -> LLMConfigItem:
    """从 ORM 行构建脱敏的 LLMConfigItem。"""
    raw_key = row.api_key or ""
    plain_key = ""
    if raw_key:
        if _is_encrypted(raw_key):
            plain_key = decrypt_api_key(raw_key) or ""
        else:
            plain_key = raw_key

    return LLMConfigItem(
        id=row.id,
        provider=row.provider,
        provider_label=row.provider_label or "",
        api_key=_mask_api_key(plain_key) if plain_key else "",
        base_url=row.base_url or "",
        model=row.model or "",
        is_primary=row.is_primary or False,
        enable_thinking=row.enable_thinking or False,
        thinking_params=row.thinking_params or "",
        # v3.5.6: 启用/禁用状态
        enabled=getattr(row, "enabled", True),
        disabled_reason=getattr(row, "disabled_reason", "") or "",
        disabled_by=getattr(row, "disabled_by", "") or "",
        disabled_at=(
            row.disabled_at.isoformat() if getattr(row, "disabled_at", None) else None
        ),
    )


# ============================================================================
# 启动时加载：将 DB 中的配置写入 settings 单例
# ============================================================================

# P1-1: 评审专属 LLM 供应商设置（kb_settings 作通用 KV 复用，key 隔离）
REVIEW_LLM_SETTING_KEY = "review_provider"


async def get_review_llm_provider(session: AsyncSession) -> str | None:
    """读取评审专属 LLM 供应商（供 ReviewAgent 异构路由使用）。

    返回 None 表示未设置，调用方应使用全局主 LLM（同源模式）。
    """
    try:
        result = await session.execute(
            select(KBSetting).where(KBSetting.key == REVIEW_LLM_SETTING_KEY)
        )
        row = result.scalar_one_or_none()
        return row.value if row and row.value else None
    except Exception:
        return None


async def load_db_config_to_settings(session: AsyncSession) -> None:
    """应用启动时调用：从 DB 读取持久化配置。

    遍历所有供应商配置:
    - 解密 API Key（若为加密格式）
    - 自动迁移旧版明文 Key（加密后重写回 DB）
    - 将 is_primary 供应商设为 settings 主配置

    优先级：DB 持久化配置 > .env 文件默认值
    """
    try:
        result = await session.execute(
            select(LLMConfigModel).order_by(LLMConfigModel.id.desc())
        )
        rows = result.scalars().all()
        if not rows:
            return  # 无持久化配置，使用 .env 默认值

        # 迁移并加载所有供应商配置
        all_configs: list[dict] = []
        primary_config: dict | None = None
        # v3.5.6：显式重建禁用缓存（唯一起源，避免残留/遗漏）。
        # 背景：禁用的供应商在上面被 continue 跳过、不进 all_configs，
        # 因此不会经过 _apply_config_to_settings → 缓存无法被填充。
        # 这里在加载起点全量重建：先清空，再把每个禁用行写入缓存。
        from app.core.llm_client import llm_client as _lc_sync
        _lc_sync._db_disabled_providers.clear()

        for row in rows:
            raw_key = row.api_key or ""
            decrypted_key = ""
            # v3.5.6：跳过被禁用的供应商（人工禁用/硬失败自动禁用），不参与主备选择
            if not getattr(row, "enabled", True):
                _lc_sync._db_disabled_providers[row.provider] = {
                    "enabled": False,
                    "reason": (getattr(row, "disabled_reason", "") or "已禁用"),
                }
                logger.info(
                    "[settings] 供应商 {} 处于禁用状态（{}），跳过加载: {}",
                    row.provider,
                    getattr(row, "disabled_by", "") or "未知",
                    (getattr(row, "disabled_reason", "") or "")[:80],
                )
                continue

            if raw_key:
                if _is_encrypted(raw_key):
                    decrypted_key = decrypt_api_key(raw_key) or ""
                else:
                    # 旧版明文 Key → 自动迁移为加密存储
                    logger.info("[settings] 自动迁移明文 API Key: provider={}", row.provider)
                    encrypted = encrypt_api_key(raw_key)
                    if encrypted:
                        row.api_key = encrypted
                        decrypted_key = raw_key
                    else:
                        decrypted_key = raw_key

            cfg = {
                "provider": row.provider,
                "api_key": decrypted_key,
                "base_url": row.base_url or "",
                "model": row.model or "",
                "is_primary": row.is_primary or False,
                "enable_thinking": row.enable_thinking or False,
                "thinking_params": row.thinking_params or "",
                # v3.5.6: 启用/禁用状态（透传给 _apply_config_to_settings 同步缓存）
                "enabled": getattr(row, "enabled", True),
                "disabled_reason": getattr(row, "disabled_reason", "") or "",
            }
            all_configs.append(cfg)

            if cfg["is_primary"] and cfg["api_key"]:
                primary_config = cfg

        # 提交迁移（明文→加密）
        await session.commit()

        # 设置主配置（无 primary 时取最后一个有 Key 的）
        if primary_config is None:
            for cfg in reversed(all_configs):
                if cfg["api_key"]:
                    primary_config = cfg
                    break

        if primary_config:
            settings.LLM_PROVIDER = primary_config["provider"]
            # v3.4.5（E2）：先全量清空，再按 DB 当前内容重写，避免已删除的供应商僵尸残留
            _reset_provider_configs([c["provider"] for c in all_configs])
            # 应用所有供应商配置到 _all_provider_configs（供 llm_client 故障转移使用）
            for cfg in all_configs:
                _apply_config_to_settings(cfg)
            logger.info(
                "[settings] 已从 DB 加载 LLM 配置: 主供应商={}, model={}, 共 {} 个供应商",
                primary_config["provider"], primary_config["model"], len(all_configs),
            )
        else:
            logger.info("[settings] DB 中有配置但无有效 API Key，使用 .env 默认值")

    except Exception as e:
        logger.warning("[settings] 从 DB 加载 LLM 配置失败（使用 .env 默认值）: {}", str(e)[:120])


def _reset_provider_configs(keep: list[str]) -> list[str]:
    """以 DB 为准确认内存中的供应商集合，清空后交由调用方重新写入。

    v3.4.5（自审补漏 E2）：`_apply_config_to_settings` 是**只增不删**的字典写入，
    配置热重载（DB 供应商数变少）时，已从 DB 移除的供应商会残留在
    `settings._all_provider_configs` 里，继续参与 llm_client 的故障转移。

    实证：DB `llm_config` 只剩 3 行，运行时 `available_providers` 却是
    `["qwen","日日新","qwen1","openrouter","火山","qwen2"]`，
    04:00 那次故障转移实际打到了 DB 里已不存在的 `qwen:qwen3.8-max`——
    用着旧 api_key 调一家已经删掉的供应商，失败原因无法解释，排查方向被误导。

    Args:
        keep: 应当保留的 provider 名称列表（通常来自 DB 全量配置）

    Returns:
        被清理掉的残留 provider 列表（供调用方留痕；空列表表示无残留）
    """
    old = getattr(settings, "_all_provider_configs", {}) or {}
    keep_set = set(keep)
    stale = [p for p in old if p not in keep_set]
    if stale:
        logger.info(
            "[settings] 清理已从 DB 移除的供应商配置（防僵尸残留参与故障转移）: {}",
            ", ".join(stale),
        )
    settings._all_provider_configs = {}
    return stale


def _apply_config_to_settings(cfg: dict) -> None:
    """将单个供应商配置写入 settings 单例。"""
    provider = cfg["provider"]

    # 存储所有供应商配置到 _all_provider_configs 映射（供 llm_client 使用）
    if not hasattr(settings, "_all_provider_configs"):
        settings._all_provider_configs = {}
    settings._all_provider_configs[provider] = {
        "api_key": cfg.get("api_key", ""),
        "base_url": cfg.get("base_url", ""),
        "model": cfg.get("model", ""),
        "enable_thinking": cfg.get("enable_thinking", False),
        "thinking_params": cfg.get("thinking_params", ""),
    }

    # v3.5.6：同步禁用状态到 llm_client 的 DB 禁用缓存（候选过滤用）
    try:
        from app.core.llm_client import llm_client as _lc
        if cfg.get("enabled", True):
            _lc._db_disabled_providers.pop(provider, None)
        else:
            _lc._db_disabled_providers[provider] = {
                "enabled": False,
                "reason": cfg.get("disabled_reason", "") or "已禁用",
            }
    except Exception as e:  # noqa: BLE001
        logger.debug("[settings] 同步禁用缓存失败（不阻断）: {}", str(e)[:100])

    # 如果当前 provider 是 is_primary，也写入传统字段
    if cfg.get("is_primary", False):
        settings.LLM_PROVIDER = provider
        if provider == "deepseek":
            settings.DEEPSEEK_API_KEY = cfg.get("api_key", "")
            if cfg.get("base_url"):
                settings.DEEPSEEK_BASE_URL = cfg["base_url"]
            if cfg.get("model"):
                settings.DEEPSEEK_MODEL = cfg["model"]
        elif provider == "openai":
            settings.OPENAI_API_KEY = cfg.get("api_key", "")
            if cfg.get("base_url"):
                settings.OPENAI_BASE_URL = cfg["base_url"]
            if cfg.get("model"):
                settings.OPENAI_MODEL = cfg["model"]


# ============================================================================
# 1. 获取 LLM 配置（多供应商 + 脱敏）
# ============================================================================

@router.get("/llm", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_llm_config(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取所有 LLM 供应商配置（API Key 脱敏）。

    返回:
    - providers: 数据库中的供应商列表（已脱敏）
    - builtin_providers: 内置供应商预设 + Key 前缀提示（用于前端输入校验）
    """
    result = await session.execute(
        select(LLMConfigModel).order_by(LLMConfigModel.is_primary.desc(), LLMConfigModel.id.desc())
    )
    rows = result.scalars().all()

    providers = [_build_item(r) for r in rows] if rows else []

    data = LLMConfigResponse(
        providers=providers,
        builtin_providers=BUILTIN_PROVIDERS,
    )

    return ApiResponse(success=True, message="ok", data=data.model_dump())


# ============================================================================
# 2. 更新 LLM 配置（批量 + 加密持久化 + 去零兼容）
# ============================================================================

@router.put("/llm", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def update_llm_config(
    req: LLMConfigRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """全量更新 LLM 供应商配置。

    策略：
    - 接收供应商列表，全量替换 DB 中的配置
    - 新写入的 API Key 使用 Fernet 加密
    - 自动识别内置供应商并填充预设 base_url / model
    - 未提供 API Key 的条目保留数据库中原有值（如有）
    - 前端传入脱敏格式("***")的 Key 视为未修改，跳过更新

    Args:
        req: LLMConfigRequest — providers 列表

    Returns:
        ApiResponse: 更新后的供应商配置（脱敏）
    """
    if not req.providers:
        raise HTTPException(status_code=400, detail="至少需要一个供应商配置")

    # 0. 查询旧配置，用于保留未修改的脱敏 Key
    old_result = await session.execute(select(LLMConfigModel))
    old_rows = old_result.scalars().all()
    old_configs: dict[str, LLMConfigModel] = {
        row.provider: row for row in old_rows if row.provider
    }

    # 重名校验（provider 字段有 unique 约束）
    seen_providers: set[str] = set()
    for item in req.providers:
        provider_name = item.provider.strip() or "custom"
        if provider_name in seen_providers:
            raise HTTPException(
                status_code=400,
                detail=f"供应商名称「{provider_name}」重复，请修改后保存",
            )
        seen_providers.add(provider_name)

    # 1. 删除旧配置
    await session.execute(delete(LLMConfigModel))

    # 2. 写入新配置
    # v3.4.5（自审补漏 E3）：主供应商**以请求体显式提交的 is_primary 为准**，
    # 不再用「列表第一项」隐式判定。
    #   原因：前端「设为主供应商」只改布尔值、不改数组顺序（Settings.tsx:setPrimary），
    #   旧的 `idx == 0` 判据会让该按钮永远失效——保存后重新打开，主供应商还是原来那家。
    #   归一化：多个 is_primary=true 时取第一个并静默收敛；一个都没有时回退到第一项。
    marked = [i for i, it in enumerate(req.providers) if it.is_primary]
    if len(marked) > 1:
        logger.warning(
            "[API/settings] 提交了 {} 个 is_primary=true，归一化取第一个（{}）",
            len(marked), (req.providers[marked[0]].provider or "").strip() or "custom",
        )
    elif not marked:
        logger.info(
            "[API/settings] 未提交任何 is_primary=true，回退第一项为主供应商（{}）",
            (req.providers[0].provider or "").strip() or "custom",
        )
    primary_idx = marked[0] if marked else 0

    new_records: list[LLMConfigModel] = []

    for idx, item in enumerate(req.providers):
        provider = item.provider.strip() or "custom"
        base_url = item.base_url.strip()
        model = item.model.strip()
        api_key = item.api_key.strip()

        # 如果是内置供应商且未填 base_url/model，自动填充预设
        if provider in BUILTIN_PROVIDERS:
            preset = BUILTIN_PROVIDERS[provider]
            if not base_url:
                base_url = preset["base_url"]
            if not model:
                model = preset["model"]

        # 加密 API Key
        encrypted_key = ""
        if api_key:
            # 如果是脱敏格式（含 ***），说明前端未修改 → 保留旧配置的加密 Key
            if "***" in api_key:
                old_row = old_configs.get(provider)
                encrypted_key = old_row.api_key if old_row else ""
            else:
                encrypted_key = encrypt_api_key(api_key)
                if not encrypted_key:
                    raise HTTPException(status_code=500, detail=f"API Key 加密失败: {provider}")

        record = LLMConfigModel(
            provider=provider,
            provider_label=item.provider_label.strip() or provider,
            api_key=encrypted_key,
            base_url=base_url,
            model=model,
            # v3.4.5（E3）：以请求体提交的 is_primary 为准，而非下标 0
            is_primary=(idx == primary_idx),
            enable_thinking=item.enable_thinking or False,
            thinking_params=(item.thinking_params or "").strip(),
        )
        session.add(record)
        new_records.append(record)

    await session.commit()

    # 3. 更新 settings 单例（即时生效）
    # v3.4.5（E2）：本次是全量替换（DB 已 delete+insert），内存中被移除的供应商必须一并清掉，
    # 否则会继续参与故障转移——调到一家用户已经删掉的供应商上。
    _reset_provider_configs([r.provider for r in new_records])
    for record in new_records:
        cfg = {
            "provider": record.provider,
            "api_key": decrypt_api_key(record.api_key) or "",
            "base_url": record.base_url or "",
            "model": record.model or "",
            "is_primary": record.is_primary or False,
            "enable_thinking": record.enable_thinking or False,
            "thinking_params": record.thinking_params or "",
        }
        _apply_config_to_settings(cfg)

    # 4. 返回更新后的配置
    result = await session.execute(
        select(LLMConfigModel).order_by(LLMConfigModel.is_primary.desc(), LLMConfigModel.id.desc())
    )
    rows = result.scalars().all()
    providers = [_build_item(r) for r in rows]

    # v3.4.5（E3）：主供应商是排查故障转移方向的第一个抓手，必须显式留痕
    primary_rec = next((r for r in new_records if r.is_primary), None)
    logger.info(
        "[API/settings] LLM 配置已更新: 共 {} 个供应商, 主供应商={}",
        len(providers), primary_rec.provider if primary_rec else "无",
    )

    data = LLMConfigResponse(
        providers=providers,
        builtin_providers=BUILTIN_PROVIDERS,
    )

    return ApiResponse(
        success=True,
        message=f"已保存 {len(providers)} 个供应商配置（加密持久化）",
        data=data.model_dump(),
    )


# ============================================================================
# 3. 测试 LLM 连通性（带精确错误分类）
# ============================================================================

@router.post("/llm/test", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def test_llm_connection(
    provider_name: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """测试指定供应商的 LLM 连通性。

    从 DB 直接查询供应商的完整配置（含解密后的 API Key），
    使用正确的 base_url / model / api_key 发起测试调用，
    返回精确分类的友好错误提示。

    外层 ApiResponse 始终返回 success=True（避免前端 request() 吞掉详细提示），
    测试结果本身在 data.success 中。

    Args:
        provider_name: 要测试的供应商标识（留空则测试主供应商）

    Returns:
        ApiResponse: data 中含 LLMTestResponse（success / message / latency_ms）
    """
    # 1. 从 DB 查询所有供应商
    result = await session.execute(
        select(LLMConfigModel).order_by(LLMConfigModel.is_primary.desc(), LLMConfigModel.id.desc())
    )
    rows = result.scalars().all()

    if not rows:
        # DB 无配置 → 回退到读 settings（.env 配置）
        fallback = settings.llm_config
        if fallback.get("api_key"):
            # 用 .env 配置测试
            logger.info("[API/settings] DB 无配置，使用 .env 兜底测试: provider={}, model={}",
                        settings.LLM_PROVIDER, fallback.get("model"))
            return await _do_test(
                provider=settings.LLM_PROVIDER,
                model=fallback.get("model", ""),
                base_url=fallback.get("base_url", ""),
                api_key=fallback["api_key"],
                label=f"{settings.LLM_PROVIDER}（.env 配置）",
            )
        return ApiResponse(
            success=True,
            message="测试完成（未配置）",
            data=LLMTestResponse(
                success=False, latency_ms=0, provider=provider_name or "",
                model="",
                message="⚠️ 未配置任何 LLM 供应商\n\n发现你已通过 .env 文件配置了 API Key，"
                        "但尚未在 UI 设置页保存。\n\n解决办法：前往设置页，添加供应商配置后"
                        "点击「保存」即可。",
            ).model_dump(),
        )

    # 找到目标供应商
    target: LLMConfigModel | None = None
    if provider_name:
        for row in rows:
            if row.provider == provider_name:
                target = row
                break
    else:
        for row in rows:
            if row.is_primary:
                target = row
                break
        if not target:
            for row in rows:
                if row.api_key:
                    target = row
                    break

    if not target:
        return ApiResponse(
            success=True,
            message="测试完成（供应商不存在）",
            data=LLMTestResponse(
                success=False, latency_ms=0, provider=provider_name or "",
                model="",
                message=f"⚠️ 未找到供应商「{provider_name or '主供应商'}」\n\n解决办法：请在设置页面添加该供应商后保存，再点击「测试连接」。",
            ).model_dump(),
        )

    # 2. 解密 API Key
    raw_key = target.api_key or ""
    api_key = ""
    if raw_key:
        if _is_encrypted(raw_key):
            api_key = decrypt_api_key(raw_key) or ""
        else:
            api_key = raw_key

    provider = target.provider
    model = target.model or ""
    base_url = target.base_url or ""
    label = target.provider_label or provider

    if not api_key:
        return ApiResponse(
            success=True,
            message=f"「{label}」测试完成（Key 为空）",
            data=LLMTestResponse(
                success=False, latency_ms=0, provider=provider, model=model,
                message=(
                    f"⚠️ 「{label}」未配置 API Key\n\n"
                    f"解决办法：点击「修改配置」按钮，在 API Key 字段中输入 {provider.upper()} 的密钥。\n"
                    f"格式要求：{PROVIDER_KEY_PREFIXES.get(provider, '无特定前缀')}xxx\n"
                    f"获取地址：可到对应供应商的控制台申请。"
                ),
            ).model_dump(),
        )

    logger.info("[API/settings] 测试 LLM 连通性: provider={}, model={}, base_url={}",
                provider, model, base_url)

    return await _do_test(provider=provider, model=model, base_url=base_url,
                          api_key=api_key, label=label)


async def _do_test(provider: str, model: str, base_url: str,
                   api_key: str, label: str) -> ApiResponse:
    """执行实际的 LLM 测试调用（抽取为辅助函数消除重复）。"""
    import litellm
    start_time = time.time()
    try:
        custom_provider = "anthropic" if provider == "claude" else "openai"
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个测试助手。"},
                {"role": "user", "content": "请只回复 ok"},
            ],
            "api_key": api_key,
            "api_base": base_url,
            "custom_llm_provider": custom_provider,
            "temperature": 0.0,
            "max_tokens": 10,
        }
        response = await litellm.acompletion(**kwargs)
        content = response.choices[0].message.content or ""
        latency_ms = int((time.time() - start_time) * 1000)

        data = LLMTestResponse(
            success=True, latency_ms=latency_ms,
            provider=provider, model=model,
            message=f"✅ 连通正常（{latency_ms}ms），响应: {content[:100]})",
        )
        return ApiResponse(
            success=True,
            message=f"「{label}」连通成功（{latency_ms}ms）",
            data=data.model_dump(),
        )

    except Exception as e:
        latency_ms = int((time.time() - start_time) * 1000)
        hint = classify_llm_error(e, provider, model)

        data = LLMTestResponse(
            success=False, latency_ms=latency_ms,
            provider=provider, model=model,
            message=hint,
        )
        return ApiResponse(
            success=True,
            message=f"「{label}」连接失败",
            data=data.model_dump(),
        )


# ============================================================================
# P1-1: 评审专属 LLM 供应商（异构路由机制；选型由用户在设置页/调用方决定）
# ============================================================================

@router.get("/review-llm", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_review_llm_config(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取评审专属 LLM 供应商（None/空 = 未启用异构，跟随全局主力）。"""
    provider = await get_review_llm_provider(session)
    available = list((getattr(settings, "_all_provider_configs", {}) or {}).keys())
    return ApiResponse(success=True, message="ok", data={
        "provider": provider or "",
        "available_providers": available,
    })


@router.put("/review-llm", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def set_review_llm_config(
    req: ReviewLLMSettingRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """设置/清除评审专属 LLM 供应商。

    provider 为空字符串表示清除（评审跟随全局主力，同源模式）。
    指定供应商调用失败时 ReviewAgent 自动回退主力池，异构是尽力而为。
    """
    provider = (req.provider or "").strip()
    if provider:
        all_configs = getattr(settings, "_all_provider_configs", {}) or {}
        if provider not in all_configs:
            raise HTTPException(status_code=400, detail=f"供应商 {provider} 未配置")
    result = await session.execute(
        select(KBSetting).where(KBSetting.key == REVIEW_LLM_SETTING_KEY)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = KBSetting(key=REVIEW_LLM_SETTING_KEY, value=provider)
        session.add(row)
    else:
        row.value = provider
    await session.commit()
    return ApiResponse(success=True, message="评审 LLM 供应商已更新" if provider else "评审 LLM 已恢复跟随主力",
                       data={"provider": provider})


# ============================================================================
# v3.5.5（docs/22 方案①）：生成性能设置（FP 子批并发开关）
# ============================================================================

@router.get("/generation", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_generation_settings(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取生成性能设置（当前并发度 + 合法区间，供设置页展示）。"""
    current = await get_gen_fp_concurrency(session)
    return ApiResponse(success=True, message="ok", data={
        "fp_concurrency": current,
        "min": GEN_CONCURRENCY_MIN,
        "max": GEN_CONCURRENCY_MAX,
    })


@router.put("/generation", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def set_generation_settings(
    req: GenerationSettingRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """设置 FP 子批并发度（立即生效，无需重启；下一次生成任务读取新值）。

    1 = 串行回退（与 v3.5.4 行为一致）；2-5 = 受控并发（Semaphore 限流）。
    """
    await session.execute(
        delete(KBSetting).where(KBSetting.key == GEN_CONCURRENCY_SETTING_KEY)
    )
    session.add(KBSetting(key=GEN_CONCURRENCY_SETTING_KEY, value=str(req.fp_concurrency)))
    await session.commit()
    return ApiResponse(success=True,
                       message=f"生成并发度已更新为 {req.fp_concurrency}"
                               + ("（串行模式）" if req.fp_concurrency == 1 else ""),
                       data={"fp_concurrency": req.fp_concurrency})


# ============================================================================
# v3.5.6：供应商启用/禁用（持久化）+ 管理员通知
# ============================================================================

class ProviderEnabledRequest(BaseModel):
    """供应商启停请求。"""
    enabled: bool = Field(..., description="True=启用，False=禁用")


@router.put("/llm/{provider_name}/enabled", response_model=ApiResponse,
            dependencies=[Depends(require_admin)])
async def set_provider_enabled_api(
    provider_name: str,
    req: ProviderEnabledRequest,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """启用/禁用单个 LLM 供应商（管理员手动操作，立即生效）。

    - 禁用：disabled_by='manual'，reason='人工禁用'；该供应商退出候选，不参与调用；
    - 启用：清空禁用标记恢复候选；需已有 api_key；
    - 若禁用的是主供应商，主标记自动让给下一个启用中的供应商。
    """
    from app.core.provider_admin import set_provider_enabled
    ok = await set_provider_enabled(provider_name, req.enabled)
    if not ok:
        detail = "供应商不存在或无 API Key（启用需先配置 Key）"
        raise HTTPException(status_code=400, detail=detail)
    return ApiResponse(
        success=True,
        message=f"供应商 {provider_name} 已{'启用' if req.enabled else '禁用（人工禁用）'}",
        data={"provider": provider_name, "enabled": req.enabled},
    )


@router.get("/notifications", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def list_notifications(
    only_unread: bool = False,
    page: int = 1,
    page_size: int = 20,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """获取管理员通知列表（登录即可见；未读数供铃铛展示）。"""
    from app.db.models import AdminNotification
    # v4.1.8：分页参数夹取（与 utils/pagination 同一约定 page≥1、page_size∈[1,100]）；
    # 计数改 COUNT——原实现把全表 id 拉进内存再 len()，通知表只增不减会越来越慢
    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 20)))
    q = select(AdminNotification).order_by(AdminNotification.created_at.desc())
    if only_unread:
        q = q.where(AdminNotification.is_read.is_(False))
    total = (await session.execute(
        select(func.count(AdminNotification.id))
    )).scalar() or 0
    rows = (await session.execute(
        q.offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    unread = (await session.execute(
        select(func.count(AdminNotification.id)).where(AdminNotification.is_read.is_(False))
    )).scalar() or 0
    items = [{
        "id": r.id, "type": r.type, "title": r.title, "content": r.content,
        "related_type": r.related_type, "related_id": r.related_id,
        "is_read": r.is_read, "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]
    return ApiResponse(success=True, message="ok", data={
        "items": items, "total": total, "unread": unread,
        "page": page, "page_size": page_size,
    })


@router.post("/notifications/read", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def mark_notifications_read(
    ids: list[int] | None = None,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse:
    """标记通知已读（ids 为空 = 全部已读）。"""
    from app.db.models import AdminNotification
    q = select(AdminNotification).where(AdminNotification.is_read.is_(False))
    if ids:
        q = q.where(AdminNotification.id.in_(ids))
    rows = (await session.execute(q)).scalars().all()
    for r in rows:
        r.is_read = True
    await session.commit()
    return ApiResponse(success=True, message=f"已标记 {len(rows)} 条通知为已读",
                       data={"marked": len(rows)})
