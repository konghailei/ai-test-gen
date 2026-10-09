# -*- coding: utf-8 -*-
"""LLM 供应商管理辅助（v3.5.6）。

职责：
1. 持久化禁用/启用供应商（写 `llm_config.enabled` / disabled_* 字段）；
2. 写管理员通知（`admin_notifications`）；
3. 主供应商被禁用时，自动把主标记让渡给下一个启用中的供应商。

**为什么单独成模块**：`llm_client`（底层调用）需要写库与写通知，但直接 import
`app.api.settings` 会在部分导入顺序下形成环；本模块只依赖 db/models，可被
llm_client 与 api/settings 双向安全引用。

所有函数均"不阻断主流程"：写库/通知失败只 warning，不影响 LLM 调用本身。
"""
from __future__ import annotations

from sqlalchemy import select

from app.db.database import async_session
from app.db.models import AdminNotification, LLMConfig
from app.utils.logger import logger

# 通知类型
NOTIFY_PROVIDER_DISABLED = "llm_provider_disabled"
NOTIFY_ALL_PROVIDERS_FAILED = "llm_all_providers_failed"


async def disable_provider_persistently(provider_name: str, reason: str) -> None:
    """持久化禁用某供应商（系统硬失败触发），并写管理员通知。

    - `enabled=False`、`disabled_by='system'`、`disabled_reason=reason`、`disabled_at=now`；
    - 若被禁用的是主供应商（is_primary=True），自动把主标记让给下一个启用中的供应商
      （按 id 顺序取第一个 enabled=True 的），避免主指向一个禁用家；
    - 写一条 `admin_notifications`（type=llm_provider_disabled），供管理员排查。
    """
    try:
        async with async_session() as s:
            row = (await s.execute(
                select(LLMConfig).where(LLMConfig.provider == provider_name)
            )).scalar_one_or_none()
            if row is None:
                logger.warning("[ProviderAdmin] 供应商不存在，跳过持久化禁用: {}", provider_name)
                return
            if not row.enabled:
                # 已被禁用（人工或系统）→ 不重复写通知
                return
            from datetime import datetime, timezone
            row.enabled = False
            row.disabled_reason = (reason or "硬失败")[:500]
            row.disabled_by = "system"
            row.disabled_at = datetime.now(timezone.utc)
            was_primary = bool(row.is_primary)

            # 主标记让渡：被禁用的主供应商 → 让给下一个启用中的
            new_primary_name = ""
            if was_primary:
                row.is_primary = False
                others = (await s.execute(
                    select(LLMConfig).where(
                        LLMConfig.provider != provider_name,
                        LLMConfig.enabled.is_(True),
                    ).order_by(LLMConfig.id)
                )).scalars().all()
                for o in others:
                    if (o.api_key or "").strip():
                        o.is_primary = True
                        new_primary_name = o.provider
                        break

            # 写管理员通知
            s.add(AdminNotification(
                type=NOTIFY_PROVIDER_DISABLED,
                title=f"LLM 供应商「{row.provider_label or provider_name}」已被禁用",
                content=(
                    f"系统检测到供应商 {provider_name} 出现硬失败（{reason or '未知原因'}），"
                    f"已自动禁用，需人工排查后在「系统设置 → LLM 供应商」手动重新启用。"
                    + (f" 主供应商已自动切换为 {new_primary_name}。" if new_primary_name else "")
                ),
                related_type="llm_provider",
                related_id=provider_name,
            ))
            await s.commit()
            logger.warning(
                "[ProviderAdmin] 已持久化禁用供应商 {}（原因={}）；主标记让渡={}",
                provider_name, reason, new_primary_name or "无",
            )
            # v3.5.6：同步内存禁用缓存（统一入口——无论调用方是 llm_client 还是 API，
            # 禁用态都立刻在当前进程生效，无需等配置重载）
            try:
                from app.core.llm_client import llm_client as _lc
                _lc._db_disabled_providers[provider_name] = {
                    "enabled": False, "reason": reason or "硬失败",
                }
            except Exception:  # noqa: BLE001
                pass
            # 刷新内存中的供应商配置（主标记/禁用态立即对后续调用生效）
            try:
                from app.api.settings import load_db_config_to_settings
                await load_db_config_to_settings(s)
            except Exception as e:  # noqa: BLE001
                logger.warning("[ProviderAdmin] 刷新 settings 失败（下次调用/重启后生效）: {}", str(e)[:120])
    except Exception as e:  # noqa: BLE001
        logger.warning("[ProviderAdmin] 持久化禁用失败（不阻断）: {}", str(e)[:150])


async def notify_all_providers_failed(attempted: int, detail: str, agent_name: str = "") -> None:
    """写一条"所有供应商调用失败"通知（软失败耗尽，非禁用）。

    v3.5.6：大哥要求"都通知，方便及时排查"——硬失败禁用与全败耗尽都发通知。
    为避免通知刷屏，同一分钟内同类通知只写一条（粗粒度去重）。
    """
    try:
        async with async_session() as s:
            from datetime import datetime, timedelta, timezone
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
            recent = (await s.execute(
                select(AdminNotification).where(
                    AdminNotification.type == NOTIFY_ALL_PROVIDERS_FAILED,
                    AdminNotification.created_at >= cutoff,
                ).limit(1)
            )).scalar_one_or_none()
            if recent is not None:
                return  # 一分钟内已有同类通知，去重
            s.add(AdminNotification(
                type=NOTIFY_ALL_PROVIDERS_FAILED,
                title="LLM 供应商全部调用失败（已重试 3 轮）",
                content=(
                    f"已尝试 {attempted} 家供应商并等待重试 2 轮后仍全部失败"
                    + (f"（调用方：{agent_name}）" if agent_name else "")
                    + f"。明细：{detail[:400]}。请管理员排查供应商连通性/额度/Key。"
                ),
                related_type="llm_provider",
                related_id="",
            ))
            await s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("[ProviderAdmin] 写全败通知失败（不阻断）: {}", str(e)[:120])


async def set_provider_enabled(provider_name: str, enabled: bool, reason: str = "") -> bool:
    """管理员手动启用/禁用供应商（供设置页调用）。

    - 手动禁用：disabled_by='manual'，reason 固定为"人工禁用"（忽略传入）；
    - 手动启用：清空 disabled_reason/by/at，enabled=True；
    - 手动启用时若该供应商无 api_key 则拒绝（返回 False）。

    Returns:
        bool: 是否操作成功
    """
    try:
        async with async_session() as s:
            row = (await s.execute(
                select(LLMConfig).where(LLMConfig.provider == provider_name)
            )).scalar_one_or_none()
            if row is None:
                return False
            from datetime import datetime, timezone
            if enabled:
                if not (row.api_key or "").strip():
                    return False
                row.enabled = True
                row.disabled_reason = ""
                row.disabled_by = ""
                row.disabled_at = None
            else:
                row.enabled = False
                row.disabled_reason = "人工禁用"
                row.disabled_by = "manual"
                row.disabled_at = datetime.now(timezone.utc)
            await s.commit()
            # v3.5.6：同步内存禁用缓存（手动启停立即生效）
            try:
                from app.core.llm_client import llm_client as _lc
                if enabled:
                    _lc._db_disabled_providers.pop(provider_name, None)
                else:
                    _lc._db_disabled_providers[provider_name] = {
                        "enabled": False, "reason": "人工禁用",
                    }
            except Exception:  # noqa: BLE001
                pass
            # 刷新内存配置（禁用态与主标记立即生效）
            try:
                from app.api.settings import load_db_config_to_settings
                await load_db_config_to_settings(s)
            except Exception as e:  # noqa: BLE001
                logger.warning("[ProviderAdmin] 刷新 settings 失败: {}", str(e)[:120])
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[ProviderAdmin] 手动启停失败: {}", str(e)[:150])
        return False
