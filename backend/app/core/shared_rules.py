# -*- coding: utf-8 -*-
"""跨功能点共享业务规则 → 逐条独立向量的编译层（v2.48）。

## 背景（为什么需要这个模块）

v2.34 起，「跨功能点共享业务规则」从 SRR 顶层 `business_rules` 迁到
`cross_fp_interactions.shared_business_rules`。v4.1.1 的 T4 修复已让
`milvus_store._srr_to_text()` 把该块渲染进需求级向量文本，但**仍是整块拼接**：

    共享业务规则: 基于角色的菜单权限控制…; 用户密码加盐哈希存储，登录连续失败5次锁定30分钟; 会话令牌有效期8小时; …（12 条混成一段）

整块拼接会造成**单条规则语义被稀释**。实测（bge-large-zh-v1.5，同一问题
「登录连续失败几次锁定？锁定多久？」的余弦相似度）：

    - 单条规则（17 字）        → 0.8394
    - 整块 CFI（12 条 287 字） → 0.4876
    - 整份需求文档（3184 字）  → 0.3299
    - KB 问答相关度门槛 KB_QA_MIN_SIMILARITY = 0.50

→ 规则明明在库里，却因相似度 0.49 < 0.50 被门槛滤除，问答答「知识库中未找到」。
（自证第三轮 R1/R2/R3 实测：Q1 文件格式 top-1 0.71 命中；Q2 登录锁定
`kb_status.filtered_low_score` = 5/5 全被滤。）

## 本模块做什么

把 `cross_fp_interactions` 下**逐条即一条规则**的子块（`shared_business_rules`
共享业务规则 + `global_constraints` 全局约束）**逐条**渲染为独立向量条目
（doc_type=`shared_rule`），每条规则一个向量，语义不再被邻居稀释
（单条相似度回到 0.71~0.84 量级）。

设计要点与 CRSR（`app/core/fp_crsr.py`）对称：
  - 确定性向量 ID：`(product_id, 规范化规则文本)` → 同一规则跨多次归档/重算
    upsert 始终同一条，不产生版本堆积；
  - 出箱投递复用 `requirement` 通道（`add_requirement`，SRR 同构，
    `_srr_to_text` 同口径）；metadata.type=`shared_rule` 由 payload 带入，
    `add_requirement` 会自动同步 doc_type；
  - metadata 必含 `status="archived"`（KB 检索按 status=archived 硬过滤）；
  - `reconcile_shared_rules()` 做「规则向量 ⇄ 仍归档需求集合」的对账：
    归档时重算，恢复活动后再算一次实现自愈，并删除已消失规则的过期条目；
  - `filter_shared_rule_switch()` 是灰度开关 `KB_SHARED_RULE_ENABLED` 的**唯一
    过滤实现**（kqa 与 clarify 两处共用，防「各写一份」drift 复发）。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timezone

from app.utils.logger import logger

# 参与「逐条成向量」的 CFI 子块。
# v2.48：先只做共享业务规则；实测发现 global_constraints（全局约束）同样是
# 「一条一个数值/规则」的形态，整块拼接后单条相似度一样被稀释——例如
# 「事件循环采样间隔为100ms」单独成条 0.7143，混在整块里会掉到 0.535，
# 而权威表述还在需求级文档（0.4871 < 门槛 0.50）→ 问答答「未提及采样间隔」。
# 故一并纳入逐条编译。
RULE_BLOCK_KEYS = ("shared_business_rules", "global_constraints")

# 解析器的「元注释」标记：这类条目不是规则本身，而是对文档的评论
# （如「所有接口：请求频率不做额外限制（原文未提及）」），
# 当成规则嵌入会污染知识库，故抽取时跳过。
_META_COMMENT_MARKERS = ("原文未提及", "文档未提及", "原文未说明", "文档未说明")


def _norm_rule(text: str) -> str:
    """规则文本规范化（去空白与中英文标点），用于跨版本/跨需求同义规则合并。

    与 fp_crsr._norm_text 同口径，保证「同一规则」在两侧判为同一条。
    """
    if not isinstance(text, str):
        return ""
    return re.sub(r"[\s\u3000「」【】()（）:：,，.。;；、\-—~～]", "", text)


# 兜底拼接时应排除的「非正文」键（纯标识/分类字段，拼进去只会产生噪声规则）
_META_ONLY_KEYS = {
    "id", "type", "category", "source", "applies_to", "applied_fps",
    "fp_ids", "fp_id", "priority", "severity", "status",
}


def rule_text_of(item) -> str:
    """从规则条目（dict 或标量）取出规则正文；取不到正文时返回空串。

    注意：dict 形态先试常规正文键；都拿不到时做兜底拼接，但**排除纯标识键**，
    否则形如 `{"id": "BR-9", "rule": "   "}` 的空规则会被拼成 "id:BR-9" 当规则，
    产出噪声条目。
    """
    if isinstance(item, dict):
        for key in ("rule", "rule_desc", "description", "name", "summary"):
            v = item.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        pairs = [
            f"{k}:{v}"
            for k, v in item.items()
            if k not in _META_ONLY_KEYS
            and isinstance(v, (str, int, float))
            and str(v).strip()
        ]
        return "; ".join(pairs)
    if item is None:
        return ""
    return str(item).strip()


def shared_rule_vector_id(product_id: str, rule_text: str) -> str:
    """共享规则向量主键（确定性）。

    与 CRSR 一致：同一 `(product_id, 规范化规则)` 恒为同一条，
    跨多次归档/重算幂等 upsert；变更由出箱 revision/content_hash 追踪。
    """
    raw = f"{product_id}|{_norm_rule(rule_text)}"
    return "SRULE-" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def filter_shared_rule_switch(sources: list[dict], enabled: bool | None = None) -> list[dict]:
    """按 `KB_SHARED_RULE_ENABLED` 开关过滤 shared_rule 条目（kqa 与 clarify 的唯一实现）。

    为什么收敛为单一实现：该开关历史只在 KB 问答侧（knowledge_qa.py）落地，
    而澄清侧（clarify_kb.py）的 doc_type 硬编码含 shared_rule 且**无过滤**——
    配置 docstring 声明「kqa/clarify 两处过滤」，实际只生效一处（**假配置**），
    「一键回退」在澄清链路失效。两处各写一份必然 drift，故收敛到此处共用。

    类型判定的两种形态都处理：
      - 顶层 `type`（kqa 检索结果已归一化为 "shared_rule"）；
      - `metadata.type`（clarify 原始检索结果的形态）。

    Args:
        sources: 检索结果列表（dict）。
        enabled: 显式开关；None 时读 settings.KB_SHARED_RULE_ENABLED（缺省 True）。
    """
    if enabled is None:
        try:
            from app.config import settings
            enabled = bool(getattr(settings, "KB_SHARED_RULE_ENABLED", True))
        except Exception:  # noqa: BLE001
            enabled = True
    if enabled:
        return list(sources)

    def _is_shared_rule(s) -> bool:
        if not isinstance(s, dict):
            return False
        if s.get("type") == "shared_rule":
            return True
        return ((s.get("metadata") or {}).get("type")) == "shared_rule"

    return [s for s in sources if not _is_shared_rule(s)]


def iter_shared_rules(srr: dict) -> list[dict]:
    """抽取 SRR 中的共享业务规则 + 全局约束（去重保序）。

    Returns:
        list[dict]: [{"text": 规则正文, "applies_to": 适用功能点(可空),
                      "rule_id": 原规则 id(可空), "block": 来源子块名}]
    """
    cfi = (srr or {}).get("cross_fp_interactions") or {}
    if not isinstance(cfi, dict):
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for block_key in RULE_BLOCK_KEYS:
        block = cfi.get(block_key) or []
        if isinstance(block, str):
            # 兼容历史形态：整块写成单条字符串（非列表）
            block = [block]
        elif isinstance(block, dict):
            block = [block]
        if not isinstance(block, (list, tuple)):
            continue
        for item in block:
            text = rule_text_of(item)
            if not text:
                continue
            # 跳过解析器元注释（非规则本身）
            if any(m in text for m in _META_COMMENT_MARKERS):
                continue
            key = _norm_rule(text)
            if not key or key in seen:
                continue
            seen.add(key)
            applies_to = ""
            rule_id = ""
            if isinstance(item, dict):
                applies_to = item.get("applies_to") or item.get("applied_fps") or ""
                if isinstance(applies_to, list):
                    applies_to = ",".join(str(x) for x in applies_to if x)
                rule_id = str(item.get("id") or "")
            out.append({
                "text": text, "applies_to": applies_to,
                "rule_id": rule_id, "block": block_key,
            })
    return out


def shared_rule_srr(rule_text: str) -> dict:
    """把一条规则包装成最小 SRR（供 `_srr_to_text` 渲染成嵌入文本）。

    用 requirement.title/summary + business_rules 三处冗余承载规则正文，
    使向量文本以规则本身为主体（无邻居规则稀释），同时标题可读、便于前端展示。
    """
    head = rule_text[:60]
    return {
        "requirement": {
            "title": f"共享业务规则：{head}",
            "summary": rule_text,
        },
        "business_rules": [{"rule": rule_text}],
    }


def shared_rule_outbox_payload(
    product_id: str,
    rule_text: str,
    source_req_id: str = "",
    source_req_title: str = "",
    applies_to: str = "",
    version_id: str = "",
    project_id: str = "",
    rule_block: str = "",
) -> dict:
    """构造 shared_rule 出箱投递 payload。

    metadata 必含 status="archived"：`search_similar_requirements` 按
    status=archived 硬过滤，漏了 = 向量存在但永远检索不到。
    """
    return {
        "requirement_json": shared_rule_srr(rule_text),
        "metadata": {
            "type": "shared_rule",            # add_requirement 会自动同步 doc_type
            "status": "archived",             # 必含：KB 检索只取 archived（硬过滤）
            "product_id": product_id or "",
            "project_id": project_id or "",
            "title": f"共享业务规则：{rule_text[:60]}",
            "rule_text": rule_text,
            "rule_id": "",
            "rule_block": rule_block or "",
            "applies_to": applies_to or "",
            "source_req_id": source_req_id or "",
            "source_req_title": source_req_title or "",
            "version_id": version_id or "",
            "compiled_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def existing_shared_rule_ids(product_id: str) -> set[str]:
    """查询向量库中该产品已有的 shared_rule 条目 ID（用于对账删除）。

    ⚠️ **同步实现**（Milvus 客户端为同步链路，含 col.load()）：必须由调用方
    经 `asyncio.to_thread(...)` 执行，禁止在事件循环中直接 await 调用——
    否则会阻塞整个事件循环（v4.0.1 实证：同步向量调用漏包 to_thread 曾致
    生成期全站超时）。
    """
    try:
        from app.db.milvus_store import vector_store
        col = vector_store._get_collection("requirements")
        col.load()
        expr = (
            f'metadata["doc_type"] == "shared_rule" '
            f'and metadata["product_id"] == "{product_id}"'
        )
        _limit = 16384
        rows = col.query(expr=expr, output_fields=["id"], limit=_limit)
        # v2.48.1：命中上限说明结果可能被截断 → 未被返回的存量条目本轮不会被
        # 判为 stale（即"漏删"而非"误删"，失败方向安全），但仍需告警以便发现
        # 规模异常（正常产品规则数远小于此值）。
        if len(rows) >= _limit:
            logger.warning(
                "[SharedRules] 存量查询命中上限 {}（结果可能不完整，本轮不删未返回条目）: product={}",
                _limit, product_id,
            )
        return {r["id"] for r in rows}
    except Exception as e:  # noqa: BLE001
        logger.warning("[SharedRules] 查询存量条目失败（跳过对账删除）: {}", str(e)[:150])
        return set()


async def reconcile_shared_rules(
    session,
    product_id: str,
    archived_requirements: list,
    version_id: str = "",
) -> dict:
    """把「产品的规则向量」对账到「产品下仍处于 archived 的需求集合」。

    解决的问题：
      - **恢复活动**：`reactivate_project` 会按 project_id 批量删向量，连带删掉
        shared_rule（一个规则可能被多个项目引用）→ 这里重算补齐，实现自愈；
      - **规则消失**：需求改版后不再包含某条规则 → 该条从期望集合移除，向量一并删除
        （补上 v1「只增不减」的缺口）。

    期望集合 = 所有 archived 需求各自 SRR 抽出的规则并集（按 (product, 规范化文本) 去重）。
    """
    from app.core.vector_outbox import enqueue as outbox_enqueue

    desired: dict[str, dict] = {}
    for req in archived_requirements or []:
        for r in iter_shared_rules(getattr(req, "srr_json", None) or {}):
            rid = shared_rule_vector_id(product_id, r["text"])
            if rid not in desired:
                desired[rid] = {
                    "text": r["text"],
                    "block": r.get("block") or "",
                    "applies_to": r.get("applies_to") or "",
                    "rule_id": r.get("rule_id") or "",
                    "req": req,
                }

    stats = {"desired": len(desired), "upserted": 0, "deleted": 0, "failed": 0}

    # 1) upsert 期望集合
    for rid, info in desired.items():
        req = info["req"]
        try:
            payload = shared_rule_outbox_payload(
                product_id=product_id,
                rule_text=info["text"],
                source_req_id=getattr(req, "id", "") or "",
                source_req_title=getattr(req, "title", "") or "",
                applies_to=info["applies_to"],
                version_id=version_id,
                project_id=getattr(req, "project_id", "") or "",
                rule_block=info["block"],
            )
            payload["metadata"]["rule_id"] = info["rule_id"]
            outbox_enqueue(
                session,
                aggregate_type="shared_rule",
                aggregate_id=rid,
                action="upsert",
                payload=payload,
                created_by="shared_rule_reconcile",
            )
            stats["upserted"] += 1
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            logger.warning("[SharedRules] 对账 upsert 失败: id={}, err={}", rid, str(e)[:150])

    # 2) 删除已不在期望集合中的存量条目
    # v2.48.1：existing_shared_rule_ids 是同步 Milvus 查询 → 必须 to_thread，
    # 否则在归档/恢复活动接口里阻塞事件循环（违反 vector_store 同步纪律）。
    _existing = await asyncio.to_thread(existing_shared_rule_ids, product_id)
    stale = _existing - set(desired.keys())
    for rid in sorted(stale):
        try:
            outbox_enqueue(
                session,
                aggregate_type="shared_rule",
                aggregate_id=rid,
                action="delete",
                payload={"metadata": {"product_id": product_id, "type": "shared_rule"}},
                created_by="shared_rule_reconcile",
            )
            stats["deleted"] += 1
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            logger.warning("[SharedRules] 对账 delete 失败: id={}, err={}", rid, str(e)[:150])

    logger.info(
        "[SharedRules] 对账完成: product={}, 期望={}, upsert={}, 删除过期={}, 失败={}",
        product_id, stats["desired"], stats["upserted"], stats["deleted"], stats["failed"],
    )
    return stats
