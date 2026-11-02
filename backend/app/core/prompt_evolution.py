"""提示词进化服务（v3.1.3 评审报告中心 P3）。

将多份评审报告中人工已采纳（adoption_status=accepted）的意见 + 人工补充意见，
经 LLM 整合为目标生成提示词末尾的"历史评审沉淀规则"区段，写入 PromptManager
新版本（自动记录 prompt_history，支持一键回退）。

设计要点（增量补丁式整合）：
1. 不重写 prompt 主体 — LLM 只负责合并"沉淀区段"，原有规则零风险；
2. 沉淀区段用固定 HTML 注释标记包裹，可被反复识别与替换：
       <!-- PROMPT_EVOLUTION:BEGIN -->
       ...（LLM 整合的规则内容）...
       <!-- PROMPT_EVOLUTION:END -->
3. 首次整合 = 追加区段；后续整合 = LLM 读取旧区段 + 新意见 → 合并去重后替换；
4. 每次 update 自动产生 prompt_history 记录，回退走既有
   POST /api/admin/prompts/{module}/{name}/restore/{history_id}。

设计依据：v3.1.3 评审报告中心 P3（多报告意见整合优化提示词，自我进化闭环）。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.llm_client import llm_client
from app.db import prompt_store
from app.db.models import PromptIntegrationRecord, ReviewReportRecord
from app.prompts.prompt_evolution import INTEGRATION_SYSTEM_PROMPT as _INTEGRATION_SYSTEM_PROMPT
from app.utils.logger import logger

# ============================================================================
# 沉淀区段标记协议
# ============================================================================

EVOLUTION_BEGIN = "<!-- PROMPT_EVOLUTION:BEGIN -->"
EVOLUTION_END = "<!-- PROMPT_EVOLUTION:END -->"

_SECTION_HEADER = """## 历史评审沉淀规则（由评审报告采纳意见自动整合，生成时必须严格遵守）

以下规则源自历次评审中人工确认的真实问题，用于修正生成行为、避免重复缺陷。
这些规则是对上文工作原则的补充；若与上文冲突，以本区段为准。"""


def split_prompt(prompt: str) -> tuple[str, str]:
    """将 prompt 拆为 (主体, 沉淀规则文本)。

    沉淀规则 = 标记内内容去掉固定小节标题（标题由代码统一注入，LLM 不维护）。
    无标记时沉淀规则为空串；标记之后若还有文本，归入主体尾部保留。
    """
    pattern = re.compile(
        re.escape(EVOLUTION_BEGIN) + r"\s*(.*?)\s*" + re.escape(EVOLUTION_END),
        re.DOTALL,
    )
    m = pattern.search(prompt)
    if not m:
        return prompt, ""
    section = m.group(1).strip()
    # 剥离代码注入的小节标题，只留规则条目给 LLM
    if section.startswith(_SECTION_HEADER):
        section = section[len(_SECTION_HEADER):].strip()
    body = (prompt[: m.start()] + prompt[m.end():]).rstrip()
    return body, section


def merge_prompt(body: str, rules: str) -> str:
    """主体 + 规则条目 → 完整 prompt（标题与标记由代码统一组装）。"""
    if not rules.strip():
        return body
    section = f"{_SECTION_HEADER}\n\n{rules.strip()}"
    return f"{body}\n\n{EVOLUTION_BEGIN}\n{section}\n{EVOLUTION_END}"


# ============================================================================
# 意见聚合
# ============================================================================

async def collect_accepted_issues(
    session,
    product_id: str,
    report_ids: list[str],
) -> tuple[list[dict], list[dict]]:
    """聚合指定报告中所有已采纳（accepted）的意见。

    Returns:
        (issues, reports)：issues 为去重后的意见摘要列表，
        reports 为 [{id, report_type, req_title}] 供记录快照。
    """
    result = await session.execute(
        select(ReviewReportRecord).where(
            ReviewReportRecord.id.in_(report_ids),
            ReviewReportRecord.product_id == product_id,
        )
    )
    records = result.scalars().all()

    issues: list[dict] = []
    seen_keys: set[str] = set()
    reports: list[dict] = []

    for r in records:
        reports.append({
            "id": r.id,
            "report_type": r.report_type,
            "req_title": r.req_title,
        })
        for i in (r.issues or []):
            if not isinstance(i, dict) or i.get("adoption_status") != "accepted":
                continue
            # 去重键：同维度同 case 的同义问题只保留一条（不同报告可能重复检出）
            key = f"{i.get('dimension', '')}|{i.get('case_id', '')}|{(i.get('message') or '')[:80]}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            issues.append({
                "source_report": r.id,
                "report_type": r.report_type,
                "dimension": i.get("dimension", ""),
                "severity": i.get("severity", ""),
                "message": i.get("message", ""),
                "suggestion": i.get("suggestion", ""),
                "human_comment": i.get("adoption_comment", ""),
            })

    return issues, reports


# ============================================================================
# LLM 整合
# ============================================================================

# v4.2.4：本段提示词已迁至 app/prompts/prompt_evolution.py 并由 prompt_manager 纳管，
# 便于在管理台查看/调优「评审意见 → 生成规则」的合并质量。
# 顶部 import 保留了 _INTEGRATION_SYSTEM_PROMPT 别名，下方调用点无需改动。


def _format_issues_for_llm(issues: list[dict], manual_notes: str) -> str:
    lines: list[str] = []
    for idx, i in enumerate(issues, 1):
        lines.append(
            f"{idx}. [{i.get('severity')}/{i.get('dimension')}] {i.get('message', '')}\n"
            f"   建议: {i.get('suggestion', '')}\n"
            f"   人工意见: {i.get('human_comment') or '（无）'}"
        )
    parts = ["【本次新增的已采纳评审意见】", "\n".join(lines) or "（无）"]
    if manual_notes.strip():
        parts.append("\n【人工补充意见（最高优先级）】\n" + manual_notes.strip())
    return "\n".join(parts)


async def evolve_prompt(
    session,
    product_id: str,
    report_ids: list[str],
    target_module: str,
    target_name: str,
    manual_notes: str = "",
    username: str = "api",
    activate: bool = False,
) -> dict:
    """执行一次提示词进化整合（P3 主入口）。

    流程：聚合 accepted 意见 → LLM 合并沉淀区段 → prompt_manager.update 写入新版本
    → 落库 PromptIntegrationRecord（含回退锚点 prompt_history_id）。

    Raises:
        ValueError: 无已采纳意见 / 目标提示词不存在 / LLM 整合失败
    """
    # 1) 聚合意见
    issues, reports = await collect_accepted_issues(session, product_id, report_ids)
    if not reports:
        raise ValueError("未找到当前产品下的有效报告（请检查 report_ids 与产品隔离）")
    if not issues and not manual_notes.strip():
        raise ValueError("所选报告中没有已采纳（accepted）的评审意见，且未填写人工补充意见")

    # 2) 读取目标 prompt 当前值
    current = prompt_store.get_prompt(target_module, target_name)
    if current is None:
        current = prompt_store.get_prompt_default(target_module, target_name)
    if not current:
        raise ValueError(f"目标提示词 {target_module}.{target_name} 不存在")

    body, old_section = split_prompt(current)

    # v4.2.3：三段式短事务——第 182 行 collect_accepted_issues 已开启事务，
    # 下面的 llm_client.call_json 是同步等待 LLM（整合分析可达数十秒）。
    # 先提交释放连接；reports / issues 均为普通 dict（见 235 行 r["id"]），
    # 不依赖 ORM 会话；第 247 行 session.add(record) 会重新开启事务。
    await session.commit()

    # 3) LLM 整合沉淀区段
    user_prompt = (
        f"【当前沉淀区段】\n{old_section or '（空，首次整合）'}\n\n"
        + _format_issues_for_llm(issues, manual_notes)
    )
    try:
        result = await llm_client.call_json(
            prompt=user_prompt,
            system_prompt=_INTEGRATION_SYSTEM_PROMPT,
            temperature=0.2,
            agent_name="PromptEvolution",
        )
    except Exception as e:
        logger.error("[PromptEvolution] LLM 整合调用失败: {}", str(e)[:200])
        raise ValueError(f"LLM 整合调用失败: {str(e)[:200]}")

    new_section = (result.get("new_section") or "").strip()
    summary = (result.get("summary") or "").strip()
    if not new_section:
        raise ValueError("LLM 未返回有效的整合规则（new_section 为空），已放弃写入")

    # 4) 组装新 prompt（P2-5: 默认只进候选区，激活后才写 override）
    new_prompt = merge_prompt(body, new_section)
    prompt_history_id = 0
    if activate:
        # 显式 activate=True（旧路径）：直接写入 override（自动产生 prompt_history）
        prompt_store.update_prompt(
            target_module, target_name, new_prompt,
            changed_by=f"evolution:{username}",
        )
        history = prompt_store.list_prompt_history(target_module, target_name, limit=1)
        prompt_history_id = history[0]["id"] if history else 0

    # 6) 落库整合记录
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    record = PromptIntegrationRecord(
        id=f"PI-{now_ms}-{uuid.uuid4().hex[:4]}",
        product_id=product_id,
        report_ids=[r["id"] for r in reports],
        report_types=sorted({r["report_type"] for r in reports}),
        target_prompt_id=f"{target_module}.{target_name}",
        accepted_count=len(issues),
        accepted_issues=issues,
        manual_notes=manual_notes.strip(),
        integration_summary=summary,
        prompt_history_id=prompt_history_id,
        candidate_prompt=("" if activate else new_prompt),
        status=("activated" if activate else "pending"),
        created_by=username,
    )
    session.add(record)
    await session.commit()

    logger.info(
        "[PromptEvolution] 整合完成: target={}.{}, reports={}, issues={}, history_id={}, by={}",
        target_module, target_name, len(reports), len(issues), prompt_history_id, username,
    )

    return {
        "integration_id": record.id,
        "target_prompt_id": record.target_prompt_id,
        "report_count": len(reports),
        "accepted_count": len(issues),
        "integration_summary": summary,
        "prompt_history_id": prompt_history_id,
        "new_section": new_section,
        "old_section": old_section,
    }
