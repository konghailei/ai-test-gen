"""ReviewAgent 用例审核 Agent。

对 GenAgent 生成的 UTCF 用例集进行 6 维度审核，生成审核报告和置信度评分。
遵循 Auto-Validation 框架理念：每个测试必须映射到具体需求（可追溯性）。

设计依据：
- docs/03-case-generation-flow.md §5.1（ReviewAgent 审核维度）
- docs/03-case-generation-flow.md §5.2（置信度计算 + 字段语义统一）

审核维度（6 项，docs/03 §5.1）：
    1. 完整性：覆盖所有 flows、boundary、business_rules（含总 SRR 的 cross_fp_interactions.shared_business_rules 与 FP SRR 的 business_rules）
    2. 去重：语义重复用例
    3. 可执行性：前置条件 + 步骤 + 预期完整
    4. 一致性：用例间无矛盾
    5. 追溯性：每个用例有需求依据
    6. 覆盖率：相对 test_points_hint 的覆盖

字段语义统一（docs/03 §5.2）：
    - gen_confidence：GenAgent 生成时 LLM 自评估，仅作 ReviewAgent 输入，不对外暴露
    - final_confidence：ReviewAgent 多因子加权计算后的最终置信度，写入 UTCF.confidence

输入：SRR JSON + 用例列表
输出：审核报告 + 更新后的用例（含 final_confidence）
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.core.llm_client import llm_client
from app.core.constants import Confidence
from app.db.milvus_store import vector_store
from app.models.utcf import ReviewReport
from app.prompts import review_agent as prompts
from app.core import prompt_manager
from app.utils.coverage import calc_point_coverage
from app.utils.id_gen import gen_batch_id, gen_error_id, gen_test_case_id
from app.utils.logger import logger


class ReviewAgent:
    """用例审核 Agent。

    对应 docs/00-architecture-overview.md §4 的 ReviewAgent 节点。

    Usage:
        agent = ReviewAgent()
        report, reviewed_cases = await agent.review(srr, test_cases)
        if report["should_supplement"]:
            # 触发 GenAgent 补充生成
            ...
    """

    def __init__(self) -> None:
        """初始化 ReviewAgent。"""
        self._llm = llm_client

    async def review(
        self,
        srr: dict,
        test_cases: list[dict],
        functional_points: list[dict] | None = None,
    ) -> tuple[dict, list[dict]]:
        """ReviewAgent 主入口：规则预检 + 6 维 LLM 审核 + 置信度计算 + 要点级覆盖。

    返回 (report, reviewed_cases)。
        """
        """对测试用例集执行 6 维度审核。

        Args:
            srr: SRR JSON 字典（用于追溯性和覆盖率检查）
            test_cases: GenAgent 生成的 UTCF 用例列表
            functional_points: 功能点列表（用于要点级覆盖映射统计）

        Returns:
            tuple[dict, list[dict]]: (审核报告, 更新后的用例列表)
            审核报告格式：
                {
                    "total_cases": int,
                    "passed_cases": int,
                    "blocked_cases": int,
                    "issues": [...],
                    "coverage_summary": {...},
                    "avg_confidence": float,
                    "should_supplement": bool,
                    "supplement_hints": [...]
                }
            更新后的用例列表：每个用例的 confidence 字段被写入 final_confidence

        Note:
            - gen_confidence 仅作输入参考，final_confidence 由多因子加权计算
            - blocker 级问题触发 should_supplement
            - 覆盖率低于阈值触发补充生成
        """
        req_id = srr.get("requirement", {}).get("id", "")
        logger.info("[ReviewAgent] ▶ 开始审核: req_id={}, cases={}", req_id, len(test_cases))

        # 1. 计算 final_confidence（多因子加权，非 LLM）
        reviewed_cases = await self._calculate_confidences(srr, test_cases)
        confidences = [c.get("confidence", 0) for c in reviewed_cases]
        avg_gen_conf = sum(
            c.get("gen_metadata", {}).get("gen_confidence", 0.5) for c in reviewed_cases
        ) / len(reviewed_cases) if reviewed_cases else 0
        avg_final_conf = sum(confidences) / len(confidences) if confidences else 0
        logger.info("[ReviewAgent] 置信度计算: avg_gen_conf={:.3f} → avg_final_conf={:.3f}",
                    avg_gen_conf, avg_final_conf)
        if reviewed_cases:
            logger.debug("[ReviewAgent] 各用例置信度: {}",
                         [(c.get("id", ""), c.get("confidence", 0)) for c in reviewed_cases])

        # 2. 规则预检：追溯性 + 可执行性（快速检查）
        rule_issues = self._rule_based_review(srr, reviewed_cases)
        rule_blockers = sum(1 for i in rule_issues if i.get("severity") == "blocker")
        logger.info("[ReviewAgent] 规则预检: issues={}, blockers={}",
                    len(rule_issues), rule_blockers)

        # 3. LLM 深度审核：完整性 + 去重 + 一致性 + 覆盖率
        logger.info("[ReviewAgent] 调用 LLM 6维度审核...")
        llm_report = await self._llm_review(srr, reviewed_cases)
        llm_issues = llm_report.get("issues", [])
        logger.info("[ReviewAgent] LLM 审核完成: issues={}, avg_confidence={}",
                    len(llm_issues), llm_report.get("avg_confidence", 0))

        # 4. 合并审核结果（含要点级覆盖映射，口径与 GenAgent._log_coverage_stats 一致）
        point_coverage = calc_point_coverage(srr, functional_points or [], reviewed_cases)
        logger.info(
            "[ReviewAgent] 要点级覆盖[{}]: 要点总数={}, 已覆盖={}, 覆盖率={:.1%}, P0/P1={}/{}, 未覆盖={}",
            "映射" if point_coverage["mapping_based"] else "回退(数量比)",
            point_coverage["total_points"], point_coverage["covered_points"],
            point_coverage["coverage_rate"],
            point_coverage["high_priority_covered"], point_coverage["high_priority_total"],
            len(point_coverage["uncovered"]),
        )
        report = self._merge_review_results(
            reviewed_cases, rule_issues, llm_report, srr, point_coverage=point_coverage
        )

        logger.info("[ReviewAgent] ◀ 审核完成 ▶ 编排器决策: "
                    "total={}, passed={}, blocked={}, avg_conf={:.3f}, "
                    "coverage={}, should_supplement={}, supplement_hints={}",
                    report["total_cases"], report["passed_cases"], report["blocked_cases"],
                    report["avg_confidence"], report["coverage_summary"],
                    report["should_supplement"], len(report["supplement_hints"]))
        return report, reviewed_cases

    # ========================================================================
    # 置信度计算（docs/03 §5.2，多因子加权）
    # ========================================================================

    async def _calculate_confidences(
        self,
        srr: dict,
        test_cases: list[dict],
    ) -> list[dict]:
        """为每个用例计算 final_confidence。

        对应 docs/03 §5.2 的 calculate_confidence 函数。
        多因子加权：match_score×0.4 + completeness×0.3 + consistency×0.2 + gen_self×0.1 + pattern_bonus

        Args:
            srr: SRR JSON 字典
            test_cases: 用例列表

        Returns:
            list[dict]: 更新后的用例列表（confidence 字段被写入 final_confidence）
        """
        reviewed: list[dict] = []
        product_id = srr.get("requirement", {}).get("product_id", "")
        for case in test_cases:
            # 异步检索 KB 相似用例（用于 match_score）— v2.2: 按 product_id 过滤
            match_results = await self._search_similar_for_review(case, product_id=product_id)

            # 计算最终置信度
            final_conf = prompts.calculate_confidence(case, match_results)

            # 写入 confidence 字段（即 final_confidence，UTCF.confidence）
            case["confidence"] = round(final_conf, 3)
            reviewed.append(case)
        return reviewed

    async def _search_similar_for_review(self, case: dict, product_id: str = "") -> list[dict]:
        """异步检索 KB 相似用例（避免阻塞事件循环）。

        v2.2: 按 product_id 过滤，防止跨产品数据泄漏。
        """
        title = case.get("title", "")
        try:
            return await asyncio.to_thread(
                vector_store.search_similar_cases,
                query=title, top_k=3, product_id=product_id or None,
            )
        except Exception:
            return []

    # ========================================================================
    # 规则预检（快速，非 LLM）
    # ========================================================================

    def _rule_based_review(
        self,
        srr: dict,
        test_cases: list[dict],
    ) -> list[dict]:
        """规则预检：追溯性 + 可执行性。

        这些检查不需要 LLM，可以快速完成。

        Args:
            srr: SRR JSON 字典
            test_cases: 用例列表

        Returns:
            list[dict]: 问题列表
        """
        issues: list[dict] = []
        req_id = srr.get("requirement", {}).get("id", "")

        for case in test_cases:
            case_id = case.get("id", "")

            # 追溯性：检查 related_requirement
            if not case.get("related_requirement"):
                issues.append({
                    "case_id": case_id,
                    "dimension": "traceability",
                    "severity": "blocker",
                    "message": "用例缺少 related_requirement，无法追溯需求",
                    "suggestion": f"建议关联到 {req_id}",
                })

            # 可执行性：检查步骤完整性
            steps = case.get("steps", [])
            for i, step in enumerate(steps):
                if not step.get("action") or not step.get("expected"):
                    issues.append({
                        "case_id": case_id,
                        "dimension": "executability",
                        "severity": "warning",
                        "message": f"步骤 {i + 1} 缺少 action 或 expected",
                        "suggestion": "补充完整的操作描述和预期结果",
                    })

            # 可执行性：检查前置条件
            if not case.get("preconditions"):
                issues.append({
                    "case_id": case_id,
                    "dimension": "executability",
                    "severity": "info",
                    "message": "用例无前置条件",
                    "suggestion": "建议补充前置条件以确保可执行性",
                })

        return issues

    # ========================================================================
    # LLM 深度审核
    # ========================================================================

    async def _llm_review(
        self,
        srr: dict,
        test_cases: list[dict],
    ) -> dict:
        """调用 LLM 执行 6 维度深度审核。

        Args:
            srr: SRR JSON 字典
            test_cases: 用例列表

        Returns:
            dict: LLM 审核报告
        """
        # 提取 gen_confidence 列表（仅作 LLM 参考）
        gen_confidences = [
            c.get("gen_metadata", {}).get("gen_confidence", 0.5)
            for c in test_cases
        ]

        user_prompt = prompts.build_user_prompt(srr, test_cases, gen_confidences)

        # P1-1 评审异构路由：评审可配置专属供应商（与生成模型家族不同 → 偏见正交，
        # 依据《深入理解 AI Agent》6.5.1 同源偏见）。未配置/失败时回退主力池，尽力而为。
        judge_provider = None
        try:
            from app.db.database import async_session as _as
            from app.api.settings import get_review_llm_provider
            async with _as() as _s:
                judge_provider = await get_review_llm_provider(_s)
        except Exception as e:
            logger.debug("[ReviewAgent] 读取评审供应商设置失败（跟随主力）: {}", str(e)[:100])

        _review_kwargs = dict(
            prompt=user_prompt,
            system_prompt=prompt_manager.get("review_agent", "SYSTEM_PROMPT"),
            schema_hint={"issues": list, "avg_confidence": float},
            temperature=0.2,
            agent_name="ReviewAgent",
        )
        if judge_provider:
            try:
                result = await self._llm.call_json(**_review_kwargs, provider=judge_provider)
                logger.info("[ReviewAgent] 评审走异构供应商: {}", judge_provider)
                return result
            except Exception as e:
                logger.warning(
                    "[ReviewAgent] 评审供应商 {} 失败，回退主力池: {}",
                    judge_provider, str(e)[:150],
                )
        try:
            result = await self._llm.call_json(**_review_kwargs)
            return result
        except Exception as e:
            logger.warning("[ReviewAgent] LLM 审核失败，使用规则降级: {}", str(e)[:200])
            # 降级：仅返回规则预检结果
            return {
                "total_cases": len(test_cases),
                "passed_cases": len(test_cases),
                "blocked_cases": 0,
                "issues": [],
                "coverage_summary": {},
                "avg_confidence": 0.0,
                "should_supplement": False,
                "supplement_hints": [],
            }

    # ========================================================================
    # 审核结果合并
    # ========================================================================

    def _merge_review_results(
        self,
        test_cases: list[dict],
        rule_issues: list[dict],
        llm_report: dict,
        srr: dict,
        point_coverage: dict | None = None,
    ) -> dict:
        """合并规则预检和 LLM 审核结果。

        Args:
            test_cases: 用例列表
            rule_issues: 规则预检发现的问题
            llm_report: LLM 审核报告
            srr: SRR JSON 字典
            point_coverage: 要点级覆盖映射结果（calc_point_coverage 输出）

        Returns:
            dict: 合并后的审核报告
        """
        # 合并问题列表
        llm_issues = llm_report.get("issues", [])
        all_issues = rule_issues + llm_issues

        # 统计
        total = len(test_cases)
        blocked_case_ids = {
            i.get("case_id", "") for i in all_issues
            if i.get("severity") == "blocker"
        }
        blocked = len(blocked_case_ids)
        passed = total - blocked

        # 覆盖率汇总
        coverage = self._calc_coverage_summary(test_cases)

        # 平均置信度
        avg_conf = 0.0
        if test_cases:
            avg_conf = sum(c.get("confidence", 0) for c in test_cases) / len(test_cases)

        # v5.2 维度级置信度（评审报告中心 P1）：确定性规则聚合，不调 LLM
        dimension_scores = self._calc_dimension_scores(
            test_cases, all_issues, point_coverage
        )

        # 判断是否需要补充生成
        should_supplement = self._should_supplement(all_issues, coverage, srr, test_cases, point_coverage=point_coverage)

        # 补充提示
        supplement_hints = (
            self._gen_supplement_hints(coverage, srr, point_coverage=point_coverage)
            if should_supplement else []
        )

        return {
            "total_cases": total,
            "passed_cases": passed,
            "blocked_cases": blocked,
            "issues": all_issues,
            "coverage_summary": coverage,
            "point_coverage": point_coverage,
            "dimension_scores": dimension_scores,
            "avg_confidence": round(avg_conf, 3),
            "should_supplement": should_supplement,
            "supplement_hints": supplement_hints,
        }

    def _calc_dimension_scores(
        self,
        test_cases: list[dict],
        issues: list[dict],
        point_coverage: dict | None = None,
    ) -> dict:
        """计算 6 维度置信度（v5.2 评审报告中心 P1，纯规则聚合，不调 LLM）。

        口径设计（评审通过：确定性数据优先，避免为维度分再调 LLM 的成本与不稳定性）：
        - traceability（追溯性）：1 - 追溯性 blocker 用例占比
        - executability（可执行性）：1 - 可执行性问题占比（扣分封顶 1.0）
        - consistency（一致性）：1 - 一致性问题占比
        - deduplication（去重）：1 - 去重问题占比
        - completeness（完整性）：要点覆盖率（映射口径）或数量比（回退口径）
        - coverage（覆盖率）：P0/P1 高优覆盖率，无高优要点时退化为完整性分

        Args:
            test_cases: 用例列表
            issues: 合并后的全部问题（含 dimension/severity）
            point_coverage: calc_point_coverage 输出

        Returns:
            dict: {dim: {"score": float[0,1], "issue_count": int}}
        """
        total = max(len(test_cases), 1)

        issue_counts: dict[str, int] = {
            d: 0 for d in (
                "completeness", "deduplication", "executability",
                "consistency", "traceability", "coverage",
            )
        }
        traceability_blocked_cases: set[str] = set()
        for i in issues:
            d = i.get("dimension", "")
            if d in issue_counts:
                issue_counts[d] += 1
            if d == "traceability" and i.get("severity") == "blocker":
                cid = i.get("case_id", "")
                if cid:
                    traceability_blocked_cases.add(cid)

        from app.utils.numbers import clamp01 as _clamp  # v3.2.2 收敛：原局部 _clamp 去重

        # 完整性/覆盖率：来自确定性要点覆盖映射
        completeness_score = 1.0
        coverage_score = 1.0
        if point_coverage and point_coverage.get("total_points", 0) > 0:
            completeness_score = float(point_coverage.get("coverage_rate", 1.0))
            if point_coverage.get("high_priority_total", 0) > 0:
                coverage_score = float(point_coverage.get("high_priority_rate", completeness_score))
            else:
                coverage_score = completeness_score
        else:
            # 无要点数据（无 FP/无 SRR 要点）时完整性/覆盖率不计问题，满分
            completeness_score = 1.0
            coverage_score = 1.0

        # 覆盖维度若有 issue（如 test_points_hint 类别未覆盖）则拉低 coverage
        if issue_counts["coverage"] > 0:
            coverage_score = min(coverage_score, 1.0 - min(1.0, issue_counts["coverage"] / total))

        return {
            "completeness": {
                "score": _clamp(completeness_score),
                "issue_count": issue_counts["completeness"],
            },
            "deduplication": {
                "score": _clamp(1.0 - min(1.0, issue_counts["deduplication"] / total)),
                "issue_count": issue_counts["deduplication"],
            },
            "executability": {
                "score": _clamp(1.0 - min(1.0, issue_counts["executability"] / total)),
                "issue_count": issue_counts["executability"],
            },
            "consistency": {
                "score": _clamp(1.0 - min(1.0, issue_counts["consistency"] / total)),
                "issue_count": issue_counts["consistency"],
            },
            "traceability": {
                "score": _clamp(1.0 - len(traceability_blocked_cases) / total),
                "issue_count": issue_counts["traceability"],
            },
            "coverage": {
                "score": _clamp(coverage_score),
                "issue_count": issue_counts["coverage"],
            },
        }

    def _calc_coverage_summary(self, test_cases: list[dict]) -> dict:
        """计算覆盖率汇总（按用例类型统计）。"""
        summary = {"functional": 0, "boundary": 0, "exception": 0, "security": 0, "performance": 0}
        for case in test_cases:
            case_type = case.get("type", "functional")
            if case_type in summary:
                summary[case_type] += 1
        return summary

    def _should_supplement(
        self,
        issues: list[dict],
        coverage: dict,
        srr: dict,
        test_cases: list[dict],
        point_coverage: dict | None = None,
    ) -> bool:
        """判断是否需要触发 GenAgent 补充生成。

        触发条件（按优先级）：
        - 有 blocker 级问题
        - 要点级覆盖（映射口径）：要点映射覆盖率 < 0.8 或 P0/P1 要点未 100% 覆盖
        - 要点级覆盖（回退口径）：数量比覆盖 < 0.5
        - 兜底：test_points_hint 中的类别未覆盖（v2.48 前旧机制，保留兼容）

        Args:
            issues: 问题列表
            coverage: 覆盖率汇总
            srr: SRR JSON
            test_cases: 用例列表
            point_coverage: 要点级覆盖映射结果（calc_point_coverage 输出）

        Returns:
            bool: True 表示需要补充生成
        """
        # 有 blocker → 需要补充
        if any(i.get("severity") == "blocker" for i in issues):
            return True

        # 要点级覆盖判断（RTM 映射口径）
        if point_coverage and point_coverage.get("total_points", 0) > 0:
            if point_coverage.get("mapping_based"):
                # 映射率不足 80% → 补充
                if point_coverage.get("coverage_rate", 1.0) < 0.8:
                    return True
                # P0/P1 要点必须 100% 覆盖
                if point_coverage.get("high_priority_total", 0) > 0 and \
                        point_coverage.get("high_priority_covered", 0) < point_coverage["high_priority_total"]:
                    return True
            elif point_coverage.get("coverage_rate", 1.0) < 0.5:
                # 回退口径沿用数量比阈值
                return True

        # 覆盖率检查：test_points_hint 中的类别是否都有用例覆盖（旧机制兜底）
        hints = srr.get("test_points_hint", [])
        covered_types = {c.get("type", "") for c in test_cases}
        for hint in hints:
            category = hint.get("category", "")
            if category and category not in covered_types:
                # 该类别未覆盖 → 需要补充
                return True

        return False

    def _gen_supplement_hints(
        self,
        coverage: dict,
        srr: dict,
        point_coverage: dict | None = None,
    ) -> list[dict]:
        """生成补充生成提示。

        Args:
            coverage: 覆盖率汇总
            srr: SRR JSON
            point_coverage: 要点级覆盖映射结果（用于定向补生成未覆盖要点）

        Returns:
            list[dict]: 补充提示列表
        """
        hints: list[dict] = []
        hints_by_cat = {
            "boundary": "边界值测试不足",
            "exception": "异常测试不足",
            "security": "安全测试不足",
            "performance": "性能测试不足",
        }
        for cat, reason in hints_by_cat.items():
            if coverage.get(cat, 0) == 0:
                hints.append({"case_type": cat, "reason": reason})

        # 要点级覆盖缺口：定向补生成未覆盖要点（结构化 hint，编排器映射为 coverage_gaps）
        if point_coverage and point_coverage.get("uncovered"):
            hints.append({
                "case_type": "coverage_gap",
                "reason": f"{len(point_coverage['uncovered'])} 个 SRR 要点未覆盖，需定向补生成",
                "uncovered_points": point_coverage["uncovered"][:20],
            })

        # 基于 test_points_hint 补充
        for hint in srr.get("test_points_hint", []):
            cat = hint.get("category", "")
            if cat and coverage.get(cat, 0) == 0:
                hints.append({
                    "case_type": cat,
                    "reason": f"需求测试点未覆盖: {hint.get('point', '')}",
                })
        return hints

    # ========================================================================
    # 知识库回流闭环（docs/02 §4.1 + docs/00 §3 阶段⑧）
    # ========================================================================

    async def persist_to_kb(
        self,
        srr: dict,
        reviewed_cases: list[dict],
        report: dict,
    ) -> dict:
        """将审核通过的用例回流到知识库（PG + Vector DB 双写）。

        对应 docs/02 §4.1 闭环回流流程：
        - 高置信度 (≥0.70) → 自动入库，标记 approved
        - 中置信度 (0.6~0.70) → 标记 reviewed，等待人工确认
        - 低置信度 (<0.6) 或 blocker → 记录到 error_cases

        Args:
            srr: SRR JSON（用于获取需求 ID、产品 ID 等）
            reviewed_cases: 审核后的用例列表（含 final_confidence）
            report: 审核报告

        Returns:
            dict: 回流结果统计
        """
        from sqlalchemy import select
        from app.db.database import async_session
        from app.db.models import TestCase as TestCaseModel, ErrorCase, Traceability

        req_id = srr.get("requirement", {}).get("id", "")
        product_id = srr.get("requirement", {}).get("product_id", "PROD-DEFAULT")
        modules = srr.get("requirement", {}).get("modules", [])

        # 统一批次 ID，便于后续按批次查询和追溯
        batch_id = gen_batch_id()

        stats = {
            "total": len(reviewed_cases),
            "auto_approved": 0,
            "pending_review": 0,
            "rejected": 0,
            "errors": 0,
            "vector_write_failed": 0,  # v3.1.3 P1-4: Milvus 写入失败计数，上抛可见
        }

        # 获取 blocker 用例 ID 集合
        blocked_case_ids = set()
        for issue in report.get("issues", []):
            if issue.get("severity") == "blocker":
                blocked_case_ids.add(issue.get("case_id", ""))

        # R3 修复：入库前按标题级严格去重。
        # 测试发现同批次出现重复标题（如"版本数据仅作者本人可见"出现 2 条），
        # 根因是 FP 分片生成 + 补生成各子轮独立调用 gen_agent.generate，
        # 每个子轮内部去重无法覆盖跨子轮的标题重复，且 persist_to_kb 按
        # case_id（唯一）去重也无法拦截同标题不同 ID 的用例。
        # 此处在入库前做最终防线：相同标题只保留置信度最高的一条。
        title_dedup: dict[str, dict] = {}
        for case in reviewed_cases:
            title = str(case.get("title", "")).strip()
            if not title:
                continue
            existing = title_dedup.get(title)
            if existing is None:
                title_dedup[title] = case
            else:
                conf_new = float(case.get("confidence", 0.0))
                conf_old = float(existing.get("confidence", 0.0))
                if conf_new > conf_old:
                    title_dedup[title] = case
        deduped_cases = []
        for case in reviewed_cases:
            title = str(case.get("title", "")).strip()
            if title and title in title_dedup:
                if title_dedup[title] is not case:
                    continue
            deduped_cases.append(case)
        if len(deduped_cases) < len(reviewed_cases):
            removed = len(reviewed_cases) - len(deduped_cases)
            logger.info(
                "[ReviewAgent/KB] 标题级去重: before={}, after={}, removed={}",
                len(reviewed_cases), len(deduped_cases), removed,
            )
            stats["title_dedup_removed"] = removed
        reviewed_cases = deduped_cases

        async with async_session() as session:
            for case in reviewed_cases:
                case_id = case.get("id", gen_test_case_id())
                confidence = case.get("confidence", 0.0)
                case_type = case.get("type", "functional")
                title = case.get("title", "")
                is_blocked = case_id in blocked_case_ids

                try:
                    # v4.4 修复 L2 根因：回归复用（v3.4：historical_regression 并入结果集）
                    # 的用例带历史 id，persist 对其重复 insert 触发 UniqueViolation，导致
                    # 批次整段丢失。此处先按 id 查库，已存在则判定为"已入过库"并跳过
                    # （不重复入库、不重复写 traceability/向量），避免误把复用当新生成。
                    existing_id = (
                        await session.execute(
                            select(TestCaseModel.id).where(TestCaseModel.id == case_id)
                        )
                    ).scalar_one_or_none()
                    if existing_id is not None:
                        stats["reused_skip"] = stats.get("reused_skip", 0) + 1
                        continue

                    # 用 savepoint 隔离每个用例的 DB 写入：
                    # 单个用例 flush 失败时只回滚 savepoint，
                    # 不会让 session 进入 inactive 状态影响后续用例与最终 commit
                    async with session.begin_nested():
                        if confidence >= Confidence.AUTO_APPROVE_THRESHOLD and not is_blocked:
                            # 高置信度：自动入库，标记 approved
                            # v3.5: 阈值 0.85→0.70。原 0.85 在冷启动（KB 无相似向量
                            # match_score=0、无模式命中 pattern_bonus=0）时不可达
                            # （理论上限 0.3+0.2+0.1=0.6~0.7），导致永远无 approved、
                            # 复用机制（Few-shot/回归基线）死循环
                            record = TestCaseModel(
                                id=case_id,
                                req_id=req_id,
                                product_id=product_id,
                                module=modules[0] if modules else "",
                                modules=modules,
                                title=title,
                                type=case_type,
                                priority=case.get("priority", "P1"),
                                status="approved",
                                case_json=case,
                                source="ai_generated",
                                confidence=confidence,
                                gen_batch_id=batch_id,
                            )
                            session.add(record)

                        elif Confidence.DEFAULT <= confidence < Confidence.AUTO_APPROVE_THRESHOLD or (
                            confidence >= 0.70 and is_blocked
                        ):
                            # 中置信度：标记 reviewed，等待人工确认
                            record = TestCaseModel(
                                id=case_id,
                                req_id=req_id,
                                product_id=product_id,
                                module=modules[0] if modules else "",
                                modules=modules,
                                title=title,
                                type=case_type,
                                priority=case.get("priority", "P1"),
                                status="reviewed",
                                case_json=case,
                                source="ai_generated",
                                confidence=confidence,
                                gen_batch_id=batch_id,
                            )
                            session.add(record)

                        else:
                            # 低置信度：先写入 test_cases（status=draft 未审核）以满足
                            # error_cases.original_case_id 的外键约束，再记录到 error_cases
                            record = TestCaseModel(
                                id=case_id,
                                req_id=req_id,
                                product_id=product_id,
                                module=modules[0] if modules else "",
                                modules=modules,
                                title=title,
                                type=case_type,
                                priority=case.get("priority", "P1"),
                                status="draft",
                                case_json=case,
                                source="ai_generated",
                                confidence=confidence,
                                gen_batch_id=batch_id,
                            )
                            session.add(record)
                            # v2.0 修复：手动 flush 确保 test_cases 的 INSERT 先执行，
                            # 否则 SQLAlchemy 异步 session flush 时可能不按 ForeignKey
                            # 依赖顺序执行，导致 error_cases 先插入触发外键约束失败
                            await session.flush()

                            error_id = gen_error_id()
                            error_record = ErrorCase(
                                error_id=error_id,
                                original_case_id=case_id,
                                error_type="incomplete",
                                error_detail=f"置信度过低: confidence={confidence:.3f}",
                                feedback={"reason": "confidence_below_threshold"},
                                corrected_json=case,
                            )
                            session.add(error_record)

                        # 写入追溯矩阵
                        # v1.4：若用例标注了 related_fp（功能点 ID），在 requirement_field
                        # 记录 "fp:{fp_id}"，建立 test_case↔functional_point 追溯链
                        related_fp = case.get("related_fp", "")
                        req_field = f"fp:{related_fp}" if related_fp else ""
                        # v4.3.1 修复 P7：原实现 trace_type="direct" if related_fp else "direct"
                        # 恒为 direct（恒真三目，死代码），且无 FP 关联的用例也被标记为直接
                        # 验证，语义矛盾。按 docs/02-knowledge-base.md trace_type 取值区分：
                        # 有关联 FP → direct（直接验证该功能点）；无关联 FP → indirect（间接覆盖）。
                        trace = Traceability(
                            test_case_id=case_id,
                            requirement_id=req_id,
                            requirement_field=req_field,
                            trace_type="direct" if related_fp else "indirect",
                            confidence=confidence,
                        )
                        session.add(trace)
                        # begin_nested 退出时自动 flush，失败则回滚到 savepoint

                    # savepoint 成功提交后，执行向量库写入与统计（不阻断 DB 事务）
                    if confidence >= Confidence.AUTO_APPROVE_THRESHOLD and not is_blocked:
                        try:
                            await asyncio.to_thread(
                                vector_store.add_test_case,
                                case_id=case_id,
                                case_json=case,
                                metadata={
                                    "req_id": req_id,
                                    "product_id": product_id,
                                    "case_type": case_type,
                                    "confidence": str(confidence),
                                    "gen_batch_id": batch_id,
                                },
                            )
                        except Exception as ve:
                            stats["vector_write_failed"] += 1
                            logger.warning(
                                "[ReviewAgent/KB] 向量库写入失败: case_id={}, err={}",
                                case_id, str(ve)[:80],
                            )
                        stats["auto_approved"] += 1

                    elif Confidence.DEFAULT <= confidence < Confidence.AUTO_APPROVE_THRESHOLD or (
                        confidence >= 0.70 and is_blocked
                    ):
                        stats["pending_review"] += 1

                    else:
                        # 同步写入 error_cases 向量库（供后续 ReviewAgent 学习）
                        try:
                            await asyncio.to_thread(
                                vector_store.add_error_case,
                                error_id=error_id,
                                original_case_id=case_id,
                                error_type="incomplete",
                                error_detail=f"置信度过低: confidence={confidence:.3f}",
                                corrected_json=case,
                                product_id=product_id,
                            )
                        except Exception as ve:
                            stats["vector_write_failed"] += 1
                            logger.warning(
                                "[ReviewAgent/KB] error_cases 向量库写入失败: {}", str(ve)[:80]
                            )
                        stats["rejected"] += 1

                except Exception as e:
                    logger.error(
                        "[ReviewAgent/KB] 用例 {} 入库失败: {}",
                        case_id, str(e)[:150],
                    )
                    stats["errors"] += 1

            await session.commit()

        logger.info(
            "[ReviewAgent/KB] 知识库回流完成: "
            "total={}, auto_approved={}, pending_review={}, rejected={}, errors={}, vector_write_failed={}",
            stats["total"],
            stats["auto_approved"],
            stats["pending_review"],
            stats["rejected"],
            stats["errors"],
            stats["vector_write_failed"],
        )
        if stats["vector_write_failed"]:
            # P1-4: 双写失败必须显式可见（ERROR 级 + stats 上抛），不能只有 WARNING 淹没
            logger.error(
                "[ReviewAgent/KB] ⚠ Milvus 向量写入失败 {} 条，PG 已提交但回归检索基线缺失，"
                "建议在用例审核页重新批量通过以补写向量",
                stats["vector_write_failed"],
            )
        return stats


# 全局单例
review_agent = ReviewAgent()
