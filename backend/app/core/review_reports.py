"""评审报告中心服务（v3.1.3 P1）。

将流水线产生的两类评审报告构建为 ReviewReportRecord 并持久化：
- case 报告：ReviewAgent 审核报告（含 v3.1.3 dimension_scores 维度级置信度）
- srr 报告：QualityGate 质量门禁报告（含 ClarifyAgent 质量分）

设计要点：
- 构建函数为纯函数（输入 dict → 输出记录 dict），便于单测与回填复用
- issue 列表统一携带稳定 issue_id 与 adoption_status="pending"，
  预留 P2 逐条采纳与 P3 多报告聚合整合生成新版提示词
- 落库失败不阻断生成主流程（调用方 try/except）

写入时机：generate API done 事件处理（与 result_json 落档同点）。
"""

from __future__ import annotations

import time
import random

from app.utils.logger import logger


# 维度中文名（前端展示兜底，前端亦有同名映射）
DIMENSION_LABELS: dict[str, str] = {
    # case 报告 6 维度
    "completeness": "完整性",
    "deduplication": "去重",
    "executability": "可执行性",
    "consistency": "一致性",
    "traceability": "追溯性",
    "coverage": "覆盖率",
    # srr 报告维度
    "gate_compliance": "门禁合规",
    "requirement_completeness": "需求完整性",
    "clarity": "清晰度",
}

_report_seq = random.randint(0, 9999)


def _gen_report_id() -> str:
    """生成评审报告 ID: RR-{时间戳毫秒}-{4位序号}（与 REQ ID 同风格）。"""
    global _report_seq
    _report_seq = (_report_seq + 1) % 10000
    ms = int(time.time() * 1000)
    return f"RR-{ms}-{_report_seq:04d}"


def _build_issues(report_id: str, raw_issues: list[dict] | None) -> list[dict]:
    """为原始 issue 列表补稳定 issue_id 与采纳状态。"""
    built: list[dict] = []
    for idx, i in enumerate(raw_issues or []):
        if not isinstance(i, dict):
            continue
        item = dict(i)
        item["issue_id"] = f"{report_id}-{idx:03d}"
        item.setdefault("adoption_status", "pending")
        item.setdefault("adoption_comment", "")
        built.append(item)
    return built


def build_case_report(
    task_id: str | None,
    req_id: str,
    product_id: str,
    req_title: str,
    review_report: dict,
    gen_batch_id: str = "",
    *,
    completion_status: str = "",
    degraded_flags: list | None = None,
    failed_subtasks: list | None = None,
) -> dict:
    """构建 case 类型评审报告记录（纯函数）。

    Args:
        task_id: 生成任务 ID
        req_id: 需求 ID
        product_id: 产品 ID
        req_title: 需求标题快照
        review_report: ReviewAgent 审核报告 dict（含 issues/dimension_scores 等）
        gen_batch_id: 生成批次 ID（可从用例 gen_batch_id 提取）
        completion_status: 任务终态（completed / awaiting_human_review / failed）
        degraded_flags: 本次运行的降级标记
        failed_subtasks: 失败的功能点/通道子任务

    Returns:
        dict: ReviewReportRecord 字段 dict（不含 ORM 对象）
    """
    report_id = _gen_report_id()
    issues = _build_issues(report_id, review_report.get("issues"))
    blocked = int(review_report.get("blocked_cases", 0) or 0)
    should_supplement = bool(review_report.get("should_supplement"))

    # v3.4（docs/15 P0-3）：
    # ① passed 必须与发布决策一致——否则会出现"报告中心说通过、发布决策说待人工审核"
    #    两套口径，复盘时不知道以谁为准；
    # ② 降级标记 / 失败子任务 / 终态随报告落库，避免这些证据只活在 SSE 事件里。
    publication = review_report.get("publication") or {}
    decision = publication.get("decision")
    passed = (decision == "auto_approve") if decision else (blocked == 0 and not should_supplement)

    return {
        "id": report_id,
        "report_type": "case",
        "req_id": req_id,
        "product_id": product_id,
        "task_id": task_id,
        "req_title": (req_title or "")[:512],
        "gen_batch_id": gen_batch_id or "",
        "overall_confidence": round(float(review_report.get("avg_confidence", 0.0) or 0.0), 3),
        "passed": passed,
        "issue_count": len(issues),
        "dimension_scores": review_report.get("dimension_scores") or {},
        "issues": issues,
        "summary": {
            **review_report,
            "completion_status": completion_status or review_report.get("completion_status", ""),
            "publication": publication,
            "degraded_flags": list(degraded_flags or []),
            "failed_subtasks": list(failed_subtasks or []),
            "content_errors": review_report.get("content_errors") or [],
        },
        "created_by": "pipeline",
    }


def build_srr_report(
    task_id: str | None,
    req_id: str,
    product_id: str,
    req_title: str,
    gate_result: dict,
    clarify_result: dict | None = None,
) -> dict:
    """构建 srr 类型评审报告记录（纯函数）。

    QualityGate 的 blockers/warnings 为字符串列表，此处转为结构化 issue；
    ClarifyAgent 的 quality_report/questions 用于维度分计算。

    SRR 维度口径：
    - gate_compliance：gate passed=1.0；forced_pass=0.6（强制放行需人工关注）；未通过=0.3
    - requirement_completeness：clarify quality_report.completeness_score，
      缺失时按 blocker 数扣分
    - clarity：无 critical 问题=1.0，否则 1 - min(1, critical 数/5)

    Args:
        task_id: 生成任务 ID
        req_id: 需求 ID
        product_id: 产品 ID
        req_title: 需求标题快照
        gate_result: QualityGateResult.model_dump() 后的 dict
        clarify_result: ClarifyAgent 输出 dict（可含 quality_report/questions）

    Returns:
        dict: ReviewReportRecord 字段 dict
    """
    report_id = _gen_report_id()
    clarify_result = clarify_result or {}
    blockers = list(gate_result.get("blockers", []) or [])
    warnings = list(gate_result.get("warnings", []) or [])
    passed = bool(gate_result.get("passed"))
    forced = bool(gate_result.get("forced_pass"))

    # 结构化 issue：blockers → severity=blocker，warnings → severity=warning
    raw_issues: list[dict] = [
        {"dimension": "gate_compliance", "severity": "blocker", "message": str(b), "suggestion": ""}
        for b in blockers if str(b).strip()
    ] + [
        {"dimension": "gate_compliance", "severity": "warning", "message": str(w), "suggestion": ""}
        for w in warnings if str(w).strip()
    ]
    issues = _build_issues(report_id, raw_issues)

    # 维度分计算
    quality_report = (
        clarify_result.get("quality_report")
        if isinstance(clarify_result.get("quality_report"), dict) else {}
    ) or {}
    overall_score = float(quality_report.get("overall_score", 0.0) or 0.0)
    completeness_score = quality_report.get("completeness_score")

    questions = clarify_result.get("questions") or []
    critical_count = sum(
        1 for q in questions
        if isinstance(q, dict) and q.get("priority") == "critical"
    )
    if critical_count > 0:
        raw_issues.append({
            "issue_id": f"{report_id}-{len(issues):03d}",
            "dimension": "clarity",
            "severity": "blocker",
            "message": f"存在 {critical_count} 个 critical 级澄清问题未解决",
            "suggestion": "请查看澄清页并回答关键问题",
            "adoption_status": "pending",
            "adoption_comment": "",
        })
        issues = raw_issues

    from app.utils.numbers import clamp01 as _clamp  # v3.2.2 收敛：原 _clamp 去重

    gate_score = 1.0 if passed else (0.6 if forced else 0.3)
    if completeness_score is None:
        completeness = _clamp(1.0 - min(1.0, len(blockers) / 2.0))
    else:
        completeness = _clamp(float(completeness_score))
    clarity = _clamp(1.0 - min(1.0, critical_count / 5.0))

    dimension_scores = {
        "gate_compliance": {"score": _clamp(gate_score), "issue_count": len(blockers) + len(warnings)},
        "requirement_completeness": {"score": completeness, "issue_count": len(blockers)},
        "clarity": {"score": clarity, "issue_count": critical_count},
    }

    return {
        "id": report_id,
        "report_type": "srr",
        "req_id": req_id,
        "product_id": product_id,
        "task_id": task_id,
        "req_title": (req_title or "")[:512],
        "gen_batch_id": "",
        "overall_confidence": _clamp(overall_score if overall_score > 0 else gate_score),
        "passed": passed,
        "issue_count": len(issues),
        "dimension_scores": dimension_scores,
        "issues": issues,
        "summary": {
            "gate_result": gate_result,
            "clarify_quality_report": quality_report,
        },
        "created_by": "pipeline",
    }


async def save_review_reports(records: list[dict]) -> list[str]:
    """批量写入评审报告记录。

    Returns:
        list[str]: 成功写入的报告 ID 列表
    """
    from app.db.database import async_session
    from app.db.models import ReviewReportRecord

    saved: list[str] = []
    async with async_session() as session:
        for rec in records:
            try:
                session.add(ReviewReportRecord(**rec))
                await session.flush()
                saved.append(rec["id"])
            except Exception as e:
                logger.warning(
                    "[ReviewReports] 报告落库失败（跳过，不阻断）: id={}, err={}",
                    rec.get("id"), str(e)[:150],
                )
        await session.commit()
    if saved:
        logger.info("[ReviewReports] 评审报告落库: count={}, ids={}", len(saved), saved)
    return saved
