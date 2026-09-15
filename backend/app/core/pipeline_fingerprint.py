"""断点续跑的输入指纹（docs/15 §4.2 第 3/4 条）。

要解决的问题：
    断点续跑原本只看"哪个任务被中断了"，不看**输入是否还是同一份**。
    于是"改完需求再续跑"会把旧用例拼到新需求上；模型/prompt/schema 换代后
    旧 checkpoint 的产物也不再兼容，却照样被复用。

做法：checkpoint 写入时附带输入指纹；续跑前重算并逐字段比对，任何一项不同都
**拒绝续跑（HTTP 409）**，只有显式 `fallback_to_restart=true` 才允许从头开始。

指纹字段（照 docs/15 §4.2 建议）：
    req_id / product_id / revision / srr_sha256 / fp_set_sha256 /
    prompt_version / model / schema_version

设计取舍：**不用 LLM、不查 Milvus，全部可离线复算**——指纹必须是廉价的，
否则"每次续跑都算一遍"没人愿意做。model 标签从 settings 组合（DB 配置在启动时
已灌进 settings），不额外查库。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# 用例可执行合同的口径版本：合同变了，旧 checkpoint 的产物不能再当"已完成"复用
CASE_CONTRACT_VERSION = "case-contract-v1"

# 指纹里参与比对的字段（顺序即展示顺序）
FINGERPRINT_FIELDS = (
    "req_id", "product_id", "revision", "srr_sha256", "fp_set_sha256",
    "prompt_version", "model", "schema_version",
)

# 中文说明（409 响应里给人看）
FIELD_LABELS = {
    "req_id": "需求 ID",
    "product_id": "产品",
    "revision": "需求修订号",
    "srr_sha256": "SRR 内容",
    "fp_set_sha256": "功能点集合",
    "prompt_version": "提示词版本",
    "model": "模型",
    "schema_version": "用例合同版本",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def srr_sha256(srr: dict | None) -> str:
    """SRR 内容 hash（键序无关）。"""
    normalized = json.dumps(srr or {}, ensure_ascii=False, sort_keys=True)
    return _sha256(normalized)


def fp_set_sha256(functional_points: list[dict] | None) -> str:
    """功能点集合 hash：只看 id 集合的排序结果（名称/优先级变化不影响"分片集合"）。"""
    ids = sorted(str(fp.get("id", "")) for fp in (functional_points or []) if fp.get("id"))
    return _sha256("|".join(ids))


def prompt_version() -> str:
    """当前生效的生成提示词版本（prompt_store override 优先，取不到用内置默认）。

    只取 hash 前 12 位：它只需要"能不能区分两个版本"，不需要可逆。
    """
    text = ""
    try:
        from app.db import prompt_store

        text = prompt_store.get_prompt("gen_agent", "SYSTEM_PROMPT") or ""
    except Exception:  # noqa: BLE001
        text = ""
    if not text:
        try:
            from app.prompts import gen_agent as gen_prompts

            text = str(getattr(gen_prompts, "SYSTEM_PROMPT", "") or "")
        except Exception:  # noqa: BLE001
            text = ""
    return _sha256(text)[:12] if text else "unknown"


def current_model_tag() -> str:
    """provider:model 组合标签（不查库；settings 在启动时已由 DB 配置灌入）。"""
    try:
        from app.config import settings

        provider = str(getattr(settings, "LLM_PROVIDER", "") or "")
        model = str(getattr(settings, f"{provider.upper()}_MODEL", "") or "") if provider else ""
        if not model:
            model = str(getattr(settings, "DEEPSEEK_MODEL", "") or "")
        return f"{provider}:{model}" if provider or model else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def build_fingerprint(
    *,
    req_id: str,
    product_id: str = "",
    revision: Any = None,
    srr: dict | None = None,
    functional_points: list[dict] | None = None,
) -> dict:
    """组装当前输入指纹（纯函数，除了 prompt_version 会读一次 prompt_store）。"""
    return {
        "req_id": str(req_id or ""),
        "product_id": str(product_id or ""),
        "revision": str(revision if revision is not None else ""),
        "srr_sha256": srr_sha256(srr),
        "fp_set_sha256": fp_set_sha256(functional_points),
        "prompt_version": prompt_version(),
        "model": current_model_tag(),
        "schema_version": CASE_CONTRACT_VERSION,
    }


def compare_fingerprints(stored: dict | None, current: dict) -> tuple[bool, list[dict]]:
    """比对指纹。返回 (是否一致, 差异列表)。

    `stored` 为空（老 checkpoint 没有指纹）→ 视为**不可验证**：按不一致处理，
    避免"老数据静默续跑"。调用方可选择 fallback_to_restart 从头开始。
    """
    if not stored:
        return False, [{"field": "fingerprint", "stored": "（无指纹）", "current": "已补全",
                        "label": "输入指纹"}]
    diffs = []
    for field in FINGERPRINT_FIELDS:
        old, new = stored.get(field), current.get(field)
        if str(old if old is not None else "") != str(new if new is not None else ""):
            diffs.append({
                "field": field,
                "label": FIELD_LABELS.get(field, field),
                "stored": _short(old),
                "current": _short(new),
            })
    return (not diffs), diffs


def _short(value: Any, limit: int = 24) -> str:
    text = str(value if value is not None else "")
    return text if len(text) <= limit else f"{text[:limit]}…"


def mismatch_message(diffs: list[dict]) -> str:
    """给人看的 409 说明。"""
    if not diffs:
        return "输入指纹不一致"
    if diffs[0].get("field") == "fingerprint":
        return ("该断点没有输入指纹（旧版本写入），无法证明它对应同一份输入，已拒绝续跑。"
                "如需重跑请带 fallback_to_restart=true。")
    parts = [f"{d['label']}（checkpoint={d['stored']} → 当前={d['current']}）" for d in diffs]
    return ("输入已变化，拒绝续跑以免把旧产物拼到新输入上：" + "；".join(parts)
            + "。如需从头重跑请带 fallback_to_restart=true。")


async def load_current_fingerprint(session, requirement, srr_json: dict | None) -> dict:
    """从库中取功能点后组装指纹（续跑校验用）。"""
    from sqlalchemy import select

    from app.db.models import FunctionalPoint

    fps = (await session.execute(
        select(FunctionalPoint).where(FunctionalPoint.requirement_id == requirement.id)
    )).scalars().all()
    return build_fingerprint(
        req_id=requirement.id,
        product_id=getattr(requirement, "product_id", ""),
        revision=getattr(requirement, "revision", None),
        srr=srr_json or {},
        functional_points=[{"id": fp.id} for fp in fps],
    )
