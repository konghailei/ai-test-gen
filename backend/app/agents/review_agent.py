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
import re
from typing import Any

from app.core.llm_client import llm_client
from app.core.constants import Confidence
from app.db.milvus_store import vector_store
from app.models.utcf import ReviewReport
from app.prompts import review_agent as prompts
from app.core import prompt_manager
from app.utils.coverage import calc_point_coverage
from app.core.publication_policy import decide_publication
from app.utils.case_validation import validation_issues
from app.models.utcf import ReviewIssue
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class LLMReviewPayload(BaseModel):
    """Only a validated LLM review can authorize automatic publication."""
    model_config = ConfigDict(extra="allow")
    issues: list[ReviewIssue]
    avg_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)


def _validated_review_payload(result: Any, cases: list[dict]) -> dict:
    payload = LLMReviewPayload.model_validate(result)
    known_ids = {c.get("id") for c in cases}
    if any(i.case_id and i.case_id not in known_ids for i in payload.issues):
        raise ValueError("审核引用了本批次以外的用例")
    return payload.model_dump()
from app.utils.id_gen import gen_batch_id, gen_error_id, gen_test_case_id
from app.utils.logger import logger


# v3.5.10（docs/32 O-2）：NFR 阈值类要点判定（供 supplement_hints 优先位用）。
# 口径：非功能点 且 描述带数值阈值（限流 60/min、禁用 600s、召回 20 条、
# 用例上下限 40、校验 512 字节……）。纯文字 NFR（如"传输加密"）无阈值可验，
# 不算此类，仍走通用 coverage_gap 提示。
_NFR_THRESHOLD_DIGIT_RE = re.compile(r"\d")


def _is_nfr_threshold_point(point: dict) -> bool:
    if not isinstance(point, dict):
        return False
    if point.get("type") != "non_functional":
        return False
    return bool(_NFR_THRESHOLD_DIGIT_RE.search(str(point.get("desc", "") or "")))


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

    @staticmethod
    def _resolve_case_source(case: dict) -> str:
        """解析用例来源，落到 DB 列 `TestCase.source`（v3.3 修 B1）。

        ## 修的是什么（docs/14 §8.2 缺陷 3）

        此前三处落库**硬编码** `source="ai_generated"`，导致：
        - 回归复用用例由 `orchestrator._build_historical_regression` 打上的
          `case_json["source"]="reused"` **只存在于 JSON**，未落到 DB 列
        - 而 B1 的检索权重分级（`human_approved`/`ai_approved`/`reused`/`imported`）
          依赖 **DB 列**，读不到 → 分级失效
        - 实测：1704 条用例中 `source` 只有 `ai_generated`(1540) 与 `manual`(1)，
          **`reused` 一条都没有**，印证缺陷

        ## 白名单

        只接受已知来源值，其余一律回退 `ai_generated`——
        避免把 LLM 自由填写的任意字符串写进 DB 列（该列有语义含义，会被下游用于分级）。
        """
        src = str((case or {}).get("source") or "").strip()
        return src if src in ("reused", "manual", "imported", "human_approved",
                              "ai_approved") else "ai_generated"

    @staticmethod
    def _persist_route(
        auto_allowed: bool,
        confidence: float,
        is_blocked: bool,
        is_unreviewed: bool = False,
    ) -> str:
        """单条用例的落库路由（纯函数，落库分支与统计分支共用一次判定）。

        v3.5.10（docs/32 B-1 + B-2）：
        - B-1：内容级缺陷只剔坏条（persist 前已 purged），不再取消好条 auto 资格；
        - B-2：所属 FP 分片审核失败（is_unreviewed）→ 未被 LLM 审过，不进 auto，
          按置信度落 reviewed 待人工（防"未审先批"，docs/31 F1）。

        抽成纯函数的原因：原实现落库分支（begin_nested 内）与统计分支（savepoint 后）
        各写一份同型条件，改一处漏一处即"统计与落库不一致"（MEMORY 对称性陷阱）；
        收敛为一次判定后可直接单测三形态叠加。

        Returns:
            "approved"（auto 入库）| "reviewed"（待人工）| "rejected"（低置信度）
        """
        if (
            auto_allowed
            and confidence >= Confidence.AUTO_APPROVE_THRESHOLD
            and not is_blocked
            and not is_unreviewed
        ):
            return "approved"
        if (
            not auto_allowed
            or is_unreviewed
            or Confidence.DEFAULT <= confidence < Confidence.AUTO_APPROVE_THRESHOLD
            or (confidence >= 0.70 and is_blocked)
        ):
            return "reviewed"
        return "rejected"

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
        # v3.4（docs/15 §4.1 / P0-2）：合同校验**只记录不中断**。
        # 畸形/空用例的处置权属于发布策略（blocked / awaiting_human_review），
        # 在这里抛异常会让 review → done 整段消失，前端连"为什么失败"都拿不到。
        content_errors = validation_issues(test_cases)
        if content_errors:
            logger.error(
                "[ReviewAgent] ⚠️ 用例不满足可执行合同（{} 项，转发布策略判定）: {}",
                len(content_errors), content_errors[:3],
            )
        if not test_cases:
            # 空用例集不调 LLM（prompt 里没有任何用例可审），直接给出可判定的报告。
            logger.error("[ReviewAgent] ⚠️ 空用例集：跳过 LLM 审核与回流，发布决策将判 blocked")
            return {
                "total_cases": 0,
                "passed_cases": 0,
                "blocked_cases": 0,
                "issues": [],
                "coverage_summary": {},
                "avg_confidence": 0.0,
                "should_supplement": False,
                "supplement_hints": [],
                "review_status": "unverified",
                "rule_passed_cases": 0,
                "review_degraded": False,
                "review_degraded_reason": "empty_case_set",
                "content_errors": content_errors,
            }, []
        logger.info("[ReviewAgent] ▶ 开始审核: req_id={}, cases={}", req_id, len(test_cases))

        # 1. 规则预检：追溯性 + 可执行性（零成本，先于置信度/LLM，方案 B）
        rule_issues = self._rule_based_review(srr, test_cases)
        blocker_ids = {i.get("case_id", "") for i in rule_issues if i.get("severity") == "blocker"}
        rule_blockers = len(blocker_ids)
        logger.info("[ReviewAgent] 规则预检: issues={}, blockers={}",
                    len(rule_issues), rule_blockers)

        # 2. 计算 final_confidence（多因子加权；blocker 跳 KB 检索但仍算置信度，方案 B）
        reviewed_cases = await self._calculate_confidences(
            srr, test_cases, skip_search_ids=blocker_ids
        )
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

        # 3. LLM 深度审核：完整性 + 去重 + 一致性 + 覆盖率（非 blocker 子集，方案 B）
        # v3.5 方案 A：规模够大（非 blocker ≥ 24 且分片后 >1 片）走 FP 分片审核
        # （组内富信息 + 跨组集合级），小集合保留单发全量审核（避免 N+1 次小调用
        # 的固定开销反噬）。
        use_shards = self._should_use_shard_review(test_cases, blocker_ids)
        logger.info(
            "[ReviewAgent] 调用 LLM 6维度审核（剔除 blocker {} 条，路径={}）...",
            len(blocker_ids), "fp_shards" if use_shards else "single",
        )
        if use_shards:
            llm_report = await self._llm_review_shards(
                srr, reviewed_cases, exclude_case_ids=blocker_ids
            )
        else:
            llm_report = await self._llm_review(
                srr, reviewed_cases, exclude_case_ids=blocker_ids
            )
        llm_issues = llm_report.get("issues", [])
        logger.info(
            "[ReviewAgent] LLM 审核完成: path={}, issues={}, avg_confidence={}",
            "shards" if use_shards else "single",
            len(llm_issues), llm_report.get("avg_confidence", 0),
        )

        # 4. 合并审核结果（含要点级覆盖映射，口径与 GenAgent._log_coverage_stats 一致）
        point_coverage = calc_point_coverage(srr, functional_points or [], reviewed_cases)
        logger.info(
            "[ReviewAgent] 要点级覆盖[{}]: 要点总数={}, 已覆盖={}, 覆盖率={:.1%}, P0/P1={}/{}, 未覆盖={}",
            "映射" if point_coverage["mapping_based"] else "未知(仅下界)",
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
        skip_search_ids: set[str] | None = None,
    ) -> list[dict]:
        """为每个用例计算 final_confidence（方案 C：批量 KB 检索）。

        对应 docs/03 §5.2 的 calculate_confidence 函数。
        多因子加权：match_score×0.2 + completeness×0.3 + consistency×0.25 + gen_self×0.15 + pattern_bonus + quality_base

        Args:
            srr: SRR JSON 字典
            test_cases: 用例列表
            skip_search_ids: 命中该集合的用例**跳过 KB 相似检索**（match_results=[]），
                但仍照常计算置信度（走冷启动 quality_base 兜底）。用于方案 B 的 blocker
                用例——它们本就要补充生成/被拦截，无需再消耗检索 I/O，但跳过检索会让
                标题去重拿 0 分错杀，故置信度仍须计算。

        Returns:
            list[dict]: 更新后的用例列表（confidence 字段被写入 final_confidence）
        """
        skip_search_ids = skip_search_ids or set()
        reviewed: list[dict] = []
        product_id = srr.get("requirement", {}).get("product_id", "")

        # 方案 C：收集需检索的用例，一次批量 embedding + 一次多向量 search。
        # blocker 用例（skip_search_ids）已被剔除，不进检索池。
        to_search = [c for c in test_cases if c.get("id", "") not in skip_search_ids]
        match_by_id: dict[str, list[dict]] = {}
        if to_search:
            queries = [c.get("title", "") for c in to_search]
            try:
                batched = await asyncio.to_thread(
                    vector_store.search_similar_cases_batch,
                    queries, top_k=3, product_id=product_id or None,
                )
                for case, mr in zip(to_search, batched):
                    match_by_id[case.get("id", "")] = mr
            except Exception as e:
                # 整批失败 → 回退逐条（保持单条失败返回 [] 的既有行为），并打 warning
                logger.warning(
                    "[ReviewAgent] 批量置信度检索失败 → 回退逐条检索: {}", str(e)[:150]
                )
                for case in to_search:
                    match_by_id[case.get("id", "")] = await self._search_similar_for_review(
                        case, product_id=product_id
                    )

        for case in test_cases:
            case_id = case.get("id", "")
            # blocker 用例：跳过 KB 检索（方案 B），仍计算置信度
            match_results = [] if case_id in skip_search_ids else match_by_id.get(case_id, [])
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

            # 完整性（B-4，docs/32）：顶层 expected 为空记 warning（软信号，
            # 不进 blocker——docs/31 F4：133/320 空缺是质量缺口而非 schema 违规；
            # 存量不回填，仅新生成生效）
            if not (str(case.get("expected") or "").strip()):
                issues.append({
                    "case_id": case_id,
                    "dimension": "completeness",
                    "severity": "warning",
                    "message": "用例缺少顶层 expected（整体预期结果）",
                    "suggestion": "补充一句话整体预期结果，概括本用例通过后系统应处于的状态",
                })

        return issues

    # ========================================================================
    # 方案 A：FP 分片审核（v3.5，docs/KNOWN-ISSUES.md L-17 问题 2+3 前半）
    # ========================================================================

    # 分片审核的并发上限：分片数通常 ≤ FP 数（几十级），信号量防极端情况
    # （几百个未分组碎片）打爆供应商免费额度与 LLM 总预算。
    _SHARD_CONCURRENCY = 4

    # 走分片审核的规模门槛：分片后 >1 片才值得 N+1 次调用；且总用例数达到
    # 该阈值才启用（小集合单发一次就够，省固定开销）。两个条件取与。
    _SHARD_MIN_CASES = 24

    @classmethod
    def _should_use_shard_review(
        cls, test_cases: list[dict], exclude_case_ids: set[str]
    ) -> bool:
        """判断是否走 FP 分片审核（方案 A 规模开关）。

        条件：非 blocker 用例数 ≥ _SHARD_MIN_CASES **且** 按 related_fp 分片后
        分片数 > 1。单分片（全部用例同 FP 或全未标注）时分片无意义，保留单发。
        """
        non_blocker = [
            c for c in test_cases if c.get("id", "") not in (exclude_case_ids or set())
        ]
        if len(non_blocker) < cls._SHARD_MIN_CASES:
            return False
        groups = cls._group_cases_by_fp(non_blocker)
        return len(groups) > 1

    @staticmethod
    def _group_cases_by_fp(
        cases: list[dict],
    ) -> dict[str, list[dict]]:
        """按 related_fp 把用例分组（方案 A 第一步：分片）。

        Args:
            cases: 用例列表（已剔除 blocker）

        Returns:
            dict: 分片映射 {fp_id: [case, ...]}，related_fp 为空的用例进
            "" 兜底桶（线上实测 23.5% 用例无此标注）。
        """
        groups: dict[str, list[dict]] = {}
        for c in cases:
            fp_id = str(c.get("related_fp", "") or "")
            groups.setdefault(fp_id, []).append(c)
        return groups

    async def _llm_review_shards(
        self,
        srr: dict,
        test_cases: list[dict],
        exclude_case_ids: set[str],
    ) -> dict:
        """FP 分片审核调度器（方案 A 主体，v3.5 L-17）。

        替代"单次全量 call_json"：按 related_fp 分片 → 每片一次组内富信息审核
        （并发上限 _SHARD_CONCURRENCY）→ 一次跨组集合级审核（判跨组重复/矛盾/
        分布）。分片失败不静默：至少一片成功才合并结果，部分失败打
        review_shard_partial_failure（不降级——已成功分片的证据仍有效）；
        全失败才落 review_degraded 终态（fail-closed：发布策略按
        review_llm_failed 拦截自动发布）。

        Args:
            srr: SRR JSON 字典
            test_cases: 用例列表（全量，含 blocker；blocker 会被剔除）
            exclude_case_ids: 剔出 LLM 审核清单的用例 ID（方案 B 的 blocker 集合）

        Returns:
            dict: 与单发审核同形的报告（issues/avg_confidence/review_degraded/...）
        """
        exclude_case_ids = exclude_case_ids or set()
        non_blocker = [
            c for c in test_cases if c.get("id", "") not in exclude_case_ids
        ]

        if not non_blocker:
            # 全是 blocker / 空集合：无 LLM 可审内容，但不算降级（方案 B 语义：
            # blocker 已在规则预检被拦截，非"审核能力缺失"）
            return {"issues": [], "avg_confidence": 0.0, "review_degraded": False}

        # ---- 1. 分片（未分组兜底桶排最后） ----
        groups = self._group_cases_by_fp(non_blocker)
        fp_ids = sorted(groups.keys(), key=lambda k: (k == "", k))

        # ---- 2. 评审供应商只读一次（分片化后若每片都读会放大 N 倍 DB 往返） ----
        judge_provider: str | None = None
        try:
            from app.db.database import async_session as _as
            from app.api.settings import get_review_llm_provider
            async with _as() as _s:
                judge_provider = await get_review_llm_provider(_s)
        except Exception as e:
            logger.debug(
                "[ReviewAgent] 读取评审供应商设置失败（跟随主力）: {}", str(e)[:100]
            )

        async def _run_shard(fp_id: str, shard_cases: list[dict]) -> tuple[str, dict | None, str]:
            """跑一个分片：组内富信息审核。返回 (fp_id, 结果或 None, 错误信息)。"""
            async with sem:
                gen_confs = [
                    c.get("gen_metadata", {}).get("gen_confidence", 0.5)
                    for c in shard_cases
                ]
                prompt = prompts.build_group_review_prompt(
                    srr, fp_id, "", shard_cases, gen_confs
                )
                kwargs = dict(
                    prompt=prompt,
                    system_prompt=prompt_manager.get("review_agent", "SYSTEM_PROMPT"),
                    schema_hint={"issues": list, "avg_confidence": float},
                    temperature=0.2,
                    agent_name="ReviewAgent",
                )

                async def _call() -> dict:
                    if judge_provider:
                        try:
                            r = await self._llm.call_json(**kwargs, provider=judge_provider)
                            return _validated_review_payload(r, shard_cases)
                        except Exception as e:
                            logger.warning(
                                "[ReviewAgent] 分片 {} 评审供应商 {} 失败，回退主力池: {}",
                                fp_id, judge_provider, str(e)[:120],
                            )
                    r = await self._llm.call_json(**kwargs)
                    return _validated_review_payload(r, shard_cases)

                try:
                    return fp_id, await _call(), ""
                except Exception as e:
                    logger.error(
                        "[ReviewAgent] 分片 {} 组内审核失败: {}", fp_id, str(e)[:150]
                    )
                    return fp_id, None, str(e)[:150]

        sem = asyncio.Semaphore(self._SHARD_CONCURRENCY)
        shard_results = await asyncio.gather(
            *(_run_shard(fid, groups[fid]) for fid in fp_ids)
        )
        ok_shards = [r for r in shard_results if r[1] is not None]
        failed_shards = [r for r in shard_results if r[1] is None]

        # ---- 3. 跨组集合级审核（仅当分片数 > 1，且至少一片成功） ----
        cross_issues: list[dict] = []
        if ok_shards and len(fp_ids) > 1:
            known_ids = {c.get("id", "") for c in non_blocker}
            type_dists: dict[str, dict[str, int]] = {
                fid: {} for fid in fp_ids
            }
            for c in non_blocker:
                fid = str(c.get("related_fp", "") or "")
                t = str(c.get("type", "") or "functional")
                type_dists[fid][t] = type_dists[fid].get(t, 0) + 1
            group_summaries = [
                {
                    "fp_id": fid or "（未分组）",
                    "case_count": len(groups[fid]),
                    "type_distribution": type_dists.get(fid, {}),
                    "titles": [str(c.get("title", "")) for c in groups[fid]],
                }
                for fid in fp_ids
            ]
            try:
                cross_kwargs = dict(
                    prompt=prompts.build_cross_group_review_prompt(srr, group_summaries),
                    system_prompt=prompt_manager.get("review_agent", "SYSTEM_PROMPT"),
                    schema_hint={"issues": list},
                    temperature=0.2,
                    agent_name="ReviewAgent",
                )
                cross_raw = (
                    await self._llm.call_json(**cross_kwargs, provider=judge_provider)
                    if judge_provider
                    else await self._llm.call_json(**cross_kwargs)
                )
                # 跨组 issues 与组内同合同校验（补 avg_confidence 占位以满足
                # LLMReviewPayload；case_id 未知即拒收，防 LLM 编造）
                payload_raw = dict(cross_raw) if isinstance(cross_raw, dict) else {}
                payload_raw.setdefault("avg_confidence", 0.5)
                cross_payload = LLMReviewPayload.model_validate(payload_raw)
                cross_issues = [
                    i.model_dump() for i in cross_payload.issues
                    if not i.case_id or i.case_id in known_ids
                ]
            except Exception as e:
                # 跨组审核失败不算全链路失败：组内证据已足够支撑报告，不静默
                logger.warning(
                    "[ReviewAgent] 跨组集合级审核失败（组内证据保留）: {}", str(e)[:150]
                )
                cross_issues = [{
                    "case_id": "",
                    "dimension": "consistency",
                    "severity": "info",
                    "message": f"跨组集合级审核未完成: {str(e)[:80]}",
                    "suggestion": "建议人工抽查跨功能点重复与组合一致性",
                }]
        else:
            logger.debug("[ReviewAgent] 分片数 <=1 或无成功分片，跳过跨组审核")

        # ---- 4. 终态判定（fail-closed 落终态，不抛异常） ----
        if not ok_shards:
            logger.error(
                "[ReviewAgent] ⛔ FP 分片审核全部失败（{} 片）→ review_degraded",
                len(fp_ids),
            )
            return {
                "issues": [],
                "avg_confidence": 0.0,
                "review_degraded": True,
                "review_degraded_reason": (
                    f"shard_review_all_failed: "
                    f"{'; '.join(e[:60] for _, _, e in failed_shards)}"
                ),
                # v3.5.10（docs/32 B-2 / F1）：全失败也要透出分片证据——
                # 否则 persist 侧 unreviewed_fp_ids 拿不到失败清单，
                # "未审用例"会漏拦（与部分失败同因，只是范围更大）。
                "shard_review": {
                    "total_shards": len(fp_ids),
                    "ok_shards": 0,
                    "failed_shards": [
                        {"fp_id": fid, "error": err} for fid, _, err in failed_shards
                    ],
                },
            }

        merged_issues: list[dict] = []
        for _, res, _err in ok_shards:
            merged_issues.extend(res.get("issues", []))
        merged_issues.extend(cross_issues)

        # avg_confidence：LLM 自评估口径，分片均值按用例数加权（每条用例权重相同，
        # 与单发口径一致）
        total_conf = 0.0
        weight_sum = 0
        for fid, res, _err in ok_shards:
            n = len(groups[fid])
            total_conf += float(res.get("avg_confidence", 0)) * n
            weight_sum += n
        avg_conf = (total_conf / weight_sum) if weight_sum else 0.0

        ok_ids = {fid for fid, _, _ in ok_shards}
        partial = [fid for fid in fp_ids if fid not in ok_ids]
        report: dict = {
            "issues": merged_issues,
            "avg_confidence": round(avg_conf, 3),
            "review_degraded": False,
            "review_status": "complete",
            "shard_review": {
                "total_shards": len(fp_ids),
                "ok_shards": len(ok_shards),
                "failed_shards": [
                    {"fp_id": fid, "error": err} for fid, _, err in failed_shards
                ],
            },
        }
        if partial:
            # 部分失败：已成功分片的证据仍有效，不算降级，但必须可见
            report["review_shard_partial_failure"] = True
            logger.warning(
                "[ReviewAgent] ⚠️ 分片审核部分失败: {}/{} 成功，失败分片={}",
                len(ok_shards), len(fp_ids), partial,
            )
        return report

    # ========================================================================
    # LLM 深度审核
    # ========================================================================

    async def _llm_review(
        self,
        srr: dict,
        test_cases: list[dict],
        exclude_case_ids: set[str] | None = None,
    ) -> dict:
        """调用 LLM 执行 6 维度深度审核。

        Args:
            srr: SRR JSON 字典
            test_cases: 用例列表
            exclude_case_ids: 从 LLM 审核清单中剔除的用例 ID（方案 B：blocker 用例已在
                _rule_based_review 被拦截，无需再送 LLM 复核）。注意 report 的全量口径
                （total_cases/blocked_cases/avg_confidence）由 _merge_review_results 保证，
                此处仅缩减 LLM 输入。

        Returns:
            dict: LLM 审核报告
        """
        # 方案 B：blocker 用例已从 LLM 审核清单剔除（它们在 _rule_based_review 已被拦截，
        # 送 LLM 复核是浪费；report 全量口径由 _merge_review_results 用全量 reviewed_cases 保证）。
        exclude_case_ids = exclude_case_ids or set()
        non_blocker = [
            c for c in test_cases if c.get("id", "") not in exclude_case_ids
        ]

        # 提取 gen_confidence 列表（仅作 LLM 参考，基于非 blocker 子集）
        gen_confidences = [
            c.get("gen_metadata", {}).get("gen_confidence", 0.5)
            for c in non_blocker
        ]

        user_prompt = prompts.build_user_prompt(srr, non_blocker, gen_confidences)

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
                return _validated_review_payload(result, non_blocker)
            except Exception as e:
                logger.warning(
                    "[ReviewAgent] 评审供应商 {} 失败，回退主力池: {}",
                    judge_provider, str(e)[:150],
                )
        try:
            result = await self._llm.call_json(**_review_kwargs)
            return _validated_review_payload(result, non_blocker)
        except Exception as e:
            # v3.3（修 N8）：LLM 审核失败 → 规则降级。
            #
            # ⚠️ 降级结果看起来是「**全部通过、0 拦截、不补生成**」——
            # 与"审核真的通过了"在数据上无法区分。必须显式标记，
            # 否则"审核能力静默缺失"会被当成"质量很好"（同 N7 的假象模式）。
            logger.error(
                "[ReviewAgent] LLM 审核失败 → 本次审核降级为规则预检（N8）: {}", str(e)[:200]
            )
            return {
                "total_cases": len(test_cases),
                "passed_cases": 0,
                "blocked_cases": 0,
                "issues": [],
                "coverage_summary": {},
                "avg_confidence": 0.0,
                "should_supplement": False,
                "supplement_hints": [],
                # 降级标记：由 orchestrator 透传进 done 事件的 degraded_flags
                "review_degraded": True,
                "review_degraded_reason": str(e)[:200],
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

        # v3.1.3 维度级置信度（评审报告中心 P1）：确定性规则聚合，不调 LLM
        dimension_scores = self._calc_dimension_scores(
            test_cases, all_issues, point_coverage
        )

        # 判断是否需要补充生成
        # A failed judge cannot provide trustworthy optimization feedback.
        should_supplement = (not llm_report.get("review_degraded") and
            self._should_supplement(all_issues, coverage, srr, test_cases, point_coverage=point_coverage))
        if llm_report.get("review_degraded"):
            for dim in ("consistency", "deduplication"):
                dimension_scores[dim]["score"] = None
                dimension_scores[dim]["status"] = "unknown"

        # 补充提示
        supplement_hints = (
            self._gen_supplement_hints(coverage, srr, point_coverage=point_coverage)
            if should_supplement else []
        )

        # v3.4：合同问题随最终报告上抛，供发布策略判 blocked
        # （只记录不中断——畸形用例不能让 review → done 整段消失）
        content_errors = validation_issues(test_cases)

        return {
            "total_cases": total,
            "passed_cases": 0 if llm_report.get("review_degraded") else passed,
            "blocked_cases": blocked,
            "issues": all_issues,
            "coverage_summary": coverage,
            "point_coverage": point_coverage,
            "dimension_scores": dimension_scores,
            "avg_confidence": round(avg_conf, 3),
            # v3.3（修 N8）：把 `_llm_review` 的降级标记**透传到最终 report**。
            # ⚠️ 不能只在 `_llm_review` 的返回值里打标——该返回值只是中间结果，
            # 会被本函数合并掉。实测探针正是靠这一点发现首版补丁无效的。
            # v3.5.10（docs/32 B-2 / F1）：分片审核证据同理透传——原实现
            # `shard_review` / `review_shard_partial_failure` 在合并时被丢弃，
            # 失败分片「终态不可见」（docs/31 F1：13 条未审用例全部 approved）。
            # review_status="partial"（非 complete）复用 publication_policy
            # 既有 `review_not_verified` 判定，批次自动落入 human_review。
            "review_status": (
                "degraded" if llm_report.get("review_degraded")
                else "partial" if llm_report.get("review_shard_partial_failure")
                else "complete"
            ),
            "shard_review": llm_report.get("shard_review"),
            "review_shard_partial_failure": bool(
                llm_report.get("review_shard_partial_failure")
            ),
            "rule_passed_cases": passed,
            # v3.4：合同问题随报告上抛，供发布策略判 blocked（不在此处中断流水线）
            "content_errors": content_errors,
            "review_degraded": bool(llm_report.get("review_degraded")),
            "review_degraded_reason": llm_report.get("review_degraded_reason", ""),
            "should_supplement": should_supplement,
            "supplement_hints": supplement_hints,
        }

    def _calc_dimension_scores(
        self,
        test_cases: list[dict],
        issues: list[dict],
        point_coverage: dict | None = None,
    ) -> dict:
        """计算 6 维度置信度（v3.1.3 评审报告中心 P1，纯规则聚合，不调 LLM）。

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

        # Missing evidence is unknown; do not manufacture full marks.
        coverage_unknown = not point_coverage or point_coverage.get("coverage_status") != "mapped"
        # 覆盖维度若有 issue（如 test_points_hint 类别未覆盖）则拉低 coverage
        if issue_counts["coverage"] > 0:
            coverage_score = min(coverage_score, 1.0 - min(1.0, issue_counts["coverage"] / total))

        # B-4（docs/32，docs/31 F4 口径盲区）：completeness 口径拆两层透出——
        # completeness_step（step 级字段完整率）与 completeness_top（顶层
        # expected 非空率）。原 completeness score 是要点覆盖率，看不出缺口在
        # 哪一层（v3.0 score 0.785 但 step 层零缺失、顶层 expected 74.5% 空）。
        n_cases = max(len(test_cases), 1)
        n_step_ok = sum(
            1 for c in test_cases
            if (c.get("steps") or [])
            and all(s.get("action") and s.get("expected") for s in c["steps"] if isinstance(s, dict))
        )
        n_top_ok = sum(
            1 for c in test_cases if str(c.get("expected") or "").strip()
        )
        completeness_step = _clamp(n_step_ok / n_cases)
        completeness_top = _clamp(n_top_ok / n_cases)

        return {
            "completeness": {
                "score": None if coverage_unknown else _clamp(completeness_score),
                "status": "unknown" if coverage_unknown else "measured",
                "issue_count": issue_counts["completeness"],
                # B-4：口径拆层（结构性事实，恒有值，不受 coverage_unknown 影响）
                "completeness_step": completeness_step,
                "completeness_top": completeness_top,
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
                "score": None if coverage_unknown else _clamp(coverage_score),
                "status": "unknown" if coverage_unknown else "measured",
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
            else:
                # v3.4（docs/15 §4.7）：覆盖"未知"（标注率 < 30%）时不再用数量比
                # 冒充覆盖率，而是请求**一次定向补生成**去补齐证据 ——
                # 宁可多花一轮，也不能把"没标注"读成"覆盖良好"。
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
            uncovered = list(point_coverage["uncovered"])
            # v3.5.10（docs/32 O-2）：NFR 阈值类要点（非功能点且描述带数值阈值，
            # 如限流 60/min、禁用 600s、召回 20 条、用例上下限 40、校验 512 字节）
            # 零命中是本轮实证的系统性缺口——泛化补生成常只补 happy path，
            # 阈值边界/超限验证没人管。放 hints 首位定向提示（coverage_gap 优先位）。
            nfr_points = [p for p in uncovered if _is_nfr_threshold_point(p)]
            rest = [p for p in uncovered if not _is_nfr_threshold_point(p)]
            if nfr_points:
                hints.insert(0, {
                    "case_type": "coverage_gap",
                    "priority": "nfr_threshold",
                    "reason": (
                        f"{len(nfr_points)} 个 NFR 阈值要点未覆盖"
                        f"（限流/禁用/召回/上下限/校验等带数值阈值的验证），"
                        f"优先定向补生成阈值边界与超限用例"
                    ),
                    "uncovered_points": nfr_points[:20],
                })
            if rest:
                hints.append({
                    "case_type": "coverage_gap",
                    "reason": f"{len(rest)} 个 SRR 要点未覆盖，需定向补生成",
                    "uncovered_points": rest[:20],
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
        *,
        publication_context: dict | None = None,
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
            "vector_write_failed": 0,
            # v3.4（docs/15 §4.4）：向量投递已改为"同事务入出箱 + relay 至少一次投递"，
            # 这里统计的是**入箱成功**的条数（真正的 Milvus 写入由出箱完成，积压可在
            # /observability 与 reconcile 里看到）。刻意不把 pending 当成"写入失败"，
            # 否则每次运行都会被判 kb_publication_incomplete。
            "vector_pending": 0,
        }

        # Re-evaluate at the side-effect boundary; a caller-supplied 'approved'
        # string cannot bypass missing evidence or a degraded review.
        context = publication_context or {}
        decision = decide_publication(
            report, reviewed_cases,
            gate_result=context.get("gate_result"),
            forced_pass=bool(context.get("forced_pass")),
            degraded_flags=context.get("degraded_flags"),
            failed_subtasks=context.get("failed_subtasks"),
            content_errors=validation_issues(reviewed_cases),
        )
        stats["publication"] = decision
        report["publication"] = decision
        if decision["decision"] == "blocked":
            # v3.4（docs/15 §4.1）：副作用边界的二次防线 —— 必须"静默不写"，
            # 不能抛异常。调用方（编排层）已据同一决策跳过回流；此处再兜一层，
            # 保证即使被直接调用也不会把畸形/降级产物写进权威知识库。
            stats["skipped"] = True
            stats["skip_reason"] = "publication_blocked"
            stats["blocked_reasons"] = decision.get("reasons", [])
            logger.error(
                "[ReviewAgent] ⛔ 发布决策=blocked，拒绝写入知识库（PG 与 Milvus 均未写入）: {}",
                decision.get("reasons"),
            )
            return stats
        # v3.5.2（docs/21 方案B"剔坏留好"）：partial_block 时先剔除内容级坏条
        # （进 error_cases 隔离），剩余条按置信度正常分级入库。
        # 坏条来源：① 审核 blocker issues（带 case_id，可精确定位）
        #           ② validation_issues 的 cases[N] 路径（结构合同失败）
        purged_ids: set[str] = set()
        purged_cases: list[dict] = []
        if decision["decision"] == "partial_block":
            for issue in report.get("issues", []):
                if isinstance(issue, dict) and issue.get("severity") == "blocker":
                    cid = issue.get("case_id", "")
                    if cid:
                        purged_ids.add(cid)
            content_errors = validation_issues(reviewed_cases)
            case_path_re = re.compile(r"^cases\[(\d+)\]")
            for err in content_errors:
                m = case_path_re.match(str(err))
                if m and int(m.group(1)) < len(reviewed_cases):
                    cid = reviewed_cases[int(m.group(1))].get("id", "")
                    if cid:
                        purged_ids.add(cid)
            if purged_ids and reviewed_cases:
                kept = [c for c in reviewed_cases if c.get("id", "") not in purged_ids]
                purged_cases = [c for c in reviewed_cases if c.get("id", "") in purged_ids]
                if not kept:
                    # 兜底 fail-closed：剔除后无存活条 → 退化为整批拒绝
                    stats["skipped"] = True
                    stats["skip_reason"] = "publication_blocked_after_purge"
                    stats["blocked_reasons"] = decision.get("case_level_reasons", [])
                    logger.error(
                        "[ReviewAgent] ⛔ partial_block 剔除后无存活用例，退化为整批拒绝: {}",
                        sorted(purged_ids),
                    )
                    return stats
                stats["purged_cases"] = len(purged_cases)
                stats["purged_case_ids"] = sorted(purged_ids)
                logger.warning(
                    "[ReviewAgent] ✂ 发布决策=partial_block，剔除 {} 条坏条进 error_cases"
                    "（{} 条好条按置信度正常入库）: {}",
                    len(purged_cases), len(kept), sorted(purged_ids),
                )
                reviewed_cases = kept
            else:
                # 无法定位坏条（理论上不该发生）：fail-closed 保持整批拒绝
                stats["skipped"] = True
                stats["skip_reason"] = "publication_blocked_unlocalizable"
                stats["blocked_reasons"] = decision.get("case_level_reasons", [])
                logger.error(
                    "[ReviewAgent] ⛔ partial_block 但无法定位坏条，保持整批拒绝（fail-closed）"
                )
                return stats
        # v3.5.4（D-3 终版）：auto_allowed 由「决策类型白名单」改为「内容级缺陷判定」。
        # 连坐问题的完整形态（docs/21 §5）：
        #   - 初版：仅 auto_approve 可 auto → partial_block 批次全部降级（v3.5.2 已修）
        #   - 残留：quality_gate_forced / human_review 等批次级信号继续连坐——
        #     v2.0 实证 31 条置信度 0.831-0.922（全部 ≥0.70 auto 档）且
        #     case_level_reasons=[] 却全部 reviewed。
        # 本质：批次级信号（gate 强制放行、覆盖率不足）描述的是批次处境，
        # 与单条用例自身质量无关；内容级缺陷已由 case_level_reasons 独立判定。
        # 故：无内容级缺陷（case_level_reasons 为空）→ 高置信条照常 auto；
        #     有内容级缺陷 → 维持人工审查（防线不放松）。
        # v3.5.10（docs/32 B-1，F6 终版）：第三层连坐残留关闭——
        # partial_block 时 case_level_reasons 非空也不再取消好条 auto 资格：
        # 坏条已在上文 purged 剔除（剔坏留好），blocker 漏剔条由 is_blocked
        # 独立拦截，未审条由 is_unreviewed（B-2 fail-closed）拦截——
        # 三条防线收敛到单条维度后，批次级信号不再有连坐的正当性。
        auto_allowed = True

        # 获取 blocker 用例 ID 集合
        blocked_case_ids = set()
        for issue in report.get("issues", []):
            if issue.get("severity") == "blocker":
                blocked_case_ids.add(issue.get("case_id", ""))

        # v3.5.10（docs/32 B-2 第二步 / F1 fail-closed）：分片审核失败的 FP 集合。
        # 失败分片的用例**没被 LLM 审过**，不能进 auto（防"未审先批"）——
        # docs/31 F1 实证 13 条未审用例全部 approved。它们按置信度落 reviewed
        # 待人工。口径与 _group_cases_by_fp 的分组键一致（str(related_fp or "")，
        # 注意 related_fp 可能为 None，不能用 str(x, "") 直接兜）。
        unreviewed_fp_ids = {
            str(f.get("fp_id", "") or "")
            for f in (report.get("shard_review") or {}).get("failed_shards", [])
            if isinstance(f, dict)
        }

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
            # v3.5.2（方案B"剔坏留好"）：partial_block 剔除的坏条先落 error_cases
            # （隔离留档，供后续 ReviewAgent 学习与提示词改进），不进主用例库。
            # 外键顺序：先 flush test_cases 的 draft 占位行，再写 error_cases。
            if purged_cases:
                for pcase in purged_cases:
                    pid = pcase.get("id", "")
                    try:
                        existing_pid = (
                            await session.execute(
                                select(TestCaseModel.id).where(TestCaseModel.id == pid)
                            )
                        ).scalar_one_or_none()
                        if existing_pid is None:
                            session.add(TestCaseModel(
                                id=pid,
                                req_id=req_id,
                                product_id=product_id,
                                module=modules[0] if modules else "",
                                modules=modules,
                                title=pcase.get("title", ""),
                                type=pcase.get("type", "functional"),
                                priority=pcase.get("priority", "P1"),
                                status="draft",
                                case_json={**pcase, "publication": {**decision, "purged": True}},
                                source=self._resolve_case_source(pcase),
                                confidence=pcase.get("confidence", 0.0),
                                gen_batch_id=batch_id,
                            ))
                            await session.flush()
                        perr_reason = "；".join(decision.get("case_level_reasons", [])) or "content_blocker"
                        session.add(ErrorCase(
                            error_id=gen_error_id(),
                            original_case_id=pid,
                            error_type="blocked_content",
                            error_detail=f"发布门禁 partial_block 剔除: {perr_reason}",
                            feedback={"reason": "partial_block_purge",
                                      "case_level_reasons": decision.get("case_level_reasons", [])},
                            corrected_json=pcase,
                        ))
                        stats["purged_recorded"] = stats.get("purged_recorded", 0) + 1
                    except Exception as pe:
                        # 坏条留档失败不阻断好条入库，但必须可见
                        logger.error(
                            "[ReviewAgent/KB] partial_block 坏条 {} 留档失败（不阻断）: {}",
                            pid, str(pe)[:120],
                        )
                        stats["purged_record_errors"] = stats.get("purged_record_errors", 0) + 1

            for case in reviewed_cases:
                case_id = case.get("id", gen_test_case_id())
                confidence = case.get("confidence", 0.0)
                case_type = case.get("type", "functional")
                title = case.get("title", "")
                is_blocked = case_id in blocked_case_ids
                # v3.5.10（docs/32 B-2 fail-closed）：所属 FP 分片审核失败
                # → 该用例未被 LLM 审过，不进 auto（防"未审先批"）。
                # 同因延伸：review_degraded（单发整体失败/分片全失败）时
                # **没有任何用例被 LLM 审过**，整批视同未审——否则该路径
                # 无 shard_review 清单可拦，auto_allowed=True 后高置信条
                # 会漏网（与 F1 同根因的 fail-open）。
                is_unreviewed = bool(report.get("review_degraded")) or (
                    str(case.get("related_fp", "") or "") in unreviewed_fp_ids
                )
                # v3.5.10：落库/统计共用一次路由判定（_persist_route 纯函数），
                # 杜绝两处同型条件漂移（MEMORY 对称性陷阱）。
                route = self._persist_route(
                    auto_allowed, confidence, is_blocked, is_unreviewed
                )

                # Persist the server decision on the artifact too, so a later
                # page load/export cannot mistake a draft for approved content.
                case = {**case, "publication": decision}
                try:
                    # v3.1.3 修复 L2 根因：回归复用（v3.4：historical_regression 并入结果集）
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
                        if route == "approved":
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
                                source=self._resolve_case_source(case),
                                confidence=confidence,
                                gen_batch_id=batch_id,
                            )
                            session.add(record)
                            # v3.4（docs/15 §4.4）：把"要写进 Milvus"的意图与业务行
                            # **同一事务**落出箱。原实现是事务外直接写 Milvus（且早于 PG
                            # commit）→ 失败即"PG 有、向量没有"的静默不一致，用例侧还没有
                            # vectorization_status 可查。现在 commit 后由 relay 至少一次投递，
                            # 幂等 upsert，失败进重试/对账。
                            try:
                                from app.core.vector_outbox import enqueue as _enqueue_outbox
                                _enqueue_outbox(
                                    session,
                                    aggregate_id=case_id,
                                    action="upsert",
                                    payload={
                                        "case_json": case,
                                        "metadata": {
                                            "req_id": req_id,
                                            "product_id": product_id,
                                            "project_id": (srr.get("requirement", {}) or {}).get("project_id", ""),
                                            "case_type": case_type,
                                            "confidence": str(confidence),
                                            "gen_batch_id": batch_id,
                                        },
                                    },
                                    created_by="pipeline",
                                )
                                stats["vector_pending"] += 1
                            except Exception as oe:
                                stats["vector_write_failed"] += 1
                                logger.warning(
                                    "[ReviewAgent/KB] 向量出箱入队失败（不阻断入库）: case_id={}, err={}",
                                    case_id, str(oe)[:120],
                                )

                        elif route == "reviewed":
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
                                source=self._resolve_case_source(case),
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
                                source=self._resolve_case_source(case),
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
                        # v3.1.3 修复 P7：原实现 trace_type="direct" if related_fp else "direct"
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

                    # v3.4（docs/15 §4.4）：这里不再"事务外直接写 Milvus"——
                    # 投递意图已在上面的 savepoint 内入出箱（与 TestCase 同事务提交），
                    # 由 core/vector_outbox 的 relay 至少一次投递（幂等 upsert）。
                    # 因此本处只做统计：入箱失败才算 vector_write_failed，真正的
                    # "向量暂时没写成功"是出箱待投递（vector_pending），会自愈而不是报错。
                    # 统计分支与落库分支共用同一 route（_persist_route 单次判定），
                    # 统计不漂移（原两处同型条件的对称性陷阱收敛）。
                    if route == "approved":
                        stats["auto_approved"] += 1

                    elif route == "reviewed":
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
